from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from telconet_sentinel.telemetry_models import (
    BASELINE_ROUTERS,
    BfdPeerTelemetry,
    InterfaceTelemetry,
    OspfNeighborTelemetry,
    RouterTelemetry,
    RouteTelemetry,
    TelemetrySnapshot,
    TelemetryState,
)


def _router(name: str, *, state: TelemetryState = TelemetryState.UP) -> RouterTelemetry:
    return RouterTelemetry(
        name=name,
        collection_success=True,
        state=state,
        interfaces=(
            InterfaceTelemetry(
                name="eth1",
                state=state,
                rx_bytes=10,
                tx_bytes=20,
                rx_drops=1,
                tx_drops=2,
            ),
        ),
        ospf_neighbors=(),
        bfd_peers=(),
        routes=(),
    )


def _snapshot() -> TelemetrySnapshot:
    return TelemetrySnapshot(
        snapshot_id="snapshot-001",
        observed_at=datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc),
        topology_fingerprint="a" * 64,
        routers=tuple(_router(name) for name in BASELINE_ROUTERS),
    )


def test_snapshot_requires_exactly_the_six_unique_baseline_routers() -> None:
    snapshot = _snapshot()

    assert tuple(router.name for router in snapshot.routers) == BASELINE_ROUTERS

    with pytest.raises(ValidationError, match="baseline routers"):
        TelemetrySnapshot(
            snapshot_id="snapshot-missing",
            observed_at=snapshot.observed_at,
            topology_fingerprint=snapshot.topology_fingerprint,
            routers=snapshot.routers[:-1],
        )

    with pytest.raises(ValidationError, match="unique"):
        TelemetrySnapshot(
            snapshot_id="snapshot-duplicate",
            observed_at=snapshot.observed_at,
            topology_fingerprint=snapshot.topology_fingerprint,
            routers=(*snapshot.routers[:-1], snapshot.routers[0]),
        )


def test_snapshot_rejects_naive_time_extra_fields_and_unbounded_values() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError, match="timezone-aware"):
        TelemetrySnapshot(
            snapshot_id="snapshot-naive",
            observed_at=datetime(2026, 10, 25, 5, 0),
            topology_fingerprint=snapshot.topology_fingerprint,
            routers=snapshot.routers,
        )

    with pytest.raises(ValidationError):
        InterfaceTelemetry(
            name="eth1",
            state=TelemetryState.UP,
            rx_bytes=2**64,
            tx_bytes=0,
            rx_drops=0,
            tx_drops=0,
        )

    with pytest.raises(ValidationError):
        InterfaceTelemetry(
            name="eth1",
            state=TelemetryState.UP,
            rx_bytes=0,
            tx_bytes=0,
            rx_drops=0,
            tx_drops=0,
            injected="not-allowed",
        )


def test_router_observations_are_unique_and_reference_known_interfaces() -> None:
    interface = InterfaceTelemetry(
        name="eth1",
        state=TelemetryState.UP,
        rx_bytes=1,
        tx_bytes=2,
        rx_drops=0,
        tx_drops=0,
    )
    neighbor = OspfNeighborTelemetry(
        neighbor="10.255.0.2",
        interface="eth1",
        state=TelemetryState.UP,
    )
    peer = BfdPeerTelemetry(
        peer="10.0.0.1",
        interface="eth1",
        configured=True,
        state=TelemetryState.DOWN,
    )
    route = RouteTelemetry(
        prefix="10.20.0.0/24",
        present=True,
        metric=30,
        egress_interface="eth1",
    )

    router = RouterTelemetry(
        name="access1",
        collection_success=True,
        state=TelemetryState.UP,
        interfaces=(interface,),
        ospf_neighbors=(neighbor,),
        bfd_peers=(peer,),
        routes=(route,),
    )
    assert router.routes[0].metric == 30

    with pytest.raises(ValidationError, match="interfaces must be unique"):
        RouterTelemetry(
            name="access1",
            collection_success=True,
            state=TelemetryState.UP,
            interfaces=(interface, interface),
        )

    with pytest.raises(ValidationError, match="unknown interface"):
        RouterTelemetry(
            name="access1",
            collection_success=True,
            state=TelemetryState.UP,
            interfaces=(interface,),
            ospf_neighbors=(
                OspfNeighborTelemetry(
                    neighbor="10.255.0.2",
                    interface="eth9",
                    state=TelemetryState.UP,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="routes must be unique"):
        RouterTelemetry(
            name="access1",
            collection_success=True,
            state=TelemetryState.UP,
            interfaces=(interface,),
            routes=(route, route),
        )


def test_bfd_absence_and_unknown_are_not_encoded_as_down() -> None:
    absent = BfdPeerTelemetry(
        peer="10.0.0.1",
        interface="eth1",
        configured=False,
        state=TelemetryState.UNKNOWN,
    )
    configured_down = BfdPeerTelemetry(
        peer="10.0.0.1",
        interface="eth1",
        configured=True,
        state=TelemetryState.DOWN,
    )

    assert absent.state is TelemetryState.UNKNOWN
    assert configured_down.state is TelemetryState.DOWN
    with pytest.raises(ValidationError, match="unconfigured BFD peer"):
        BfdPeerTelemetry(
            peer="10.0.0.1",
            interface="eth1",
            configured=False,
            state=TelemetryState.DOWN,
        )


def test_failed_collection_is_explicit_and_cannot_carry_partial_observations() -> None:
    failed = RouterTelemetry(
        name="core1",
        collection_success=False,
        state=TelemetryState.UNKNOWN,
        collection_error="container_unavailable",
    )
    assert failed.collection_error == "container_unavailable"

    with pytest.raises(ValidationError, match="failed collection"):
        RouterTelemetry(
            name="core1",
            collection_success=False,
            state=TelemetryState.DOWN,
            collection_error="container_unavailable",
        )

    with pytest.raises(ValidationError, match="partial observations"):
        RouterTelemetry(
            name="core1",
            collection_success=False,
            state=TelemetryState.UNKNOWN,
            collection_error="container_unavailable",
            interfaces=(
                InterfaceTelemetry(
                    name="eth1",
                    state=TelemetryState.UNKNOWN,
                    rx_bytes=0,
                    tx_bytes=0,
                    rx_drops=0,
                    tx_drops=0,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="successful collection"):
        RouterTelemetry(
            name="core1",
            collection_success=True,
            state=TelemetryState.UP,
            collection_error="stale error",
        )

    with pytest.raises(ValidationError, match="successful collection.*up state"):
        RouterTelemetry(
            name="core1",
            collection_success=True,
            state=TelemetryState.DOWN,
        )


def test_route_presence_has_consistent_metric_and_egress_semantics() -> None:
    absent = RouteTelemetry(prefix="10.20.0.0/24", present=False)
    assert absent.metric is None

    with pytest.raises(ValidationError, match="present route"):
        RouteTelemetry(prefix="10.20.0.0/24", present=True)
    with pytest.raises(ValidationError, match="absent route"):
        RouteTelemetry(
            prefix="10.20.0.0/24",
            present=False,
            metric=30,
            egress_interface="eth1",
        )
    with pytest.raises(ValidationError, match="canonical"):
        RouteTelemetry(prefix="10.20.0.1/24", present=False)
