import json
from pathlib import Path

import pytest

from telconet_sentinel.metrics import load_experiment_evidence, render_experiment_metrics


def test_loads_experiment_evidence_from_json(tmp_path: Path) -> None:
    path = tmp_path / "experiment.json"
    expected = {
        "profiles": {
            profile: {
                "observed_detection_upper_bound_ms": 300,
                "packets_lost_until_failover": 2,
                "capture_packet_loss_percent": 1.5,
            }
            for profile in ("ospf_only", "bfd_100x3")
        }
    }
    path.write_text(json.dumps(expected), encoding="utf-8")

    assert load_experiment_evidence(path) == expected


def test_rejects_non_object_experiment_evidence(tmp_path: Path) -> None:
    path = tmp_path / "experiment.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_experiment_evidence(path)


def test_rejects_incomplete_experiment_profile_at_load_time(tmp_path: Path) -> None:
    path = tmp_path / "experiment.json"
    ospf = {
        "observed_detection_upper_bound_ms": 3900,
        "packets_lost_until_failover": 29,
        "capture_packet_loss_percent": 20,
    }
    path.write_text(json.dumps({"profiles": {"ospf_only": ospf}}), encoding="utf-8")

    with pytest.raises(ValueError, match="bfd_100x3"):
        load_experiment_evidence(path)


def test_rejects_negative_experiment_metric_at_load_time(tmp_path: Path) -> None:
    evidence = {
        "profiles": {
            profile: {
                "observed_detection_upper_bound_ms": -1,
                "packets_lost_until_failover": 0,
                "capture_packet_loss_percent": 0,
            }
            for profile in ("ospf_only", "bfd_100x3")
        }
    }
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(ValueError, match="non-negative"):
        load_experiment_evidence(path)


def test_renders_prometheus_metrics_for_both_detection_profiles() -> None:
    evidence = {
        "profiles": {
            "ospf_only": {
                "observed_detection_upper_bound_ms": 3900,
                "packets_lost_until_failover": 29,
                "capture_packet_loss_percent": 50.0,
            },
            "bfd_100x3": {
                "observed_detection_upper_bound_ms": 300,
                "packets_lost_until_failover": 3,
                "capture_packet_loss_percent": 16.667,
            },
        }
    }

    rendered = render_experiment_metrics(evidence)

    assert 'telconet_detection_seconds{profile="ospf_only"} 3.9' in rendered
    assert 'telconet_detection_seconds{profile="bfd_100x3"} 0.3' in rendered
    assert 'telconet_failover_lost_packets{profile="ospf_only"} 29' in rendered
    assert 'telconet_failover_lost_packets{profile="bfd_100x3"} 3' in rendered
    assert 'telconet_capture_packet_loss_ratio{profile="ospf_only"} 0.5' in rendered
    assert rendered.endswith("\n")


def test_renders_repeated_trial_distribution_and_summary_metrics() -> None:
    repeated = {
        "trial_count_per_profile": 20,
        "profiles": {
            profile: {
                "detection_ms": {"p50": p50, "p95": p95, "max": maximum},
                "trials": [
                    {"trial": trial, "detection_ms": p50 + trial}
                    for trial in range(1, 21)
                ],
            }
            for profile, p50, p95, maximum in (
                ("ospf_only", 3300, 3500, 3600),
                ("bfd_100x3", 330, 390, 410),
            )
        },
    }
    rendered = render_experiment_metrics(
        {
            "profiles": {
                profile: {
                    "observed_detection_upper_bound_ms": 300,
                    "packets_lost_until_failover": 2,
                    "capture_packet_loss_percent": 1.5,
                }
                for profile in ("ospf_only", "bfd_100x3")
            }
        },
        repeated,
    )

    assert (
        'telconet_detection_summary_seconds{profile="ospf_only",stat="p95"} 3.5'
        in rendered
    )
    assert (
        'telconet_trial_detection_seconds{profile="bfd_100x3",trial="01"} 0.331'
        in rendered
    )
