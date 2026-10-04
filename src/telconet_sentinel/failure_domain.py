from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .audit import AccessPathResult, audit_fault_scenarios
from .fault import FaultKind, FaultScenario
from .intent import DesignCatalog, DesignRole, load_intent
from .models import ServiceImpact
from .topology import Topology
from .yaml_loader import load_yaml

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,127}$")


class FailureDomainType(str, Enum):
    SHARED_CONDUIT = "shared_conduit"
    POWER_DOMAIN = "power_domain"
    SITE = "site"


class FailureDomainIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: FailureDomainType
    links: tuple[str, ...] = Field(default=(), max_length=32)
    nodes: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_components(self) -> FailureDomainIntent:
        if len(self.links) != len(set(self.links)) or len(self.nodes) != len(set(self.nodes)):
            raise ValueError("failure domain components must be unique")
        if len(self.links) + len(self.nodes) < 2:
            raise ValueError("failure domain requires at least two components")
        for component in (*self.links, *self.nodes):
            if not _IDENTIFIER_PATTERN.fullmatch(component):
                raise ValueError(f"invalid failure domain component: {component}")
        return self


class FailureDomainCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    design: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_domains: dict[str, FailureDomainIntent] = Field(min_length=1, max_length=64)

    @field_validator("version")
    @classmethod
    def validate_version(cls, version: int) -> int:
        if version != 1:
            raise ValueError("unsupported schema version")
        return version

    @field_validator("failure_domains")
    @classmethod
    def validate_domain_ids(
        cls,
        domains: dict[str, FailureDomainIntent],
    ) -> dict[str, FailureDomainIntent]:
        for domain_id in domains:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", domain_id):
                raise ValueError(f"invalid failure domain id: {domain_id}")
        return domains


@dataclass(frozen=True, slots=True)
class FailureDomainScenarioResult:
    domain_id: str
    domain_type: FailureDomainType
    excluded_links: tuple[str, ...]
    excluded_nodes: tuple[str, ...]
    service_impact: ServiceImpact
    affected_nodes: tuple[str, ...]
    affected_prefixes: tuple[str, ...]
    paths: tuple[AccessPathResult, ...]


@dataclass(frozen=True, slots=True)
class FailureDomainAudit:
    design: str
    topology_sha256: str
    scenarios: tuple[FailureDomainScenarioResult, ...]

    @property
    def total_scenarios(self) -> int:
        return len(self.scenarios)

    @property
    def passes_all(self) -> bool:
        return self.count(ServiceImpact.OUTAGE) == 0

    def count(self, impact: ServiceImpact) -> int:
        return sum(scenario.service_impact is impact for scenario in self.scenarios)


def load_failure_domains(path: Path) -> FailureDomainCatalog:
    document = load_yaml(path)
    return FailureDomainCatalog.model_validate(document)


def topology_fingerprint(topology: Topology) -> str:
    document = {
        "nodes": [
            {
                "name": node.name,
                "role": node.role.value,
                "prefixes": list(node.prefixes),
            }
            for node in topology.nodes
        ],
        "links": [
            {
                "id": link.id,
                "endpoints": sorted((link.endpoint_a, link.endpoint_b)),
                "cost": link.cost,
            }
            for link in topology.links
        ],
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def validate_failure_domain_design(
    catalog: FailureDomainCatalog,
    designs: DesignCatalog,
    design_directory: Path,
) -> None:
    design = designs.designs.get(catalog.design)
    if design is None or design.role is not DesignRole.CANDIDATE:
        raise ValueError(
            f"failure-domain catalog must reference a candidate design: {catalog.design}"
        )
    declared_topology = load_intent(design_directory / design.intent).to_topology()
    if topology_fingerprint(declared_topology) != catalog.topology_sha256:
        raise ValueError("failure-domain design catalog fingerprint mismatch")


def audit_failure_domains(
    topology: Topology,
    catalog: FailureDomainCatalog,
) -> FailureDomainAudit:
    if topology_fingerprint(topology) != catalog.topology_sha256:
        raise ValueError("failure-domain topology fingerprint mismatch")
    domain_items = tuple(sorted(catalog.failure_domains.items()))
    faults = tuple(
        FaultScenario(
            id=f"srlg:{domain_id}",
            kind=FaultKind.COMPOSITE,
            excluded_links=frozenset(domain.links),
            excluded_nodes=frozenset(domain.nodes),
        )
        for domain_id, domain in domain_items
    )
    audit = audit_fault_scenarios(topology, faults)
    scenarios = tuple(
        FailureDomainScenarioResult(
            domain_id=domain_id,
            domain_type=domain.type,
            excluded_links=tuple(sorted(domain.links)),
            excluded_nodes=tuple(sorted(domain.nodes)),
            service_impact=result.service_impact,
            affected_nodes=result.affected_nodes,
            affected_prefixes=result.affected_prefixes,
            paths=result.paths,
        )
        for (domain_id, domain), result in zip(domain_items, audit.scenarios, strict=True)
    )
    return FailureDomainAudit(
        design=catalog.design,
        topology_sha256=catalog.topology_sha256,
        scenarios=scenarios,
    )


def _escape_prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_failure_domain_metrics(audit: FailureDomainAudit) -> str:
    lines = [
        "# HELP telconet_srlg_design_pass Whether every declared shared-risk failure "
        "preserves service reachability.",
        "# TYPE telconet_srlg_design_pass gauge",
        f"telconet_srlg_design_pass {1 if audit.passes_all else 0}",
        "# HELP telconet_srlg_scenarios_total Declared shared-risk failures by impact.",
        "# TYPE telconet_srlg_scenarios_total gauge",
    ]
    for impact in ServiceImpact:
        lines.append(
            "telconet_srlg_scenarios_total"
            f'{{impact="{impact.value}"}} {audit.count(impact)}'
        )
    lines.extend(
        [
            "# HELP telconet_srlg_impact Impact classification for each declared "
            "shared-risk failure.",
            "# TYPE telconet_srlg_impact gauge",
        ]
    )
    for scenario in audit.scenarios:
        domain_id = _escape_prometheus_label(scenario.domain_id)
        lines.append(
            "telconet_srlg_impact"
            f'{{domain_id="{domain_id}",domain_type="{scenario.domain_type.value}",'
            f'impact="{scenario.service_impact.value}"}} 1'
        )
    return "\n".join(lines) + "\n"


def build_failure_domain_evidence(audit: FailureDomainAudit) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "shared_risk_failure_domain",
        "source": "deterministic_graph_analysis",
        "measured": False,
        "design": audit.design,
        "topology_sha256": audit.topology_sha256,
        "passes_all": audit.passes_all,
        "summary": {
            "total": audit.total_scenarios,
            "outage": audit.count(ServiceImpact.OUTAGE),
            "degraded": audit.count(ServiceImpact.DEGRADED),
            "redundancy_reduced": audit.count(ServiceImpact.REDUNDANCY_REDUCED),
        },
        "scenarios": [
            {
                "domain_id": scenario.domain_id,
                "domain_type": scenario.domain_type.value,
                "excluded_links": list(scenario.excluded_links),
                "excluded_nodes": list(scenario.excluded_nodes),
                "service_impact": scenario.service_impact.value,
                "affected_nodes": list(scenario.affected_nodes),
                "affected_prefixes": list(scenario.affected_prefixes),
                "paths": [
                    {
                        "access_node": path.access_node,
                        "baseline_cost": path.baseline_cost,
                        "post_fault_cost": path.post_fault_cost,
                    }
                    for path in scenario.paths
                ],
            }
            for scenario in audit.scenarios
        ],
    }
