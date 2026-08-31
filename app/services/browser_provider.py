from __future__ import annotations

import json
import re
import time
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlsplit

from app.config import BROWSER_PROFILE_DIR, MAX_COVER_BYTES, MAX_MEDIA_BYTES
from app.services.douyin_provider import (
    DEFAULT_USER_AGENT,
    JobCancelled,
    ProgressCallback,
    ProviderError,
    ProviderResult,
    is_allowed_douyin_page_url,
    is_public_https_url,
)


DETAIL_URL_MARKERS = (
    "/aweme/v1/web/aweme/detail/",
    "/aweme/v1/web/aweme/post/",
    "/aweme/v1/web/module/feed/",
    "/aweme/v1/web/tab/feed/",
)


def _first_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.startswith(("https://", "http://")) else None
    if not isinstance(value, dict):
        return None
    for key in ("url_list", "UrlList", "urlList"):
        urls = value.get(key)
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    return url
    return None


def _all_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("https://", "http://")) else []
    if not isinstance(value, dict):
        return []
    result: list[str] = []
    for key in ("url_list", "UrlList", "urlList"):
        urls = value.get(key)
        if isinstance(urls, list):
            result.extend(url for url in urls if isinstance(url, str) and url.startswith(("https://", "http://")))
    return result


def _pick(mapping: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _as_number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def find_aweme_detail(payload: Any, video_id: str, depth: int = 0) -> dict[str, Any] | None:
    if depth > 10:
        return None
    if isinstance(payload, dict):
        candidate_id = str(
            _pick(payload, "aweme_id", "awemeId", "id", default="") or ""
        )
        if candidate_id == video_id and isinstance(payload.get("video"), dict):
            return payload
        for key in (
            "aweme_detail",
            "aweme_details",
            "aweme_list",
            "itemStruct",
            "itemInfo",
            "item_list",
            "data",
        ):
            if key in payload:
                found = find_aweme_detail(payload[key], video_id, depth + 1)
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = find_aweme_detail(value, video_id, depth + 1)
                if found:
                    return found
    elif isinstance(payload, list):
        for value in payload[:100]:
            found = find_aweme_detail(value, video_id, depth + 1)
            if found:
                return found
    return None


def _video_id_from_url(url: str) -> str:
    match = re.search(r"/(?:video|share/video)/(\d+)", url)
    return match.group(1) if match else ""


def parse_aweme_metadata(detail: dict[str, Any], source_url: str, canonical_url: str) -> dict[str, Any]:
    video = detail.get("video") or {}
    author = detail.get("author") or {}
    statistics = detail.get("statistics") or detail.get("stats") or {}
    music = detail.get("music") or {}
    description = str(_pick(detail, "desc", "description", default="") or "").strip()
    video_id = str(_pick(detail, "aweme_id", "awemeId", "id", default="") or "")

    duration = _as_number(_pick(video, "duration", "Duration"))
    if duration and duration > 1000:
        duration /= 1000

    cover = None
    for key in ("origin_cover", "originCover", "cover", "dynamic_cover"):
        cover = _first_url(video.get(key))
        if cover:
            break

    create_time = _as_number(_pick(detail, "create_time", "createTime"))
    hashtags = re.findall(r"#([^#\s]+)", description)
    return {
        "id": video_id,
        "title": description or "未命名视频",
        "description": description,
        "hashtags": hashtags,
        "author": _pick(author, "nickname", "unique_id", "uniqueId", default="未知作者"),
        "author_id": _pick(author, "uid", "sec_uid", "secUid", "unique_id", "uniqueId"),
        "author_url": None,
        "duration": duration,
        "thumbnail": cover.replace("http://", "https://", 1) if cover else None,
        "canonical_url": canonical_url,
        "source_url": source_url,
        "upload_date": None,
        "timestamp": create_time,
        "view_count": _as_number(_pick(statistics, "play_count", "playCount")),
        "like_count": _as_number(_pick(statistics, "digg_count", "diggCount")),
        "comment_count": _as_number(_pick(statistics, "comment_count", "commentCount")),
        "share_count": _as_number(_pick(statistics, "share_count", "shareCount")),
        "save_count": _as_number(_pick(statistics, "collect_count", "collectCount")),
        "music": _pick(music, "title"),
        "music_artists": [_pick(music, "author")] if _pick(music, "author") else None,
        "width": _as_number(_pick(video, "width", "Width")),
        "height": _as_number(_pick(video, "height", "Height")),
        "fps": None,
        "format": None,
        "format_id": None,
        "ext": "mp4",
        "extractor": "Douyin browser session",
        "resolution_method": "browser",
    }


def extract_media_candidates(detail: dict[str, Any]) -> list[dict[str, Any]]:
    video = detail.get("video") or {}
    base_width = _as_number(_pick(video, "width", "Width")) or 0
    base_height = _as_number(_pick(video, "height", "Height")) or 0
    candidates: list[dict[str, Any]] = []

    def add(address: Any, *, kind: str, parent: dict[str, Any] | None = None) -> None:
        parent = parent or {}
        urls = _all_urls(address)
        if not urls:
            return
        url_key = str(_pick(address, "url_key", "UrlKey", default="") or "") if isinstance(address, dict) else ""
        codec_text = " ".join(
            str(value or "")
            for value in (
                url_key,
                _pick(parent, "gear_name", "GearName"),
                _pick(parent, "codec_type", "CodecType"),
            )
        ).lower()
        if any(token in codec_text for token in ("bytevc2", "h266", "vvc")):
            return
        width = _as_number(_pick(address, "width", "Width")) if isinstance(address, dict) else None
        height = _as_number(_pick(address, "height", "Height")) if isinstance(address, dict) else None
        width = width or _as_number(_pick(parent, "width", "Width")) or base_width
        height = height or _as_number(_pick(parent, "height", "Height")) or base_height
        bitrate = _as_number(_pick(parent, "bit_rate", "bitRate", "Bitrate")) or 0
        size = _as_number(_pick(address, "data_size", "DataSize")) if isinstance(address, dict) else 0
        watermarked = kind == "download_addr"
        codec = "h265" if any(token in codec_text for token in ("bytevc1", "h265", "hevc")) else "h264"
        score = (
            0 if watermarked else 1,
            int(width * height),
            int(bitrate),
            int(size or 0),
            1 if codec == "h264" else 0,
        )
        for url in urls:
            candidates.append(
                {
                    "url": url.replace("http://", "https://", 1),
                    "kind": kind,
                    "codec": codec,
                    "width": width or None,
                    "height": height or None,
                    "bitrate": bitrate or None,
                    "filesize": size or None,
                    "watermarked": watermarked,
                    "score": score,
                }
            )

    for key in ("play_addr_h264", "playAddrH264", "play_addr", "playAddr", "play_addr_bytevc1"):
        add(video.get(key), kind=key)
    for bitrate in _pick(video, "bit_rate", "bitRate", "bitrateInfo", default=[]) or []:
        if isinstance(bitrate, dict):
            add(_pick(bitrate, "play_addr", "playAddr", "PlayAddr"), kind="bitrate", parent=bitrate)
    add(_pick(video, "download_addr", "downloadAddr"), kind="download_addr")

    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(candidate["url"], candidate)
    return sorted(unique.values(), key=lambda item: item["score"], reverse=True)


def extract_caption_candidates(detail: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    stickers = detail.get("interaction_stickers") or []
    for sticker in stickers if isinstance(stickers, list) else []:
        info = sticker.get("auto_video_caption_info") if isinstance(sticker, dict) else None
        captions = info.get("auto_captions") if isinstance(info, dict) else []
        for caption in captions or []:
            url = _first_url(caption.get("url")) if isinstance(caption, dict) else None
            if url:
                result.append({"url": url, "language": str(caption.get("language") or "zh"), "format": "json"})

    video = detail.get("video") or {}
    cla_info = video.get("cla_info") or video.get("claInfo") or {}
    captions = _pick(cla_info, "caption_infos", "captionInfos", default=[]) or []
    for caption in captions if isinstance(captions, list) else []:
        if not isinstance(caption, dict):
            continue
        url = _pick(caption, "url", "Url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            result.append(
                {
                    "url": url,
                    "language": str(_pick(caption, "lang", "LanguageCodeName", default="zh")),
                    "format": str(_pick(caption, "Format", "format", default="srt")).lower(),
                }
            )
    return sorted(result, key=lambda item: 0 if item["language"].lower().startswith("zh") else 1)


class BrowserProvider:
    def process(
        self,
        *,
        source_url: str,
        job_dir: Path,
        download: bool,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> ProviderResult:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise ProviderError(
                "BROWSER_RUNTIME_MISSING",
                "浏览器辅助组件未安装，请重新运行启动脚本完成依赖安装。",
            ) from exc

        progress_callback(
            "browser_wait",
            8,
            "正在打开独立 Edge；如出现验证码或登录提示，请在窗口中手动完成",
            None,
        )

        context = None
        try:
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(BROWSER_PROFILE_DIR),
                        channel="msedge",
                        headless=False,
                        locale="zh-CN",
                        accept_downloads=False,
                        viewport={"width": 1280, "height": 820},
                    )
                except PlaywrightError as exc:
                    raise ProviderError(
                        "EDGE_LAUNCH_FAILED",
                        "无法启动独立 Edge。请关闭残留的“抖音解析辅助”窗口后重试。",
                    ) from exc

                page = context.pages[0] if context.pages else context.new_page()
                detail_holder: dict[str, Any] = {}
                media_responses: list[dict[str, Any]] = []
                video_id_holder = {"value": _video_id_from_url(source_url)}

                def on_response(response: Any) -> None:
                    try:
                        response_url = response.url
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type.startswith("video/") or (
                            response.request.resource_type == "media"
                            and not content_type.startswith("audio/")
                        ):
                            media_responses.append(
                                {
                                    "url": response_url,
                                    "content_length": int(response.headers.get("content-length") or 0),
                                }
                            )
                        if not is_allowed_douyin_page_url(response_url) or not any(
                            marker in response_url for marker in DETAIL_URL_MARKERS
                        ):
                            return
                        payload = response.json()
                        target_id = video_id_holder["value"] or _video_id_from_url(page.url)
                        found = find_aweme_detail(payload, target_id)
                        if found:
                            detail_holder["detail"] = found
                    except Exception:
                        return

                page.on("response", on_response)
                try:
                    page.goto(source_url, wait_until="domcontentloaded", timeout=45_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.bring_to_front()
                except PlaywrightError:
                    pass

                canonical_url = page.url
                if not is_allowed_douyin_page_url(canonical_url):
                    raise ProviderError(
                        "UNEXPECTED_REDIRECT",
                        "短链跳转到了非抖音页面，已为安全起见停止处理。",
                    )
                video_id_holder["value"] = _video_id_from_url(canonical_url) or video_id_holder["value"]
                video_id = video_id_holder["value"]
                if not video_id:
                    raise ProviderError("VIDEO_ID_MISSING", "短链已展开，但没有识别到视频 ID。")

                wait_started = time.monotonic()
                deadline = wait_started + 150
                last_message_at = 0.0
                while time.monotonic() < deadline and not detail_holder.get("detail"):
                    if cancel_event.is_set():
                        raise JobCancelled("任务已取消")
                    if not context.pages:
                        raise ProviderError("BROWSER_CLOSED", "辅助浏览器窗口已关闭，任务未完成。")

                    self._try_page_embedded_data(page, video_id, detail_holder)
                    if detail_holder.get("detail"):
                        break
                    if time.monotonic() - wait_started > 8:
                        fallback_metadata = self._read_dom_metadata(
                            page, source_url, page.url, video_id
                        )
                        fallback_media = self._read_video_source(page)
                        if fallback_metadata.get("description") and (not download or fallback_media):
                            break
                    if time.monotonic() - last_message_at > 12:
                        last_message_at = time.monotonic()
                        progress_callback(
                            "browser_wait",
                            10,
                            "等待抖音页面完成校验；若窗口中有提示，请手动完成并保持页面开启",
                            None,
                        )
                    page.wait_for_timeout(500)

                canonical_url = page.url
                if not is_allowed_douyin_page_url(canonical_url):
                    raise ProviderError(
                        "UNEXPECTED_REDIRECT",
                        "浏览器校验后跳转到了非抖音页面，已为安全起见停止处理。",
                    )
                detail = detail_holder.get("detail")
                dom_metadata = self._read_dom_metadata(page, source_url, canonical_url, video_id)
                if detail:
                    metadata = parse_aweme_metadata(detail, source_url, canonical_url)
                    metadata = {**dom_metadata, **{key: value for key, value in metadata.items() if value not in (None, "")}}
                    api_candidates = extract_media_candidates(detail)
                else:
                    metadata = dom_metadata
                    api_candidates = []

                current_src = self._read_video_source(page)
                dom_candidates = []
                if current_src and is_public_https_url(current_src):
                    dom_candidates.append(
                        {
                            "url": current_src,
                            "kind": "dom_video",
                            "codec": None,
                            "width": None,
                            "height": None,
                            "bitrate": None,
                            "filesize": None,
                            "watermarked": False,
                            "score": (1, 0, 0, 0, 0),
                        }
                    )

                network_candidates = [
                    {
                        "url": item["url"],
                        "kind": "browser_media",
                        "codec": None,
                        "width": None,
                        "height": None,
                        "bitrate": None,
                        "filesize": item["content_length"] or None,
                        "watermarked": False,
                        "score": (1, 0, 0, item["content_length"], 0),
                    }
                    for item in sorted(media_responses, key=lambda item: item["content_length"], reverse=True)
                    if detail and is_public_https_url(item["url"])
                ]
                media_candidates = api_candidates + dom_candidates + network_candidates

                if not detail and not metadata.get("description") and not media_candidates:
                    raise ProviderError(
                        "BROWSER_VERIFICATION_TIMEOUT",
                        "等待浏览器校验超时。请确认窗口中的验证码或登录提示已经完成，再重试。",
                    )

                progress_callback("metadata_ready", 14, "已从浏览器会话读取作品信息", metadata)
                subtitle_path = self._download_caption(context, detail, job_dir, canonical_url) if detail else None
                cover_path = self._download_cover(context, metadata.get("thumbnail"), job_dir, canonical_url)
                video_path = None
                if download:
                    if not media_candidates:
                        raise ProviderError(
                            "MEDIA_URL_MISSING",
                            "页面已经打开，但没有捕获到可下载的视频流。请在辅助窗口中播放视频后重试。",
                        )
                    cookies = context.cookies()
                    user_agent = page.evaluate("() => navigator.userAgent") or DEFAULT_USER_AGENT
                    video_path, selected = self._download_media(
                        media_candidates,
                        cookies,
                        str(user_agent),
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
                            "format": f"{selected.get('codec') or 'unknown'} · {selected.get('kind')}",
                            "format_id": selected.get("kind"),
                            "ext": "mp4",
                            "watermarked": selected.get("watermarked", False),
                        }
                    )

                return ProviderResult(
                    metadata=metadata,
                    video_path=video_path,
                    subtitle_path=subtitle_path,
                    cover_path=cover_path,
                    method="browser",
                )
        except JobCancelled:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "BROWSER_EXTRACTION_FAILED",
                "浏览器辅助解析未成功，请确认链接仍有效并在窗口中完成验证。",
            ) from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    @staticmethod
    def _try_page_embedded_data(page: Any, video_id: str, holder: dict[str, Any]) -> None:
        try:
            texts = page.locator(
                "script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#RENDER_DATA, script[type='application/json']"
            ).all_text_contents()
        except Exception:
            return
        for text in texts[:30]:
            if video_id not in text or len(text) > 20_000_000:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            found = find_aweme_detail(payload, video_id)
            if found:
                holder["detail"] = found
                return

    @staticmethod
    def _read_dom_metadata(
        page: Any, source_url: str, canonical_url: str, video_id: str
    ) -> dict[str, Any]:
        def meta(selector: str) -> str | None:
            try:
                value = page.locator(selector).first.get_attribute("content", timeout=1000)
                return value.strip() if isinstance(value, str) and value.strip() else None
            except Exception:
                return None

        description = meta("meta[property='og:description']") or meta("meta[name='description']") or ""
        title = meta("meta[property='og:title']")
        if not title:
            try:
                title = page.title()
            except Exception:
                title = None
        title = (title or description or "未命名视频").removesuffix(" - 抖音").strip()
        return {
            "id": video_id,
            "title": title,
            "description": description,
            "hashtags": re.findall(r"#([^#\s]+)", description),
            "author": "未知作者",
            "author_id": None,
            "author_url": None,
            "duration": None,
            "thumbnail": meta("meta[property='og:image']"),
            "canonical_url": canonical_url,
            "source_url": source_url,
            "upload_date": None,
            "timestamp": None,
            "view_count": None,
            "like_count": None,
            "comment_count": None,
            "share_count": None,
            "save_count": None,
            "music": None,
            "music_artists": None,
            "width": None,
            "height": None,
            "fps": None,
            "format": None,
            "format_id": None,
            "ext": "mp4",
            "extractor": "Douyin browser session",
            "resolution_method": "browser",
        }

    @staticmethod
    def _read_video_source(page: Any) -> str | None:
        try:
            sources = page.locator("video").evaluate_all(
                "els => els.map(v => v.currentSrc || v.src).filter(Boolean)"
            )
            return next((item for item in sources if isinstance(item, str) and item.startswith("https://")), None)
        except Exception:
            return None

    @staticmethod
    def _download_media(
        candidates: list[dict[str, Any]],
        browser_cookies: list[dict[str, Any]],
        user_agent: str,
        referer: str,
        job_dir: Path,
        cancel_event: Event,
        progress_callback: ProgressCallback,
        expected_duration: Any = None,
    ) -> tuple[Path, dict[str, Any]]:
        try:
            from curl_cffi import requests
        except Exception as exc:
            raise ProviderError("HTTP_RUNTIME_MISSING", "下载组件未安装，请重新运行启动脚本。") from exc

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            url = item.get("url")
            if not isinstance(url, str) or url in seen or not is_public_https_url(url):
                continue
            seen.add(url)
            unique.append(item)

        last_error: Exception | None = None
        for candidate in unique[:12]:
            partial_path = job_dir / "video.mp4.part"
            final_path = job_dir / "video.mp4"
            session = None
            response = None
            try:
                session = requests.Session(impersonate="chrome")
                for cookie in browser_cookies:
                    try:
                        session.cookies.set(
                            cookie["name"],
                            cookie["value"],
                            domain=cookie.get("domain"),
                            path=cookie.get("path") or "/",
                        )
                    except Exception:
                        continue
                response = session.get(
                    candidate["url"],
                    headers={"User-Agent": user_agent, "Referer": referer, "Accept": "*/*"},
                    stream=True,
                    timeout=60,
                    allow_redirects=True,
                )
                if response.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {response.status_code}")
                if not is_public_https_url(str(response.url)):
                    raise RuntimeError("Media redirect left the public HTTPS boundary")
                content_type = (response.headers.get("content-type") or "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise RuntimeError(f"Unexpected media type: {content_type}")
                total = int(response.headers.get("content-length") or candidate.get("filesize") or 0)
                if total > MAX_MEDIA_BYTES:
                    raise ProviderError(
                        "MEDIA_TOO_LARGE",
                        "视频超过本程序的 3 GB 安全上限，已停止下载。",
                    )
                downloaded = 0
                last_update = 0.0
                with partial_path.open("wb") as target:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel_event.is_set():
                            raise JobCancelled("任务已取消")
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_MEDIA_BYTES:
                            raise ProviderError(
                                "MEDIA_TOO_LARGE",
                                "视频超过本程序的 3 GB 安全上限，已停止下载。",
                            )
                        target.write(chunk)
                        now = time.monotonic()
                        if now - last_update > 0.35:
                            last_update = now
                            ratio = min(downloaded / total, 1.0) if total else 0
                            progress_callback(
                                "downloading",
                                18 + ratio * 47,
                                f"浏览器会话下载中 · {downloaded / 1024 / 1024:.1f} MB",
                                None,
                            )
                if downloaded < 1024:
                    raise RuntimeError("Downloaded media is unexpectedly small")
                if total and downloaded != total:
                    raise RuntimeError(f"Incomplete media download: {downloaded} != {total}")
                if not BrowserProvider._validate_video_candidate(partial_path, expected_duration):
                    raise RuntimeError("Downloaded candidate is not the target video stream")
                partial_path.replace(final_path)
                return final_path, candidate
            except JobCancelled:
                partial_path.unlink(missing_ok=True)
                raise
            except ProviderError:
                partial_path.unlink(missing_ok=True)
                raise
            except Exception as exc:
                last_error = exc
                partial_path.unlink(missing_ok=True)
                continue
            finally:
                try:
                    if response is not None:
                        response.close()
                except Exception:
                    pass
                try:
                    if session is not None:
                        session.close()
                except Exception:
                    pass

        raise ProviderError(
            "MEDIA_DOWNLOAD_FAILED",
            "已找到视频流，但媒体服务器拒绝下载或返回了无效文件。",
        ) from last_error

    @staticmethod
    def _validate_video_candidate(path: Path, expected_duration: Any) -> bool:
        try:
            import av

            with av.open(str(path)) as container:
                stream = next((item for item in container.streams if item.type == "video"), None)
                if stream is None:
                    return False
                actual_duration = (
                    float(container.duration / av.time_base) if container.duration else None
                )
                expected = _as_number(expected_duration)
                if expected and actual_duration:
                    tolerance = max(3.0, float(expected) * 0.35)
                    if abs(actual_duration - float(expected)) > tolerance:
                        return False
                packet_count = 0
                for packet in container.demux(stream):
                    if packet.size:
                        packet_count += 1
                return packet_count > 0
        except Exception:
            return False

    @staticmethod
    def _download_cover(context: Any, url: Any, job_dir: Path, referer: str) -> Path | None:
        if not isinstance(url, str) or not is_public_https_url(url):
            return None
        try:
            response = context.request.get(url, headers={"Referer": referer}, timeout=15_000)
            if not response.ok or not is_public_https_url(str(response.url)):
                return None
            declared_size = int(response.headers.get("content-length") or 0)
            if declared_size > MAX_COVER_BYTES:
                return None
            body = response.body()
            if len(body) > MAX_COVER_BYTES:
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            extension = {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
            path = job_dir / f"cover{extension}"
            path.write_bytes(body)
            return path
        except Exception:
            return None

    @staticmethod
    def _download_caption(
        context: Any, detail: dict[str, Any], job_dir: Path, referer: str
    ) -> Path | None:
        for caption in extract_caption_candidates(detail)[:5]:
            caption_url = caption.get("url")
            if not isinstance(caption_url, str) or not is_public_https_url(caption_url):
                continue
            try:
                response = context.request.get(caption_url, headers={"Referer": referer}, timeout=15_000)
                if not response.ok or not is_public_https_url(str(response.url)):
                    continue
                declared_size = int(response.headers.get("content-length") or 0)
                if declared_size > 10 * 1024**2:
                    continue
                body = response.body()
                if len(body) > 10 * 1024**2:
                    continue
                text = body.decode("utf-8", errors="replace")
                if caption["format"] == "json" or text.lstrip().startswith(("{", "[")):
                    target = job_dir / "video.platform.srt"
                    payload = json.loads(text)
                    utterances = payload.get("utterances") if isinstance(payload, dict) else None
                    if not isinstance(utterances, list):
                        continue
                    blocks = []
                    for index, item in enumerate(utterances, start=1):
                        if not isinstance(item, dict) or not item.get("text"):
                            continue
                        start = (_as_number(item.get("start_time")) or 0) / 1000
                        end = (_as_number(item.get("end_time")) or start) / 1000
                        blocks.append(
                            f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{str(item['text']).strip()}"
                        )
                    if not blocks:
                        continue
                    target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
                else:
                    is_vtt = caption["format"] == "vtt" or text.lstrip().upper().startswith("WEBVTT")
                    target = job_dir / f"video.platform.{('vtt' if is_vtt else 'srt')}"
                    target.write_text(text, encoding="utf-8")
                return target
            except Exception:
                continue
        return None


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
