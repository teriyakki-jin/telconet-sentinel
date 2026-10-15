from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

from .configuration import configuration_fingerprint
from .report import render_report_from_paths

_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
_MAX_BUNDLE_BYTES = 25 * 1024 * 1024
_BUNDLE_NAME = "telconet-portfolio-evidence"


def _artifact_specs() -> tuple[tuple[str, str], ...]:
    configuration = (
        "lab/designs.yml",
        "lab/failure-domains.yml",
        "lab/intent.yml",
        "lab/intent-dual-homed.yml",
        "lab/runtime-expectations.yml",
        "lab/telconet.clab.yml",
        "lab/frr/daemons",
        "lab/frr/access1/frr.conf",
        "lab/frr/access2/frr.conf",
        "lab/frr/agg1/frr.conf",
        "lab/frr/agg2/frr.conf",
        "lab/frr/core1/frr.conf",
        "lab/frr/core2/frr.conf",
    )
    raw_measurements = (
        "evidence/remote-blackhole-bfd.log",
        "evidence/remote-blackhole-ospf.log",
        *(
            f"evidence/repeated/{profile}/trial-{trial:02d}.log"
            for profile in ("bfd_100x3", "ospf_only")
            for trial in range(1, 21)
        ),
    )
    derived = (
        "evidence/bfd-comparison.json",
        "evidence/bfd-repeated-trials.json",
        "evidence/failure-domain-audit.json",
    )
    specs = (
        *((path, "configuration") for path in configuration),
        *((path, "raw_measurement") for path in raw_measurements),
        *((path, "derived_evidence") for path in derived),
        ("docs/EXPERIMENT_REPORT.md", "generated_report"),
    )
    return tuple(sorted(specs))


def _normalized_bytes(path: Path, relative_path: str) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(_MAX_ARTIFACT_BYTES + 1)
    if len(content) > _MAX_ARTIFACT_BYTES:
        raise ValueError(
            f"evidence artifact exceeds {_MAX_ARTIFACT_BYTES} bytes: {relative_path}"
        )
    return content.replace(b"\r\n", b"\n")


def _artifact(project_root: Path, relative_path: str, role: str) -> dict[str, object]:
    path = project_root / relative_path
    if path.is_symlink():
        raise ValueError(f"evidence artifact must not be a symlink: {relative_path}")
    if not path.is_file():
        raise ValueError(f"evidence artifact is missing: {relative_path}")
    if not path.resolve().is_relative_to(project_root):
        raise ValueError(f"evidence artifact escapes project root: {relative_path}")
    content = _normalized_bytes(path, relative_path)
    return {
        "path": relative_path,
        "role": role,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError(f"evidence manifest exceeds {_MAX_MANIFEST_BYTES} bytes")
    value = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(value, dict):
        raise ValueError("evidence manifest must be a JSON object")
    return cast(dict[str, Any], value)


def _validate_trial_inventory(project_root: Path) -> None:
    for profile in ("bfd_100x3", "ospf_only"):
        directory = project_root / "evidence" / "repeated" / profile
        actual = sorted(path.name for path in directory.glob("trial-*.log"))
        expected = [f"trial-{trial:02d}.log" for trial in range(1, 21)]
        if actual != expected:
            raise ValueError(
                f"{profile} raw-log inventory must contain exactly trial-01.log "
                "through trial-20.log"
            )


def _validate_generated_claims(project_root: Path) -> None:
    evidence = project_root / "evidence"
    expected_report = render_report_from_paths(
        evidence / "bfd-comparison.json",
        evidence / "bfd-repeated-trials.json",
        evidence / "failure-domain-audit.json",
        project_root=project_root,
    )
    report_path = project_root / "docs" / "EXPERIMENT_REPORT.md"
    if report_path.read_text(encoding="utf-8") != expected_report:
        raise ValueError("generated report does not match recomputed evidence")


def build_evidence_manifest(project_root: Path) -> dict[str, object]:
    root = project_root.resolve(strict=True)
    _validate_trial_inventory(root)
    _validate_generated_claims(root)
    artifacts = [
        _artifact(root, relative_path, role)
        for relative_path, role in _artifact_specs()
    ]
    total_bytes = sum(cast(int, artifact["size_bytes"]) for artifact in artifacts)
    if total_bytes > _MAX_BUNDLE_BYTES:
        raise ValueError(f"evidence bundle exceeds {_MAX_BUNDLE_BYTES} bytes")
    payload: dict[str, object] = {
        "schema_version": 1,
        "bundle": _BUNDLE_NAME,
        "artifact_count": len(artifacts),
        "configuration_sha256": configuration_fingerprint(root),
        "artifacts": artifacts,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    payload["bundle_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def verify_evidence_manifest(
    project_root: Path, manifest_path: Path
) -> dict[str, object]:
    actual = _load_manifest(manifest_path)
    expected = build_evidence_manifest(project_root)
    if actual != expected:
        raise ValueError("evidence manifest mismatch: regenerate only after reviewing inputs")
    return {
        "verified": True,
        "artifact_count": expected["artifact_count"],
        "configuration_sha256": expected["configuration_sha256"],
        "bundle_sha256": expected["bundle_sha256"],
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("evidence manifest output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            newline="\n",
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify the traceable portfolio evidence manifest."
    )
    parser.add_argument("mode", choices=("generate", "verify"))
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = args.project_root.resolve(strict=True)
    canonical_manifest = project_root / "evidence" / "manifest.json"
    manifest = args.manifest or canonical_manifest
    if manifest.absolute() != canonical_manifest.absolute():
        raise ValueError("manifest must be the canonical evidence manifest")

    if args.mode == "generate":
        generated = build_evidence_manifest(project_root)
        _write_manifest(canonical_manifest, generated)
        print(
            "evidence manifest generated: "
            f"{generated['artifact_count']} artifacts, {generated['bundle_sha256']}"
        )
        return 0

    result = verify_evidence_manifest(project_root, canonical_manifest)
    print(
        "evidence manifest verified: "
        f"{result['artifact_count']} artifacts, {result['bundle_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
