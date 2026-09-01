from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def load_experiment_evidence(path: Path) -> dict[str, Any]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ValueError("experiment evidence must be a JSON object")
    validate_experiment_evidence(evidence)
    return evidence


def validate_experiment_evidence(evidence: Mapping[str, Any]) -> None:
    profiles = evidence.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ValueError("experiment evidence profiles must be a mapping")
    for profile in ("ospf_only", "bfd_100x3"):
        values = profiles.get(profile)
        if not isinstance(values, Mapping):
            raise ValueError(f"experiment evidence requires profile: {profile}")
        required = {
            "observed_detection_upper_bound_ms": values.get(
                "observed_detection_upper_bound_ms"
            ),
            "packets_lost_until_failover": values.get("packets_lost_until_failover"),
            "capture_packet_loss_percent": values.get("capture_packet_loss_percent"),
        }
        for name, value in required.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{profile}.{name} must be numeric")
            if float(value) < 0:
                raise ValueError(f"{profile}.{name} must be non-negative")
        lost_packets = required["packets_lost_until_failover"]
        if not isinstance(lost_packets, int) or isinstance(lost_packets, bool):
            raise ValueError(f"{profile}.packets_lost_until_failover must be an integer")
        capture_loss = required["capture_packet_loss_percent"]
        assert isinstance(capture_loss, (int, float))
        if capture_loss > 100:
            raise ValueError(f"{profile}.capture_packet_loss_percent must not exceed 100")


def load_repeated_experiment_evidence(path: Path) -> dict[str, Any]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ValueError("repeated experiment evidence must be a JSON object")
    validate_repeated_experiment_evidence(evidence)
    return evidence


def validate_repeated_experiment_evidence(evidence: Mapping[str, Any]) -> None:
    trial_count = evidence.get("trial_count_per_profile")
    if not isinstance(trial_count, int) or isinstance(trial_count, bool):
        raise ValueError("repeated trial count must be an integer")
    if trial_count < 20:
        raise ValueError("repeated evidence requires at least 20 trials per profile")
    if trial_count > 30:
        raise ValueError("repeated evidence supports at most 30 trials per profile")

    profiles = evidence.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ValueError("repeated experiment profiles must be a mapping")
    for profile in ("ospf_only", "bfd_100x3"):
        values = profiles.get(profile)
        if not isinstance(values, Mapping):
            raise ValueError(f"repeated experiment requires profile: {profile}")
        summary = values.get("detection_ms")
        if not isinstance(summary, Mapping):
            raise ValueError(f"{profile}.detection_ms must be a mapping")
        for stat in ("p50", "p95", "max"):
            value = summary.get(stat)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{profile}.detection_ms.{stat} must be numeric")
            if float(value) < 0:
                raise ValueError(f"{profile}.detection_ms.{stat} must be non-negative")

        trials = values.get("trials")
        if not isinstance(trials, list) or len(trials) != trial_count:
            raise ValueError(f"{profile}.trials must contain {trial_count} trials")
        for expected_trial, trial in enumerate(trials, start=1):
            if not isinstance(trial, Mapping):
                raise ValueError(f"{profile}.trials entries must be mappings")
            if trial.get("trial") != expected_trial:
                raise ValueError(f"{profile}.trials must be sequential")
            detection = trial.get("detection_ms")
            if isinstance(detection, bool) or not isinstance(detection, (int, float)):
                raise ValueError(f"{profile}.trials detection_ms must be numeric")
            if float(detection) < 0:
                raise ValueError(f"{profile}.trials detection_ms must be non-negative")


def render_experiment_metrics(
    evidence: Mapping[str, Any],
    repeated_evidence: Mapping[str, Any] | None = None,
) -> str:
    validate_experiment_evidence(evidence)
    if repeated_evidence is not None:
        validate_repeated_experiment_evidence(repeated_evidence)
    lines = [
        "# HELP telconet_detection_seconds Observed failure detection upper bound.",
        "# TYPE telconet_detection_seconds gauge",
        "# HELP telconet_failover_lost_packets Packets lost before failover became observable.",
        "# TYPE telconet_failover_lost_packets gauge",
        "# HELP telconet_capture_packet_loss_ratio Packet loss ratio for the full capture.",
        "# TYPE telconet_capture_packet_loss_ratio gauge",
    ]
    profiles = evidence["profiles"]
    assert isinstance(profiles, Mapping)
    for profile in ("ospf_only", "bfd_100x3"):
        values = profiles.get(profile)
        assert isinstance(values, Mapping)
        detection_seconds = float(values["observed_detection_upper_bound_ms"]) / 1000
        lost_packets = int(values["packets_lost_until_failover"])
        capture_loss_ratio = float(values["capture_packet_loss_percent"]) / 100
        lines.append(f'telconet_detection_seconds{{profile="{profile}"}} {detection_seconds:g}')
        lines.append(f'telconet_failover_lost_packets{{profile="{profile}"}} {lost_packets}')
        lines.append(
            f'telconet_capture_packet_loss_ratio{{profile="{profile}"}} '
            f"{capture_loss_ratio:g}"
        )
    if repeated_evidence is not None:
        lines.extend(
            [
                "# HELP telconet_detection_summary_seconds Detection upper-bound "
                "summary across repeated trials.",
                "# TYPE telconet_detection_summary_seconds gauge",
                "# HELP telconet_trial_detection_seconds Detection upper bound for "
                "one repeated trial.",
                "# TYPE telconet_trial_detection_seconds gauge",
            ]
        )
        repeated_profiles = repeated_evidence["profiles"]
        assert isinstance(repeated_profiles, Mapping)
        for profile in ("ospf_only", "bfd_100x3"):
            repeated_values = repeated_profiles[profile]
            assert isinstance(repeated_values, Mapping)
            summary = repeated_values["detection_ms"]
            assert isinstance(summary, Mapping)
            for stat in ("p50", "p95", "max"):
                seconds = float(summary[stat]) / 1000
                lines.append(
                    "telconet_detection_summary_seconds"
                    f'{{profile="{profile}",stat="{stat}"}} {seconds:g}'
                )
            trials = repeated_values["trials"]
            assert isinstance(trials, list)
            for trial in trials:
                assert isinstance(trial, Mapping)
                trial_number = int(trial["trial"])
                seconds = float(trial["detection_ms"]) / 1000
                lines.append(
                    "telconet_trial_detection_seconds"
                    f'{{profile="{profile}",trial="{trial_number:02d}"}} {seconds:g}'
                )
    return "\n".join(lines) + "\n"
