from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from app.config import MAX_SHARE_TEXT_LENGTH


Platform = Literal[
    "douyin",
    "jimeng",
    "xiaoyunque",
    "jianying",
    "xiaohongshu",
    "kuaishou",
]

PLATFORM_LABELS: dict[Platform, str] = {
    "douyin": "抖音",
    "jimeng": "即梦",
    "xiaoyunque": "小云雀",
    "jianying": "剪映",
    "xiaohongshu": "小红书",
    "kuaishou": "快手",
}

HOST_PLATFORMS: dict[str, Platform] = {
    "douyin.com": "douyin",
    "www.douyin.com": "douyin",
    "v.douyin.com": "douyin",
    "iesdouyin.com": "douyin",
    "www.iesdouyin.com": "douyin",
    "jimeng.jianying.com": "jimeng",
    "xiaoyunque.jianying.com": "xiaoyunque",
    "lv.ulikecam.com": "jianying",
    "xhslink.cn": "xiaohongshu",
    "xiaohongshu.com": "xiaohongshu",
    "www.xiaohongshu.com": "xiaohongshu",
    "v.kuaishou.com": "kuaishou",
    "kuaishou.com": "kuaishou",
    "www.kuaishou.com": "kuaishou",
}

ALLOWED_HOSTS = set(HOST_PLATFORMS)
_HOST_PATTERN = "|".join(
    sorted((re.escape(host) for host in ALLOWED_HOSTS), key=len, reverse=True)
)
_MARKDOWN_TARGET_RE = re.compile(r"\]\(\s*(https?://[^\s)<>]+)\s*\)", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(
    r"\[\s*([^\]]*?)\s*\]\(\s*https?://[^\s)<>]+\s*\)", re.IGNORECASE
)
_URL_RE = re.compile(r"https?://[^\s<>\[\]\"']+", re.IGNORECASE)
_BARE_RE = re.compile(
    rf"(?<![\w./:@-])((?:{_HOST_PATTERN})/[A-Za-z0-9_?&=/%+.,~#\\-]+)",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、”’）】》)}]"
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([_&?=.%+#~/-])")


@dataclass(frozen=True, slots=True)
class ParsedShare:
    platform: Platform
    url: str
    share_caption: str

    @property
    def platform_label(self) -> str:
        return PLATFORM_LABELS[self.platform]


@dataclass(slots=True)
class ShareParseError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _unescape_share_text(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\\/", "/").replace("\\.", ".")
    return _MARKDOWN_ESCAPE_RE.sub(r"\1", value)


def _clean_candidate(value: str) -> str:
    return _unescape_share_text(value.strip()).rstrip(_TRAILING_PUNCTUATION)


def _normalize_supported_url(value: str) -> tuple[Platform, str] | None:
    value = _clean_candidate(value)
    if not value.lower().startswith(("https://", "http://")):
        value = f"https://{value}"

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None

    platform = HOST_PLATFORMS.get(host)
    if parsed.scheme.lower() != "https" or not platform:
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    if not parsed.path or parsed.path == "/":
        return None

    netloc = host if port is None else f"{host}:{port}"
    return platform, urlunsplit(("https", netloc, parsed.path, parsed.query, ""))


def _extract_share_caption(share_text: str) -> str:
    text = _unescape_share_text(share_text)
    text = _MARKDOWN_LINK_RE.sub(r" \1 ", text)
    text = re.sub(r"\[\s*([^\]]*?)\s*\]\s*", r" \1 ", text)
    text = _URL_RE.sub(" ", text)
    text = _BARE_RE.sub(" ", text)
    text = re.sub(r"^\s*\d{1,3}\s*[.、)]\s*", "", text)

    boilerplate = (
        r"复制此链接，?打开抖音搜索.*$",
        r"[A-Z]{2}\d{4}，?来【即梦】.*$",
        r"[A-Z]{2}\d{4}，?点击链接或复制本条信息.*$",
        r"复制此链接，?打开【剪映】.*$",
        r"先复制文字，然后进入【小红书】查看笔记.*$",
        r"该作品在快手被播放过.*$",
        r"点击链接，?打开【快手】直接观看.*$",
    )
    for pattern in boilerplate:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n，。;；")
    return text[:5_000]


def parse_share(share_text: str) -> ParsedShare:
    if not isinstance(share_text, str) or not share_text.strip():
        raise ShareParseError("EMPTY_INPUT", "请先粘贴视频分享文字或链接。")
    if len(share_text) > MAX_SHARE_TEXT_LENGTH:
        raise ShareParseError(
            "INPUT_TOO_LONG",
            f"分享文字不能超过 {MAX_SHARE_TEXT_LENGTH} 个字符。",
        )

    normalized_text = _unescape_share_text(share_text)
    raw_candidates: list[str] = []
    raw_candidates.extend(_MARKDOWN_TARGET_RE.findall(normalized_text))
    raw_candidates.extend(_URL_RE.findall(normalized_text))
    raw_candidates.extend(_BARE_RE.findall(normalized_text))

    parsed_urls: list[tuple[Platform, str]] = []
    for candidate in raw_candidates:
        normalized = _normalize_supported_url(candidate)
        if normalized and normalized not in parsed_urls:
            parsed_urls.append(normalized)

    if not parsed_urls:
        raise ShareParseError(
            "NO_SUPPORTED_URL",
            "没有识别到支持的视频链接。",
        )
    if len(parsed_urls) > 1:
        raise ShareParseError(
            "MULTIPLE_URLS",
            "检测到多个不同的视频链接，请每次只处理一条。",
        )

    platform, url = parsed_urls[0]
    return ParsedShare(
        platform=platform,
        url=url,
        share_caption=_extract_share_caption(share_text),
    )


def extract_douyin_url(share_text: str) -> str:
    """Backward-compatible helper retained for callers that only accept Douyin."""
    parsed = parse_share(share_text)
    if parsed.platform != "douyin":
        raise ShareParseError("NO_DOUYIN_URL", "没有识别到有效的抖音链接。")
    return parsed.url
