from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .evidence import write_evidence
from .measurement import PING_REPLY, PING_SUMMARY, ROUTE_METRIC, TRACE_HOP, _fields, _timestamp_ns

BFD_PEER_ROW = re.compile(
    r"^\s*\d+\s+\S+\s+(?P<peer>\S+)\s+(?P<status>up|down)\s+",
    re.IGNORECASE,
)


def parse_detection_log(raw_log: str) -> dict[str, Any]:
    header: dict[str, str] | None = None
    states: dict[str, dict[str, Any]] = {}
    active_state: str | None = None
    replies: list[tuple[int, int, int, int]] = []
    events: dict[str, tuple[int, int]] = {}
    ping_summary: tuple[int, int, float] | None = None
    bfd_peer_up = False

    for line_index, line in enumerate(raw_log.splitlines()):
        if line.startswith("TELCONET_DETECTION_MEASUREMENT "):
            header = _fields(line)
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
            bfd_peer = BFD_PEER_ROW.search(line)
            if bfd_peer and bfd_peer["peer"] == "10.0.1.1":
                bfd_peer_up = bfd_peer["status"].lower() == "up"
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

    missing = []
    if header is None:
        missing.append("header")
    if not {"baseline", "failover"} <= states.keys():
        missing.append("state blocks")
    if not {"blackhole_start", "blackhole_end"} <= events.keys():
        missing.append("event markers")
    if ping_summary is None or not replies:
        missing.append("ping results")
    if missing:
        raise ValueError(f"detection log is missing: {', '.join(missing)}")

    assert header is not None
    assert ping_summary is not None
    start_index, start_epoch_ns = events["blackhole_start"]
    end_index, _ = events["blackhole_end"]
    replies_before = [reply for reply in replies if reply[0] < start_index]
    replies_during = [reply for reply in replies if start_index < reply[0] < end_index]
    if not replies_before or not replies_during:
        raise ValueError("detection log is missing ping samples around the blackhole")

    baseline_ttl = Counter(reply[2] for reply in replies_before).most_common(1)[0][0]
    changed_reply = next((reply for reply in replies_during if reply[2] != baseline_ttl), None)
    if changed_reply is None:
        raise ValueError("detection log is missing an observable path transition")
    detection_delta_ns = changed_reply[3] - start_epoch_ns
    if detection_delta_ns < 0:
        raise ValueError("path-change reply timestamp must follow the blackhole event")

    sent, received, loss_percent = ping_summary
    last_pre_fault_sequence = replies_before[-1][1]
    received_until_failover = sum(reply[1] <= changed_reply[1] for reply in replies_during)
    expected_until_failover = changed_reply[1] - last_pre_fault_sequence
    lost_until_failover = expected_until_failover - received_until_failover
    if lost_until_failover < 0:
        raise ValueError("ping sequences around failover must be monotonically increasing")
    return {
        "profile": header["profile"],
        "detector": header["detector"],
        "configured_detection_ms": int(header["configured_detection_ms"]),
        "capture_started_at": header["capture_started_at"],
        "configuration_sha256": header["configuration_sha256"],
        "bfd_peer_up": bfd_peer_up,
        "observed_detection_upper_bound_ms": (detection_delta_ns + 999_999) // 1_000_000,
        "packets_lost_until_failover": lost_until_failover,
        "capture_packet_loss_percent": loss_percent,
        "packets_transmitted": sent,
        "packets_received": received,
        "baseline": states["baseline"],
        "failover": states["failover"],
        "fault": {
            "mode": "remote_packet_blackhole",
            "event_epoch_ns": start_epoch_ns,
            "first_changed_sequence": changed_reply[1],
            "first_changed_reply_epoch_ns": changed_reply[3],
        },
    }


def build_comparison(ospf_log: str, bfd_log: str) -> dict[str, Any]:
    ospf_result = parse_detection_log(ospf_log)
    bfd_result = parse_detection_log(bfd_log)
    if ospf_result["profile"] != "ospf_only" or bfd_result["profile"] != "bfd_100x3":
        raise ValueError("comparison requires ospf_only and bfd_100x3 profiles")
    if ospf_result["configuration_sha256"] != bfd_result["configuration_sha256"]:
        raise ValueError("comparison logs must use the same base configuration")
    if ospf_result["bfd_peer_up"]:
        raise ValueError("ospf_only profile must not have an up BFD peer")
    if not bfd_result["bfd_peer_up"]:
        raise ValueError("bfd_100x3 profile requires an observed up BFD peer")

    ospf_detection = ospf_result["observed_detection_upper_bound_ms"]
    bfd_detection = bfd_result["observed_detection_upper_bound_ms"]
    ospf_lost = ospf_result["packets_lost_until_failover"]
    bfd_lost = bfd_result["packets_lost_until_failover"]
    return {
        "scenario": "remote_access_uplink_blackhole",
        "source": "containerlab_observation",
        "measured": True,
        "profiles": {
            "ospf_only": ospf_result,
            "bfd_100x3": bfd_result,
        },
        "improvement": {
            "detection_time_reduction_percent": round(
                (ospf_detection - bfd_detection) / ospf_detection * 100, 2
            ),
            "lost_packets_until_failover_reduction_percent": round(
                (ospf_lost - bfd_lost) / ospf_lost * 100, 2
            )
            if ospf_lost
            else 0.0,
        },
        "limitations": [
            "The fault is emulated with tc netem in local Linux namespaces.",
            "Event markers precede tc commands, so observed bounds include command overhead.",
            "Results are a controlled comparison and are not production-network SLOs.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare OSPF and BFD blackhole detection")
    parser.add_argument("--ospf-log", type=Path, required=True)
    parser.add_argument("--bfd-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    comparison = build_comparison(
        args.ospf_log.read_text(encoding="utf-8"),
        args.bfd_log.read_text(encoding="utf-8"),
    )
    write_evidence(args.output, comparison)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
