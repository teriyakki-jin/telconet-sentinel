from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Protocol

from .telemetry_models import TelemetrySnapshot


class TelemetryRepository(Protocol):
    def save(self, snapshot: TelemetrySnapshot) -> bool: ...

    def latest(self, topology_fingerprint: str | None = None) -> TelemetrySnapshot | None: ...

    def recent(self, topology_fingerprint: str | None = None) -> tuple[TelemetrySnapshot, ...]: ...


def _sort_key(snapshot: TelemetrySnapshot) -> tuple[object, str]:
    return snapshot.observed_at, snapshot.snapshot_id


class TelemetryStore:
    def __init__(self, capacity: int = 120) -> None:
        if capacity < 1:
            raise ValueError("telemetry store capacity must be positive")
        self.capacity = capacity
        self._snapshots: dict[str, TelemetrySnapshot] = {}
        self._lock = RLock()

    def save(self, snapshot: TelemetrySnapshot) -> bool:
        with self._lock:
            existing = self._snapshots.get(snapshot.snapshot_id)
            if existing is not None:
                if existing != snapshot:
                    raise ValueError(
                        "telemetry snapshot id already exists with a different payload"
                    )
                return False
            if len(self._snapshots) >= self.capacity:
                oldest_retained = min(self._snapshots.values(), key=_sort_key)
                if _sort_key(snapshot) <= _sort_key(oldest_retained):
                    return False
            self._snapshots[snapshot.snapshot_id] = snapshot
            retained = sorted(self._snapshots.values(), key=_sort_key, reverse=True)[
                : self.capacity
            ]
            self._snapshots = {item.snapshot_id: item for item in retained}
            return True

    def latest(self, topology_fingerprint: str | None = None) -> TelemetrySnapshot | None:
        with self._lock:
            candidates = [
                snapshot
                for snapshot in self._snapshots.values()
                if topology_fingerprint is None
                or snapshot.topology_fingerprint == topology_fingerprint
            ]
            if not candidates:
                return None
            return max(candidates, key=_sort_key)

    def recent(self, topology_fingerprint: str | None = None) -> tuple[TelemetrySnapshot, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        snapshot
                        for snapshot in self._snapshots.values()
                        if topology_fingerprint is None
                        or snapshot.topology_fingerprint == topology_fingerprint
                    ),
                    key=_sort_key,
                    reverse=True,
                )
            )


class SQLiteTelemetryStore:
    def __init__(self, database_path: Path, capacity: int = 120) -> None:
        if capacity < 1:
            raise ValueError("telemetry store capacity must be positive")
        self.database_path = database_path
        self.capacity = capacity
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_snapshots_observed
                    ON telemetry_snapshots(observed_at DESC, snapshot_id DESC);
                """
            )
            self._prune(connection)

    def save(self, snapshot: TelemetrySnapshot) -> bool:
        payload = snapshot.model_dump_json()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM telemetry_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if row is not None:
                existing = TelemetrySnapshot.model_validate_json(str(row["payload"]))
                if existing != snapshot:
                    raise ValueError(
                        "telemetry snapshot id already exists with a different payload"
                    )
                return False
            retained_count = int(
                connection.execute("SELECT COUNT(*) FROM telemetry_snapshots").fetchone()[0]
            )
            if retained_count >= self.capacity:
                oldest = connection.execute(
                    """
                    SELECT observed_at, snapshot_id
                    FROM telemetry_snapshots
                    ORDER BY observed_at ASC, snapshot_id ASC
                    LIMIT 1
                    """
                ).fetchone()
                assert oldest is not None
                candidate_key = (
                    snapshot.observed_at.isoformat(),
                    snapshot.snapshot_id,
                )
                retained_key = (str(oldest["observed_at"]), str(oldest["snapshot_id"]))
                if candidate_key <= retained_key:
                    return False
            connection.execute(
                """
                INSERT INTO telemetry_snapshots(snapshot_id, observed_at, payload)
                VALUES (?, ?, ?)
                """,
                (snapshot.snapshot_id, snapshot.observed_at.isoformat(), payload),
            )
            self._prune(connection)
            return True

    def latest(self, topology_fingerprint: str | None = None) -> TelemetrySnapshot | None:
        snapshots = self._load_recent(limit=1, topology_fingerprint=topology_fingerprint)
        return snapshots[0] if snapshots else None

    def recent(self, topology_fingerprint: str | None = None) -> tuple[TelemetrySnapshot, ...]:
        return self._load_recent(limit=self.capacity, topology_fingerprint=topology_fingerprint)

    def _load_recent(
        self,
        limit: int,
        topology_fingerprint: str | None,
    ) -> tuple[TelemetrySnapshot, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM telemetry_snapshots
                ORDER BY observed_at DESC, snapshot_id DESC
                """,
            ).fetchall()
        snapshots = (TelemetrySnapshot.model_validate_json(str(row["payload"])) for row in rows)
        return tuple(
            snapshot
            for snapshot in snapshots
            if topology_fingerprint is None or snapshot.topology_fingerprint == topology_fingerprint
        )[:limit]

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM telemetry_snapshots
            WHERE snapshot_id NOT IN (
                SELECT snapshot_id
                FROM telemetry_snapshots
                ORDER BY observed_at DESC, snapshot_id DESC
                LIMIT ?
            )
            """,
            (self.capacity,),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
