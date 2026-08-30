import pytest

from telconet_sentinel.models import NetworkEvent
from telconet_sentinel.service import IncidentService
from telconet_sentinel.topology import Topology


def test_server_clock_opens_new_incident_after_dedup_window(
    redundant_topology: Topology,
) -> None:
    current = [100.0]
    service = IncidentService(redundant_topology, clock=lambda: current[0])
    first = service.ingest(NetworkEvent("access1--agg1"))

    current[0] += 61
    recurrence = service.ingest(NetworkEvent("access1--agg1"))

    assert recurrence.id != first.id


def test_incident_store_evicts_oldest_entry(redundant_topology: Topology) -> None:
    service = IncidentService(redundant_topology, max_incidents=2)
    first = service.ingest(NetworkEvent("access1--agg1"))
    service.ingest(NetworkEvent("access1--agg2"))
    service.ingest(NetworkEvent("access2--agg1"))

    with pytest.raises(KeyError, match="incident not found"):
        service.get(first.id)
