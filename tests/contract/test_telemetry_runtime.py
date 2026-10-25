from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_api_image_contains_only_the_declarative_telemetry_inventory() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY lab/telemetry-targets.yml ./lab/telemetry-targets.yml" in dockerfile
    assert "COPY lab/telconet.clab.yml ./lab/telconet.clab.yml" in dockerfile
    assert "TELCONET_TELEMETRY_TARGETS=/app/lab/telemetry-targets.yml" in dockerfile
    assert "/var/run/docker.sock" not in dockerfile


def test_compose_persists_telemetry_without_exposing_docker_to_the_api() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]

    assert api["environment"]["TELCONET_TELEMETRY_TARGETS"] == (
        "/app/lab/telemetry-targets.yml"
    )
    assert api["environment"]["TELCONET_TELEMETRY_DB"] == (
        "/var/lib/telconet/telemetry.sqlite3"
    )
    assert all("docker.sock" not in volume for volume in api.get("volumes", []))
    assert api["read_only"] is True


def test_compose_allows_the_provisioned_one_second_dashboard_refresh() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert compose["services"]["grafana"]["environment"][
        "GF_DASHBOARDS_MIN_REFRESH_INTERVAL"
    ] == "1s"


def test_demo_script_runs_the_host_collector_without_mounting_the_docker_socket() -> None:
    script = (ROOT / "scenarios" / "telemetry_demo.sh").read_text(encoding="utf-8")

    assert "telconet-telemetry" in script
    assert "--config" in script
    assert "--once" in script
    assert "docker.sock" not in script
