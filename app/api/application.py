from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, settings as default_settings
from app.core.logging import (
    configure_logging,
    correlation_id_var,
    job_id_var,
)
from app.db import Database, IdempotencyConflict
from app.services.jobs import JobCoordinator
from app.services.otio_export import export_otio
from app.services.timeline_engine import TimelineEngine
from app.storage.objects import create_storage

logger = logging.getLogger(__name__)


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_url: str | None = None
    timeline: dict[str, Any] | None = None
    callback_url: str | None = None


class InstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2000)
    answer: str | None = Field(default=None, max_length=1000)


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_url: str | None = None


class ApiFailure(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def _error(
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        }
    }


def _valid_correlation_id(value: str | None) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return str(uuid.uuid4())


def create_app(config: Settings = default_settings) -> FastAPI:
    if config.rate_limit_per_minute < 1 or config.job_workers < 1:
        raise ValueError("RATE_LIMIT_PER_MINUTE and JOB_WORKERS must be positive.")
    configure_logging(config.log_level)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(config.database_path)
    try:
        storage = create_storage(config)
    except (RuntimeError, ValueError, OSError) as error:
        raise RuntimeError(f"Could not initialize configured storage: {error}") from error
    jobs = JobCoordinator(database, storage, config)

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        yield
        jobs.executor.shutdown(wait=False, cancel_futures=True)

    application = FastAPI(
        title=config.app_name,
        version="1.0.0",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        description=(
            "A deterministic, source-anchored prompt video-editing API. "
            "All edit instructions use the current-timeline time policy. "
            "When DEMO_ACCESS_TOKEN is configured, send it in X-Demo-Token."
        ),
        lifespan=lifespan,
    )
    application.state.settings = config
    application.state.database = database
    application.state.storage = storage
    application.state.jobs = jobs
    application.state.session_locks: dict[str, Any] = {}
    request_windows: dict[str, deque[float]] = {}
    rate_limit_lock = threading.Lock()

    @application.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id = _valid_correlation_id(
            request.headers.get("X-Correlation-Id")
        )
        correlation_token = correlation_id_var.set(correlation_id)
        job_token = job_id_var.set("-")
        try:
            public_api_docs = {
                "/api/v1/docs",
                "/api/v1/redoc",
                "/api/v1/openapi.json",
            }
            if (
                config.demo_access_token
                and request.url.path.startswith("/api/v1/")
                and request.url.path not in public_api_docs
                and not hmac.compare_digest(
                    request.headers.get("X-Demo-Token", ""),
                    config.demo_access_token,
                )
            ):
                response = JSONResponse(
                    status_code=401,
                    content=_error(
                        "DEMO_ACCESS_REQUIRED",
                        "A valid X-Demo-Token header is required.",
                    ),
                )
                response.headers["X-Correlation-Id"] = correlation_id
                return response
            client_key = request.client.host if request.client else "unknown"
            now = time.monotonic()
            with rate_limit_lock:
                timestamps = request_windows.setdefault(client_key, deque())
                while timestamps and now - timestamps[0] >= 60:
                    timestamps.popleft()
                if len(timestamps) >= config.rate_limit_per_minute:
                    response = JSONResponse(
                        status_code=429,
                        content=_error(
                            "RATE_LIMIT_EXCEEDED",
                            "Too many requests. Retry after the current rate window.",
                            retryable=True,
                        ),
                    )
                    response.headers["X-Correlation-Id"] = correlation_id
                    return response
                timestamps.append(now)
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = correlation_id
            logger.info(
                "request completed",
                extra={
                    "event": "http.request",
                    "status_code": response.status_code,
                },
            )
            return response
        finally:
            correlation_id_var.reset(correlation_token)
            job_id_var.reset(job_token)

    @application.exception_handler(ApiFailure)
    async def handle_api_failure(request: Request, error: ApiFailure):
        del request
        return JSONResponse(
            status_code=error.status_code,
            content=_error(
                error.code,
                error.message,
                error.retryable,
                error.details,
            ),
        )

    @application.exception_handler(IdempotencyConflict)
    async def handle_idempotency_conflict(request: Request, error: IdempotencyConflict):
        del request
        return JSONResponse(
            status_code=409,
            content=_error("IDEMPOTENCY_KEY_REUSED", str(error)),
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        del request
        return JSONResponse(
            status_code=422,
            content=_error(
                "VALIDATION_ERROR",
                "The request did not match the required schema.",
                details={"issues": error.errors()},
            ),
        )

    @application.exception_handler(HTTPException)
    async def handle_http_error(request: Request, error: HTTPException):
        del request
        detail = error.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = _error(
                "HTTP_ERROR",
                str(detail),
                details={"status_code": error.status_code},
            )
        return JSONResponse(status_code=error.status_code, content=content)

    @application.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception):
        logger.exception(
            "unhandled request error",
            extra={"event": "http.error"},
        )
        del request, error
        return JSONResponse(
            status_code=500,
            content=_error(
                "INTERNAL_ERROR",
                "An unexpected internal error occurred.",
                retryable=True,
            ),
        )

    @application.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready", tags=["health"])
    def ready() -> dict[str, str]:
        database.get_job_counts()
        if not config.data_dir.is_dir():
            raise ApiFailure(503, "NOT_READY", "Configured data directory is unavailable.", True)
        try:
            storage.ready()
        except (OSError, RuntimeError) as error:
            raise ApiFailure(
                503,
                "STORAGE_NOT_READY",
                "Configured output storage is unavailable.",
                True,
            ) from error
        return {"status": "ready"}

    @application.post("/api/v1/sessions", status_code=202, tags=["sessions"])
    def create_session(
        body: SessionCreateRequest,
        request: Request,
        idempotency_key: str = Header(
            ..., alias="Idempotency-Key", min_length=1, max_length=255
        ),
    ) -> dict[str, Any]:
        if (body.timeline is None) == (body.video_url is None):
            raise ApiFailure(
                422,
                "INVALID_SESSION_INPUT",
                "Provide exactly one of video_url or timeline.",
            )
        request_data = body.model_dump(exclude_none=True)
        request_data["correlation_id"] = correlation_id_var.get()
        payload = {
            "video_url": body.video_url,
            "timeline": body.timeline,
            "callback_url": body.callback_url,
        }
        response, _ = jobs.submit(
            kind="create_session",
            scope="POST:/api/v1/sessions",
            idempotency_key=idempotency_key,
            fingerprint_payload=payload,
            request=request_data,
            task=jobs.create_session_task,
        )
        response["poll_url"] = str(request.url_for("get_job", job_id=response["job_id"]))
        return response

    @application.post(
        "/api/v1/sessions/upload",
        status_code=202,
        tags=["sessions"],
    )
    async def upload_session(
        request: Request,
        file: UploadFile = File(...),
        callback_url: str | None = None,
        idempotency_key: str = Header(
            ..., alias="Idempotency-Key", min_length=1, max_length=255
        ),
    ) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".mp4", ".mov"}:
            raise ApiFailure(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                "Only MP4 and MOV video files are accepted.",
            )
        upload_path = config.data_dir / "uploads" / f"{uuid.uuid4()}{suffix}"
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        try:
            with upload_path.open("wb") as output:
                while block := await file.read(1024 * 1024):
                    total += len(block)
                    if total > config.max_upload_bytes:
                        raise ApiFailure(
                            413,
                            "UPLOAD_TOO_LARGE",
                            "The uploaded video exceeds the configured size limit.",
                        )
                    digest.update(block)
                    output.write(block)
        except Exception:
            upload_path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        if total == 0:
            upload_path.unlink(missing_ok=True)
            raise ApiFailure(400, "EMPTY_UPLOAD", "The uploaded file is empty.")
        fingerprint = {
            "sha256": digest.hexdigest(),
            "filename": Path(file.filename or "").name,
            "callback_url": callback_url,
        }
        request_data = {
            "media_path": str(upload_path),
            "callback_url": callback_url,
            "correlation_id": correlation_id_var.get(),
        }
        try:
            response, created = jobs.submit(
                kind="create_session",
                scope="POST:/api/v1/sessions/upload",
                idempotency_key=idempotency_key,
                fingerprint_payload=fingerprint,
                request=request_data,
                task=jobs.create_session_task,
            )
        except IdempotencyConflict:
            upload_path.unlink(missing_ok=True)
            raise
        if not created:
            upload_path.unlink(missing_ok=True)
        response["poll_url"] = str(request.url_for("get_job", job_id=response["job_id"]))
        return response

    @application.get("/api/v1/jobs/{job_id}", name="get_job", tags=["jobs"])
    def get_job(job_id: str) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise ApiFailure(404, "JOB_NOT_FOUND", f"Job {job_id} was not found.")
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": job["progress"],
            "result_links": (job["result"] or {}).get("result_links", []),
            "result": job["result"],
            "error": job["error"],
        }

    @application.get("/api/v1/sessions/{session_id}", tags=["sessions"])
    def get_session(session_id: str) -> dict[str, Any]:
        session = database.get_session(session_id)
        if session is None:
            raise ApiFailure(404, "SESSION_NOT_FOUND", f"Session {session_id} was not found.")
        engine = TimelineEngine.from_state(session["state"])
        return {
            "session_id": session_id,
            "timeline_hash": engine.timeline_hash,
            "duration_s": engine.timeline.current_duration,
            "source_sha256": session["original_sha256"],
            "media_info": session["media_info"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
        }

    @application.get("/api/v1/sessions/{session_id}/timeline", tags=["sessions"])
    def get_timeline(session_id: str) -> dict[str, Any]:
        session = database.get_session(session_id)
        if session is None:
            raise ApiFailure(404, "SESSION_NOT_FOUND", f"Session {session_id} was not found.")
        return TimelineEngine.from_state(session["state"]).timeline.to_dict()

    @application.get(
        "/api/v1/sessions/{session_id}/timeline.otio",
        tags=["sessions"],
    )
    def get_otio_timeline(session_id: str) -> dict[str, Any]:
        session = database.get_session(session_id)
        if session is None:
            raise ApiFailure(404, "SESSION_NOT_FOUND", f"Session {session_id} was not found.")
        engine = TimelineEngine.from_state(session["state"])
        frame_rate = float(session["media_info"].get("frame_rate") or 30.0)
        return export_otio(
            engine.timeline,
            session["media_path"],
            frame_rate,
        )

    @application.get(
        "/api/v1/sessions/{session_id}/manifest",
        tags=["sessions"],
    )
    def get_manifest(session_id: str) -> dict[str, Any]:
        session = database.get_session(session_id)
        if session is None:
            raise ApiFailure(404, "SESSION_NOT_FOUND", f"Session {session_id} was not found.")
        engine = TimelineEngine.from_state(session["state"])
        return engine.manifest(
            provider_name=session["media_info"].get(
                "transcription_provider",
                "provided-timeline",
            ),
            provider_version=session["media_info"].get(
                "transcription_provider_version",
                "1",
            ),
            warnings=session["media_info"].get("warnings", []),
        )

    @application.post(
        "/api/v1/sessions/{session_id}/instructions",
        tags=["sessions"],
    )
    def apply_instruction(
        session_id: str,
        body: InstructionRequest,
        idempotency_key: str = Header(
            ..., alias="Idempotency-Key", min_length=1, max_length=255
        ),
    ) -> dict[str, Any]:
        payload = body.model_dump()

        def execute(connection):
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ApiFailure(
                    404,
                    "SESSION_NOT_FOUND",
                    f"Session {session_id} was not found.",
                )
            engine = TimelineEngine.from_state(json.loads(row["state_json"]))
            result = engine.execute(body.instruction, body.answer).to_dict()
            if result["status"] in {"applied", "needs_clarification"}:
                connection.execute(
                    "UPDATE sessions SET state_json = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(
                            engine.export_state(),
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        Database._now(),
                        session_id,
                    ),
                )
            result["session_id"] = session_id
            return result

        response, _ = database.execute_once(
            f"POST:/api/v1/sessions/{session_id}/instructions",
            idempotency_key,
            payload,
            execute,
        )
        return response

    @application.post(
        "/api/v1/sessions/{session_id}/render",
        status_code=202,
        tags=["renders"],
    )
    def render_session(
        session_id: str,
        body: RenderRequest,
        request: Request,
        idempotency_key: str = Header(
            ..., alias="Idempotency-Key", min_length=1, max_length=255
        ),
    ) -> dict[str, Any]:
        session = database.get_session(session_id)
        if session is None:
            raise ApiFailure(404, "SESSION_NOT_FOUND", f"Session {session_id} was not found.")
        payload = {"session_id": session_id, "callback_url": body.callback_url}
        response, _ = jobs.submit(
            kind="render",
            scope=f"POST:/api/v1/sessions/{session_id}/render",
            idempotency_key=idempotency_key,
            fingerprint_payload=payload,
            request={
                **payload,
                "correlation_id": correlation_id_var.get(),
            },
            task=jobs.render_task,
        )
        response["poll_url"] = str(request.url_for("get_job", job_id=response["job_id"]))
        return response

    @application.get("/api/v1/files/{key:path}", tags=["files"])
    def get_file(key: str) -> Response:
        try:
            content = storage.read(key)
        except (OSError, ValueError) as error:
            raise ApiFailure(404, "FILE_NOT_FOUND", "The requested output file was not found.") from error
        content_type = (
            "video/mp4" if key.lower().endswith(".mp4") else "application/json"
        )
        return Response(content, media_type=content_type)

    original_openapi = application.openapi

    def openapi_with_optional_demo_token() -> dict[str, Any]:
        schema = original_openapi()
        security_schemes = schema.setdefault("components", {}).setdefault(
            "securitySchemes", {}
        )
        security_schemes["DemoAccessToken"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-Demo-Token",
        }
        for route, operations in schema.get("paths", {}).items():
            if route.startswith("/api/v1/"):
                for method, operation in operations.items():
                    if method in {"get", "post", "put", "patch", "delete"}:
                        operation["security"] = [
                            {"DemoAccessToken": []},
                            {},
                        ]
        application.openapi_schema = schema
        return schema

    application.openapi = openapi_with_optional_demo_token
    return application


app = create_app()
