from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from telconet_sentinel.runtime_resilience import (
    RuntimeN1Observation,
    _refuse_input_overwrite,
    build_runtime_n1_evidence,
    build_runtime_n1_plan,
    extract_full_neighbor_count,
    extract_selected_route_metric,
    load_runtime_n1_plan,
    main,
)

ROOT = Path(__file__).parents[2]
INTENT = ROOT / "lab" / "intent-dual-homed.yml"
LAB = ROOT / "lab" / "telconet-dual-homed.clab.yml"


def _observation_document(plan: object) -> dict[str, object]:
    scenarios = []
    for scenario in plan.scenarios:  # type: ignore[attr-defined]
        paths = []
        for path in scenario.paths:
            paths.append(
                {
                    "access_node": path.access_node,
                    "fault_metric": path.fault_metric,
                    "fault_reachable": path.fault_metric is not None,
                    "restored_metric": path.baseline_metric,
                    "restored_reachable": True,
                }
            )
        scenarios.append({"scenario_id": scenario.scenario_id, "paths": paths})
    return {
        "schema_version": 1,
        "source": "containerlab_frr_observation",
        "scenarios": scenarios,
    }


def test_builds_exact_link_and_node_runtime_plan_from_candidate_intent() -> None:
    plan = build_runtime_n1_plan(INTENT, LAB)

    assert plan.schema_version == 1
    assert plan.source == "intent_runtime_n1_plan"
    assert len(plan.topology_sha256) == 64
    assert len(plan.lab_sha256) == 64
    assert plan.service_prefix == "10.20.0.10/32"
    assert len(plan.scenarios) == 15
    assert sum(scenario.kind == "link" for scenario in plan.scenarios) == 11
    assert sum(scenario.kind == "node" for scenario in plan.scenarios) == 4
    assert {
        item.router: item.full_neighbors for item in plan.baseline_adjacencies
    } == {
        "access1": 2,
        "access2": 2,
        "agg1": 4,
        "agg2": 4,
        "core1": 4,
        "core2": 4,
        "service-host": 2,
    }

    access_link = next(
        scenario
        for scenario in plan.scenarios
        if scenario.scenario_id == "link:access1--agg1"
    )
    assert [(item.node, item.interface) for item in access_link.injections] == [
        ("access1", "eth1"),
        ("agg1", "eth1"),
    ]
    path_metrics = [
        (path.access_node, path.baseline_metric, path.fault_metric)
        for path in access_link.paths
    ]
    assert path_metrics == [
        ("access1", 30, 140),
        ("access2", 50, 50),
    ]

    service_link = next(
        scenario
        for scenario in plan.scenarios
        if scenario.scenario_id == "link:core1--service-host"
    )
    assert [path.fault_metric for path in service_link.paths] == [70, 50]

    core = next(
        scenario for scenario in plan.scenarios if scenario.scenario_id == "node:core1"
    )
    assert [(item.node, item.interface) for item in core.injections] == [
        ("core1", "eth1"),
        ("core1", "eth2"),
        ("core1", "eth3"),
        ("core1", "eth4"),
    ]
    assert [path.fault_metric for path in core.paths] == [140, 50]


def test_plan_parses_each_bounded_yaml_snapshot_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open
    opened: list[Path] = []

    def tracked_open(path: Path, *args: object, **kwargs: object) -> object:
        if path in {INTENT, LAB}:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)

    build_runtime_n1_plan(INTENT, LAB)

    assert opened.count(INTENT) == 1
    assert opened.count(LAB) == 1


def test_plan_rejects_oversized_topology_before_fault_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = ["  access1:\n    role: access"]
    nodes.extend(f"  core{index}:\n    role: core" for index in range(63))
    nodes.append(
        "  service-host:\n"
        "    role: service\n"
        "    prefixes: [10.20.0.10/32]"
    )
    candidate_intent = tmp_path / "oversized-intent.yml"
    candidate_intent.write_text(
        "version: 1\nnodes:\n" + "\n".join(nodes) + "\nlinks: []\n",
        encoding="utf-8",
    )

    def unexpected_audit(*args: object, **kwargs: object) -> None:
        raise AssertionError("fault audit must not run for an oversized topology")

    monkeypatch.setattr(
        "telconet_sentinel.runtime_resilience.audit_fault_scenarios",
        unexpected_audit,
    )

    with pytest.raises(ValueError, match="at most 64 nodes"):
        build_runtime_n1_plan(candidate_intent, LAB)


def test_plan_rejects_excessive_yaml_alias_expansion(tmp_path: Path) -> None:
    aliases = "\n".join(
        f"  access{index}: *access" for index in range(2, 68)
    )
    candidate_intent = tmp_path / "aliases.yml"
    candidate_intent.write_text(
        "version: 1\n"
        "nodes:\n"
        "  access1: &access\n"
        "    role: access\n"
        f"{aliases}\n"
        "links: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="too many YAML aliases"):
        build_runtime_n1_plan(candidate_intent, LAB)


def test_runtime_observations_match_all_model_expectations() -> None:
    plan = build_runtime_n1_plan(INTENT, LAB)
    observation = RuntimeN1Observation.model_validate(_observation_document(plan))

    evidence = build_runtime_n1_evidence(plan, observation)

    assert evidence["passed_all"] is True
    assert evidence["summary"] == {"total": 120, "passed": 120, "failed": 0}
    assert len(evidence["scenarios"]) == 15
    assert all(item["passed"] for item in evidence["scenarios"])


def test_runtime_metric_and_reachability_drift_fail_independent_checks() -> None:
    plan = build_runtime_n1_plan(INTENT, LAB)
    document = _observation_document(plan)
    first_path = document["scenarios"][0]["paths"][0]  # type: ignore[index]
    first_path["fault_metric"] = 999
    first_path["restored_reachable"] = False
    observation = RuntimeN1Observation.model_validate(document)

    evidence = build_runtime_n1_evidence(plan, observation)

    assert evidence["passed_all"] is False
    assert evidence["summary"] == {"total": 120, "passed": 118, "failed": 2}
    failed = [
        check
        for scenario in evidence["scenarios"]
        for check in scenario["checks"]
        if not check["passed"]
    ]
    assert [check["check"] for check in failed] == [
        "fault_route_metric",
        "restored_reachability",
    ]


def test_runtime_observation_requires_exact_unique_scenario_and_access_sets() -> None:
    plan = build_runtime_n1_plan(INTENT, LAB)
    missing = _observation_document(plan)
    missing["scenarios"].pop()  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="scenario set"):
        build_runtime_n1_evidence(
            plan,
            RuntimeN1Observation.model_validate(missing),
        )

    duplicate = _observation_document(plan)
    duplicate["scenarios"].append(copy.deepcopy(duplicate["scenarios"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ValidationError, match="scenario ids must be unique"):
        RuntimeN1Observation.model_validate(duplicate)

    wrong_access = _observation_document(plan)
    wrong_access["scenarios"][0]["paths"][0]["access_node"] = "access9"  # type: ignore[index]
    with pytest.raises(ValueError, match="access-node set"):
        build_runtime_n1_evidence(
            plan,
            RuntimeN1Observation.model_validate(wrong_access),
        )


def test_runtime_observation_is_strict_and_bounded() -> None:
    plan = build_runtime_n1_plan(INTENT, LAB)
    document = _observation_document(plan)
    document["extra"] = True
    with pytest.raises(ValidationError, match="extra"):
        RuntimeN1Observation.model_validate(document)

    coerced = _observation_document(plan)
    coerced["schema_version"] = "1"
    with pytest.raises(ValidationError, match="schema_version"):
        RuntimeN1Observation.model_validate(coerced)


def test_extracts_exactly_one_selected_installed_ospf_route_metric() -> None:
    route = {
        "10.20.0.10/32": [
            {
                "protocol": "ospf",
                "selected": True,
                "installed": True,
                "metric": 140,
            }
        ]
    }
    assert extract_selected_route_metric(json.dumps(route)) == 140

    route["10.20.0.10/32"].append(copy.deepcopy(route["10.20.0.10/32"][0]))
    with pytest.raises(ValueError, match="exactly one selected"):
        extract_selected_route_metric(json.dumps(route))

    route["10.20.0.10/32"] = [
        {"protocol": "static", "selected": True, "installed": True, "metric": 1}
    ]
    with pytest.raises(ValueError, match="exactly one selected"):
        extract_selected_route_metric(json.dumps(route))


def test_counts_only_full_ospf_adjacencies() -> None:
    ospf = {
        "neighbors": {
            "10.255.0.21": [{"nbrState": "Full/-"}],
            "10.255.0.22": [{"nbrState": "Full/-"}],
            "10.255.0.31": [{"nbrState": "Init/-"}],
        }
    }

    assert extract_full_neighbor_count(json.dumps(ospf)) == 2

    ospf["neighbors"]["10.255.0.21"] = "ambiguous"
    with pytest.raises(ValueError, match="neighbor entries must be lists"):
        extract_full_neighbor_count(json.dumps(ospf))

def test_cli_generates_plan_and_fail_closed_evidence(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    observation_path = tmp_path / "observation.json"
    evidence_path = tmp_path / "evidence.json"

    assert (
        main(
            [
                "plan",
                "--intent",
                str(INTENT),
                "--lab",
                str(LAB),
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    plan = load_runtime_n1_plan(plan_path)
    observation_path.write_text(
        json.dumps(_observation_document(plan)), encoding="utf-8"
    )
    assert (
        main(
            [
                "verify",
                "--plan",
                str(plan_path),
                "--observation",
                str(observation_path),
                "--output",
                str(evidence_path),
            ]
        )
        == 0
    )
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["passed_all"] is True

    drift = _observation_document(plan)
    drift["scenarios"][0]["paths"][0]["fault_metric"] = 999  # type: ignore[index]
    observation_path.write_text(json.dumps(drift), encoding="utf-8")
    assert (
        main(
            [
                "verify",
                "--plan",
                str(plan_path),
                "--observation",
                str(observation_path),
                "--output",
                str(evidence_path),
            ]
        )
        == 1
    )


def test_cli_settings_are_derived_from_the_candidate_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate_intent = tmp_path / "intent.yml"
    candidate_intent.write_text(
        INTENT.read_text(encoding="utf-8").replace(
            "10.20.0.10/32",
            "10.20.1.10/32",
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    assert (
        main(
            [
                "plan",
                "--intent",
                str(candidate_intent),
                "--lab",
                str(LAB),
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["settings", "--plan", str(plan_path)]) == 0

    assert capsys.readouterr().out == "10.20.1.10/32\t10.20.1.10\t30\t50\n"


def test_plan_loader_rejects_ambiguous_json(tmp_path: Path) -> None:
    plan = build_runtime_n1_plan(INTENT, LAB).model_dump(mode="json")
    raw = json.dumps(plan).replace(
        '"schema_version": 1,',
        '"schema_version": 1, "schema_version": 1,',
        1,
    )
    path = tmp_path / "plan.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_runtime_n1_plan(path)


def test_output_cannot_alias_an_input_through_parent_segments(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    alias_parent = tmp_path / "nested"
    alias_parent.mkdir()

    with pytest.raises(ValueError, match="must not overwrite"):
        _refuse_input_overwrite(
            alias_parent / ".." / input_path.name,
            (input_path,),
        )
