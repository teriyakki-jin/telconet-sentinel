# Intent schema and fault model

## Purpose

The network declaration, design relationship, fault definition, and audit result are separate contracts. This prevents YAML parsing details, graph operations, and API response shapes from becoming one coupled module.

## Design catalog

`lab/designs.yml` identifies the immutable historical baseline and its candidate design.

```yaml
version: 1
designs:
  historical-baseline:
    intent: intent.yml
    role: baseline
  service-dual-homed:
    intent: intent-dual-homed.yml
    role: candidate
    based_on: historical-baseline
```

The catalog is separate because `lab/intent.yml` participates in the configuration fingerprint for the checked-in BFD trials. Adding metadata to that file would incorrectly make historical evidence appear to have been collected from a different configuration.

Catalog paths accept local YAML filenames only. Parent traversal, absolute paths, duplicate intent paths, missing baselines, multiple baselines, and unknown fields are rejected.

## Network intent

`NetworkIntent` is a strict Pydantic model for schema version 1. It validates the entire document before producing a `Topology`.

Validation includes:

- supported schema version;
- known fields only at every level;
- bounded node, link, and design identifiers;
- canonical IP prefixes and no duplicate advertised prefix;
- positive strict-integer link cost, rejecting booleans;
- unique link identifiers;
- two known, different endpoints per link.

`config.load_topology()` remains as the compatibility entry point and delegates to `load_intent().to_topology()`.

## Fault scenario

`FaultScenario` is immutable and contains sets of excluded links and nodes.

```python
FaultScenario(
    id="shared-risk:service-entry",
    kind=FaultKind.COMPOSITE,
    excluded_links=frozenset(
        {"core1--service-host", "core2--service-host"}
    ),
)
```

Single-link and transport-node factories generate deterministic scenario identifiers. Composite scenarios represent declared SRLG/failure-domain failures without adding another path-classification implementation.

## Failure-domain catalog

`lab/failure-domains.yml` is a separate strict version 1 document bound to the `service-dual-homed` design and its canonical topology SHA-256. Each domain has a bounded identifier, an allowlisted type, and at least two unique link or node components. The loader also caps domain/component counts and rejects duplicate YAML mapping keys. Unknown fields, coerced versions, duplicate components, nonexistent topology references, and topology fingerprint mismatches are rejected.

The sidecar intentionally does not modify either intent file or the historical experiment fingerprint. A declaration is an analysis assumption: it records which components should fail together, but does not claim that the lab discovered a real conduit, power feed, or site relationship.

## Common audit engine

`audit_fault_scenarios()` performs one algorithm:

1. Validate Access nodes, service nodes, baseline reachability, scenario identity, and component references.
2. Calculate each Access node's baseline shortest-path cost.
3. Recalculate with every link and node declared by the fault excluded.
4. Classify the result as `OUTAGE`, `DEGRADED`, or `REDUNDANCY_REDUCED`.
5. Return the affected Access nodes, advertised prefixes, and each Access node's baseline and post-fault path cost.

The established link and node API responses and Prometheus metrics remain unchanged. They are adapters over the common result rather than separate analysis implementations. The failure-domain adapter exposes the same result through `/api/resilience/failure-domains/candidate` and `telconet_srlg_*` metrics.
