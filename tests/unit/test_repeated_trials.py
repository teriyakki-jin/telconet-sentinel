from pathlib import Path

import pytest

from telconet_sentinel.repeated_trials import build_repeated_evidence, main


def _raw_log(profile: str, detection_ms: int, lost_packets: int, config: str = "abc") -> str:
    detector = "bfd" if profile == "bfd_100x3" else "ospf_dead_timer"
    configured_ms = 300 if profile == "bfd_100x3" else 4000
    changed_seconds = 100.1 + detection_ms / 1000
    changed_sequence = lost_packets + 2
    received = 80 - lost_packets
    loss_percent = lost_packets / 80 * 100
    bfd_row = "1234 10.0.1.0 10.0.1.1 up -" if profile == "bfd_100x3" else ""
    header = (
        "TELCONET_DETECTION_MEASUREMENT version=1 "
        f"profile={profile} detector={detector} "
        f"configured_detection_ms={configured_ms} "
        "capture_started_at=2026-08-31T13:00:00+09:00 "
        f"configuration_sha256={config}"
    )
    return f"""\
{header}
STATE_BEGIN name=baseline
  Known via "ospf", distance 110, metric 30, best
 1  10.10.1.1  0.01 ms
STATE_END name=baseline
[100.000000] 64 bytes from 10.20.0.10: icmp_seq=1 ttl=61 time=0.1 ms
EVENT type=blackhole_start epoch_ns=100100000000
{bfd_row}
[{changed_seconds:.6f}] 64 bytes from 10.20.0.10: icmp_seq={changed_sequence} ttl=60 time=0.1 ms
STATE_BEGIN name=failover
  Known via "ospf", distance 110, metric 140, best
 1  10.10.1.1  0.01 ms
STATE_END name=failover
EVENT type=blackhole_end epoch_ns=130000000000
80 packets transmitted, {received} received, {loss_percent:g}% packet loss, time 8000ms
"""


def _twenty_logs(profile: str, multiplier: int) -> list[tuple[str, str]]:
    return [
        (
            f"{profile}/trial-{trial:02d}.log",
            _raw_log(profile, trial * multiplier, trial),
        )
        for trial in range(1, 21)
    ]


def test_builds_p50_p95_max_and_per_trial_distribution() -> None:
    evidence = build_repeated_evidence(
        _twenty_logs("ospf_only", 1000),
        _twenty_logs("bfd_100x3", 100),
    )

    assert evidence["trial_count_per_profile"] == 20
    assert evidence["profiles"]["ospf_only"]["detection_ms"] == {
        "min": 1000,
        "p50": 10500.0,
        "p95": 19000,
        "max": 20000,
        "mean": 10500.0,
    }
    assert evidence["profiles"]["bfd_100x3"]["detection_ms"]["p95"] == 1900
    assert evidence["profiles"]["ospf_only"]["trials"][0] == {
        "trial": 1,
        "source": "ospf_only/trial-01.log",
        "detection_ms": 1000,
        "lost_packets": 1,
        "capture_loss_percent": 1.25,
    }
    assert evidence["improvement"]["p50_detection_reduction_percent"] == 90.0
    assert evidence["improvement"]["p95_detection_reduction_percent"] == 90.0


def test_rejects_unbalanced_or_too_few_trials() -> None:
    ospf = _twenty_logs("ospf_only", 1000)
    bfd = _twenty_logs("bfd_100x3", 100)

    with pytest.raises(ValueError, match="same number"):
        build_repeated_evidence(ospf, bfd[:-1])
    with pytest.raises(ValueError, match="at least 20"):
        build_repeated_evidence(ospf[:19], bfd[:19])
    with pytest.raises(ValueError, match="at most 30"):
        build_repeated_evidence(ospf + ospf[:11], bfd + bfd[:11])


def test_cli_reads_repeated_log_arguments_and_writes_json(tmp_path: Path) -> None:
    ospf_paths = []
    bfd_paths = []
    for profile, multiplier, paths in (
        ("ospf_only", 1000, ospf_paths),
        ("bfd_100x3", 100, bfd_paths),
    ):
        for source, raw_log in _twenty_logs(profile, multiplier):
            path = tmp_path / source
            path.parent.mkdir(exist_ok=True)
            path.write_text(raw_log, encoding="utf-8")
            paths.append(path)
    output = tmp_path / "repeated.json"
    args = ["--output", str(output)]
    for path in ospf_paths:
        args.extend(("--ospf-log", str(path)))
    for path in bfd_paths:
        args.extend(("--bfd-log", str(path)))

    assert main(args) == 0
    assert '"trial_count_per_profile": 20' in output.read_text(encoding="utf-8")
