from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from telconet_sentinel.config import load_topology
from telconet_sentinel.configuration import candidate_runtime_configuration_fingerprint
from telconet_sentinel.runtime_resilience import (
    RuntimeN1PublishedEvidence,
    load_runtime_n1_published_evidence,
    render_runtime_n1_metrics,
    validate_runtime_n1_published_topology,
)

ROOT = Path(__file__).parents[2]
PUBLISHED = ROOT / "evidence" / "runtime-n1-dashboard.json"


def test_loads_attested_runtime_summary_and_renders_prometheus_metrics() -> None:
    evidence = load_runtime_n1_published_evidence(PUBLISHED)

    assert evidence.source_commit == "a66cb597c9fc6bab6e60ea789075983ebb5677cf"
    assert evidence.workflow_run_id == 38834563515
    assert evidence.archive_sha256 == (
        "aca9c339f4815a829ef2e467afe66f5fefd7ebdd7e80665d6db15ee94ae77e61"
    )
    assert evidence.passed_all is True
    assert evidence.total_checks == 120
    assert evidence.passed_checks == 120
    assert evidence.failed_checks == 0
    assert len(evidence.scenarios) == 15
    assert evidence.runtime_configuration_sha256 == (
        candidate_runtime_configuration_fingerprint(ROOT)
    )

    rendered = render_runtime_n1_metrics(evidence)

    assert "telconet_runtime_n1_pass 1" in rendered
    assert 'telconet_runtime_n1_scenarios_total{kind="link"} 11' in rendered
    assert 'telconet_runtime_n1_scenarios_total{kind="node"} 4' in rendered
    assert 'telconet_runtime_n1_checks_total{result="passed"} 120' in rendered
    assert 'telconet_runtime_n1_checks_total{result="failed"} 0' in rendered
    assert (
        'telconet_runtime_n1_scenario_pass{kind="link",'
        'scenario_id="link:core1--service-host"} 1'
    ) in rendered
    assert (
        'telconet_runtime_n1_evidence_info{source_commit="a66cb597c9fc6bab6e60ea789075983ebb5677cf",'
        'workflow_run="38834563515"} 1'
    ) in rendered


def test_published_runtime_summary_rejects_inconsistent_or_duplicate_results() -> None:
    document = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    document["passed_checks"] = 119
    with pytest.raises(ValidationError, match="check totals"):
        RuntimeN1PublishedEvidence.model_validate(document)

    document = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    document["scenarios"].append(document["scenarios"][0])
    with pytest.raises(ValidationError, match="scenario ids must be unique"):
        RuntimeN1PublishedEvidence.model_validate(document)


def test_published_runtime_summary_rejects_a_failed_scenario_labeled_as_pass() -> None:
    document = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    document["scenarios"][0]["passed"] = False

    with pytest.raises(ValidationError, match="scenario and check results conflict"):
        RuntimeN1PublishedEvidence.model_validate(document)


@pytest.mark.parametrize(
    ("failed_checks", "passed_checks", "failed_scenario"),
    ((1, 119, False), (0, 120, True)),
)
def test_published_runtime_summary_rejects_scenario_check_contradictions(
    failed_checks: int,
    passed_checks: int,
    failed_scenario: bool,
) -> None:
    document = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    document["failed_checks"] = failed_checks
    document["passed_checks"] = passed_checks
    document["passed_all"] = False
    if failed_scenario:
        document["scenarios"][0]["passed"] = False

    with pytest.raises(ValidationError, match="scenario and check results conflict"):
        RuntimeN1PublishedEvidence.model_validate(document)


def test_published_runtime_summary_requires_eight_checks_per_scenario() -> None:
    document = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    document["total_checks"] = 119
    document["passed_checks"] = 119

    with pytest.raises(ValidationError, match="eight checks per scenario"):
        RuntimeN1PublishedEvidence.model_validate(document)


def test_published_runtime_summary_must_cover_the_candidate_fault_matrix() -> None:
    evidence = load_runtime_n1_published_evidence(PUBLISHED)
    topology = load_topology(ROOT / "lab" / "intent-dual-homed.yml")
    configuration_sha256 = candidate_runtime_configuration_fingerprint(ROOT)
    document = evidence.model_dump(mode="json")
    document["scenarios"] = document["scenarios"][:-1]
    document["total_checks"] = 112
    document["passed_checks"] = 112
    incomplete = RuntimeN1PublishedEvidence.model_validate(document)

    with pytest.raises(ValueError, match="scenario matrix does not match"):
        validate_runtime_n1_published_topology(
            incomplete, topology, configuration_sha256
        )


def test_published_runtime_summary_rejects_a_different_candidate_topology() -> None:
    evidence = load_runtime_n1_published_evidence(PUBLISHED)
    baseline = load_topology(ROOT / "lab" / "intent.yml")

    with pytest.raises(ValueError, match="does not match candidate intent"):
        validate_runtime_n1_published_topology(
            evidence,
            baseline,
            candidate_runtime_configuration_fingerprint(ROOT),
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "lab/telconet-dual-homed.clab.yml",
        "lab/frr/dual-homed/core1.conf",
    ),
)
def test_candidate_runtime_configuration_fingerprint_binds_effective_files(
    tmp_path: Path,
    relative_path: str,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "lab", project / "lab")
    original = candidate_runtime_configuration_fingerprint(project)
    target = project / relative_path
    target.write_text(
        target.read_text(encoding="utf-8") + "\n! drift\n",
        encoding="utf-8",
    )

    assert candidate_runtime_configuration_fingerprint(project) != original
