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


def render_experiment_metrics(evidence: Mapping[str, Any]) -> str:
    validate_experiment_evidence(evidence)
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
    return "\n".join(lines) + "\n"
