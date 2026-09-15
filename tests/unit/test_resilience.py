import pytest

from telconet_sentinel.models import Link, Node, NodeRole, ServiceImpact
from telconet_sentinel.resilience import audit_single_link_failures, render_resilience_metrics
from telconet_sentinel.topology import Topology


def test_audits_every_link_and_exposes_the_service_single_point(
    redundant_topology: Topology,
) -> None:
    audit = audit_single_link_failures(redundant_topology)

    assert audit.total_scenarios == 10
    assert audit.passes_n_minus_one is False
    assert audit.count(ServiceImpact.OUTAGE) == 1
    assert audit.count(ServiceImpact.DEGRADED) == 5
    assert audit.count(ServiceImpact.REDUNDANCY_REDUCED) == 4
    assert [scenario.link_id for scenario in audit.scenarios] == sorted(
        scenario.link_id for scenario in audit.scenarios
    )

    outage = next(
        scenario
        for scenario in audit.scenarios
        if scenario.service_impact is ServiceImpact.OUTAGE
    )
    assert outage.link_id == "core1--service-host"
    assert outage.endpoints == ("core1", "service-host")
    assert outage.affected_nodes == ("access1", "access2")
    assert outage.affected_prefixes == ("10.10.1.0/24", "10.10.2.0/24")


def test_marks_a_topology_without_outage_scenarios_as_n_minus_one_compliant(
    redundant_topology: Topology,
) -> None:
    links = [
        *redundant_topology.links,
        Link(
            "core2--service-host",
            "core2",
            "service-host",
            10,
        ),
    ]

    audit = audit_single_link_failures(
        Topology(redundant_topology.nodes, links),
    )

    assert audit.passes_n_minus_one is True
    assert audit.count(ServiceImpact.OUTAGE) == 0


def test_renders_bounded_label_prometheus_metrics(
    redundant_topology: Topology,
) -> None:
    rendered = render_resilience_metrics(audit_single_link_failures(redundant_topology))

    assert "telconet_n1_design_pass 0" in rendered
    assert 'telconet_n1_scenarios_total{impact="outage"} 1' in rendered
    assert (
        'telconet_n1_link_impact{impact="outage",link_id="core1--service-host"} 1'
        in rendered
    )


def test_rejects_audit_without_access_nodes_or_a_baseline_service_path() -> None:
    with pytest.raises(ValueError, match="access node"):
        audit_single_link_failures(
            Topology([Node("service", NodeRole.SERVICE)], []),
        )

    without_service = Topology(
        [Node("access", NodeRole.ACCESS), Node("core", NodeRole.CORE)],
        [Link("access--core", "access", "core")],
    )
    with pytest.raises(ValueError, match="service node"):
        audit_single_link_failures(without_service)

    disconnected = Topology(
        [
            Node("access", NodeRole.ACCESS),
            Node("service", NodeRole.SERVICE),
            Node("core", NodeRole.CORE),
        ],
        [Link("access--core", "access", "core")],
    )
    with pytest.raises(ValueError, match="baseline service path"):
        audit_single_link_failures(disconnected)
