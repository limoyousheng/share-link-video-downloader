from __future__ import annotations

import os
import socket
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DOUYIN_APP_DATA_DIR", BASE_DIR / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
MODELS_DIR = Path(os.getenv("DOUYIN_ASR_MODELS_DIR", BASE_DIR / "models")).resolve()
DB_PATH = DATA_DIR / "jobs.sqlite3"
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"

MAX_SHARE_TEXT_LENGTH = 12_000
MAX_MEDIA_BYTES = int(os.getenv("DOUYIN_MAX_MEDIA_BYTES", str(3 * 1024**3)))
MAX_COVER_BYTES = 15 * 1024**2


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


BROWSER_FALLBACK_ENABLED = env_flag("DOUYIN_BROWSER_FALLBACK", True)


def get_allowed_hosts() -> list[str]:
    hosts = {"127.0.0.1", "localhost"}
    configured = os.getenv("DOUYIN_ALLOWED_HOSTS", "")
    hosts.update(item.strip().lower().rstrip(".") for item in configured.split(",") if item.strip())

    try:
        hostname = socket.gethostname().strip().lower().rstrip(".")
        if hostname:
            hosts.add(hostname)
        for info in socket.getaddrinfo(hostname or None, None):
            address = str(info[4][0]).split("%", 1)[0]
            if address:
                hosts.add(address)
    except OSError:
        pass
    return sorted(hosts)


def ensure_directories() -> None:
    for path in (DATA_DIR, JOBS_DIR, MODELS_DIR, BROWSER_PROFILE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def get_ffmpeg_executable() -> str | None:
    configured = os.getenv("DOUYIN_FFMPEG")
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return str(path)

    try:
        import imageio_ffmpeg

        path = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        return str(path) if path.is_file() else None
    except Exception:
        return None


def get_cookie_file() -> str | None:
    configured = os.getenv("DOUYIN_COOKIE_FILE")
    if not configured:
        return None
    path = Path(configured).expanduser().resolve()
    return str(path) if path.is_file() else None
