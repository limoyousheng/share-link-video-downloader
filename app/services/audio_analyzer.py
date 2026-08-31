from __future__ import annotations

import math
from pathlib import Path
from threading import Event
from typing import Any

from app.services.douyin_provider import JobCancelled, ProgressCallback, ProviderError


SAMPLE_RATE = 16_000
FRAME_SAMPLES = 480  # 30 ms at 16 kHz
ACTIVE_RMS_FLOOR = 10 ** (-50 / 20)
SILENT_RMS_FLOOR = 10 ** (-60 / 20)
SILENT_PEAK_FLOOR = 10 ** (-46 / 20)
SPEECH_KINDS = {"speech_only", "speech_background"}
AUDIO_KINDS = {*SPEECH_KINDS, "non_speech", "no_audio", "unknown"}


def classify_audio_metrics(
    *,
    duration: float,
    overall_rms: float,
    peak: float,
    active_ratio: float,
    speech_duration: float,
    background_active_ratio: float,
    background_rms: float,
) -> str:
    """Classify conservative audio features without claiming non-speech is music.

    Silero VAD provides the clear-speech decision. Energy outside those speech
    windows only tells us that background/non-speech sound exists; it cannot
    reliably distinguish music, singing, ambience, and effects.
    """

    if (
        duration <= 0
        or overall_rms < SILENT_RMS_FLOOR
        or peak < SILENT_PEAK_FLOOR
        or active_ratio < 0.02
    ):
        return "no_audio"
    if speech_duration < min(0.3, max(duration * 0.03, 0.12)):
        return "non_speech"
    has_background = (
        background_active_ratio >= 0.18
        and background_rms >= ACTIVE_RMS_FLOOR
        and background_rms >= overall_rms * 0.08
    )
    return "speech_background" if has_background else "speech_only"


def _round_metric(value: float) -> float:
    return round(float(value), 4)


def _empty_result(kind: str, *, duration: float = 0.0) -> dict[str, Any]:
    return {
        "status": "ready",
        "kind": kind,
        "method": "silero_vad_energy_v1",
        "duration": round(max(duration, 0.0), 3),
        "speech_duration": 0.0,
        "speech_ratio": 0.0,
        "active_audio_ratio": 0.0,
        "background_audio_ratio": 0.0,
        "speech_intervals": [],
    }


class AudioAnalyzer:
    def analyze(
        self,
        *,
        video_path: Path,
        has_audio: bool | None,
        cancel_event: Event,
        progress_callback: ProgressCallback,
    ) -> dict[str, Any]:
        if has_audio is False:
            return _empty_result("no_audio")
        if cancel_event.is_set():
            raise JobCancelled("任务已取消")

        progress_callback("analyzing_audio", 69, "正在区分讲话和音乐/背景声", None)
        try:
            import numpy as np
            from faster_whisper.audio import decode_audio
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            audio = decode_audio(str(video_path), sampling_rate=SAMPLE_RATE)
            audio = np.asarray(audio, dtype=np.float32)
            duration = len(audio) / SAMPLE_RATE
            if audio.size == 0:
                return _empty_result("no_audio")
            if cancel_event.is_set():
                raise JobCancelled("任务已取消")

            peak = float(np.max(np.abs(audio)))
            overall_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
            frame_count = len(audio) // FRAME_SAMPLES
            if frame_count <= 0:
                return _empty_result(
                    "no_audio" if overall_rms < SILENT_RMS_FLOOR else "unknown",
                    duration=duration,
                )

            framed = audio[: frame_count * FRAME_SAMPLES].reshape(frame_count, FRAME_SAMPLES)
            frame_rms = np.sqrt(np.mean(framed.astype(np.float64) ** 2, axis=1))
            active_mask = frame_rms >= ACTIVE_RMS_FLOOR
            active_ratio = float(np.mean(active_mask))

            vad_options = VadOptions(
                threshold=0.65,
                min_speech_duration_ms=250,
                min_silence_duration_ms=400,
                speech_pad_ms=180,
            )
            raw_chunks = get_speech_timestamps(
                audio,
                vad_options=vad_options,
                sampling_rate=SAMPLE_RATE,
            )
            intervals: list[dict[str, float]] = []
            speech_frame_mask = np.zeros(frame_count, dtype=bool)
            for chunk in raw_chunks:
                if cancel_event.is_set():
                    raise JobCancelled("任务已取消")
                start_sample = max(0, int(chunk["start"]))
                end_sample = min(len(audio), int(chunk["end"]))
                if end_sample <= start_sample:
                    continue
                start = start_sample / SAMPLE_RATE
                end = end_sample / SAMPLE_RATE
                intervals.append({"start": round(start, 3), "end": round(end, 3)})
                start_frame = max(0, start_sample // FRAME_SAMPLES)
                end_frame = min(
                    frame_count,
                    math.ceil(end_sample / FRAME_SAMPLES),
                )
                speech_frame_mask[start_frame:end_frame] = True

            speech_duration = sum(item["end"] - item["start"] for item in intervals)
            speech_ratio = min(speech_duration / duration, 1.0) if duration else 0.0
            background_mask = ~speech_frame_mask
            background_active = active_mask & background_mask
            background_active_ratio = float(np.mean(background_active))
            background_rms = (
                float(np.sqrt(np.mean(frame_rms[background_mask] ** 2)))
                if np.any(background_mask)
                else 0.0
            )
            kind = classify_audio_metrics(
                duration=duration,
                overall_rms=overall_rms,
                peak=peak,
                active_ratio=active_ratio,
                speech_duration=speech_duration,
                background_active_ratio=background_active_ratio,
                background_rms=background_rms,
            )
            if kind not in SPEECH_KINDS:
                intervals = []
                speech_duration = 0.0
                speech_ratio = 0.0

            return {
                "status": "ready",
                "kind": kind,
                "method": "silero_vad_energy_v1",
                "duration": round(duration, 3),
                "speech_duration": round(speech_duration, 3),
                "speech_ratio": _round_metric(speech_ratio),
                "active_audio_ratio": _round_metric(active_ratio),
                "background_audio_ratio": _round_metric(background_active_ratio),
                "overall_rms": _round_metric(overall_rms),
                "background_rms": _round_metric(background_rms),
                "speech_intervals": intervals,
            }
        except JobCancelled:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if any(token in message for token in ("audio stream", "no audio", "failed to decode")):
                return _empty_result("no_audio")
            raise ProviderError(
                "AUDIO_ANALYSIS_FAILED",
                "声音类型分析失败，视频仍可正常预览和下载。",
            ) from exc
