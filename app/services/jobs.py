from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import hashlib
import hmac
import json
import logging
from pathlib import Path
import socket
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import ipaddress
from typing import Any, Callable

from app.core.config import Settings
from app.core.logging import correlation_id_var
from app.db import Database
from app.media.intake import (
    MediaError,
    download_video_url,
    probe_video,
    sha256_file,
    timeline_from_transcript,
)
from app.media.render import FFmpegRenderer
from app.models.timeline import Timeline
from app.providers.transcription import create_transcriber
from app.services.timeline_engine import TimelineEngine
from app.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int], None]
JobTask = Callable[[str, dict[str, Any], ProgressCallback], dict[str, Any]]


class JobCoordinator:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        settings: Settings,
        max_workers: int | None = None,
    ):
        self.database = database
        self.storage = storage
        self.settings = settings
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers or settings.job_workers,
            thread_name_prefix="video-edit-job",
        )
        self._transcriber = None
        self._futures: dict[str, Future[Any]] = {}

    def submit(
        self,
        kind: str,
        scope: str,
        idempotency_key: str,
        fingerprint_payload: dict[str, Any],
        request: dict[str, Any],
        task: JobTask,
    ) -> tuple[dict[str, Any], bool]:
        job_id = str(uuid.uuid4())
        response, created = self.database.create_job(
            scope,
            idempotency_key,
            fingerprint_payload,
            job_id,
            kind,
            request,
        )
        if created:
            self._futures[job_id] = self.executor.submit(
                self._run,
                job_id,
                request,
                task,
            )
        return response, created

    def _run(self, job_id: str, request: dict[str, Any], task: JobTask) -> None:
        correlation_token = correlation_id_var.set(
            request.get("correlation_id", "-")
        )
        def update_progress(progress: int) -> None:
            self.database.update_job(
                job_id,
                "running",
                max(1, min(99, progress)),
            )

        try:
            self.database.update_job(job_id, "running", 1)
            logger.info("job started", extra={"job_id": job_id, "event": "job.started"})
            result = task(job_id, request, update_progress)
            session_id = result.get("session_id")
            self.database.update_job(
                job_id,
                "succeeded",
                100,
                result=result,
                session_id=session_id,
            )
            logger.info(
                "job succeeded",
                extra={"job_id": job_id, "event": "job.succeeded"},
            )
            self._notify_callback(job_id, request.get("callback_url"), result)
        except (MediaError, ValueError, KeyError) as error:
            failure = {
                "error": {
                    "code": "MEDIA_PROCESSING_FAILED",
                    "message": str(error),
                    "retryable": False,
                    "details": {},
                }
            }
            self.database.update_job(
                job_id,
                "failed",
                100,
                error=failure,
            )
            logger.warning(
                "job failed",
                extra={
                    "job_id": job_id,
                    "event": "job.failed",
                    "error_code": "MEDIA_PROCESSING_FAILED",
                },
            )
        except Exception as error:
            failure = {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "The background job failed unexpectedly.",
                    "retryable": True,
                    "details": {},
                }
            }
            self.database.update_job(
                job_id,
                "failed",
                100,
                error=failure,
            )
            logger.exception(
                "background job failed unexpectedly",
                extra={"job_id": job_id, "event": "job.failed"},
            )
        finally:
            correlation_id_var.reset(correlation_token)
            self._futures.pop(job_id, None)

    def get_transcriber(self):
        if self._transcriber is None:
            self._transcriber = create_transcriber(self.settings)
        return self._transcriber

    def create_session_task(
        self,
        job_id: str,
        request: dict[str, Any],
        progress: ProgressCallback,
    ) -> dict[str, Any]:
        media_path: Path | None = None
        media_info: dict[str, Any] = {}
        transcript_provider = "provided-timeline"
        provider_version = "1"
        timings: dict[str, float] = {}
        started_at = time.perf_counter()
        if request.get("timeline") is not None:
            timeline = Timeline.from_dict(request["timeline"])
            progress(45)
        else:
            media_path = Path(request["media_path"]) if request.get("media_path") else None
            if request.get("video_url"):
                media_path = self.settings.data_dir / "uploads" / f"{job_id}.mp4"
                download_video_url(
                    request["video_url"],
                    media_path,
                    self.settings,
                )
            if media_path is None:
                raise MediaError("A video file, video URL, or timeline is required.")
            progress(15)
            probe_started = time.perf_counter()
            media_info = probe_video(media_path, self.settings)
            timings["probe"] = (time.perf_counter() - probe_started) * 1000
            progress(35)
            provider = self.get_transcriber()
            transcription_started = time.perf_counter()
            words = provider.transcribe(media_path)
            timings["transcription"] = (
                time.perf_counter() - transcription_started
            ) * 1000
            transcript_provider = provider.name
            provider_version = provider.version
            media_info["transcription_provider"] = provider.name
            media_info["transcription_provider_version"] = provider.version
            timeline = timeline_from_transcript(
                media_info["duration_s"],
                [
                    {
                        "id": word.id,
                        "text": word.text,
                        "start": word.start,
                        "end": word.end,
                        "speaker": word.speaker,
                        "confidence": word.confidence,
                    }
                    for word in words
                ],
            )
            timeline.warnings.extend(media_info["warnings"])
            if not media_info["has_audio"]:
                timeline.warnings.append(
                    "Source has no audio; rendered output will contain silence."
                )
            if media_info["variable_frame_rate"]:
                timeline.warnings.append(
                    "Variable frame rate detected; FFmpeg normalizes timing during render."
                )
            if any(word.speaker is None for word in timeline.words):
                timeline.warnings.append(
                    "The configured transcription provider does not assign speaker labels."
                )
            low_confidence = [
                word.id for word in timeline.words if word.confidence < 0.55
            ]
            if low_confidence:
                timeline.warnings.append(
                    "Low-confidence transcript words require confirmation before "
                    "bleeping: " + ", ".join(low_confidence[:20])
                )
            overlapping = any(
                left.speaker != right.speaker
                and left.speaker is not None
                and right.speaker is not None
                and left.start < right.end
                and left.end > right.start
                for index, left in enumerate(timeline.words)
                for right in timeline.words[index + 1:]
            )
            if overlapping:
                timeline.warnings.append(
                    "Overlapping speech with different speaker labels was detected."
                )
            progress(55)

        session_id = str(uuid.uuid4())
        engine = TimelineEngine(timeline)
        manifest = engine.manifest(transcript_provider, provider_version)
        timings["session_setup"] = (time.perf_counter() - started_at) * 1000
        manifest["timings_ms"] = timings
        if media_path:
            manifest["source"] = {
                "sha256": media_info["source_sha256"],
                "media_info": media_info,
            }
        timeline_key, manifest_key = self._save_session_artifacts(
            session_id,
            timeline.to_dict(),
            manifest,
        )
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.database.create_session(
                    connection,
                    session_id,
                    engine.export_state(),
                    str(media_path) if media_path else None,
                    media_info.get("source_sha256"),
                    media_info,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        progress(90)
        return {
            "session_id": session_id,
            "timeline_hash": engine.timeline_hash,
            "duration_s": timeline.current_duration,
            "manifest": manifest,
            "result_links": [
                self.storage.download_url(timeline_key),
                self.storage.download_url(manifest_key),
            ],
            "source_sha256": media_info.get("source_sha256"),
        }

    def render_task(
        self,
        job_id: str,
        request: dict[str, Any],
        progress: ProgressCallback,
    ) -> dict[str, Any]:
        session_id = request["session_id"]
        session = self.database.get_session(session_id)
        if session is None:
            raise KeyError(f"session {session_id} does not exist")
        media_path = session["media_path"]
        if not media_path:
            raise MediaError("This timeline-only session has no source video to render.")
        engine = TimelineEngine.from_state(session["state"])
        output_path = self.settings.data_dir / "renders" / f"{job_id}.mp4"
        progress(20)
        render_started = time.perf_counter()
        render_info = FFmpegRenderer(self.settings).render(
            Path(media_path),
            engine.timeline,
            output_path,
            has_audio=bool(session["media_info"].get("has_audio")),
        )
        render_duration_ms = (time.perf_counter() - render_started) * 1000
        progress(80)
        video_key = f"sessions/{session_id}/renders/{job_id}.mp4"
        self.storage.put(video_key, output_path, "video/mp4")
        manifest = engine.manifest()
        manifest["models"] = [{
            "provider": session["media_info"].get(
                "transcription_provider",
                self.settings.transcriber,
            ),
            "version": session["media_info"].get(
                "transcription_provider_version",
                "configured",
            ),
        }]
        manifest["timings_ms"] = {"render": render_duration_ms}
        manifest["render"] = {
            "duration_s": render_info["duration_s"],
            "expected_duration_s": render_info["expected_duration_s"],
            "length_difference_frames": render_info["length_difference_frames"],
            "av_offset_frames": render_info["av_offset_frames"],
            "frame_rate": render_info["frame_rate"],
        }
        manifest_key = self._write_json(
            f"sessions/{session_id}/renders/{job_id}-manifest.json",
            manifest,
        )
        return {
            "session_id": session_id,
            "render": render_info,
            "manifest": manifest,
            "result_links": [
                self.storage.download_url(video_key),
                self.storage.download_url(manifest_key),
            ],
        }

    def _save_session_artifacts(
        self,
        session_id: str,
        timeline: dict[str, Any],
        manifest: dict[str, Any],
    ) -> tuple[str, str]:
        timeline_key = f"sessions/{session_id}/timeline.json"
        manifest_key = f"sessions/{session_id}/manifest.json"
        self._write_json(timeline_key, timeline)
        self._write_json(manifest_key, manifest)
        return timeline_key, manifest_key

    def _write_json(self, key: str, payload: dict[str, Any]) -> str:
        path = self.settings.data_dir / "work" / f"{uuid.uuid4()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            return self.storage.put(key, path, "application/json")
        finally:
            path.unlink(missing_ok=True)

    def _notify_callback(
        self,
        job_id: str,
        callback_url: str | None,
        result: dict[str, Any],
    ) -> None:
        if not callback_url:
            return
        if not self.settings.callback_secret:
            logger.warning(
                "callback URL ignored because CALLBACK_SECRET is not configured",
                extra={"job_id": job_id, "event": "callback.skipped"},
            )
            return
        parts = urlsplit(callback_url)
        if parts.scheme != "https" or not parts.hostname:
            logger.warning(
                "callback URL must use HTTPS",
                extra={"job_id": job_id, "event": "callback.rejected"},
            )
            return
        try:
            for address in socket.getaddrinfo(parts.hostname, parts.port or 443):
                if not ipaddress.ip_address(address[4][0]).is_global:
                    raise ValueError("callback host resolves to a private address")
            body = json.dumps(
                {"job_id": job_id, "status": "succeeded", "result": result},
                separators=(",", ":"),
            ).encode("utf-8")
            signature = hmac.new(
                self.settings.callback_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            request = Request(
                callback_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-IncuBrix-Signature": f"sha256={signature}",
                },
                method="POST",
            )

            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            with build_opener(NoRedirect).open(request, timeout=5) as response:
                if response.status >= 300:
                    raise OSError(f"callback returned HTTP {response.status}")
        except (OSError, ValueError) as error:
            logger.warning(
                "callback delivery failed: %s",
                error,
                extra={
                    "job_id": job_id,
                    "event": "callback.failed",
                },
            )
