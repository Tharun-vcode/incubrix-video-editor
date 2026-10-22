from dataclasses import replace
from pathlib import Path
import time

from fastapi.testclient import TestClient
import yaml

from app.api.application import create_app
from app.core.config import settings
from app.models.timeline import Timeline, Word


def make_client(tmp_path):
    config = replace(
        settings,
        data_dir=tmp_path,
        database_path=tmp_path / "engine.sqlite3",
        output_dir=tmp_path / "outputs",
        storage_backend="filesystem",
    )
    return TestClient(create_app(config))


def wait_for_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"Job {job_id} did not finish in time")


def test_health_and_readiness_are_available(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}


def test_configured_demo_token_protects_api_and_is_documented(tmp_path):
    config = replace(
        settings,
        data_dir=tmp_path,
        database_path=tmp_path / "engine.sqlite3",
        output_dir=tmp_path / "outputs",
        storage_backend="filesystem",
        demo_access_token="revocable-demo-token",
    )
    with TestClient(create_app(config)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/docs").status_code == 200
        assert client.get("/api/v1/openapi.json").status_code == 200
        denied = client.get("/api/v1/sessions/missing")
        allowed = client.get(
            "/api/v1/sessions/missing",
            headers={"X-Demo-Token": "revocable-demo-token"},
        )
        schema = client.app.openapi()

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "DEMO_ACCESS_REQUIRED"
    assert allowed.status_code == 404
    assert schema["components"]["securitySchemes"]["DemoAccessToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Demo-Token",
    }
    assert schema["paths"]["/api/v1/sessions/{session_id}"]["get"]["security"] == [
        {"DemoAccessToken": []},
        {},
    ]


def test_checked_in_openapi_snapshot_matches_api_contract(tmp_path):
    config = replace(
        settings,
        data_dir=tmp_path,
        database_path=tmp_path / "engine.sqlite3",
        output_dir=tmp_path / "outputs",
        storage_backend="filesystem",
    )
    with TestClient(create_app(config)) as client:
        generated = client.app.openapi()

    snapshot = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi.yaml")
        .read_text(encoding="utf-8")
    )
    assert snapshot == generated


def test_timeline_session_jobs_and_instruction_idempotency(tmp_path):
    timeline = Timeline(
        duration=10.0,
        words=[Word("w1", "hello", 1.0, 1.5), Word("w2", "world", 4.0, 4.5)],
    )
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/sessions",
            headers={"Idempotency-Key": "create-session-1"},
            json={"timeline": timeline.to_dict()},
        )

        assert response.status_code == 202
        job = wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded"
        session_id = job["result"]["session_id"]
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 200

        edit = client.post(
            f"/api/v1/sessions/{session_id}/instructions",
            headers={
                "Idempotency-Key": "edit-1",
                "X-Correlation-Id": "assessment-test",
            },
            json={"instruction": "Cut the first 2 seconds"},
        )
        retry = client.post(
            f"/api/v1/sessions/{session_id}/instructions",
            headers={"Idempotency-Key": "edit-1"},
            json={"instruction": "Cut the first 2 seconds"},
        )

        assert edit.status_code == 200
        assert retry.json() == edit.json()
        assert edit.json()["status"] == "applied"
        assert edit.headers["X-Correlation-Id"] == "assessment-test"
        assert client.get(f"/api/v1/sessions/{session_id}/timeline").json()["duration"] == 8.0


def test_reusing_idempotency_key_with_different_body_is_conflict(tmp_path):
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/sessions",
            headers={"Idempotency-Key": "create-unique"},
            json={"timeline": Timeline(duration=5).to_dict()},
        )
        session_job = wait_for_job(client, created.json()["job_id"])
        session_id = session_job["result"]["session_id"]
        endpoint = f"/api/v1/sessions/{session_id}/instructions"
        headers = {"Idempotency-Key": "same-key"}

        first = client.post(
            endpoint,
            headers=headers,
            json={"instruction": "Cut the first 1 seconds"},
        )
        conflict = client.post(
            endpoint,
            headers=headers,
            json={"instruction": "Cut the last 1 seconds"},
        )

        assert first.status_code == 200
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_post_requires_idempotency_key_and_returns_standard_error(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/sessions",
            json={"timeline": Timeline(duration=5).to_dict()},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_session_input_requires_exactly_one_source(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/sessions",
            headers={"Idempotency-Key": "bad-input"},
            json={},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SESSION_INPUT"


def test_upload_rejects_unsupported_extension(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/sessions/upload",
            headers={"Idempotency-Key": "invalid-upload"},
            files={"file": ("video.txt", b"not a video", "text/plain")},
        )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
