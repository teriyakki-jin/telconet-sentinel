from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from telconet_sentinel.api import create_app
from telconet_sentinel.convergence import ConvergenceStore
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


def test_exposes_repeated_trial_metrics_when_evidence_is_available(
    redundant_topology: Topology,
) -> None:
    evidence = {
        "profiles": {
            profile: {
                "observed_detection_upper_bound_ms": 300,
                "packets_lost_until_failover": 2,
                "capture_packet_loss_percent": 1.5,
            }
            for profile in ("ospf_only", "bfd_100x3")
        }
    }
    repeated = {
        "trial_count_per_profile": 20,
        "profiles": {
            profile: {
                "detection_ms": {"p50": 300, "p95": 350, "max": 400},
                "trials": [
                    {"trial": trial, "detection_ms": 300 + trial}
                    for trial in range(1, 21)
                ],
            }
            for profile in ("ospf_only", "bfd_100x3")
        },
    }
    client = TestClient(
        create_app(
            redundant_topology,
            experiment_evidence=evidence,
            repeated_experiment_evidence=repeated,
        )
    )

    response = client.get("/metrics")

    assert response.status_code == 200
    assert 'telconet_trial_detection_seconds{profile="bfd_100x3",trial="20"}' in (
        response.text
    )


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


def _experiment_evidence() -> dict[str, object]:
    return {
        "profiles": {
            profile: {
                "observed_detection_upper_bound_ms": 300,
                "packets_lost_until_failover": 2,
                "capture_packet_loss_percent": 1.5,
            }
            for profile in ("ospf_only", "bfd_100x3")
        }
    }


def test_creates_and_reads_a_live_convergence_run(redundant_topology: Topology) -> None:
    store = ConvergenceStore()
    client = TestClient(create_app(redundant_topology, convergence_store=store))

    missing = client.get("/api/convergence-runs/latest")
    created = client.post(
        "/api/convergence-runs",
        json={
            "profile": "bfd_100x3",
            "source": "access1",
            "target": "10.20.0.10",
        },
    )

    assert missing.status_code == 404
    assert created.status_code == 201
    assert created.json()["status"] == "collecting"
    assert created.json()["events"] == []
    latest = client.get("/api/convergence-runs/latest")
    assert latest.status_code == 200
    assert latest.json()["id"] == created.json()["id"]


def test_records_live_events_and_exposes_current_metrics(
    redundant_topology: Topology,
) -> None:
    store = ConvergenceStore()
    client = TestClient(
        create_app(
            redundant_topology,
            experiment_evidence=_experiment_evidence(),
            convergence_store=store,
        )
    )
    started_at = datetime.now(timezone.utc)
    run = client.post(
        "/api/convergence-runs",
        json={
            "profile": "bfd_100x3",
            "source": "access1",
            "target": "10.20.0.10",
            "started_at": started_at.isoformat(),
        },
    ).json()

    events = [
        {"event": "blackhole_injected", "offset_ms": 0},
        {"event": "bfd_down", "offset_ms": 312},
        {"event": "ospf_neighbor_down", "offset_ms": 338},
        {"event": "route_failover", "offset_ms": 421, "route_metric": 140},
        {
            "event": "data_plane_recovered",
            "offset_ms": 498,
            "icmp_sequence": 8,
        },
    ]
    for event in events:
        response = client.post(f"/api/convergence-runs/{run['id']}/events", json=event)
        assert response.status_code == 201

    assert response.json()["status"] == "complete"
    assert response.json()["events"][-1]["icmp_sequence"] == 8
    metrics = client.get("/metrics").text
    assert (
        'telconet_live_event_offset_seconds{event="route_failover",profile="bfd_100x3"} '
        "0.421" in metrics
    )


def test_rejects_invalid_or_duplicate_live_events(redundant_topology: Topology) -> None:
    client = TestClient(create_app(redundant_topology))
    run = client.post(
        "/api/convergence-runs",
        json={
            "profile": "bfd_100x3",
            "source": "access1",
            "target": "10.20.0.10",
        },
    ).json()
    endpoint = f"/api/convergence-runs/{run['id']}/events"

    invalid = client.post(endpoint, json={"event": "bfd_down", "offset_ms": -1})
    first = client.post(
        endpoint,
        json={"event": "blackhole_injected", "offset_ms": 0},
    )
    duplicate = client.post(
        endpoint,
        json={"event": "blackhole_injected", "offset_ms": 1},
    )
    missing = client.post(
        "/api/convergence-runs/missing/events",
        json={"event": "blackhole_injected", "offset_ms": 0},
    )

    assert invalid.status_code == 422
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert missing.status_code == 404
