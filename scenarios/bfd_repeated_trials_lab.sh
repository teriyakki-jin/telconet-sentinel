#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet.clab.yml"
trials="${TELCONET_TRIALS:-20}"
skip_aggregate="${TELCONET_SKIP_AGGREGATE:-0}"
evidence_root="${project_root}/evidence/repeated"
ospf_dir="${evidence_root}/ospf_only"
bfd_dir="${evidence_root}/bfd_100x3"
repeated_json="${project_root}/evidence/bfd-repeated-trials.json"

client_a="clab-telconet-sentinel-client-a"
access_1="clab-telconet-sentinel-access1"
agg_1="clab-telconet-sentinel-agg1"
service_ip="10.20.0.10"

if ! [[ "${trials}" =~ ^[0-9]+$ ]] || (( trials < 1 || trials > 30 )); then
  echo "TELCONET_TRIALS must be an integer from 1 through 30" >&2
  exit 1
fi
if (( trials < 20 )) && [[ "${skip_aggregate}" != "1" ]]; then
  echo "At least 20 trials are required unless TELCONET_SKIP_AGGREGATE=1" >&2
  exit 1
fi

for command in containerlab python3 docker; do
  command -v "${command}" >/dev/null || {
    echo "${command} is required inside WSL/Linux" >&2
    exit 1
  }
done
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

mkdir -p "${ospf_dir}" "${bfd_dir}"

clear_blackhole() {
  docker exec "${agg_1}" tc qdisc del dev eth1 clsact >/dev/null 2>&1 || true
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

wait_for_no_bfd() {
  local attempts=0
  while (( attempts < 30 )); do
    if docker exec "${access_1}" vtysh -c "show bfd peers brief" 2>/dev/null |
      grep -q "Session count: 0"; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  echo "BFD session did not disappear within 30 seconds" >&2
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

disable_bfd_profile() {
  docker exec "${access_1}" vtysh \
    -c "configure terminal" \
    -c "interface eth1" \
    -c "no ip ospf bfd" >/dev/null 2>&1
  docker exec "${agg_1}" vtysh \
    -c "configure terminal" \
    -c "interface eth1" \
    -c "no ip ospf bfd" >/dev/null 2>&1
}

capture_profile() {
  local profile="$1"
  local detector="$2"
  local configured_detection_ms="$3"
  local trial="$4"
  local raw_log="$5"
  local capture_started_at
  capture_started_at="$(date --iso-8601=seconds)"

  capture_body() {
    echo "TELCONET_DETECTION_MEASUREMENT version=1 profile=${profile} detector=${detector} configured_detection_ms=${configured_detection_ms} trial=${trial} capture_started_at=${capture_started_at} configuration_sha256=${configuration_sha256}"
    record_state baseline
    if [[ "${profile}" == "bfd_100x3" ]]; then
      docker exec "${access_1}" vtysh -c "show bfd peers brief" 2>/dev/null
    fi

    (
      sleep 1
      echo "EVENT type=blackhole_start epoch_ns=$(date +%s%N)"
      docker exec "${agg_1}" tc qdisc add dev eth1 clsact
      docker exec "${agg_1}" \
        tc filter add dev eth1 ingress protocol all pref 1 flower action drop
      wait_for_metric 140
      record_state failover
    ) &
    local controller_pid=$!

    docker exec "${client_a}" ping -D -i 0.1 -c 80 -W 1 "${service_ip}"
    wait "${controller_pid}"
    echo "EVENT type=blackhole_end epoch_ns=$(date +%s%N)"
    clear_blackhole
  }

  capture_body | tee "${raw_log}" >/dev/null
  clear_blackhole
  wait_for_metric 30
}

wait_for_metric 30
for (( trial = 1; trial <= trials; trial++ )); do
  trial_id="$(printf '%02d' "${trial}")"
  echo "[${trial_id}/${trials}] OSPF-only capture"
  disable_bfd_profile
  wait_for_no_bfd
  wait_for_metric 30
  capture_profile ospf_only ospf_dead_timer 4000 \
    "${trial}" "${ospf_dir}/trial-${trial_id}.log"

  echo "[${trial_id}/${trials}] BFD 100ms x3 capture"
  enable_bfd_profile
  capture_profile bfd_100x3 bfd 300 \
    "${trial}" "${bfd_dir}/trial-${trial_id}.log"
done
disable_bfd_profile

if [[ "${skip_aggregate}" != "1" ]]; then
  args=(--output "${repeated_json}")
  for (( trial = 1; trial <= trials; trial++ )); do
    trial_id="$(printf '%02d' "${trial}")"
    args+=(--ospf-log "${ospf_dir}/trial-${trial_id}.log")
    args+=(--bfd-log "${bfd_dir}/trial-${trial_id}.log")
  done
  PYTHONPATH="${project_root}/src" python3 -m telconet_sentinel.repeated_trials \
    "${args[@]}"
  echo "Repeated evidence: ${repeated_json}"
fi

echo "Raw trial logs: ${evidence_root}"
echo "Lab remains running for inspection. Destroy it with:"
echo "containerlab destroy --topo ${lab_file}"
