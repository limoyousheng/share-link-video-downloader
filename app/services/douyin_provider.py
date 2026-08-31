from __future__ import annotations

import json
import ipaddress
import re
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib.parse import urlsplit

from app.config import MAX_COVER_BYTES, MAX_MEDIA_BYTES, get_cookie_file, get_ffmpeg_executable


ProgressCallback = Callable[[str, float, str, dict[str, Any] | None], None]

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
)
DOUYIN_PAGE_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
PLATFORM_REFERERS = {
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
}
PLATFORM_NAMES = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}


@dataclass(slots=True)
class ProviderError(RuntimeError):
    code: str
    message: str
    browser_fallback_allowed: bool = False

    def __str__(self) -> str:
        return self.message


class JobCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class ProviderResult:
    metadata: dict[str, Any]
    video_path: Path | None
    subtitle_path: Path | None
    cover_path: Path | None
    method: str


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("_type") in {"playlist", "multi_video"}:
        entries = value.get("entries") or []
        return next((item for item in entries if isinstance(item, dict)), value)
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def clean_metadata(info: dict[str, Any], source_url: str, method: str) -> dict[str, Any]:
    info = _first_mapping(info)
    description = str(info.get("description") or info.get("title") or "").strip()
    title = str(info.get("title") or description or "未命名视频").strip()
    hashtags = re.findall(r"#([^#\s]+)", description)

    thumbnail = info.get("thumbnail")
    if not thumbnail:
        thumbnails = info.get("thumbnails") or []
        for candidate in reversed(thumbnails):
            if isinstance(candidate, dict) and candidate.get("url"):
                thumbnail = candidate["url"]
                break

    webpage_url = info.get("webpage_url") or info.get("original_url") or source_url
    if not isinstance(webpage_url, str) or not webpage_url.startswith("https://"):
        webpage_url = source_url

    artists = info.get("artists")
    if not artists and info.get("artist"):
        artists = [info["artist"]]

    if isinstance(thumbnail, str) and thumbnail.startswith("http://"):
        thumbnail = "https://" + thumbnail.removeprefix("http://")

    return {
        "id": str(info.get("id") or ""),
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "author": info.get("channel") or info.get("uploader") or info.get("creator") or "未知作者",
        "author_id": info.get("channel_id") or info.get("uploader_id"),
        "author_url": info.get("channel_url") or info.get("uploader_url"),
        "duration": _number(info.get("duration")),
        "thumbnail": thumbnail if isinstance(thumbnail, str) and thumbnail.startswith("https://") else None,
        "canonical_url": webpage_url,
        "source_url": source_url,
        "upload_date": info.get("upload_date"),
        "timestamp": _number(info.get("timestamp")),
        "view_count": _number(info.get("view_count")),
        "like_count": _number(info.get("like_count")),
        "comment_count": _number(info.get("comment_count")),
        "share_count": _number(info.get("repost_count")),
        "save_count": _number(info.get("save_count")),
        "music": info.get("track"),
        "music_artists": artists if isinstance(artists, list) else None,
        "width": _number(info.get("width")),
        "height": _number(info.get("height")),
        "fps": _number(info.get("fps")),
        "format": info.get("format"),
        "format_id": info.get("format_id"),
        "ext": info.get("ext"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "resolution_method": method,
    }


def locate_downloaded_video(job_dir: Path) -> Path | None:
    candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and path.stat().st_size > 0
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def locate_platform_subtitle(job_dir: Path) -> Path | None:
    candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUBTITLE_EXTENSIONS
        and path.name not in {"transcript.srt", "transcript.vtt"}
    ]
    preferred = sorted(
        candidates,
        key=lambda path: (
            0 if any(token in path.name.lower() for token in ("zh", "cn", "hans")) else 1,
            path.name,
        ),
    )
    return preferred[0] if preferred else None


class _SilentLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


class YtDlpProvider:
    def __init__(self) -> None:
        self.ffmpeg_executable = get_ffmpeg_executable()

    def _options(
        self,
        *,
        job_dir: Path,
        cancel_event: Event,
        progress_callback: ProgressCallback,
        download: bool,
        platform: str,
    ) -> dict[str, Any]:
        metadata_sent = False
        last_progress_at = 0.0

        def progress_hook(payload: dict[str, Any]) -> None:
            nonlocal metadata_sent, last_progress_at
            if cancel_event.is_set():
                raise JobCancelled("任务已取消")

            info = payload.get("info_dict")
            if isinstance(info, dict) and not metadata_sent:
                metadata_sent = True
                progress_callback(
                    "downloading",
                    16,
                    "已解析视频，开始下载最高可访问质量的视频流",
                    clean_metadata(info, str(info.get("original_url") or ""), "yt-dlp"),
                )

            if payload.get("status") != "downloading":
                return
            now = time.monotonic()
            if now - last_progress_at < 0.35:
                return
            last_progress_at = now
            downloaded = payload.get("downloaded_bytes") or 0
            if downloaded > MAX_MEDIA_BYTES:
                raise ProviderError("MEDIA_TOO_LARGE", "视频超过本程序的 3 GB 安全上限，已停止下载。")
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            ratio = min(downloaded / total, 1.0) if total else 0
            percent = 16 + ratio * 49
            speed = payload.get("speed") or 0
            eta = payload.get("eta")
            message = "正在下载视频"
            if speed:
                message += f" · {speed / 1024 / 1024:.1f} MB/s"
            if isinstance(eta, (int, float)):
                message += f" · 约 {int(eta)} 秒"
            progress_callback("downloading", percent, message, None)

        options: dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "logger": _SilentLogger(),
            "windowsfilenames": True,
            "outtmpl": str(job_dir / "video.%(ext)s"),
            "overwrites": True,
            "continuedl": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 25,
            "concurrent_fragment_downloads": 4,
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "max_filesize": MAX_MEDIA_BYTES,
            "http_headers": {
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": PLATFORM_REFERERS.get(platform, "https://www.douyin.com/"),
            },
        }
        if download:
            options.update(
                {
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": ["zh-Hans", "zh-CN", "zh", "zh.*"],
                    "subtitlesformat": "srt/best",
                    "progress_hooks": [progress_hook],
                }
            )
        if self.ffmpeg_executable:
            options["ffmpeg_location"] = self.ffmpeg_executable
        if cookie_file := get_cookie_file():
            options["cookiefile"] = cookie_file
        return options

    def process(
        self,
        *,
        source_url: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
        platform: str = "douyin",
    ) -> ProviderResult:
        try:
            import yt_dlp

            options = self._options(
                job_dir=job_dir,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                download=download,
                platform=platform,
            )
            with yt_dlp.YoutubeDL(options) as downloader:
                raw_info = downloader.extract_info(source_url, download=download)
                info = downloader.sanitize_info(_first_mapping(raw_info))
        except JobCancelled:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise self._friendly_error(exc, platform) from exc

        metadata = clean_metadata(info, source_url, "yt-dlp")
        metadata["platform"] = platform
        video_path = locate_downloaded_video(job_dir) if download else None
        if download and not video_path:
            raise ProviderError(
                "VIDEO_FILE_MISSING",
                "解析成功，但没有找到下载完成的视频文件。",
                browser_fallback_allowed=True,
            )
        if video_path and video_path.stat().st_size > MAX_MEDIA_BYTES:
            video_path.unlink(missing_ok=True)
            raise ProviderError("MEDIA_TOO_LARGE", "视频超过本程序的 3 GB 安全上限，已停止下载。")

        cover_path = self._download_cover(metadata.get("thumbnail"), job_dir, metadata.get("canonical_url"))
        return ProviderResult(
            metadata=metadata,
            video_path=video_path,
            subtitle_path=locate_platform_subtitle(job_dir),
            cover_path=cover_path,
            method="yt-dlp",
        )

    @staticmethod
    def _friendly_error(exc: Exception, platform: str = "douyin") -> ProviderError:
        message = str(exc)
        lowered = message.lower()
        platform_name = PLATFORM_NAMES.get(platform, "平台")
        allow_browser = platform == "douyin"
        if "fresh cookies" in lowered or "sign in" in lowered or "cookies" in lowered:
            return ProviderError(
                "PLATFORM_VERIFICATION_REQUIRED",
                f"{platform_name}要求登录或浏览器校验，当前公开会话无法访问。",
                browser_fallback_allowed=allow_browser,
            )
        if any(token in lowered for token in ("private", "friends only", "followers only")):
            return ProviderError(
                "ACCESS_RESTRICTED",
                "该作品不是公开可访问内容，程序不会尝试绕过访问限制。",
            )
        if "429" in lowered or "too many requests" in lowered:
            return ProviderError(
                "RATE_LIMITED",
                f"{platform_name}暂时限制了请求，请稍后再试。",
                browser_fallback_allowed=allow_browser,
            )
        if "403" in lowered or "forbidden" in lowered:
            return ProviderError(
                "ACCESS_DENIED",
                f"{platform_name}拒绝了当前公开请求，请稍后再试。",
                browser_fallback_allowed=allow_browser,
            )
        if "max-filesize" in lowered or "larger than max" in lowered:
            return ProviderError("MEDIA_TOO_LARGE", "视频超过本程序的 3 GB 安全上限，已停止下载。")
        if any(token in lowered for token in ("404", "not available", "unavailable", "removed")):
            return ProviderError("VIDEO_UNAVAILABLE", "链接已失效、作品已删除或当前不可访问。")
        if any(token in lowered for token in ("timed out", "timeout", "network", "connection")):
            return ProviderError(
                "NETWORK_ERROR",
                "网络连接失败，请检查网络后重试。",
                browser_fallback_allowed=allow_browser,
            )
        return ProviderError(
            "EXTRACTION_FAILED",
            f"{platform_name}页面结构或校验方式发生了变化，解析未成功。",
            browser_fallback_allowed=allow_browser,
        )

    @staticmethod
    def _download_cover(url: Any, job_dir: Path, referer: Any) -> Path | None:
        if not isinstance(url, str) or not is_public_https_url(url):
            return None
        partial = job_dir / "cover.part"
        try:
            import httpx

            with httpx.Client(
                follow_redirects=True,
                timeout=15,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": str(referer or "https://www.douyin.com/")},
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    if not is_public_https_url(str(response.url)):
                        return None
                    declared_size = int(response.headers.get("content-length") or 0)
                    if declared_size > MAX_COVER_BYTES:
                        return None
                    downloaded = 0
                    with partial.open("wb") as target:
                        for chunk in response.iter_bytes(chunk_size=256 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_COVER_BYTES:
                                return None
                            target.write(chunk)
                    if downloaded == 0:
                        return None
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    extension = {"image/png": ".png", "image/webp": ".webp"}.get(media_type, ".jpg")
                    path = job_dir / f"cover{extension}"
                    partial.replace(path)
                    return path
        except Exception:
            return None
        finally:
            partial.unlink(missing_ok=True)


def purge_partial_downloads(job_dir: Path) -> None:
    for path in job_dir.iterdir():
        if path.is_file() and (path.suffix.lower() in {".part", ".ytdl"} or ".part" in path.name):
            try:
                path.unlink()
            except OSError:
                pass


def copy_platform_subtitle(source: Path, destination: Path) -> None:
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)


def write_metadata_file(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def is_allowed_douyin_page_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower().rstrip(".") in DOUYIN_PAGE_HOSTS
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def _hostname_is_public(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal", ".lan")):
        return False
    # inet_aton catches legacy IPv4 spellings such as 127.1, 0177.0.0.1
    # and 0x7f.0.0.1 that URL clients may still resolve as loopback.
    try:
        legacy_ipv4 = ipaddress.ip_address(socket.inet_aton(lowered))
        return legacy_ipv4.is_global
    except OSError:
        pass
    try:
        return ipaddress.ip_address(lowered).is_global
    except ValueError:
        # The URL comes from a validated Douyin page/API response, not directly
        # from user input. Reject literal/private hosts while allowing DNS names;
        # resolving here would break systems that use proxy/fake-IP DNS ranges.
        return "." in lowered


def is_public_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme == "https"
            and bool(hostname)
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and _hostname_is_public(hostname)
        )
    except ValueError:
        return False
