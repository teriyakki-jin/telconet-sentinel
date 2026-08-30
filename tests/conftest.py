import pytest

from telconet_sentinel.models import Link, Node, NodeRole
from telconet_sentinel.topology import Topology


@pytest.fixture
def redundant_topology() -> Topology:
    nodes = [
        Node("access1", NodeRole.ACCESS, ("10.10.1.0/24",)),
        Node("access2", NodeRole.ACCESS, ("10.10.2.0/24",)),
        Node("agg1", NodeRole.AGGREGATION),
        Node("agg2", NodeRole.AGGREGATION),
        Node("core1", NodeRole.CORE),
        Node("core2", NodeRole.CORE),
        Node("service-host", NodeRole.SERVICE, ("10.20.0.0/24",)),
    ]
    links = [
        Link("access1--agg1", "access1", "agg1", 10),
        Link("access1--agg2", "access1", "agg2", 100),
        Link("access2--agg1", "access2", "agg1", 100),
        Link("access2--agg2", "access2", "agg2", 10),
        Link("agg1--core1", "agg1", "core1", 10),
        Link("agg1--core2", "agg1", "core2", 100),
        Link("agg2--core1", "agg2", "core1", 100),
        Link("agg2--core2", "agg2", "core2", 10),
        Link("core1--core2", "core1", "core2", 20),
        Link("core1--service-host", "core1", "service-host", 10),
    ]
    return Topology(nodes, links)
