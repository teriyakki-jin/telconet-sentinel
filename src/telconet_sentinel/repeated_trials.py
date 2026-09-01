from __future__ import annotations

import argparse
import math
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .bfd_comparison import parse_detection_log
from .evidence import write_evidence

NamedLog = tuple[str, str]


def _nearest_rank(values: Sequence[float | int], percentile: float) -> float | int:
    ordered = sorted(values)
    rank = math.ceil(percentile * len(ordered))
    return ordered[rank - 1]


def _summary(values: Sequence[float | int]) -> dict[str, float | int]:
    return {
        "min": min(values),
        "p50": statistics.median(values),
        "p95": _nearest_rank(values, 0.95),
        "max": max(values),
        "mean": round(statistics.mean(values), 2),
    }


def _profile_summary(profile: str, logs: Sequence[NamedLog]) -> dict[str, Any]:
    trials = []
    configuration_sha256: str | None = None
    for trial, (source, raw_log) in enumerate(logs, start=1):
        parsed = parse_detection_log(raw_log)
        if parsed["profile"] != profile:
            raise ValueError(f"expected {profile} log, got {parsed['profile']}")
        if profile == "bfd_100x3" and not parsed["bfd_peer_up"]:
            raise ValueError("BFD repeated trial requires an observed up BFD peer")
        if profile == "ospf_only" and parsed["bfd_peer_up"]:
            raise ValueError("OSPF-only repeated trial must not have an up BFD peer")
        if configuration_sha256 is None:
            configuration_sha256 = parsed["configuration_sha256"]
        elif parsed["configuration_sha256"] != configuration_sha256:
            raise ValueError("repeated trials must use one base configuration")
        trials.append(
            {
                "trial": trial,
                "source": source,
                "detection_ms": parsed["observed_detection_upper_bound_ms"],
                "lost_packets": parsed["packets_lost_until_failover"],
                "capture_loss_percent": parsed["capture_packet_loss_percent"],
            }
        )

    return {
        "configuration_sha256": configuration_sha256,
        "detection_ms": _summary([item["detection_ms"] for item in trials]),
        "lost_packets": _summary([item["lost_packets"] for item in trials]),
        "capture_loss_percent": _summary(
            [item["capture_loss_percent"] for item in trials]
        ),
        "trials": trials,
    }


def build_repeated_evidence(
    ospf_logs: Sequence[NamedLog], bfd_logs: Sequence[NamedLog]
) -> dict[str, Any]:
    if len(ospf_logs) != len(bfd_logs):
        raise ValueError("repeated profiles must contain the same number of trials")
    if len(ospf_logs) < 20:
        raise ValueError("repeated evidence requires at least 20 trials per profile")
    if len(ospf_logs) > 30:
        raise ValueError("repeated evidence supports at most 30 trials per profile")

    ospf = _profile_summary("ospf_only", ospf_logs)
    bfd = _profile_summary("bfd_100x3", bfd_logs)
    if ospf["configuration_sha256"] != bfd["configuration_sha256"]:
        raise ValueError("both repeated profiles must use one base configuration")

    ospf_p50 = float(ospf["detection_ms"]["p50"])
    ospf_p95 = float(ospf["detection_ms"]["p95"])
    bfd_p50 = float(bfd["detection_ms"]["p50"])
    bfd_p95 = float(bfd["detection_ms"]["p95"])
    return {
        "scenario": "remote_access_uplink_blackhole_repeated",
        "source": "containerlab_repeated_observation",
        "measured": True,
        "trial_count_per_profile": len(ospf_logs),
        "environment": {"configuration_sha256": ospf["configuration_sha256"]},
        "profiles": {"ospf_only": ospf, "bfd_100x3": bfd},
        "improvement": {
            "p50_detection_reduction_percent": round(
                (ospf_p50 - bfd_p50) / ospf_p50 * 100, 2
            ),
            "p95_detection_reduction_percent": round(
                (ospf_p95 - bfd_p95) / ospf_p95 * 100, 2
            ),
        },
        "limitations": [
            "Trials run sequentially in one local containerlab deployment.",
            "The 100 ms probe interval makes detection values upper bounds.",
            "Results are controlled local observations, not production-network SLOs.",
        ],
    }


def _named_logs(paths: Sequence[Path]) -> list[NamedLog]:
    return [
        (f"{path.parent.name}/{path.name}", path.read_text(encoding="utf-8"))
        for path in paths
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize repeated OSPF and BFD trials")
    parser.add_argument("--ospf-log", action="append", type=Path, required=True)
    parser.add_argument("--bfd-log", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    evidence = build_repeated_evidence(
        _named_logs(args.ospf_log),
        _named_logs(args.bfd_log),
    )
    write_evidence(args.output, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
