# Shared-risk failure-domain design audit

## Question

Does the dual-homed candidate preserve service reachability when components that share a declared conduit, power domain, or site fail together?

Passing link and node N-1 proves only that the modeled components survive one independent failure at a time. It does not prove that the two apparent paths are physically independent. This audit makes selected common-cause assumptions explicit in `lab/failure-domains.yml` and applies them as composite `FaultScenario` objects through the common audit engine.

The catalog records the canonical SHA-256 of the candidate topology. Startup rejects the audit when the supplied topology differs, preventing results from being labeled as `service-dual-homed` after an untracked topology substitution. Domain and component counts are capped, and duplicate YAML mapping keys are rejected.

## Declared scenarios and result

| Failure domain | Type | Components removed together | Result | Access-path cost |
|---|---|---|---|---|
| `primary-site-power` | `power_domain` | nodes `agg1`, `core1` | `DEGRADED` | access1 `30 → 140`; access2 `50 → 50` |
| `service-entry` | `shared_conduit` | links `core1--service-host`, `core2--service-host` | **`OUTAGE`** | access1·access2 become unreachable |

Summary: 2 declared failure domains, 1 outage, 1 degraded, and 0 redundancy-reduced. The dual-homed candidate passes all 11 single-link and all four transport-node checks, but does **not** preserve reachability for every declared shared-risk failure.

## What the finding means

`service-entry` shows that two logical adjacencies do not remove a common service-entry risk when both are assigned to the same failure domain. A design change would need an independently modeled service attachment or another service location before this declared risk could pass.

`primary-site-power` removes the primary aggregation and core together. Access1 retains the backup path with a cost increase from 30 to 140, while access2's active cost remains 50. The classification is therefore `DEGRADED`, not an outage.

## Interfaces

```bash
curl http://127.0.0.1:8000/api/resilience/failure-domains/candidate
curl http://127.0.0.1:8000/metrics | grep telconet_srlg
```

Grafana provisions the domain status, impact counts, and scenario matrix at `http://127.0.0.1:3000/d/telconet-srlg-resilience`.

`evidence/failure-domain-audit.json` records the deterministic result with `measured: false`. The integration suite rebuilds it from `intent-dual-homed.yml` and `failure-domains.yml`, so hand-edited result claims fail CI.

## Scope boundary

The failure domains are declared hypotheses for deterministic graph analysis. Containerlab does not establish real conduit routes, power feeds, rack placement, physical path diversity, capacity headroom, or correlated failure probability. No convergence time or packet-loss value is inferred from this audit. Those properties require inventory evidence and controlled lab or field measurements.
