from telconet_sentinel.impact import analyze_link_down
from telconet_sentinel.models import Link, NetworkEvent, Node, NodeRole, ServiceImpact
from telconet_sentinel.topology import Topology


def test_redundant_access_link_failure_is_degraded(redundant_topology: Topology) -> None:
    result = analyze_link_down(redundant_topology, NetworkEvent("access1--agg1"))

    assert result.failed_component == "access1--agg1"
    assert result.service_impact is ServiceImpact.DEGRADED
    assert result.affected_nodes == ("access1",)
    assert result.affected_prefixes == ("10.10.1.0/24",)
    assert "alternate service path available" in result.evidence


def test_single_homed_access_link_failure_is_outage() -> None:
    topology = Topology(
        [
            Node("access1", NodeRole.ACCESS, ("10.10.1.0/24",)),
            Node("agg1", NodeRole.AGGREGATION),
            Node("core1", NodeRole.CORE),
            Node("service-host", NodeRole.SERVICE, ("10.20.0.0/24",)),
        ],
        [
            Link("access1--agg1", "access1", "agg1"),
            Link("agg1--core1", "agg1", "core1"),
            Link("core1--service-host", "core1", "service-host"),
        ],
    )

    result = analyze_link_down(topology, NetworkEvent("access1--agg1"))

    assert result.service_impact is ServiceImpact.OUTAGE
    assert result.affected_nodes == ("access1",)
    assert "no alternate service path" in result.evidence


def test_aggregation_uplink_failure_affects_attached_access_nodes(
    redundant_topology: Topology,
) -> None:
    result = analyze_link_down(redundant_topology, NetworkEvent("agg1--core1"))

    assert result.service_impact is ServiceImpact.DEGRADED
    assert result.affected_nodes == ("access1",)
    assert result.affected_prefixes == ("10.10.1.0/24",)


def test_backup_link_failure_only_reduces_redundancy(redundant_topology: Topology) -> None:
    result = analyze_link_down(redundant_topology, NetworkEvent("access1--agg2"))

    assert result.service_impact is ServiceImpact.REDUNDANCY_REDUCED
    assert result.affected_nodes == ()
    assert result.affected_prefixes == ()
    assert "active shortest path unchanged" in result.evidence


def test_core_interconnect_failure_increases_path_to_attached_service(
    redundant_topology: Topology,
) -> None:
    result = analyze_link_down(redundant_topology, NetworkEvent("core1--core2"))

    assert result.service_impact is ServiceImpact.DEGRADED
    assert result.affected_nodes == ("access2",)
    assert result.affected_prefixes == ("10.10.2.0/24",)
