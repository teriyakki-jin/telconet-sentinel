from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telconet_sentinel.telemetry_models import (
    BASELINE_ROUTERS,
    RouterTelemetry,
    TelemetrySnapshot,
    TelemetryState,
)
from telconet_sentinel.telemetry_store import SQLiteTelemetryStore, TelemetryStore


def _snapshot(
    snapshot_id: str,
    observed_at: datetime,
    *,
    fingerprint: str = "a" * 64,
) -> TelemetrySnapshot:
    return TelemetrySnapshot(
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        topology_fingerprint=fingerprint,
        routers=tuple(
            RouterTelemetry(
                name=name,
                collection_success=True,
                state=TelemetryState.UP,
            )
            for name in BASELINE_ROUTERS
        ),
    )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_store_is_idempotent_and_rejects_conflicting_snapshot_ids(
    store_kind: str,
    tmp_path: Path,
) -> None:
    store = (
        TelemetryStore(capacity=3)
        if store_kind == "memory"
        else SQLiteTelemetryStore(tmp_path / "telemetry.sqlite3", capacity=3)
    )
    timestamp = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    snapshot = _snapshot("snapshot-001", timestamp)

    assert store.save(snapshot) is True
    assert store.save(snapshot) is False
    assert store.latest() == snapshot

    conflict = snapshot.model_copy(update={"observed_at": timestamp + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="different payload"):
        store.save(conflict)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_older_delayed_snapshot_never_replaces_latest_and_capacity_keeps_newest(
    store_kind: str,
    tmp_path: Path,
) -> None:
    store = (
        TelemetryStore(capacity=2)
        if store_kind == "memory"
        else SQLiteTelemetryStore(tmp_path / "telemetry.sqlite3", capacity=2)
    )
    now = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    newest = _snapshot("snapshot-newest", now)
    middle = _snapshot("snapshot-middle", now - timedelta(seconds=1))
    stale = _snapshot("snapshot-stale", now - timedelta(minutes=1))

    assert store.save(middle) is True
    assert store.save(newest) is True
    assert store.save(stale) is False
    assert store.save(stale) is False

    assert store.latest() == newest
    assert [item.snapshot_id for item in store.recent()] == [
        "snapshot-newest",
        "snapshot-middle",
    ]


def test_sqlite_store_persists_latest_across_instances(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "telemetry.sqlite3"
    timestamp = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    snapshot = _snapshot("snapshot-persisted", timestamp)
    SQLiteTelemetryStore(database_path, capacity=2).save(snapshot)

    reopened = SQLiteTelemetryStore(database_path, capacity=2)

    assert reopened.latest() == snapshot


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_latest_and_recent_can_be_scoped_to_topology_fingerprint(
    store_kind: str,
    tmp_path: Path,
) -> None:
    store = (
        TelemetryStore(capacity=4)
        if store_kind == "memory"
        else SQLiteTelemetryStore(tmp_path / "telemetry.sqlite3", capacity=4)
    )
    now = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    old_topology = _snapshot("snapshot-old-topology", now, fingerprint="a" * 64)
    current_topology = _snapshot(
        "snapshot-current-topology",
        now - timedelta(seconds=1),
        fingerprint="b" * 64,
    )
    store.save(old_topology)
    store.save(current_topology)

    assert store.latest("b" * 64) == current_topology
    assert store.recent("b" * 64) == (current_topology,)
    assert store.latest("c" * 64) is None


def test_store_capacity_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capacity"):
        TelemetryStore(capacity=0)
    with pytest.raises(ValueError, match="capacity"):
        SQLiteTelemetryStore(tmp_path / "telemetry.sqlite3", capacity=0)
