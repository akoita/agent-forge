"""SQLite-backed durable run store for the hosted agent-forge service.

Provides atomic run creation with per-owner idempotency keys, lease-based
worker claiming with fencing tokens, and recovery queries across API/worker
restarts.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


def _format_dt(value: datetime) -> str:
    """Format a timezone-aware datetime consistently for lexical UTC ordering."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a UTC-aware datetime."""
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with a different payload."""


@dataclass(frozen=True)
class RunLease:
    """Active worker execution lease and fencing token for a hosted run."""

    run_id: str
    lease_token: str
    lease_owner: str
    attempt: int
    expires_at: datetime


@dataclass(frozen=True)
class PersistedRun:
    """Snapshot of a persisted hosted run record."""

    run_id: str
    owner_service_id: str | None
    owner_key: str
    idempotency_key: str | None
    request_fingerprint: str
    status: str
    dispatch_state: str
    attempt: int
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    request_json: str
    workspace_dir: Path
    task_prompt: str
    agent_config_json: str
    request_origin: str | None
    user_agent: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_json: str | None
    report_json: str | None


def _row_to_run(row: sqlite3.Row) -> PersistedRun:
    """Convert a SQLite row into a :class:`PersistedRun`."""
    return PersistedRun(
        run_id=str(row["run_id"]),
        owner_service_id=row["owner_service_id"],
        owner_key=str(row["owner_key"]),
        idempotency_key=row["idempotency_key"],
        request_fingerprint=str(row["request_fingerprint"]),
        status=str(row["status"]),
        dispatch_state=str(row["dispatch_state"]),
        attempt=int(row["attempt"]),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=_parse_dt(row["lease_expires_at"]),
        request_json=str(row["request_json"]),
        workspace_dir=Path(str(row["workspace_dir"])),
        task_prompt=str(row["task_prompt"]),
        agent_config_json=str(row["agent_config_json"]),
        request_origin=row["request_origin"],
        user_agent=row["user_agent"],
        created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
        started_at=_parse_dt(row["started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
        error_json=row["error_json"],
        report_json=row["report_json"],
    )


class HostedRunStore:
    """SQLite persistence layer for hosted run state, leases, and recovery."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """Return the SQLite database file path."""
        return self._db_path

    def initialize(self) -> None:
        """Ensure parent directory, schema, and indexes exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_service_id TEXT,
                    owner_key TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dispatch_state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    request_json TEXT NOT NULL,
                    workspace_dir TEXT NOT NULL,
                    task_prompt TEXT NOT NULL,
                    agent_config_json TEXT NOT NULL,
                    request_origin TEXT,
                    user_agent TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error_json TEXT,
                    report_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_hosted_runs_idempotency
                ON hosted_runs (owner_key, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hosted_runs_owner_status
                ON hosted_runs (owner_service_id, status, created_at)
                """
            )
        finally:
            conn.close()

    def get(self, run_id: str) -> PersistedRun | None:
        """Fetch a single persisted run by ID, or ``None`` if absent."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM hosted_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return _row_to_run(row) if row is not None else None
        finally:
            conn.close()

    def get_by_idempotency_key(
        self,
        owner_key: str,
        idempotency_key: str,
    ) -> PersistedRun | None:
        """Look up an existing run for ``(owner_key, idempotency_key)``."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM hosted_runs
                WHERE owner_key = ? AND idempotency_key = ?
                """,
                (owner_key, idempotency_key),
            ).fetchone()
            return _row_to_run(row) if row is not None else None
        finally:
            conn.close()

    def list_all(self) -> list[PersistedRun]:
        """Return all persisted runs ordered by creation time."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM hosted_runs ORDER BY created_at ASC",
            ).fetchall()
            return [_row_to_run(row) for row in rows]
        finally:
            conn.close()

    def create_or_get(
        self,
        *,
        run_id: str,
        owner_service_id: str | None,
        owner_key: str,
        idempotency_key: str | None,
        request_fingerprint: str,
        request_json: str,
        workspace_dir: Path,
        task_prompt: str,
        agent_config_json: str,
        request_origin: str | None,
        user_agent: str | None,
        created_at: datetime,
    ) -> tuple[PersistedRun, bool]:
        """Atomically persist a new run or return an existing idempotent run."""
        created_str = _format_dt(created_at)
        with self._transaction() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM hosted_runs
                    WHERE owner_key = ? AND idempotency_key = ?
                    """,
                    (owner_key, idempotency_key),
                ).fetchone()
                if existing is not None:
                    persisted = _row_to_run(existing)
                    if persisted.request_fingerprint != request_fingerprint:
                        msg = "idempotency key already used with a different request payload"
                        raise IdempotencyConflictError(msg)
                    return persisted, False

            conn.execute(
                """
                INSERT INTO hosted_runs (
                    run_id,
                    owner_service_id,
                    owner_key,
                    idempotency_key,
                    request_fingerprint,
                    status,
                    dispatch_state,
                    attempt,
                    request_json,
                    workspace_dir,
                    task_prompt,
                    agent_config_json,
                    request_origin,
                    user_agent,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, 'accepted', 'pending', 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    owner_service_id,
                    owner_key,
                    idempotency_key,
                    request_fingerprint,
                    request_json,
                    str(workspace_dir),
                    task_prompt,
                    agent_config_json,
                    request_origin,
                    user_agent,
                    created_str,
                ),
            )
            inserted = conn.execute(
                "SELECT * FROM hosted_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if inserted is None:
                msg = f"failed to read back newly inserted run: {run_id}"
                raise RuntimeError(msg)
            return _row_to_run(inserted), True

    def mark_dispatched(self, run_id: str) -> None:
        """Record that a newly accepted run was enqueued for worker dispatch."""
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE hosted_runs
                SET status = CASE WHEN status = 'accepted' THEN 'queued' ELSE status END,
                    dispatch_state = 'dispatched'
                WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (run_id,),
            )

    def try_claim_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        now: datetime | None = None,
    ) -> RunLease | None:
        """Atomically claim execution ownership of a non-terminal run."""
        current = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        current_str = _format_dt(current)
        expires_at = current + timedelta(seconds=lease_ttl_seconds)
        expires_str = _format_dt(expires_at)

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM hosted_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None

            status = str(row["status"])
            if status in TERMINAL_RUN_STATUSES:
                return None

            existing_expiry = row["lease_expires_at"]
            if (
                status == "running"
                and existing_expiry is not None
                and existing_expiry > current_str
            ):
                return None

            attempt = int(row["attempt"]) + 1
            lease_token = uuid.uuid4().hex
            conn.execute(
                """
                UPDATE hosted_runs
                SET status = 'running',
                    dispatch_state = 'dispatched',
                    attempt = ?,
                    lease_owner = ?,
                    lease_token = ?,
                    lease_expires_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE run_id = ?
                """,
                (attempt, worker_id, lease_token, expires_str, current_str, run_id),
            )
            return RunLease(
                run_id=run_id,
                lease_token=lease_token,
                lease_owner=worker_id,
                attempt=attempt,
                expires_at=expires_at,
            )

    def renew_lease(
        self,
        run_id: str,
        *,
        lease_token: str,
        lease_ttl_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """Extend the lease expiration if the caller still owns the active token."""
        current = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        expires_str = _format_dt(current + timedelta(seconds=lease_ttl_seconds))
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE hosted_runs
                SET lease_expires_at = ?
                WHERE run_id = ? AND status = 'running' AND lease_token = ?
                """,
                (expires_str, run_id, lease_token),
            )
            return cursor.rowcount == 1

    def finalize_run(
        self,
        run_id: str,
        *,
        lease_token: str,
        status: Literal["completed", "failed", "cancelled"],
        completed_at: datetime,
        report_json: str | None = None,
        error_json: str | None = None,
    ) -> bool:
        """Atomically persist terminal state iff ``lease_token`` is still active."""
        completed_str = _format_dt(completed_at)
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE hosted_runs
                SET status = ?,
                    completed_at = ?,
                    report_json = COALESCE(?, report_json),
                    error_json = ?,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE run_id = ? AND lease_token = ? AND status = 'running'
                """,
                (status, completed_str, report_json, error_json, run_id, lease_token),
            )
            return cursor.rowcount == 1

    def cancel_run(
        self,
        run_id: str,
        *,
        completed_at: datetime,
        error_json: str | None = None,
    ) -> tuple[PersistedRun | None, bool]:
        """Atomically mark a non-terminal run cancelled and release any active lease."""
        completed_str = _format_dt(completed_at)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM hosted_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None, False

            existing = _row_to_run(row)
            if existing.status in TERMINAL_RUN_STATUSES:
                return existing, False

            conn.execute(
                """
                UPDATE hosted_runs
                SET status = 'cancelled',
                    completed_at = ?,
                    error_json = COALESCE(?, error_json),
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (completed_str, error_json, run_id),
            )
            updated = conn.execute(
                "SELECT * FROM hosted_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if updated is None:
                return None, False
            return _row_to_run(updated), True

    def list_recoverable(
        self,
        *,
        now: datetime | None = None,
        reclaim_all_running: bool = False,
    ) -> list[PersistedRun]:
        """Return runs needing dispatch or lease recovery."""
        current_str = _format_dt(now.astimezone(UTC) if now is not None else datetime.now(UTC))
        conn = self._connect()
        try:
            if reclaim_all_running:
                rows = conn.execute(
                    """
                    SELECT * FROM hosted_runs
                    WHERE status IN ('accepted', 'queued', 'running')
                    ORDER BY created_at ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM hosted_runs
                    WHERE (status = 'accepted' AND dispatch_state = 'pending')
                       OR status = 'queued'
                       OR (
                           status = 'running'
                           AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                       )
                    ORDER BY created_at ASC
                    """,
                    (current_str,),
                ).fetchall()
            return [_row_to_run(row) for row in rows]
        finally:
            conn.close()

    def requeue_for_recovery(self, run_id: str, *, expected_attempt: int) -> bool:
        """Reset an uncompleted or lease-expired run for queue re-dispatch."""
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE hosted_runs
                SET status = 'queued',
                    dispatch_state = 'dispatched',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE run_id = ?
                  AND attempt = ?
                  AND status IN ('accepted', 'queued', 'running')
                """,
                (run_id, expected_attempt),
            )
            return cursor.rowcount == 1

    def count_owner_runs(self, owner_service_id: str, *, since: datetime) -> tuple[int, int]:
        """Return ``(active_runs, daily_runs)`` for a client service identity."""
        since_str = _format_dt(since)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COALESCE(
                        SUM(
                            CASE WHEN status IN ('accepted', 'queued', 'running') THEN 1 ELSE 0 END
                        ),
                        0
                    ) AS active_count,
                    COALESCE(
                        SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END),
                        0
                    ) AS daily_count
                FROM hosted_runs
                WHERE owner_service_id = ?
                """,
                (since_str, owner_service_id),
            ).fetchone()
            if row is None:
                return 0, 0
            return int(row["active_count"]), int(row["daily_count"])
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
