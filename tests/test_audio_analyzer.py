import pytest

from app.services.audio_analyzer import classify_audio_metrics


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (
            {
                "duration": 10.0,
                "overall_rms": 0.0001,
                "peak": 0.001,
                "active_ratio": 0.0,
                "speech_duration": 0.0,
                "background_active_ratio": 0.0,
                "background_rms": 0.0,
            },
            "no_audio",
        ),
        (
            {
                "duration": 30.0,
                "overall_rms": 0.18,
                "peak": 0.9,
                "active_ratio": 0.98,
                "speech_duration": 0.0,
                "background_active_ratio": 0.98,
                "background_rms": 0.18,
            },
            "non_speech",
        ),
        (
            {
                "duration": 8.0,
                "overall_rms": 0.08,
                "peak": 0.8,
                "active_ratio": 0.28,
                "speech_duration": 3.0,
                "background_active_ratio": 0.03,
                "background_rms": 0.001,
            },
            "speech_only",
        ),
        (
            {
                "duration": 12.0,
                "overall_rms": 0.14,
                "peak": 0.95,
                "active_ratio": 0.99,
                "speech_duration": 4.0,
                "background_active_ratio": 0.62,
                "background_rms": 0.08,
            },
            "speech_background",
        ),
    ],
)
def test_classify_audio_metrics(metrics, expected):
    assert classify_audio_metrics(**metrics) == expected


def test_short_vad_blip_is_not_called_normal_speech():
    kind = classify_audio_metrics(
        duration=20.0,
        overall_rms=0.12,
        peak=0.8,
        active_ratio=0.9,
        speech_duration=0.2,
        background_active_ratio=0.88,
        background_rms=0.11,
    )

    assert kind == "non_speech"
