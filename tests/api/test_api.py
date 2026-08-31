from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from telconet_sentinel.api import create_app
from telconet_sentinel.topology import Topology


def test_health_and_topology(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))

    assert client.get("/health").json() == {"status": "ok"}
    topology = client.get("/api/topology")
    assert topology.status_code == 200
    assert len(topology.json()["nodes"]) == 7
    assert len(topology.json()["links"]) == 10
    assert {link["id"]: link["cost"] for link in topology.json()["links"]}["access1--agg1"] == 10


def test_metrics_returns_service_unavailable_without_evidence(
    redundant_topology: Topology,
) -> None:
    client = TestClient(create_app(redundant_topology))

    response = client.get("/metrics")

    assert response.status_code == 503
    assert response.json()["detail"] == "experiment evidence is unavailable"


def test_exposes_latest_experiment_as_prometheus_metrics(redundant_topology: Topology) -> None:
    evidence = {
        "profiles": {
            "ospf_only": {
                "observed_detection_upper_bound_ms": 3900,
                "packets_lost_until_failover": 29,
                "capture_packet_loss_percent": 50.0,
            },
            "bfd_100x3": {
                "observed_detection_upper_bound_ms": 300,
                "packets_lost_until_failover": 3,
                "capture_packet_loss_percent": 16.667,
            },
        }
    }
    client = TestClient(create_app(redundant_topology, experiment_evidence=evidence))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'telconet_detection_seconds{profile="bfd_100x3"} 0.3' in response.text


def test_rejects_invalid_experiment_evidence_when_app_starts(
    redundant_topology: Topology,
) -> None:
    with pytest.raises(ValueError, match="profiles"):
        create_app(redundant_topology, experiment_evidence={})


def test_ingests_event_and_returns_explainable_incident(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))

    response = client.post(
        "/api/events",
        json={"event_type": "link_down", "link_id": "access1--agg1"},
    )

    assert response.status_code == 201
    incident = response.json()
    assert incident["failed_component"] == "access1--agg1"
    assert incident["affected_nodes"] == ["access1"]
    assert incident["affected_prefixes"] == ["10.10.1.0/24"]
    assert incident["service_impact"] == "degraded"
    assert incident["recommended_action"] == {
        "action": "restore_link",
        "target": "access1--agg1",
    }
    assert incident["status"] == "awaiting_approval"


def test_deduplicates_repeated_event(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))
    payload = {"event_type": "link_down", "link_id": "access1--agg1"}

    first = client.post("/api/events", json=payload).json()
    second = client.post("/api/events", json=payload).json()

    assert second["id"] == first["id"]


def test_approves_recovery_once(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))
    incident = client.post(
        "/api/events",
        json={"event_type": "link_down", "link_id": "access1--agg1"},
    ).json()

    approved = client.post(f"/api/incidents/{incident['id']}/approve")
    repeated = client.post(f"/api/incidents/{incident['id']}/approve")

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert repeated.status_code == 409


def test_approved_incident_does_not_hide_recurrence(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))
    payload = {"event_type": "link_down", "link_id": "access1--agg1"}
    first = client.post("/api/events", json=payload).json()
    client.post(f"/api/incidents/{first['id']}/approve")

    recurrence = client.post("/api/events", json=payload).json()

    assert recurrence["id"] != first["id"]
    assert recurrence["status"] == "awaiting_approval"


def test_client_timestamp_does_not_bypass_server_dedup_window(
    redundant_topology: Topology,
) -> None:
    client = TestClient(create_app(redundant_topology))
    start = datetime(2026, 8, 30, tzinfo=timezone.utc)
    first = client.post(
        "/api/events",
        json={
            "event_type": "link_down",
            "link_id": "access1--agg1",
            "observed_at": start.isoformat(),
        },
    ).json()

    recurrence = client.post(
        "/api/events",
        json={
            "event_type": "link_down",
            "link_id": "access1--agg1",
            "observed_at": (start + timedelta(seconds=61)).isoformat(),
        },
    ).json()

    assert recurrence["id"] == first["id"]


def test_rejects_timezone_naive_observed_at(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))

    response = client.post(
        "/api/events",
        json={
            "event_type": "link_down",
            "link_id": "access1--agg1",
            "observed_at": "2026-08-30T00:00:00",
        },
    )

    assert response.status_code == 422


def test_rejects_unknown_link_and_unsupported_event(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))

    unknown = client.post(
        "/api/events",
        json={"event_type": "link_down", "link_id": "missing--link"},
    )
    unsupported = client.post(
        "/api/events",
        json={"event_type": "run_shell", "link_id": "access1--agg1"},
    )

    assert unknown.status_code == 400
    assert unsupported.status_code == 400
    assert "unknown link" in unknown.json()["detail"]
    assert "unsupported event" in unsupported.json()["detail"]


def test_rejects_oversized_event_fields(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))

    response = client.post(
        "/api/events",
        json={"event_type": "link_down", "link_id": "a" * 129},
    )

    assert response.status_code == 422
