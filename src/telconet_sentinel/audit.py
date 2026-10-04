from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .fault import FaultScenario
from .models import NodeRole, ServiceImpact
from .topology import Topology


@dataclass(frozen=True, slots=True)
class FaultScenarioResult:
    fault: FaultScenario
    service_impact: ServiceImpact
    affected_nodes: tuple[str, ...]
    affected_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FaultAuditResult:
    scenarios: tuple[FaultScenarioResult, ...]

    @property
    def total_scenarios(self) -> int:
        return len(self.scenarios)

    @property
    def passes_n_minus_one(self) -> bool:
        return self.count(ServiceImpact.OUTAGE) == 0

    def count(self, impact: ServiceImpact) -> int:
        return sum(
            scenario.service_impact is impact
            for scenario in self.scenarios
        )


def audit_fault_scenarios(
    topology: Topology,
    scenarios: Iterable[FaultScenario],
) -> FaultAuditResult:
    scenario_items = tuple(scenarios)
    access_nodes = tuple(
        node.name for node in topology.nodes if node.role is NodeRole.ACCESS
    )
    service_nodes = {
        node.name for node in topology.nodes if node.role is NodeRole.SERVICE
    }
    if not access_nodes:
        raise ValueError("N-1 audit requires at least one access node")
    if not service_nodes:
        raise ValueError("N-1 audit requires at least one service node")
    if not scenario_items:
        raise ValueError("audit requires at least one fault scenario")
    scenario_ids = [scenario.id for scenario in scenario_items]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("duplicate fault scenario id")

    baseline_costs: dict[str, int] = {}
    for access in access_nodes:
        distance = topology.shortest_distance(access, service_nodes)
        if distance is None:
            raise ValueError(f"access node has no baseline service path: {access}")
        baseline_costs[access] = distance

    results: list[FaultScenarioResult] = []
    for fault in scenario_items:
        for link_id in fault.excluded_links:
            topology.link(link_id)
        for node_name in fault.excluded_nodes:
            topology.node(node_name)

        unavailable: set[str] = set()
        degraded: set[str] = set()
        for access in access_nodes:
            after = topology.shortest_distance_excluding(
                access,
                service_nodes,
                excluded_links=fault.excluded_links,
                excluded_nodes=fault.excluded_nodes,
            )
            if after is None:
                unavailable.add(access)
            elif after > baseline_costs[access]:
                degraded.add(access)

        affected_access = unavailable | degraded
        if unavailable:
            impact = ServiceImpact.OUTAGE
        elif degraded:
            impact = ServiceImpact.DEGRADED
        else:
            impact = ServiceImpact.REDUNDANCY_REDUCED
        affected_nodes = tuple(sorted(affected_access))
        results.append(
            FaultScenarioResult(
                fault=fault,
                service_impact=impact,
                affected_nodes=affected_nodes,
                affected_prefixes=tuple(
                    sorted(
                        prefix
                        for node_name in affected_nodes
                        for prefix in topology.node(node_name).prefixes
                    )
                ),
            )
        )
    return FaultAuditResult(tuple(results))
