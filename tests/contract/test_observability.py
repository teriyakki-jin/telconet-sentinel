import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_declares_local_hardened_observability_services() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["api"]["environment"]["TELCONET_REPEATED_EXPERIMENT"] == (
        "/app/evidence/bfd-repeated-trials.json"
    )
    assert services["prometheus"]["image"] == "prom/prometheus:v3.14.0"
    assert services["grafana"]["image"] == "grafana/grafana:13.1.0"
    assert services["prometheus"]["ports"] == ["127.0.0.1:9090:9090"]
    assert services["grafana"]["ports"] == ["127.0.0.1:3000:3000"]
    assert services["grafana"]["environment"][
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"
    ] == "/etc/grafana/dashboards/bfd-comparison.json"
    assert services["grafana"]["environment"]["GF_AUTH_BASIC_ENABLED"] == "false"
    assert services["grafana"]["environment"][
        "GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION"
    ] == "true"
    assert services["grafana"]["environment"]["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"
    assert (
        "./observability/grafana/provisioning/datasources:"
        "/etc/grafana/provisioning/datasources:ro"
    ) in services["grafana"]["volumes"]
    for service in ("api", "prometheus", "grafana"):
        assert services[service]["security_opt"] == ["no-new-privileges:true"]
    assert services["prometheus"]["read_only"] is True
    assert services["grafana"]["read_only"] is True

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "TELCONET_REPEATED_EXPERIMENT=/app/evidence/bfd-repeated-trials.json" in (
        dockerfile
    )
    assert "COPY evidence/bfd-repeated-trials.json" in dockerfile


def test_prometheus_scrapes_the_api_metrics_endpoint() -> None:
    config = yaml.safe_load(
        (ROOT / "observability" / "prometheus.yml").read_text(encoding="utf-8")
    )
    job = next(item for item in config["scrape_configs"] if item["job_name"] == "telconet")

    assert job["metrics_path"] == "/metrics"
    assert job["scrape_interval"] == "5s"
    assert job["static_configs"] == [{"targets": ["api:8000"]}]


def test_grafana_provisions_prometheus_and_bfd_dashboard() -> None:
    datasource = yaml.safe_load(
        (
            ROOT
            / "observability"
            / "grafana"
            / "provisioning"
            / "datasources"
            / "prometheus.yml"
        ).read_text(encoding="utf-8")
    )["datasources"][0]
    dashboard = json.loads(
        (
            ROOT / "observability" / "grafana" / "dashboards" / "bfd-comparison.json"
        ).read_text(encoding="utf-8")
    )

    assert datasource["uid"] == "prometheus"
    assert datasource["url"] == "http://prometheus:9090"
    assert datasource["isDefault"] is True
    assert datasource["editable"] is False
    assert dashboard["uid"] == "telconet-bfd-comparison"
    assert dashboard["title"] == "TelcoNet Sentinel · OSPF vs BFD"
    assert {"telconet", "ospf", "bfd"} <= set(dashboard["tags"])

    queries = {
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    }
    assert 'telconet_detection_seconds{profile="ospf_only"}' in queries
    assert 'telconet_detection_seconds{profile="bfd_100x3"}' in queries
    assert "telconet_failover_lost_packets" in queries
    assert "telconet_capture_packet_loss_ratio" in queries
    assert (
        '(sum(telconet_detection_seconds{profile="ospf_only"}) - '
        'sum(telconet_detection_seconds{profile="bfd_100x3"})) / '
        'sum(telconet_detection_seconds{profile="ospf_only"}) * 100'
    ) in queries


def test_grafana_provisions_repeated_trial_distribution_dashboard() -> None:
    dashboard = json.loads(
        (
            ROOT
            / "observability"
            / "grafana"
            / "dashboards"
            / "bfd-repeated-trials.json"
        ).read_text(encoding="utf-8")
    )

    assert dashboard["uid"] == "telconet-bfd-repeated-trials"
    assert dashboard["title"] == "TelcoNet Sentinel · 20-Trial Distribution"
    queries = {
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    }
    assert 'telconet_detection_summary_seconds{stat="p50"}' in queries
    assert 'telconet_detection_summary_seconds{stat="p95"}' in queries
    assert 'telconet_detection_summary_seconds{stat="max"}' in queries
    assert "telconet_trial_detection_seconds" in queries
