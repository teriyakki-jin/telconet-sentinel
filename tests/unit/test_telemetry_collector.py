from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import telconet_sentinel.telemetry_collector as collector
from telconet_sentinel.models import NodeRole
from telconet_sentinel.telemetry_config import (
    BoundTelemetryPeer,
    BoundTelemetryTarget,
    TelemetryConfig,
    TelemetryLink,
    TelemetryTarget,
    TelemetryTopologyBinding,
)
from telconet_sentinel.telemetry_models import (
    BASELINE_ROUTERS,
    RouterTelemetry,
    TelemetrySnapshot,
    TelemetryState,
)


def _binding(*targets: BoundTelemetryTarget) -> TelemetryTopologyBinding:
    targets_by_name = {target.node: target for target in targets}
    all_targets = tuple(
        targets_by_name.get(node, _target(node)) for node in BASELINE_ROUTERS
    )
    return TelemetryTopologyBinding(
        config=TelemetryConfig(
            version=1,
            poll_interval_seconds=1,
            service_prefix="10.20.0.0/24",
            routers=tuple(
                TelemetryTarget(node=target.node, container=target.container)
                for target in all_targets
            ),
        ),
        fingerprint="a" * 64,
        targets=all_targets,
        links=(
            TelemetryLink(
                id="access1--agg1",
                source="access1",
                source_interface="eth1",
                target="agg1",
                target_interface="eth1",
            ),
        ),
    )


def _target(node: str = "access1") -> BoundTelemetryTarget:
    return BoundTelemetryTarget(
        node=node,
        container=f"clab-telconet-sentinel-{node}",
        role=NodeRole.ACCESS,
        interfaces=("eth1", "eth2"),
        protocol_peers=(
            BoundTelemetryPeer(peer="agg1", interface="eth1"),
            BoundTelemetryPeer(peer="agg2", interface="eth2"),
        ),
    )


def _failed_snapshot() -> TelemetrySnapshot:
    return TelemetrySnapshot(
        snapshot_id="snapshot-1",
        observed_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
        topology_fingerprint="a" * 64,
        routers=tuple(
            collector.failed_router_telemetry(node, "timeout")
            for node in BASELINE_ROUTERS
        ),
    )


def test_parses_interface_counters_and_represents_missing_interface_as_unknown() -> None:
    output = json.dumps(
        [
            {
                "ifname": "eth1",
                "operstate": "UP",
                "stats64": {
                    "rx": {"bytes": 1234, "dropped": 4},
                    "tx": {"bytes": 5678, "dropped": 5},
                },
            },
            {
                "ifname": "lo",
                "operstate": "UNKNOWN",
                "stats64": {
                    "rx": {"bytes": 99, "dropped": 0},
                    "tx": {"bytes": 99, "dropped": 0},
                },
            },
        ]
    )

    interfaces = collector.parse_interface_telemetry(output, ("eth1", "eth2"))

    assert [interface.name for interface in interfaces] == ["eth1", "eth2"]
    assert interfaces[0].state is TelemetryState.UP
    assert interfaces[0].rx_bytes == 1234
    assert interfaces[0].tx_bytes == 5678
    assert interfaces[0].rx_drops == 4
    assert interfaces[0].tx_drops == 5
    assert interfaces[1].state is TelemetryState.UNKNOWN
    assert interfaces[1].rx_bytes == 0
    assert interfaces[1].tx_bytes == 0


def test_parses_expected_protocol_peers_with_stable_node_labels() -> None:
    ospf = json.dumps(
        {
            "neighbors": {
                "10.255.0.21": [
                    {"nbrState": "Full/-", "ifaceName": "eth1:10.0.1.0"}
                ],
                "10.255.0.22": [
                    {"converged": "Init", "ifaceName": "eth2:10.0.1.2"}
                ],
            }
        }
    )
    bfd = json.dumps(
        [
            {"peer": "10.0.1.1", "interface": "eth1", "status": "up"},
            {"peer": "10.0.1.3", "interface": "eth2", "status": "down"},
        ]
    )
    route = json.dumps(
        {
            "10.20.0.0/24": [
                {
                    "selected": False,
                    "installed": False,
                    "metric": 999,
                    "nexthops": [],
                },
                {
                    "selected": True,
                    "installed": True,
                    "metric": 30,
                    "nexthops": [
                        {"active": True, "fib": True, "interfaceName": "eth1"}
                    ],
                },
            ]
        }
    )

    expected = (
        BoundTelemetryPeer(peer="agg1", interface="eth1"),
        BoundTelemetryPeer(peer="agg2", interface="eth2"),
    )
    neighbors = collector.parse_ospf_neighbors(ospf, expected)
    peers = collector.parse_bfd_peers(bfd, expected)
    selected_route = collector.parse_selected_route(route, "10.20.0.0/24")

    assert [(item.neighbor, item.interface, item.state) for item in neighbors] == [
        ("agg1", "eth1", TelemetryState.UP),
        ("agg2", "eth2", TelemetryState.DOWN),
    ]
    assert [(item.peer, item.interface, item.configured, item.state) for item in peers] == [
        ("agg1", "eth1", True, TelemetryState.UP),
        ("agg2", "eth2", True, TelemetryState.DOWN),
    ]
    assert selected_route.prefix == "10.20.0.0/24"
    assert selected_route.present is True
    assert selected_route.metric == 30
    assert selected_route.egress_interface == "eth1"


def test_successful_protocol_queries_emit_missing_expected_peers_explicitly() -> None:
    expected = (
        BoundTelemetryPeer(peer="agg1", interface="eth1"),
        BoundTelemetryPeer(peer="agg2", interface="eth2"),
    )

    neighbors = collector.parse_ospf_neighbors('{"neighbors":{}}', expected)
    peers = collector.parse_bfd_peers("[]", expected)

    assert [(item.neighbor, item.interface, item.state) for item in neighbors] == [
        ("agg1", "eth1", TelemetryState.DOWN),
        ("agg2", "eth2", TelemetryState.DOWN),
    ]
    assert [(item.peer, item.interface, item.configured, item.state) for item in peers] == [
        ("agg1", "eth1", False, TelemetryState.UNKNOWN),
        ("agg2", "eth2", False, TelemetryState.UNKNOWN),
    ]


@pytest.mark.parametrize("protocol", ["ospf", "bfd"])
def test_rejects_protocol_rows_on_interfaces_outside_the_binding(protocol: str) -> None:
    expected = (BoundTelemetryPeer(peer="agg1", interface="eth1"),)
    if protocol == "ospf":
        output = json.dumps(
            {
                "neighbors": {
                    "198.51.100.99": [
                        {"nbrState": "Full/-", "ifaceName": "eth9:198.51.100.1"}
                    ]
                }
            }
        )
        parser = collector.parse_ospf_neighbors
    else:
        output = json.dumps(
            [{"peer": "198.51.100.99", "interface": "eth9", "status": "up"}]
        )
        parser = collector.parse_bfd_peers

    with pytest.raises(ValueError, match="unexpected protocol interface: eth9"):
        parser(output, expected)


def test_represents_absent_service_route_without_inventing_a_metric() -> None:
    route = collector.parse_selected_route("{}", "10.20.0.0/24")

    assert route.prefix == "10.20.0.0/24"
    assert route.present is False
    assert route.metric is None
    assert route.egress_interface is None


def test_collect_router_uses_only_fixed_argument_arrays_and_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[list[str], dict[str, Any]]] = []
    outputs = iter(
        (
            json.dumps(
                [
                    {
                        "ifname": "eth1",
                        "operstate": "UP",
                        "stats64": {
                            "rx": {"bytes": 10, "dropped": 0},
                            "tx": {"bytes": 20, "dropped": 1},
                        },
                    },
                    {
                        "ifname": "eth2",
                        "operstate": "DOWN",
                        "stats64": {
                            "rx": {"bytes": 30, "dropped": 2},
                            "tx": {"bytes": 40, "dropped": 3},
                        },
                    },
                ]
            ),
            '{"neighbors":{}}',
            '[]',
            '{"10.20.0.0/24":[{"selected":true,"installed":true,'
            '"metric":30,"nexthops":[{"active":true,"fib":true,'
            '"interfaceName":"eth1"}]}]}',
        )
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        invocations.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    router = collector.collect_router(_target(), "10.20.0.0/24")

    assert router.collection_success is True
    assert router.state is TelemetryState.UP
    assert router.collection_error is None
    assert [invocation[0] for invocation in invocations] == [
        [
            "docker",
            "exec",
            "clab-telconet-sentinel-access1",
            "ip",
            "-s",
            "-j",
            "link",
            "show",
        ],
        [
            "docker",
            "exec",
            "clab-telconet-sentinel-access1",
            "vtysh",
            "-c",
            "show ip ospf neighbor json",
        ],
        [
            "docker",
            "exec",
            "clab-telconet-sentinel-access1",
            "vtysh",
            "-c",
            "show bfd peers json",
        ],
        [
            "docker",
            "exec",
            "clab-telconet-sentinel-access1",
            "vtysh",
            "-c",
            "show ip route 10.20.0.0/24 json",
        ],
    ]
    assert all(kwargs["shell"] is False for _, kwargs in invocations)
    assert all(kwargs["timeout"] == collector.COMMAND_TIMEOUT_SECONDS for _, kwargs in invocations)
    assert all(kwargs["check"] is True for _, kwargs in invocations)


def test_failed_router_collection_is_unknown_not_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=collector.COMMAND_TIMEOUT_SECONDS)

    monkeypatch.setattr(collector.subprocess, "run", timed_out)

    router = collector.collect_router(_target(), "10.20.0.0/24")

    assert router.name == "access1"
    assert router.collection_success is False
    assert router.state is TelemetryState.UNKNOWN
    assert router.collection_error == "collection timed out"
    assert router.interfaces == ()
    assert router.ospf_neighbors == ()
    assert router.bfd_peers == ()
    assert router.routes == ()


def test_collect_snapshot_keeps_other_routers_when_one_collection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = (_target("access1"), _target("access2"))
    binding = _binding(*targets)

    def fake_collect(target: BoundTelemetryTarget, prefix: str) -> RouterTelemetry:
        assert prefix == "10.20.0.0/24"
        if target.node == "access2":
            raise ValueError("invalid FRR JSON")
        return RouterTelemetry(
            name=target.node,
            collection_success=True,
            state=TelemetryState.UP,
        )

    monkeypatch.setattr(collector, "collect_router", fake_collect)

    snapshot = collector.collect_snapshot(binding)

    assert isinstance(snapshot, TelemetrySnapshot)
    assert snapshot.topology_fingerprint == "a" * 64
    assert [router.name for router in snapshot.routers] == list(BASELINE_ROUTERS)
    assert snapshot.routers[0].state is TelemetryState.UP
    assert snapshot.routers[1].state is TelemetryState.UNKNOWN
    assert snapshot.routers[1].collection_error == "collector error"


def test_api_client_posts_the_strict_snapshot_json(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Any] = []

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"accepted":true}'

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        assert timeout == collector.HTTP_TIMEOUT_SECONDS
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(collector.urllib.request, "urlopen", fake_urlopen)
    snapshot = _failed_snapshot()

    response = collector.TelemetryApiClient("http://127.0.0.1:8000/").publish(snapshot)

    assert response == {"accepted": True}
    request = requests[0]
    assert request.full_url == "http://127.0.0.1:8000/api/telemetry/snapshots"
    assert request.method == "POST"
    assert json.loads(request.data)["routers"][0]["state"] == "unknown"


def test_run_collector_once_collects_and_publishes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(_target())
    published: list[TelemetrySnapshot] = []
    snapshot = _failed_snapshot()

    class FakeApi:
        def publish(self, snapshot: TelemetrySnapshot) -> dict[str, bool]:
            published.append(snapshot)
            return {"accepted": True}

    monkeypatch.setattr(collector, "collect_snapshot", lambda _: snapshot)

    collector.run_collector(binding, FakeApi(), once=True, interval_seconds=1)

    assert published == [snapshot]


def test_run_collector_retries_transient_publish_failures_in_continuous_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding(_target())
    snapshot = _failed_snapshot()
    publish_attempts = 0
    sleep_calls = 0

    class StopCollector(Exception):
        pass

    class FakeApi:
        def publish(self, _snapshot: TelemetrySnapshot) -> dict[str, bool]:
            nonlocal publish_attempts
            publish_attempts += 1
            if publish_attempts == 1:
                raise RuntimeError("telemetry API is unavailable")
            return {"accepted": True}

    def stop_after_second_iteration(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise StopCollector

    monkeypatch.setattr(collector, "collect_snapshot", lambda _: snapshot)
    monkeypatch.setattr(collector.time, "sleep", stop_after_second_iteration)

    with pytest.raises(StopCollector):
        collector.run_collector(binding, FakeApi(), once=False, interval_seconds=1)

    assert publish_attempts == 2
    assert "telemetry publish failed: telemetry API is unavailable" in capsys.readouterr().err


def test_run_collector_once_reports_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(_target())
    snapshot = _failed_snapshot()

    class FakeApi:
        def publish(self, _snapshot: TelemetrySnapshot) -> dict[str, bool]:
            raise RuntimeError("telemetry API is unavailable")

    monkeypatch.setattr(collector, "collect_snapshot", lambda _: snapshot)

    with pytest.raises(RuntimeError, match="telemetry API is unavailable"):
        collector.run_collector(binding, FakeApi(), once=True, interval_seconds=1)


def test_cli_exposes_only_config_api_once_and_interval_options() -> None:
    parser = collector._build_parser()
    option_strings = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option not in {"-h", "--help"}
    }

    assert option_strings == {"--config", "--api-url", "--once", "--interval"}
    with pytest.raises(SystemExit):
        parser.parse_args(["--router", "attacker-controlled"])


def test_main_loads_only_canonical_lab_inputs_from_the_config_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    config = project / "lab" / "telemetry.yml"
    config.parent.mkdir(parents=True)
    config.write_text("version: 1\n", encoding="utf-8")
    calls: list[tuple[Path, Path, Path]] = []
    binding = _binding(_target())

    def fake_load(config_path: Path, intent_path: Path, clab_path: Path) -> Any:
        calls.append((config_path, intent_path, clab_path))
        return binding

    monkeypatch.setattr(collector, "load_telemetry_binding", fake_load)
    monkeypatch.setattr(collector, "run_collector", lambda *_args, **_kwargs: None)

    result = collector.main(
        ["--config", str(config), "--api-url", "http://127.0.0.1:8000", "--once"]
    )

    assert result == 0
    assert calls == [
        (
            config,
            project / "lab" / "intent.yml",
            project / "lab" / "telconet.clab.yml",
        )
    ]
