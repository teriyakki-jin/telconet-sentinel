from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from telconet_sentinel.intent import load_intent
from telconet_sentinel.models import NodeRole
from telconet_sentinel.runtime_conformance import (
    RuntimeExpectation,
    build_runtime_conformance_evidence,
    main,
)

ROOT = Path(__file__).parents[2]
CONFIGURATION_SHA256 = "1" * 64
EXPECTATION = ROOT / "lab" / "runtime-expectations.yml"


def _expectation() -> RuntimeExpectation:
    return RuntimeExpectation.model_validate(
        {
            "version": 1,
            "router": "access1",
            "service_node": "service-host",
            "route_prefix": "10.20.0.0/24",
            "route_next_hop": "10.0.1.1",
            "route_interface": "eth1",
            "ospf_neighbors": [
                {
                    "router_id": "10.255.0.21",
                    "interface": "eth1",
                },
                {
                    "router_id": "10.255.0.22",
                    "interface": "eth2",
                },
            ],
            "bfd": {
                "peer": "10.0.1.1",
                "interface": "eth1",
                "receive_interval_ms": 100,
                "transmit_interval_ms": 100,
                "detect_multiplier": 3,
                "detection_timeout_ms": 300,
            },
        }
    )


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "captured_at": "2026-09-18T06:50:31Z",
        "router": "access1",
        "ospf": {
            "neighbors": {
                "10.255.0.21": [
                    {
                        "nbrState": "Full/-",
                        "converged": "Full",
                        "ifaceAddress": "10.0.1.1",
                        "ifaceName": "eth1:10.0.1.0",
                    }
                ],
                "10.255.0.22": [
                    {
                        "nbrState": "Full/-",
                        "converged": "Full",
                        "ifaceAddress": "10.0.1.3",
                        "ifaceName": "eth2:10.0.1.2",
                    }
                ],
            }
        },
        "route": {
            "10.20.0.0/24": [
                {
                    "protocol": "ospf",
                    "selected": True,
                    "installed": True,
                    "metric": 30,
                    "nexthops": [
                        {
                            "ip": "10.0.1.1",
                            "interfaceName": "eth1",
                            "active": True,
                            "fib": True,
                        }
                    ],
                }
            ]
        },
        "bfd": {
            "peer": "10.0.1.1",
            "interface": "eth1",
            "status": "up",
            "receive-interval": 100,
            "transmit-interval": 100,
            "detect-multiplier": 3,
            "detection-timeout": 300,
        },
    }


def test_runtime_snapshot_matches_declared_intent_and_profile() -> None:
    evidence = build_runtime_conformance_evidence(
        load_intent(ROOT / "lab" / "intent.yml"),
        _expectation(),
        _snapshot(),
        configuration_sha256=CONFIGURATION_SHA256,
    )

    assert evidence["source"] == "frr_runtime_observation"
    assert evidence["measured"] is True
    assert evidence["passed_all"] is True
    assert evidence["summary"] == {"total": 13, "passed": 13, "failed": 0}
    assert {check["id"] for check in evidence["checks"]} == {
        "ospf.neighbor.10.255.0.21",
        "ospf.neighbor.10.255.0.22",
        "ospf.adjacencies",
        "route.installed",
        "route.selected_entry_count",
        "route.protocol",
        "route.metric",
        "route.next_hops",
        "bfd.status",
        "bfd.receive_interval_ms",
        "bfd.transmit_interval_ms",
        "bfd.detect_multiplier",
        "bfd.detection_timeout_ms",
    }
    assert next(
        check for check in evidence["checks"] if check["id"] == "route.metric"
    ) == {"id": "route.metric", "expected": 30, "actual": 30, "passed": True}


def test_runtime_drift_is_reported_as_failed_checks() -> None:
    snapshot = copy.deepcopy(_snapshot())
    snapshot["ospf"]["neighbors"]["10.255.0.21"][0]["converged"] = "Init"
    snapshot["ospf"]["neighbors"]["10.255.0.21"][0]["nbrState"] = "Init/-"
    snapshot["route"]["10.20.0.0/24"][0]["metric"] = 140
    snapshot["bfd"]["status"] = "down"

    evidence = build_runtime_conformance_evidence(
        load_intent(ROOT / "lab" / "intent.yml"),
        _expectation(),
        snapshot,
        configuration_sha256=CONFIGURATION_SHA256,
    )

    assert evidence["passed_all"] is False
    assert evidence["summary"] == {"total": 13, "passed": 9, "failed": 4}
    failed = {
        check["id"]: check
        for check in evidence["checks"]
        if check["passed"] is False
    }
    assert set(failed) == {
        "ospf.adjacencies",
        "ospf.neighbor.10.255.0.21",
        "route.metric",
        "bfd.status",
    }
    assert failed["route.metric"]["expected"] == 30
    assert failed["route.metric"]["actual"] == 140


def test_unexpected_adjacency_and_fib_next_hop_fail_exact_set_checks() -> None:
    snapshot = copy.deepcopy(_snapshot())
    snapshot["ospf"]["neighbors"]["10.255.0.99"] = [
        {
            "nbrState": "Full/-",
            "converged": "Full",
            "ifaceAddress": "10.0.9.9",
            "ifaceName": "eth9:10.0.9.8",
        }
    ]
    snapshot["route"]["10.20.0.0/24"][0]["nexthops"].append(
        {
            "ip": "10.0.1.3",
            "interfaceName": "eth2",
            "active": True,
            "fib": True,
        }
    )

    evidence = build_runtime_conformance_evidence(
        load_intent(ROOT / "lab" / "intent.yml"),
        _expectation(),
        snapshot,
        configuration_sha256=CONFIGURATION_SHA256,
    )

    failed_ids = {
        check["id"] for check in evidence["checks"] if check["passed"] is False
    }
    assert failed_ids == {"ospf.adjacencies", "route.next_hops"}


def test_duplicate_adjacency_entry_and_selected_route_fail_exact_checks() -> None:
    snapshot = copy.deepcopy(_snapshot())
    snapshot["ospf"]["neighbors"]["10.255.0.21"].append(
        {
            "nbrState": "Init/-",
            "converged": "Init",
            "ifaceAddress": "10.0.9.9",
            "ifaceName": "eth9:10.0.9.8",
        }
    )
    duplicate_route = copy.deepcopy(snapshot["route"]["10.20.0.0/24"][0])
    snapshot["route"]["10.20.0.0/24"].append(duplicate_route)

    evidence = build_runtime_conformance_evidence(
        load_intent(ROOT / "lab" / "intent.yml"),
        _expectation(),
        snapshot,
        configuration_sha256=CONFIGURATION_SHA256,
    )

    failed_ids = {
        check["id"] for check in evidence["checks"] if check["passed"] is False
    }
    assert failed_ids == {"ospf.adjacencies", "route.selected_entry_count"}


def test_negotiated_bfd_detection_timeout_must_match_profile() -> None:
    snapshot = copy.deepcopy(_snapshot())
    snapshot["bfd"]["detection-timeout"] = 3_000

    evidence = build_runtime_conformance_evidence(
        load_intent(ROOT / "lab" / "intent.yml"),
        _expectation(),
        snapshot,
        configuration_sha256=CONFIGURATION_SHA256,
    )

    failed = [check for check in evidence["checks"] if check["passed"] is False]
    assert failed == [
        {
            "id": "bfd.detection_timeout_ms",
            "expected": 300,
            "actual": 3_000,
            "passed": False,
        }
    ]


def test_runtime_expectation_rejects_unknown_or_ambiguous_fields() -> None:
    document = _expectation().model_dump()
    document["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected"):
        RuntimeExpectation.model_validate(document)


@pytest.mark.parametrize(
    ("node_name", "role", "message"),
    [
        ("access1", NodeRole.CORE, "access role"),
        ("service-host", NodeRole.CORE, "service role"),
    ],
)
def test_runtime_contract_requires_semantic_node_roles(
    node_name: str,
    role: NodeRole,
    message: str,
) -> None:
    intent = load_intent(ROOT / "lab" / "intent.yml")
    nodes = dict(intent.nodes)
    nodes[node_name] = nodes[node_name].model_copy(update={"role": role})
    altered_intent = intent.model_copy(update={"nodes": nodes})

    with pytest.raises(ValueError, match=message):
        build_runtime_conformance_evidence(
            altered_intent,
            _expectation(),
            _snapshot(),
            configuration_sha256=CONFIGURATION_SHA256,
        )


def test_cli_writes_evidence_and_returns_nonzero_on_drift(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "runtime-snapshot.json"
    output_path = tmp_path / "runtime-conformance.json"
    snapshot = _snapshot()
    snapshot["route"]["10.20.0.0/24"][0]["metric"] = 140
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = main(
        [
            "--intent",
            str(ROOT / "lab" / "intent.yml"),
            "--expectation",
            str(EXPECTATION),
            "--snapshot",
            str(snapshot_path),
            "--project-root",
            str(ROOT),
            "--output",
            str(output_path),
        ]
    )

    assert result == 1
    evidence = json.loads(output_path.read_text(encoding="utf-8"))
    assert evidence["passed_all"] is False
    assert evidence["configuration_sha256"] != CONFIGURATION_SHA256


def test_cli_returns_zero_for_a_conformant_snapshot(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "runtime-snapshot.json"
    output_path = tmp_path / "runtime-conformance.json"
    snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")

    assert (
        main(
            [
                "--intent",
                str(ROOT / "lab" / "intent.yml"),
                "--expectation",
                str(EXPECTATION),
                "--snapshot",
                str(snapshot_path),
                "--project-root",
                str(ROOT),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    assert json.loads(output_path.read_text(encoding="utf-8"))["passed_all"] is True


def test_cli_does_not_overwrite_a_snapshot_input(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "runtime-snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    original = snapshot_path.read_bytes()

    with pytest.raises(ValueError, match="output must not overwrite an input"):
        main(
            [
                "--intent",
                str(ROOT / "lab" / "intent.yml"),
                "--expectation",
                str(EXPECTATION),
                "--snapshot",
                str(snapshot_path),
                "--project-root",
                str(ROOT),
                "--output",
                str(snapshot_path),
            ]
        )
    assert snapshot_path.read_bytes() == original


@pytest.mark.parametrize(
    "raw_snapshot",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":NaN}',
    ],
)
def test_cli_rejects_ambiguous_json_snapshot(
    tmp_path: Path, raw_snapshot: str
) -> None:
    snapshot_path = tmp_path / "runtime-snapshot.json"
    output_path = tmp_path / "runtime-conformance.json"
    snapshot_path.write_text(raw_snapshot, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key|non-finite JSON value"):
        main(
            [
                "--intent",
                str(ROOT / "lab" / "intent.yml"),
                "--expectation",
                str(EXPECTATION),
                "--snapshot",
                str(snapshot_path),
                "--project-root",
                str(ROOT),
                "--output",
                str(output_path),
            ]
        )


def test_cli_binds_intent_and_expectation_to_project_root(tmp_path: Path) -> None:
    custom_expectation = tmp_path / "runtime-expectations.yml"
    custom_expectation.write_text(EXPECTATION.read_text(encoding="utf-8"), encoding="utf-8")
    snapshot_path = tmp_path / "runtime-snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical project-root file"):
        main(
            [
                "--intent",
                str(ROOT / "lab" / "intent.yml"),
                "--expectation",
                str(custom_expectation),
                "--snapshot",
                str(snapshot_path),
                "--project-root",
                str(ROOT),
                "--output",
                str(tmp_path / "output.json"),
            ]
        )


def test_cli_does_not_overwrite_project_provenance(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    shutil.copytree(ROOT / "lab", project_root / "lab")
    snapshot_path = tmp_path / "runtime-snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    protected = project_root / "lab" / "frr" / "daemons"
    original = protected.read_bytes()

    with pytest.raises(ValueError, match="provenance input"):
        main(
            [
                "--intent",
                str(project_root / "lab" / "intent.yml"),
                "--expectation",
                str(project_root / "lab" / "runtime-expectations.yml"),
                "--snapshot",
                str(snapshot_path),
                "--project-root",
                str(project_root),
                "--output",
                str(protected),
            ]
        )
    assert protected.read_bytes() == original
