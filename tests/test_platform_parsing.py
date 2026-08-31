from app.services.platform_providers import (
    extract_kuaishou_init_state,
    find_kuaishou_photo,
    kuaishou_media_candidates,
    kuaishou_target_id,
    select_jianying_template,
)


def test_extracts_exact_kuaishou_photo_and_prefers_avc():
    state = {
        "obfuscated": {
            "feed": [
                {
                    "photo": {
                        "photoId": "recommended",
                        "manifest": {"adaptationSet": [{"representation": []}]},
                    }
                },
                {
                    "photo": {
                        "photoId": "target",
                        "caption": "目标文案",
                        "mainMvUrls": [{"url": "https://media.example.com/main.mp4"}],
                        "manifest": {
                            "adaptationSet": [
                                {
                                    "representation": [
                                        {
                                            "id": "hevc-1080",
                                            "url": "https://media.example.com/hevc.mp4",
                                            "videoCodec": "hevc",
                                            "width": 1080,
                                            "height": 1920,
                                            "fileSize": 200,
                                        },
                                        {
                                            "id": "avc-720",
                                            "url": "https://media.example.com/avc.mp4",
                                            "videoCodec": "avc",
                                            "width": 720,
                                            "height": 1280,
                                            "fileSize": 100,
                                        },
                                    ]
                                }
                            ]
                        },
                    }
                },
            ]
        }
    }
    html = f"<script>window.INIT_STATE = {__import__('json').dumps(state)};</script>"

    parsed = extract_kuaishou_init_state(html)
    photo = find_kuaishou_photo(parsed, "target")
    candidates = kuaishou_media_candidates(photo or {})

    assert photo and photo["caption"] == "目标文案"
    assert candidates[0]["url"].endswith("avc.mp4")
    assert candidates[0]["codec"] == "avc"
    assert candidates[-1]["url"].endswith("hevc.mp4")


def test_does_not_fall_back_to_recommended_kuaishou_photo():
    state = {
        "x": {
            "photo": {
                "photoId": "other",
                "manifest": {"adaptationSet": [{"representation": [{}]}]},
            }
        }
    }
    assert find_kuaishou_photo(state, "target") is None
    assert find_kuaishou_photo(state, None) is None


def test_extracts_kuaishou_target_id_from_final_url():
    assert kuaishou_target_id(
        "https://v.m.chenzhongtech.com/fw/photo/slug?shareObjectId=5248945574736656706"
    ) == "5248945574736656706"
    assert kuaishou_target_id("https://www.kuaishou.com/short-video/3xxv9299atraxie") == "3xxv9299atraxie"


def test_selects_requested_jianying_template_only():
    payload = {
        "ret": "0",
        "data": {
            "templates": [
                {"id": "other", "video_url": "https://media.example.com/other.mp4"},
                {"web_id": "target", "video_url": "https://media.example.com/target.mp4"},
            ]
        },
    }
    selected = select_jianying_template(payload, "target")
    assert selected and selected["video_url"].endswith("target.mp4")
    assert select_jianying_template(payload, "missing") is None
    assert select_jianying_template({"ret": "1", "data": payload["data"]}, "target") is None
