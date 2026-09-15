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

## Current result

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

Summary: 10 scenarios, 1 outage, 5 degraded, and 4 redundancy-reduced. The current topology therefore does not pass the single-link N-1 criterion.

## Design finding

The Access, Aggregation, and Core transit layers have alternate paths. The service network is attached only through `core1--service-host`, so that link is the unique service-facing single point of failure. Adding an independently costed service attachment to core2 is the smallest topology change that should remove this link-failure outage; the same audit must then be rerun to verify the claim.

## Interfaces

```bash
curl http://127.0.0.1:8000/api/resilience/single-link-failures
curl http://127.0.0.1:8000/metrics | grep telconet_n1
```

Grafana displays the pass status, classification counts, and per-link matrix at `http://127.0.0.1:3000/d/telconet-n1-resilience`.

## Scope boundary

This is deterministic analysis of the declared graph and OSPF costs. It does not model shared-risk link groups, node or power failures, ECMP capacity, packet loss, protocol convergence time, or physical path diversity. Those require additional intent fields and lab experiments rather than assumptions.
