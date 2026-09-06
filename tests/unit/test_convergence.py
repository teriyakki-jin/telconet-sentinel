from datetime import datetime, timedelta, timezone

import pytest

from telconet_sentinel.convergence import (
    ConvergenceEvent,
    ConvergenceEventKind,
    ConvergenceStore,
    render_live_metrics,
)

START = datetime(2026, 9, 6, 1, 2, 3, tzinfo=timezone.utc)


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


def _complete_run(store: ConvergenceStore) -> str:
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


def test_records_a_complete_live_convergence_timeline() -> None:
    store = ConvergenceStore()

    run_id = _complete_run(store)
    run = store.get(run_id)

    assert run.profile == "bfd_100x3"
    assert run.status == "complete"
    assert [event.kind.value for event in run.events] == [
        "blackhole_injected",
        "bfd_down",
        "ospf_neighbor_down",
        "route_failover",
        "data_plane_recovered",
    ]
    assert run.events[-1].icmp_sequence == 8


def test_ospf_only_run_completes_without_a_fabricated_bfd_event() -> None:
    store = ConvergenceStore()
    run = store.create_run("ospf_only", "access1", "10.20.0.10", START)
    for event in (
        _event(ConvergenceEventKind.BLACKHOLE_INJECTED, 0),
        _event(ConvergenceEventKind.OSPF_NEIGHBOR_DOWN, 4_000),
        _event(ConvergenceEventKind.ROUTE_FAILOVER, 4_100, route_metric=140),
        _event(
            ConvergenceEventKind.DATA_PLANE_RECOVERED,
            4_200,
            icmp_sequence=45,
        ),
    ):
        store.append_event(run.id, event)

    assert run.status == "complete"
    assert 'telconet_live_bfd_peer_up{profile="ospf_only"}' not in render_live_metrics(run)
    with pytest.raises(ValueError, match="not valid for profile"):
        store.append_event(run.id, _event(ConvergenceEventKind.BFD_DOWN, 4_300))


def test_rejects_invalid_or_ambiguous_event_sequences() -> None:
    store = ConvergenceStore()
    run = store.create_run("bfd_100x3", "access1", "10.20.0.10", START)

    with pytest.raises(ValueError, match="first event"):
        store.append_event(run.id, _event(ConvergenceEventKind.BFD_DOWN, 300))

    store.append_event(run.id, _event(ConvergenceEventKind.BLACKHOLE_INJECTED, 0))
    store.append_event(run.id, _event(ConvergenceEventKind.BFD_DOWN, 300))

    with pytest.raises(ValueError, match="duplicate"):
        store.append_event(run.id, _event(ConvergenceEventKind.BFD_DOWN, 310))
    with pytest.raises(ValueError, match="monotonic"):
        store.append_event(run.id, _event(ConvergenceEventKind.OSPF_NEIGHBOR_DOWN, 299))
    with pytest.raises(ValueError, match="route_metric"):
        store.append_event(run.id, _event(ConvergenceEventKind.ROUTE_FAILOVER, 400))
    with pytest.raises(ValueError, match="icmp_sequence"):
        store.append_event(run.id, _event(ConvergenceEventKind.DATA_PLANE_RECOVERED, 500))


def test_rejects_invalid_run_metadata_and_unknown_run() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ConvergenceStore(capacity=0)

    store = ConvergenceStore()
    with pytest.raises(ValueError, match="profile"):
        store.create_run("unknown", "access1", "10.20.0.10", START)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.create_run(
            "bfd_100x3",
            "access1",
            "10.20.0.10",
            datetime(2026, 9, 6),
        )
    with pytest.raises(KeyError, match="run not found"):
        store.get("missing")


def test_bounds_memory_and_returns_the_latest_run() -> None:
    store = ConvergenceStore(capacity=2)
    first = store.create_run("ospf_only", "access1", "10.20.0.10", START)
    second = store.create_run("bfd_100x3", "access1", "10.20.0.10", START)
    third = store.create_run("bfd_100x3", "access2", "10.20.0.10", START)

    with pytest.raises(KeyError):
        store.get(first.id)
    assert store.get(second.id) is second
    assert store.latest() is third


def test_renders_latest_live_state_and_stage_offsets_as_metrics() -> None:
    store = ConvergenceStore()
    run_id = _complete_run(store)

    metrics = render_live_metrics(store.get(run_id))

    assert 'telconet_live_run_complete{profile="bfd_100x3"} 1' in metrics
    assert (
        'telconet_live_event_offset_seconds{event="bfd_down",profile="bfd_100x3"} 0.312'
        in metrics
    )
    assert (
        'telconet_live_event_offset_seconds{event="data_plane_recovered",profile="bfd_100x3"} 0.498'
        in metrics
    )
    assert 'telconet_live_bfd_peer_up{profile="bfd_100x3"} 0' in metrics
    assert 'telconet_live_ospf_neighbor_full{profile="bfd_100x3"} 0' in metrics
    assert 'telconet_live_route_metric{profile="bfd_100x3"} 140' in metrics
    assert 'telconet_live_data_plane_reachable{profile="bfd_100x3"} 1' in metrics


def test_renders_metric_metadata_without_samples_before_a_run() -> None:
    metrics = render_live_metrics(None)

    assert "# HELP telconet_live_event_offset_seconds" in metrics
    assert "telconet_live_event_offset_seconds{" not in metrics
