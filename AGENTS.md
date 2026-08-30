# TelcoNet Sentinel working agreement

## Scope

- This repository is a local simulation of an IP transport network for learning and portfolio use.
- Never include real carrier topology, device names, addresses, credentials, or operational data.
- Describe results as simulation results; do not generalize them to a production carrier network.

## Engineering rules

- Write or update a failing test before changing domain, API, or evidence behavior.
- Keep branch coverage at or above 80%.
- `lab/intent.yml` is the source of truth for impact-analysis nodes, costs, and links.
- Recovery requests accept only typed, allowlisted actions. Never accept shell commands through the API.
- Do not invent convergence, packet-loss, or MTTR values. Record performance fields only after a real lab run.
- Keep the initial implementation explainable and deterministic; do not add ML to root-cause analysis.

## Validation

```text
pytest -q --cov=telconet_sentinel --cov-branch --cov-fail-under=80
ruff check .
mypy src
```
