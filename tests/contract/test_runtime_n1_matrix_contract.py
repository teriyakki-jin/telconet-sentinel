from pathlib import Path

ROOT = Path(__file__).parents[2]
SCENARIO = ROOT / "scenarios" / "runtime_n1_matrix_e2e.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "lab-e2e.yml"


def test_runtime_n1_matrix_executes_all_typed_plan_rows_and_verifies_evidence() -> None:
    script = SCENARIO.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert 'runtime_n1=(uv run telconet-runtime-n1)' in script
    assert '"${runtime_n1[@]}" plan' in script
    assert '"${runtime_n1[@]}" rows' in script
    assert '"${runtime_n1[@]}" settings' in script
    assert '"${runtime_n1[@]}" metric' in script
    assert '"${runtime_n1[@]}" adjacencies' in script
    assert '"${runtime_n1[@]}" neighbor-count' in script
    assert '"${runtime_n1[@]}" verify' in script
    assert "ip link set dev" in script
    assert 'show ip route ${service_prefix} json' in script
    assert 'ping -n -c 1 -W 1 "${service_address}"' in script
    assert 'wait_metric access1 "${access1_baseline}"' in script
    assert 'wait_metric access2 "${access2_baseline}"' in script
    assert "show ip ospf neighbor json" in script
    assert "wait_full_mesh" in script
    assert "trap cleanup EXIT" in script
    assert "RUNTIME_N1_MATRIX scenarios=15 checks=120 status=pass" in script


def test_containerlab_ci_runs_and_attests_runtime_n1_matrix_artifacts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "bash scenarios/runtime_n1_matrix_e2e.sh" in workflow
    assert "artifacts/runtime-n1-matrix" in workflow
    assert "artifacts/telconet-live-evidence.tar.gz" in workflow
