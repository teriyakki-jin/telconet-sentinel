# Runbook: Access uplink failure

## Purpose

Validate that traffic to a service behind the Core remains reachable after the primary `access1--agg1` link fails and that impact analysis classifies the path-cost increase as `degraded`.

## Preconditions

- WSL2 or Linux
- A running Docker Engine available inside that environment
- containerlab installed
- `quay.io/frrouting/frr:10.7.0` and `alpine:3.22` available or pullable
- Python environment installed with `pip install -e .`

## Deploy and inspect

```bash
containerlab deploy --topo lab/telconet.clab.yml
docker exec clab-telconet-sentinel-access1 vtysh -c 'show ip ospf neighbor'
docker exec clab-telconet-sentinel-access1 vtysh -c 'show ip route ospf'
docker exec clab-telconet-sentinel-client-a ping -c 5 10.20.0.10
docker exec clab-telconet-sentinel-client-a traceroute -n 10.20.0.10
```

Expected state: `access1` has OSPF adjacencies toward both aggregation routers and client-to-client traffic succeeds.

## Inject, analyze, and restore

```bash
docker exec clab-telconet-sentinel-access1 ip link set eth1 down
python -m telconet_sentinel.demo --link access1--agg1 --output evidence/simulated-link-failure.json
docker exec clab-telconet-sentinel-client-a ping -c 10 10.20.0.10
docker exec clab-telconet-sentinel-client-a traceroute -n 10.20.0.10
docker exec clab-telconet-sentinel-access1 ip link set eth1 up
```

The generated JSON is analysis of a scenario-injected event. It is not a convergence measurement. Add `convergence_ms` or packet-loss values only after capturing them from the running lab.

## Cleanup

```bash
containerlab destroy --topo lab/telconet.clab.yml
```
