#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet.clab.yml"
ospf_log="${project_root}/evidence/remote-blackhole-ospf.log"
bfd_log="${project_root}/evidence/remote-blackhole-bfd.log"
comparison_json="${project_root}/evidence/bfd-comparison.json"

client_a="clab-telconet-sentinel-client-a"
access_1="clab-telconet-sentinel-access1"
agg_1="clab-telconet-sentinel-agg1"
service_ip="10.20.0.10"

command -v containerlab >/dev/null || {
  echo "containerlab is required inside WSL/Linux" >&2
  exit 1
}
command -v python3 >/dev/null || {
  echo "python3 is required to build comparison evidence" >&2
  exit 1
}
docker info >/dev/null

configuration_sha256="$(
  PYTHONPATH="${project_root}/src" python3 -m telconet_sentinel.configuration \
    --project-root "${project_root}"
)"

if docker ps -a --format '{{.Names}}' | grep -q '^clab-telconet-sentinel-'; then
  containerlab destroy --topo "${lab_file}" --cleanup
fi
containerlab deploy --topo "${lab_file}" --reconfigure

if ! docker exec "${client_a}" ping -D -c 1 127.0.0.1 >/dev/null 2>&1; then
  docker exec "${client_a}" apk add --no-cache iputils >/dev/null
fi

clear_blackhole() {
  docker exec "${agg_1}" tc qdisc del dev eth1 root >/dev/null 2>&1 || true
}
trap clear_blackhole EXIT
clear_blackhole

wait_for_metric() {
  local expected_metric="$1"
  local attempts=0
  while (( attempts < 30 )); do
    if docker exec "${access_1}" vtysh -c "show ip route 10.20.0.0/24" 2>/dev/null |
      grep -q "metric ${expected_metric}, best"; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  echo "OSPF metric ${expected_metric} did not converge within 30 seconds" >&2
  return 1
}

record_state() {
  local state="$1"
  echo "STATE_BEGIN name=${state}"
  docker exec "${access_1}" vtysh -c "show ip route 10.20.0.0/24" 2>/dev/null |
    sed 's/[[:space:]]*$//'
  docker exec "${client_a}" traceroute -n -m 8 -w 1 "${service_ip}"
  echo "STATE_END name=${state}"
}

wait_for_bfd() {
  local attempts=0
  while (( attempts < 30 )); do
    if docker exec "${access_1}" vtysh -c "show bfd peer 10.0.1.1 json" 2>/dev/null |
      grep -Eq '"status"[[:space:]]*:[[:space:]]*"up"'; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  echo "BFD peer 10.0.1.1 did not become up within 30 seconds" >&2
  return 1
}

enable_bfd_profile() {
  docker exec "${access_1}" vtysh \
    -c "configure terminal" \
    -c "interface eth1" \
    -c "ip ospf bfd 3 100 100" >/dev/null 2>&1
  docker exec "${agg_1}" vtysh \
    -c "configure terminal" \
    -c "interface eth1" \
    -c "ip ospf bfd 3 100 100" >/dev/null 2>&1
  wait_for_bfd
}

capture_profile() {
  local profile="$1"
  local detector="$2"
  local configured_detection_ms="$3"
  local raw_log="$4"
  local capture_started_at
  capture_started_at="$(date --iso-8601=seconds)"

  capture_body() {
    echo "TELCONET_DETECTION_MEASUREMENT version=1 profile=${profile} detector=${detector} configured_detection_ms=${configured_detection_ms} capture_started_at=${capture_started_at} configuration_sha256=${configuration_sha256}"
    record_state baseline
    if [[ "${profile}" == "bfd_100x3" ]]; then
      docker exec "${access_1}" vtysh -c "show bfd peers brief" 2>/dev/null
    fi

    (
      sleep 2
      echo "EVENT type=blackhole_start epoch_ns=$(date +%s%N)"
      docker exec "${agg_1}" tc qdisc add dev eth1 root netem loss 100%
      wait_for_metric 140
      record_state failover
    ) &
    local controller_pid=$!

    docker exec "${client_a}" ping -D -i 0.1 -c 140 -W 1 "${service_ip}"
    wait "${controller_pid}"
    echo "EVENT type=blackhole_end epoch_ns=$(date +%s%N)"
    clear_blackhole
  }

  capture_body | tee "${raw_log}"
  clear_blackhole
  wait_for_metric 30
}

wait_for_metric 30
capture_profile ospf_only ospf_dead_timer 4000 "${ospf_log}"
enable_bfd_profile
capture_profile bfd_100x3 bfd 300 "${bfd_log}"

PYTHONPATH="${project_root}/src" python3 -m telconet_sentinel.bfd_comparison \
  --ospf-log "${ospf_log}" \
  --bfd-log "${bfd_log}" \
  --output "${comparison_json}"

echo "OSPF-only raw log: ${ospf_log}"
echo "BFD raw log: ${bfd_log}"
echo "Comparison evidence: ${comparison_json}"
echo "Lab remains running for inspection. Destroy it with:"
echo "containerlab destroy --topo ${lab_file}"
