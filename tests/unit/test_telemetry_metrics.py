from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telconet_sentinel.telemetry_config import load_telemetry_binding
from telconet_sentinel.telemetry_metrics import render_telemetry_metrics
from telconet_sentinel.telemetry_models import (
    BfdPeerTelemetry,
    InterfaceTelemetry,
    OspfNeighborTelemetry,
    RouterTelemetry,
    RouteTelemetry,
    TelemetrySnapshot,
    TelemetryState,
)

PROJECT_ROOT = Path(__file__).parents[2]


def _binding():
    return load_telemetry_binding(
        PROJECT_ROOT / "lab" / "telemetry-targets.yml",
        PROJECT_ROOT / "lab" / "intent.yml",
        PROJECT_ROOT / "lab" / "telconet.clab.yml",
    )


def _snapshot() -> TelemetrySnapshot:
    binding = _binding()
    routers: list[RouterTelemetry] = []
    for target in binding.targets:
        interfaces = tuple(
            InterfaceTelemetry(
                name=interface,
                state=(
                    TelemetryState.UNKNOWN
                    if target.node == "access2" and interface == "eth1"
                    else TelemetryState.UP
                ),
                rx_bytes=100,
                tx_bytes=200,
                rx_drops=3,
                tx_drops=4,
            )
            for interface in target.interfaces
        )
        routers.append(
            RouterTelemetry(
                name=target.node,
                collection_success=True,
                state=TelemetryState.UP,
                interfaces=interfaces,
                ospf_neighbors=tuple(
                    OspfNeighborTelemetry(
                        neighbor=peer.peer,
                        interface=peer.interface,
                        state=(
                            TelemetryState.UNKNOWN
                            if target.node == "access2"
                            else TelemetryState.UP
                        ),
                    )
                    for peer in target.protocol_peers
                ),
                bfd_peers=tuple(
                    BfdPeerTelemetry(
                        peer=peer.peer,
                        interface=peer.interface,
                        configured=target.node != "access2",
                        state=(
                            TelemetryState.DOWN
                            if target.node == "access1" and index == 0
                            else TelemetryState.UNKNOWN
                            if target.node == "access2"
                            else TelemetryState.UP
                        ),
                    )
                    for index, peer in enumerate(target.protocol_peers)
                ),
                routes=(
                    RouteTelemetry(
                        prefix=binding.config.service_prefix,
                        present=target.node != "core2",
                        metric=30 if target.node != "core2" else None,
                        egress_interface=(target.interfaces[0] if target.node != "core2" else None),
                    ),
                ),
            )
        )
    return TelemetrySnapshot(
        snapshot_id="snapshot-metrics",
        observed_at=datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc),
        topology_fingerprint=binding.fingerprint,
        routers=tuple(routers),
    )


def test_metrics_render_canonical_router_interface_protocol_and_route_series() -> None:
    binding = _binding()
    output = render_telemetry_metrics(_snapshot(), binding)

    assert "telconet_telemetry_snapshot_timestamp_seconds 1792904400" in output
    assert 'telconet_router_collection_success{node="access1"} 1' in output
    assert (
        'telconet_interface_oper_up{node="access1",interface="eth1",link_id="access1--agg1"} 1'
    ) in output
    assert (
        'telconet_interface_receive_bytes_total{node="access1",interface="eth1",'
        'link_id="access1--agg1"} 100'
    ) in output
    assert (
        'telconet_interface_transmit_drops_total{node="access1",interface="eth1",'
        'link_id="access1--agg1"} 4'
    ) in output
    assert (
        'telconet_ospf_neighbor_full{node="access2",neighbor="agg1",interface="eth1"} -1'
    ) in output
    assert ('telconet_bfd_peer_configured{node="access2",peer="agg1",interface="eth1"} 0') in output
    assert ('telconet_bfd_peer_up{node="access1",peer="agg1",interface="eth1"} 0') in output
    assert ('telconet_bfd_peer_up{node="access2",peer="agg1",interface="eth1"} -1') in output
    assert ('telconet_selected_route_present{node="core2",prefix="10.20.0.0/24"} 0') in output
    assert ('telconet_selected_route_metric{node="access1",prefix="10.20.0.0/24"} 30') in output
    assert (
        'telconet_selected_route_egress{node="access1",prefix="10.20.0.0/24",'
        'link_id="access1--agg1"} 1'
    ) in output


def test_metrics_render_topology_info_and_preserve_unknown_link_state() -> None:
    output = render_telemetry_metrics(_snapshot(), _binding())

    assert 'telconet_topology_node_info{node="access1",role="access"} 1' in output
    assert (
        'telconet_topology_link_oper_up{link_id="access1--agg1",source="access1",target="agg1"} 1'
    ) in output
    assert (
        'telconet_topology_link_oper_up{link_id="access1--agg2",source="access1",target="agg2"} 1'
    ) in output
    assert (
        'telconet_topology_link_oper_up{link_id="access2--agg1",source="access2",target="agg1"} -1'
    ) in output


def test_metrics_without_snapshot_expose_metadata_only() -> None:
    output = render_telemetry_metrics(None, _binding())

    assert "# HELP telconet_telemetry_snapshot_timestamp_seconds" in output
    assert "\ntelconet_telemetry_snapshot_timestamp_seconds " not in output
    assert 'telconet_topology_node_info{node="core1",role="core"} 1' in output


def test_metrics_reject_snapshot_from_a_different_topology() -> None:
    snapshot = _snapshot().model_copy(update={"topology_fingerprint": "b" * 64})

    with pytest.raises(ValueError, match="fingerprint"):
        render_telemetry_metrics(snapshot, _binding())


def test_metrics_render_remains_available_after_server_clock_rollback() -> None:
    future_snapshot = _snapshot().model_copy(
        update={"observed_at": datetime.now(timezone.utc) + timedelta(days=1)}
    )

    output = render_telemetry_metrics(future_snapshot, _binding())

    assert "telconet_telemetry_snapshot_timestamp_seconds" in output
