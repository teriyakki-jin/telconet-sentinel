from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from telconet_sentinel.api import create_app
from telconet_sentinel.config import load_topology
from telconet_sentinel.telemetry_config import (
    TelemetryTopologyBinding,
    load_telemetry_binding,
)
from telconet_sentinel.telemetry_models import TelemetrySnapshot
from telconet_sentinel.telemetry_store import TelemetryStore

ROOT = Path(__file__).parents[2]


def _binding() -> TelemetryTopologyBinding:
    return load_telemetry_binding(
        ROOT / "lab" / "telemetry-targets.yml",
        ROOT / "lab" / "intent.yml",
        ROOT / "lab" / "telconet.clab.yml",
    )


def _snapshot_payload(
    binding: TelemetryTopologyBinding,
    *,
    snapshot_id: str = "snapshot-0001",
    observed_at: datetime | None = None,
) -> dict[str, object]:
    timestamp = observed_at or datetime(2026, 10, 25, 1, 2, 3, tzinfo=timezone.utc)
    routers = []
    for target in binding.targets:
        name = target.node
        interfaces = [
            {
                "name": interface,
                "state": "up",
                "rx_bytes": 1_024 if name == "access1" and interface == "eth1" else 0,
                "tx_bytes": 2_048 if name == "access1" and interface == "eth1" else 0,
                "rx_drops": 0,
                "tx_drops": 1 if name == "access1" and interface == "eth1" else 0,
            }
            for interface in target.interfaces
        ]
        routers.append(
            {
                "name": name,
                "collection_success": True,
                "state": "up",
                "interfaces": interfaces,
                "ospf_neighbors": [
                    {
                        "neighbor": peer.peer,
                        "interface": peer.interface,
                        "state": "up",
                    }
                    for peer in target.protocol_peers
                ],
                "bfd_peers": [
                    {
                        "peer": peer.peer,
                        "interface": peer.interface,
                        "configured": name == "access1" and peer.interface == "eth1",
                        "state": (
                            "up"
                            if name == "access1" and peer.interface == "eth1"
                            else "unknown"
                        ),
                    }
                    for peer in target.protocol_peers
                ],
                "routes": [
                    {
                        "prefix": "10.20.0.0/24",
                        "present": True,
                        "metric": 30,
                        "egress_interface": target.interfaces[0],
                    }
                ],
            }
        )
    return {
        "snapshot_id": snapshot_id,
        "observed_at": timestamp.isoformat(),
        "topology_fingerprint": binding.fingerprint,
        "routers": routers,
    }


def _client() -> tuple[TestClient, TelemetryTopologyBinding]:
    binding = _binding()
    app = create_app(
        load_topology(ROOT / "lab" / "intent.yml"),
        experiment_evidence={
            "profiles": {
                profile: {
                    "observed_detection_upper_bound_ms": 1,
                    "packets_lost_until_failover": 0,
                    "capture_packet_loss_percent": 0,
                }
                for profile in ("ospf_only", "bfd_100x3")
            }
        },
        telemetry_binding=binding,
        telemetry_store=TelemetryStore(capacity=3),
    )
    return TestClient(app), binding


def test_ingests_and_reads_latest_typed_telemetry_snapshot() -> None:
    client, binding = _client()
    payload = _snapshot_payload(binding)

    created = client.post("/api/telemetry/snapshots", json=payload)
    latest = client.get("/api/telemetry/snapshots/latest")

    assert created.status_code == 201
    assert TelemetrySnapshot.model_validate(created.json()) == (
        TelemetrySnapshot.model_validate(payload)
    )
    assert latest.status_code == 200
    assert TelemetrySnapshot.model_validate(latest.json()) == (
        TelemetrySnapshot.model_validate(payload)
    )


def test_telemetry_ingestion_rejects_configuration_drift_and_unknown_fields() -> None:
    client, binding = _client()
    drifted = _snapshot_payload(binding)
    drifted["topology_fingerprint"] = "0" * 64
    unknown = _snapshot_payload(binding)
    unknown["command"] = "docker exec anything"

    assert client.post("/api/telemetry/snapshots", json=drifted).status_code == 409
    assert client.post("/api/telemetry/snapshots", json=unknown).status_code == 422


def test_telemetry_ingestion_rejects_a_clock_poisoning_snapshot() -> None:
    client, binding = _client()
    future = _snapshot_payload(
        binding,
        observed_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    response = client.post("/api/telemetry/snapshots", json=future)

    assert response.status_code == 409
    assert "future skew" in response.json()["detail"]


def test_duplicate_retry_is_idempotent_and_older_snapshot_cannot_replace_latest() -> None:
    client, binding = _client()
    current_at = datetime(2026, 10, 25, 1, 2, 3, tzinfo=timezone.utc)
    current = _snapshot_payload(binding, observed_at=current_at)
    older = _snapshot_payload(
        binding,
        snapshot_id="snapshot-older",
        observed_at=current_at - timedelta(seconds=1),
    )

    assert client.post("/api/telemetry/snapshots", json=current).status_code == 201
    assert client.post("/api/telemetry/snapshots", json=current).status_code == 200
    assert client.post("/api/telemetry/snapshots", json=older).status_code == 409
    assert client.get("/api/telemetry/snapshots/latest").json()["snapshot_id"] == "snapshot-0001"


def test_metrics_expose_telemetry_without_treating_unknown_as_healthy() -> None:
    client, binding = _client()
    payload = _snapshot_payload(binding)
    routers = payload["routers"]
    assert isinstance(routers, list)
    routers[1]["collection_success"] = False  # type: ignore[index]
    routers[1]["state"] = "unknown"  # type: ignore[index]
    routers[1]["collection_error"] = "router did not answer"  # type: ignore[index]
    routers[1]["interfaces"] = []  # type: ignore[index]
    routers[1]["ospf_neighbors"] = []  # type: ignore[index]
    routers[1]["bfd_peers"] = []  # type: ignore[index]
    routers[1]["routes"] = []  # type: ignore[index]

    assert client.post("/api/telemetry/snapshots", json=payload).status_code == 201
    metrics = client.get("/metrics")

    assert metrics.status_code == 200
    assert 'telconet_router_collection_success{node="access1"} 1' in metrics.text
    assert 'telconet_router_collection_success{node="access2"} 0' in metrics.text
    assert (
        'telconet_interface_receive_bytes_total{node="access1",interface="eth1",'
        'link_id="access1--agg1"} 1024' in metrics.text
    )
    assert (
        'telconet_selected_route_metric{node="access1",prefix="10.20.0.0/24"} 30'
        in metrics.text
    )


def test_telemetry_endpoints_are_unavailable_without_a_server_side_binding() -> None:
    client = TestClient(create_app(load_topology(ROOT / "lab" / "intent.yml")))

    assert client.get("/api/telemetry/snapshots/latest").status_code == 404
    assert client.post("/api/telemetry/snapshots", json={}).status_code == 404


def test_snapshot_from_a_previous_topology_fingerprint_is_quarantined() -> None:
    binding = _binding()
    store = TelemetryStore(capacity=3)
    old_payload = _snapshot_payload(binding)
    old_payload["topology_fingerprint"] = "0" * 64
    store.save(TelemetrySnapshot.model_validate(old_payload))
    app = create_app(
        load_topology(ROOT / "lab" / "intent.yml"),
        experiment_evidence={
            "profiles": {
                profile: {
                    "observed_detection_upper_bound_ms": 1,
                    "packets_lost_until_failover": 0,
                    "capture_packet_loss_percent": 0,
                }
                for profile in ("ospf_only", "bfd_100x3")
            }
        },
        telemetry_binding=binding,
        telemetry_store=store,
    )
    client = TestClient(app)

    assert client.get("/api/telemetry/snapshots/latest").status_code == 404
    metric_lines = client.get("/metrics").text.splitlines()
    assert not any(
        line.startswith("telconet_telemetry_snapshot_timestamp_seconds ")
        for line in metric_lines
    )


def test_snapshot_rejected_by_global_retention_is_not_reported_as_a_retry() -> None:
    binding = _binding()
    store = TelemetryStore(capacity=1)
    current_time = datetime.now(timezone.utc)
    old_payload = _snapshot_payload(
        binding,
        snapshot_id="old-topology",
        observed_at=current_time,
    )
    old_payload["topology_fingerprint"] = "0" * 64
    store.save(TelemetrySnapshot.model_validate(old_payload))
    app = create_app(
        load_topology(ROOT / "lab" / "intent.yml"),
        experiment_evidence={
            "profiles": {
                profile: {
                    "observed_detection_upper_bound_ms": 1,
                    "packets_lost_until_failover": 0,
                    "capture_packet_loss_percent": 0,
                }
                for profile in ("ospf_only", "bfd_100x3")
            }
        },
        telemetry_binding=binding,
        telemetry_store=store,
    )
    client = TestClient(app)
    current_payload = _snapshot_payload(
        binding,
        snapshot_id="current-topology",
        observed_at=current_time - timedelta(seconds=1),
    )

    response = client.post("/api/telemetry/snapshots", json=current_payload)

    assert response.status_code == 409
    assert "retention" in response.json()["detail"]
    assert client.get("/api/telemetry/snapshots/latest").status_code == 404
