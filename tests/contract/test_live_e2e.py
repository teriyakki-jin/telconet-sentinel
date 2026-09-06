import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_live_dashboard_exposes_each_convergence_stage() -> None:
    dashboard = json.loads(
        (
            ROOT / "observability" / "grafana" / "dashboards" / "live-convergence.json"
        ).read_text(encoding="utf-8")
    )

    assert dashboard["uid"] == "telconet-live-convergence"
    assert dashboard["title"] == "TelcoNet Sentinel · Live Convergence Timeline"
    assert dashboard["refresh"] == "1s"
    queries = {
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    }
    assert "telconet_live_event_offset_seconds" in queries
    assert "telconet_live_bfd_peer_up" in queries
    assert "telconet_live_ospf_neighbor_full" in queries
    assert "telconet_live_route_metric" in queries
    assert "telconet_live_data_plane_reachable" in queries


def test_prometheus_scrapes_live_state_each_second() -> None:
    config = yaml.safe_load(
        (ROOT / "observability" / "prometheus.yml").read_text(encoding="utf-8")
    )
    job = next(item for item in config["scrape_configs"] if item["job_name"] == "telconet")

    assert config["global"]["scrape_interval"] == "1s"
    assert job["scrape_interval"] == "1s"


def test_e2e_workflow_deploys_runs_and_always_collects_artifacts() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "lab-e2e.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    triggers = workflow["on"]
    job = workflow["jobs"]["lab-e2e"]
    steps = job["steps"]

    assert {"pull_request", "schedule", "workflow_dispatch"} <= set(triggers)
    assert job["timeout-minutes"] == "20"
    assert workflow["permissions"] == {"contents": "read"}
    assert any(
        step.get("run") == "bash scenarios/live_convergence_e2e.sh" for step in steps
    )
    upload = next(step for step in steps if step.get("name") == "Upload lab evidence")
    assert upload["if"] == "always()"
    assert "artifacts/live-convergence" in upload["with"]["path"]


def test_e2e_script_has_a_cleanup_trap_and_uses_the_live_collector() -> None:
    script = (ROOT / "scenarios" / "live_convergence_e2e.sh").read_text(
        encoding="utf-8"
    )

    assert "containerlab deploy" in script
    assert "telconet_sentinel.live_collector" in script
    assert "assert events == required" in script
    assert "containerlab destroy" in script
    assert "trap cleanup EXIT" in script
