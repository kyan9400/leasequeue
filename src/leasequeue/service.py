import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from leasequeue.database import Database
from leasequeue.schemas import JobCreate


class QueueError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def decode_json(value: str) -> dict[str, Any]:
    result = json.loads(value)
    return result if isinstance(result, dict) else {}


class QueueService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_job(
        self, request: JobCreate, idempotency_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        available_at = now + timedelta(seconds=request.delay_seconds)
        payload = json.dumps(request.payload, separators=(",", ":"), sort_keys=True)
        if len(payload.encode("utf-8")) > 65_536:
            raise QueueError(413, "payload_too_large", "Job payload must not exceed 64 KiB")

        fingerprint_source = json.dumps(
            {
                "queue": request.queue,
                "task": request.task,
                "payload": request.payload,
                "priority": request.priority,
                "max_attempts": request.max_attempts,
                "delay_seconds": request.delay_seconds,
                "retry_base_seconds": request.retry_base_seconds,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()
        job_id = str(uuid.uuid4())

        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    INSERT INTO jobs (
                        id, queue, task, payload, status, priority, idempotency_key,
                        request_fingerprint, attempt, max_attempts, retry_base_seconds,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        request.queue,
                        request.task,
                        payload,
                        request.priority,
                        idempotency_key,
                        fingerprint,
                        request.max_attempts,
                        request.retry_base_seconds,
                        iso_time(available_at),
                        iso_time(now),
                        iso_time(now),
                    ),
                )
                await self._append_event(
                    connection,
                    job_id,
                    "submitted",
                    {"queue": request.queue, "task": request.task},
                    now,
                )
                row = await self._get_row(connection, job_id)
                await connection.commit()
                return self._job_view(row), False
            except aiosqlite.IntegrityError as error:
                await connection.rollback()
                if not idempotency_key:
                    raise
                row = await self._get_by_idempotency(connection, request.queue, idempotency_key)
                if row is None:
                    raise QueueError(
                        409, "submission_conflict", "Job submission conflicted"
                    ) from error
                if row["request_fingerprint"] != fingerprint:
                    raise QueueError(
                        409,
                        "idempotency_mismatch",
                        "The idempotency key was already used for a different request",
                    ) from error
                return self._job_view(row), True
            except Exception:
                await connection.rollback()
                raise

    async def list_jobs(
        self, *, queue: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if queue:
            clauses.append("queue = ?")
            parameters.append(queue)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                f"""
                SELECT * FROM jobs
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                parameters,
            )
            rows = await cursor.fetchall()
        return [self._job_view(row) for row in rows]

    async def get_job(self, job_id: str) -> dict[str, Any]:
        async with self.database.connect() as connection:
            row = await self._get_row(connection, job_id)
        return self._job_view(row)

    async def get_events(self, job_id: str) -> list[dict[str, Any]]:
        async with self.database.connect() as connection:
            await self._get_row(connection, job_id)
            cursor = await connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id ASC", (job_id,)
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "event_type": row["event_type"],
                "detail": decode_json(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def stats(self) -> dict[str, Any]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            )
            counts = {row["status"]: row["count"] for row in await cursor.fetchall()}
            oldest_cursor = await connection.execute(
                """
                SELECT MIN(created_at) AS oldest
                FROM jobs
                WHERE status IN ('queued', 'retry')
                """
            )
            oldest = (await oldest_cursor.fetchone())["oldest"]
        result = {name: counts.get(name, 0) for name in self._statuses()}
        result["total"] = sum(result.values())
        result["oldest_pending_seconds"] = None
        if oldest:
            age = utc_now() - datetime.fromisoformat(oldest)
            result["oldest_pending_seconds"] = max(0, int(age.total_seconds()))
        return result

    async def claim(
        self, *, queue: str, worker_id: str, lease_seconds: int
    ) -> tuple[dict[str, Any], str] | None:
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        token = secrets.token_urlsafe(32)
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await self._reap_expired(connection, now)
                cursor = await connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE queue = ?
                      AND status IN ('queued', 'retry')
                      AND available_at <= ?
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    """,
                    (queue, iso_time(now)),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.commit()
                    return None
                await connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', attempt = attempt + 1, lease_token = ?,
                        lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (token, worker_id, iso_time(lease_expires_at), iso_time(now), row["id"]),
                )
                await self._append_event(
                    connection,
                    row["id"],
                    "claimed",
                    {"workerId": worker_id, "leaseSeconds": lease_seconds},
                    now,
                )
                claimed = await self._get_row(connection, row["id"])
                await connection.commit()
                return self._job_view(claimed), token
            except Exception:
                await connection.rollback()
                raise

    async def heartbeat(
        self, job_id: str, lease_token: str, lease_seconds: int = 60
    ) -> dict[str, Any]:
        now = utc_now()
        expires_at = now + timedelta(seconds=lease_seconds)
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get_row(connection, job_id)
                self._require_active_lease(row, lease_token, now)
                await connection.execute(
                    "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                    (iso_time(expires_at), iso_time(now), job_id),
                )
                await self._append_event(
                    connection,
                    job_id,
                    "heartbeat",
                    {"leaseSeconds": lease_seconds},
                    now,
                )
                updated = await self._get_row(connection, job_id)
                await connection.commit()
                return self._job_view(updated)
            except Exception:
                await connection.rollback()
                raise

    async def complete(self, job_id: str, lease_token: str) -> dict[str, Any]:
        return await self._finish(job_id, lease_token, error=None)

    async def fail(self, job_id: str, lease_token: str, error: str) -> dict[str, Any]:
        return await self._finish(job_id, lease_token, error=error)

    async def redrive(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get_row(connection, job_id)
                if row["status"] != "dead":
                    raise QueueError(409, "not_dead", "Only dead-lettered jobs can be redriven")
                await connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued', attempt = 0, available_at = ?, last_error = NULL,
                        lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                        completed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (iso_time(now), iso_time(now), job_id),
                )
                await self._append_event(connection, job_id, "redriven", {}, now)
                updated = await self._get_row(connection, job_id)
                await connection.commit()
                return self._job_view(updated)
            except Exception:
                await connection.rollback()
                raise

    async def cancel(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get_row(connection, job_id)
                if row["status"] not in {"queued", "retry"}:
                    raise QueueError(409, "not_cancellable", "Only pending jobs can be cancelled")
                await connection.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (iso_time(now), job_id),
                )
                await self._append_event(connection, job_id, "cancelled", {}, now)
                updated = await self._get_row(connection, job_id)
                await connection.commit()
                return self._job_view(updated)
            except Exception:
                await connection.rollback()
                raise

    async def _finish(self, job_id: str, lease_token: str, error: str | None) -> dict[str, Any]:
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._get_row(connection, job_id)
                self._require_active_lease(row, lease_token, now)
                if error is None:
                    status = "completed"
                    available_at = row["available_at"]
                    completed_at = iso_time(now)
                    event_type = "completed"
                    detail: dict[str, Any] = {"workerId": row["lease_owner"]}
                    last_error = None
                else:
                    is_exhausted = row["attempt"] >= row["max_attempts"]
                    status = "dead" if is_exhausted else "retry"
                    delay = min(
                        row["retry_base_seconds"] * (2 ** max(0, row["attempt"] - 1)),
                        3600,
                    )
                    available_at = iso_time(now + timedelta(seconds=delay))
                    completed_at = None
                    event_type = "dead_lettered" if is_exhausted else "retry_scheduled"
                    detail = {"error": error, "retryInSeconds": None if is_exhausted else delay}
                    last_error = error
                await connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, available_at = ?, lease_token = NULL, lease_owner = NULL,
                        lease_expires_at = NULL, last_error = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        available_at,
                        last_error,
                        completed_at,
                        iso_time(now),
                        job_id,
                    ),
                )
                await self._append_event(connection, job_id, event_type, detail, now)
                updated = await self._get_row(connection, job_id)
                await connection.commit()
                return self._job_view(updated)
            except Exception:
                await connection.rollback()
                raise

    async def _reap_expired(self, connection: aiosqlite.Connection, now: datetime) -> None:
        cursor = await connection.execute(
            "SELECT * FROM jobs WHERE status = 'running' AND lease_expires_at <= ?",
            (iso_time(now),),
        )
        for row in await cursor.fetchall():
            is_exhausted = row["attempt"] >= row["max_attempts"]
            status = "dead" if is_exhausted else "retry"
            await connection.execute(
                """
                UPDATE jobs
                SET status = ?, available_at = ?, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = 'Lease expired', updated_at = ?
                WHERE id = ?
                """,
                (status, iso_time(now), iso_time(now), row["id"]),
            )
            await self._append_event(
                connection,
                row["id"],
                "dead_lettered" if is_exhausted else "lease_expired",
                {"workerId": row["lease_owner"]},
                now,
            )

    @staticmethod
    async def _append_event(
        connection: aiosqlite.Connection,
        job_id: str,
        event_type: str,
        detail: dict[str, Any],
        now: datetime,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO job_events (job_id, event_type, detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                job_id,
                event_type,
                json.dumps(detail, separators=(",", ":"), sort_keys=True),
                iso_time(now),
            ),
        )

    @staticmethod
    async def _get_row(connection: aiosqlite.Connection, job_id: str) -> aiosqlite.Row:
        cursor = await connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row is None:
            raise QueueError(404, "job_not_found", "Job not found")
        return row

    @staticmethod
    async def _get_by_idempotency(
        connection: aiosqlite.Connection, queue: str, key: str
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(
            "SELECT * FROM jobs WHERE queue = ? AND idempotency_key = ?", (queue, key)
        )
        return await cursor.fetchone()

    @staticmethod
    def _require_active_lease(row: aiosqlite.Row, token: str, now: datetime) -> None:
        stored_token = row["lease_token"] or ""
        if row["status"] != "running" or not hmac.compare_digest(stored_token, token):
            raise QueueError(409, "lease_rejected", "The lease token is invalid or inactive")
        expires_at = datetime.fromisoformat(row["lease_expires_at"])
        if expires_at <= now:
            raise QueueError(409, "lease_expired", "The lease has expired")

    @staticmethod
    def _job_view(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "queue": row["queue"],
            "task": row["task"],
            "payload": decode_json(row["payload"]),
            "status": row["status"],
            "priority": row["priority"],
            "attempt": row["attempt"],
            "max_attempts": row["max_attempts"],
            "retry_base_seconds": row["retry_base_seconds"],
            "available_at": row["available_at"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _statuses() -> tuple[str, ...]:
        return ("queued", "running", "retry", "completed", "dead", "cancelled")
