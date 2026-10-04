import pytest

from telconet_sentinel.audit import audit_fault_scenarios
from telconet_sentinel.fault import (
    FaultKind,
    FaultScenario,
    link_failure_scenarios,
    transport_node_failure_scenarios,
)
from telconet_sentinel.models import Link, Node, NodeRole, ServiceImpact
from telconet_sentinel.topology import Topology


def test_generates_deterministic_link_and_transport_node_scenarios(
    redundant_topology: Topology,
) -> None:
    links = link_failure_scenarios(redundant_topology)
    nodes = transport_node_failure_scenarios(redundant_topology)

    assert len(links) == 10
    assert links[0].id == "link:access1--agg1"
    assert links[0].excluded_links == frozenset({"access1--agg1"})
    assert [scenario.id for scenario in nodes] == [
        "node:agg1",
        "node:agg2",
        "node:core1",
        "node:core2",
    ]


def test_common_engine_preserves_existing_link_and_node_results(
    redundant_topology: Topology,
) -> None:
    link_audit = audit_fault_scenarios(
        redundant_topology,
        link_failure_scenarios(redundant_topology),
    )
    node_audit = audit_fault_scenarios(
        redundant_topology,
        transport_node_failure_scenarios(redundant_topology),
    )

    assert link_audit.total_scenarios == 10
    assert link_audit.count(ServiceImpact.OUTAGE) == 1
    assert link_audit.count(ServiceImpact.DEGRADED) == 5
    assert node_audit.total_scenarios == 4
    assert node_audit.count(ServiceImpact.OUTAGE) == 1
    assert node_audit.scenarios[2].fault.id == "node:core1"
    assert node_audit.scenarios[2].affected_nodes == ("access1", "access2")


def test_common_engine_supports_a_multi_component_fault(
    redundant_topology: Topology,
) -> None:
    candidate = Topology(
        redundant_topology.nodes,
        [
            *redundant_topology.links,
            Link("core2--service-host", "core2", "service-host", 30),
        ],
    )
    shared_risk = FaultScenario(
        id="shared-risk:service-entry",
        kind=FaultKind.COMPOSITE,
        excluded_links=frozenset(
            {"core1--service-host", "core2--service-host"}
        ),
    )

    audit = audit_fault_scenarios(candidate, (shared_risk,))

    assert audit.passes_n_minus_one is False
    assert audit.scenarios[0].service_impact is ServiceImpact.OUTAGE
    assert audit.scenarios[0].affected_nodes == ("access1", "access2")
    assert [
        (path.access_node, path.baseline_cost, path.post_fault_cost)
        for path in audit.scenarios[0].paths
    ] == [
        ("access1", 30, None),
        ("access2", 50, None),
    ]


def test_common_engine_rejects_empty_or_unknown_faults(
    redundant_topology: Topology,
) -> None:
    with pytest.raises(ValueError, match="at least one fault scenario"):
        audit_fault_scenarios(redundant_topology, ())

    unknown = FaultScenario(
        id="link:missing",
        kind=FaultKind.LINK,
        excluded_links=frozenset({"missing"}),
    )
    with pytest.raises(ValueError, match="unknown link"):
        audit_fault_scenarios(redundant_topology, (unknown,))

    duplicate_id = (
        FaultScenario(
            id="duplicate",
            kind=FaultKind.LINK,
            excluded_links=frozenset({"access1--agg1"}),
        ),
        FaultScenario(
            id="duplicate",
            kind=FaultKind.LINK,
            excluded_links=frozenset({"access1--agg2"}),
        ),
    )
    with pytest.raises(ValueError, match="duplicate fault scenario id"):
        audit_fault_scenarios(redundant_topology, duplicate_id)


def test_fault_scenario_requires_a_component_and_matching_kind() -> None:
    with pytest.raises(ValueError, match="fault scenario id"):
        FaultScenario(
            id="",
            kind=FaultKind.LINK,
            excluded_links=frozenset({"access1--agg1"}),
        )

    with pytest.raises(ValueError, match="at least one component"):
        FaultScenario(id="empty", kind=FaultKind.COMPOSITE)

    with pytest.raises(ValueError, match="link fault"):
        FaultScenario(
            id="bad-link",
            kind=FaultKind.LINK,
            excluded_nodes=frozenset({"core1"}),
        )


def test_fault_factory_accepts_maximum_length_component_ids() -> None:
    long_name = "a" * 128
    topology = Topology(
        [
            Node("access", NodeRole.ACCESS),
            Node(long_name, NodeRole.AGGREGATION),
            Node("service", NodeRole.SERVICE),
        ],
        [Link(long_name, "access", "service")],
    )

    assert link_failure_scenarios(topology)[0].id == f"link:{long_name}"
    assert transport_node_failure_scenarios(topology)[0].id == f"node:{long_name}"


def test_fault_scenario_normalizes_sets_and_rejects_untyped_kind() -> None:
    mutable_links = {"access1--agg1"}
    scenario = FaultScenario(
        id="link:access1--agg1",
        kind=FaultKind.LINK,
        excluded_links=mutable_links,  # type: ignore[arg-type]
    )
    mutable_links.add("access1--agg2")

    assert scenario.excluded_links == frozenset({"access1--agg1"})
    with pytest.raises(ValueError, match="fault kind"):
        FaultScenario(
            id="bad-kind",
            kind="garbage",  # type: ignore[arg-type]
            excluded_links=frozenset({"access1--agg1"}),
        )
