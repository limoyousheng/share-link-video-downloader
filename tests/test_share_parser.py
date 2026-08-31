import pytest

from app.services.share_parser import ShareParseError, extract_douyin_url, parse_share


SAMPLE = (
    "6.64 大肥鱼 # deepseek # 鲸鱼娘  "
    "[https://v.douyin.com/N4PvrG9807w/](https://v.douyin.com/N4PvrG9807w/) "
    "复制此链接，打开抖音搜索，直接观看视频！ :1pm M\\@w\\.sR 05/02 hBt:/"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SAMPLE, "https://v.douyin.com/N4PvrG9807w/"),
        ("https://v.douyin.com/Ab_C-12/，", "https://v.douyin.com/Ab_C-12/"),
        ("v.douyin.com/abc123/", "https://v.douyin.com/abc123/"),
        (
            "https:\\/\\/www\\.douyin\\.com/video/7678779657860601334",
            "https://www.douyin.com/video/7678779657860601334",
        ),
        (
            "重复 https://v.douyin.com/abc/ 和 [同一个](https://v.douyin.com/abc/)",
            "https://v.douyin.com/abc/",
        ),
    ],
)
def test_extract_douyin_url(text, expected):
    assert extract_douyin_url(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "没有链接",
        "http://v.douyin.com/abc/",
        "https://douyin.com.evil.example/video/123",
        "https://user:pass@v.douyin.com/abc/",
        "https://v.douyin.com:444/abc/",
    ],
)
def test_rejects_invalid_or_unsafe_urls(text):
    with pytest.raises(ShareParseError):
        extract_douyin_url(text)


def test_rejects_multiple_distinct_urls():
    with pytest.raises(ShareParseError) as error:
        extract_douyin_url("https://v.douyin.com/one/ https://v.douyin.com/two/")
    assert error.value.code == "MULTIPLE_URLS"


def test_rejects_oversized_input():
    with pytest.raises(ShareParseError) as error:
        extract_douyin_url("x" * 12_001 + " https://v.douyin.com/a/")
    assert error.value.code == "INPUT_TOO_LONG"


@pytest.mark.parametrize(
    ("text", "platform", "expected_url", "caption_contains"),
    [
        (
            "快来看 新天游 创作的故事《战损机甲变身》！ "
            "[https://jimeng.jianying.com/s/Example\\_AbC123/?t=8011]"
            "(https://jimeng.jianying.com/s/Example_AbC123/?t=8011) BA0759，来【即梦】录入分身，一起出镜吧！",
            "jimeng",
            "https://jimeng.jianying.com/s/Example_AbC123/?t=8011",
            "战损机甲变身",
        ),
        (
            "看我在小云雀发现了什么！ https://xiaoyunque.jianying.com/s/Example-AbC123/ "
            "CA6628，点击链接或复制本条信息，打开【小云雀】App查看精彩内容！",
            "xiaoyunque",
            "https://xiaoyunque.jianying.com/s/Example-AbC123/",
            "小云雀",
        ),
        (
            "剪映模板 [链接](https://lv.ulikecam.com/activity/lv/sharevideo?template\\_id=1234567890123456789"
            "\\&sec\\_uid=abc\\&item\\_type=0) 复制此链接，打开【剪映】，热门视频抢先剪",
            "jianying",
            "https://lv.ulikecam.com/activity/lv/sharevideo?template_id=1234567890123456789&sec_uid=abc&item_type=0",
            "剪映模板",
        ),
        (
            "终于碰到真的会跳舞的ai漫剧女主了！ https://xhslink.cn/o/ExampleAbC123 "
            "先复制文字，然后进入【小红书】查看笔记。",
            "xiaohongshu",
            "https://xhslink.cn/o/ExampleAbC123",
            "ai漫剧女主",
        ),
        (
            "https://v.kuaishou.com/ExampleAbC123 给法拉利设计logo #LOGO设计 "
            "该作品在快手被播放过1,195.3万次，点击链接，打开【快手】直接观看！",
            "kuaishou",
            "https://v.kuaishou.com/ExampleAbC123",
            "法拉利",
        ),
    ],
)
def test_parse_supported_platforms(text, platform, expected_url, caption_contains):
    parsed = parse_share(text)
    assert parsed.platform == platform
    assert parsed.url == expected_url
    assert caption_contains in parsed.share_caption
    assert "http" not in parsed.share_caption
    assert "[" not in parsed.share_caption


def test_rejects_multiple_platform_urls():
    with pytest.raises(ShareParseError) as error:
        parse_share("https://xhslink.cn/o/one https://v.kuaishou.com/two")
    assert error.value.code == "MULTIPLE_URLS"


@pytest.mark.parametrize(
    "text",
    [
        "http://xhslink.cn/o/example",
        "https://xhslink.cn.evil.example/o/example",
        "https://user:pass@v.kuaishou.com/example",
        "https://lv.ulikecam.com:444/activity/lv/sharevideo?template_id=1",
    ],
)
def test_parse_share_rejects_unsafe_platform_urls(text):
    with pytest.raises(ShareParseError):
        parse_share(text)
