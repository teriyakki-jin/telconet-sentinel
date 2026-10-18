from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from .audit import audit_fault_scenarios
from .failure_domain import topology_fingerprint
from .fault import link_failure_scenarios, transport_node_failure_scenarios
from .intent import NetworkIntent
from .models import NodeRole
from .topology import Topology
from .yaml_loader import UniqueKeySafeLoader

_MAX_YAML_BYTES = 1024 * 1024
_MAX_JSON_BYTES = 5 * 1024 * 1024
_MAX_YAML_EVENTS = 10_000
_MAX_YAML_ALIASES = 64
_MAX_YAML_DEPTH = 32
_MAX_TOPOLOGY_NODES = 64
_MAX_TOPOLOGY_LINKS = 128
_MAX_ACCESS_NODES = 16
_MAX_PREFIXES = 128
_MAX_FAULT_SCENARIOS = 64
_NODE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_INTERFACE_PATTERN = re.compile(r"^eth[0-9]{1,2}$")
_SCENARIO_PATTERN = re.compile(r"^(?:link|node):[a-z][a-z0-9-]{0,127}$")


class InjectionInterface(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    interface: str = Field(pattern=r"^eth[0-9]{1,2}$")


class RuntimePathExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_node: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    baseline_metric: StrictInt = Field(ge=0)
    fault_metric: StrictInt | None = Field(default=None, ge=0)


class BaselineAdjacencyExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    router: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    full_neighbors: StrictInt = Field(ge=1, le=16)


class RuntimeFaultPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^(?:link|node):[a-z][a-z0-9-]{0,127}$")
    kind: Literal["link", "node"]
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    injections: tuple[InjectionInterface, ...] = Field(min_length=1, max_length=16)
    paths: tuple[RuntimePathExpectation, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_unique_items(self) -> RuntimeFaultPlan:
        injection_keys = [(item.node, item.interface) for item in self.injections]
        if len(injection_keys) != len(set(injection_keys)):
            raise ValueError("injection interfaces must be unique")
        access_nodes = [path.access_node for path in self.paths]
        if len(access_nodes) != len(set(access_nodes)):
            raise ValueError("plan access nodes must be unique")
        return self


class RuntimeN1Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt
    source: Literal["intent_runtime_n1_plan"]
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lab_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_prefix: str = Field(min_length=1, max_length=64)
    baseline_adjacencies: tuple[BaselineAdjacencyExpectation, ...] = Field(
        min_length=1, max_length=64
    )
    scenarios: tuple[RuntimeFaultPlan, ...] = Field(min_length=1, max_length=64)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported runtime N-1 plan version")
        return value

    @field_validator("service_prefix")
    @classmethod
    def validate_service_prefix(cls, value: str) -> str:
        try:
            network = ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError("service prefix must be a canonical IP network") from exc
        if not isinstance(network, IPv4Network) or network.prefixlen != 32:
            raise ValueError("runtime probe requires an IPv4 /32 service prefix")
        return str(network)

    @model_validator(mode="after")
    def validate_unique_scenarios(self) -> RuntimeN1Plan:
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("plan scenario ids must be unique")
        routers = [item.router for item in self.baseline_adjacencies]
        if len(routers) != len(set(routers)):
            raise ValueError("baseline adjacency routers must be unique")
        return self


class RuntimePathObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_node: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    fault_metric: StrictInt | None = Field(default=None, ge=0)
    fault_reachable: StrictBool
    restored_metric: StrictInt | None = Field(default=None, ge=0)
    restored_reachable: StrictBool


class RuntimeScenarioObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^(?:link|node):[a-z][a-z0-9-]{0,127}$")
    paths: tuple[RuntimePathObservation, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_unique_paths(self) -> RuntimeScenarioObservation:
        access_nodes = [path.access_node for path in self.paths]
        if len(access_nodes) != len(set(access_nodes)):
            raise ValueError("observation access nodes must be unique")
        return self


class RuntimeN1Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt
    source: Literal["containerlab_frr_observation"]
    scenarios: tuple[RuntimeScenarioObservation, ...] = Field(
        min_length=1, max_length=64
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported runtime N-1 observation version")
        return value

    @model_validator(mode="after")
    def validate_unique_scenarios(self) -> RuntimeN1Observation:
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("observation scenario ids must be unique")
        return self


class RuntimeN1PublishedScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^(?:link|node):[a-z][a-z0-9-]{0,127}$")
    kind: Literal["link", "node"]
    passed: StrictBool

    @model_validator(mode="after")
    def validate_kind_matches_id(self) -> RuntimeN1PublishedScenario:
        if not self.scenario_id.startswith(f"{self.kind}:"):
            raise ValueError("scenario kind must match its id")
        return self


class RuntimeN1PublishedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt
    source: Literal["github_actions_attested_runtime_n1"]
    source_repository: Literal["teriyakki-jin/telconet-sentinel"]
    source_ref: Literal["refs/heads/main"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    workflow_run_id: StrictInt = Field(ge=1)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lab_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed_all: StrictBool
    total_checks: StrictInt = Field(ge=1, le=1024)
    passed_checks: StrictInt = Field(ge=0, le=1024)
    failed_checks: StrictInt = Field(ge=0, le=1024)
    scenarios: tuple[RuntimeN1PublishedScenario, ...] = Field(
        min_length=1, max_length=64
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported published runtime N-1 evidence version")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> RuntimeN1PublishedEvidence:
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("published runtime scenario ids must be unique")
        if self.total_checks != self.passed_checks + self.failed_checks:
            raise ValueError("published runtime check totals are inconsistent")
        if self.total_checks != len(self.scenarios) * 8:
            raise ValueError("published runtime evidence requires eight checks per scenario")
        every_scenario_passed = all(scenario.passed for scenario in self.scenarios)
        if (self.failed_checks == 0) != every_scenario_passed:
            raise ValueError("published runtime scenario and check results conflict")
        expected_pass = self.failed_checks == 0
        if self.passed_all != expected_pass:
            raise ValueError("published runtime passed_all conflicts with results")
        return self


def _ensure_bounded_file(path: Path, label: str, limit: int) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return raw


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> Any:
    raw = _ensure_bounded_file(path, label, _MAX_JSON_BYTES)
    return json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def load_runtime_n1_plan(path: Path) -> RuntimeN1Plan:
    return RuntimeN1Plan.model_validate(_load_json(path, "runtime N-1 plan"))


def load_runtime_n1_observation(path: Path) -> RuntimeN1Observation:
    return RuntimeN1Observation.model_validate(
        _load_json(path, "runtime N-1 observation")
    )


def load_runtime_n1_published_evidence(path: Path) -> RuntimeN1PublishedEvidence:
    return RuntimeN1PublishedEvidence.model_validate(
        _load_json(path, "published runtime N-1 evidence")
    )


def validate_runtime_n1_published_topology(
    evidence: RuntimeN1PublishedEvidence,
    topology: Topology,
    runtime_configuration_sha256: str,
) -> None:
    if topology_fingerprint(topology) != evidence.topology_sha256:
        raise ValueError("published runtime N-1 evidence does not match candidate intent")
    if runtime_configuration_sha256 != evidence.runtime_configuration_sha256:
        raise ValueError(
            "published runtime N-1 runtime configuration does not match current files"
        )
    expected_scenario_ids = {
        scenario.id
        for scenario in (
            *link_failure_scenarios(topology),
            *transport_node_failure_scenarios(topology),
        )
    }
    published_scenario_ids = {
        scenario.scenario_id for scenario in evidence.scenarios
    }
    if published_scenario_ids != expected_scenario_ids:
        raise ValueError(
            "published runtime N-1 scenario matrix does not match candidate intent"
        )


def render_runtime_n1_metrics(evidence: RuntimeN1PublishedEvidence) -> str:
    kind_counts = {
        kind: sum(scenario.kind == kind for scenario in evidence.scenarios)
        for kind in ("link", "node")
    }
    lines = [
        "# HELP telconet_runtime_n1_pass Whether the attested runtime N-1 matrix passed.",
        "# TYPE telconet_runtime_n1_pass gauge",
        f"telconet_runtime_n1_pass {1 if evidence.passed_all else 0}",
        "# HELP telconet_runtime_n1_scenarios_total Runtime N-1 scenarios by kind.",
        "# TYPE telconet_runtime_n1_scenarios_total gauge",
        f'telconet_runtime_n1_scenarios_total{{kind="link"}} {kind_counts["link"]}',
        f'telconet_runtime_n1_scenarios_total{{kind="node"}} {kind_counts["node"]}',
        "# HELP telconet_runtime_n1_checks_total Runtime N-1 checks by result.",
        "# TYPE telconet_runtime_n1_checks_total gauge",
        f'telconet_runtime_n1_checks_total{{result="passed"}} {evidence.passed_checks}',
        f'telconet_runtime_n1_checks_total{{result="failed"}} {evidence.failed_checks}',
        "# HELP telconet_runtime_n1_scenario_pass Whether one runtime scenario passed.",
        "# TYPE telconet_runtime_n1_scenario_pass gauge",
    ]
    lines.extend(
        "telconet_runtime_n1_scenario_pass"
        f'{{kind="{scenario.kind}",scenario_id="{scenario.scenario_id}"}} '
        f"{1 if scenario.passed else 0}"
        for scenario in evidence.scenarios
    )
    lines.extend(
        (
            "# HELP telconet_runtime_n1_evidence_info Attested runtime evidence identity.",
            "# TYPE telconet_runtime_n1_evidence_info gauge",
            "telconet_runtime_n1_evidence_info"
            f'{{source_commit="{evidence.source_commit}",'
            f'workflow_run="{evidence.workflow_run_id}"}} 1',
        )
    )
    return "\n".join(lines) + "\n"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    return cast(Sequence[object], value)


def _load_yaml_snapshot(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8") from exc
    alias_count = 0
    depth = 0
    start_events = (MappingStartEvent, SequenceStartEvent)
    end_events = (MappingEndEvent, SequenceEndEvent)
    for event_count, event in enumerate(
        yaml.parse(text, Loader=UniqueKeySafeLoader), start=1
    ):
        if event_count > _MAX_YAML_EVENTS:
            raise ValueError(f"{label} contains too many YAML events")
        if isinstance(event, AliasEvent):
            alias_count += 1
            if alias_count > _MAX_YAML_ALIASES:
                raise ValueError(f"{label} contains too many YAML aliases")
        if isinstance(event, start_events):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise ValueError(f"{label} exceeds the YAML nesting limit")
        elif isinstance(event, end_events):
            depth -= 1
    return yaml.load(text, Loader=UniqueKeySafeLoader)


def _validate_raw_topology_limits(document: Mapping[str, Any]) -> None:
    nodes = _mapping(document.get("nodes"), "candidate intent nodes")
    if not 1 <= len(nodes) <= _MAX_TOPOLOGY_NODES:
        raise ValueError(
            f"candidate intent must contain at most {_MAX_TOPOLOGY_NODES} nodes"
        )
    links = _sequence(document.get("links"), "candidate intent links")
    if not 1 <= len(links) <= _MAX_TOPOLOGY_LINKS:
        raise ValueError(
            f"candidate intent must contain 1 to {_MAX_TOPOLOGY_LINKS} links"
        )


def _parse_endpoint(value: object, nodes: set[str]) -> tuple[str, str]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError("containerlab endpoint must be node:interface")
    node, interface = value.split(":", 1)
    if node not in nodes or not _NODE_PATTERN.fullmatch(node):
        raise ValueError(f"containerlab endpoint uses an unknown node: {node}")
    if not _INTERFACE_PATTERN.fullmatch(interface):
        raise ValueError(f"unsupported containerlab interface: {interface}")
    return node, interface


def _load_lab_links(path: Path) -> tuple[bytes, dict[frozenset[str], dict[str, str]]]:
    raw = _ensure_bounded_file(path, "containerlab topology", _MAX_YAML_BYTES)
    document = _mapping(
        _load_yaml_snapshot(raw, "containerlab topology"),
        "containerlab document",
    )
    topology = _mapping(document.get("topology"), "containerlab topology")
    node_document = _mapping(topology.get("nodes"), "containerlab nodes")
    if not 1 <= len(node_document) <= 64:
        raise ValueError("containerlab nodes must contain 1 to 64 entries")
    nodes = set(node_document)
    if any(not isinstance(node, str) or not _NODE_PATTERN.fullmatch(node) for node in nodes):
        raise ValueError("containerlab node name is invalid")
    links = _sequence(topology.get("links"), "containerlab links")
    if not 1 <= len(links) <= 128:
        raise ValueError("containerlab links must contain 1 to 128 entries")

    pairs: dict[frozenset[str], dict[str, str]] = {}
    for index, link_value in enumerate(links):
        link = _mapping(link_value, f"containerlab link {index}")
        if set(link) != {"endpoints"}:
            raise ValueError("containerlab link must contain only endpoints")
        endpoint_values = _sequence(link["endpoints"], "containerlab endpoints")
        if len(endpoint_values) != 2:
            raise ValueError("containerlab link must contain exactly two endpoints")
        endpoints = [_parse_endpoint(value, nodes) for value in endpoint_values]
        pair = frozenset(node for node, _ in endpoints)
        if len(pair) != 2:
            raise ValueError("containerlab self-links are not supported")
        if pair in pairs:
            raise ValueError("parallel containerlab links are not supported")
        pairs[pair] = {node: interface for node, interface in endpoints}
    return raw.replace(b"\r\n", b"\n"), pairs


def build_runtime_n1_plan(intent_path: Path, lab_path: Path) -> RuntimeN1Plan:
    intent_raw = _ensure_bounded_file(intent_path, "candidate intent", _MAX_YAML_BYTES)
    intent_document = _mapping(
        _load_yaml_snapshot(intent_raw, "candidate intent"),
        "candidate intent",
    )
    _validate_raw_topology_limits(intent_document)
    topology = NetworkIntent.model_validate(intent_document).to_topology()
    lab_raw, lab_links = _load_lab_links(lab_path)
    access_nodes = [node for node in topology.nodes if node.role is NodeRole.ACCESS]
    if not 1 <= len(access_nodes) <= _MAX_ACCESS_NODES:
        raise ValueError(
            f"candidate intent must contain 1 to {_MAX_ACCESS_NODES} access nodes"
        )
    prefix_count = sum(len(node.prefixes) for node in topology.nodes)
    if prefix_count > _MAX_PREFIXES:
        raise ValueError(
            f"candidate intent must contain at most {_MAX_PREFIXES} prefixes"
        )
    services = [node for node in topology.nodes if node.role is NodeRole.SERVICE]
    service_prefixes = [prefix for node in services for prefix in node.prefixes]
    if len(service_prefixes) != 1:
        raise ValueError("runtime N-1 plan requires exactly one service prefix")

    faults = (
        *link_failure_scenarios(topology),
        *transport_node_failure_scenarios(topology),
    )
    if len(faults) > _MAX_FAULT_SCENARIOS:
        raise ValueError(
            f"runtime N-1 plan supports at most {_MAX_FAULT_SCENARIOS} scenarios"
        )
    audit = audit_fault_scenarios(topology, faults)
    degrees = {node.name: 0 for node in topology.nodes}
    for link in topology.links:
        degrees[link.endpoint_a] += 1
        degrees[link.endpoint_b] += 1
    if any(count == 0 for count in degrees.values()):
        raise ValueError("runtime N-1 plan requires every intent node to have a neighbor")
    scenarios: list[RuntimeFaultPlan] = []
    for result in audit.scenarios:
        fault = result.fault
        if fault.excluded_links:
            component_id = next(iter(fault.excluded_links))
            link = topology.link(component_id)
            pair = frozenset((link.endpoint_a, link.endpoint_b))
            interfaces = lab_links.get(pair)
            if interfaces is None:
                raise ValueError(f"intent link is missing from containerlab: {component_id}")
            injections = tuple(
                InjectionInterface(node=node, interface=interfaces[node])
                for node in (link.endpoint_a, link.endpoint_b)
            )
        else:
            component_id = next(iter(fault.excluded_nodes))
            injections = tuple(
                sorted(
                    (
                        InjectionInterface(node=component_id, interface=interfaces[component_id])
                        for interfaces in lab_links.values()
                        if component_id in interfaces
                    ),
                    key=lambda item: item.interface,
                )
            )
            if not injections:
                raise ValueError(
                    f"intent node has no containerlab interfaces: {component_id}"
                )
        scenarios.append(
            RuntimeFaultPlan(
                scenario_id=fault.id,
                kind=cast(Literal["link", "node"], fault.kind.value),
                component_id=component_id,
                injections=injections,
                paths=tuple(
                    RuntimePathExpectation(
                        access_node=path.access_node,
                        baseline_metric=path.baseline_cost,
                        fault_metric=path.post_fault_cost,
                    )
                    for path in result.paths
                ),
            )
        )
    return RuntimeN1Plan(
        schema_version=1,
        source="intent_runtime_n1_plan",
        topology_sha256=topology_fingerprint(topology),
        lab_sha256=hashlib.sha256(lab_raw).hexdigest(),
        service_prefix=service_prefixes[0],
        baseline_adjacencies=tuple(
            BaselineAdjacencyExpectation(router=node.name, full_neighbors=degrees[node.name])
            for node in topology.nodes
        ),
        scenarios=tuple(scenarios),
    )


def _canonical_sha256(model: BaseModel) -> str:
    document = model.model_dump(mode="json")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_runtime_n1_evidence(
    plan: RuntimeN1Plan,
    observation: RuntimeN1Observation,
) -> dict[str, object]:
    expected_ids = {scenario.scenario_id for scenario in plan.scenarios}
    observed_by_id = {
        scenario.scenario_id: scenario for scenario in observation.scenarios
    }
    if set(observed_by_id) != expected_ids:
        raise ValueError("runtime observation scenario set does not match plan")

    scenario_evidence: list[dict[str, object]] = []
    total = 0
    passed = 0
    for scenario in plan.scenarios:
        observed = observed_by_id[scenario.scenario_id]
        observed_paths = {path.access_node: path for path in observed.paths}
        expected_paths = {path.access_node: path for path in scenario.paths}
        if set(observed_paths) != set(expected_paths):
            raise ValueError(
                f"runtime observation access-node set does not match plan: "
                f"{scenario.scenario_id}"
            )
        checks: list[dict[str, object]] = []
        for access_node, expected in expected_paths.items():
            actual = observed_paths[access_node]
            comparisons = (
                ("fault_route_metric", expected.fault_metric, actual.fault_metric),
                (
                    "fault_reachability",
                    expected.fault_metric is not None,
                    actual.fault_reachable,
                ),
                ("restored_route_metric", expected.baseline_metric, actual.restored_metric),
                ("restored_reachability", True, actual.restored_reachable),
            )
            for check_name, expected_value, actual_value in comparisons:
                check_passed = expected_value == actual_value
                checks.append(
                    {
                        "check": check_name,
                        "access_node": access_node,
                        "expected": expected_value,
                        "actual": actual_value,
                        "passed": check_passed,
                    }
                )
                total += 1
                passed += int(check_passed)
        scenario_evidence.append(
            {
                "scenario_id": scenario.scenario_id,
                "kind": scenario.kind,
                "component_id": scenario.component_id,
                "passed": all(cast(bool, check["passed"]) for check in checks),
                "checks": checks,
            }
        )
    return {
        "schema_version": 1,
        "scope": "runtime_n1_model_conformance",
        "source": "containerlab_frr_observation",
        "measured": True,
        "plan_sha256": _canonical_sha256(plan),
        "observation_sha256": _canonical_sha256(observation),
        "topology_sha256": plan.topology_sha256,
        "lab_sha256": plan.lab_sha256,
        "passed_all": passed == total,
        "summary": {"total": total, "passed": passed, "failed": total - passed},
        "scenarios": scenario_evidence,
    }


def _walk(value: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        yield mapping
        for nested in mapping.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def extract_selected_route_metric(raw: str) -> int:
    document = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    selected = [
        values
        for values in _walk(document)
        if values.get("protocol") == "ospf"
        and values.get("selected") is True
        and values.get("installed") is True
        and isinstance(values.get("metric"), int)
        and not isinstance(values.get("metric"), bool)
    ]
    if len(selected) != 1:
        raise ValueError("route output must contain exactly one selected installed OSPF route")
    return cast(int, selected[0]["metric"])


def extract_full_neighbor_count(raw: str) -> int:
    document = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    root = _mapping(document, "OSPF neighbor output")
    neighbors = _mapping(root.get("neighbors"), "OSPF neighbors")
    count = 0
    for entries in neighbors.values():
        if not isinstance(entries, list):
            raise ValueError("OSPF neighbor entries must be lists")
        for entry in entries:
            values = _mapping(entry, "OSPF neighbor entry")
            if str(values.get("nbrState", "")).lower().startswith("full"):
                count += 1
    return count


def _write_json(path: Path, document: BaseModel | Mapping[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = document.model_dump(mode="json") if isinstance(document, BaseModel) else document
    content = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _refuse_input_overwrite(output: Path, inputs: Sequence[Path]) -> None:
    output_path = output.resolve(strict=False)
    if any(output_path == path.resolve(strict=False) for path in inputs):
        raise ValueError("output must not overwrite an input")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate runtime N-1 model conformance")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="Build a typed fault plan")
    plan.add_argument("--intent", type=Path, required=True)
    plan.add_argument("--lab", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)

    rows = commands.add_parser("rows", help="Render trusted plan rows for the runner")
    rows.add_argument("--plan", type=Path, required=True)

    settings = commands.add_parser(
        "settings", help="Render trusted service and baseline settings"
    )
    settings.add_argument("--plan", type=Path, required=True)

    adjacencies = commands.add_parser(
        "adjacencies", help="Render expected baseline adjacency counts"
    )
    adjacencies.add_argument("--plan", type=Path, required=True)

    metric = commands.add_parser("metric", help="Extract one selected OSPF metric")
    metric.add_argument("--route", type=Path, required=True)

    neighbor_count = commands.add_parser(
        "neighbor-count", help="Count Full OSPF adjacencies"
    )
    neighbor_count.add_argument("--ospf", type=Path, required=True)

    verify = commands.add_parser("verify", help="Compare runtime observation with plan")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--observation", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        _refuse_input_overwrite(args.output, (args.intent, args.lab))
        plan = build_runtime_n1_plan(args.intent, args.lab)
        _write_json(args.output, plan)
        print(f"runtime N-1 plan generated: {len(plan.scenarios)} scenarios")
        return 0
    if args.command == "rows":
        plan = load_runtime_n1_plan(args.plan)
        for scenario in plan.scenarios:
            paths = {path.access_node: path for path in scenario.paths}
            if set(paths) != {"access1", "access2"}:
                raise ValueError("runtime runner requires access1 and access2 paths")
            if paths["access1"].fault_metric is None or paths["access2"].fault_metric is None:
                raise ValueError("runtime runner supports only reachable N-1 scenarios")
            injections = ",".join(
                f"{item.node}:{item.interface}" for item in scenario.injections
            )
            print(
                "\t".join(
                    (
                        scenario.scenario_id,
                        scenario.kind,
                        scenario.component_id,
                        injections,
                        str(paths["access1"].fault_metric),
                        str(paths["access2"].fault_metric),
                    )
                )
            )
        return 0
    if args.command == "settings":
        plan = load_runtime_n1_plan(args.plan)
        baseline_by_access: dict[str, int] = {}
        for scenario in plan.scenarios:
            for path in scenario.paths:
                previous = baseline_by_access.setdefault(
                    path.access_node, path.baseline_metric
                )
                if previous != path.baseline_metric:
                    raise ValueError(
                        f"inconsistent baseline metric for {path.access_node}"
                    )
        if set(baseline_by_access) != {"access1", "access2"}:
            raise ValueError("runtime runner requires access1 and access2 baselines")
        service_address = str(ip_network(plan.service_prefix).network_address)
        print(
            "\t".join(
                (
                    plan.service_prefix,
                    service_address,
                    str(baseline_by_access["access1"]),
                    str(baseline_by_access["access2"]),
                )
            )
        )
        return 0
    if args.command == "adjacencies":
        plan = load_runtime_n1_plan(args.plan)
        for item in plan.baseline_adjacencies:
            print(f"{item.router}\t{item.full_neighbors}")
        return 0
    if args.command == "metric":
        raw = _ensure_bounded_file(args.route, "FRR route output", _MAX_JSON_BYTES)
        print(extract_selected_route_metric(raw.decode("utf-8")))
        return 0
    if args.command == "neighbor-count":
        raw = _ensure_bounded_file(args.ospf, "FRR OSPF output", _MAX_JSON_BYTES)
        print(extract_full_neighbor_count(raw.decode("utf-8")))
        return 0

    _refuse_input_overwrite(args.output, (args.plan, args.observation))
    plan = load_runtime_n1_plan(args.plan)
    observation = load_runtime_n1_observation(args.observation)
    evidence = build_runtime_n1_evidence(plan, observation)
    _write_json(args.output, evidence)
    if evidence["passed_all"]:
        summary = cast(Mapping[str, int], evidence["summary"])
        print(
            f"runtime N-1 conformance passed: {len(plan.scenarios)} scenarios, "
            f"{summary['passed']} checks"
        )
        return 0
    summary = cast(Mapping[str, int], evidence["summary"])
    print(f"runtime N-1 conformance failed: {summary['failed']} checks")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
