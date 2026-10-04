from __future__ import annotations

import ipaddress
import re
from enum import Enum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from .models import Link, Node, NodeRole
from .topology import Topology
from .yaml_loader import load_yaml

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,127}$")


class DesignRole(str, Enum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class DesignCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*\.yml$")
    role: DesignRole
    based_on: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_relationship(self) -> DesignCatalogEntry:
        if self.role is DesignRole.CANDIDATE and self.based_on is None:
            raise ValueError("candidate design must define based_on")
        if self.role is DesignRole.BASELINE and self.based_on is not None:
            raise ValueError("baseline design cannot define based_on")
        return self


class DesignCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    designs: dict[str, DesignCatalogEntry] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def validate_version(cls, version: int) -> int:
        if version != 1:
            raise ValueError("unsupported schema version")
        return version

    @model_validator(mode="after")
    def validate_relationships(self) -> DesignCatalog:
        baseline_ids = {
            design_id
            for design_id, design in self.designs.items()
            if design.role is DesignRole.BASELINE
        }
        if len(baseline_ids) != 1:
            raise ValueError("design catalog must define exactly one baseline")
        for design_id, design in self.designs.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", design_id):
                raise ValueError(f"invalid design id: {design_id}")
            if (
                design.role is DesignRole.CANDIDATE
                and design.based_on not in baseline_ids
            ):
                raise ValueError(
                    f"candidate {design_id} references unknown baseline: "
                    f"{design.based_on}"
                )
        intents = [design.intent for design in self.designs.values()]
        if len(intents) != len(set(intents)):
            raise ValueError("design intent paths must be unique")
        return self

    def load_topologies(self, directory: Path) -> dict[str, Topology]:
        return {
            design_id: load_intent(directory / design.intent).to_topology()
            for design_id, design in self.designs.items()
        }


class NodeIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: NodeRole
    prefixes: tuple[str, ...] = ()

    @field_validator("prefixes")
    @classmethod
    def validate_prefixes(cls, prefixes: tuple[str, ...]) -> tuple[str, ...]:
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("node prefixes must be unique")
        for prefix in prefixes:
            try:
                ipaddress.ip_network(prefix, strict=True)
            except ValueError as exc:
                raise ValueError(f"invalid canonical prefix: {prefix}") from exc
        return prefixes


class LinkIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    endpoints: tuple[str, str]
    cost: StrictInt = Field(default=1, gt=0)

    @field_validator("endpoints")
    @classmethod
    def validate_endpoint_names(cls, endpoints: tuple[str, str]) -> tuple[str, str]:
        for endpoint in endpoints:
            if not _IDENTIFIER_PATTERN.fullmatch(endpoint):
                raise ValueError(f"invalid endpoint name: {endpoint}")
        return endpoints


class NetworkIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    nodes: dict[str, NodeIntent] = Field(min_length=1)
    links: tuple[LinkIntent, ...] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def validate_version(cls, version: int) -> int:
        if version != 1:
            raise ValueError("unsupported schema version")
        return version

    @model_validator(mode="after")
    def validate_graph_references(self) -> NetworkIntent:
        for name in self.nodes:
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                raise ValueError(f"invalid node name: {name}")

        link_ids = [link.id for link in self.links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("duplicate link id")

        known_nodes = set(self.nodes)
        for link in self.links:
            if link.endpoints[0] == link.endpoints[1]:
                raise ValueError(f"self link is not allowed: {link.id}")
            unknown = set(link.endpoints) - known_nodes
            if unknown:
                raise ValueError(
                    f"unknown endpoint in {link.id}: {', '.join(sorted(unknown))}"
                )

        prefix_owners: dict[str, str] = {}
        for name, node in self.nodes.items():
            for prefix in node.prefixes:
                owner = prefix_owners.get(prefix)
                if owner is not None:
                    raise ValueError(
                        f"duplicate prefix {prefix}: {owner}, {name}"
                    )
                prefix_owners[prefix] = name
        return self

    def to_topology(self) -> Topology:
        return Topology(
            (
                Node(name=name, role=node.role, prefixes=node.prefixes)
                for name, node in self.nodes.items()
            ),
            (
                Link(
                    id=link.id,
                    endpoint_a=link.endpoints[0],
                    endpoint_b=link.endpoints[1],
                    cost=link.cost,
                )
                for link in self.links
            ),
        )


def load_intent(path: Path) -> NetworkIntent:
    document = load_yaml(path)
    return NetworkIntent.model_validate(document)


def load_design_catalog(path: Path) -> DesignCatalog:
    document = load_yaml(path)
    return DesignCatalog.model_validate(document)
