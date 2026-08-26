from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    queue TEXT NOT NULL,
    task TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'retry', 'completed', 'dead', 'cancelled')
    ),
    priority INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    request_fingerprint TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    retry_base_seconds INTEGER NOT NULL CHECK (retry_base_seconds BETWEEN 1 AND 3600),
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (queue, idempotency_key)
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
ON jobs (queue, status, available_at, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events (job_id, id ASC);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as connection:
            await connection.executescript(SCHEMA)
            await connection.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path, timeout=5)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            await connection.close()

    async def ready(self) -> bool:
        try:
            async with self.connect() as connection:
                await connection.execute("SELECT 1")
        except (OSError, aiosqlite.Error):
            return False
        return True
