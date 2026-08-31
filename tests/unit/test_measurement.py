import json
from pathlib import Path

from telconet_sentinel.measurement import main, parse_measurement_log

RAW_LOG = """\
TELCONET_MEASUREMENT version=1 capture_started_at=2026-08-30T17:21:26+09:00 \
ping_interval_ms=100 ping_count=8
ENV containerlab=0.79.0 docker_engine=29.1.3 frr=10.7.0 \
host=WSL2_Ubuntu_22.04 configuration_sha256=abc123
STATE_BEGIN name=baseline
Routing entry for 10.20.0.0/24
  Known via "ospf", distance 110, metric 30, best
traceroute to 10.20.0.10 (10.20.0.10), 8 hops max, 46 byte packets
 1  10.10.1.1  0.008 ms
 2  10.0.1.1  0.004 ms
 3  10.0.2.1  0.006 ms
 4  10.20.0.10  0.003 ms
STATE_END name=baseline
[1788078086.500000] 64 bytes from 10.20.0.10: icmp_seq=0 ttl=61 time=0.100 ms
[1788078086.600000] 64 bytes from 10.20.0.10: icmp_seq=1 ttl=61 time=0.100 ms
EVENT type=link_down epoch_ns=1788078086650000000
[1788078086.700000] 64 bytes from 10.20.0.10: icmp_seq=2 ttl=61 time=0.100 ms
[1788078086.800000] 64 bytes from 10.20.0.10: icmp_seq=3 ttl=60 time=0.100 ms
STATE_BEGIN name=failover
  Known via "ospf", distance 110, metric 140, best
 1  10.10.1.1  0.006 ms
 2  10.0.1.3  0.003 ms
 3  10.0.2.7  0.015 ms
 4  10.0.2.8  0.018 ms
 5  10.20.0.10  0.003 ms
STATE_END name=failover
[1788078086.900000] 64 bytes from 10.20.0.10: icmp_seq=4 ttl=60 time=0.100 ms
EVENT type=link_up epoch_ns=1788078087000000000
[1788078087.100000] 64 bytes from 10.20.0.10: icmp_seq=5 ttl=60 time=0.100 ms
[1788078087.200000] 64 bytes from 10.20.0.10: icmp_seq=6 ttl=60 time=0.100 ms
[1788078087.300000] 64 bytes from 10.20.0.10: icmp_seq=7 ttl=61 time=0.100 ms
STATE_BEGIN name=recovery
  Known via "ospf", distance 110, metric 30, best
 1  10.10.1.1  0.006 ms
 2  10.0.1.1  0.003 ms
 3  10.0.2.1  0.003 ms
 4  10.20.0.10  0.003 ms
STATE_END name=recovery
8 packets transmitted, 8 received, 0% packet loss, time 700ms
"""


def test_parses_loss_paths_metrics_and_transition_bounds() -> None:
    evidence = parse_measurement_log(RAW_LOG)

    assert evidence["measured"] is True
    assert evidence["environment"]["configuration_sha256"] == "abc123"
    assert evidence["baseline"]["ospf_metric"] == 30
    assert evidence["baseline"]["path"] == [
        "10.10.1.1",
        "10.0.1.1",
        "10.0.2.1",
        "10.20.0.10",
    ]
    assert evidence["failover"]["ospf_metric"] == 140
    assert evidence["failover"]["path_change_upper_bound_ms"] == 150
    assert evidence["failover"]["ping"]["packet_loss_percent"] == 0.0
    assert evidence["recovery"]["path_return_upper_bound_ms"] == 300


def test_rejects_incomplete_measurement_log() -> None:
    try:
        parse_measurement_log("TELCONET_MEASUREMENT version=1")
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("incomplete measurement log must be rejected")


def test_cli_recalculates_json_from_raw_log(tmp_path: Path) -> None:
    raw_path = tmp_path / "measurement.log"
    output_path = tmp_path / "measurement.json"
    raw_path.write_text(RAW_LOG, encoding="utf-8")

    assert main(["--raw-log", str(raw_path), "--output", str(output_path)]) == 0
    assert json.loads(output_path.read_text(encoding="utf-8")) == parse_measurement_log(RAW_LOG)
