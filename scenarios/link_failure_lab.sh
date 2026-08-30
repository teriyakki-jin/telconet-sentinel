#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lab_file="${project_root}/lab/telconet.clab.yml"

command -v containerlab >/dev/null || {
  echo "containerlab is required inside WSL/Linux" >&2
  exit 1
}
docker info >/dev/null

containerlab deploy --topo "${lab_file}"

client_a="clab-telconet-sentinel-client-a"
access_1="clab-telconet-sentinel-access1"
service_ip="10.20.0.10"

echo "Baseline connectivity"
docker exec "${client_a}" ping -c 5 "${service_ip}"
docker exec "${client_a}" traceroute -n "${service_ip}"

echo "Injecting access1--agg1 link failure"
docker exec "${access_1}" ip link set eth1 down
docker exec "${client_a}" ping -c 10 "${service_ip}"
docker exec "${client_a}" traceroute -n "${service_ip}"

echo "Restoring access1--agg1 link"
docker exec "${access_1}" ip link set eth1 up
docker exec "${client_a}" ping -c 5 "${service_ip}"

echo "Lab remains running for inspection. Destroy it with:"
echo "containerlab destroy --topo ${lab_file}"
