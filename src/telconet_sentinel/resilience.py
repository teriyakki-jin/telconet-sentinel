from __future__ import annotations

from dataclasses import dataclass

from .impact import analyze_link_down
from .models import NetworkEvent, NodeRole, ServiceImpact
from .topology import Topology


@dataclass(frozen=True, slots=True)
class LinkFailureScenario:
    link_id: str
    endpoints: tuple[str, str]
    service_impact: ServiceImpact
    affected_nodes: tuple[str, ...]
    affected_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResilienceAudit:
    scenarios: tuple[LinkFailureScenario, ...]

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


@dataclass(frozen=True, slots=True)
class NodeFailureScenario:
    node_name: str
    role: NodeRole
    service_impact: ServiceImpact
    affected_nodes: tuple[str, ...]
    affected_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NodeResilienceAudit:
    scenarios: tuple[NodeFailureScenario, ...]

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


def audit_single_link_failures(topology: Topology) -> ResilienceAudit:
    access_nodes = tuple(
        node.name for node in topology.nodes if node.role is NodeRole.ACCESS
    )
    if not access_nodes:
        raise ValueError("N-1 audit requires at least one access node")
    service_nodes = {
        node.name for node in topology.nodes if node.role is NodeRole.SERVICE
    }
    if not service_nodes:
        raise ValueError("N-1 audit requires at least one service node")
    for access in access_nodes:
        if topology.shortest_distance(access, service_nodes) is None:
            raise ValueError(f"access node has no baseline service path: {access}")

    scenarios: list[LinkFailureScenario] = []
    for link in topology.links:
        analysis = analyze_link_down(topology, NetworkEvent(link.id))
        scenarios.append(
            LinkFailureScenario(
                link_id=link.id,
                endpoints=(link.endpoint_a, link.endpoint_b),
                service_impact=analysis.service_impact,
                affected_nodes=analysis.affected_nodes,
                affected_prefixes=analysis.affected_prefixes,
            )
        )
    return ResilienceAudit(tuple(scenarios))


def audit_single_node_failures(topology: Topology) -> NodeResilienceAudit:
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

    baseline_costs: dict[str, int] = {}
    for access in access_nodes:
        distance = topology.shortest_distance(access, service_nodes)
        if distance is None:
            raise ValueError(f"access node has no baseline service path: {access}")
        baseline_costs[access] = distance

    scenarios: list[NodeFailureScenario] = []
    transport_roles = {NodeRole.AGGREGATION, NodeRole.CORE}
    for failed_node in (
        node for node in topology.nodes if node.role in transport_roles
    ):
        unavailable: set[str] = set()
        degraded: set[str] = set()
        for access in access_nodes:
            after = topology.shortest_distance(
                access,
                service_nodes,
                excluded_node=failed_node.name,
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
        scenarios.append(
            NodeFailureScenario(
                node_name=failed_node.name,
                role=failed_node.role,
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
    return NodeResilienceAudit(tuple(scenarios))


def _escape_prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_resilience_metrics(audit: ResilienceAudit) -> str:
    lines = [
        "# HELP telconet_n1_design_pass Whether every simulated single-link failure "
        "preserves service reachability.",
        "# TYPE telconet_n1_design_pass gauge",
        f"telconet_n1_design_pass {1 if audit.passes_n_minus_one else 0}",
        "# HELP telconet_n1_scenarios_total Simulated single-link failures by impact.",
        "# TYPE telconet_n1_scenarios_total gauge",
    ]
    for impact in ServiceImpact:
        lines.append(
            "telconet_n1_scenarios_total"
            f'{{impact="{impact.value}"}} {audit.count(impact)}'
        )
    lines.extend(
        [
            "# HELP telconet_n1_link_impact Impact classification for each simulated link failure.",
            "# TYPE telconet_n1_link_impact gauge",
        ]
    )
    for scenario in audit.scenarios:
        link_id = _escape_prometheus_label(scenario.link_id)
        lines.append(
            "telconet_n1_link_impact"
            f'{{impact="{scenario.service_impact.value}",link_id="{link_id}"}} 1'
        )
    return "\n".join(lines) + "\n"


def render_candidate_resilience_metrics(audit: ResilienceAudit) -> str:
    return "\n".join(
        [
            "# HELP telconet_n1_candidate_design_pass Whether every modeled "
            "single-link failure preserves candidate service reachability.",
            "# TYPE telconet_n1_candidate_design_pass gauge",
            f"telconet_n1_candidate_design_pass {1 if audit.passes_n_minus_one else 0}",
            "# HELP telconet_n1_candidate_outages_total Modeled single-link "
            "failures causing candidate service outage.",
            "# TYPE telconet_n1_candidate_outages_total gauge",
            f"telconet_n1_candidate_outages_total {audit.count(ServiceImpact.OUTAGE)}",
            "",
        ]
    )


def render_node_resilience_metrics(
    audit: NodeResilienceAudit,
    *,
    design: str = "baseline",
) -> str:
    if design not in {"baseline", "candidate"}:
        raise ValueError(f"unsupported design: {design}")
    infix = "_candidate" if design == "candidate" else ""
    prefix = f"telconet_n1_node{infix}"
    return "\n".join(
        [
            f"# HELP {prefix}_design_pass Whether every modeled transport-node "
            f"failure preserves {design} service reachability.",
            f"# TYPE {prefix}_design_pass gauge",
            f"{prefix}_design_pass {1 if audit.passes_n_minus_one else 0}",
            f"# HELP {prefix}_outages_total Modeled transport-node failures "
            f"causing {design} service outage.",
            f"# TYPE {prefix}_outages_total gauge",
            f"{prefix}_outages_total {audit.count(ServiceImpact.OUTAGE)}",
            "",
        ]
    )
