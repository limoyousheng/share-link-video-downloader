from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import (
    BASE_DIR,
    BROWSER_FALLBACK_ENABLED,
    DB_PATH,
    JOBS_DIR,
    ensure_directories,
    get_allowed_hosts,
)
from app.schemas import JobCreate
from app.services.job_manager import JobManager, JobQueueFull, TERMINAL_STATUSES
from app.services.share_parser import ShareParseError
from app.store import JobStore


STATIC_DIR = BASE_DIR / "app" / "static"
SESSION_COOKIE = "video_session"
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PUBLIC_URL = os.getenv("DOUYIN_PUBLIC_URL", "").strip().rstrip("/")
_AUDIO_KINDS = {"speech_only", "speech_background", "non_speech", "no_audio", "unknown"}
_AUDIO_LABELS = {
    "speech_only": "讲话",
    "speech_background": "讲话 + 音乐/背景声",
    "non_speech": "音乐/其他声音",
    "no_audio": "无声音",
    "unknown": "未能判定",
}

ensure_directories()
store = JobStore(DB_PATH)
manager = JobManager(store)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.start()
    try:
        yield
    finally:
        manager.stop()


app = FastAPI(
    title="视频下载",
    version="2.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_allowed_hosts())
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.lower().rstrip("."), port
    except ValueError:
        return None


def _is_same_origin(value: str, request: Request) -> bool:
    candidate = _origin(value)
    request_origin = _origin(f"{request.url.scheme}://{request.headers.get('host', '')}")
    allowed = {item for item in (request_origin, _origin(PUBLIC_URL)) if item is not None}
    return candidate is not None and candidate in allowed


def _session_token(request: Request) -> tuple[str, bool]:
    current = request.cookies.get(SESSION_COOKIE, "")
    if SESSION_PATTERN.fullmatch(current):
        return current, False
    return secrets.token_urlsafe(32), True


@app.middleware("http")
async def security_and_session(request: Request, call_next):
    token, is_new_session = _session_token(request)
    request.state.owner_hash = hashlib.sha256(token.encode("ascii")).hexdigest()

    response = None
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        cross_site = request.headers.get("sec-fetch-site", "").lower() == "cross-site"
        if cross_site or (origin and not _is_same_origin(origin, request)) or (
            referer and not _is_same_origin(referer, request)
        ):
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "SAME_ORIGIN_REQUIRED",
                        "message": "接口拒绝了跨站请求。",
                    }
                },
            )
    if response is None:
        response = await call_next(request)

    if is_new_session:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def serialize_job(job: dict) -> dict:
    metadata = job.get("metadata") or {}
    transcript = metadata.get("transcript") or {}
    raw_audio = metadata.get("audio_analysis") or {}
    published_text = metadata.get("description") or metadata.get("title") or ""
    speech_text = transcript.get("text") or ""
    if not isinstance(published_text, str):
        published_text = ""
    if not isinstance(speech_text, str):
        speech_text = ""
    speech_source = transcript.get("source")
    if not isinstance(speech_source, str):
        speech_source = None
    transcription_requested = bool((job.get("options") or {}).get("transcribe"))
    terminal = job.get("status") in TERMINAL_STATUSES
    audio_kind = raw_audio.get("kind")
    if not isinstance(audio_kind, str) or audio_kind not in _AUDIO_KINDS:
        audio_kind = "unknown"
    raw_audio_status = raw_audio.get("status")
    if raw_audio_status == "ready":
        audio_status = "ready"
    elif raw_audio_status == "unavailable":
        audio_status = "unavailable"
    elif not transcription_requested:
        audio_status = "not_requested"
    elif terminal:
        audio_status = "unavailable"
    else:
        audio_status = "pending"

    music_title = metadata.get("music")
    if not isinstance(music_title, str):
        music_title = ""
    music_artists = metadata.get("music_artists")
    if not isinstance(music_artists, list):
        music_artists = []
    music_artists = [
        item.strip()[:200]
        for item in music_artists
        if isinstance(item, str) and item.strip()
    ][:10]

    if speech_text:
        speech_status = "ready"
    elif audio_status == "ready" and audio_kind in {"non_speech", "no_audio"}:
        speech_status = "not_present"
    elif not transcription_requested:
        speech_status = "not_requested"
    elif terminal:
        speech_status = "unavailable"
    else:
        speech_status = "pending"
    video = next(
        (item for item in job.get("artifacts") or [] if item.get("key") == "video"),
        None,
    )
    artifacts = []
    if video:
        artifacts.append(
            {
                "key": "video",
                "kind": "video",
                "size": video.get("size"),
                "mime": video.get("mime"),
                "url": f"/api/v1/jobs/{job['id']}/video",
                "download_url": f"/api/v1/jobs/{job['id']}/download",
            }
        )
    return {
        "id": job["id"],
        "status": job.get("status"),
        "phase": job.get("phase"),
        "progress": job.get("progress"),
        "message": job.get("message"),
        "error_message": job.get("error_message"),
        "error_code": job.get("error_code"),
        "copy": {
            "published_text": published_text.strip()[:50_000],
            "speech_text": speech_text.strip()[:100_000],
            "speech_status": speech_status,
            "speech_source": speech_source,
        },
        "audio": {
            "status": audio_status,
            "kind": audio_kind,
            "label": _AUDIO_LABELS[audio_kind],
            "music_title": music_title.strip()[:500],
            "music_artists": music_artists,
        },
        "artifacts": artifacts,
        "terminal": terminal,
    }


def _owned_job(request: Request, job_id: str) -> dict:
    job = store.get_for_owner(job_id, request.state.owner_hash)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={"code": "JOB_NOT_FOUND", "message": "任务不存在。"},
        )
    return job


def _video_path(job: dict) -> tuple[Path, dict]:
    artifact = next(
        (item for item in job.get("artifacts") or [] if item.get("key") == "video"),
        None,
    )
    if not artifact:
        raise HTTPException(
            status_code=404,
            detail={"code": "VIDEO_NOT_READY", "message": "视频尚未准备完成。"},
        )

    job_dir = (JOBS_DIR / job["id"]).resolve()
    path = (job_dir / str(artifact.get("filename") or "")).resolve()
    if path.parent != job_dir or not path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "VIDEO_NOT_FOUND", "message": "视频文件不存在。"},
        )
    return path, artifact


@app.post("/api/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(payload: JobCreate, request: Request) -> dict:
    options = {
        "download_video": True,
        "transcribe": True,
        "browser_fallback": BROWSER_FALLBACK_ENABLED,
        "asr_model": "medium",
        "language": "zh",
    }
    try:
        job = manager.create(payload.share_text, options, request.state.owner_hash)
    except ShareParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except JobQueueFull as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "QUEUE_FULL", "message": str(exc)},
        ) from exc
    return serialize_job(job)


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    return serialize_job(_owned_job(request, job_id))


@app.get("/api/v1/jobs/{job_id}/video")
def preview_video(job_id: str, request: Request) -> FileResponse:
    path, artifact = _video_path(_owned_job(request, job_id))
    return FileResponse(path=path, media_type=artifact.get("mime") or "video/mp4")


@app.get("/api/v1/jobs/{job_id}/download")
def download_video(job_id: str, request: Request) -> FileResponse:
    path, artifact = _video_path(_owned_job(request, job_id))
    return FileResponse(
        path=path,
        media_type=artifact.get("mime") or "application/octet-stream",
        filename=str(artifact.get("display_name") or path.name),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    logger.error(
        "Unhandled API error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "INTERNAL_ERROR", "message": "服务遇到未预期错误。"}},
    )
