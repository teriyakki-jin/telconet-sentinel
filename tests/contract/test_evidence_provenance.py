from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_ci_verifies_checked_in_evidence_manifest() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]

    assert any(
        step.get("run") == "uv run telconet-evidence verify --project-root ."
        for step in steps
    )


def test_main_branch_attests_the_packaged_live_lab_evidence() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "lab-e2e.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    lab_job = workflow["jobs"]["lab-e2e"]
    steps = lab_job["steps"]

    assert lab_job["permissions"] == {"contents": "read"}
    image_lock = next(
        step for step in steps if step.get("name") == "Load pinned lab images"
    )
    assert "quay.io/frrouting/frr@sha256:65e5967b" in image_lock["run"]
    assert "alpine@sha256:5291449c" in image_lock["run"]
    assert any(
        step.get("name") == "Package live evidence"
        and "telconet-live-evidence.tar.gz" in step.get("run", "")
        for step in steps
    )
    package = next(step for step in steps if step.get("name") == "Package live evidence")
    assert "-type l" in package["run"]
    assert "52428800" in package["run"]

    attest_job = workflow["jobs"]["attest-live-evidence"]
    assert attest_job["needs"] == "lab-e2e"
    assert attest_job["if"] == (
        "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
    )
    assert attest_job["permissions"] == {
        "attestations": "write",
        "artifact-metadata": "write",
        "contents": "read",
        "id-token": "write",
    }
    attestation = next(
        step
        for step in attest_job["steps"]
        if step.get("name") == "Attest live evidence provenance"
    )
    assert attestation["with"]["subject-path"] == (
        "artifacts/telconet-live-evidence.tar.gz"
    )
    assert attestation["uses"].startswith("actions/attest@")
    assert not attestation["uses"].endswith("@v4")
