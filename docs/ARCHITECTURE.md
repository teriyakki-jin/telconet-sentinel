# Architecture

## Design goal

TelcoNet Sentinel separates the network lab, analysis logic, and recovery execution boundary. The API analyzes an event and creates a typed recovery proposal, but it does not receive or execute arbitrary shell commands.

```mermaid
flowchart LR
    CLAB["containerlab · FRR routers"] --> EVENT["link event"]
    EVENT --> API["FastAPI incident service"]
    INTENT["intent.yml"] --> GRAPH["in-memory topology graph"]
    GRAPH --> IMPACT["cost-aware impact analysis"]
    API --> IMPACT
    IMPACT --> INCIDENT["incident + impact + evidence"]
    INCIDENT --> APPROVAL["typed state transition"]
    APPROVAL --> RUNBOOK["local allowlisted runbook"]
    RUNBOOK --> CLAB
```

## Trust boundaries

- The API accepts `event_type`, `link_id`, and an optional timestamp.
- Only topology link identifiers are valid recovery targets.
- Only `restore_link` is allowed in Phase 1.
- The Phase 1 approval endpoint has no operator identity; it changes local typed state only.
- The local runbook owns privileged lab commands and is not invoked by the API.
- The API container does not mount the Docker socket.

## Topology

```mermaid
flowchart TB
    CA[client-a] --- A1[access1]
    CB[client-b] --- A2[access2]
    A1 --- G1[agg1]
    A1 --- G2[agg2]
    A2 --- G1
    A2 --- G2
    G1 --- C1[core1]
    G1 --- C2[core2]
    G2 --- C1
    G2 --- C2
    C1 --- C2
    C1 --- SVC[service-host]
```

All router links participate in OSPF area 0. Interface costs create explicit primary and backup paths. The customer-facing `/24` networks are advertised from passive access interfaces.

## Phase boundaries

- Phase 1: OSPF cost-aware impact analysis, typed recovery state, scenario evidence.
- Phase 2: BFD and FRR syslog collection, measured convergence comparison.
- Phase 3: BGP/MPLS L3VPN and streaming telemetry.
