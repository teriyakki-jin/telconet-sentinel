from __future__ import annotations

from collections.abc import Iterable

from .telemetry_config import TelemetryTopologyBinding, validate_telemetry_snapshot
from .telemetry_models import RouterTelemetry, TelemetrySnapshot, TelemetryState

_METRICS = (
    (
        "telconet_telemetry_snapshot_timestamp_seconds",
        "Unix timestamp of the latest validated telemetry snapshot.",
        "gauge",
    ),
    (
        "telconet_router_collection_success",
        "Whether telemetry collection succeeded for the router.",
        "gauge",
    ),
    (
        "telconet_interface_oper_up",
        "Interface operational state: up=1, down=0, unknown=-1.",
        "gauge",
    ),
    (
        "telconet_interface_receive_bytes_total",
        "Bytes received by the interface.",
        "counter",
    ),
    (
        "telconet_interface_transmit_bytes_total",
        "Bytes transmitted by the interface.",
        "counter",
    ),
    (
        "telconet_interface_receive_drops_total",
        "Packets dropped while receiving on the interface.",
        "counter",
    ),
    (
        "telconet_interface_transmit_drops_total",
        "Packets dropped while transmitting on the interface.",
        "counter",
    ),
    (
        "telconet_ospf_neighbor_full",
        "OSPF neighbor state: full=1, down=0, unknown=-1.",
        "gauge",
    ),
    (
        "telconet_bfd_peer_configured",
        "Whether BFD is configured for the peer.",
        "gauge",
    ),
    (
        "telconet_bfd_peer_up",
        "Configured BFD peer state: up=1, down=0, unknown or absent=-1.",
        "gauge",
    ),
    (
        "telconet_selected_route_present",
        "Whether the selected service route is present.",
        "gauge",
    ),
    (
        "telconet_selected_route_metric",
        "Metric of the selected service route.",
        "gauge",
    ),
    (
        "telconet_selected_route_egress",
        "Selected egress link for the service route.",
        "gauge",
    ),
    (
        "telconet_topology_node_info",
        "Static node and role information from validated topology binding.",
        "gauge",
    ),
    (
        "telconet_topology_link_oper_up",
        "Combined endpoint state: up=1, down=0, unknown=-1.",
        "gauge",
    ),
)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(items: Iterable[tuple[str, str]]) -> str:
    return "{" + ",".join(f'{name}="{_escape_label(value)}"' for name, value in items) + "}"


def _state_value(state: TelemetryState) -> int:
    if state is TelemetryState.UP:
        return 1
    if state is TelemetryState.DOWN:
        return 0
    return -1


def _router_map(
    snapshot: TelemetrySnapshot,
) -> dict[str, RouterTelemetry]:
    return {router.name: router for router in snapshot.routers}


def _interface_state(
    routers: dict[str, RouterTelemetry],
    node: str,
    interface_name: str,
) -> TelemetryState:
    router = routers.get(node)
    if router is None or not router.collection_success:
        return TelemetryState.UNKNOWN
    for interface in router.interfaces:
        if interface.name == interface_name:
            return interface.state
    return TelemetryState.UNKNOWN


def _link_state(source: TelemetryState, target: TelemetryState) -> int:
    if TelemetryState.DOWN in (source, target):
        return 0
    if source is TelemetryState.UP and target is TelemetryState.UP:
        return 1
    return -1


def render_telemetry_metrics(
    snapshot: TelemetrySnapshot | None,
    binding: TelemetryTopologyBinding,
) -> str:
    lines: list[str] = []
    for name, help_text, metric_type in _METRICS:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))

    for target in binding.targets:
        lines.append(
            "telconet_topology_node_info"
            + _labels((("node", target.node), ("role", target.role.value)))
            + " 1"
        )

    if snapshot is None:
        return "\n".join(lines) + "\n"

    validate_telemetry_snapshot(snapshot, binding)
    routers = _router_map(snapshot)
    lines.append(
        "telconet_telemetry_snapshot_timestamp_seconds "
        + f"{snapshot.observed_at.timestamp():.6f}".rstrip("0").rstrip(".")
    )

    for target in binding.targets:
        router = routers[target.node]
        node_labels = (("node", router.name),)
        lines.append(
            "telconet_router_collection_success"
            + _labels(node_labels)
            + f" {int(router.collection_success)}"
        )
        for interface in sorted(router.interfaces, key=lambda item: item.name):
            link_id = binding.interface_link_id(router.name, interface.name) or "unmanaged"
            labels = (
                ("node", router.name),
                ("interface", interface.name),
                ("link_id", link_id),
            )
            label_text = _labels(labels)
            lines.extend(
                (
                    f"telconet_interface_oper_up{label_text} {_state_value(interface.state)}",
                    f"telconet_interface_receive_bytes_total{label_text} {interface.rx_bytes}",
                    f"telconet_interface_transmit_bytes_total{label_text} {interface.tx_bytes}",
                    f"telconet_interface_receive_drops_total{label_text} {interface.rx_drops}",
                    f"telconet_interface_transmit_drops_total{label_text} {interface.tx_drops}",
                )
            )

        for neighbor in sorted(
            router.ospf_neighbors, key=lambda item: (item.neighbor, item.interface)
        ):
            labels = (
                ("node", router.name),
                ("neighbor", neighbor.neighbor),
                ("interface", neighbor.interface),
            )
            lines.append(
                "telconet_ospf_neighbor_full" + _labels(labels) + f" {_state_value(neighbor.state)}"
            )

        for peer in sorted(router.bfd_peers, key=lambda item: (item.peer, item.interface)):
            labels = (
                ("node", router.name),
                ("peer", peer.peer),
                ("interface", peer.interface),
            )
            label_text = _labels(labels)
            lines.extend(
                (
                    f"telconet_bfd_peer_configured{label_text} {int(peer.configured)}",
                    f"telconet_bfd_peer_up{label_text} {_state_value(peer.state)}",
                )
            )

        for route in sorted(router.routes, key=lambda item: item.prefix):
            route_labels = (("node", router.name), ("prefix", route.prefix))
            lines.append(
                "telconet_selected_route_present" + _labels(route_labels) + f" {int(route.present)}"
            )
            if route.present:
                assert route.metric is not None
                assert route.egress_interface is not None
                link_id = (
                    binding.interface_link_id(router.name, route.egress_interface) or "unmanaged"
                )
                lines.append(
                    "telconet_selected_route_metric" + _labels(route_labels) + f" {route.metric}"
                )
                lines.append(
                    "telconet_selected_route_egress"
                    + _labels((*route_labels, ("link_id", link_id)))
                    + " 1"
                )

    target_nodes = set(routers)
    for link in binding.links:
        if link.source not in target_nodes or link.target not in target_nodes:
            continue
        source_state = _interface_state(routers, link.source, link.source_interface)
        target_state = _interface_state(routers, link.target, link.target_interface)
        labels = (
            ("link_id", link.id),
            ("source", link.source),
            ("target", link.target),
        )
        lines.append(
            "telconet_topology_link_oper_up"
            + _labels(labels)
            + f" {_link_state(source_state, target_state)}"
        )

    return "\n".join(lines) + "\n"
