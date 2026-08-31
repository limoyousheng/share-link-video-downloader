from __future__ import annotations

import json
import re
import time
from pathlib import Path
from threading import Event
from typing import Any, Iterable
from urllib.parse import parse_qsl, urljoin, urlsplit

from app.config import MAX_MEDIA_BYTES
from app.services.browser_provider import BrowserProvider
from app.services.douyin_provider import (
    DEFAULT_USER_AGENT,
    JobCancelled,
    ProgressCallback,
    ProviderError,
    ProviderResult,
    YtDlpProvider,
    clean_metadata,
    is_public_https_url,
)


JIMENG_PAGE_HOSTS = {"jimeng.jianying.com"}
YUNQUE_PAGE_HOSTS = {"xiaoyunque.jianying.com"}
KUAISHOU_PAGE_HOSTS = {
    "v.kuaishou.com",
    "v.m.chenzhongtech.com",
    "www.kuaishou.com",
    "kuaishou.com",
}
JIMENG_API = (
    "https://jimeng.jianying.com/luckycat/cn/jianying/campaign/v1/"
    "dreamina/share/landing_page?uid=0&aid=581595&app_name=dreamina&duanwai_huiliu_page=1"
)
YUNQUE_API = (
    "https://xiaoyunque.jianying.com/luckycat/cn/jianying/campaign/v1/"
    "pippit/share/landing_page?aid=8700"
)
KUAISHOU_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36"
)
INVALID_JIANYING_STATUSES = {"4", "5", "6", "7", "100"}
JIANYING_IMAGE_TYPES = {"2001", "2004"}


def _host_allowed(url: str, hosts: set[str]) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower().rstrip(".") in hosts
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def _query_dict(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _seconds(value: Any) -> int | float | None:
    number = _number(value)
    if number is not None and number > 1000:
        return round(number / 1000, 3)
    return number


def _https(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("http://"):
        value = "https://" + value.removeprefix("http://")
    return value if is_public_https_url(value) else None


def _first_url(value: Any) -> str | None:
    if isinstance(value, str):
        return _https(value)
    if isinstance(value, dict):
        for key in ("url", "uri", "video_url", "play_url"):
            if result := _https(value.get(key)):
                return result
        for key in ("url_list", "urls", "backupUrl"):
            if result := _first_url(value.get(key)):
                return result
    if isinstance(value, list):
        for item in value:
            if result := _first_url(item):
                return result
    return None


def _cookies_for_downloader(session: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    for cookie in jar or []:
        result.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
            }
        )
    return result


def _base_info(
    *,
    source_url: str,
    canonical_url: str,
    platform: str,
    method: str,
    video_id: Any,
    title: str,
    description: str,
    author: Any = None,
    author_id: Any = None,
    duration: Any = None,
    thumbnail: Any = None,
    width: Any = None,
    height: Any = None,
    fps: Any = None,
    timestamp: Any = None,
    view_count: Any = None,
    like_count: Any = None,
    comment_count: Any = None,
    share_count: Any = None,
    music: Any = None,
    music_artists: Any = None,
    format_name: Any = None,
) -> dict[str, Any]:
    info = {
        "id": str(video_id or ""),
        "title": title or description or "未命名视频",
        "description": description or title,
        "uploader": author or "未知作者",
        "uploader_id": author_id,
        "duration": duration,
        "thumbnail": _https(thumbnail),
        "width": width,
        "height": height,
        "fps": fps,
        "timestamp": timestamp,
        "view_count": view_count,
        "like_count": like_count,
        "comment_count": comment_count,
        "repost_count": share_count,
        "track": music,
        "artists": music_artists,
        "format": format_name,
        "ext": "mp4",
        "webpage_url": canonical_url,
    }
    metadata = clean_metadata(info, source_url, method)
    metadata["platform"] = platform
    return metadata


def _download_result(
    *,
    session: Any,
    candidates: list[dict[str, Any]],
    user_agent: str,
    canonical_url: str,
    job_dir: Path,
    cancel_event: Event,
    progress_callback: ProgressCallback,
    metadata: dict[str, Any],
    download: bool,
    method: str,
) -> ProviderResult:
    cover_path = YtDlpProvider._download_cover(
        metadata.get("thumbnail"), job_dir, canonical_url
    )
    video_path = None
    if download:
        if not candidates:
            raise ProviderError("MEDIA_URL_MISSING", "公开页面没有提供可下载的视频流。")
        video_path, selected = BrowserProvider._download_media(
            candidates,
            _cookies_for_downloader(session),
            user_agent,
            canonical_url,
            job_dir,
            cancel_event,
            progress_callback,
            metadata.get("duration"),
        )
        metadata.update(
            {
                "width": selected.get("width") or metadata.get("width"),
                "height": selected.get("height") or metadata.get("height"),
                "fps": selected.get("fps") or metadata.get("fps"),
                "format": selected.get("format") or selected.get("codec") or metadata.get("format"),
                "format_id": selected.get("format_id") or selected.get("kind"),
                "ext": "mp4",
            }
        )
    return ProviderResult(
        metadata=metadata,
        video_path=video_path,
        subtitle_path=None,
        cover_path=cover_path,
        method=method,
    )


class PublicShareProvider:
    def process(
        self,
        *,
        platform: str,
        source_url: str,
        share_caption: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> ProviderResult:
        try:
            if platform == "jimeng":
                return self._process_jimeng(
                    source_url, share_caption, job_dir, download, cancel_event, progress_callback
                )
            if platform == "xiaoyunque":
                return self._process_yunque(
                    source_url, share_caption, job_dir, download, cancel_event, progress_callback
                )
            if platform == "kuaishou":
                return self._process_kuaishou(
                    source_url, share_caption, job_dir, download, cancel_event, progress_callback
                )
            raise ProviderError("PLATFORM_NOT_SUPPORTED", "该平台暂不支持公开页面解析。")
        except (JobCancelled, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError("NETWORK_ERROR", "平台连接失败，请检查网络后重试。") from exc

    @staticmethod
    def _session(user_agent: str = DEFAULT_USER_AGENT) -> Any:
        try:
            from curl_cffi import requests
        except Exception as exc:
            raise ProviderError("HTTP_RUNTIME_MISSING", "下载组件未安装，请重新运行启动脚本。") from exc
        session = requests.Session(impersonate="chrome")
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            }
        )
        return session

    @staticmethod
    def _follow_public_page(
        session: Any,
        source_url: str,
        allowed_hosts: set[str],
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, Any]:
        current = source_url
        if not _host_allowed(current, allowed_hosts):
            raise ProviderError("UNSAFE_SOURCE_URL", "链接地址不在该平台的允许范围内。")
        for _ in range(8):
            response = session.get(
                current,
                headers=headers or {},
                allow_redirects=False,
                timeout=30,
            )
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise ProviderError("INVALID_REDIRECT", "平台返回了无效跳转。")
                current = urljoin(current, location)
                if not _host_allowed(current, allowed_hosts):
                    raise ProviderError("UNEXPECTED_REDIRECT", "平台链接跳转到了不受信任的页面。")
                continue
            if response.status_code < 200 or response.status_code >= 300:
                response.close()
                raise ProviderError("VIDEO_UNAVAILABLE", "链接已失效或作品当前不可公开访问。")
            if not _host_allowed(str(response.url), allowed_hosts):
                response.close()
                raise ProviderError("UNEXPECTED_REDIRECT", "平台链接跳转到了不受信任的页面。")
            declared = int(response.headers.get("content-length") or 0)
            if declared > 20 * 1024**2 or len(response.content) > 20 * 1024**2:
                response.close()
                raise ProviderError("PAGE_TOO_LARGE", "平台页面异常，已停止处理。")
            return str(response.url), response
        raise ProviderError("TOO_MANY_REDIRECTS", "平台链接跳转次数过多。")

    @staticmethod
    def _post_json(
        session: Any,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        allowed_hosts: set[str],
    ) -> dict[str, Any]:
        response = session.post(
            endpoint,
            json=body,
            headers=headers,
            allow_redirects=False,
            timeout=30,
        )
        try:
            if not 200 <= response.status_code < 300 or not _host_allowed(str(response.url), allowed_hosts):
                raise ProviderError("PLATFORM_API_FAILED", "平台公开页面数据读取失败。")
            if int(response.headers.get("content-length") or 0) > 20 * 1024**2:
                raise ProviderError("PLATFORM_API_FAILED", "平台公开页面返回了异常数据。")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ProviderError("PLATFORM_API_FAILED", "平台公开页面返回了无效数据。")
            return payload
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("PLATFORM_API_FAILED", "平台公开页面数据读取失败。") from exc
        finally:
            response.close()

    def _process_jimeng(
        self,
        source_url: str,
        share_caption: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> ProviderResult:
        session = self._session()
        try:
            canonical_url, page_response = self._follow_public_page(
                session, source_url, JIMENG_PAGE_HOSTS
            )
            page_response.close()
            query = _query_dict(canonical_url)
            item_id = query.get("id")
            if not item_id or not item_id.isdigit():
                raise ProviderError("VIDEO_ID_MISSING", "即梦分享链接中没有有效作品 ID。")
            payload = self._post_json(
                session,
                JIMENG_API,
                {"query_params": query, "item_id": item_id},
                {
                    "Referer": canonical_url,
                    "Content-Type": "application/json",
                    "appid": "581595",
                    "sign-ver": "1",
                },
                JIMENG_PAGE_HOSTS,
            )
            data = payload.get("data") if str(payload.get("err_no")) == "0" else None
            if not isinstance(data, dict) or str(data.get("page_status")) not in {"0", "0.0"}:
                raise ProviderError("VIDEO_UNAVAILABLE", "该即梦作品当前不能从公开分享页访问。")
            page_info = data.get("page_info") or {}
            creation = page_info.get("creation") if isinstance(page_info, dict) else None
            if not isinstance(creation, dict):
                raise ProviderError("VIDEO_UNAVAILABLE", "即梦公开分享页没有返回目标作品。")
            media = creation.get("metadata") or {}
            download_info = media.get("download_info") or {}
            video_url = _https(download_info.get("url")) or _https(media.get("video_url"))
            if not video_url:
                raise ProviderError("MEDIA_URL_MISSING", "该即梦作品没有公开可下载的视频流。")

            prompt = self._jimeng_prompt(creation.get("description"))
            description = prompt or share_caption
            creator_info = creation.get("creator_info") or {}
            creator = creator_info.get("creator") if isinstance(creator_info, dict) else {}
            statistics = creation.get("statistics") or {}
            metadata = _base_info(
                source_url=source_url,
                canonical_url=canonical_url,
                platform="jimeng",
                method="jimeng-public-api",
                video_id=media.get("video_id") or item_id,
                title=description or f"即梦作品 {item_id}",
                description=description,
                author=(creator or {}).get("user_name") if isinstance(creator, dict) else None,
                author_id=(creator or {}).get("user_id") if isinstance(creator, dict) else None,
                thumbnail=media.get("cover_url"),
                like_count=statistics.get("like_count") if isinstance(statistics, dict) else None,
                comment_count=statistics.get("comment_count") if isinstance(statistics, dict) else None,
                share_count=statistics.get("share_count") if isinstance(statistics, dict) else None,
                format_name=download_info.get("format") if isinstance(download_info, dict) else None,
            )
            progress_callback("metadata_ready", 14, "已读取即梦作品信息", metadata)
            return _download_result(
                session=session,
                candidates=[{"url": video_url, "kind": "jimeng_download", "format": download_info.get("format")}],
                user_agent=DEFAULT_USER_AGENT,
                canonical_url=canonical_url,
                job_dir=job_dir,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                metadata=metadata,
                download=download,
                method="jimeng-public-api",
            )
        finally:
            session.close()

    @staticmethod
    def _jimeng_prompt(description: Any) -> str:
        if not isinstance(description, dict):
            return ""
        prompt = description.get("prompt")
        if isinstance(prompt, str):
            return prompt.strip()
        if not isinstance(prompt, list):
            return ""
        parts = [
            _text(item.get("text"))
            for item in prompt
            if isinstance(item, dict) and str(item.get("item_type")) == "1"
        ]
        return "".join(part for part in parts if part).strip()

    def _process_yunque(
        self,
        source_url: str,
        share_caption: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> ProviderResult:
        session = self._session()
        try:
            canonical_url, page_response = self._follow_public_page(
                session, source_url, YUNQUE_PAGE_HOSTS
            )
            page_response.close()
            query = _query_dict(canonical_url)
            payload = self._post_json(
                session,
                YUNQUE_API,
                {"query_params": query},
                {
                    "Referer": canonical_url,
                    "Content-Type": "application/json",
                    "appid": "8700",
                    "sign-ver": "1",
                },
                YUNQUE_PAGE_HOSTS,
            )
            data = payload.get("data") if str(payload.get("err_no")) == "0" else None
            if not isinstance(data, dict) or str(data.get("page_type")) not in {"2", "2.0"}:
                raise ProviderError("VIDEO_UNAVAILABLE", "该小云雀作品当前不能从公开分享页访问。")
            page_info = data.get("page_info") or {}
            inspiration = page_info.get("inspiration_page") if isinstance(page_info, dict) else None
            item = inspiration.get("item_info") if isinstance(inspiration, dict) else None
            if not isinstance(item, dict) or str(item.get("type")) not in {"2", "2.0"}:
                raise ProviderError("VIDEO_UNAVAILABLE", "该小云雀分享内容不是公开视频作品。")
            video_info = item.get("video_info") or []
            video = video_info[0] if isinstance(video_info, list) and video_info else None
            if not isinstance(video, dict) or not (video_url := _https(video.get("video_url"))):
                raise ProviderError("MEDIA_URL_MISSING", "该小云雀作品没有公开可下载的视频流。")

            title = _text(item.get("title"))
            description = _text(item.get("desc")) or title or share_caption
            user = inspiration.get("user_info") or {}
            video_id = query.get("inspiration_id") or query.get("template_id")
            metadata = _base_info(
                source_url=source_url,
                canonical_url=canonical_url,
                platform="xiaoyunque",
                method="xiaoyunque-public-api",
                video_id=video_id,
                title=title or description or "小云雀作品",
                description=description,
                author=user.get("nick_name") if isinstance(user, dict) else None,
                author_id=user.get("user_id") if isinstance(user, dict) else None,
                thumbnail=video.get("cover_url") or item.get("cover_url"),
                width=video.get("width"),
                height=video.get("height"),
                duration=_seconds(video.get("duration")),
            )
            progress_callback("metadata_ready", 14, "已读取小云雀作品信息", metadata)
            return _download_result(
                session=session,
                candidates=[
                    {
                        "url": video_url,
                        "kind": "xiaoyunque_video",
                        "width": video.get("width"),
                        "height": video.get("height"),
                    }
                ],
                user_agent=DEFAULT_USER_AGENT,
                canonical_url=canonical_url,
                job_dir=job_dir,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                metadata=metadata,
                download=download,
                method="xiaoyunque-public-api",
            )
        finally:
            session.close()

    def _process_kuaishou(
        self,
        source_url: str,
        share_caption: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> ProviderResult:
        session = self._session(KUAISHOU_MOBILE_UA)
        try:
            canonical_url, response = self._follow_public_page(
                session,
                source_url,
                KUAISHOU_PAGE_HOSTS,
                headers={
                    "User-Agent": KUAISHOU_MOBILE_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            try:
                page_text = response.text
            finally:
                response.close()
            state = extract_kuaishou_init_state(page_text)
            target_id = kuaishou_target_id(canonical_url)
            photo = find_kuaishou_photo(state, target_id)
            if not photo:
                raise ProviderError(
                    "VIDEO_UNAVAILABLE",
                    "快手公开分享页未返回目标作品；作品可能已删除、受限或触发平台校验。",
                )
            candidates = kuaishou_media_candidates(photo)
            if not candidates:
                raise ProviderError("MEDIA_URL_MISSING", "该快手作品没有公开可下载的视频流。")
            description = _text(photo.get("caption")) or share_caption
            duration = _seconds(photo.get("duration"))
            timestamp = _number(photo.get("timestamp"))
            if timestamp and timestamp > 10_000_000_000:
                timestamp /= 1000
            metadata = _base_info(
                source_url=source_url,
                canonical_url=canonical_url,
                platform="kuaishou",
                method="kuaishou-mobile-share",
                video_id=photo.get("photoId") or target_id,
                title=description or "快手作品",
                description=description,
                author=photo.get("userName"),
                author_id=photo.get("userId"),
                duration=duration,
                thumbnail=_first_url(photo.get("coverUrls")),
                width=photo.get("width"),
                height=photo.get("height"),
                timestamp=timestamp,
                view_count=photo.get("viewCount"),
                like_count=photo.get("likeCount"),
                comment_count=photo.get("commentCount"),
                share_count=photo.get("shareCount"),
            )
            progress_callback("metadata_ready", 14, "已读取快手作品信息", metadata)
            return _download_result(
                session=session,
                candidates=candidates,
                user_agent=KUAISHOU_MOBILE_UA,
                canonical_url=canonical_url,
                job_dir=job_dir,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                metadata=metadata,
                download=download,
                method="kuaishou-mobile-share",
            )
        finally:
            session.close()


def extract_kuaishou_init_state(page_text: str) -> dict[str, Any]:
    match = re.search(r"(?:window\.)?INIT_STATE\s*=\s*", page_text)
    if not match:
        return {}
    source = page_text[match.end() :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(source)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _walk(value: Any, depth: int = 0) -> Iterable[Any]:
    if depth > 14:
        return
    yield value
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:500]:
            if isinstance(child, (dict, list)):
                yield from _walk(child, depth + 1)


def find_kuaishou_photo(state: Any, target_id: str | None) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for node in _walk(state):
        if not isinstance(node, dict) or not isinstance(node.get("photo"), dict):
            continue
        photo = node["photo"]
        manifest = photo.get("manifest")
        if photo.get("photoId") is not None and isinstance(manifest, dict) and manifest.get("adaptationSet"):
            candidates.append(photo)
    if not target_id:
        return None
    return next((item for item in candidates if str(item.get("photoId")) == str(target_id)), None)


def kuaishou_target_id(url: str) -> str | None:
    query = _query_dict(url)
    for key in ("shareObjectId", "photoId", "photo_id"):
        if value := _text(query.get(key)):
            return value
    match = re.search(r"/(?:fw/photo|short-video)/([^/?#]+)", urlsplit(url).path)
    return match.group(1) if match else None


def kuaishou_media_candidates(photo: dict[str, Any]) -> list[dict[str, Any]]:
    representations: list[dict[str, Any]] = []
    manifest = photo.get("manifest") or {}
    for adaptation in manifest.get("adaptationSet") or []:
        if not isinstance(adaptation, dict):
            continue
        for representation in adaptation.get("representation") or []:
            if not isinstance(representation, dict):
                continue
            if url := _https(representation.get("url")):
                representations.append({**representation, "url": url})

    def score(item: dict[str, Any]) -> tuple[int, int, int, int]:
        return (
            int(_number(item.get("height")) or 0),
            int(_number(item.get("width")) or 0),
            int(_number(item.get("avgBitrate")) or 0),
            int(_number(item.get("fileSize")) or 0),
        )

    avc = sorted(
        (
            item
            for item in representations
            if str(item.get("videoCodec") or "").lower() in {"avc", "h264", "avc1"}
        ),
        key=score,
        reverse=True,
    )
    other = sorted((item for item in representations if item not in avc), key=score, reverse=True)
    result: list[dict[str, Any]] = []

    def add(item: dict[str, Any], kind: str) -> None:
        url = _https(item.get("url"))
        if not url or any(existing["url"] == url for existing in result):
            return
        result.append(
            {
                "url": url,
                "kind": kind,
                "codec": item.get("videoCodec"),
                "width": _number(item.get("width")),
                "height": _number(item.get("height")),
                "fps": _number(item.get("frameRate")),
                "filesize": _number(item.get("fileSize")),
                "format": item.get("qualityLabel") or item.get("quality"),
                "format_id": item.get("id"),
            }
        )

    for item in avc:
        add(item, "kuaishou_avc")
    for entry in photo.get("mainMvUrls") or []:
        if isinstance(entry, dict) and (url := _first_url(entry)):
            add({"url": url, "videoCodec": "h264"}, "kuaishou_main")
    for item in other:
        add(item, "kuaishou_fallback")
    return result


def select_jianying_template(payload: Any, template_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or str(payload.get("ret")) not in {"0", "0.0"}:
        return None
    data = payload.get("data") or {}
    templates = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(templates, list):
        return None
    return next(
        (
            item
            for item in templates
            if isinstance(item, dict)
            and template_id in {str(item.get("id") or ""), str(item.get("web_id") or "")}
        ),
        None,
    )


class JianyingProvider:
    def process(
        self,
        *,
        source_url: str,
        share_caption: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> ProviderResult:
        parsed = urlsplit(source_url)
        template_id = _query_dict(source_url).get("template_id", "")
        if (
            not _host_allowed(source_url, {"lv.ulikecam.com"})
            or parsed.path != "/activity/lv/sharevideo"
            or not template_id.isdigit()
        ):
            raise ProviderError("INVALID_JIANYING_URL", "剪映链接缺少有效的模板 ID。")

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise ProviderError(
                "BROWSER_RUNTIME_MISSING", "剪映页面解析组件未安装，请重新运行启动脚本。"
            ) from exc

        progress_callback("browser_wait", 8, "正在读取剪映公开分享页", None)
        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                    context = browser.new_context(
                        locale="zh-CN",
                        user_agent=DEFAULT_USER_AGENT,
                        viewport={"width": 1280, "height": 820},
                    )
                except PlaywrightError as exc:
                    raise ProviderError("EDGE_LAUNCH_FAILED", "无法启动剪映页面解析组件。") from exc
                page = context.new_page()
                holder: dict[str, Any] = {}
                media_responses: list[dict[str, Any]] = []

                def on_response(response: Any) -> None:
                    try:
                        response_url = response.url
                        response_parsed = urlsplit(response_url)
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type.startswith("video/") or (
                            response.request.resource_type == "media" and not content_type.startswith("audio/")
                        ):
                            if is_public_https_url(response_url):
                                media_responses.append(
                                    {
                                        "url": response_url,
                                        "content_length": int(response.headers.get("content-length") or 0),
                                    }
                                )
                        if (
                            response_parsed.scheme == "https"
                            and response_parsed.hostname == "lv-api.ulikecam.com"
                            and response_parsed.path == "/lv/v1/web/replicate/multi_get_templates"
                        ):
                            payload = response.json()
                            if selected := select_jianying_template(payload, template_id):
                                holder["template"] = selected
                    except Exception:
                        return

                page.on("response", on_response)
                try:
                    page.goto(source_url, wait_until="domcontentloaded", timeout=45_000)
                except PlaywrightTimeoutError:
                    pass
                if not _host_allowed(page.url, {"lv.ulikecam.com"}):
                    raise ProviderError(
                        "UNEXPECTED_REDIRECT", "剪映链接跳转到了登录、验证或非公开页面。"
                    )

                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not holder.get("template"):
                    if cancel_event.is_set():
                        raise JobCancelled("任务已取消")
                    page.wait_for_timeout(250)
                template = holder.get("template")
                if not isinstance(template, dict):
                    raise ProviderError(
                        "VIDEO_UNAVAILABLE", "剪映公开分享页没有返回该模板，作品可能已下架或受限。"
                    )
                if str(template.get("status")) in INVALID_JIANYING_STATUSES:
                    raise ProviderError("VIDEO_UNAVAILABLE", "该剪映模板已经下架或当前不可用。")
                if str(template.get("item_type")) in JIANYING_IMAGE_TYPES:
                    raise ProviderError("VIDEO_UNAVAILABLE", "该剪映分享内容是图片模板，不是视频。")

                canonical_url = page.url
                title = _text(template.get("title"))
                short_title = _text(template.get("short_title"))
                description = title or share_caption or short_title
                author = template.get("author") or {}
                duration = _seconds(template.get("duration"))
                metadata = _base_info(
                    source_url=source_url,
                    canonical_url=canonical_url,
                    platform="jianying",
                    method="jianying-public-page",
                    video_id=template.get("id") or template.get("web_id") or template_id,
                    title=short_title or description or "剪映模板",
                    description=description,
                    author=author.get("name") if isinstance(author, dict) else None,
                    author_id=author.get("uid") if isinstance(author, dict) else None,
                    duration=duration,
                    thumbnail=template.get("cover_url"),
                    width=template.get("cover_width"),
                    height=template.get("cover_height"),
                    timestamp=template.get("create_time"),
                    view_count=template.get("play_amount"),
                    like_count=template.get("like_count"),
                    music=(template.get("music_info") or {}).get("title")
                    if isinstance(template.get("music_info"), dict)
                    else None,
                )
                progress_callback("metadata_ready", 14, "已读取剪映模板信息", metadata)

                current_sources = page.locator("video").evaluate_all(
                    "els => els.map(v => v.currentSrc || v.src).filter(Boolean)"
                )
                candidates: list[dict[str, Any]] = []
                if video_url := _https(template.get("video_url")):
                    candidates.append({"url": video_url, "kind": "jianying_api"})
                for value in current_sources if isinstance(current_sources, list) else []:
                    if url := _https(value):
                        candidates.append({"url": url, "kind": "jianying_dom"})
                for item in sorted(media_responses, key=lambda value: value["content_length"], reverse=True):
                    candidates.append(
                        {
                            "url": item["url"],
                            "kind": "jianying_media",
                            "filesize": item["content_length"] or None,
                        }
                    )
                if download and not candidates:
                    raise ProviderError("MEDIA_URL_MISSING", "该剪映模板没有公开可下载的视频流。")

                cover_path = BrowserProvider._download_cover(
                    context, metadata.get("thumbnail"), job_dir, canonical_url
                )
                video_path = None
                if download:
                    user_agent = page.evaluate("() => navigator.userAgent") or DEFAULT_USER_AGENT
                    video_path, selected = BrowserProvider._download_media(
                        candidates,
                        context.cookies(),
                        str(user_agent),
                        canonical_url,
                        job_dir,
                        cancel_event,
                        progress_callback,
                        duration,
                    )
                    metadata.update(
                        {
                            "format": selected.get("kind"),
                            "format_id": selected.get("kind"),
                            "ext": "mp4",
                        }
                    )
                return ProviderResult(
                    metadata=metadata,
                    video_path=video_path,
                    subtitle_path=None,
                    cover_path=cover_path,
                    method="jianying-public-page",
                )
        except JobCancelled:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "JIANYING_EXTRACTION_FAILED", "剪映公开分享页解析失败，请稍后重试。"
            ) from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
