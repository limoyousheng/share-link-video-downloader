from __future__ import annotations

import asyncio
import hashlib
from http.cookies import SimpleCookie
from typing import Any

import httpx
import pytest

import app.main as main_module


HTTP_BASE_URL = "http://127.0.0.1:8765"
HTTPS_BASE_URL = "https://127.0.0.1:8765"


def request(
    method: str,
    path: str,
    *,
    base_url: str = HTTP_BASE_URL,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            return await client.request(method, path, headers=headers, json=json)

    return asyncio.run(run())


def make_job(job_id: str = "a" * 32) -> dict[str, Any]:
    return {
        "id": job_id,
        "status": "queued",
        "phase": "queued",
        "progress": 0,
        "message": "等待处理",
        "error_message": None,
        "artifacts": [],
    }


def test_rejects_untrusted_host_header():
    response = request("GET", "/api/v1/health", headers={"Host": "rebind.example"})
    assert response.status_code == 400


def test_rejects_cross_site_api_request():
    response = request(
        "GET",
        "/api/v1/health",
        headers={"Origin": "https://malicious.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SAME_ORIGIN_REQUIRED"


def test_accepts_same_local_origin():
    response = request(
        "GET",
        "/api/v1/health",
        headers={"Origin": HTTP_BASE_URL, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("base_url", "expects_secure"),
    [(HTTP_BASE_URL, False), (HTTPS_BASE_URL, True)],
)
def test_session_cookie_attributes_and_reuse(base_url: str, expects_secure: bool):
    async def run() -> tuple[httpx.Response, httpx.Response, str]:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            first = await client.get("/api/v1/health")
            token = client.cookies.get(main_module.SESSION_COOKIE)
            second = await client.get("/api/v1/health")
            return first, second, token or ""

    first, second, token = asyncio.run(run())
    assert first.status_code == 200
    assert main_module.SESSION_PATTERN.fullmatch(token)

    set_cookie = first.headers["set-cookie"]
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    morsel = parsed[main_module.SESSION_COOKIE]
    assert morsel["path"] == "/"
    assert morsel["httponly"] is True
    assert morsel["samesite"].lower() == "strict"
    assert bool(morsel["secure"]) is expects_secure
    assert morsel["domain"] == ""
    assert morsel["max-age"] == ""
    assert morsel["expires"] == ""
    assert "set-cookie" not in second.headers


def test_job_is_hidden_from_a_different_anonymous_session(monkeypatch: pytest.MonkeyPatch):
    job = make_job()
    captured: dict[str, Any] = {}

    def fake_create(source_text: str, options: dict[str, Any], owner_hash: str) -> dict[str, Any]:
        captured.update(source_text=source_text, options=options, owner_hash=owner_hash)
        return job

    def fake_get_for_owner(job_id: str, owner_hash: str) -> dict[str, Any] | None:
        if job_id == job["id"] and owner_hash == captured.get("owner_hash"):
            return job
        return None

    monkeypatch.setattr(main_module.manager, "create", fake_create)
    monkeypatch.setattr(main_module.store, "get_for_owner", fake_get_for_owner)

    async def run() -> tuple[httpx.Response, httpx.Response, httpx.Response, str]:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url=HTTP_BASE_URL) as owner:
            created = await owner.post(
                "/api/v1/jobs",
                json={"share_text": "https://v.douyin.com/example/"},
            )
            token = owner.cookies.get(main_module.SESSION_COOKIE) or ""
            visible = await owner.get(f"/api/v1/jobs/{job['id']}")
        async with httpx.AsyncClient(transport=transport, base_url=HTTP_BASE_URL) as stranger:
            hidden = await stranger.get(f"/api/v1/jobs/{job['id']}")
        return created, visible, hidden, token

    created, visible, hidden, token = asyncio.run(run())
    assert created.status_code == 202
    assert visible.status_code == 200
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["code"] == "JOB_NOT_FOUND"
    assert captured["owner_hash"] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert captured["options"] == {
        "download_video": True,
        "transcribe": True,
        "browser_fallback": main_module.BROWSER_FALLBACK_ENABLED,
        "asr_model": "medium",
        "language": "zh",
    }


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"description": "  作品描述  ", "title": "备用标题"},
            "作品描述",
        ),
        (
            {"description": "", "title": "  备用标题  "},
            "备用标题",
        ),
    ],
)
def test_serialize_job_published_copy_uses_description_then_title(
    metadata: dict[str, Any],
    expected: str,
):
    job = make_job()
    job["metadata"] = metadata

    serialized = main_module.serialize_job(job)

    assert serialized["copy"]["published_text"] == expected


def test_serialize_job_marks_transcript_ready():
    job = make_job()
    job.update(
        status="completed",
        options={"transcribe": True},
        metadata={
            "transcript": {
                "text": "  这是语音文稿。  ",
                "source": "faster-whisper",
            }
        },
    )

    copy = main_module.serialize_job(job)["copy"]

    assert copy["speech_text"] == "这是语音文稿。"
    assert copy["speech_status"] == "ready"
    assert copy["speech_source"] == "faster-whisper"


@pytest.mark.parametrize(
    ("kind", "label", "speech_status"),
    [
        ("speech_only", "讲话", "unavailable"),
        ("speech_background", "讲话 + 音乐/背景声", "unavailable"),
        ("non_speech", "音乐/其他声音", "not_present"),
        ("no_audio", "无声音", "not_present"),
        ("unknown", "未能判定", "unavailable"),
    ],
)
def test_serialize_job_exposes_sanitized_audio_analysis(
    kind: str,
    label: str,
    speech_status: str,
):
    job = make_job()
    job.update(
        status="completed",
        options={"transcribe": True},
        metadata={
            "audio_analysis": {"status": "ready", "kind": kind, "speech_ratio": 0.42},
            "music": "测试音乐",
            "music_artists": ["测试作者", 123, ""],
        },
    )

    serialized = main_module.serialize_job(job)

    assert serialized["audio"] == {
        "status": "ready",
        "kind": kind,
        "label": label,
        "music_title": "测试音乐",
        "music_artists": ["测试作者"],
    }
    assert serialized["copy"]["speech_status"] == speech_status
    assert "speech_ratio" not in serialized["audio"]


def test_serialize_job_marks_audio_pending_for_new_job():
    job = make_job()
    job["options"] = {"transcribe": True}

    serialized = main_module.serialize_job(job)

    assert serialized["audio"]["status"] == "pending"
    assert serialized["audio"]["kind"] == "unknown"


def test_serialize_job_keeps_legacy_transcript_without_inventing_audio_kind():
    job = make_job()
    job.update(
        status="completed",
        options={"transcribe": True},
        metadata={"transcript": {"text": "旧任务文稿", "source": "local_asr"}},
    )

    serialized = main_module.serialize_job(job)

    assert serialized["copy"]["speech_status"] == "ready"
    assert serialized["audio"]["status"] == "unavailable"
    assert serialized["audio"]["kind"] == "unknown"


def test_serialize_job_marks_requested_transcript_pending():
    job = make_job()
    job["options"] = {"transcribe": True}

    copy = main_module.serialize_job(job)["copy"]

    assert copy["speech_text"] == ""
    assert copy["speech_status"] == "pending"


def test_serialize_job_marks_terminal_transcript_unavailable():
    job = make_job()
    job.update(status="completed", options={"transcribe": True})

    copy = main_module.serialize_job(job)["copy"]

    assert copy["speech_text"] == ""
    assert copy["speech_status"] == "unavailable"


def test_serialize_job_marks_legacy_job_transcript_not_requested():
    job = make_job()
    job["status"] = "completed"

    copy = main_module.serialize_job(job)["copy"]

    assert copy["speech_text"] == ""
    assert copy["speech_status"] == "not_requested"


def test_serialize_job_does_not_expose_internal_job_fields():
    job = make_job()
    job.update(
        metadata={
            "description": "可公开的发布文案",
            "private_metadata": "metadata-secret",
        },
        options={
            "transcribe": True,
            "internal_option": "options-secret",
        },
        owner_hash="owner-secret",
    )

    serialized = main_module.serialize_job(job)

    assert serialized["copy"]["published_text"] == "可公开的发布文案"
    assert "metadata" not in serialized
    assert "options" not in serialized
    assert "owner_hash" not in serialized
    assert "metadata-secret" not in repr(serialized)
    assert "options-secret" not in repr(serialized)
    assert "owner-secret" not in repr(serialized)


def test_job_list_route_is_not_available(monkeypatch: pytest.MonkeyPatch):
    def unexpected_list(*_: Any, **__: Any) -> None:
        raise AssertionError("The removed list route must not query the store")

    monkeypatch.setattr(main_module.store, "list_recent", unexpected_list)
    response = request("GET", "/api/v1/jobs")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/api/v1/jobs/{'a' * 32}/cancel"),
        ("POST", f"/api/v1/jobs/{'a' * 32}/retry"),
        ("GET", f"/api/v1/jobs/{'a' * 32}/artifacts/video"),
    ],
)
def test_removed_job_routes_are_not_available(method: str, path: str):
    response = request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("download_video", False),
        ("transcribe", True),
        ("browser_fallback", False),
        ("asr_model", "medium"),
        ("language", "auto"),
    ],
)
def test_job_create_rejects_extra_parameters(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
):
    def unexpected_create(*_: Any, **__: Any) -> None:
        raise AssertionError("Validation must reject the request before manager.create")

    monkeypatch.setattr(main_module.manager, "create", unexpected_create)
    response = request(
        "POST",
        "/api/v1/jobs",
        json={"share_text": "https://v.douyin.com/example/", field: value},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
