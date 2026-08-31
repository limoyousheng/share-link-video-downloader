from app.services.browser_provider import (
    extract_media_candidates,
    find_aweme_detail,
    parse_aweme_metadata,
)
from app.services.douyin_provider import is_public_https_url


def sample_detail():
    return {
        "aweme_id": "123456789",
        "desc": "一条测试文案 #DeepSeek #鲸鱼娘",
        "create_time": 1_700_000_000,
        "author": {"nickname": "测试作者", "uid": "author-1"},
        "statistics": {"play_count": 10000, "digg_count": 900},
        "music": {"title": "原声", "author": "测试作者"},
        "video": {
            "width": 1080,
            "height": 1920,
            "duration": 12_500,
            "cover": {"url_list": ["https://example.com/cover.jpg"]},
            "play_addr_h264": {
                "url_list": ["https://video.example.com/h264.mp4"],
                "data_size": 10_000,
            },
            "download_addr": {
                "url_list": ["https://video.example.com/watermarked.mp4"],
                "data_size": 20_000,
            },
        },
    }


def test_find_nested_aweme_detail():
    detail = sample_detail()
    assert find_aweme_detail({"data": {"aweme_list": [detail]}}, "123456789") is detail


def test_parse_metadata_and_hashtags():
    metadata = parse_aweme_metadata(
        sample_detail(),
        "https://v.douyin.com/abc/",
        "https://www.douyin.com/video/123456789",
    )
    assert metadata["author"] == "测试作者"
    assert metadata["duration"] == 12.5
    assert metadata["hashtags"] == ["DeepSeek", "鲸鱼娘"]


def test_prefers_non_watermarked_media():
    candidates = extract_media_candidates(sample_detail())
    assert candidates[0]["url"].endswith("h264.mp4")
    assert candidates[0]["watermarked"] is False


def test_public_media_url_boundary():
    assert is_public_https_url("https://video.example.com/path.mp4")
    assert not is_public_https_url("http://video.example.com/path.mp4")
    assert not is_public_https_url("https://127.0.0.1/private.mp4")
    assert not is_public_https_url("https://127.1/private.mp4")
    assert not is_public_https_url("https://0177.0.0.1/private.mp4")
    assert not is_public_https_url("https://0x7f.0.0.1/private.mp4")
    assert not is_public_https_url("https://2130706433/private.mp4")
    assert not is_public_https_url("https://metadata.internal/private.mp4")
    assert not is_public_https_url("https://user:pass@video.example.com/path.mp4")
