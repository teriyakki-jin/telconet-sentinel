#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet.clab.yml"
raw_log="${project_root}/evidence/measured-link-failure.log"
evidence_json="${project_root}/evidence/measured-link-failure.json"

client_a="clab-telconet-sentinel-client-a"
access_1="clab-telconet-sentinel-access1"
service_ip="10.20.0.10"

command -v containerlab >/dev/null || {
  echo "containerlab is required inside WSL/Linux" >&2
  exit 1
}
command -v python3 >/dev/null || {
  echo "python3 is required to build evidence" >&2
  exit 1
}
docker info >/dev/null

configuration_sha256="$(
  {
    sha256sum "${lab_file}" | awk '{print $1}'
    sha256sum "${project_root}/lab/intent.yml" | awk '{print $1}'
    find "${project_root}/lab/frr" -type f -print0 | sort -z | xargs -0 sha256sum |
      awk '{print $1}'
  } | sha256sum | awk '{print $1}'
)"

if docker ps -a --format '{{.Names}}' | grep -q '^clab-telconet-sentinel-'; then
  containerlab destroy --topo "${lab_file}" --cleanup
fi
containerlab deploy --topo "${lab_file}" --reconfigure
if ! docker exec "${client_a}" ping -D -c 1 127.0.0.1 >/dev/null 2>&1; then
  docker exec "${client_a}" apk add --no-cache iputils >/dev/null
fi

restore_primary() {
  docker exec "${access_1}" ip link set eth1 up >/dev/null 2>&1 || true
}
trap restore_primary EXIT
restore_primary

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

wait_for_metric 30
docker exec "${client_a}" ping -c 1 -W 1 "${service_ip}" >/dev/null

containerlab_version="$(containerlab version 2>/dev/null | awk '/version:/ {print $2; exit}')"
docker_version="$(docker version --format '{{.Server.Version}}')"
frr_image="$(docker inspect --format '{{.Config.Image}}' "${access_1}")"
frr_version="${frr_image##*:}"
host_version="WSL2_Ubuntu_$(. /etc/os-release && printf '%s' "${VERSION_ID}")"
capture_started_at="$(date --iso-8601=seconds)"

capture_measurement() {
  echo "TELCONET_MEASUREMENT version=1 capture_started_at=${capture_started_at} ping_interval_ms=100 ping_count=160"
  echo "ENV containerlab=${containerlab_version} docker_engine=${docker_version} frr=${frr_version} host=${host_version} configuration_sha256=${configuration_sha256}"
  record_state baseline

  (
    sleep 2
    echo "EVENT type=link_down epoch_ns=$(date +%s%N)"
    docker exec "${access_1}" ip link set eth1 down
    wait_for_metric 140
    record_state failover
    sleep 4
    echo "EVENT type=link_up epoch_ns=$(date +%s%N)"
    docker exec "${access_1}" ip link set eth1 up
  ) &
  local controller_pid=$!

  docker exec "${client_a}" ping -D -i 0.1 -c 160 -W 1 "${service_ip}"
  wait "${controller_pid}"
  wait_for_metric 30
  record_state recovery
}

capture_measurement | tee "${raw_log}"
PYTHONPATH="${project_root}/src" python3 -m telconet_sentinel.measurement \
  --raw-log "${raw_log}" \
  --output "${evidence_json}"

echo "Raw measurement: ${raw_log}"
echo "Calculated evidence: ${evidence_json}"
echo "Lab remains running for inspection. Destroy it with:"
echo "containerlab destroy --topo ${lab_file}"
