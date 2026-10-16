#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet-dual-homed.clab.yml"
intent_file="${project_root}/lab/intent-dual-homed.yml"
artifact_dir="${TELCONET_N1_ARTIFACT_DIR:-${project_root}/artifacts/runtime-n1-matrix}"
plan_file="${artifact_dir}/plan.json"
observation_file="${artifact_dir}/observation.json"
evidence_file="${artifact_dir}/evidence.json"
lab_prefix="clab-telconet-dual-homed-"
deployed=0

if [[ -e "${artifact_dir}" ]]; then
  echo "runtime N-1 artifact directory already exists: ${artifact_dir}" >&2
  exit 1
fi
mkdir -p "${artifact_dir}"

if command -v uv >/dev/null 2>&1; then
  runtime_n1=(uv run telconet-runtime-n1)
else
  runtime_n1=(telconet-runtime-n1)
fi

clab() {
  if [[ "${TELCONET_CLAB_SUDO:-0}" == "1" ]]; then
    sudo containerlab "$@"
  else
    containerlab "$@"
  fi
}

set_interface_state() {
  local endpoint="$1" state="$2"
  local node="${endpoint%%:*}"
  local interface="${endpoint##*:}"
  [[ "${node}" =~ ^[a-z][a-z0-9-]{0,63}$ ]]
  [[ "${interface}" =~ ^eth[0-9]{1,2}$ ]]
  docker exec "${lab_prefix}${node}" ip link set dev "${interface}" "${state}"
}

restore_all_fault_interfaces() {
  local endpoint
  for endpoint in \
    access1:eth1 access1:eth2 access2:eth1 access2:eth2 \
    agg1:eth1 agg1:eth2 agg1:eth3 agg1:eth4 \
    agg2:eth1 agg2:eth2 agg2:eth3 agg2:eth4 \
    core1:eth1 core1:eth2 core1:eth3 core1:eth4 \
    core2:eth1 core2:eth2 core2:eth3 core2:eth4 \
    service-host:eth1 service-host:eth2; do
    set_interface_state "${endpoint}" up >/dev/null 2>&1 || true
  done
}

cleanup() {
  set +e
  if [[ "${deployed}" == "1" ]]; then
    restore_all_fault_interfaces
    clab destroy --topo "${lab_file}" --cleanup >"${artifact_dir}/destroy.log" 2>&1
  fi
}
trap cleanup EXIT

for command in containerlab docker; do
  command -v "${command}" >/dev/null || {
    echo "${command} is required" >&2
    exit 1
  }
done
docker info >/dev/null

if docker ps -a --format '{{.Names}}' | grep -q '^clab-telconet-dual-homed-'; then
  echo "dual-homed lab already exists; refusing to replace it" >&2
  exit 1
fi

"${runtime_n1[@]}" plan \
  --intent "${intent_file}" \
  --lab "${lab_file}" \
  --output "${plan_file}"
IFS=$'\t' read -r service_prefix service_address access1_baseline access2_baseline \
  < <("${runtime_n1[@]}" settings --plan "${plan_file}")
[[ "${service_prefix}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]
[[ "${service_address}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
[[ "${access1_baseline}" =~ ^[0-9]+$ ]]
[[ "${access2_baseline}" =~ ^[0-9]+$ ]]

deployed=1
clab deploy --topo "${lab_file}" 2>&1 | tee "${artifact_dir}/deploy.log"

wait_metric() {
  local router="$1" expected="$2" file="$3"
  local actual
  for _ in $(seq 1 80); do
    if docker exec "${lab_prefix}${router}" \
      vtysh -c "show ip route ${service_prefix} json" >"${file}" 2>"${file}.err" &&
      grep -Eq '"metric"[[:space:]]*:[[:space:]]*'"${expected}"'([,}])' "${file}"; then
      actual="$("${runtime_n1[@]}" metric --route "${file}")"
      if [[ "${actual}" == "${expected}" ]]; then
        return 0
      fi
    fi
    sleep 0.25
  done
  docker exec "${lab_prefix}${router}" vtysh -c "show ip route json" \
    >"${file}.full-rib.json" 2>&1 || true
  echo "${router} did not converge to metric ${expected}" >&2
  return 1
}

wait_ping() {
  local client="$1" file="$2"
  for _ in $(seq 1 20); do
    if docker exec "${lab_prefix}${client}" \
      ping -n -c 1 -W 1 "${service_address}" >"${file}" 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "${client} did not reach the dual-homed service" >&2
  return 1
}

wait_neighbor_count() {
  local router="$1" expected="$2" file="$3"
  local actual
  for _ in $(seq 1 80); do
    if docker exec "${lab_prefix}${router}" \
      vtysh -c "show ip ospf neighbor json" >"${file}" 2>"${file}.err"; then
      actual="$("${runtime_n1[@]}" neighbor-count --ospf "${file}")"
      if [[ "${actual}" == "${expected}" ]]; then
        return 0
      fi
    fi
    sleep 0.25
  done
  echo "${router} did not restore ${expected} Full OSPF adjacencies" >&2
  return 1
}

wait_full_mesh() {
  local directory="$1" router expected
  mkdir -p "${directory}"
  while IFS=$'\t' read -r router expected; do
    [[ "${router}" =~ ^[a-z][a-z0-9-]{0,63}$ ]]
    [[ "${expected}" =~ ^[0-9]+$ ]]
    wait_neighbor_count "${router}" "${expected}" "${directory}/${router}.json"
  done < <("${runtime_n1[@]}" adjacencies --plan "${plan_file}")
}

wait_full_mesh "${artifact_dir}/baseline-neighbors"
wait_metric access1 "${access1_baseline}" "${artifact_dir}/baseline-access1-route.json"
wait_metric access2 "${access2_baseline}" "${artifact_dir}/baseline-access2-route.json"
wait_ping client-a "${artifact_dir}/baseline-client-a.log"
wait_ping client-b "${artifact_dir}/baseline-client-b.log"

printf '{"schema_version":1,"source":"containerlab_frr_observation","scenarios":[' \
  >"${observation_file}"
scenario_count=0
first_scenario=1

while IFS=$'\t' read -r scenario_id kind component injections access1_expected access2_expected; do
  [[ "${scenario_id}" =~ ^(link|node):[a-z][a-z0-9-]{0,127}$ ]]
  [[ "${kind}" == "link" || "${kind}" == "node" ]]
  [[ "${component}" =~ ^[a-z][a-z0-9-]{0,127}$ ]]
  [[ "${access1_expected}" =~ ^[0-9]+$ ]]
  [[ "${access2_expected}" =~ ^[0-9]+$ ]]

  scenario_slug="${scenario_id/:/-}"
  scenario_dir="${artifact_dir}/scenarios/${scenario_slug}"
  mkdir -p "${scenario_dir}"
  IFS=',' read -r -a injection_items <<<"${injections}"
  if [[ "${#injection_items[@]}" -eq 0 ]]; then
    echo "scenario has no injection interfaces: ${scenario_id}" >&2
    exit 1
  fi
  printf '%s\n' "${injection_items[@]}" >"${scenario_dir}/injections.txt"
  for endpoint in "${injection_items[@]}"; do
    set_interface_state "${endpoint}" down
    node="${endpoint%%:*}"
    interface="${endpoint##*:}"
    docker exec "${lab_prefix}${node}" cat "/sys/class/net/${interface}/operstate" \
      | tee -a "${scenario_dir}/fault-operstate.log" \
      | grep -qx 'down'
  done

  wait_metric access1 "${access1_expected}" "${scenario_dir}/fault-access1-route.json"
  wait_metric access2 "${access2_expected}" "${scenario_dir}/fault-access2-route.json"
  wait_ping client-a "${scenario_dir}/fault-client-a.log"
  wait_ping client-b "${scenario_dir}/fault-client-b.log"
  access1_fault="$("${runtime_n1[@]}" metric \
    --route "${scenario_dir}/fault-access1-route.json")"
  access2_fault="$("${runtime_n1[@]}" metric \
    --route "${scenario_dir}/fault-access2-route.json")"

  for endpoint in "${injection_items[@]}"; do
    set_interface_state "${endpoint}" up
  done
  wait_full_mesh "${scenario_dir}/restored-neighbors"
  wait_metric access1 "${access1_baseline}" "${scenario_dir}/restored-access1-route.json"
  wait_metric access2 "${access2_baseline}" "${scenario_dir}/restored-access2-route.json"
  wait_ping client-a "${scenario_dir}/restored-client-a.log"
  wait_ping client-b "${scenario_dir}/restored-client-b.log"
  access1_restored="$("${runtime_n1[@]}" metric \
    --route "${scenario_dir}/restored-access1-route.json")"
  access2_restored="$("${runtime_n1[@]}" metric \
    --route "${scenario_dir}/restored-access2-route.json")"

  if [[ "${first_scenario}" == "0" ]]; then
    printf ',' >>"${observation_file}"
  fi
  first_scenario=0
  printf '%s' \
    "{\"scenario_id\":\"${scenario_id}\",\"paths\":[" \
    "{\"access_node\":\"access1\",\"fault_metric\":${access1_fault}," \
    "\"fault_reachable\":true,\"restored_metric\":${access1_restored}," \
    "\"restored_reachable\":true}," \
    "{\"access_node\":\"access2\",\"fault_metric\":${access2_fault}," \
    "\"fault_reachable\":true,\"restored_metric\":${access2_restored}," \
    "\"restored_reachable\":true}]}" >>"${observation_file}"
  scenario_count=$((scenario_count + 1))
done < <("${runtime_n1[@]}" rows --plan "${plan_file}")

printf ']}\n' >>"${observation_file}"
if [[ "${scenario_count}" != "15" ]]; then
  echo "expected 15 runtime N-1 scenarios, observed ${scenario_count}" >&2
  exit 1
fi

"${runtime_n1[@]}" verify \
  --plan "${plan_file}" \
  --observation "${observation_file}" \
  --output "${evidence_file}"
echo "RUNTIME_N1_MATRIX scenarios=15 checks=120 status=pass" | \
  tee "${artifact_dir}/result.log"
