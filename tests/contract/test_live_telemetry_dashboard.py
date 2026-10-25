import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
DASHBOARD_PATH = (
    ROOT / "observability" / "grafana" / "dashboards" / "live-telemetry.json"
)


def _dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def _panels_by_title() -> dict[str, dict[str, Any]]:
    return {panel["title"]: panel for panel in _dashboard()["panels"]}


def _queries(panel: dict[str, Any]) -> set[str]:
    return {str(target["expr"]) for target in panel.get("targets", [])}


def test_live_telemetry_dashboard_metadata_and_navigation() -> None:
    dashboard = _dashboard()

    assert dashboard["uid"] == "telconet-live-telemetry"
    assert dashboard["title"] == "TelcoNet Sentinel · Live Network Telemetry"
    assert dashboard["refresh"] == "1s"
    assert dashboard["schemaVersion"] >= 42
    assert {"telconet", "telemetry", "ospf", "bfd", "containerlab"} <= set(
        dashboard["tags"]
    )

    links = {link["title"]: link["url"] for link in dashboard["links"]}
    assert links["Network overview"] == "/d/telconet-network-overview"
    assert links["Live convergence"] == "/d/telconet-live-convergence"

    built_in_panel_types = {"nodeGraph", "stat", "table", "text", "timeseries"}
    assert {panel["type"] for panel in dashboard["panels"]} <= built_in_panel_types


def test_live_telemetry_dashboard_has_operational_summary_cards() -> None:
    panels = _panels_by_title()

    assert _queries(panels["TELEMETRY AGE"]) == {
        "time() - telconet_telemetry_snapshot_timestamp_seconds"
    }
    assert _queries(panels["HEALTHY ROUTERS"]) == {
        "sum(telconet_router_collection_success)"
    }
    assert _queries(panels["OSPF FULL"]) == {"min(telconet_ospf_neighbor_full)"}
    assert _queries(panels["BFD STATUS"]) == {
        "min(telconet_bfd_peer_up * on(node,peer,interface) "
        "(telconet_bfd_peer_configured == 1))"
    }
    assert _queries(panels["ACCESS1 ROUTE"]) == {
        'telconet_selected_route_metric{node="access1"} or on() vector(-1)'
    }
    assert _queries(panels["ACCESS2 ROUTE"]) == {
        'telconet_selected_route_metric{node="access2"} or on() vector(-1)'
    }
    assert "Primary is 30" in panels["ACCESS1 ROUTE"]["description"]
    assert "backup is 140" in panels["ACCESS1 ROUTE"]["description"]
    assert "Primary is 50" in panels["ACCESS2 ROUTE"]["description"]
    assert "backup is 120" in panels["ACCESS2 ROUTE"]["description"]
    for title in ("ACCESS1 ROUTE", "ACCESS2 ROUTE"):
        assert panels[title]["fieldConfig"]["defaults"]["mappings"][0]["options"][
            "-1"
        ] == {"color": "gray", "text": "UNKNOWN"}

    for title in (
        "TELEMETRY AGE",
        "HEALTHY ROUTERS",
        "OSPF FULL",
        "BFD STATUS",
        "ACCESS1 ROUTE",
        "ACCESS2 ROUTE",
    ):
        panel = panels[title]
        assert panel["type"] == "stat"
        assert panel["fieldConfig"]["defaults"]["noValue"] == "UNKNOWN"
        assert panel["targets"][0]["instant"] is True


def test_live_telemetry_dashboard_uses_a_builtin_metric_backed_node_graph() -> None:
    panel = _panels_by_title()["LIVE NETWORK TOPOLOGY"]

    assert panel["type"] == "nodeGraph"
    assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}
    targets = {target["refId"]: target for target in panel["targets"]}
    assert set(targets) == {"edges", "nodes"}
    assert targets["edges"]["expr"] == (
        'label_replace(telconet_topology_link_oper_up, "id", "$1", '
        '"link_id", "(.*)")'
    )
    assert targets["nodes"]["expr"] == (
        'label_replace(label_replace(label_replace(telconet_topology_node_info, '
        '"id", "$1", "node", "(.*)"), "title", "$1", "node", "(.*)"), '
        '"subtitle", "$1", "role", "(.*)")'
    )
    assert all(target["format"] == "table" for target in targets.values())
    assert all(target["instant"] is True for target in targets.values())
    assert "transformations" not in panel


def test_live_telemetry_dashboard_has_route_neighbor_and_interface_views() -> None:
    panels = _panels_by_title()

    route_table = panels["SELECTED SERVICE ROUTES"]
    assert route_table["type"] == "table"
    assert _queries(route_table) == {
        "(telconet_selected_route_metric * on(node,prefix) group_left(link_id) "
        "telconet_selected_route_egress) and on(node,prefix) "
        "(telconet_selected_route_present == 1)"
    }

    ospf_table = panels["OSPF NEIGHBORS"]
    bfd_table = panels["BFD PEERS"]
    assert ospf_table["type"] == "table"
    assert bfd_table["type"] == "table"
    assert _queries(ospf_table) == {"telconet_ospf_neighbor_full"}
    assert _queries(bfd_table) == {
        "telconet_bfd_peer_up * on(node,peer,interface) "
        "(telconet_bfd_peer_configured == 1)"
    }

    throughput = panels["INTERFACE THROUGHPUT"]
    assert throughput["type"] == "timeseries"
    assert _queries(throughput) == {
        "rate(telconet_interface_receive_bytes_total[15s]) * 8",
        "rate(telconet_interface_transmit_bytes_total[15s]) * 8",
    }
    assert throughput["fieldConfig"]["defaults"]["unit"] == "bps"

    drops = panels["INTERFACE DROPS"]
    assert drops["type"] == "timeseries"
    assert _queries(drops) == {
        "rate(telconet_interface_receive_drops_total[15s])",
        "rate(telconet_interface_transmit_drops_total[15s])",
    }
    assert panels["INTERFACE OPER STATE"]["type"] == "table"
    assert _queries(panels["INTERFACE OPER STATE"]) == {
        "telconet_interface_oper_up"
    }


def test_live_telemetry_dashboard_documents_stale_and_measurement_semantics() -> None:
    panels = _panels_by_title()
    freshness = panels["TELEMETRY AGE"]
    steps = freshness["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert steps == [
        {"color": "green", "value": None},
        {"color": "orange", "value": 3},
        {"color": "red", "value": 10},
    ]

    for title in (
        "OSPF FULL",
        "BFD STATUS",
        "LIVE NETWORK TOPOLOGY",
        "INTERFACE OPER STATE",
        "OSPF NEIGHBORS",
        "BFD PEERS",
    ):
        mappings = panels[title]["fieldConfig"]["defaults"]["mappings"]
        assert mappings[0]["options"]["-1"] == {
            "color": "gray",
            "text": "UNKNOWN",
        }

    content = panels["TELEMETRY CONTRACT"]["options"]["content"]
    normalized = content.casefold()
    assert "host-polled" in normalized
    assert "1 second" in normalized
    assert "unknown" in normalized
    assert "stale" in normalized
    assert "not sub-second convergence evidence" in normalized
    assert "collector" in normalized and "monotonic" in normalized
