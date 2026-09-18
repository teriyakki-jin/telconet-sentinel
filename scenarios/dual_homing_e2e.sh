#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet-dual-homed.clab.yml"
intent_file="${project_root}/lab/intent-dual-homed.yml"
artifact_dir="${TELCONET_DUAL_ARTIFACT_DIR:-${project_root}/artifacts/dual-homing}"
core1="clab-telconet-dual-homed-core1"
access1="clab-telconet-dual-homed-access1"
client_a="clab-telconet-dual-homed-client-a"
client_b="clab-telconet-dual-homed-client-b"
deployed=0

mkdir -p "${artifact_dir}"

clab() {
  if [[ "${TELCONET_CLAB_SUDO:-0}" == "1" ]]; then
    sudo containerlab "$@"
  else
    containerlab "$@"
  fi
}

cleanup() {
  set +e
  if [[ "${deployed}" == "1" ]]; then
    docker exec "${core1}" ip link set dev eth4 up >/dev/null 2>&1
    clab destroy --topo "${lab_file}" --cleanup >"${artifact_dir}/destroy.log" 2>&1
  fi
}
trap cleanup EXIT

for command in containerlab docker; do
  command -v "${command}" >/dev/null || { echo "${command} is required" >&2; exit 1; }
done
docker info >/dev/null

if docker ps -a --format '{{.Names}}' | grep -q '^clab-telconet-dual-homed-'; then
  echo "dual-homed lab already exists; refusing to replace it" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  python_command=(uv run python)
else
  python_command=(python3)
fi
"${python_command[@]}" -c '
import pathlib, sys
from telconet_sentinel.config import load_topology
from telconet_sentinel.resilience import audit_single_link_failures
audit = audit_single_link_failures(load_topology(pathlib.Path(sys.argv[1])))
assert audit.passes_n_minus_one and audit.total_scenarios == 11
print("N1_MODEL scenarios=11 outages=0")
' "${intent_file}" | tee "${artifact_dir}/n1-model.log"

deployed=1
clab deploy --topo "${lab_file}" 2>&1 | tee "${artifact_dir}/deploy.log"

wait_metric() {
  local expected="$1" file="$2"
  for _ in $(seq 1 60); do
    if docker exec "${access1}" vtysh -c "show ip route 10.20.0.10/32 json" \
      >"${file}" 2>"${file}.err" &&
      grep -Eq '"metric"[[:space:]]*:[[:space:]]*'"${expected}"'([,}])' "${file}"; then
      return 0
    fi
    sleep 1
  done
  docker exec "${access1}" vtysh -c "show ip route json" \
    >"${file}.full-rib.json" 2>&1 || true
  docker exec "clab-telconet-dual-homed-service-host" \
    vtysh -c "show ip ospf neighbor json" \
    >"${file}.service-neighbors.json" 2>&1 || true
  echo "expected service route metric ${expected} was not observed" >&2
  return 1
}

wait_ping() {
  local client="$1" file="$2"
  for _ in $(seq 1 30); do
    if docker exec "${client}" ping -n -c 1 -W 1 10.20.0.10 >"${file}" 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "${client} did not reach the dual-homed service" >&2
  return 1
}

wait_metric 30 "${artifact_dir}/route-before.json"
wait_ping "${client_a}" "${artifact_dir}/client-a-before.log"
wait_ping "${client_b}" "${artifact_dir}/client-b-before.log"

docker exec "${core1}" ip link set dev eth4 down
echo "SERVICE_LINK_DOWN core1:eth4" | tee "${artifact_dir}/fault.log"
wait_metric 70 "${artifact_dir}/route-failover.json"
wait_ping "${client_a}" "${artifact_dir}/client-a-failover.log"
wait_ping "${client_b}" "${artifact_dir}/client-b-failover.log"

docker exec "${core1}" ip link set dev eth4 up
wait_metric 30 "${artifact_dir}/route-restored.json"
wait_ping "${client_a}" "${artifact_dir}/client-a-restored.log"
wait_ping "${client_b}" "${artifact_dir}/client-b-restored.log"
echo "E2E_DUAL_HOMING baseline=30 failover=70 restored=30 clients=2" | \
  tee "${artifact_dir}/result.log"
