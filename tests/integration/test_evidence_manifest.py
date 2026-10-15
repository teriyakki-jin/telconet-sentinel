from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from telconet_sentinel.evidence_manifest import (
    build_evidence_manifest,
    main,
    verify_evidence_manifest,
)

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "evidence" / "manifest.json"


def test_checked_in_manifest_verifies_every_portfolio_claim_input() -> None:
    result = verify_evidence_manifest(ROOT, MANIFEST)

    assert result["verified"] is True
    assert result["artifact_count"] == 59
    assert len(result["bundle_sha256"]) == 64

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    paths = {artifact["path"] for artifact in manifest["artifacts"]}
    assert {"lab/designs.yml", "lab/intent-dual-homed.yml"} <= paths
    roles = {artifact["role"] for artifact in manifest["artifacts"]}
    assert roles == {
        "configuration",
        "derived_evidence",
        "generated_report",
        "raw_measurement",
    }


def test_manifest_is_deterministic_and_binds_configuration_fingerprint() -> None:
    first = build_evidence_manifest(ROOT)
    second = build_evidence_manifest(ROOT)

    assert first == second
    assert len(first["configuration_sha256"]) == 64
    assert len(first["bundle_sha256"]) == 64
    assert first["artifact_count"] == len(first["artifacts"])
    assert [item["path"] for item in first["artifacts"]] == sorted(
        item["path"] for item in first["artifacts"]
    )


def test_verifier_rejects_tampered_raw_measurement(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "evidence", project / "evidence")
    shutil.copytree(ROOT / "lab", project / "lab")
    shutil.copytree(ROOT / "docs", project / "docs")
    raw_log = project / "evidence" / "remote-blackhole-bfd.log"
    raw_log.write_text(raw_log.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="raw-log recalculation|manifest mismatch"):
        verify_evidence_manifest(project, project / "evidence" / "manifest.json")


def test_verifier_rejects_manifest_with_duplicate_json_key(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        MANIFEST.read_text(encoding="utf-8").replace(
            '"artifact_count": 59,',
            '"artifact_count": 59,\n  "artifact_count": 59,',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        verify_evidence_manifest(ROOT, manifest)


def test_manifest_rejects_an_oversized_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "evidence", project / "evidence")
    shutil.copytree(ROOT / "lab", project / "lab")
    shutil.copytree(ROOT / "docs", project / "docs")
    (project / "lab" / "runtime-expectations.yml").write_bytes(
        b"x" * (5 * 1024 * 1024 + 1)
    )

    with pytest.raises(ValueError, match="artifact exceeds 5242880 bytes"):
        build_evidence_manifest(project)


def test_cli_writes_then_verifies_only_the_canonical_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "evidence", project / "evidence")
    shutil.copytree(ROOT / "lab", project / "lab")
    shutil.copytree(ROOT / "docs", project / "docs")
    manifest = project / "evidence" / "manifest.json"
    manifest.unlink(missing_ok=True)

    assert main(["generate", "--project-root", str(project)]) == 0
    assert main(["verify", "--project-root", str(project)]) == 0
    assert json.loads(manifest.read_text(encoding="utf-8"))["schema_version"] == 1

    with pytest.raises(ValueError, match="canonical evidence manifest"):
        main(
            [
                "verify",
                "--project-root",
                str(project),
                "--manifest",
                str(tmp_path / "elsewhere.json"),
            ]
        )


def test_manifest_output_does_not_follow_a_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "evidence", project / "evidence")
    shutil.copytree(ROOT / "lab", project / "lab")
    shutil.copytree(ROOT / "docs", project / "docs")
    manifest = project / "evidence" / "manifest.json"
    manifest.unlink(missing_ok=True)
    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    try:
        manifest.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="symlink"):
        main(["generate", "--project-root", str(project)])
    assert target.read_text(encoding="utf-8") == "unchanged"
