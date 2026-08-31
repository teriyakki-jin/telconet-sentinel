import json
from pathlib import Path

import pytest

from telconet_sentinel.bfd_comparison import (
    build_comparison,
    main,
    parse_detection_log,
)


def _raw_log(
    profile: str,
    detector: str,
    configured_ms: int,
    changed_at: str,
    changed_sequence: int,
) -> str:
    header = (
        "TELCONET_DETECTION_MEASUREMENT version=1 "
        f"profile={profile} detector={detector} "
        f"configured_detection_ms={configured_ms} "
        "capture_started_at=2026-08-31T13:00:00+09:00 "
        "configuration_sha256=abc123"
    )
    received = 3 if profile == "ospf_only" else 5
    loss = 50 if profile == "ospf_only" else 16.667
    return f"""\
{header}
STATE_BEGIN name=baseline
  Known via "ospf", distance 110, metric 30, best
 1  10.10.1.1  0.01 ms
 2  10.0.1.1  0.01 ms
 3  10.0.2.1  0.01 ms
 4  10.20.0.10  0.01 ms
STATE_END name=baseline
[100.000000] 64 bytes from 10.20.0.10: icmp_seq=1 ttl=61 time=0.1 ms
EVENT type=blackhole_start epoch_ns=100100000000
{'1234 10.0.1.0 10.0.1.1 up -' if profile == 'bfd_100x3' else ''}
[100.200000] 64 bytes from 10.20.0.10: icmp_seq=2 ttl=61 time=0.1 ms
[{changed_at}] 64 bytes from 10.20.0.10: icmp_seq={changed_sequence} ttl=60 time=0.1 ms
STATE_BEGIN name=failover
  Known via "ospf", distance 110, metric 140, best
 1  10.10.1.1  0.01 ms
 2  10.0.1.3  0.01 ms
 3  10.0.2.7  0.01 ms
 4  10.0.2.8  0.01 ms
 5  10.20.0.10  0.01 ms
STATE_END name=failover
EVENT type=blackhole_end epoch_ns=105000000000
6 packets transmitted, {received} received, {loss}% packet loss, time 5000ms
"""


OSPF_LOG = _raw_log("ospf_only", "ospf_dead_timer", 4000, "104.000000", 5)
BFD_LOG = _raw_log("bfd_100x3", "bfd", 300, "100.400000", 3)


def test_parses_remote_blackhole_detection_from_timestamps() -> None:
    result = parse_detection_log(OSPF_LOG)

    assert result["profile"] == "ospf_only"
    assert result["configured_detection_ms"] == 4000
    assert result["observed_detection_upper_bound_ms"] == 3900
    assert result["packets_lost_until_failover"] == 2
    assert result["capture_packet_loss_percent"] == 50.0
    assert result["baseline"]["ospf_metric"] == 30
    assert result["failover"]["ospf_metric"] == 140


def test_builds_ospf_and_bfd_comparison() -> None:
    comparison = build_comparison(OSPF_LOG, BFD_LOG)

    assert comparison["measured"] is True
    assert comparison["profiles"]["bfd_100x3"]["bfd_peer_up"] is True
    assert comparison["profiles"]["bfd_100x3"]["observed_detection_upper_bound_ms"] == 300
    assert comparison["improvement"]["detection_time_reduction_percent"] == 92.31
    assert comparison["improvement"]["lost_packets_until_failover_reduction_percent"] == 100.0


def test_rejects_bfd_profile_without_observed_up_session() -> None:
    missing_session = BFD_LOG.replace("1234 10.0.1.0 10.0.1.1 up -\n", "")

    with pytest.raises(ValueError, match="up BFD peer"):
        build_comparison(OSPF_LOG, missing_session)


def test_cli_writes_comparison_json(tmp_path: Path) -> None:
    ospf_path = tmp_path / "ospf.log"
    bfd_path = tmp_path / "bfd.log"
    output_path = tmp_path / "comparison.json"
    ospf_path.write_text(OSPF_LOG, encoding="utf-8")
    bfd_path.write_text(BFD_LOG, encoding="utf-8")

    assert main(
        [
            "--ospf-log",
            str(ospf_path),
            "--bfd-log",
            str(bfd_path),
            "--output",
            str(output_path),
        ]
    ) == 0
    assert json.loads(output_path.read_text(encoding="utf-8")) == build_comparison(
        OSPF_LOG, BFD_LOG
    )
