from __future__ import annotations

import argparse
import re
import shlex
from collections import Counter
from pathlib import Path
from typing import Any

from .evidence import write_evidence

PING_REPLY = re.compile(
    r"^\[(?P<seconds>\d+)\.(?P<fraction>\d+)\].*"
    r"icmp_seq=(?P<seq>\d+) ttl=(?P<ttl>\d+) time=(?P<time>[\d.]+) ms"
)
PING_SUMMARY = re.compile(
    r"(?P<sent>\d+) packets transmitted, (?P<received>\d+) (?:packets )?received, "
    r"(?P<loss>[\d.]+)% packet loss"
)
ROUTE_METRIC = re.compile(r'Known via "ospf".* metric (?P<metric>\d+),')
TRACE_HOP = re.compile(r"^\s*\d+\s+(?P<address>\d+\.\d+\.\d+\.\d+)\s")


def _fields(line: str) -> dict[str, str]:
    return dict(token.split("=", 1) for token in shlex.split(line)[1:] if "=" in token)


def _timestamp_ns(seconds: str, fraction: str) -> int:
    nanoseconds = (fraction + "000000000")[:9]
    return int(seconds) * 1_000_000_000 + int(nanoseconds)


def parse_measurement_log(raw_log: str) -> dict[str, Any]:
    header: dict[str, str] | None = None
    environment: dict[str, str] | None = None
    states: dict[str, dict[str, Any]] = {}
    active_state: str | None = None
    replies: list[tuple[int, int, int, int]] = []
    events: dict[str, tuple[int, int]] = {}
    ping_summary: tuple[int, int, float] | None = None

    for line_index, line in enumerate(raw_log.splitlines()):
        if line.startswith("TELCONET_MEASUREMENT "):
            header = _fields(line)
        elif line.startswith("ENV "):
            environment = _fields(line)
        elif line.startswith("STATE_BEGIN "):
            active_state = _fields(line).get("name")
            if active_state:
                states[active_state] = {"path": []}
        elif line.startswith("STATE_END "):
            active_state = None
        elif line.startswith("EVENT "):
            event_fields = _fields(line)
            event_type = event_fields.get("type")
            epoch_ns = event_fields.get("epoch_ns")
            if event_type and epoch_ns:
                events[event_type] = (line_index, int(epoch_ns))
        else:
            reply = PING_REPLY.search(line)
            if reply:
                replies.append(
                    (
                        line_index,
                        int(reply["seq"]),
                        int(reply["ttl"]),
                        _timestamp_ns(reply["seconds"], reply["fraction"]),
                    )
                )
            summary = PING_SUMMARY.search(line)
            if summary:
                ping_summary = (
                    int(summary["sent"]),
                    int(summary["received"]),
                    float(summary["loss"]),
                )
            if active_state:
                metric = ROUTE_METRIC.search(line)
                if metric:
                    states[active_state]["ospf_metric"] = int(metric["metric"])
                hop = TRACE_HOP.search(line)
                if hop:
                    states[active_state]["path"].append(hop["address"])

    required_states = {"baseline", "failover", "recovery"}
    missing = []
    if header is None:
        missing.append("header")
    if environment is None:
        missing.append("environment")
    elif "configuration_sha256" not in environment:
        missing.append("configuration hash")
    if not required_states <= states.keys():
        missing.append("state blocks")
    if not {"link_down", "link_up"} <= events.keys():
        missing.append("event markers")
    if ping_summary is None or not replies:
        missing.append("ping results")
    if missing:
        raise ValueError(f"measurement log is missing: {', '.join(missing)}")

    assert header is not None
    assert environment is not None
    assert ping_summary is not None
    interval_ms = int(header["ping_interval_ms"])
    down_index, down_epoch_ns = events["link_down"]
    up_index, up_epoch_ns = events["link_up"]

    replies_before_down = [reply for reply in replies if reply[0] < down_index]
    replies_between_events = [reply for reply in replies if down_index < reply[0] < up_index]
    replies_after_up = [reply for reply in replies if reply[0] > up_index]
    if not replies_before_down or not replies_between_events or not replies_after_up:
        raise ValueError("measurement log is missing ping samples around event markers")

    baseline_ttl = Counter(reply[2] for reply in replies_before_down).most_common(1)[0][0]
    changed_reply = next(
        (reply for reply in replies_between_events if reply[2] != baseline_ttl), None
    )
    restored_reply = next((reply for reply in replies_after_up if reply[2] == baseline_ttl), None)
    if changed_reply is None or restored_reply is None:
        raise ValueError("measurement log is missing an observable path transition")
    failover_delta_ns = changed_reply[3] - down_epoch_ns
    recovery_delta_ns = restored_reply[3] - up_epoch_ns
    if failover_delta_ns < 0 or recovery_delta_ns < 0:
        raise ValueError("measurement reply timestamps must follow their event markers")

    sent, received, loss_percent = ping_summary
    return {
        "baseline": states["baseline"],
        "capture_started_at": header["capture_started_at"],
        "environment": {
            "containerlab": environment["containerlab"],
            "docker_engine": environment["docker_engine"],
            "frr": environment["frr"],
            "host": environment["host"].replace("_", " "),
            "configuration_sha256": environment["configuration_sha256"],
        },
        "failed_link": "access1--agg1",
        "failover": {
            **states["failover"],
            "event_epoch_ns": down_epoch_ns,
            "path_change_upper_bound_ms": (failover_delta_ns + 999_999) // 1_000_000,
            "ping": {
                "interval_ms": interval_ms,
                "packet_loss_percent": loss_percent,
                "received": received,
                "transmitted": sent,
            },
            "sampling_evidence": (
                f"link-down marker at {down_epoch_ns}; TTL changed from {baseline_ttl} "
                f"at sequence {changed_reply[1]} with reply timestamp {changed_reply[3]}"
            ),
        },
        "injection": {
            "command": "docker exec clab-telconet-sentinel-access1 ip link set eth1 down",
            "interface": "access1:eth1",
        },
        "limitations": [
            "Results were measured in local Linux network namespaces, not on carrier hardware.",
            (
                "Bounds compare event epoch markers with iputils -D reply timestamps; "
                "100 ms probing means the path may have changed earlier."
            ),
            (
                "Event markers are written before docker exec link commands, so measured "
                "deltas include command execution overhead."
            ),
            (
                "The failure was a local administrative link-down, so remote-fault "
                "detection time was not measured."
            ),
        ],
        "measured": True,
        "recovery": {
            "command": "docker exec clab-telconet-sentinel-access1 ip link set eth1 up",
            "event_epoch_ns": up_epoch_ns,
            "ospf_metric": states["recovery"]["ospf_metric"],
            "path": states["recovery"]["path"],
            "path_return_upper_bound_ms": (recovery_delta_ns + 999_999) // 1_000_000,
            "sampling_evidence": (
                f"link-up marker at {up_epoch_ns}; TTL returned to {baseline_ttl} "
                f"at sequence {restored_reply[1]} with reply timestamp {restored_reply[3]}"
            ),
        },
        "scenario": "access_uplink_failure",
        "source": "containerlab_observation",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build measured evidence from a raw lab log")
    parser.add_argument("--raw-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    evidence = parse_measurement_log(args.raw_log.read_text(encoding="utf-8"))
    write_evidence(args.output, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
