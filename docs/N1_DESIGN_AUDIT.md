# N-1 single-link design audit

## Question

Does every Access node retain a path to a service node after any one modeled link is removed?

The audit loads `lab/intent.yml`, excludes each link exactly once, and compares every Access node's baseline and post-failure shortest-path cost. It reuses the same topology-aware impact logic as the incident API.

## Classification contract

| Classification | Condition |
|---|---|
| `OUTAGE` | At least one Access node loses every path to every service node |
| `DEGRADED` | All Access nodes remain reachable, but at least one shortest-path cost increases |
| `REDUNDANCY_REDUCED` | Active shortest-path costs are unchanged after the link is removed |

The design passes single-link N-1 only when the audit contains zero `OUTAGE` scenarios. A pass does not claim unchanged latency, capacity, or production availability.

## Historical baseline result

| Failed link | Classification | Affected Access nodes |
|---|---|---|
| `access1--agg1` | `DEGRADED` | access1 |
| `access1--agg2` | `REDUNDANCY_REDUCED` | none |
| `access2--agg1` | `REDUNDANCY_REDUCED` | none |
| `access2--agg2` | `DEGRADED` | access2 |
| `agg1--core1` | `DEGRADED` | access1 |
| `agg1--core2` | `REDUNDANCY_REDUCED` | none |
| `agg2--core1` | `REDUNDANCY_REDUCED` | none |
| `agg2--core2` | `DEGRADED` | access2 |
| `core1--core2` | `DEGRADED` | access2 |
| `core1--service-host` | **`OUTAGE`** | access1, access2 |

Summary: 10 scenarios, 1 outage, 5 degraded, and 4 redundancy-reduced. The historical baseline topology therefore does not pass the single-link N-1 criterion.

## Design finding

The Access, Aggregation, and Core transit layers have alternate paths. In the baseline, the service network is attached only through `core1--service-host`, so that link is the unique service-facing single point of failure.

## Dual-homed candidate and measured lab check

`lab/intent-dual-homed.yml` preserves the baseline graph and adds `core2--service-host` at cost 30. Its 11 modeled single-link failures produce **0 OUTAGE**, so the candidate passes the graph-based N-1 criterion. The original baseline, configurations, 40-trial evidence, and historical fingerprint remain unchanged.

The separate `lab/telconet-dual-homed.clab.yml` runs an FRR service endpoint advertising `10.20.0.10/32` into OSPF through both cores. The [successful containerlab CI run](https://github.com/teriyakki-jin/telconet-sentinel/actions/runs/35316616585) records access1's service route at metric **30 before**, **70 after core1's service-facing link is down**, and **30 after restoration**. The access1 and access2 clients each completed an ICMP probe to the service VIP after failover. This is one physical lab fault experiment, not 11 physical fault injections or a zero-loss test.

Run locally with `bash scenarios/dual_homing_e2e.sh` where containerlab and Docker are available. The script refuses to replace an existing candidate lab and stores raw artifacts under `artifacts/dual-homing/`.

## Interfaces

```bash
curl http://127.0.0.1:8000/api/resilience/single-link-failures
curl http://127.0.0.1:8000/api/resilience/single-link-failures/candidate
curl http://127.0.0.1:8000/metrics | grep telconet_n1
```

Grafana compares baseline GAP (10 scenarios, 1 OUTAGE) and candidate PASS (11 scenarios, 0 OUTAGE) at `http://127.0.0.1:3000/d/telconet-n1-resilience`. The link-by-link table remains the baseline audit.

## Scope boundary

The N-1 results are deterministic analyses of declared graphs and OSPF costs. They do not model shared-risk link groups, node or power failures, ECMP capacity, packet loss, protocol convergence time, or physical path diversity. The candidate lab service endpoint runs FRR and OSPF; it is not an unmodified general-purpose server. Those other properties require additional intent fields and lab experiments rather than assumptions.
