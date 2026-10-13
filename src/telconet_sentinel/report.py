from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

from .bfd_comparison import build_comparison
from .config import load_topology
from .configuration import configuration_fingerprint
from .failure_domain import (
    audit_failure_domains,
    build_failure_domain_evidence,
    load_failure_domains,
    validate_failure_domain_design,
)
from .intent import load_design_catalog
from .metrics import validate_experiment_evidence, validate_repeated_experiment_evidence
from .repeated_trials import build_repeated_evidence

JsonObject = Mapping[str, Any]
_MAX_INPUT_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_RAW_LOG_BYTES = 10 * 1024 * 1024
_MAX_LIMITATIONS = 32
_MAX_TEXT_LENGTH = 512
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _bounded_read(path: Path, label: str) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError(
            f"{label} evidence exceeds {_MAX_INPUT_BYTES} bytes"
        )
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


def _load_json_object(path: Path, label: str) -> tuple[dict[str, Any], str]:
    raw = _bounded_read(path, label)
    value = json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} evidence must be a JSON object")
    normalized = raw.replace(b"\r\n", b"\n")
    return cast(dict[str, Any], value), hashlib.sha256(normalized).hexdigest()


def _mapping(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return cast(JsonObject, value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    return cast(Sequence[object], value)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _markdown_text(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{label} exceeds {_MAX_TEXT_LENGTH} characters")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} must not contain line breaks")
    escaped = html.escape(text, quote=False).replace("\\", "\\\\")
    for character in ("`", "|", "[", "]", "(", ")"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _html_code(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{label} exceeds {_MAX_TEXT_LENGTH} characters")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} must not contain line breaks")
    escaped = html.escape(text).replace("|", "&#124;")
    return f"<code>{escaped}</code>"


def _profile(evidence: JsonObject, name: str) -> JsonObject:
    profiles = _mapping(evidence.get("profiles"), "profiles")
    return _mapping(profiles.get(name), f"profiles.{name}")


def _format_number(value: object, label: str) -> str:
    number = _number(value, label)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _format_list(value: object, label: str) -> str:
    items = _sequence(value, label)
    if not items:
        return "—"
    return ", ".join(_markdown_text(item, f"{label} item") for item in items)


def _configuration_fingerprint(
    comparison: JsonObject,
    repeated: JsonObject,
    project_root: Path,
) -> str:
    fingerprints = [
        _text(
            _profile(comparison, profile).get("configuration_sha256"),
            f"comparison.{profile}.configuration_sha256",
        )
        for profile in ("ospf_only", "bfd_100x3")
    ]
    repeated_environment = _mapping(repeated.get("environment"), "repeated.environment")
    fingerprints.append(
        _text(
            repeated_environment.get("configuration_sha256"),
            "repeated.environment.configuration_sha256",
        )
    )
    fingerprints.extend(
        _text(
            _profile(repeated, profile).get("configuration_sha256"),
            f"repeated.{profile}.configuration_sha256",
        )
        for profile in ("ospf_only", "bfd_100x3")
    )
    if any(_SHA256_PATTERN.fullmatch(value) is None for value in fingerprints):
        raise ValueError("configuration fingerprint must be a lowercase SHA-256")
    if len(set(fingerprints)) != 1:
        raise ValueError("evidence uses more than one configuration fingerprint")
    current = configuration_fingerprint(project_root)
    if fingerprints[0] != current:
        raise ValueError("evidence configuration fingerprint does not match current lab")
    return current


def _validate_classification(
    comparison: JsonObject,
    repeated: JsonObject,
    failure_domains: JsonObject,
) -> None:
    if comparison.get("measured") is not True:
        raise ValueError("comparison evidence must be measured")
    if repeated.get("measured") is not True:
        raise ValueError("repeated evidence must be measured")
    if comparison.get("source") != "containerlab_observation":
        raise ValueError("comparison source must be containerlab_observation")
    if comparison.get("scenario") != "remote_access_uplink_blackhole":
        raise ValueError("comparison scenario must be remote_access_uplink_blackhole")
    if repeated.get("source") != "containerlab_repeated_observation":
        raise ValueError("repeated source must be containerlab_repeated_observation")
    if repeated.get("scenario") != "remote_access_uplink_blackhole_repeated":
        raise ValueError(
            "repeated scenario must be remote_access_uplink_blackhole_repeated"
        )
    if failure_domains.get("measured") is not False:
        raise ValueError("failure-domain evidence must be modeled, not measured")
    if failure_domains.get("source") != "deterministic_graph_analysis":
        raise ValueError("failure-domain evidence must use deterministic graph analysis")


def _read_raw_log(path: Path) -> str:
    return _bounded_read(path, f"raw log {path.name}").decode("utf-8")


def _validate_measured_evidence(
    comparison: JsonObject,
    repeated: JsonObject,
    project_root: Path,
) -> None:
    evidence_directory = project_root / "evidence"
    recomputed_comparison = build_comparison(
        _read_raw_log(evidence_directory / "remote-blackhole-ospf.log"),
        _read_raw_log(evidence_directory / "remote-blackhole-bfd.log"),
    )
    if comparison != recomputed_comparison:
        raise ValueError("comparison evidence does not match raw-log recalculation")

    expected_trials = _integer(
        repeated.get("trial_count_per_profile"),
        "repeated.trial_count_per_profile",
    )
    profile_paths: dict[str, list[Path]] = {}
    for profile in ("ospf_only", "bfd_100x3"):
        directory = evidence_directory / "repeated" / profile
        paths = sorted(directory.glob("trial-*.log"))
        if len(paths) != expected_trials:
            raise ValueError(
                f"expected {expected_trials} trial logs for {profile}, "
                f"found {len(paths)}"
            )
        profile_paths[profile] = paths

    total_raw_bytes = 0

    def named_logs(profile: str) -> list[tuple[str, str]]:
        nonlocal total_raw_bytes
        logs: list[tuple[str, str]] = []
        for path in profile_paths[profile]:
            raw_log = _read_raw_log(path)
            total_raw_bytes += len(raw_log.encode("utf-8"))
            if total_raw_bytes > _MAX_TOTAL_RAW_LOG_BYTES:
                raise ValueError(
                    "repeated raw logs exceed "
                    f"{_MAX_TOTAL_RAW_LOG_BYTES} bytes in total"
                )
            logs.append((f"{profile}/{path.name}", raw_log))
        return logs

    recomputed_repeated = build_repeated_evidence(
        named_logs("ospf_only"),
        named_logs("bfd_100x3"),
    )
    if repeated != recomputed_repeated:
        raise ValueError("repeated evidence does not match raw-log recalculation")


def _validate_failure_domain_evidence(
    evidence: JsonObject,
    project_root: Path,
) -> None:
    design_directory = project_root / "lab"
    designs = load_design_catalog(design_directory / "designs.yml")
    catalog = load_failure_domains(design_directory / "failure-domains.yml")
    validate_failure_domain_design(catalog, designs, design_directory)
    design = designs.designs[catalog.design]
    topology = load_topology(design_directory / design.intent)
    recomputed = build_failure_domain_evidence(
        audit_failure_domains(topology, catalog)
    )
    if evidence != recomputed:
        raise ValueError("failure-domain evidence does not match recomputed audit")


def _render_detection_table(repeated: JsonObject) -> list[str]:
    rows = []
    for profile, label in (
        ("ospf_only", "OSPF only"),
        ("bfd_100x3", "BFD 100 ms × 3"),
    ):
        summary = _mapping(
            _profile(repeated, profile).get("detection_ms"),
            f"repeated.{profile}.detection_ms",
        )
        rows.append(
            "| "
            + " | ".join(
                (
                    label,
                    f"{_format_number(summary.get('p50'), f'{profile}.p50')} ms",
                    f"{_format_number(summary.get('p95'), f'{profile}.p95')} ms",
                    f"{_format_number(summary.get('max'), f'{profile}.max')} ms",
                )
            )
            + " |"
        )
    return rows


def _render_single_run_table(comparison: JsonObject) -> list[str]:
    rows = []
    for profile, label in (
        ("ospf_only", "OSPF only"),
        ("bfd_100x3", "BFD 100 ms × 3"),
    ):
        values = _profile(comparison, profile)
        baseline = _mapping(values.get("baseline"), f"comparison.{profile}.baseline")
        failover = _mapping(values.get("failover"), f"comparison.{profile}.failover")
        rows.append(
            "| "
            + " | ".join(
                (
                    label,
                    _format_number(
                        values.get("observed_detection_upper_bound_ms"),
                        f"comparison.{profile}.detection",
                    )
                    + " ms",
                    str(
                        _integer(
                            values.get("packets_lost_until_failover"),
                            f"comparison.{profile}.lost_packets",
                        )
                    ),
                    f"{_integer(baseline.get('ospf_metric'), f'{profile}.baseline_metric')}"
                    " → "
                    f"{_integer(failover.get('ospf_metric'), f'{profile}.failover_metric')}",
                )
            )
            + " |"
        )
    return rows


def _render_failure_domain_table(failure_domains: JsonObject) -> list[str]:
    scenarios = _sequence(failure_domains.get("scenarios"), "failure_domains.scenarios")
    rows = []
    for index, value in enumerate(scenarios):
        scenario = _mapping(value, f"failure_domains.scenarios[{index}]")
        rows.append(
            "| "
            + " | ".join(
                (
                    f"`{_markdown_text(scenario.get('domain_id'), 'domain_id')}`",
                    _markdown_text(scenario.get("domain_type"), "domain_type"),
                    _markdown_text(
                        scenario.get("service_impact"), "service_impact"
                    ).upper(),
                    _format_list(scenario.get("affected_nodes"), "affected_nodes"),
                )
            )
            + " |"
        )
    return rows


def _limitations(comparison: JsonObject, repeated: JsonObject) -> list[str]:
    result: list[str] = []
    for label, evidence in (("comparison", comparison), ("repeated", repeated)):
        values = _sequence(evidence.get("limitations"), f"{label}.limitations")
        if len(values) > _MAX_LIMITATIONS:
            raise ValueError(
                f"{label}.limitations exceeds {_MAX_LIMITATIONS} entries"
            )
        for item in values:
            text = _markdown_text(item, f"{label}.limitations item")
            if ("production" in text.lower() or "slo" in text.lower()):
                continue
            if text not in result:
                result.append(text)
    result.append(
        "The SRLG section is deterministic graph analysis over declared failure "
        "domains; it is not a physical route-diversity survey."
    )
    return result


def render_report_from_paths(
    comparison_path: Path,
    repeated_path: Path,
    failure_domain_path: Path,
    *,
    project_root: Path,
) -> str:
    comparison, comparison_sha256 = _load_json_object(
        comparison_path, "comparison"
    )
    validate_experiment_evidence(comparison)
    repeated, repeated_sha256 = _load_json_object(repeated_path, "repeated")
    validate_repeated_experiment_evidence(repeated)
    failure_domains, failure_domain_sha256 = _load_json_object(
        failure_domain_path, "failure-domain"
    )
    _validate_classification(comparison, repeated, failure_domains)
    fingerprint = _configuration_fingerprint(comparison, repeated, project_root)
    _validate_measured_evidence(comparison, repeated, project_root)
    _validate_failure_domain_evidence(failure_domains, project_root)
    design_name = _text(failure_domains.get("design"), "failure_domains.design")
    topology_sha256 = _text(
        failure_domains.get("topology_sha256"),
        "failure_domains.topology_sha256",
    )

    trial_count = _integer(repeated.get("trial_count_per_profile"), "trial_count")
    improvement = _mapping(repeated.get("improvement"), "repeated.improvement")
    p95_reduction_value = _number(
        improvement.get("p95_detection_reduction_percent"),
        "p95_detection_reduction_percent",
    )
    p95_reduction = _format_number(
        abs(p95_reduction_value), "p95_detection_reduction_percent"
    )
    if p95_reduction_value > 0:
        detection_summary = (
            f"Reduced the p95 detection upper bound by **{p95_reduction}%** "
            "with BFD 100 ms × 3."
        )
    elif p95_reduction_value < 0:
        detection_summary = (
            f"Increased the p95 detection upper bound by **{p95_reduction}%** "
            "with BFD 100 ms × 3."
        )
    else:
        detection_summary = (
            "Did not change the p95 detection upper bound with BFD 100 ms × 3 "
            "(**0%** change)."
        )
    ospf_lost = _mapping(
        _profile(repeated, "ospf_only").get("lost_packets"),
        "repeated.ospf_only.lost_packets",
    )
    bfd_lost = _mapping(
        _profile(repeated, "bfd_100x3").get("lost_packets"),
        "repeated.bfd_100x3.lost_packets",
    )
    ospf_lost_p95 = _number(ospf_lost.get("p95"), "ospf lost p95")
    bfd_lost_p95 = _number(bfd_lost.get("p95"), "bfd lost p95")
    if bfd_lost_p95 < ospf_lost_p95:
        packet_loss_summary = (
            "Reduced p95 packets lost before observable failover from "
            f"**{_format_number(ospf_lost_p95, 'ospf lost p95')}** to "
            f"**{_format_number(bfd_lost_p95, 'bfd lost p95')}**."
        )
    elif bfd_lost_p95 > ospf_lost_p95:
        packet_loss_summary = (
            "Increased p95 packets lost before observable failover from "
            f"**{_format_number(ospf_lost_p95, 'ospf lost p95')}** to "
            f"**{_format_number(bfd_lost_p95, 'bfd lost p95')}**."
        )
    else:
        packet_loss_summary = (
            "Did not change p95 packets lost before observable failover; "
            f"both profiles recorded **{_format_number(ospf_lost_p95, 'lost p95')}**."
        )

    baseline_metrics = {
        _integer(
            _mapping(
                _profile(comparison, profile).get("baseline"),
                f"comparison.{profile}.baseline",
            ).get("ospf_metric"),
            f"comparison.{profile}.baseline.ospf_metric",
        )
        for profile in ("ospf_only", "bfd_100x3")
    }
    failover_metrics = {
        _integer(
            _mapping(
                _profile(comparison, profile).get("failover"),
                f"comparison.{profile}.failover",
            ).get("ospf_metric"),
            f"comparison.{profile}.failover.ospf_metric",
        )
        for profile in ("ospf_only", "bfd_100x3")
    }
    if len(baseline_metrics) != 1 or len(failover_metrics) != 1:
        raise ValueError("comparison profiles must use the same path metrics")
    baseline_metric = next(iter(baseline_metrics))
    failover_metric = next(iter(failover_metrics))
    outage_count = _integer(
        _mapping(failure_domains.get("summary"), "failure_domains.summary").get(
            "outage"
        ),
        "failure_domains.summary.outage",
    )

    lines = [
        "<!-- Generated by telconet-report; do not edit manually. -->",
        "# Network Resilience Experiment Report",
        "",
        "This report is generated from versioned evidence. It keeps measured "
        "containerlab observations separate from deterministic topology analysis.",
        "",
        "## Executive summary",
        "",
        f"- Compared OSPF-only and BFD failure detection across **{trial_count} "
        "trials per profile**.",
        f"- {detection_summary}",
        f"- {packet_loss_summary}",
        f"- Found **{outage_count}** declared shared-risk outage scenario in "
        f"candidate `{_markdown_text(design_name, 'design')}`.",
        "",
        "## Evidence classification",
        "",
        "| Evidence | Classification | Meaning |",
        "|---|---|---|",
        "| Single-run OSPF/BFD comparison | Measured in containerlab | One "
        "controlled carrier-up blackhole observation per profile |",
        "| Repeated OSPF/BFD trials | Measured in containerlab | Distribution "
        f"across {trial_count} sequential observations per profile |",
        "| SRLG audit | Modeled from declared failure domains | Deterministic "
        "reachability and path-cost calculation |",
        "",
        "## Repeated detection experiment",
        "",
        "| Profile | p50 upper bound | p95 upper bound | Maximum upper bound |",
        "|---|---:|---:|---:|",
        *_render_detection_table(repeated),
        "",
        "The 100 ms probe interval makes these values observation upper bounds, "
        "not protocol-internal processing times.",
        "",
        "## Single-run path transition",
        "",
        "| Profile | Detection upper bound | Lost packets before failover | OSPF metric |",
        "|---|---:|---:|---:|",
        *_render_single_run_table(comparison),
        "",
        f"Both runs started on metric `{baseline_metric}` and converged to the "
        f"intended backup path at metric `{failover_metric}` after the same remote "
        "packet blackhole was injected.",
        "",
        "## Shared-risk audit",
        "",
        "| Failure domain | Type | Impact | Affected access nodes |",
        "|---|---|---|---|",
        *_render_failure_domain_table(failure_domains),
        "",
        "Passing link or node N-1 does not establish physical path independence. "
        "The `service-entry` result demonstrates the remaining common-risk boundary.",
        "",
        "## Provenance",
        "",
        f"Configuration fingerprint: `{fingerprint}`",
        f"Candidate topology fingerprint: `{topology_sha256}`",
        "",
        "The hashes below are LF-normalized content-integrity digests, not "
        "signatures or proofs of authorship.",
        "",
        "| Input | SHA-256 |",
        "|---|---|",
        f"| {_html_code(comparison_path.name, 'comparison filename')} "
        f"| `{comparison_sha256}` |",
        f"| {_html_code(repeated_path.name, 'repeated filename')} "
        f"| `{repeated_sha256}` |",
        f"| {_html_code(failure_domain_path.name, 'failure-domain filename')} "
        f"| `{failure_domain_sha256}` |",
        "",
        "Regenerate the report:",
        "",
        "```bash",
        "telconet-report --output docs/EXPERIMENT_REPORT.md",
        "```",
        "",
        "## Interpretation boundaries",
        "",
        *[f"- {limitation}" for limitation in _limitations(comparison, repeated)],
        "- Results apply to this local lab topology and configuration; they are "
        "not a production SLA.",
        "",
    ]
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a traceable Markdown report from versioned evidence."
    )
    parser.add_argument(
        "--comparison",
        type=Path,
        default=Path("evidence/bfd-comparison.json"),
    )
    parser.add_argument(
        "--repeated",
        type=Path,
        default=Path("evidence/bfd-repeated-trials.json"),
    )
    parser.add_argument(
        "--failure-domains",
        type=Path,
        default=Path("evidence/failure-domain-audit.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/EXPERIMENT_REPORT.md"),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path("."),
        help="Repository root used to verify current lab inputs.",
    )
    return parser


def _write_report(path: Path, report: str) -> None:
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
            temporary.write(report)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_output_path(
    output: Path,
    direct_inputs: Sequence[Path],
    project_root: Path,
) -> None:
    resolved_output = output.resolve()
    if resolved_output in {path.resolve() for path in direct_inputs}:
        raise ValueError("output must not overwrite an input evidence file")

    provenance_inputs = {
        path.resolve()
        for directory in (project_root / "evidence", project_root / "lab")
        if directory.is_dir()
        for path in directory.rglob("*")
        if path.is_file()
    }
    if resolved_output in provenance_inputs:
        raise ValueError("output must not overwrite a provenance input")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    input_paths = (args.comparison, args.repeated, args.failure_domains)
    project_root = args.project_root.resolve()
    _validate_output_path(args.output, input_paths, project_root)
    report = render_report_from_paths(
        args.comparison,
        args.repeated,
        args.failure_domains,
        project_root=project_root,
    )
    _write_report(args.output, report)
    print(f"wrote experiment report to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
