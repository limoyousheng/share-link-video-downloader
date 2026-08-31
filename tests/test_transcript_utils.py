from types import SimpleNamespace

from app.services.transcriber import (
    _segment_rows,
    deduplicate_transcript_rows,
    filter_subtitle_segments,
    normalize_transcript_text,
    srt_to_vtt,
    subtitle_to_plain_text,
    subtitle_segments,
    use_platform_subtitle,
    vtt_to_srt,
)


def test_normalize_transcript_text_converts_traditional_chinese_and_spacing():
    assert normalize_transcript_text("那就再來一次 ！") == "那就再来一次！"


def test_segment_rows_rejects_high_no_speech_probability():
    segment = SimpleNamespace(
        start=1.0,
        end=2.0,
        text="请慢慢来",
        no_speech_prob=0.82,
        avg_logprob=-0.2,
        compression_ratio=1.0,
        words=[],
    )

    assert _segment_rows(segment) == []


def test_segment_rows_splits_words_across_a_long_gap():
    words = [
        SimpleNamespace(start=1.0, end=1.3, word="你好", probability=0.95),
        SimpleNamespace(start=4.0, end=4.4, word="世界", probability=0.9),
    ]
    segment = SimpleNamespace(
        start=1.0,
        end=4.4,
        text="你好世界",
        no_speech_prob=0.05,
        avg_logprob=-0.1,
        compression_ratio=1.0,
        words=words,
    )

    rows = _segment_rows(segment)

    assert [row["text"] for row in rows] == ["你好", "世界"]
    assert all(row["confidence"] >= 0.9 for row in rows)


def test_deduplicate_transcript_rows_removes_recent_repeated_hallucinations():
    rows = [
        {"start": 1.0, "end": 2.0, "text": "请慢慢来"},
        {"start": 2.0, "end": 3.0, "text": "请慢慢来。"},
        {"start": 3.0, "end": 4.0, "text": "知道了"},
    ]

    result = deduplicate_transcript_rows(rows)

    assert [row["text"] for row in result] == ["请慢慢来", "知道了"]


def test_subtitle_to_plain_text_deduplicates_adjacent_lines():
    content = """1
00:00:00,000 --> 00:00:01,000
你好

2
00:00:01,000 --> 00:00:02,000
你好

3
00:00:02,000 --> 00:00:03,000
世界
"""
    assert subtitle_to_plain_text(content) == "你好\n世界"


def test_vtt_to_srt():
    content = """WEBVTT

00:00:00.000 --> 00:00:01.250
第一句

00:00:01.250 --> 00:00:02.000
第二句
"""
    converted = vtt_to_srt(content)
    assert "00:00:00,000 --> 00:00:01,250" in converted
    assert "2\n00:00:01,250 --> 00:00:02,000" in converted


def test_vtt_to_srt_discards_cue_identifier():
    content = """WEBVTT

speaker-1
00:00:00.000 --> 00:00:01.000 align:start
你好
"""
    converted = vtt_to_srt(content)
    assert "speaker-1" not in converted
    assert converted == "1\n00:00:00,000 --> 00:00:01,000\n你好\n"


def test_vtt_to_srt_accepts_timestamp_without_hours():
    converted = vtt_to_srt("WEBVTT\n\n00:01.000 --> 00:02.250\n短句\n")
    assert "00:00:01,000 --> 00:00:02,250" in converted


def test_srt_to_vtt():
    content = """1
00:00:00,000 --> 00:00:01,250
第一句

2
00:00:01,250 --> 00:00:02,000
第二句
"""
    converted = srt_to_vtt(content)
    assert converted.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.250" in converted
    assert "\n1\n" not in converted


def test_platform_subtitle_detects_vtt_header_even_with_srt_suffix(tmp_path):
    source = tmp_path / "mislabelled.srt"
    source.write_text("WEBVTT\n\n00:01.000 --> 00:02.000\n你好\n", encoding="utf-8")
    result = use_platform_subtitle(source, tmp_path)
    assert result["text"] == "你好"
    assert (tmp_path / "transcript.srt").read_text(encoding="utf-8").startswith("1\n")
    assert (tmp_path / "transcript.vtt").read_text(encoding="utf-8").startswith("WEBVTT")


def test_platform_subtitle_keeps_only_clear_speech_windows(tmp_path):
    source = tmp_path / "captions.srt"
    source.write_text(
        """1
00:00:01,000 --> 00:00:02,000
歌曲歌词

2
00:00:04,000 --> 00:00:05,000
正常讲话
""",
        encoding="utf-8",
    )

    result = use_platform_subtitle(
        source,
        tmp_path,
        speech_intervals=[{"start": 3.9, "end": 5.1}],
    )

    assert result["text"] == "正常讲话"
    assert result["segments"] == [{"start": 4.0, "end": 5.0, "text": "正常讲话"}]


def test_subtitle_segment_filter_uses_overlap_tolerance():
    segments = subtitle_segments("1\n00:00:01,000 --> 00:00:02,000\n你好\n")

    filtered = filter_subtitle_segments(segments, [{"start": 2.2, "end": 2.5}])

    assert filtered == segments
