from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_runtime_expectation_declares_the_live_experiment_contract() -> None:
    document = yaml.safe_load(
        (ROOT / "lab" / "runtime-expectations.yml").read_text(encoding="utf-8")
    )

    assert document["router"] == "access1"
    assert document["service_node"] == "service-host"
    assert document["route_prefix"] == "10.20.0.0/24"
    assert document["route_next_hop"] == "10.0.1.1"
    assert document["route_interface"] == "eth1"
    assert document["ospf_neighbors"] == [
        {"router_id": "10.255.0.21", "interface": "eth1"},
        {"router_id": "10.255.0.22", "interface": "eth2"},
    ]
    assert document["bfd"] == {
        "peer": "10.0.1.1",
        "interface": "eth1",
        "receive_interval_ms": 100,
        "transmit_interval_ms": 100,
        "detect_multiplier": 3,
        "detection_timeout_ms": 300,
    }


def test_live_e2e_gates_fault_injection_on_runtime_conformance() -> None:
    script = (ROOT / "scenarios" / "live_convergence_e2e.sh").read_text(
        encoding="utf-8"
    )

    conformance = script.index("telconet_sentinel.runtime_conformance")
    collector = script.index("telconet_sentinel.live_collector")
    assert conformance < collector
    assert "telconet_sentinel.runtime_snapshot" in script
    assert '"${artifact_dir}/runtime-snapshot.json"' in script
    assert '"${artifact_dir}/runtime-conformance.json"' in script
    assert "RUNTIME_CONFORMANCE status=pass" in script
