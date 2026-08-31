from __future__ import annotations

import gc
import json
import hashlib
import os
import re
import threading
import time
from pathlib import Path
from threading import Event
from typing import Any

from app.config import MODELS_DIR
from app.services.douyin_provider import JobCancelled, ProgressCallback, ProviderError


_TIMESTAMP_RE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
_MODEL_SPECS = {
    "tiny": (75_538_270, "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"),
    "base": (145_217_532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
    "small": (483_546_902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),
    "medium": (1_527_906_378, "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae"),
}
_MAX_NO_SPEECH_PROBABILITY = 0.75
_MIN_WORD_CONFIDENCE = 0.30
_MAX_WORD_GAP_SECONDS = 1.8
_OPENCC_CONVERTER: Any | None = None
_OPENCC_LOCK = threading.Lock()


def simplify_chinese(text: str) -> str:
    global _OPENCC_CONVERTER
    if _OPENCC_CONVERTER is None:
        with _OPENCC_LOCK:
            if _OPENCC_CONVERTER is None:
                try:
                    from opencc import OpenCC

                    _OPENCC_CONVERTER = OpenCC("t2s")
                except Exception:
                    _OPENCC_CONVERTER = False
    if _OPENCC_CONVERTER:
        try:
            text = _OPENCC_CONVERTER.convert(text)
        except Exception:
            pass
    return text


def normalize_transcript_text(text: str) -> str:
    text = simplify_chinese(text).replace("\u3000", " ")
    text = text.translate(str.maketrans({"﹐": "，", "﹔": "；", "?": "？", "!": "！"}))
    text = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.endswith(("，", ",")):
        text = text[:-1] + "。"
    return text


def _text_key(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", text).lower()


def deduplicate_transcript_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    recent_keys: list[str] = []
    for row in rows:
        text = normalize_transcript_text(str(row.get("text") or ""))
        key = _text_key(text)
        if not key or key in recent_keys[-4:]:
            continue
        cleaned = dict(row)
        cleaned["text"] = text
        result.append(cleaned)
        recent_keys.append(key)
    return result


def _segment_rows(segment: Any) -> list[dict[str, Any]]:
    no_speech_probability = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
    average_log_probability = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
    compression_ratio = float(getattr(segment, "compression_ratio", 0.0) or 0.0)
    if (
        no_speech_probability >= _MAX_NO_SPEECH_PROBABILITY
        or average_log_probability < -1.25
        or compression_ratio > 2.4
    ):
        return []

    words = [
        word
        for word in (getattr(segment, "words", None) or [])
        if getattr(word, "start", None) is not None
        and getattr(word, "end", None) is not None
        and str(getattr(word, "word", "")).strip()
    ]
    probabilities = [float(getattr(word, "probability", 0.0) or 0.0) for word in words]
    mean_probability = sum(probabilities) / len(probabilities) if probabilities else None
    if mean_probability is not None and mean_probability < _MIN_WORD_CONFIDENCE:
        return []

    base = {
        "avg_logprob": round(average_log_probability, 4),
        "no_speech_probability": round(no_speech_probability, 4),
        "confidence": round(mean_probability, 4) if mean_probability is not None else None,
    }
    if not words:
        text = normalize_transcript_text(str(getattr(segment, "text", "")))
        if not text:
            return []
        return [
            {
                **base,
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": text,
            }
        ]

    groups: list[list[Any]] = [[]]
    previous_end: float | None = None
    for word in words:
        start = float(word.start)
        if previous_end is not None and start - previous_end > _MAX_WORD_GAP_SECONDS:
            groups.append([])
        groups[-1].append(word)
        previous_end = float(word.end)

    rows: list[dict[str, Any]] = []
    for group in groups:
        if not group:
            continue
        text = normalize_transcript_text("".join(str(word.word) for word in group))
        if not text:
            continue
        group_probabilities = [float(getattr(word, "probability", 0.0) or 0.0) for word in group]
        confidence = sum(group_probabilities) / len(group_probabilities)
        if confidence < _MIN_WORD_CONFIDENCE:
            continue
        rows.append(
            {
                **base,
                "start": round(float(group[0].start), 3),
                "end": round(float(group[-1].end), 3),
                "text": text,
                "confidence": round(confidence, 4),
            }
        )
    return rows


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def _srt_timestamp(value: str) -> str:
    normalized = value.replace(".", ",")
    if normalized.split(",", 1)[0].count(":") == 1:
        normalized = f"00:{normalized}"
    return normalized


def _timestamp_seconds(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        hours, minutes, seconds = parts[-3:]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def subtitle_to_plain_text(text: str) -> str:
    lines: list[str] = []
    previous = ""
    for raw_line in text.replace("\ufeff", "").splitlines():
        line = _HTML_TAG_RE.sub("", raw_line).strip()
        if not line or line.upper() == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        if line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines).strip()


def vtt_to_srt(text: str) -> str:
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.replace("\ufeff", "").splitlines():
        line = raw_line.strip()
        if not line or line.upper() == "WEBVTT":
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if "-->" in line:
            match = _TIMESTAMP_RE.search(line)
            if match:
                line = f"{_srt_timestamp(match.group('start'))} --> {_srt_timestamp(match.group('end'))}"
        elif line.isdigit() and not current:
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))

    result: list[str] = []
    valid_blocks: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        timestamp_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timestamp_index is not None:
            valid_blocks.append("\n".join(lines[timestamp_index:]))
    for index, block in enumerate(valid_blocks, start=1):
        result.append(f"{index}\n{block}")
    return "\n\n".join(result) + ("\n" if result else "")


def srt_to_vtt(text: str) -> str:
    cues: list[str] = []
    for raw_block in re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", "").strip()):
        lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
        if lines and lines[0].isdigit():
            lines = lines[1:]
        timestamp_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timestamp_index is None:
            continue
        lines[timestamp_index] = lines[timestamp_index].replace(",", ".")
        cues.append("\n".join(lines[timestamp_index:]))
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


def subtitle_segments(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for raw_block in re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", "").strip()):
        lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
        timestamp_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timestamp_index is None:
            continue
        match = _TIMESTAMP_RE.search(lines[timestamp_index])
        if not match:
            continue
        content = normalize_transcript_text("\n".join(lines[timestamp_index + 1 :]))
        if not content:
            continue
        segments.append(
            {
                "start": round(_timestamp_seconds(match.group("start")), 3),
                "end": round(_timestamp_seconds(match.group("end")), 3),
                "text": content,
            }
        )
    return segments


def _segments_to_srt(segments: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"{index}\n{_srt_time(item['start'])} --> {_srt_time(item['end'])}\n{item['text']}"
        for index, item in enumerate(segments, start=1)
    ) + ("\n" if segments else "")


def filter_subtitle_segments(
    segments: list[dict[str, Any]],
    speech_intervals: list[dict[str, float]] | None,
) -> list[dict[str, Any]]:
    if speech_intervals is None:
        return segments
    tolerance = 0.35
    return [
        segment
        for segment in segments
        if any(
            float(segment["end"]) >= float(interval.get("start") or 0.0) - tolerance
            and float(segment["start"]) <= float(interval.get("end") or 0.0) + tolerance
            for interval in speech_intervals
        )
    ]


def use_platform_subtitle(
    source: Path,
    job_dir: Path,
    speech_intervals: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    raw_text = source.read_text(encoding="utf-8-sig", errors="replace")
    if source.suffix.lower() == ".vtt" or raw_text.lstrip().upper().startswith("WEBVTT"):
        srt_text = vtt_to_srt(raw_text)
    else:
        srt_text = raw_text
    segments = filter_subtitle_segments(subtitle_segments(srt_text), speech_intervals)
    srt_text = _segments_to_srt(segments)
    vtt_text = srt_to_vtt(srt_text)
    plain_text = normalize_transcript_text(subtitle_to_plain_text(srt_text))
    if not plain_text:
        raise ProviderError("EMPTY_PLATFORM_CAPTION", "平台字幕为空。")

    srt_path = job_dir / "transcript.srt"
    vtt_path = job_dir / "transcript.vtt"
    txt_path = job_dir / "transcript.txt"
    json_path = job_dir / "transcript.json"
    srt_path.write_text(srt_text, encoding="utf-8")
    vtt_path.write_text(vtt_text, encoding="utf-8")
    txt_path.write_text(plain_text + "\n", encoding="utf-8")
    payload = {
        "source": "platform_captions",
        "language": "zh",
        "text": plain_text,
        "segments": segments,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "text": plain_text,
        "source": "platform_captions",
        "language": "zh",
        "segments": segments,
        "paths": {"txt": txt_path, "srt": srt_path, "vtt": vtt_path, "json": json_path},
    }


class Transcriber:
    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._model_lock = threading.Lock()

    def _model(
        self,
        model_name: str,
        progress_callback: ProgressCallback,
        cancel_event: Event,
    ) -> Any:
        with self._model_lock:
            if model_name in self._models:
                return self._models[model_name]
            if self._models:
                self._models.clear()
                gc.collect()
            progress_callback(
                "loading_model",
                73,
                f"正在加载 {model_name} 语音模型；首次使用会联网下载，之后可离线复用",
                None,
            )
            try:
                # Some Windows/proxy setups map public domains to fake-IP ranges,
                # where Hugging Face's Xet transport can stall. Plain HTTP is
                # slower but substantially more compatible for first-run setup.
                os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
                os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "180")
                from faster_whisper import WhisperModel
                model_source = os.getenv("DOUYIN_MODEL_SOURCE", "auto").lower()
                local_model = None
                if model_source != "huggingface":
                    try:
                        local_model = self._ensure_modelscope_model(
                            model_name, progress_callback, cancel_event
                        )
                    except JobCancelled:
                        raise
                    except Exception:
                        if model_source == "modelscope":
                            raise
                model = WhisperModel(
                    str(local_model) if local_model else model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=min(max(os.cpu_count() or 4, 4), 12),
                    num_workers=1,
                    download_root=str(MODELS_DIR),
                    local_files_only=bool(local_model),
                )
            except JobCancelled:
                raise
            except Exception as exc:
                raise ProviderError(
                    "ASR_MODEL_LOAD_FAILED",
                    "语音模型加载失败。首次使用请保持联网，并确认磁盘空间充足。",
                ) from exc
            self._models[model_name] = model
            return model

    @staticmethod
    def _ensure_modelscope_model(
        model_name: str,
        progress_callback: ProgressCallback,
        cancel_event: Event,
    ) -> Path:
        if model_name not in _MODEL_SPECS:
            raise ValueError(f"Unsupported model: {model_name}")
        try:
            import requests
        except Exception as exc:
            raise RuntimeError("requests is unavailable") from exc

        target_dir = MODELS_DIR / f"faster-whisper-{model_name}"
        target_dir.mkdir(parents=True, exist_ok=True)
        expected_size, expected_hash = _MODEL_SPECS[model_name]
        model_path = target_dir / "model.bin"
        verified_marker = target_dir / ".model.verified"
        if model_path.is_file() and model_path.stat().st_size == expected_size:
            marker_value = (
                verified_marker.read_text(encoding="ascii", errors="ignore").strip()
                if verified_marker.exists()
                else ""
            )
            if marker_value != expected_hash:
                with model_path.open("rb") as model_file:
                    digest = hashlib.file_digest(model_file, "sha256").hexdigest()
                if digest != expected_hash:
                    raise RuntimeError("Downloaded model checksum does not match")
                verified_marker.write_text(expected_hash, encoding="ascii")

        # curl_cffi cannot load a CA bundle whose Windows path contains some
        # non-ASCII characters. The regular Requests TLS stack works with the
        # project-local certifi bundle and still supports resumable downloads.
        session = requests.Session()
        session.headers.update({"User-Agent": "video-downloader/2.1"})
        try:
            for filename in _MODEL_FILES:
                target = target_dir / filename
                expected = expected_size if filename == "model.bin" else None
                if target.is_file() and target.stat().st_size > 0 and (
                    expected is None or target.stat().st_size == expected
                ):
                    continue
                partial = target.with_suffix(target.suffix + ".part")
                url = (
                    f"https://www.modelscope.cn/models/Systran/"
                    f"faster-whisper-{model_name}/resolve/master/{filename}"
                )
                for attempt in range(1, 6):
                    existing = partial.stat().st_size if partial.exists() else 0
                    headers = {"Range": f"bytes={existing}-"} if existing else {}
                    response = None
                    try:
                        response = session.get(
                            url,
                            headers=headers,
                            stream=True,
                            timeout=(30, 180),
                            allow_redirects=True,
                        )
                        if response.status_code not in (200, 206):
                            raise RuntimeError(
                                f"Model download returned HTTP {response.status_code}"
                            )
                        content_type = (response.headers.get("content-type") or "").lower()
                        if "text/html" in content_type:
                            raise RuntimeError("Model download returned HTML")
                        append = existing > 0 and response.status_code == 206
                        downloaded = existing if append else 0
                        remaining = int(response.headers.get("content-length") or 0)
                        total = downloaded + remaining if remaining else expected or 0
                        last_report_at = 0.0
                        last_report_bytes = downloaded
                        with partial.open("ab" if append else "wb") as output:
                            for chunk in response.iter_content(chunk_size=2 * 1024 * 1024):
                                if cancel_event.is_set():
                                    raise JobCancelled("任务已取消")
                                if not chunk:
                                    continue
                                output.write(chunk)
                                downloaded += len(chunk)
                                if filename == "model.bin":
                                    now = time.monotonic()
                                    if (
                                        now - last_report_at >= 0.5
                                        or downloaded - last_report_bytes >= 4 * 1024 * 1024
                                    ):
                                        last_report_at = now
                                        last_report_bytes = downloaded
                                        progress_callback(
                                            "loading_model",
                                            73,
                                            (
                                                f"首次下载 {model_name} 语音模型 · "
                                                f"{downloaded / 1024 / 1024:.1f} / "
                                                f"{(total or expected_size) / 1024 / 1024:.1f} MB"
                                            ),
                                            None,
                                        )
                        if filename == "model.bin" and downloaded != last_report_bytes:
                            progress_callback(
                                "loading_model",
                                73,
                                (
                                    f"首次下载 {model_name} 语音模型 · "
                                    f"{downloaded / 1024 / 1024:.1f} / "
                                    f"{(total or expected_size) / 1024 / 1024:.1f} MB"
                                ),
                                None,
                            )
                        if expected is not None and partial.stat().st_size != expected:
                            raise RuntimeError(
                                f"Model size mismatch: {partial.stat().st_size} != {expected}"
                            )
                        partial.replace(target)
                        break
                    except JobCancelled:
                        raise
                    except requests.RequestException:
                        if attempt >= 5:
                            raise
                        progress_callback(
                            "loading_model",
                            73,
                            f"模型下载连接中断，正在从断点重试（{attempt}/5）",
                            None,
                        )
                        time.sleep(min(attempt * 1.5, 5.0))
                    finally:
                        if response is not None:
                            response.close()

            if not all((target_dir / filename).is_file() for filename in _MODEL_FILES):
                raise RuntimeError("Model files are incomplete")
            if not verified_marker.exists():
                with model_path.open("rb") as model_file:
                    digest = hashlib.file_digest(model_file, "sha256").hexdigest()
                if digest != expected_hash:
                    raise RuntimeError("Downloaded model checksum does not match")
                verified_marker.write_text(expected_hash, encoding="ascii")
            return target_dir
        finally:
            session.close()

    def transcribe(
        self,
        *,
        video_path: Path,
        job_dir: Path,
        model_name: str,
        language: str,
        duration: float | None,
        speech_intervals: list[dict[str, float]] | None,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> dict[str, Any]:
        clip_timestamps: str | list[float] = "0"
        if speech_intervals is not None:
            clip_timestamps = []
            for interval in speech_intervals:
                start = max(float(interval.get("start") or 0.0), 0.0)
                end = max(float(interval.get("end") or 0.0), start)
                if end - start >= 0.12:
                    clip_timestamps.extend((round(start, 3), round(end, 3)))
            if not clip_timestamps:
                raise ProviderError("NO_SPEECH", "没有检测到清晰的正常讲话。")

        model = self._model(model_name, progress_callback, cancel_event)
        if cancel_event.is_set():
            raise JobCancelled("任务已取消")

        progress_callback("transcribing", 76, "正在识别正常讲话", None)
        try:
            segments_generator, info = model.transcribe(
                str(video_path),
                language=None if language == "auto" else language,
                task="transcribe",
                beam_size=6,
                patience=1.1,
                repetition_penalty=1.15,
                no_repeat_ngram_size=3,
                temperature=0.0,
                no_speech_threshold=0.45,
                condition_on_previous_text=False,
                word_timestamps=True,
                vad_filter=speech_intervals is None,
                vad_parameters={
                    "threshold": 0.65,
                    "min_speech_duration_ms": 250,
                    "min_silence_duration_ms": 400,
                    "speech_pad_ms": 180,
                },
                clip_timestamps=clip_timestamps,
                hallucination_silence_threshold=1.5,
                # Metadata hashtags are intentionally not injected. Broad tags
                # biased music/noise toward plausible but incorrect Chinese text.
                hotwords=None,
            )
            segments: list[dict[str, Any]] = []
            for segment in segments_generator:
                if cancel_event.is_set():
                    raise JobCancelled("任务已取消")
                for row in _segment_rows(segment):
                    segments.append(row)
                    known_duration = duration or getattr(info, "duration", None)
                    ratio = min(row["end"] / known_duration, 1.0) if known_duration else 0
                    progress_callback(
                        "transcribing",
                        77 + ratio * 18,
                        f"讲话转写中 · 已识别到 {_srt_time(row['end']).split(',')[0]}",
                        None,
                    )
            segments = deduplicate_transcript_rows(segments)
        except JobCancelled:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if any(token in message for token in ("audio stream", "no audio", "failed to decode")):
                raise ProviderError("NO_AUDIO", "视频没有可识别的音轨，无法生成语音文稿。") from exc
            raise ProviderError(
                "TRANSCRIPTION_FAILED",
                "语音转写失败，视频仍可正常下载。",
            ) from exc

        if not segments:
            raise ProviderError("NO_SPEECH", "没有检测到清晰的正常讲话。")

        text = "\n".join(item["text"] for item in segments)
        srt = "\n\n".join(
            f"{index}\n{_srt_time(item['start'])} --> {_srt_time(item['end'])}\n{item['text']}"
            for index, item in enumerate(segments, start=1)
        ) + "\n"
        vtt = "WEBVTT\n\n" + "\n\n".join(
            f"{_vtt_time(item['start'])} --> {_vtt_time(item['end'])}\n{item['text']}"
            for item in segments
        ) + "\n"

        txt_path = job_dir / "transcript.txt"
        srt_path = job_dir / "transcript.srt"
        vtt_path = job_dir / "transcript.vtt"
        json_path = job_dir / "transcript.json"
        txt_path.write_text(text + "\n", encoding="utf-8")
        srt_path.write_text(srt, encoding="utf-8")
        vtt_path.write_text(vtt, encoding="utf-8")
        detected_language = str(getattr(info, "language", language))
        payload = {
            "source": "local_asr_v2",
            "model": model_name,
            "language": detected_language,
            "language_probability": getattr(info, "language_probability", None),
            "text": text,
            "segments": segments,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "text": text,
            "source": "local_asr_v2",
            "model": model_name,
            "language": detected_language,
            "segments": segments,
            "paths": {"txt": txt_path, "srt": srt_path, "vtt": vtt_path, "json": json_path},
        }
