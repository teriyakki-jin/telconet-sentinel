from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .telemetry_config import (
    BoundTelemetryPeer,
    BoundTelemetryTarget,
    TelemetryTopologyBinding,
    load_telemetry_binding,
)
from .telemetry_models import (
    BfdPeerTelemetry,
    InterfaceTelemetry,
    OspfNeighborTelemetry,
    RouterTelemetry,
    RouteTelemetry,
    TelemetrySnapshot,
    TelemetryState,
)

COMMAND_TIMEOUT_SECONDS = 3.0
HTTP_TIMEOUT_SECONDS = 5.0

_INTERFACE_COMMAND = ("ip", "-s", "-j", "link", "show")
_OSPF_COMMAND = ("vtysh", "-c", "show ip ospf neighbor json")
_BFD_COMMAND = ("vtysh", "-c", "show bfd peers json")


def _load_json(output: str) -> object:
    try:
        return cast(object, json.loads(output))
    except json.JSONDecodeError as exc:
        raise ValueError("telemetry command returned invalid JSON") from exc


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(Sequence[object], value)
    return ()


def _nonnegative_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _state(value: object, *, up_values: tuple[str, ...] = ("up",)) -> TelemetryState:
    normalized = str(value).strip().lower()
    if any(normalized.startswith(expected) for expected in up_values):
        return TelemetryState.UP
    if normalized in {"", "unknown"}:
        return TelemetryState.UNKNOWN
    return TelemetryState.DOWN


def parse_interface_telemetry(
    output: str,
    expected_interfaces: tuple[str, ...],
) -> tuple[InterfaceTelemetry, ...]:
    document = _load_json(output)
    if not isinstance(document, list):
        raise ValueError("interface telemetry must be a JSON array")
    entries: dict[str, Mapping[str, object]] = {}
    for value in document:
        entry = _mapping(value)
        name = entry.get("ifname")
        if isinstance(name, str):
            entries[name] = entry

    observations: list[InterfaceTelemetry] = []
    for name in expected_interfaces:
        interface_entry = entries.get(name)
        if interface_entry is None:
            observations.append(
                InterfaceTelemetry(
                    name=name,
                    state=TelemetryState.UNKNOWN,
                    rx_bytes=0,
                    tx_bytes=0,
                    rx_drops=0,
                    tx_drops=0,
                )
            )
            continue
        stats = _mapping(interface_entry.get("stats64"))
        rx = _mapping(stats.get("rx"))
        tx = _mapping(stats.get("tx"))
        observations.append(
            InterfaceTelemetry(
                name=name,
                state=_state(interface_entry.get("operstate")),
                rx_bytes=_nonnegative_int(rx.get("bytes")),
                tx_bytes=_nonnegative_int(tx.get("bytes")),
                rx_drops=_nonnegative_int(rx.get("dropped")),
                tx_drops=_nonnegative_int(tx.get("dropped")),
            )
        )
    return tuple(observations)


def _expected_peers_by_interface(
    expected_peers: tuple[BoundTelemetryPeer, ...],
) -> dict[str, BoundTelemetryPeer]:
    return {expected.interface: expected for expected in expected_peers}


def _reject_unexpected_interface(
    interface: str,
    expected_by_interface: Mapping[str, BoundTelemetryPeer],
) -> None:
    if interface not in expected_by_interface:
        raise ValueError(f"unexpected protocol interface: {interface}")


def parse_ospf_neighbors(
    output: str,
    expected_peers: tuple[BoundTelemetryPeer, ...],
) -> tuple[OspfNeighborTelemetry, ...]:
    document = _mapping(_load_json(output))
    neighbors = _mapping(document.get("neighbors"))
    expected_by_interface = _expected_peers_by_interface(expected_peers)
    states_by_interface: dict[str, TelemetryState] = {}
    for values in neighbors.values():
        for value in _sequence(values):
            entry = _mapping(value)
            raw_interface = entry.get("ifaceName")
            if not isinstance(raw_interface, str) or not raw_interface:
                continue
            interface = raw_interface.split(":", 1)[0]
            _reject_unexpected_interface(interface, expected_by_interface)
            if interface in states_by_interface:
                raise ValueError(f"duplicate protocol interface: {interface}")
            raw_state = entry.get("converged", entry.get("nbrState"))
            states_by_interface[interface] = _state(
                raw_state,
                up_values=("full",),
            )
    return tuple(
        OspfNeighborTelemetry(
            neighbor=expected.peer,
            interface=expected.interface,
            state=states_by_interface.get(expected.interface, TelemetryState.DOWN),
        )
        for expected in expected_peers
    )


def _walk_mappings(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, object], value)
        yield mapping
        for nested in mapping.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def parse_bfd_peers(
    output: str,
    expected_peers: tuple[BoundTelemetryPeer, ...],
) -> tuple[BfdPeerTelemetry, ...]:
    document = _load_json(output)
    expected_by_interface = _expected_peers_by_interface(expected_peers)
    states_by_interface: dict[str, TelemetryState] = {}
    for entry in _walk_mappings(document):
        interface = entry.get("interface")
        if not isinstance(interface, str) or not (
            "peer" in entry or "status" in entry
        ):
            continue
        _reject_unexpected_interface(interface, expected_by_interface)
        if interface in states_by_interface:
            raise ValueError(f"duplicate protocol interface: {interface}")
        states_by_interface[interface] = _state(entry.get("status"))
    return tuple(
        BfdPeerTelemetry(
            peer=expected.peer,
            interface=expected.interface,
            configured=expected.interface in states_by_interface,
            state=states_by_interface.get(expected.interface, TelemetryState.UNKNOWN),
        )
        for expected in expected_peers
    )


def parse_selected_route(output: str, prefix: str) -> RouteTelemetry:
    document = _mapping(_load_json(output))
    entries = _sequence(document.get(prefix))
    selected: Mapping[str, object] | None = None
    for value in entries:
        candidate = _mapping(value)
        if candidate.get("selected") is True and candidate.get("installed") is True:
            selected = candidate
            break
    if selected is None:
        return RouteTelemetry(
            prefix=prefix,
            present=False,
            metric=None,
            egress_interface=None,
        )

    raw_metric = selected.get("metric")
    metric = (
        raw_metric
        if isinstance(raw_metric, int) and not isinstance(raw_metric, bool) and raw_metric >= 0
        else None
    )
    egress_interface: str | None = None
    for value in _sequence(selected.get("nexthops")):
        next_hop = _mapping(value)
        interface = next_hop.get("interfaceName")
        if (
            next_hop.get("active") is True
            and next_hop.get("fib") is True
            and isinstance(interface, str)
        ):
            egress_interface = interface
            break
    return RouteTelemetry(
        prefix=prefix,
        present=True,
        metric=metric,
        egress_interface=egress_interface,
    )


def failed_router_telemetry(name: str, error: str) -> RouterTelemetry:
    return RouterTelemetry(
        name=name,
        collection_success=False,
        state=TelemetryState.UNKNOWN,
        collection_error=error[:200],
        interfaces=(),
        ospf_neighbors=(),
        bfd_peers=(),
        routes=(),
    )


def _run_in_container(container: str, command: tuple[str, ...]) -> str:
    result = subprocess.run(
        ["docker", "exec", container, *command],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return result.stdout


def collect_router(target: BoundTelemetryTarget, service_prefix: str) -> RouterTelemetry:
    try:
        interfaces = parse_interface_telemetry(
            _run_in_container(target.container, _INTERFACE_COMMAND),
            target.interfaces,
        )
        ospf_neighbors = parse_ospf_neighbors(
            _run_in_container(target.container, _OSPF_COMMAND),
            target.protocol_peers,
        )
        bfd_peers = parse_bfd_peers(
            _run_in_container(target.container, _BFD_COMMAND),
            target.protocol_peers,
        )
        route = parse_selected_route(
            _run_in_container(
                target.container,
                ("vtysh", "-c", f"show ip route {service_prefix} json"),
            ),
            service_prefix,
        )
    except subprocess.TimeoutExpired:
        return failed_router_telemetry(target.node, "collection timed out")
    except subprocess.CalledProcessError:
        return failed_router_telemetry(target.node, "collection command failed")
    except (OSError, ValueError):
        return failed_router_telemetry(target.node, "invalid telemetry response")

    return RouterTelemetry(
        name=target.node,
        collection_success=True,
        state=TelemetryState.UP,
        collection_error=None,
        interfaces=interfaces,
        ospf_neighbors=ospf_neighbors,
        bfd_peers=bfd_peers,
        routes=(route,),
    )


def collect_snapshot(binding: TelemetryTopologyBinding) -> TelemetrySnapshot:
    routers: list[RouterTelemetry] = []
    for target in binding.targets:
        try:
            router = collect_router(target, binding.config.service_prefix)
        except Exception:  # isolate one router without reporting it as operationally down
            router = failed_router_telemetry(target.node, "collector error")
        routers.append(router)
    return TelemetrySnapshot(
        snapshot_id=str(uuid.uuid4()),
        observed_at=datetime.now(timezone.utc),
        topology_fingerprint=binding.fingerprint,
        routers=tuple(routers),
    )


class TelemetryApiClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def publish(self, snapshot: TelemetrySnapshot) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}/api/telemetry/snapshots",
            data=json.dumps(snapshot.model_dump(mode="json")).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"telemetry API rejected snapshot: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("telemetry API is unavailable") from exc
        if not isinstance(document, dict):
            raise RuntimeError("telemetry API response must be a JSON object")
        return cast(dict[str, Any], document)


def run_collector(
    binding: TelemetryTopologyBinding,
    api: TelemetryApiClient,
    *,
    once: bool,
    interval_seconds: float,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("telemetry interval must be greater than zero")
    while True:
        started = time.monotonic()
        snapshot = collect_snapshot(binding)
        try:
            api.publish(snapshot)
        except RuntimeError as exc:
            if once:
                raise
            print(f"telemetry publish failed: {exc}; retrying", file=sys.stderr, flush=True)
        else:
            print(
                f"published telemetry snapshot {snapshot.snapshot_id} "
                f"for {len(snapshot.routers)} routers",
                flush=True,
            )
            if once:
                return
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval_seconds - elapsed))


def _positive_interval(value: str) -> float:
    interval = float(value)
    if interval <= 0:
        raise argparse.ArgumentTypeError("interval must be greater than zero")
    return interval


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect typed telemetry from the FRR lab")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=_positive_interval)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config_path = cast(Path, args.config)
    project_root = config_path.parent.parent
    binding = load_telemetry_binding(
        config_path,
        project_root / "lab" / "intent.yml",
        project_root / "lab" / "telconet.clab.yml",
    )
    configured_interval = float(binding.config.poll_interval_seconds)
    interval = configured_interval if args.interval is None else float(args.interval)
    run_collector(
        binding,
        TelemetryApiClient(str(args.api_url)),
        once=bool(args.once),
        interval_seconds=interval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
