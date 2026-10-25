from __future__ import annotations

import ipaddress
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from .intent import load_intent
from .models import NodeRole
from .telemetry_models import BASELINE_ROUTERS, TelemetrySnapshot
from .yaml_loader import load_yaml

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
TELEMETRY_MAX_FUTURE_SKEW_SECONDS = 60


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TelemetryTarget(_ConfigModel):
    node: Identifier
    container: Identifier


class TelemetryConfig(_ConfigModel):
    version: StrictInt
    poll_interval_seconds: StrictInt = Field(ge=1, le=60)
    service_prefix: str = Field(min_length=3, max_length=64)
    routers: tuple[TelemetryTarget, ...]

    @field_validator("version")
    @classmethod
    def validate_version(cls, version: int) -> int:
        if version != 1:
            raise ValueError("unsupported telemetry configuration version")
        return version

    @field_validator("service_prefix")
    @classmethod
    def validate_service_prefix(cls, prefix: str) -> str:
        try:
            parsed = ipaddress.ip_network(prefix, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid canonical service prefix: {prefix}") from exc
        if str(parsed) != prefix:
            raise ValueError(f"invalid canonical service prefix: {prefix}")
        return prefix

    @model_validator(mode="after")
    def validate_targets(self) -> TelemetryConfig:
        nodes = [target.node for target in self.routers]
        containers = [target.container for target in self.routers]
        if len(nodes) != len(set(nodes)) or len(containers) != len(set(containers)):
            raise ValueError("telemetry router nodes and containers must be unique")
        if set(nodes) != set(BASELINE_ROUTERS):
            raise ValueError("telemetry config must contain exactly the baseline routers")
        return self


class BoundTelemetryPeer(_ConfigModel):
    peer: Identifier
    interface: Identifier


class BoundTelemetryTarget(_ConfigModel):
    node: Identifier
    container: Identifier
    role: NodeRole
    interfaces: tuple[Identifier, ...] = Field(min_length=1, max_length=64)
    protocol_peers: tuple[BoundTelemetryPeer, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_inventory(self) -> BoundTelemetryTarget:
        if len(self.interfaces) != len(set(self.interfaces)):
            raise ValueError("bound target interfaces must be unique")
        peer_keys = [(peer.peer, peer.interface) for peer in self.protocol_peers]
        if len(peer_keys) != len(set(peer_keys)):
            raise ValueError("bound target protocol peers must be unique")
        unknown_interfaces = {peer.interface for peer in self.protocol_peers} - set(self.interfaces)
        if unknown_interfaces:
            raise ValueError("bound protocol peer references unknown interface")
        return self


class TelemetryLink(_ConfigModel):
    id: Identifier
    source: Identifier
    source_interface: Identifier
    target: Identifier
    target_interface: Identifier


class TelemetryTopologyBinding(_ConfigModel):
    config: TelemetryConfig
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[BoundTelemetryTarget, ...] = Field(min_length=6, max_length=6)
    links: tuple[TelemetryLink, ...] = Field(min_length=1, max_length=64)

    @property
    def roles(self) -> dict[str, str]:
        return {target.node: target.role.value for target in self.targets}

    def target(self, node: str) -> BoundTelemetryTarget:
        for target in self.targets:
            if target.node == node:
                return target
        raise KeyError(f"unknown telemetry target: {node}")

    def interface_link_id(self, node: str, interface: str) -> str | None:
        for link in self.links:
            if link.source == node and link.source_interface == interface:
                return link.id
            if link.target == node and link.target_interface == interface:
                return link.id
        return None


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} keys must be strings")
    return value


def _sequence(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return value


def _parse_endpoint(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError(f"invalid containerlab endpoint: {value!r}")
    node, interface = value.split(":", maxsplit=1)
    if not node or not interface:
        raise ValueError(f"invalid containerlab endpoint: {value!r}")
    return node, interface


def _load_clab_topology(
    path: Path,
) -> tuple[str, set[str], dict[frozenset[str], tuple[tuple[str, str], tuple[str, str]]]]:
    document = _mapping(load_yaml(path), "containerlab document")
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("containerlab name must be a non-empty string")
    topology = _mapping(document.get("topology"), "containerlab topology")
    nodes = set(_mapping(topology.get("nodes"), "containerlab nodes"))
    links_by_pair: dict[frozenset[str], tuple[tuple[str, str], tuple[str, str]]] = {}
    used_interfaces: set[tuple[str, str]] = set()
    for index, raw_link in enumerate(
        _sequence(topology.get("links"), "containerlab links"), start=1
    ):
        link = _mapping(raw_link, f"containerlab link {index}")
        endpoints = _sequence(link.get("endpoints"), f"containerlab link {index} endpoints")
        if len(endpoints) != 2:
            raise ValueError("containerlab links must contain exactly two endpoints")
        parsed = (_parse_endpoint(endpoints[0]), _parse_endpoint(endpoints[1]))
        for endpoint in parsed:
            if endpoint[0] not in nodes:
                raise ValueError(f"containerlab link references unknown node: {endpoint[0]}")
            if endpoint in used_interfaces:
                raise ValueError(
                    f"containerlab interface is used by multiple links: {endpoint[0]}:{endpoint[1]}"
                )
            used_interfaces.add(endpoint)
        pair = frozenset((parsed[0][0], parsed[1][0]))
        if len(pair) != 2 or pair in links_by_pair:
            raise ValueError("containerlab node pairs must be unique and non-reflexive")
        links_by_pair[pair] = parsed
    return name, nodes, links_by_pair


def _fingerprint(paths: tuple[Path, Path, Path]) -> str:
    labels = (b"telemetry-targets.yml", b"intent.yml", b"telconet.clab.yml")
    components: list[bytes] = []
    for label, path in zip(labels, paths, strict=True):
        content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        components.append(label + b"\0" + sha256(content).hexdigest().encode("ascii") + b"\n")
    return sha256(b"".join(components)).hexdigest()


def load_telemetry_binding(
    config_path: Path,
    intent_path: Path,
    clab_path: Path,
) -> TelemetryTopologyBinding:
    config = TelemetryConfig.model_validate(load_yaml(config_path))
    intent = load_intent(intent_path)
    clab_name, clab_nodes, clab_links = _load_clab_topology(clab_path)

    target_nodes = {target.node for target in config.routers}
    missing_from_intent = target_nodes - set(intent.nodes)
    if missing_from_intent:
        raise ValueError(
            "telemetry targets missing from intent: " + ", ".join(sorted(missing_from_intent))
        )
    missing_from_clab = target_nodes - clab_nodes
    if missing_from_clab:
        raise ValueError(
            "telemetry targets missing from containerlab: " + ", ".join(sorted(missing_from_clab))
        )

    expected_containers = {node: f"clab-{clab_name}-{node}" for node in target_nodes}
    for target in config.routers:
        if target.container != expected_containers[target.node]:
            raise ValueError(
                f"telemetry container name for {target.node} must be "
                f"{expected_containers[target.node]}"
            )

    service_owners = {
        node_name
        for node_name, node in intent.nodes.items()
        if config.service_prefix in node.prefixes
    }
    if service_owners != {"service-host"}:
        raise ValueError("telemetry service_prefix must be owned by service-host in network intent")

    intent_pairs = {frozenset(link.endpoints) for link in intent.links}
    intent_nodes = set(intent.nodes)
    managed_clab_pairs = {pair for pair in clab_links if pair.issubset(intent_nodes)}
    undeclared_pairs = managed_clab_pairs - intent_pairs
    if undeclared_pairs:
        rendered = ", ".join(
            "--".join(sorted(pair))
            for pair in sorted(undeclared_pairs, key=lambda item: tuple(sorted(item)))
        )
        raise ValueError(f"containerlab managed link not declared in intent: {rendered}")

    interfaces_by_node: dict[str, set[str]] = defaultdict(set)
    for clab_endpoints in clab_links.values():
        for node, interface in clab_endpoints:
            if node in target_nodes:
                interfaces_by_node[node].add(interface)

    links: list[TelemetryLink] = []
    for intent_link in intent.links:
        pair = frozenset(intent_link.endpoints)
        matched_endpoints = clab_links.get(pair)
        if matched_endpoints is None:
            raise ValueError(f"intent link missing from containerlab: {intent_link.id}")
        by_node = {node: interface for node, interface in matched_endpoints}
        source_node, target_node = intent_link.endpoints
        links.append(
            TelemetryLink(
                id=intent_link.id,
                source=source_node,
                source_interface=by_node[source_node],
                target=target_node,
                target_interface=by_node[target_node],
            )
        )

    protocol_peers_by_node: dict[str, list[BoundTelemetryPeer]] = defaultdict(list)
    for link in links:
        if link.source in target_nodes and link.target in target_nodes:
            protocol_peers_by_node[link.source].append(
                BoundTelemetryPeer(peer=link.target, interface=link.source_interface)
            )
            protocol_peers_by_node[link.target].append(
                BoundTelemetryPeer(peer=link.source, interface=link.target_interface)
            )

    targets = tuple(
        BoundTelemetryTarget(
            node=config_target.node,
            container=config_target.container,
            role=intent.nodes[config_target.node].role,
            interfaces=tuple(sorted(interfaces_by_node[config_target.node])),
            protocol_peers=tuple(
                sorted(
                    protocol_peers_by_node[config_target.node],
                    key=lambda peer: (peer.interface, peer.peer),
                )
            ),
        )
        for config_target in config.routers
    )
    return TelemetryTopologyBinding(
        config=config,
        fingerprint=_fingerprint((config_path, intent_path, clab_path)),
        targets=targets,
        links=tuple(links),
    )


def validate_telemetry_snapshot(
    snapshot: TelemetrySnapshot,
    binding: TelemetryTopologyBinding,
) -> None:
    if snapshot.topology_fingerprint != binding.fingerprint:
        raise ValueError("telemetry snapshot topology fingerprint does not match binding")
    targets = {target.node: target for target in binding.targets}
    for router in snapshot.routers:
        allowed_interfaces = set(targets[router.name].interfaces)
        observed_interfaces = {interface.name for interface in router.interfaces}
        unexpected = observed_interfaces - allowed_interfaces
        if unexpected:
            raise ValueError(
                f"telemetry snapshot contains interfaces outside topology binding for "
                f"{router.name}: {', '.join(sorted(unexpected))}"
            )
        if router.collection_success and observed_interfaces != allowed_interfaces:
            raise ValueError(
                f"successful telemetry collection for {router.name} must contain the "
                "complete bound interface set"
            )
        unexpected_prefixes = {
            route.prefix for route in router.routes if route.prefix != binding.config.service_prefix
        }
        if unexpected_prefixes:
            raise ValueError("telemetry snapshot contains routes outside service prefix allowlist")
        if router.collection_success and len(router.routes) != 1:
            raise ValueError(
                f"successful telemetry collection for {router.name} must contain "
                "exactly one service route observation"
            )
        if router.collection_success:
            expected_peers = {
                (peer.peer, peer.interface) for peer in targets[router.name].protocol_peers
            }
            observed_ospf = {
                (neighbor.neighbor, neighbor.interface) for neighbor in router.ospf_neighbors
            }
            if observed_ospf != expected_peers:
                raise ValueError(
                    f"successful telemetry collection for {router.name} must contain "
                    "the complete topology-bound OSPF peer set"
                )
            observed_bfd = {(peer.peer, peer.interface) for peer in router.bfd_peers}
            if observed_bfd != expected_peers:
                raise ValueError(
                    f"successful telemetry collection for {router.name} must contain "
                    "the complete topology-bound BFD peer set"
                )


def validate_telemetry_ingest(
    snapshot: TelemetrySnapshot,
    binding: TelemetryTopologyBinding,
    *,
    now: datetime | None = None,
) -> None:
    server_now = now or datetime.now(timezone.utc)
    if server_now.tzinfo is None or server_now.utcoffset() is None:
        raise ValueError("telemetry validation clock must be timezone-aware")
    future_limit = server_now.astimezone(timezone.utc) + timedelta(
        seconds=TELEMETRY_MAX_FUTURE_SKEW_SECONDS
    )
    if snapshot.observed_at > future_limit:
        raise ValueError("telemetry snapshot exceeds allowed future skew")
    validate_telemetry_snapshot(snapshot, binding)
