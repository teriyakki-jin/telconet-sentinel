import json
from pathlib import Path

from telconet_sentinel.evidence import build_simulated_evidence, write_evidence
from telconet_sentinel.models import NetworkEvent
from telconet_sentinel.topology import Topology


def test_simulated_evidence_is_explicitly_not_a_lab_measurement(
    redundant_topology: Topology,
) -> None:
    evidence = build_simulated_evidence(
        redundant_topology,
        NetworkEvent("access1--agg1"),
    )

    assert evidence["scenario"] == "access_uplink_failure"
    assert evidence["source"] == "scenario_injected_event"
    assert evidence["measured"] is False
    assert evidence["failed_link"] == "access1--agg1"
    assert evidence["analysis"]["service_impact"] == "degraded"
    assert evidence["analysis"]["affected_prefixes"] == ["10.10.1.0/24"]
    assert evidence["recovery"]["status"] == "awaiting_approval"
    assert "convergence_ms" not in evidence
    assert "packet_loss_percent" not in evidence


def test_writes_deterministic_json(tmp_path: Path, redundant_topology: Topology) -> None:
    path = tmp_path / "link-failure.json"
    evidence = build_simulated_evidence(
        redundant_topology,
        NetworkEvent("access1--agg1"),
    )

    write_evidence(path, evidence)

    assert json.loads(path.read_text(encoding="utf-8")) == evidence
    assert path.read_text(encoding="utf-8").endswith("\n")
