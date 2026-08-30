from .models import ImpactAnalysis, NetworkEvent, NodeRole, ServiceImpact
from .topology import Topology


def analyze_link_down(topology: Topology, event: NetworkEvent) -> ImpactAnalysis:
    if event.event_type != "link_down":
        raise ValueError(f"unsupported event type: {event.event_type}")

    failed_link = topology.link(event.link_id)
    access_nodes = tuple(node.name for node in topology.nodes if node.role is NodeRole.ACCESS)
    service_nodes = {node.name for node in topology.nodes if node.role is NodeRole.SERVICE}
    if not service_nodes:
        raise ValueError("topology must define at least one service node")
    changed_costs: list[str] = []
    unavailable: set[str] = set()
    affected_access: set[str] = set()
    for access in access_nodes:
        before = topology.shortest_distance(access, service_nodes)
        after = topology.shortest_distance(access, service_nodes, excluded_link=failed_link.id)
        if before is None:
            raise ValueError(f"access node has no baseline service path: {access}")
        if after is None:
            unavailable.add(access)
            affected_access.add(access)
            changed_costs.append(f"shortest service path lost: {access} {before}->unreachable")
        elif after > before:
            affected_access.add(access)
            changed_costs.append(f"shortest service path cost changed: {access} {before}->{after}")

    if unavailable:
        impact = ServiceImpact.OUTAGE
        path_evidence = "no alternate service path"
    elif affected_access:
        impact = ServiceImpact.DEGRADED
        path_evidence = "alternate service path available"
    else:
        impact = ServiceImpact.REDUNDANCY_REDUCED
        path_evidence = "active shortest path unchanged"

    affected_nodes = tuple(sorted(affected_access))
    prefixes = tuple(
        sorted(
            prefix for node_name in affected_nodes for prefix in topology.node(node_name).prefixes
        )
    )

    return ImpactAnalysis(
        failed_component=failed_link.id,
        evidence=(
            f"link_down observed on {failed_link.id}",
            path_evidence,
            *changed_costs,
        ),
        affected_nodes=affected_nodes,
        affected_prefixes=prefixes,
        service_impact=impact,
    )
