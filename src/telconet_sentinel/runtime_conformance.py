from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from .configuration import configuration_fingerprint
from .intent import NetworkIntent, load_intent
from .models import NodeRole
from .yaml_loader import load_yaml

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_INTERFACE_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]{1,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
_MAX_COMPONENT_BYTES = 1024 * 1024
_MAX_DEFINITION_BYTES = 1024 * 1024


def _canonical_ipv4(value: str, label: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value}") from exc
    if address.version != 4 or str(address) != value:
        raise ValueError(f"invalid canonical {label}: {value}")
    return value


class OspfNeighborExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    router_id: str
    interface: str = Field(pattern=_INTERFACE_PATTERN.pattern)

    @field_validator("router_id")
    @classmethod
    def validate_router_id(cls, value: str) -> str:
        return _canonical_ipv4(value, "OSPF router ID")


class BfdExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    peer: str
    interface: str = Field(pattern=_INTERFACE_PATTERN.pattern)
    receive_interval_ms: StrictInt = Field(gt=0, le=60_000)
    transmit_interval_ms: StrictInt = Field(gt=0, le=60_000)
    detect_multiplier: StrictInt = Field(gt=0, le=255)
    detection_timeout_ms: StrictInt = Field(gt=0, le=60_000)

    @field_validator("peer")
    @classmethod
    def validate_peer(cls, value: str) -> str:
        return _canonical_ipv4(value, "BFD peer")


class RuntimeExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    router: str = Field(pattern=_IDENTIFIER_PATTERN.pattern)
    service_node: str = Field(pattern=_IDENTIFIER_PATTERN.pattern)
    route_prefix: str
    route_next_hop: str
    route_interface: str = Field(pattern=_INTERFACE_PATTERN.pattern)
    ospf_neighbors: tuple[OspfNeighborExpectation, ...] = Field(
        min_length=1,
        max_length=32,
    )
    bfd: BfdExpectation

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported runtime expectation version")
        return value

    @field_validator("route_prefix")
    @classmethod
    def validate_route_prefix(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid route prefix: {value}") from exc
        if network.version != 4 or str(network) != value:
            raise ValueError(f"invalid canonical route prefix: {value}")
        return value

    @field_validator("route_next_hop")
    @classmethod
    def validate_route_next_hop(cls, value: str) -> str:
        return _canonical_ipv4(value, "route next hop")

    @model_validator(mode="after")
    def validate_neighbors(self) -> RuntimeExpectation:
        router_ids = [neighbor.router_id for neighbor in self.ospf_neighbors]
        if len(router_ids) != len(set(router_ids)):
            raise ValueError("OSPF neighbor router IDs must be unique")
        return self


class RuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt
    captured_at: datetime
    router: str = Field(pattern=_IDENTIFIER_PATTERN.pattern)
    ospf: dict[str, Any]
    route: dict[str, Any]
    bfd: dict[str, Any]

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported runtime snapshot version")
        return value


def load_runtime_expectation(path: Path) -> RuntimeExpectation:
    return RuntimeExpectation.model_validate(load_yaml(path))


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(Sequence[object], value)
    return ()


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _load_json_object(path: Path, label: str, max_bytes: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    value = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _load_snapshot(path: Path) -> dict[str, Any]:
    return _load_json_object(path, "runtime snapshot", _MAX_SNAPSHOT_BYTES)


def _ensure_bounded_file(path: Path, label: str) -> None:
    if path.stat().st_size > _MAX_DEFINITION_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_DEFINITION_BYTES} bytes")


def _check(check_id: str, expected: object, actual: object) -> dict[str, object]:
    return {
        "id": check_id,
        "expected": expected,
        "actual": actual,
        "passed": actual == expected,
    }


def _ospf_checks(
    snapshot: RuntimeSnapshot,
    expectation: RuntimeExpectation,
) -> list[dict[str, object]]:
    neighbors = _mapping(snapshot.ospf.get("neighbors"))
    expected_adjacencies = sorted(
        (
            {
                "router_id": neighbor.router_id,
                "interface": neighbor.interface,
                "state": "Full",
            }
            for neighbor in expectation.ospf_neighbors
        ),
        key=lambda item: (item["router_id"], item["interface"], item["state"]),
    )
    actual_adjacencies: list[dict[str, str]] = []
    for router_id, values in neighbors.items():
        for value in _sequence(values):
            entry = _mapping(value)
            actual_adjacencies.append(
                {
                    "router_id": str(router_id),
                    "interface": str(entry.get("ifaceName", "")).split(":", 1)[0],
                    "state": str(
                        entry.get("converged") or entry.get("nbrState", "")
                    ).split("/", 1)[0],
                }
            )
    actual_adjacencies.sort(
        key=lambda item: (item["router_id"], item["interface"], item["state"])
    )
    checks = [
        _check(
            "ospf.adjacencies",
            expected_adjacencies,
            actual_adjacencies,
        )
    ]
    for expected_neighbor in expectation.ospf_neighbors:
        entries = _sequence(neighbors.get(expected_neighbor.router_id))
        matching = None
        for value in entries:
            entry = _mapping(value)
            interface = str(entry.get("ifaceName", "")).split(":", 1)[0]
            state = str(entry.get("converged") or entry.get("nbrState", "")).split(
                "/", 1
            )[0]
            if interface == expected_neighbor.interface:
                matching = {"state": state, "interface": interface}
                break
        checks.append(
            _check(
                f"ospf.neighbor.{expected_neighbor.router_id}",
                {"state": "Full", "interface": expected_neighbor.interface},
                matching if matching is not None else "missing",
            )
        )
    return checks


def _route_checks(
    snapshot: RuntimeSnapshot,
    expectation: RuntimeExpectation,
    expected_metric: int,
) -> list[dict[str, object]]:
    entries = _sequence(snapshot.route.get(expectation.route_prefix))
    selected_routes: list[Mapping[str, Any]] = []
    for value in entries:
        route = _mapping(value)
        if route.get("installed") is True and route.get("selected") is True:
            selected_routes.append(route)
    installed_route: Mapping[str, Any] = (
        selected_routes[0] if selected_routes else {}
    )

    active_next_hops: list[dict[str, object]] = []
    for value in _sequence(installed_route.get("nexthops")):
        next_hop = _mapping(value)
        if next_hop.get("active") is True and next_hop.get("fib") is True:
            active_next_hops.append(
                {
                    "ip": next_hop.get("ip"),
                    "interface": next_hop.get("interfaceName"),
                }
            )

    expected_next_hops = [
        {
            "ip": expectation.route_next_hop,
            "interface": expectation.route_interface,
        }
    ]
    active_next_hops.sort(key=lambda item: (str(item["ip"]), str(item["interface"])))
    return [
        _check("route.installed", True, bool(installed_route)),
        _check("route.selected_entry_count", 1, len(selected_routes)),
        _check("route.protocol", "ospf", installed_route.get("protocol")),
        _check("route.metric", expected_metric, installed_route.get("metric")),
        _check("route.next_hops", expected_next_hops, active_next_hops),
    ]


def _bfd_checks(
    snapshot: RuntimeSnapshot,
    expectation: RuntimeExpectation,
) -> list[dict[str, object]]:
    bfd = snapshot.bfd
    expected_bfd = expectation.bfd
    peer_matches = (
        bfd.get("peer") == expected_bfd.peer
        and bfd.get("interface") == expected_bfd.interface
    )
    status: object = bfd.get("status") if peer_matches else "missing"
    return [
        _check("bfd.status", "up", status),
        _check(
            "bfd.receive_interval_ms",
            expected_bfd.receive_interval_ms,
            bfd.get("receive-interval") if peer_matches else None,
        ),
        _check(
            "bfd.transmit_interval_ms",
            expected_bfd.transmit_interval_ms,
            bfd.get("transmit-interval") if peer_matches else None,
        ),
        _check(
            "bfd.detect_multiplier",
            expected_bfd.detect_multiplier,
            bfd.get("detect-multiplier") if peer_matches else None,
        ),
        _check(
            "bfd.detection_timeout_ms",
            expected_bfd.detection_timeout_ms,
            bfd.get("detection-timeout") if peer_matches else None,
        ),
    ]


def build_runtime_conformance_evidence(
    intent: NetworkIntent,
    expectation: RuntimeExpectation,
    raw_snapshot: Mapping[str, Any],
    *,
    configuration_sha256: str,
) -> dict[str, Any]:
    if _SHA256_PATTERN.fullmatch(configuration_sha256) is None:
        raise ValueError("configuration_sha256 must be a lowercase SHA-256")
    if expectation.router not in intent.nodes:
        raise ValueError(f"runtime router is not declared in intent: {expectation.router}")
    if expectation.service_node not in intent.nodes:
        raise ValueError(
            f"runtime service node is not declared in intent: {expectation.service_node}"
        )
    if intent.nodes[expectation.router].role is not NodeRole.ACCESS:
        raise ValueError(f"runtime router must have access role: {expectation.router}")
    if intent.nodes[expectation.service_node].role is not NodeRole.SERVICE:
        raise ValueError(
            f"runtime service node must have service role: {expectation.service_node}"
        )
    if expectation.route_prefix not in intent.nodes[expectation.service_node].prefixes:
        raise ValueError(
            f"route prefix {expectation.route_prefix} is not owned by "
            f"{expectation.service_node} in intent"
        )
    snapshot = RuntimeSnapshot.model_validate(raw_snapshot)
    if snapshot.router != expectation.router:
        raise ValueError(
            f"snapshot router {snapshot.router} does not match {expectation.router}"
        )
    expected_metric = intent.to_topology().shortest_distance(
        expectation.router, {expectation.service_node}
    )
    if expected_metric is None:
        raise ValueError("intent has no service path for runtime router")

    checks = [
        *_ospf_checks(snapshot, expectation),
        *_route_checks(snapshot, expectation, expected_metric),
        *_bfd_checks(snapshot, expectation),
    ]
    passed = sum(check["passed"] is True for check in checks)
    return {
        "schema_version": 1,
        "scenario": "pre_fault_runtime_conformance",
        "source": "frr_runtime_observation",
        "measured": True,
        "captured_at": snapshot.captured_at.isoformat().replace("+00:00", "Z"),
        "router": expectation.router,
        "service_node": expectation.service_node,
        "route_prefix": expectation.route_prefix,
        "configuration_sha256": configuration_sha256,
        "intent_sha256": _digest(intent.model_dump(mode="json")),
        "expectation_sha256": _digest(expectation.model_dump(mode="json")),
        "snapshot_sha256": _digest(snapshot.model_dump(mode="json")),
        "passed_all": passed == len(checks),
        "summary": {
            "total": len(checks),
            "passed": passed,
            "failed": len(checks) - passed,
        },
        "checks": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an FRR runtime snapshot against lab intent"
    )
    parser.add_argument("--intent", type=Path, required=True)
    parser.add_argument("--expectation", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _snapshot_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strictly compose FRR JSON outputs into a runtime snapshot"
    )
    parser.add_argument(
        "--router",
        required=True,
        type=str,
    )
    parser.add_argument("--ospf", type=Path, required=True)
    parser.add_argument("--route", type=Path, required=True)
    parser.add_argument("--bfd", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("output must not be a symbolic link")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(
                evidence,
                temporary,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_output_path(
    output: Path,
    direct_inputs: Sequence[Path],
    provenance_root: Path | None = None,
) -> None:
    resolved_output = output.resolve()
    if resolved_output in {path.resolve() for path in direct_inputs}:
        raise ValueError("output must not overwrite an input")
    if provenance_root is None:
        return
    lab_directory = provenance_root / "lab"
    provenance_inputs = {
        path.resolve()
        for path in lab_directory.rglob("*")
        if path.is_file()
    }
    if resolved_output in provenance_inputs:
        raise ValueError("output must not overwrite a provenance input")


def snapshot_main(argv: list[str] | None = None) -> int:
    args = _snapshot_parser().parse_args(argv)
    if _IDENTIFIER_PATTERN.fullmatch(args.router) is None:
        raise ValueError(f"invalid router name: {args.router}")
    inputs = (args.ospf, args.route, args.bfd)
    _validate_output_path(args.output, inputs)
    snapshot = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "router": args.router,
        "ospf": _load_json_object(args.ospf, "OSPF JSON", _MAX_COMPONENT_BYTES),
        "route": _load_json_object(args.route, "route JSON", _MAX_COMPONENT_BYTES),
        "bfd": _load_json_object(args.bfd, "BFD JSON", _MAX_COMPONENT_BYTES),
    }
    _write_evidence(args.output, snapshot)
    print(f"wrote runtime snapshot to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = args.project_root.resolve()
    canonical_intent = (project_root / "lab" / "intent.yml").resolve()
    canonical_expectation = (
        project_root / "lab" / "runtime-expectations.yml"
    ).resolve()
    if (
        args.intent.resolve() != canonical_intent
        or args.expectation.resolve() != canonical_expectation
    ):
        raise ValueError(
            "intent and expectation must use the canonical project-root files"
        )
    inputs = (args.intent, args.expectation, args.snapshot)
    _validate_output_path(args.output, inputs, project_root)
    _ensure_bounded_file(args.intent, "intent")
    _ensure_bounded_file(args.expectation, "runtime expectation")
    snapshot = _load_snapshot(args.snapshot)
    evidence = build_runtime_conformance_evidence(
        load_intent(args.intent),
        load_runtime_expectation(args.expectation),
        snapshot,
        configuration_sha256=configuration_fingerprint(project_root),
    )
    _write_evidence(args.output, evidence)
    if evidence["passed_all"]:
        print(f"runtime conformance passed: {evidence['summary']['passed']} checks")
        return 0
    print(f"runtime conformance failed: {evidence['summary']['failed']} checks")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
