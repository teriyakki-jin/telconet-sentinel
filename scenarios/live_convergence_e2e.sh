#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet.clab.yml"
artifact_dir="${TELCONET_ARTIFACT_DIR:-${project_root}/artifacts/live-convergence}"
api_url="${TELCONET_API_URL:-http://127.0.0.1:18000}"
api_port="${api_url##*:}"
api_pid=""
keep_lab="${TELCONET_KEEP_LAB:-0}"
client_a="clab-telconet-sentinel-client-a"
access_1="clab-telconet-sentinel-access1"
agg_1="clab-telconet-sentinel-agg1"

mkdir -p "${artifact_dir}"

if command -v uv >/dev/null 2>&1; then
  python_command=(uv run python)
else
  python_command=(python3)
fi

deploy_lab() {
  if [[ "${TELCONET_CLAB_SUDO:-0}" == "1" ]]; then
    sudo containerlab deploy --topo "${lab_file}" --reconfigure
  else
    containerlab deploy --topo "${lab_file}" --reconfigure
  fi
}

destroy_lab() {
  if [[ "${TELCONET_CLAB_SUDO:-0}" == "1" ]]; then
    sudo containerlab destroy --topo "${lab_file}" --cleanup
  else
    containerlab destroy --topo "${lab_file}" --cleanup
  fi
}

cleanup() {
  set +e
  docker exec "${agg_1}" tc qdisc del dev eth1 clsact >/dev/null 2>&1
  if [[ -n "${api_pid}" ]]; then
    kill "${api_pid}" >/dev/null 2>&1
    wait "${api_pid}" >/dev/null 2>&1
  fi
  docker ps -a >"${artifact_dir}/containers-after.txt" 2>&1
  if [[ "${keep_lab}" != "1" ]]; then
    destroy_lab >>"${artifact_dir}/destroy.log" 2>&1
  fi
}
trap cleanup EXIT

for command in containerlab docker curl; do
  command -v "${command}" >/dev/null || {
    echo "${command} is required" >&2
    exit 1
  }
done
docker info >/dev/null

if docker ps -a --format '{{.Names}}' | grep -q '^clab-telconet-sentinel-'; then
  destroy_lab >"${artifact_dir}/pre-cleanup.log" 2>&1
fi
deploy_lab 2>&1 | tee "${artifact_dir}/deploy.log"

for _ in $(seq 1 30); do
  if docker exec "${access_1}" \
    vtysh -c "show ip route 10.20.0.0/24 json" \
    >"${artifact_dir}/route-baseline.json" 2>"${artifact_dir}/route-baseline.err" &&
    grep -Eq '"metric"[[:space:]]*:[[:space:]]*30' \
      "${artifact_dir}/route-baseline.json"; then
    break
  fi
  sleep 1
done
grep -Eq '"metric"[[:space:]]*:[[:space:]]*30' \
  "${artifact_dir}/route-baseline.json"

docker exec "${access_1}" vtysh \
  -c "configure terminal" \
  -c "interface eth1" \
  -c "ip ospf bfd 3 100 100" \
  >>"${artifact_dir}/bfd-enable.log" 2>&1
docker exec "${agg_1}" vtysh \
  -c "configure terminal" \
  -c "interface eth1" \
  -c "ip ospf bfd 3 100 100" \
  >>"${artifact_dir}/bfd-enable.log" 2>&1

for _ in $(seq 1 30); do
  if docker exec "${access_1}" vtysh -c "show bfd peer 10.0.1.1 json" |
    grep -Eq '"status"[[:space:]]*:[[:space:]]*"up"'; then
    break
  fi
  sleep 1
done
docker exec "${access_1}" vtysh -c "show bfd peer 10.0.1.1 json" |
  grep -Eq '"status"[[:space:]]*:[[:space:]]*"up"'
docker exec "${access_1}" vtysh -c "show bfd peer 10.0.1.1 json" \
  >"${artifact_dir}/bfd-baseline.json" 2>"${artifact_dir}/bfd-baseline.err"
docker exec "${access_1}" vtysh -c "show ip ospf neighbor json" \
  >"${artifact_dir}/ospf-baseline.json" 2>"${artifact_dir}/ospf-baseline.err"

if [[ -z "${TELCONET_API_URL:-}" ]]; then
  "${python_command[@]}" -m uvicorn telconet_sentinel.main:app \
    --host 127.0.0.1 --port "${api_port}" \
    >"${artifact_dir}/api.log" 2>&1 &
  api_pid=$!
fi

for _ in $(seq 1 30); do
  if curl --fail --silent "${api_url}/health" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "${api_url}/health" >"${artifact_dir}/health.json"

"${python_command[@]}" -m telconet_sentinel.live_collector \
  --api-url "${api_url}" \
  --output "${artifact_dir}/live-convergence.json" \
  2>&1 | tee "${artifact_dir}/collector.log"

curl --fail --silent "${api_url}/metrics" >"${artifact_dir}/metrics.prom"
"${python_command[@]}" -c '
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
run = json.loads(path.read_text(encoding="utf-8"))
events = [event["event"] for event in run["events"]]
required = [
    "blackhole_injected", "bfd_down", "ospf_neighbor_down",
    "route_failover", "data_plane_recovered",
]
assert run["status"] == "complete", run
assert events == required, events
print("E2E_RESULT status=complete events=" + ",".join(events))
' "${artifact_dir}/live-convergence.json"
