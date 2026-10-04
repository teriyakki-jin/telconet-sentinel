from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .models import NodeRole
from .topology import Topology


class FaultKind(str, Enum):
    LINK = "link"
    NODE = "node"
    COMPOSITE = "composite"


@dataclass(frozen=True, slots=True)
class FaultScenario:
    id: str
    kind: FaultKind
    excluded_links: frozenset[str] = field(default_factory=frozenset)
    excluded_nodes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FaultKind):
            raise ValueError("invalid fault kind")
        object.__setattr__(self, "excluded_links", frozenset(self.excluded_links))
        object.__setattr__(self, "excluded_nodes", frozenset(self.excluded_nodes))
        if not re.fullmatch(r"[a-z][a-z0-9:-]{0,255}", self.id):
            raise ValueError("invalid fault scenario id")
        component_count = len(self.excluded_links) + len(self.excluded_nodes)
        if component_count == 0:
            raise ValueError("fault scenario requires at least one component")
        if self.kind is FaultKind.LINK and (
            len(self.excluded_links) != 1 or self.excluded_nodes
        ):
            raise ValueError("link fault requires exactly one excluded link")
        if self.kind is FaultKind.NODE and (
            len(self.excluded_nodes) != 1 or self.excluded_links
        ):
            raise ValueError("node fault requires exactly one excluded node")
        if self.kind is FaultKind.COMPOSITE and component_count < 2:
            raise ValueError("composite fault requires multiple components")


def link_failure_scenarios(topology: Topology) -> tuple[FaultScenario, ...]:
    return tuple(
        FaultScenario(
            id=f"link:{link.id}",
            kind=FaultKind.LINK,
            excluded_links=frozenset({link.id}),
        )
        for link in topology.links
    )


def transport_node_failure_scenarios(
    topology: Topology,
) -> tuple[FaultScenario, ...]:
    transport_roles = {NodeRole.AGGREGATION, NodeRole.CORE}
    scenarios = tuple(
        FaultScenario(
            id=f"node:{node.name}",
            kind=FaultKind.NODE,
            excluded_nodes=frozenset({node.name}),
        )
        for node in topology.nodes
        if node.role in transport_roles
    )
    if not scenarios:
        raise ValueError("N-1 node audit requires at least one transport node")
    return scenarios
