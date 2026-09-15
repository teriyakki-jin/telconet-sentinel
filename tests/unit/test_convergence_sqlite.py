from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telconet_sentinel.convergence import (
    ConvergenceEvent,
    ConvergenceEventKind,
    SQLiteConvergenceStore,
)

START = datetime(2026, 9, 15, 1, 2, 3, tzinfo=timezone.utc)


def _event(
    kind: ConvergenceEventKind,
    offset_ms: int,
    *,
    route_metric: int | None = None,
    icmp_sequence: int | None = None,
) -> ConvergenceEvent:
    return ConvergenceEvent(
        kind=kind,
        offset_ms=offset_ms,
        observed_at=START + timedelta(milliseconds=offset_ms),
        route_metric=route_metric,
        icmp_sequence=icmp_sequence,
    )


def _complete_run(store: SQLiteConvergenceStore) -> str:
    run = store.create_run("bfd_100x3", "access1", "10.20.0.10", START)
    for event in (
        _event(ConvergenceEventKind.BLACKHOLE_INJECTED, 0),
        _event(ConvergenceEventKind.BFD_DOWN, 312),
        _event(ConvergenceEventKind.OSPF_NEIGHBOR_DOWN, 338),
        _event(ConvergenceEventKind.ROUTE_FAILOVER, 421, route_metric=140),
        _event(
            ConvergenceEventKind.DATA_PLANE_RECOVERED,
            498,
            icmp_sequence=8,
        ),
    ):
        store.append_event(run.id, event)
    return run.id


def test_persists_a_complete_run_across_store_instances(tmp_path: Path) -> None:
    database = tmp_path / "state" / "convergence.sqlite3"
    run_id = _complete_run(SQLiteConvergenceStore(database))

    restored = SQLiteConvergenceStore(database).get(run_id)

    assert restored.status == "complete"
    assert restored.started_at == START
    assert [event.kind for event in restored.events] == list(ConvergenceEventKind)
    assert restored.events[-2].route_metric == 140
    assert restored.events[-1].icmp_sequence == 8


def test_persistent_capacity_prunes_old_runs_and_events(tmp_path: Path) -> None:
    database = tmp_path / "convergence.sqlite3"
    store = SQLiteConvergenceStore(database, capacity=2)
    first = store.create_run("ospf_only", "access1", "10.20.0.10", START)
    store.append_event(first.id, _event(ConvergenceEventKind.BLACKHOLE_INJECTED, 0))
    second = store.create_run("bfd_100x3", "access1", "10.20.0.10", START)
    third = store.create_run("bfd_100x3", "access2", "10.20.0.10", START)

    reopened = SQLiteConvergenceStore(database, capacity=2)

    with pytest.raises(KeyError, match="run not found"):
        reopened.get(first.id)
    assert reopened.get(second.id).id == second.id
    assert reopened.latest() is not None
    assert reopened.latest().id == third.id


def test_rejected_event_does_not_modify_persistent_run(tmp_path: Path) -> None:
    database = tmp_path / "convergence.sqlite3"
    store = SQLiteConvergenceStore(database)
    run = store.create_run("bfd_100x3", "access1", "10.20.0.10", START)
    store.append_event(run.id, _event(ConvergenceEventKind.BLACKHOLE_INJECTED, 0))

    with pytest.raises(ValueError, match="duplicate"):
        store.append_event(
            run.id,
            _event(ConvergenceEventKind.BLACKHOLE_INJECTED, 1),
        )

    restored = SQLiteConvergenceStore(database).get(run.id)
    assert len(restored.events) == 1


def test_rejects_invalid_persistent_store_capacity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capacity"):
        SQLiteConvergenceStore(tmp_path / "convergence.sqlite3", capacity=0)
