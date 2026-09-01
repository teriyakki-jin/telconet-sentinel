from pathlib import Path

import yaml

from telconet_sentinel.config import load_topology
from telconet_sentinel.models import NodeRole

ROOT = Path(__file__).parents[2]
LAB_FILE = ROOT / "lab" / "telconet.clab.yml"
INTENT_FILE = ROOT / "lab" / "intent.yml"
SCENARIO_FILE = ROOT / "scenarios" / "link_failure_lab.sh"
BFD_SCENARIO_FILE = ROOT / "scenarios" / "bfd_comparison_lab.sh"
REPEATED_SCENARIO_FILE = ROOT / "scenarios" / "bfd_repeated_trials_lab.sh"
OSPF_DESIGN_FILE = ROOT / "docs" / "OSPF_DESIGN.md"


def test_intent_is_single_source_for_impact_topology() -> None:
    topology = load_topology(INTENT_FILE)

    assert len(topology.nodes) == 7
    assert len(topology.links) == 10
    assert {node.name for node in topology.nodes if node.role is NodeRole.ACCESS} == {
        "access1",
        "access2",
    }
    assert topology.node("access1").prefixes == ("10.10.1.0/24",)
    assert topology.node("service-host").role is NodeRole.SERVICE


def test_containerlab_declares_six_routers_two_clients_and_service() -> None:
    document = yaml.safe_load(LAB_FILE.read_text(encoding="utf-8"))
    nodes = document["topology"]["nodes"]

    assert set(nodes) == {
        "core1",
        "core2",
        "agg1",
        "agg2",
        "access1",
        "access2",
        "client-a",
        "client-b",
        "service-host",
    }
    assert len(document["topology"]["links"]) == 12
    for router in ("core1", "core2", "agg1", "agg2", "access1", "access2"):
        assert nodes[router]["image"] == "quay.io/frrouting/frr:10.7.0"
        assert ":latest" not in nodes[router]["image"]
        assert nodes[router]["privileged"] is False
        assert set(nodes[router]["cap-add"]) == {"NET_ADMIN", "NET_RAW", "SYS_ADMIN"}
        assert all(bind.endswith(":ro") for bind in nodes[router]["binds"])


def test_each_router_enables_ospf_with_unique_router_id() -> None:
    router_ids: set[str] = set()
    for router in ("core1", "core2", "agg1", "agg2", "access1", "access2"):
        config = (ROOT / "lab" / "frr" / router / "frr.conf").read_text(encoding="utf-8")
        assert "router ospf" in config
        router_id_line = next(
            line.strip() for line in config.splitlines() if "ospf router-id" in line
        )
        router_ids.add(router_id_line)

    assert len(router_ids) == 6


def test_every_router_transit_interface_matches_ospf_intent() -> None:
    topology = load_topology(INTENT_FILE)
    document = yaml.safe_load(LAB_FILE.read_text(encoding="utf-8"))
    router_names = {"core1", "core2", "agg1", "agg2", "access1", "access2"}
    endpoint_interfaces = {
        frozenset(endpoint.split(":", 1)[0] for endpoint in link["endpoints"]): {
            node: interface
            for endpoint in link["endpoints"]
            for node, interface in [endpoint.split(":", 1)]
        }
        for link in document["topology"]["links"]
    }

    for link in topology.links:
        if {link.endpoint_a, link.endpoint_b} <= router_names:
            interfaces = endpoint_interfaces[frozenset((link.endpoint_a, link.endpoint_b))]
            for router in (link.endpoint_a, link.endpoint_b):
                config = (ROOT / "lab" / "frr" / router / "frr.conf").read_text(
                    encoding="utf-8"
                )
                block = config.split(f"interface {interfaces[router]}\n", 1)[1].split("\n!", 1)[0]
                assert "ip ospf network point-to-point" in block
                assert "ip ospf hello-interval 1" in block
                assert "ip ospf dead-interval 4" in block
                assert f"ip ospf cost {link.cost}" in block


def test_containerlab_router_links_match_intent() -> None:
    topology = load_topology(INTENT_FILE)
    document = yaml.safe_load(LAB_FILE.read_text(encoding="utf-8"))
    clab_pairs = {
        frozenset(endpoint.split(":", 1)[0] for endpoint in link["endpoints"])
        for link in document["topology"]["links"]
    }

    for link in topology.links:
        assert frozenset((link.endpoint_a, link.endpoint_b)) in clab_pairs


def test_failure_scenario_captures_raw_log_and_builds_evidence() -> None:
    scenario = SCENARIO_FILE.read_text(encoding="utf-8")

    assert "TELCONET_MEASUREMENT version=1" in scenario
    assert "containerlab destroy" in scenario
    assert "configuration_sha256" in scenario
    assert "apk add --no-cache iputils" in scenario
    assert "ping -D -i 0.1 -c 160" in scenario
    assert "EVENT type=link_down" in scenario
    assert "EVENT type=link_up" in scenario
    assert "python3 -m telconet_sentinel.measurement" in scenario
    assert "measured-link-failure.log" in scenario
    assert "measured-link-failure.json" in scenario


def test_bfd_scenario_uses_remote_blackhole_and_two_profiles() -> None:
    scenario = BFD_SCENARIO_FILE.read_text(encoding="utf-8")
    daemons = (ROOT / "lab" / "frr" / "daemons").read_text(encoding="utf-8")

    assert "bfdd=yes" in daemons
    assert "tc qdisc add dev eth1 root netem loss 100%" in scenario
    assert "ip ospf bfd 3 100 100" in scenario
    assert "show bfd peers brief" in scenario
    assert "capture_profile ospf_only" in scenario
    assert "capture_profile bfd_100x3" in scenario
    assert "python3 -m telconet_sentinel.bfd_comparison" in scenario


def test_repeated_scenario_runs_twenty_paired_profiles() -> None:
    scenario = REPEATED_SCENARIO_FILE.read_text(encoding="utf-8")

    assert 'trials="${TELCONET_TRIALS:-20}"' in scenario
    assert "ping -D -i 0.1 -c 80" in scenario
    assert "tc qdisc add dev eth1 clsact" in scenario
    assert "tc filter add dev eth1 ingress protocol all pref 1 flower action drop" in (
        scenario
    )
    assert "no ip ospf bfd" in scenario
    assert "Session count: 0" in scenario
    assert "capture_profile ospf_only" in scenario
    assert "capture_profile bfd_100x3" in scenario
    assert (
        "disable_bfd_profile\n  wait_for_no_bfd\n  wait_for_metric 30\n  "
        "capture_profile ospf_only"
        in scenario
    )
    assert "python3 -m telconet_sentinel.repeated_trials" in scenario
    assert "bfd-repeated-trials.json" in scenario


def test_ospf_design_document_tracks_implemented_policy() -> None:
    design = OSPF_DESIGN_FILE.read_text(encoding="utf-8")

    assert "Area 0" in design
    assert "point-to-point" in design
    assert "/31" in design
    assert "/32" in design
    assert "hello 1초 / dead 4초" in design
    assert "10 + 10 + 10 = 30" in design
    assert "100 + 10 + 20 + 10 = 140" in design
    for router_id in (
        "10.255.0.11",
        "10.255.0.12",
        "10.255.0.21",
        "10.255.0.22",
        "10.255.0.31",
        "10.255.0.32",
    ):
        assert router_id in design
