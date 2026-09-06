from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _walk(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _load_json(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("FRR output must be valid JSON") from exc


def parse_bfd_peer_up(output: str, peer: str) -> bool:
    document = _load_json(output)
    for values in _walk(document):
        if values.get("peer") == peer and str(values.get("status", "")).lower() == "up":
            return True
        if peer in values:
            for peer_values in _walk(values[peer]):
                if str(peer_values.get("status", "")).lower() == "up":
                    return True
    return False


def parse_ospf_neighbor_full(output: str, peer: str) -> bool:
    document = _load_json(output)
    address_fields = ("address", "ifaceAddress", "neighborAddress", "peer")
    state_fields = ("nbrState", "state")
    for values in _walk(document):
        addresses = {str(values.get(field, "")) for field in address_fields}
        if peer not in addresses:
            continue
        if any(str(values.get(field, "")).lower().startswith("full") for field in state_fields):
            return True
    return False


def parse_route_metric(output: str) -> int:
    try:
        document = json.loads(output)
    except json.JSONDecodeError:
        document = None
    if document is not None:
        candidates = [
            values
            for values in _walk(document)
            if isinstance(values.get("metric"), int)
            and not isinstance(values.get("metric"), bool)
        ]
        selected = next((values for values in candidates if values.get("selected") is True), None)
        if selected is not None:
            return int(selected["metric"])
        if candidates:
            return int(candidates[0]["metric"])
    match = re.search(r"\bmetric\s+(\d+)\b", output)
    if match is None:
        raise ValueError("FRR output does not contain a route metric")
    return int(match.group(1))


@dataclass(frozen=True, slots=True)
class PingRecovery:
    sequence: int
    observed_ns: int


class PingRecoveryTracker:
    _reply_pattern = re.compile(r"\b\d+\s+bytes from .+\b(?:icmp_)?seq=(\d+)\b")

    def __init__(self) -> None:
        self._last_sequence: int | None = None
        self._injected = False
        self._recovered = False

    def observe_reply(self, line: str, observed_ns: int) -> PingRecovery | None:
        match = self._reply_pattern.search(line)
        if match is None:
            return None
        sequence = int(match.group(1))
        previous = self._last_sequence
        self._last_sequence = sequence
        if not self._injected or self._recovered or previous is None:
            return None
        if sequence <= previous + 1:
            return None
        self._recovered = True
        return PingRecovery(sequence=sequence, observed_ns=observed_ns)

    def mark_injected(self) -> None:
        self._injected = True

    @property
    def last_sequence(self) -> int | None:
        return self._last_sequence


@dataclass(frozen=True, slots=True)
class LiveSample:
    bfd_up: bool
    ospf_full: bool
    route_metric: int
    icmp_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class LiveTransition:
    event: str
    offset_ms: int
    route_metric: int | None = None
    icmp_sequence: int | None = None


class LiveTransitionDetector:
    def __init__(self, failover_metric: int = 140) -> None:
        self._failover_metric = failover_metric
        self._recorded: set[str] = set()

    def observe(self, sample: LiveSample, offset_ms: int) -> list[LiveTransition]:
        candidates = (
            (not sample.bfd_up, LiveTransition("bfd_down", offset_ms)),
            (not sample.ospf_full, LiveTransition("ospf_neighbor_down", offset_ms)),
            (
                sample.route_metric == self._failover_metric,
                LiveTransition("route_failover", offset_ms, route_metric=sample.route_metric),
            ),
            (
                sample.icmp_sequence is not None,
                LiveTransition(
                    "data_plane_recovered",
                    offset_ms,
                    icmp_sequence=sample.icmp_sequence,
                ),
            ),
        )
        transitions = []
        for observed, candidate in candidates:
            if candidate.event in self._recorded:
                continue
            if not observed:
                break
            transitions.append(candidate)
            self._recorded.add(candidate.event)
        return transitions


class LiveApiClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def create_run(self, profile: str, source: str, target: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/convergence-runs",
            {
                "profile": profile,
                "source": source,
                "target": target,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def record_event(self, run_id: str, transition: LiveTransition) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": transition.event,
            "offset_ms": transition.offset_ms,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if transition.route_metric is not None:
            payload["route_metric"] = transition.route_metric
        if transition.icmp_sequence is not None:
            payload["icmp_sequence"] = transition.icmp_sequence
        return self._request(
            "POST",
            f"/api/convergence-runs/{run_id}/events",
            payload,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/convergence-runs/{run_id}")

    def health(self) -> None:
        self._request("GET", "/health")

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(dict(payload)).encode()
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                document = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"live API rejected {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"live API is unavailable: {exc.reason}") from exc
        if not isinstance(document, dict):
            raise RuntimeError("live API response must be a JSON object")
        return document


class DockerFrrProbe:
    def __init__(self, router: str, peer: str, route_prefix: str) -> None:
        self._router = router
        self._peer = peer
        self._route_prefix = route_prefix

    def sample(self, icmp_sequence: int | None = None) -> LiveSample:
        bfd = self._vtysh("show bfd peers json")
        ospf = self._vtysh("show ip ospf neighbor json")
        route = self._vtysh(f"show ip route {self._route_prefix} json")
        return LiveSample(
            bfd_up=parse_bfd_peer_up(bfd, self._peer),
            ospf_full=parse_ospf_neighbor_full(ospf, self._peer),
            route_metric=parse_route_metric(route),
            icmp_sequence=icmp_sequence,
        )

    def _vtysh(self, command: str) -> str:
        result = subprocess.run(
            ["docker", "exec", self._router, "vtysh", "-c", command],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout


def _start_ping(
    container: str,
    target: str,
) -> tuple[subprocess.Popen[str], queue.Queue[tuple[str, int]]]:
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            container,
            "ping",
            "-n",
            "-i",
            "0.1",
            "-W",
            "1",
            target,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output: queue.Queue[tuple[str, int]] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.put((line, time.monotonic_ns()))

    threading.Thread(target=read_output, daemon=True).start()
    return process, output


def _drain_ping(
    output: queue.Queue[tuple[str, int]],
    tracker: PingRecoveryTracker,
) -> PingRecovery | None:
    recovery = None
    while True:
        try:
            line, observed_ns = output.get_nowait()
        except queue.Empty:
            return recovery
        detected = tracker.observe_reply(line, observed_ns)
        if detected is not None:
            recovery = detected


def _run_command(*command: str) -> None:
    subprocess.run(list(command), check=True)


def _wait_for_ping_baseline(
    output: queue.Queue[tuple[str, int]],
    tracker: PingRecoveryTracker,
    timeout_seconds: float = 5,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _drain_ping(output, tracker)
        if tracker.last_sequence is not None:
            return
        time.sleep(0.05)
    raise RuntimeError("ICMP baseline was not observed before fault injection")


def collect_live_convergence(
    *,
    api: LiveApiClient,
    probe: DockerFrrProbe,
    client_container: str,
    fault_container: str,
    fault_interface: str,
    source: str,
    target: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    api.health()
    baseline = probe.sample()
    if not baseline.bfd_up or not baseline.ospf_full or baseline.route_metric != 30:
        raise RuntimeError(
            "live baseline requires BFD up, OSPF Full, and service route metric 30"
        )

    ping_process, ping_output = _start_ping(client_container, target)
    tracker = PingRecoveryTracker()
    try:
        _wait_for_ping_baseline(ping_output, tracker)
        run = api.create_run("bfd_100x3", source, target)
        run_id = str(run["id"])
        _run_command(
            "docker",
            "exec",
            fault_container,
            "tc",
            "qdisc",
            "add",
            "dev",
            fault_interface,
            "clsact",
        )
        _run_command(
            "docker",
            "exec",
            fault_container,
            "tc",
            "filter",
            "add",
            "dev",
            fault_interface,
            "ingress",
            "protocol",
            "all",
            "pref",
            "1",
            "flower",
            "action",
            "drop",
        )
        fault_ns = time.monotonic_ns()
        tracker.mark_injected()
        api.record_event(run_id, LiveTransition("blackhole_injected", 0))
        detector = LiveTransitionDetector()
        recorded: set[str] = set()
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            recovery = _drain_ping(ping_output, tracker)
            sequence = recovery.sequence if recovery is not None else None
            offset_ms = max(0, round((time.monotonic_ns() - fault_ns) / 1_000_000))
            sample = probe.sample(icmp_sequence=sequence)
            transitions = detector.observe(sample, offset_ms)
            for transition in transitions:
                api.record_event(run_id, transition)
                recorded.add(transition.event)
                print(
                    f"LIVE_EVENT event={transition.event} offset_ms={transition.offset_ms}",
                    flush=True,
                )
            if recorded == {
                "bfd_down",
                "ospf_neighbor_down",
                "route_failover",
                "data_plane_recovered",
            }:
                result = api.get_run(run_id)
                if result.get("status") != "complete":
                    raise RuntimeError("live API did not mark the convergence run complete")
                return result
            time.sleep(poll_interval_seconds)
        missing = sorted(
            {
                "bfd_down",
                "ospf_neighbor_down",
                "route_failover",
                "data_plane_recovered",
            }
            - recorded
        )
        raise RuntimeError(f"live convergence timed out; missing events: {', '.join(missing)}")
    finally:
        subprocess.run(
            [
                "docker",
                "exec",
                fault_container,
                "tc",
                "qdisc",
                "del",
                "dev",
                fault_interface,
                "clsact",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ping_process.terminate()
        try:
            ping_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            ping_process.kill()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect a live FRR convergence timeline")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--router", default="clab-telconet-sentinel-access1")
    parser.add_argument("--client", default="clab-telconet-sentinel-client-a")
    parser.add_argument("--fault-node", default="clab-telconet-sentinel-agg1")
    parser.add_argument("--fault-interface", default="eth1")
    parser.add_argument("--peer", default="10.0.1.1")
    parser.add_argument("--route-prefix", default="10.20.0.0/24")
    parser.add_argument("--source", default="access1")
    parser.add_argument("--target", default="10.20.0.10")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    result = collect_live_convergence(
        api=LiveApiClient(args.api_url),
        probe=DockerFrrProbe(args.router, args.peer, args.route_prefix),
        client_container=args.client,
        fault_container=args.fault_node,
        fault_interface=args.fault_interface,
        source=args.source,
        target=args.target,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Live convergence evidence: {args.output}")


if __name__ == "__main__":
    main()
