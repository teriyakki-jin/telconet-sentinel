from pathlib import Path

import yaml

from telconet_sentinel.config import load_topology
from telconet_sentinel.models import ServiceImpact
from telconet_sentinel.resilience import audit_single_link_failures

ROOT = Path(__file__).parents[2]
LAB = ROOT / "lab"


def test_dual_homed_intent_passes_every_single_link_failure() -> None:
    baseline = load_topology(LAB / "intent.yml")
    candidate = load_topology(LAB / "intent-dual-homed.yml")
    audit = audit_single_link_failures(candidate)

    assert len(baseline.links) == 10
    assert len(candidate.links) == 11
    assert candidate.link("core1--service-host").cost == 10
    assert candidate.link("core2--service-host").cost == 30
    assert candidate.node("service-host").prefixes == ("10.20.0.0/24",)
    assert candidate.shortest_distance("access1", {"service-host"}) == 30
    assert candidate.shortest_distance(
        "access1", {"service-host"}, excluded_link="access1--agg1"
    ) == 140
    assert audit.passes_n_minus_one
    assert audit.count(ServiceImpact.OUTAGE) == 0


def test_dual_homed_lab_matches_intent_and_keeps_historical_lab_unchanged() -> None:
    variant = yaml.safe_load((LAB / "telconet-dual-homed.clab.yml").read_text())
    baseline = yaml.safe_load((LAB / "telconet.clab.yml").read_text())
    nodes = variant["topology"]["nodes"]
    links = variant["topology"]["links"]
    pairs = {
        frozenset(endpoint.split(":", 1)[0] for endpoint in link["endpoints"])
        for link in links
    }

    assert "-service-host" not in baseline["topology"]["nodes"]["core2"].get("binds", [])
    assert len(baseline["topology"]["links"]) == 12
    assert len(links) == 13
    assert {"core1", "service-host"} in [set(pair) for pair in pairs]
    assert {"core2", "service-host"} in [set(pair) for pair in pairs]
    assert nodes["service-host"]["image"] == "quay.io/frrouting/frr:10.7.0"
    assert all(bind.endswith(":ro") for bind in nodes["service-host"]["binds"])


def test_dual_homed_frr_service_prefix_and_transit_costs() -> None:
    core1 = (LAB / "frr" / "dual-homed" / "core1.conf").read_text()
    core2 = (LAB / "frr" / "dual-homed" / "core2.conf").read_text()
    service = (LAB / "frr" / "dual-homed" / "service-host.conf").read_text()

    assert "ip address 10.0.3.0/31" in core1
    assert "ip ospf cost 1" in core1
    assert "ip address 10.0.3.2/31" in core2
    assert "ip ospf cost 21" in core2
    assert "ip address 10.20.0.10/24" in service
    assert "ip ospf cost 9" in service
    assert "passive-interface lo" in service


def test_dual_homing_e2e_injects_and_restores_service_link_failure() -> None:
    scenario = (ROOT / "scenarios" / "dual_homing_e2e.sh").read_text()
    workflow = (ROOT / ".github" / "workflows" / "lab-e2e.yml").read_text()

    assert "telconet-dual-homed.clab.yml" in scenario
    assert "intent-dual-homed.yml" in scenario
    assert "ip link set dev eth4 down" in scenario
    assert "ip link set dev eth4 up" in scenario
    assert "show ip route 10.20.0.0/24 json" in scenario
    assert "E2E_DUAL_HOMING" in scenario
    assert "trap cleanup EXIT" in scenario
    assert "bash scenarios/dual_homing_e2e.sh" in workflow
    assert "artifacts/dual-homing" in workflow
