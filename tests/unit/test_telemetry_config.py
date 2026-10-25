from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from telconet_sentinel.telemetry_config import (
    load_telemetry_binding,
    validate_telemetry_ingest,
    validate_telemetry_snapshot,
)
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


def test_load_binding_cross_validates_and_maps_intent_links_to_clab_interfaces() -> None:
    binding = load_telemetry_binding(
        PROJECT_ROOT / "lab" / "telemetry-targets.yml",
        PROJECT_ROOT / "lab" / "intent.yml",
        PROJECT_ROOT / "lab" / "telconet.clab.yml",
    )

    assert len(binding.targets) == 6
    access1 = next(target for target in binding.targets if target.node == "access1")
    assert access1.container == "clab-telconet-sentinel-access1"
    assert access1.role == "access"
    assert access1.interfaces == ("eth1", "eth2", "eth3")
    assert tuple((peer.peer, peer.interface) for peer in access1.protocol_peers) == (
        ("agg1", "eth1"),
        ("agg2", "eth2"),
    )
    link = next(link for link in binding.links if link.id == "access1--agg1")
    assert (
        link.source,
        link.source_interface,
        link.target,
        link.target_interface,
    ) == ("access1", "eth1", "agg1", "eth1")
    assert len(binding.fingerprint) == 64


def test_binding_fingerprint_is_deterministic_and_normalizes_line_endings(
    tmp_path: Path,
) -> None:
    source_paths = (
        PROJECT_ROOT / "lab" / "telemetry-targets.yml",
        PROJECT_ROOT / "lab" / "intent.yml",
        PROJECT_ROOT / "lab" / "telconet.clab.yml",
    )
    lf_paths: list[Path] = []
    crlf_paths: list[Path] = []
    for source in source_paths:
        text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        lf_path = tmp_path / f"lf-{source.name}"
        crlf_path = tmp_path / f"crlf-{source.name}"
        lf_path.write_text(text, encoding="utf-8", newline="\n")
        crlf_path.write_text(text, encoding="utf-8", newline="\r\n")
        lf_paths.append(lf_path)
        crlf_paths.append(crlf_path)

    lf_binding = load_telemetry_binding(*lf_paths)
    crlf_binding = load_telemetry_binding(*crlf_paths)

    assert lf_binding.fingerprint == crlf_binding.fingerprint


def test_binding_rejects_missing_router_target(tmp_path: Path) -> None:
    config = (PROJECT_ROOT / "lab" / "telemetry-targets.yml").read_text(encoding="utf-8")
    start = config.index("  - node: core2")
    bad_config = config[:start]
    path = tmp_path / "targets.yml"
    path.write_text(bad_config, encoding="utf-8")

    with pytest.raises((ValueError, ValidationError), match="baseline routers"):
        load_telemetry_binding(
            path,
            PROJECT_ROOT / "lab" / "intent.yml",
            PROJECT_ROOT / "lab" / "telconet.clab.yml",
        )


def test_binding_rejects_intent_link_missing_from_clab(tmp_path: Path) -> None:
    clab = (PROJECT_ROOT / "lab" / "telconet.clab.yml").read_text(encoding="utf-8")
    clab = clab.replace(
        "    - endpoints: [access1:eth1, agg1:eth1]\n",
        "",
    )
    clab_path = tmp_path / "telconet.clab.yml"
    clab_path.write_text(clab, encoding="utf-8")

    with pytest.raises(ValueError, match="missing from containerlab"):
        load_telemetry_binding(
            PROJECT_ROOT / "lab" / "telemetry-targets.yml",
            PROJECT_ROOT / "lab" / "intent.yml",
            clab_path,
        )


def test_binding_rejects_managed_clab_link_not_declared_in_intent(tmp_path: Path) -> None:
    clab = (PROJECT_ROOT / "lab" / "telconet.clab.yml").read_text(encoding="utf-8")
    clab += "    - endpoints: [access1:eth9, core1:eth9]\n"
    clab_path = tmp_path / "telconet.clab.yml"
    clab_path.write_text(clab, encoding="utf-8")

    with pytest.raises(ValueError, match="not declared in intent"):
        load_telemetry_binding(
            PROJECT_ROOT / "lab" / "telemetry-targets.yml",
            PROJECT_ROOT / "lab" / "intent.yml",
            clab_path,
        )


def test_binding_rejects_target_container_with_wrong_lab_prefix(tmp_path: Path) -> None:
    config = (PROJECT_ROOT / "lab" / "telemetry-targets.yml").read_text(encoding="utf-8")
    config = config.replace(
        "clab-telconet-sentinel-access1",
        "clab-another-lab-access1",
        1,
    )
    path = tmp_path / "targets.yml"
    path.write_text(config, encoding="utf-8")

    with pytest.raises(ValueError, match="container name"):
        load_telemetry_binding(
            path,
            PROJECT_ROOT / "lab" / "intent.yml",
            PROJECT_ROOT / "lab" / "telconet.clab.yml",
        )


def _validated_snapshot(
    *,
    omit_interface: bool = False,
    omit_route: bool = False,
    omit_protocol_peer: bool = False,
    unknown_protocol_peer: bool = False,
    unknown_bfd_peer: bool = False,
):
    binding = load_telemetry_binding(
        PROJECT_ROOT / "lab" / "telemetry-targets.yml",
        PROJECT_ROOT / "lab" / "intent.yml",
        PROJECT_ROOT / "lab" / "telconet.clab.yml",
    )
    routers: list[RouterTelemetry] = []
    for target in binding.targets:
        interface_names = target.interfaces
        if omit_interface and target.node == "access1":
            interface_names = target.interfaces[:-1]
        routes = (
            ()
            if omit_route and target.node == "access1"
            else (RouteTelemetry(prefix=binding.config.service_prefix, present=False),)
        )
        routers.append(
            RouterTelemetry(
                name=target.node,
                collection_success=True,
                state=TelemetryState.UP,
                interfaces=tuple(
                    InterfaceTelemetry(
                        name=name,
                        state=TelemetryState.UP,
                        rx_bytes=0,
                        tx_bytes=0,
                        rx_drops=0,
                        tx_drops=0,
                    )
                    for name in interface_names
                ),
                ospf_neighbors=tuple(
                    OspfNeighborTelemetry(
                        neighbor=(
                            "untrusted-peer"
                            if unknown_protocol_peer and target.node == "access1" and index == 0
                            else peer.peer
                        ),
                        interface=peer.interface,
                        state=TelemetryState.UP,
                    )
                    for index, peer in enumerate(target.protocol_peers)
                    if not (omit_protocol_peer and target.node == "access1" and index == 0)
                ),
                bfd_peers=tuple(
                    BfdPeerTelemetry(
                        peer=(
                            "untrusted-peer"
                            if unknown_bfd_peer and target.node == "access1" and index == 0
                            else peer.peer
                        ),
                        interface=peer.interface,
                        configured=False,
                        state=TelemetryState.UNKNOWN,
                    )
                    for index, peer in enumerate(target.protocol_peers)
                ),
                routes=routes,
            )
        )
    snapshot = TelemetrySnapshot(
        snapshot_id="snapshot-validation",
        observed_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
        topology_fingerprint=binding.fingerprint,
        routers=tuple(routers),
    )
    return binding, snapshot


def test_snapshot_validation_rejects_partial_successful_interface_set() -> None:
    binding, snapshot = _validated_snapshot(omit_interface=True)

    with pytest.raises(ValueError, match="complete bound interface set"):
        validate_telemetry_snapshot(snapshot, binding)


def test_snapshot_validation_requires_explicit_service_route_observation() -> None:
    binding, snapshot = _validated_snapshot(omit_route=True)

    with pytest.raises(ValueError, match="exactly one service route"):
        validate_telemetry_snapshot(snapshot, binding)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"omit_protocol_peer": True}, "complete topology-bound OSPF peer set"),
        ({"unknown_protocol_peer": True}, "complete topology-bound OSPF peer set"),
        ({"unknown_bfd_peer": True}, "complete topology-bound BFD peer set"),
    ],
)
def test_snapshot_validation_requires_exact_topology_protocol_peers(
    options: dict[str, bool],
    message: str,
) -> None:
    binding, snapshot = _validated_snapshot(**options)

    with pytest.raises(ValueError, match=message):
        validate_telemetry_snapshot(snapshot, binding)


def test_snapshot_validation_rejects_timestamp_beyond_clock_skew_budget() -> None:
    binding, snapshot = _validated_snapshot()
    server_now = snapshot.observed_at - timedelta(seconds=61)

    with pytest.raises(ValueError, match="future skew"):
        validate_telemetry_ingest(snapshot, binding, now=server_now)
