from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import uuid4


class ConvergenceEventKind(str, Enum):
    BLACKHOLE_INJECTED = "blackhole_injected"
    BFD_DOWN = "bfd_down"
    OSPF_NEIGHBOR_DOWN = "ospf_neighbor_down"
    ROUTE_FAILOVER = "route_failover"
    DATA_PLANE_RECOVERED = "data_plane_recovered"


REQUIRED_EVENTS_BY_PROFILE: dict[str, frozenset[ConvergenceEventKind]] = {
    "ospf_only": frozenset(
        {
            ConvergenceEventKind.BLACKHOLE_INJECTED,
            ConvergenceEventKind.OSPF_NEIGHBOR_DOWN,
            ConvergenceEventKind.ROUTE_FAILOVER,
            ConvergenceEventKind.DATA_PLANE_RECOVERED,
        }
    ),
    "bfd_100x3": frozenset(ConvergenceEventKind),
}


@dataclass(frozen=True, slots=True)
class ConvergenceEvent:
    kind: ConvergenceEventKind
    offset_ms: int
    observed_at: datetime
    route_metric: int | None = None
    icmp_sequence: int | None = None


@dataclass(slots=True)
class ConvergenceRun:
    id: str
    profile: str
    source: str
    target: str
    started_at: datetime
    events: list[ConvergenceEvent] = field(default_factory=list)

    @property
    def status(self) -> str:
        recorded = {event.kind for event in self.events}
        required = REQUIRED_EVENTS_BY_PROFILE[self.profile]
        return "complete" if recorded == required else "collecting"


class ConvergenceRepository(Protocol):
    def create_run(
        self,
        profile: str,
        source: str,
        target: str,
        started_at: datetime | None = None,
    ) -> ConvergenceRun: ...

    def append_event(self, run_id: str, event: ConvergenceEvent) -> ConvergenceRun: ...

    def get(self, run_id: str) -> ConvergenceRun: ...

    def latest(self) -> ConvergenceRun | None: ...


def _validate_run(
    profile: str,
    source: str,
    target: str,
    started_at: datetime,
) -> None:
    if profile not in REQUIRED_EVENTS_BY_PROFILE:
        raise ValueError(f"unsupported convergence profile: {profile}")
    if not source or not target:
        raise ValueError("convergence source and target must not be empty")
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("convergence started_at must be timezone-aware")


def _validate_event(run: ConvergenceRun, event: ConvergenceEvent) -> None:
    if event.offset_ms < 0:
        raise ValueError("convergence event offset must be non-negative")
    if event.observed_at.tzinfo is None or event.observed_at.utcoffset() is None:
        raise ValueError("convergence event observed_at must be timezone-aware")
    if event.kind is ConvergenceEventKind.ROUTE_FAILOVER and (
        event.route_metric is None or event.route_metric < 1
    ):
        raise ValueError("route_failover requires a positive route_metric")
    if event.kind is ConvergenceEventKind.DATA_PLANE_RECOVERED and (
        event.icmp_sequence is None or event.icmp_sequence < 0
    ):
        raise ValueError("data_plane_recovered requires a non-negative icmp_sequence")
    if event.kind not in REQUIRED_EVENTS_BY_PROFILE[run.profile]:
        raise ValueError(f"event {event.kind.value} is not valid for profile {run.profile}")
    if not run.events and event.kind is not ConvergenceEventKind.BLACKHOLE_INJECTED:
        raise ValueError("first event must be blackhole_injected")
    if any(recorded.kind is event.kind for recorded in run.events):
        raise ValueError(f"duplicate convergence event: {event.kind.value}")
    if run.events and event.offset_ms < run.events[-1].offset_ms:
        raise ValueError("convergence event offsets must be monotonic")


class ConvergenceStore:
    def __init__(self, capacity: int = 20) -> None:
        if capacity < 1:
            raise ValueError("convergence store capacity must be positive")
        self.capacity = capacity
        self._runs: dict[str, ConvergenceRun] = {}
        self._order: deque[str] = deque()
        self._lock = RLock()

    def create_run(
        self,
        profile: str,
        source: str,
        target: str,
        started_at: datetime | None = None,
    ) -> ConvergenceRun:
        timestamp = started_at or datetime.now(timezone.utc)
        _validate_run(profile, source, target, timestamp)
        run = ConvergenceRun(
            id=f"run-{uuid4().hex[:12]}",
            profile=profile,
            source=source,
            target=target,
            started_at=timestamp,
        )
        with self._lock:
            self._runs[run.id] = run
            self._order.append(run.id)
            while len(self._order) > self.capacity:
                expired = self._order.popleft()
                del self._runs[expired]
        return run

    def append_event(self, run_id: str, event: ConvergenceEvent) -> ConvergenceRun:
        with self._lock:
            run = self._lookup(run_id)
            _validate_event(run, event)
            run.events.append(event)
            return run

    def get(self, run_id: str) -> ConvergenceRun:
        with self._lock:
            return self._lookup(run_id)

    def latest(self) -> ConvergenceRun | None:
        with self._lock:
            return self._runs[self._order[-1]] if self._order else None

    def _lookup(self, run_id: str) -> ConvergenceRun:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"convergence run not found: {run_id}") from exc


class SQLiteConvergenceStore:
    def __init__(self, database_path: Path, capacity: int = 20) -> None:
        if capacity < 1:
            raise ValueError("convergence store capacity must be positive")
        self.database_path = database_path
        self.capacity = capacity
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS convergence_runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    profile TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS convergence_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    offset_ms INTEGER NOT NULL CHECK (offset_ms >= 0),
                    observed_at TEXT NOT NULL,
                    route_metric INTEGER,
                    icmp_sequence INTEGER,
                    UNIQUE (run_id, kind),
                    FOREIGN KEY (run_id) REFERENCES convergence_runs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_convergence_events_run
                    ON convergence_events(run_id, sequence);
                """
            )
            self._prune(connection)

    def create_run(
        self,
        profile: str,
        source: str,
        target: str,
        started_at: datetime | None = None,
    ) -> ConvergenceRun:
        timestamp = started_at or datetime.now(timezone.utc)
        _validate_run(profile, source, target, timestamp)
        run = ConvergenceRun(
            id=f"run-{uuid4().hex[:12]}",
            profile=profile,
            source=source,
            target=target,
            started_at=timestamp,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO convergence_runs(id, profile, source, target, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run.id, run.profile, run.source, run.target, run.started_at.isoformat()),
            )
            self._prune(connection)
        return run

    def append_event(self, run_id: str, event: ConvergenceEvent) -> ConvergenceRun:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._load(connection, run_id)
            _validate_event(run, event)
            connection.execute(
                """
                INSERT INTO convergence_events(
                    run_id, kind, offset_ms, observed_at, route_metric, icmp_sequence
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event.kind.value,
                    event.offset_ms,
                    event.observed_at.isoformat(),
                    event.route_metric,
                    event.icmp_sequence,
                ),
            )
            run.events.append(event)
        return run

    def get(self, run_id: str) -> ConvergenceRun:
        with self._connection() as connection:
            return self._load(connection, run_id)

    def latest(self) -> ConvergenceRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM convergence_runs ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._load(connection, str(row["id"]))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load(self, connection: sqlite3.Connection, run_id: str) -> ConvergenceRun:
        row = connection.execute(
            """
            SELECT id, profile, source, target, started_at
            FROM convergence_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"convergence run not found: {run_id}")
        events = connection.execute(
            """
            SELECT kind, offset_ms, observed_at, route_metric, icmp_sequence
            FROM convergence_events
            WHERE run_id = ?
            ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        return ConvergenceRun(
            id=str(row["id"]),
            profile=str(row["profile"]),
            source=str(row["source"]),
            target=str(row["target"]),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            events=[
                ConvergenceEvent(
                    kind=ConvergenceEventKind(str(event["kind"])),
                    offset_ms=int(event["offset_ms"]),
                    observed_at=datetime.fromisoformat(str(event["observed_at"])),
                    route_metric=(
                        None
                        if event["route_metric"] is None
                        else int(event["route_metric"])
                    ),
                    icmp_sequence=(
                        None
                        if event["icmp_sequence"] is None
                        else int(event["icmp_sequence"])
                    ),
                )
                for event in events
            ],
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM convergence_runs
            WHERE sequence NOT IN (
                SELECT sequence
                FROM convergence_runs
                ORDER BY sequence DESC
                LIMIT ?
            )
            """,
            (self.capacity,),
        )


def render_live_metrics(run: ConvergenceRun | None) -> str:
    lines = [
        "# HELP telconet_live_run_complete Whether the latest live run recorded all stages.",
        "# TYPE telconet_live_run_complete gauge",
        "# HELP telconet_live_event_offset_seconds Observed offset from fault injection.",
        "# TYPE telconet_live_event_offset_seconds gauge",
        "# HELP telconet_live_bfd_peer_up Whether the observed BFD peer is up.",
        "# TYPE telconet_live_bfd_peer_up gauge",
        "# HELP telconet_live_ospf_neighbor_full Whether the observed OSPF neighbor is Full.",
        "# TYPE telconet_live_ospf_neighbor_full gauge",
        "# HELP telconet_live_route_metric Current observed service route metric.",
        "# TYPE telconet_live_route_metric gauge",
        "# HELP telconet_live_data_plane_reachable Whether the live ICMP probe is reachable.",
        "# TYPE telconet_live_data_plane_reachable gauge",
    ]
    if run is None:
        return "\n".join(lines) + "\n"

    label = f'profile="{run.profile}"'
    events = {event.kind: event for event in run.events}
    complete = 1 if run.status == "complete" else 0
    lines.append(f"telconet_live_run_complete{{{label}}} {complete}")
    for event in run.events:
        seconds = event.offset_ms / 1000
        lines.append(
            "telconet_live_event_offset_seconds"
            f'{{event="{event.kind.value}",{label}}} {seconds:g}'
        )

    bfd_up = 0 if ConvergenceEventKind.BFD_DOWN in events else 1
    ospf_full = 0 if ConvergenceEventKind.OSPF_NEIGHBOR_DOWN in events else 1
    route_event = events.get(ConvergenceEventKind.ROUTE_FAILOVER)
    route_metric = route_event.route_metric if route_event is not None else 30
    reachable = (
        1
        if ConvergenceEventKind.DATA_PLANE_RECOVERED in events
        else 0
        if ConvergenceEventKind.BLACKHOLE_INJECTED in events
        else 1
    )
    if run.profile == "bfd_100x3":
        lines.append(f"telconet_live_bfd_peer_up{{{label}}} {bfd_up}")
    lines.extend(
        [
            f"telconet_live_ospf_neighbor_full{{{label}}} {ospf_full}",
            f"telconet_live_route_metric{{{label}}} {route_metric}",
            f"telconet_live_data_plane_reachable{{{label}}} {reachable}",
        ]
    )
    return "\n".join(lines) + "\n"
