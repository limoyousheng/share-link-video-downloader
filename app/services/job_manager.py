from __future__ import annotations

import mimetypes
import logging
import queue
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from app.config import JOBS_DIR
from app.services.audio_analyzer import AudioAnalyzer, SPEECH_KINDS
from app.services.browser_provider import BrowserProvider
from app.services.douyin_provider import (
    JobCancelled,
    ProviderError,
    ProviderResult,
    YtDlpProvider,
    purge_partial_downloads,
    write_metadata_file,
)
from app.services.platform_providers import JianyingProvider, PublicShareProvider
from app.services.share_parser import parse_share
from app.services.transcriber import (
    Transcriber,
    use_platform_subtitle,
)
from app.store import JobStore


TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
logger = logging.getLogger(__name__)


class JobQueueFull(RuntimeError):
    pass


class JobNotRetryable(RuntimeError):
    pass


def safe_download_stem(title: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "video"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if cleaned.upper() in reserved:
        cleaned = f"video-{cleaned}"
    return cleaned[:80].rstrip(" .") or "video"


def _mime_for(path: Path) -> str:
    known = {
        ".srt": "application/x-subrip; charset=utf-8",
        ".vtt": "text/vtt; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".webp": "image/webp",
    }
    return known.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class JobManager:
    def __init__(self, store: JobStore):
        self.store = store
        self.ytdlp = YtDlpProvider()
        self.browser = BrowserProvider()
        self.public_shares = PublicShareProvider()
        self.jianying = JianyingProvider()
        self.audio_analyzer = AudioAnalyzer()
        self.transcriber = Transcriber()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._cancel_events: dict[str, threading.Event] = {}
        self._events_lock = threading.RLock()
        self._submit_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        self.store.mark_interrupted()
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._worker, name="video-job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._events_lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def create(
        self,
        source_text: str,
        options: dict[str, Any],
        owner_hash: str,
    ) -> dict[str, Any]:
        parsed_share = parse_share(source_text)
        source_url = parsed_share.url
        options = {key: value for key, value in options.items() if key != "share_text"}
        options["platform"] = parsed_share.platform
        options["share_caption"] = parsed_share.share_caption
        with self._submit_lock:
            if self.store.has_active_for_owner(owner_hash):
                raise JobQueueFull("当前已有一个视频正在处理，请稍后再试。")
            if self._queue.qsize() >= 8:
                raise JobQueueFull("等待中的任务已达到 8 个，请稍后再试。")
            job_id = uuid.uuid4().hex
            job_dir = (JOBS_DIR / job_id).resolve()
            job_dir.mkdir(parents=True, exist_ok=False)
            event = threading.Event()
            with self._events_lock:
                self._cancel_events[job_id] = event
            job = self.store.create(
                job_id=job_id,
                owner_hash=owner_hash,
                source_text=source_text,
                source_url=source_url,
                options=options,
            )
            self._queue.put_nowait(job_id)
        return job

    def retry(self, job_id: str, browser_fallback: bool | None = None) -> dict[str, Any] | None:
        job = self.store.get(job_id)
        if not job:
            return None
        if job["status"] not in TERMINAL_STATUSES:
            raise JobNotRetryable("任务仍在处理中，不能重复创建重试任务。")
        options = dict(job["options"])
        if browser_fallback is not None:
            options["browser_fallback"] = browser_fallback
        owner_hash = str(job.get("owner_hash") or "")
        return self.create(job["source_text"], options, owner_hash)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.store.get(job_id)
        if not job:
            return None
        if job["status"] in TERMINAL_STATUSES:
            return job
        with self._events_lock:
            event = self._cancel_events.get(job_id)
            if event:
                event.set()
        return self.store.mark_cancelling(job_id)

    def _worker(self) -> None:
        while not self._stopping.is_set():
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                break
            try:
                self._process(job_id)
            except Exception:
                logger.exception("Job worker recovered from an unexpected task failure: %s", job_id)
                try:
                    current = self.store.get(job_id)
                    if current and current.get("status") not in TERMINAL_STATUSES:
                        self.store.update(
                            job_id,
                            status="failed",
                            phase="failed",
                            message="程序遇到未预期错误，请重试或查看终端日志。",
                            error_code="INTERNAL_ERROR",
                            error_message="程序遇到未预期错误，请重试或查看终端日志。",
                        )
                except Exception:
                    logger.exception("Failed to persist worker recovery state for job %s", job_id)
            finally:
                self._queue.task_done()
                with self._events_lock:
                    self._cancel_events.pop(job_id, None)

    def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        with self._events_lock:
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
        job_dir = (JOBS_DIR / job_id).resolve()
        options = job["options"]
        latest_metadata: dict[str, Any] = {}

        def update_progress(
            phase: str,
            progress: float,
            message: str,
            metadata: dict[str, Any] | None,
        ) -> None:
            nonlocal latest_metadata
            fields: dict[str, Any] = {
                "status": "running",
                "phase": phase,
                "progress": round(max(0, min(progress, 99)), 1),
                "message": message,
                "error_code": None,
                "error_message": None,
            }
            if metadata:
                latest_metadata = {**latest_metadata, **metadata}
                fields["metadata_json"] = latest_metadata
            self.store.update(job_id, **fields)

        try:
            if cancel_event.is_set():
                raise JobCancelled("任务已取消")
            update_progress("resolving", 4, "正在展开短链并读取作品信息", None)
            result: ProviderResult
            platform = str(options.get("platform") or "douyin")
            share_caption = str(options.get("share_caption") or "").strip()
            if platform in {"douyin", "xiaohongshu"}:
                try:
                    result = self.ytdlp.process(
                        platform=platform,
                        source_url=job["source_url"],
                        job_dir=job_dir,
                        download=bool(options.get("download_video")),
                        cancel_event=cancel_event,
                        progress_callback=update_progress,
                    )
                except ProviderError as first_error:
                    if not (
                        platform == "douyin"
                        and options.get("browser_fallback")
                        and first_error.browser_fallback_allowed
                    ):
                        raise
                    purge_partial_downloads(job_dir)
                    update_progress(
                        "browser_wait",
                        7,
                        "快速解析需要校验，正在切换到独立 Edge 辅助",
                        None,
                    )
                    result = self.browser.process(
                        source_url=job["source_url"],
                        job_dir=job_dir,
                        download=bool(options.get("download_video")),
                        cancel_event=cancel_event,
                        progress_callback=update_progress,
                    )
            elif platform in {"jimeng", "xiaoyunque", "kuaishou"}:
                result = self.public_shares.process(
                    platform=platform,
                    source_url=job["source_url"],
                    share_caption=share_caption,
                    job_dir=job_dir,
                    download=bool(options.get("download_video")),
                    cancel_event=cancel_event,
                    progress_callback=update_progress,
                )
            elif platform == "jianying":
                result = self.jianying.process(
                    source_url=job["source_url"],
                    share_caption=share_caption,
                    job_dir=job_dir,
                    download=bool(options.get("download_video")),
                    cancel_event=cancel_event,
                    progress_callback=update_progress,
                )
            else:
                raise ProviderError("PLATFORM_NOT_SUPPORTED", "该平台暂不支持解析。")

            latest_metadata = {**latest_metadata, **result.metadata}
            latest_metadata["platform"] = platform
            if share_caption and not str(latest_metadata.get("description") or "").strip():
                latest_metadata["description"] = share_caption
                if not str(latest_metadata.get("title") or "").strip():
                    latest_metadata["title"] = share_caption
            if result.video_path:
                self._probe_media(result.video_path, latest_metadata)
            latest_metadata["resolution_method"] = result.method

            artifacts = self._base_artifacts(job_dir, result, latest_metadata)
            self.store.update(
                job_id,
                metadata_json=latest_metadata,
                artifacts_json=artifacts,
                progress=67 if result.video_path else 92,
                phase="downloaded" if result.video_path else "metadata_ready",
                message="视频已下载" if result.video_path else "作品信息已解析",
            )

            transcription: dict[str, Any] | None = None
            transcription_warning: ProviderError | None = None
            if options.get("transcribe"):
                if not result.video_path:
                    raise ProviderError("VIDEO_REQUIRED", "语音转写需要先下载视频。")

                audio_analysis: dict[str, Any]
                try:
                    audio_analysis = self.audio_analyzer.analyze(
                        video_path=result.video_path,
                        has_audio=latest_metadata.get("has_audio"),
                        cancel_event=cancel_event,
                        progress_callback=update_progress,
                    )
                except ProviderError as exc:
                    audio_analysis = {
                        "status": "unavailable",
                        "kind": "unknown",
                        "method": "silero_vad_energy_v1",
                        "speech_intervals": [],
                    }
                    transcription_warning = exc

                kind = str(audio_analysis.get("kind") or "unknown")
                audio_message = {
                    "speech_only": "检测到清晰讲话，正在生成文稿",
                    "speech_background": "检测到讲话和音乐/背景声，正在生成文稿",
                    "non_speech": "检测到音乐或其他声音，未发现清晰讲话",
                    "no_audio": "视频没有有效声音",
                    "unknown": "声音类型暂时无法可靠判定",
                }.get(kind, "声音分析完成")
                update_progress(
                    "audio_ready",
                    72 if kind in SPEECH_KINDS else 94,
                    audio_message,
                    {"audio_analysis": audio_analysis},
                )

                should_transcribe = (
                    audio_analysis.get("status") != "ready" or kind in SPEECH_KINDS
                )
                if should_transcribe and result.subtitle_path:
                    try:
                        update_progress("transcribing", 75, "检测到平台字幕，正在整理讲话文稿", None)
                        transcription = use_platform_subtitle(
                            result.subtitle_path,
                            job_dir,
                            audio_analysis.get("speech_intervals"),
                        )
                    except ProviderError:
                        transcription = None
                if should_transcribe and transcription is None:
                    try:
                        transcription = self.transcriber.transcribe(
                            video_path=result.video_path,
                            job_dir=job_dir,
                            model_name=str(options.get("asr_model") or "medium"),
                            language=str(options.get("language") or "zh"),
                            duration=latest_metadata.get("duration"),
                            speech_intervals=(
                                audio_analysis.get("speech_intervals")
                                if audio_analysis.get("status") == "ready"
                                else None
                            ),
                            cancel_event=cancel_event,
                            progress_callback=update_progress,
                        )
                    except ProviderError as exc:
                        if exc.code not in {"NO_AUDIO", "NO_SPEECH"}:
                            transcription_warning = exc

            if transcription:
                latest_metadata["transcript"] = {
                    "text": transcription["text"],
                    "source": transcription["source"],
                    "language": transcription["language"],
                    "model": transcription.get("model"),
                    "segment_count": len(transcription.get("segments") or []),
                }
                artifacts.extend(self._transcript_artifacts(job_dir))

            metadata_path = job_dir / "metadata.json"
            write_metadata_file(metadata_path, latest_metadata)
            artifacts = [item for item in artifacts if item["key"] != "metadata"]
            artifacts.append(self._artifact("metadata", "metadata", metadata_path, "作品信息.json"))

            if cancel_event.is_set():
                raise JobCancelled("任务已取消")
            if transcription_warning:
                self.store.update(
                    job_id,
                    status="partial",
                    phase="completed",
                    progress=100,
                    message=f"视频已完成，但{transcription_warning.message}",
                    metadata_json=latest_metadata,
                    artifacts_json=artifacts,
                    error_code=transcription_warning.code,
                    error_message=transcription_warning.message,
                )
            else:
                self.store.update(
                    job_id,
                    status="completed",
                    phase="completed",
                    progress=100,
                    message="处理完成",
                    metadata_json=latest_metadata,
                    artifacts_json=artifacts,
                    error_code=None,
                    error_message=None,
                )
        except JobCancelled:
            purge_partial_downloads(job_dir)
            self.store.update(
                job_id,
                status="cancelled",
                phase="cancelled",
                progress=0,
                message="任务已取消",
                error_code=None,
                error_message=None,
            )
        except ProviderError as exc:
            purge_partial_downloads(job_dir)
            self.store.update(
                job_id,
                status="failed",
                phase="failed",
                message=exc.message,
                error_code=exc.code,
                error_message=exc.message,
                metadata_json=latest_metadata,
            )
        except Exception:
            logger.exception("Unexpected error while processing job %s", job_id)
            purge_partial_downloads(job_dir)
            self.store.update(
                job_id,
                status="failed",
                phase="failed",
                message="程序遇到未预期错误，请重试或查看终端日志。",
                error_code="INTERNAL_ERROR",
                error_message="程序遇到未预期错误，请重试或查看终端日志。",
                metadata_json=latest_metadata,
            )

    @staticmethod
    def _probe_media(path: Path, metadata: dict[str, Any]) -> None:
        try:
            import av

            with av.open(str(path)) as container:
                stream = next((item for item in container.streams if item.type == "video"), None)
                if stream is None:
                    raise ProviderError("INVALID_MEDIA", "下载文件中没有视频流。")
                metadata["width"] = metadata.get("width") or stream.width
                metadata["height"] = metadata.get("height") or stream.height
                if not metadata.get("fps") and stream.average_rate:
                    metadata["fps"] = round(float(stream.average_rate), 3)
                if not metadata.get("duration") and container.duration:
                    metadata["duration"] = round(container.duration / 1_000_000, 3)
                metadata["video_codec"] = getattr(stream.codec_context, "name", None)
                metadata["has_audio"] = any(item.type == "audio" for item in container.streams)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("INVALID_MEDIA", "视频文件下载完成，但媒体完整性检查未通过。") from exc

    def _base_artifacts(
        self,
        job_dir: Path,
        result: ProviderResult,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        stem = safe_download_stem(str(metadata.get("title") or "video"))
        if result.video_path:
            artifacts.append(
                self._artifact("video", "video", result.video_path, f"{stem}{result.video_path.suffix.lower()}")
            )
        if result.cover_path:
            artifacts.append(
                self._artifact("cover", "cover", result.cover_path, f"{stem}-封面{result.cover_path.suffix.lower()}")
            )
        return artifacts

    def _transcript_artifacts(self, job_dir: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for key, filename, display_name in (
            ("transcript_txt", "transcript.txt", "语音文稿.txt"),
            ("transcript_srt", "transcript.srt", "语音字幕.srt"),
            ("transcript_vtt", "transcript.vtt", "语音字幕.vtt"),
            ("transcript_json", "transcript.json", "语音分段.json"),
        ):
            path = job_dir / filename
            if path.is_file():
                result.append(self._artifact(key, "transcript", path, display_name))
        return result

    @staticmethod
    def _artifact(key: str, kind: str, path: Path, display_name: str) -> dict[str, Any]:
        return {
            "key": key,
            "kind": kind,
            "filename": path.name,
            "display_name": display_name,
            "size": path.stat().st_size,
            "mime": _mime_for(path),
        }
