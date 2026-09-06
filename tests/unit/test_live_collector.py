import json
import queue
import subprocess

import pytest

import telconet_sentinel.live_collector as collector
from telconet_sentinel.live_collector import (
    DockerFrrProbe,
    LiveApiClient,
    LiveSample,
    LiveTransitionDetector,
    PingRecovery,
    PingRecoveryTracker,
    parse_bfd_peer_up,
    parse_ospf_neighbor_full,
    parse_route_metric,
)


def test_parses_frr_bfd_peer_json() -> None:
    up = '{"10.0.1.1":[{"peer":"10.0.1.1","status":"up"}]}'
    down = '{"10.0.1.1":[{"peer":"10.0.1.1","status":"down"}]}'

    assert parse_bfd_peer_up(up, "10.0.1.1") is True
    assert parse_bfd_peer_up(down, "10.0.1.1") is False
    with pytest.raises(ValueError, match="valid JSON"):
        parse_bfd_peer_up("not json", "10.0.1.1")


def test_parses_frr_ospf_neighbor_json() -> None:
    full = (
        '{"neighbors":[{"address":"10.0.1.1","nbrState":"Full/DR"},'
        '{"address":"10.0.2.1","nbrState":"2-Way/DROther"}]}'
    )

    assert parse_ospf_neighbor_full(full, "10.0.1.1") is True
    assert parse_ospf_neighbor_full(full, "10.0.2.1") is False
    assert parse_ospf_neighbor_full("{}", "10.0.1.1") is False


def test_parses_selected_route_metric_with_text_fallback() -> None:
    route = (
        '{"10.20.0.0/24":['
        '{"metric":999,"selected":false},{"metric":140,"selected":true}]}'
    )

    assert parse_route_metric(route) == 140
    assert parse_route_metric("Known via ospf, distance 110, metric 30, best") == 30
    with pytest.raises(ValueError, match="route metric"):
        parse_route_metric("{}")


def test_reports_post_fault_ping_replies_and_ignores_pre_fault_queue_entries() -> None:
    tracker = PingRecoveryTracker()
    assert tracker.observe_reply("64 bytes from 10.20.0.10: icmp_seq=1 ttl=61", 10) is None
    assert tracker.observe_reply("64 bytes from 10.20.0.10: icmp_seq=2 ttl=61", 20) is None
    assert tracker.last_sequence == 2

    tracker.mark_injected(100)

    assert tracker.observe_reply("64 bytes from 10.20.0.10: icmp_seq=3 ttl=61", 90) is None
    recovered = tracker.observe_reply(
        "64 bytes from 10.20.0.10: icmp_seq=8 ttl=60 time=0.2 ms",
        498_000_000,
    )
    assert recovered is not None
    assert recovered.sequence == 8
    assert recovered.observed_ns == 498_000_000
    later_reply = tracker.observe_reply(
        "64 bytes from 10.20.0.10: icmp_seq=9 ttl=60",
        600_000_000,
    )
    assert later_reply is not None
    assert later_reply.sequence == 9


def test_ignores_non_reply_ping_output() -> None:
    tracker = PingRecoveryTracker()
    tracker.mark_injected(0)

    assert (
        tracker.observe_reply(
            "From 10.10.1.1 icmp_seq=4 Destination Host Unreachable",
            1,
        )
        is None
    )


def test_accepts_busybox_ping_sequence_format() -> None:
    tracker = PingRecoveryTracker()
    assert tracker.observe_reply("64 bytes from 10.20.0.10: seq=1 ttl=62", 1) is None
    tracker.mark_injected(1)

    recovery = tracker.observe_reply("64 bytes from 10.20.0.10: seq=5 ttl=62", 2)

    assert recovery is not None
    assert recovery.sequence == 5


def test_detects_each_control_and_data_plane_transition_once() -> None:
    detector = LiveTransitionDetector()

    assert detector.observe(LiveSample(True, True, 30), 0) == []
    transitions = detector.observe(LiveSample(False, False, 140, 8), 498)

    assert [transition.event for transition in transitions] == [
        "bfd_down",
        "ospf_neighbor_down",
        "route_failover",
        "data_plane_recovered",
    ]
    assert transitions[2].route_metric == 140
    assert transitions[3].icmp_sequence == 8
    assert detector.observe(LiveSample(False, False, 140, 9), 600) == []


def test_holds_later_transition_until_causal_prerequisites_are_observed() -> None:
    detector = LiveTransitionDetector()

    assert detector.observe(LiveSample(True, True, 140), 285) == []
    transitions = detector.observe(LiveSample(False, False, 140), 410)

    assert [transition.event for transition in transitions] == [
        "bfd_down",
        "ospf_neighbor_down",
        "route_failover",
    ]
    assert all(transition.offset_ms == 410 for transition in transitions)


def test_docker_probe_collects_one_typed_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = {
        "show bfd peers json": '{"10.0.1.1":[{"status":"up"}]}',
        "show ip ospf neighbor json": (
            '{"neighbors":[{"address":"10.0.1.1","nbrState":"Full/P2P"}]}'
        ),
        "show ip route 10.20.0.0/24 json": (
            '{"10.20.0.0/24":[{"metric":30,"selected":true}]}'
        ),
    }

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=outputs[command[-1]], stderr="")

    monkeypatch.setattr(collector.subprocess, "run", fake_run)

    sample = DockerFrrProbe(
        "clab-telconet-sentinel-access1",
        "10.0.1.1",
        "10.20.0.0/24",
    ).sample()

    assert sample == LiveSample(bfd_up=True, ospf_full=True, route_metric=30)


def test_live_api_client_sends_typed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id":"run-1","status":"collecting"}'

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        assert timeout == 5
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(collector.urllib.request, "urlopen", fake_urlopen)
    client = LiveApiClient("http://127.0.0.1:8000/")

    created = client.create_run("bfd_100x3", "access1", "10.20.0.10")
    client.record_event("run-1", collector.LiveTransition("route_failover", 421, 140))

    assert created["id"] == "run-1"
    request = requests[-1]
    assert request.full_url.endswith("/api/convergence-runs/run-1/events")
    payload = json.loads(request.data)
    assert payload["event"] == "route_failover"
    assert payload["route_metric"] == 140


def test_collects_and_publishes_a_complete_live_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.events: list[collector.LiveTransition] = []

        def health(self) -> None:
            return None

        def create_run(self, profile: str, source: str, target: str) -> dict[str, str]:
            assert (profile, source, target) == ("bfd_100x3", "access1", "10.20.0.10")
            return {"id": "run-1"}

        def record_event(
            self,
            run_id: str,
            transition: collector.LiveTransition,
        ) -> dict[str, str]:
            assert run_id == "run-1"
            self.events.append(transition)
            return {"status": "collecting"}

        def get_run(self, run_id: str) -> dict[str, str]:
            assert run_id == "run-1"
            return {"id": run_id, "status": "complete"}

    class FakeProbe:
        def __init__(self) -> None:
            self.calls = 0

        def sample(self, icmp_sequence: int | None = None) -> LiveSample:
            trace.append("sample")
            self.calls += 1
            if self.calls == 1:
                return LiveSample(True, True, 30)
            return LiveSample(False, False, 140, icmp_sequence)

    class FakeProcess:
        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: int) -> None:
            assert timeout == 2

        def kill(self) -> None:
            raise AssertionError("graceful ping termination should succeed")

    api = FakeApi()
    process = FakeProcess()
    ping_output: queue.Queue[tuple[str, int]] = queue.Queue()
    fault_commands: list[tuple[str, ...]] = []
    trace: list[str] = []
    monkeypatch.setattr(collector, "_start_ping", lambda *_: (process, ping_output))
    monkeypatch.setattr(collector, "_wait_for_ping_baseline", lambda *_: None)
    monkeypatch.setattr(
        collector,
        "_drain_ping",
        lambda *_: PingRecovery(sequence=8, observed_ns=498_000_000),
    )
    monkeypatch.setattr(
        collector,
        "_run_command",
        lambda *command: (fault_commands.append(command), trace.append(command[4])),
    )
    clock_values = iter((1_000_000_000, 1_566_000_000))
    monkeypatch.setattr(
        collector.time,
        "monotonic_ns",
        lambda: (trace.append("clock"), next(clock_values))[1],
    )
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
    )

    result = collector.collect_live_convergence(
        api=api,  # type: ignore[arg-type]
        probe=FakeProbe(),  # type: ignore[arg-type]
        client_container="client-a",
        fault_container="agg1",
        fault_interface="eth1",
        source="access1",
        target="10.20.0.10",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert result["status"] == "complete"
    assert [event.event for event in api.events] == [
        "blackhole_injected",
        "bfd_down",
        "ospf_neighbor_down",
        "route_failover",
        "data_plane_recovered",
    ]
    assert len(fault_commands) == 2
    assert trace == ["sample", "qdisc", "clock", "filter", "sample", "clock"]
    assert process.terminated is True


def test_refuses_to_inject_a_fault_without_a_healthy_baseline() -> None:
    class UnhealthyProbe:
        def sample(self, icmp_sequence: int | None = None) -> LiveSample:
            return LiveSample(False, True, 30, icmp_sequence)

    class HealthyApi:
        def health(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="baseline requires BFD up"):
        collector.collect_live_convergence(
            api=HealthyApi(),  # type: ignore[arg-type]
            probe=UnhealthyProbe(),  # type: ignore[arg-type]
            client_container="client-a",
            fault_container="agg1",
            fault_interface="eth1",
            source="access1",
            target="10.20.0.10",
            timeout_seconds=1,
            poll_interval_seconds=0,
        )
