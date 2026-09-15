# Convergence state and alert runbook

## Scope

This runbook covers the local simulation stack only. The 20-second hold is a lab guardrail for persistent abnormal state, not a carrier-network SLO. Prometheus evaluates alerts locally; Alertmanager and external notification receivers are intentionally out of scope.

## State persistence

The API stores the latest 20 convergence runs and their typed events in `/var/lib/telconet/convergence.sqlite3`. Docker Compose mounts the `telconet-state` named volume at that path, so an API process or container restart does not discard the latest timeline.

```bash
docker compose up -d --build
docker compose restart api
curl http://127.0.0.1:8000/api/convergence-runs/latest
```

`docker compose down` preserves the named volume. Treat volume removal as an explicit destructive reset.

## Alert rules

| Alert | Trigger held for 20 seconds | First check |
|---|---|---|
| `TelcoNetApiScrapeMissing` | `up{job="telconet"} == 0` | API container health and `/metrics` |
| `TelcoNetConvergenceStalled` | latest run is incomplete | collector status and latest event |
| `TelcoNetDataPlaneUnreachable` | latest run reports no recovered data plane | ping probe and backup route metric |

Inspect alert state at `http://127.0.0.1:9090/alerts` and the **Firing simulation alerts** panel on the live convergence dashboard. A scrape failure can make application metrics absent, so diagnose `TelcoNetApiScrapeMissing` before relying on the other two alerts.

## Validation

The validation workflow checks both Prometheus configuration and alert transition timing:

```bash
docker run --rm --entrypoint /bin/promtool \
  -v "${PWD}/observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "${PWD}/observability/prometheus/rules:/etc/prometheus/rules:ro" \
  prom/prometheus:v3.14.0 \
  check config /etc/prometheus/prometheus.yml

docker run --rm --entrypoint /bin/promtool \
  -v "${PWD}/observability/prometheus/rules:/etc/prometheus/rules:ro" \
  -v "${PWD}/observability/prometheus/tests:/etc/prometheus/tests:ro" \
  prom/prometheus:v3.14.0 \
  test rules /etc/prometheus/tests/convergence.yml
```

The test asserts that no alert fires before the hold duration and that all three alerts fire for persistent zero-valued simulation inputs.
