from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable


class IdempotencyConflict(ValueError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    media_path TEXT,
                    original_sha256 TEXT,
                    media_info_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0
                        CHECK (progress >= 0 AND progress <= 100),
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created
                    ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def execute_once(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        callback: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        request_hash = self._request_hash(payload)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT request_hash, response_json FROM idempotency "
                    "WHERE scope = ? AND key = ?",
                    (scope, key),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key was already used with a different request."
                        )
                    response = json.loads(existing["response_json"])
                    connection.commit()
                    return response, False
                response = callback(connection)
                connection.execute(
                    "INSERT INTO idempotency "
                    "(scope, key, request_hash, response_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        scope,
                        key,
                        request_hash,
                        json.dumps(response, separators=(",", ":"), ensure_ascii=False),
                        self._now(),
                    ),
                )
                connection.commit()
                return response, True
            except Exception:
                connection.rollback()
                raise

    def create_job(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        job_id: str,
        kind: str,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        def create(connection: sqlite3.Connection) -> dict[str, Any]:
            now = self._now()
            response = {
                "job_id": job_id,
                "status": "queued",
                "progress": 0,
                "result_links": [],
            }
            connection.execute(
                "INSERT INTO jobs "
                "(id, kind, status, progress, request_json, created_at, updated_at) "
                "VALUES (?, ?, 'queued', 0, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    json.dumps(request, separators=(",", ":"), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return response

        return self.execute_once(scope, key, payload, create)

    def create_session(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        state: dict[str, Any],
        media_path: str | None,
        original_sha256: str | None,
        media_info: dict[str, Any],
    ) -> None:
        now = self._now()
        connection.execute(
            "INSERT INTO sessions "
            "(id, state_json, media_path, original_sha256, media_info_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                json.dumps(state, separators=(",", ":"), ensure_ascii=False),
                media_path,
                original_sha256,
                json.dumps(media_info, separators=(",", ":"), ensure_ascii=False),
                now,
                now,
            ),
        )

    def save_session(
        self,
        session_id: str,
        state: dict[str, Any],
    ) -> None:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "UPDATE sessions SET state_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(state, separators=(",", ":"), ensure_ascii=False),
                    self._now(),
                    session_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"session {session_id} does not exist")

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id, state_json, media_path, original_sha256, "
                "media_info_json, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "state": json.loads(row["state_json"]),
            "media_path": row["media_path"],
            "original_sha256": row["original_sha256"],
            "media_info": json.loads(row["media_info_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_job(
        self,
        job_id: str,
        status: str,
        progress: int,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, progress = ?, result_json = ?, "
                "error_json = ?, session_id = COALESCE(?, session_id), updated_at = ? "
                "WHERE id = ?",
                (
                    status,
                    progress,
                    json.dumps(result, separators=(",", ":"), ensure_ascii=False)
                    if result is not None else None,
                    json.dumps(error, separators=(",", ":"), ensure_ascii=False)
                    if error is not None else None,
                    session_id,
                    self._now(),
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"job {job_id} does not exist")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id, kind, status, progress, result_json, error_json, "
                "session_id, created_at, updated_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "progress": row["progress"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_job_counts(self) -> dict[str, int]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}
