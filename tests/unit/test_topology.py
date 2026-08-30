import pytest

from telconet_sentinel.models import Link, Node, NodeRole
from telconet_sentinel.topology import Topology


def test_redundant_access_keeps_core_path_when_primary_link_fails(
    redundant_topology: Topology,
) -> None:
    assert redundant_topology.has_path("access1", {"core1", "core2"}, excluded_link="access1--agg1")


def test_neighbors_are_deterministic(redundant_topology: Topology) -> None:
    assert redundant_topology.neighbors("access1") == ("agg1", "agg2")


def test_shortest_distance_uses_link_cost_and_exclusion(redundant_topology: Topology) -> None:
    assert redundant_topology.shortest_distance("access1", {"core1", "core2"}) == 20
    assert (
        redundant_topology.shortest_distance(
            "access1", {"core1", "core2"}, excluded_link="access1--agg1"
        )
        == 110
    )


def test_rejects_link_with_unknown_endpoint() -> None:
    nodes = [Node("access1", NodeRole.ACCESS)]

    with pytest.raises(ValueError, match="unknown endpoint"):
        Topology(nodes, [Link("bad-link", "access1", "missing")])


def test_rejects_duplicate_link_id() -> None:
    nodes = [Node("access1", NodeRole.ACCESS), Node("agg1", NodeRole.AGGREGATION)]
    links = [
        Link("access1--agg1", "access1", "agg1"),
        Link("access1--agg1", "agg1", "access1"),
    ]

    with pytest.raises(ValueError, match="duplicate link"):
        Topology(nodes, links)


def test_rejects_non_positive_link_cost() -> None:
    nodes = [Node("access1", NodeRole.ACCESS), Node("agg1", NodeRole.AGGREGATION)]

    with pytest.raises(ValueError, match="positive"):
        Topology(nodes, [Link("access1--agg1", "access1", "agg1", 0)])
