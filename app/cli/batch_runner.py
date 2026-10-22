from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any
import uuid

import httpx
import yaml


class BatchRunner:
    def __init__(
        self,
        base_url: str,
        output_dir: Path,
        timeout_seconds: int = 900,
    ):
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir
        self.timeout_seconds = timeout_seconds
        self.client = httpx.Client(
            timeout=httpx.Timeout(60, connect=10),
            headers={"X-Correlation-Id": str(uuid.uuid4())},
        )

    def run_file(self, session_file: Path) -> dict[str, Any]:
        session_file = session_file.resolve()
        payload = yaml.safe_load(session_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
            raise ValueError(f"{session_file.name} must contain a steps list.")
        session_id = self._create_session(session_file, payload)
        output_dir = self.output_dir / session_file.stem
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        for index, step in enumerate(payload["steps"], start=1):
            if not isinstance(step, dict) or not isinstance(step.get("say"), str):
                raise ValueError(f"{session_file.name} step {index} must contain say.")
            instruction = step["say"]
            if instruction.strip().lower() == "render":
                response = self._post(
                    f"/api/v1/sessions/{session_id}/render",
                    {"callback_url": payload.get("callback_url")},
                    self._key(session_file, index, "render"),
                )
                render_job = self._wait_for_job(response["job_id"])
                response = {
                    "status": render_job["status"],
                    "operations": [{"kind": "render"}],
                    "summary": "Render job completed.",
                    "question": None,
                    "options": [],
                    "render_result": render_job.get("result"),
                    "error": render_job.get("error"),
                }
            else:
                response = self._post(
                    f"/api/v1/sessions/{session_id}/instructions",
                    {"instruction": instruction, "answer": step.get("answer")},
                    self._key(session_file, index, "instruction"),
                )
            results.append(
                {
                    "step": index,
                    "instruction": instruction,
                    "answer": step.get("answer"),
                    **response,
                }
            )

        session_response = self._get(f"/api/v1/sessions/{session_id}")
        timeline = self._get(f"/api/v1/sessions/{session_id}/timeline")
        output = {
            "session_id": session_id,
            "steps": results,
            "timeline_hash": session_response["timeline_hash"],
            "duration_s": session_response["duration_s"],
            "final_timeline": timeline,
            "final_video": self._latest_render_link(results),
        }
        (output_dir / "timeline.json").write_text(
            json.dumps(timeline, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (output_dir / "results.json").write_text(
            json.dumps(output, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return output

    def _create_session(self, session_file: Path, payload: dict[str, Any]) -> str:
        timeline = payload.get("timeline")
        timeline_file = payload.get("timeline_file")
        if timeline_file:
            path = (session_file.parent / timeline_file).resolve()
            timeline = json.loads(path.read_text(encoding="utf-8"))
        if timeline is not None:
            response = self._post(
                "/api/v1/sessions",
                {
                    "timeline": timeline,
                    "callback_url": payload.get("callback_url"),
                },
                self._key(session_file, 0, "create"),
            )
        else:
            video = payload.get("video")
            if not video:
                raise ValueError(
                    f"{session_file.name} must provide timeline/timeline_file or video."
                )
            video_path = (session_file.parent / str(video)).resolve()
            if video_path.is_file():
                with video_path.open("rb") as source:
                    response = self._post_upload(
                        video_path,
                        source,
                        payload.get("callback_url"),
                        self._key(session_file, 0, "create"),
                    )
            else:
                response = self._post(
                    "/api/v1/sessions",
                    {
                        "video_url": str(video),
                        "callback_url": payload.get("callback_url"),
                    },
                    self._key(session_file, 0, "create"),
                )
        job = self._wait_for_job(response["job_id"])
        if job["status"] != "succeeded":
            raise RuntimeError(
                f"Session creation failed for {session_file.name}: {job.get('error')}"
            )
        result = job.get("result") or {}
        if not result.get("session_id"):
            raise RuntimeError("Session creation job returned no session_id.")
        return result["session_id"]

    def _post_upload(
        self,
        path: Path,
        source,
        callback_url: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/api/v1/sessions/upload",
            headers={"Idempotency-Key": idempotency_key},
            params={"callback_url": callback_url} if callback_url else None,
            files={"file": (path.name, source, "video/mp4")},
        )
        return self._decode(response)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}{path}",
            headers={"Idempotency-Key": idempotency_key},
            json=payload,
        )
        return self._decode(response)

    def _get(self, path: str) -> dict[str, Any]:
        return self._decode(self.client.get(f"{self.base_url}{path}"))

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(
                f"API returned non-JSON HTTP {response.status_code}."
            ) from error
        if response.is_error:
            raise RuntimeError(
                f"API returned HTTP {response.status_code}: {payload}"
            )
        return payload

    def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            job = self._get(f"/api/v1/jobs/{job_id}")
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.25)
        raise TimeoutError(f"Job {job_id} did not finish within the configured timeout.")

    @staticmethod
    def _key(session_file: Path, step: int, action: str) -> str:
        digest = hashlib.sha256(session_file.read_bytes()).hexdigest()[:16]
        return f"{digest}-{step:04d}-{action}"

    @staticmethod
    def _latest_render_link(results: list[dict[str, Any]]) -> str | None:
        for step in reversed(results):
            result = step.get("render_result") or {}
            links = result.get("result_links") or []
            if links:
                return links[0]
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one or more scripted sessions through the HTTP API."
    )
    parser.add_argument("input", type=Path, help="Session YAML file or folder")
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000",
        help="Base URL for the editing API",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("batch-output"),
        help="Folder for per-session results.json and timeline.json",
    )
    parser.add_argument("--timeout", type=int, default=900, help="Job timeout in seconds")
    arguments = parser.parse_args()
    files = (
        sorted(
            list(arguments.input.glob("*.yaml"))
            + list(arguments.input.glob("*.yml"))
        )
        if arguments.input.is_dir()
        else [arguments.input]
    )
    if not files:
        raise SystemExit(f"No YAML session files found in {arguments.input}.")
    runner = BatchRunner(arguments.api_url, arguments.output, arguments.timeout)
    failures: list[str] = []
    try:
        for session_file in files:
            try:
                result = runner.run_file(session_file)
                print(
                    f"{session_file.name}: {result['timeline_hash']} "
                    f"({result['duration_s']:.3f}s)"
                )
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                failures.append(f"{session_file.name}: {error}")
                print(f"{session_file.name}: FAILED: {error}")
    finally:
        runner.client.close()
    if failures:
        raise SystemExit(f"{len(failures)} session(s) failed.")


if __name__ == "__main__":
    main()
