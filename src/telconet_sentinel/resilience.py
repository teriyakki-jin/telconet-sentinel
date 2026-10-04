from __future__ import annotations

from dataclasses import dataclass

from .audit import audit_fault_scenarios
from .fault import link_failure_scenarios, transport_node_failure_scenarios
from .models import NodeRole, ServiceImpact
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
    audit = audit_fault_scenarios(topology, link_failure_scenarios(topology))
    scenarios: list[LinkFailureScenario] = []
    for result in audit.scenarios:
        link_id = next(iter(result.fault.excluded_links))
        link = topology.link(link_id)
        scenarios.append(
            LinkFailureScenario(
                link_id=link_id,
                endpoints=(link.endpoint_a, link.endpoint_b),
                service_impact=result.service_impact,
                affected_nodes=result.affected_nodes,
                affected_prefixes=result.affected_prefixes,
            )
        )
    return ResilienceAudit(tuple(scenarios))


def audit_single_node_failures(topology: Topology) -> NodeResilienceAudit:
    audit = audit_fault_scenarios(
        topology,
        transport_node_failure_scenarios(topology),
    )
    scenarios: list[NodeFailureScenario] = []
    for result in audit.scenarios:
        node_name = next(iter(result.fault.excluded_nodes))
        node = topology.node(node_name)
        scenarios.append(
            NodeFailureScenario(
                node_name=node_name,
                role=node.role,
                service_impact=result.service_impact,
                affected_nodes=result.affected_nodes,
                affected_prefixes=result.affected_prefixes,
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
