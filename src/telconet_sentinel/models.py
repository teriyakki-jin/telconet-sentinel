from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class NodeRole(str, Enum):
    ACCESS = "access"
    AGGREGATION = "aggregation"
    CORE = "core"
    SERVICE = "service"


class ServiceImpact(str, Enum):
    DEGRADED = "degraded"
    OUTAGE = "outage"
    REDUNDANCY_REDUCED = "redundancy_reduced"


class IncidentStatus(str, Enum):
    ANALYZED = "analyzed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    role: NodeRole
    prefixes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Link:
    id: str
    endpoint_a: str
    endpoint_b: str
    cost: int = 1


@dataclass(frozen=True, slots=True)
class NetworkEvent:
    link_id: str
    event_type: str = "link_down"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ImpactAnalysis:
    failed_component: str
    evidence: tuple[str, ...]
    affected_nodes: tuple[str, ...]
    affected_prefixes: tuple[str, ...]
    service_impact: ServiceImpact


@dataclass(slots=True)
class RecoveryPlan:
    incident_id: str
    action: str
    target: str
    status: IncidentStatus = IncidentStatus.AWAITING_APPROVAL


@dataclass(slots=True)
class Incident:
    id: str
    fingerprint: str
    event: NetworkEvent
    analysis: ImpactAnalysis
    recovery_plan: RecoveryPlan
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
