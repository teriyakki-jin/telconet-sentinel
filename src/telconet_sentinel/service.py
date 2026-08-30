from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from threading import RLock
from time import monotonic
from uuid import uuid4

from .impact import analyze_link_down
from .models import Incident, IncidentStatus, NetworkEvent
from .recovery import approve_recovery, create_recovery_plan
from .topology import Topology


class IncidentService:
    def __init__(
        self,
        topology: Topology,
        dedup_window: timedelta = timedelta(seconds=60),
        clock: Callable[[], float] = monotonic,
        max_incidents: int = 1_000,
    ) -> None:
        if dedup_window <= timedelta(0):
            raise ValueError("dedup window must be positive")
        if max_incidents <= 0:
            raise ValueError("max incidents must be positive")
        self.topology = topology
        self._dedup_window = dedup_window
        self._clock = clock
        self._max_incidents = max_incidents
        self._incidents: OrderedDict[str, Incident] = OrderedDict()
        self._incident_id_by_fingerprint: dict[str, str] = {}
        self._last_seen_by_fingerprint: dict[str, float] = {}
        self._lock = RLock()

    def ingest(self, event: NetworkEvent) -> Incident:
        with self._lock:
            received_at = self._clock()
            fingerprint = sha256(f"{event.event_type}:{event.link_id}".encode()).hexdigest()
            existing_id = self._incident_id_by_fingerprint.get(fingerprint)
            if existing_id is not None:
                existing = self._incidents[existing_id]
                elapsed = received_at - self._last_seen_by_fingerprint[fingerprint]
                if (
                    existing.recovery_plan.status is IncidentStatus.AWAITING_APPROVAL
                    and 0 <= elapsed <= self._dedup_window.total_seconds()
                ):
                    self._last_seen_by_fingerprint[fingerprint] = received_at
                    return existing

            analysis = analyze_link_down(self.topology, event)
            incident_id = f"inc-{uuid4().hex[:12]}"
            plan = create_recovery_plan(
                self.topology,
                incident_id=incident_id,
                action="restore_link",
                target=analysis.failed_component,
            )
            incident = Incident(
                id=incident_id,
                fingerprint=fingerprint,
                event=event,
                analysis=analysis,
                recovery_plan=plan,
            )
            self._incidents[incident.id] = incident
            self._incident_id_by_fingerprint[fingerprint] = incident.id
            self._last_seen_by_fingerprint[fingerprint] = received_at
            while len(self._incidents) > self._max_incidents:
                evicted_id, evicted = self._incidents.popitem(last=False)
                if self._incident_id_by_fingerprint.get(evicted.fingerprint) == evicted_id:
                    del self._incident_id_by_fingerprint[evicted.fingerprint]
                    del self._last_seen_by_fingerprint[evicted.fingerprint]
            return incident

    def get(self, incident_id: str) -> Incident:
        with self._lock:
            try:
                return self._incidents[incident_id]
            except KeyError as exc:
                raise KeyError(f"incident not found: {incident_id}") from exc

    def approve(self, incident_id: str) -> Incident:
        with self._lock:
            incident = self.get(incident_id)
            approve_recovery(incident.recovery_plan)
            return incident
