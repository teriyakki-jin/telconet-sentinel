from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

BASELINE_ROUTERS = ("access1", "access2", "agg1", "agg2", "core1", "core2")
_MAX_COUNTER = 2**64 - 1
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
_ERROR_PATTERN = re.compile(r"^[^\r\n]{1,256}$")

Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
Counter = Annotated[StrictInt, Field(ge=0, le=_MAX_COUNTER)]


class TelemetryState(str, Enum):
    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


class _TelemetryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InterfaceTelemetry(_TelemetryModel):
    name: Identifier
    state: TelemetryState
    rx_bytes: Counter
    tx_bytes: Counter
    rx_drops: Counter
    tx_drops: Counter


class OspfNeighborTelemetry(_TelemetryModel):
    neighbor: Identifier
    interface: Identifier
    state: TelemetryState


class BfdPeerTelemetry(_TelemetryModel):
    peer: Identifier
    interface: Identifier
    configured: StrictBool
    state: TelemetryState

    @model_validator(mode="after")
    def validate_absence_semantics(self) -> BfdPeerTelemetry:
        if not self.configured and self.state is not TelemetryState.UNKNOWN:
            raise ValueError("unconfigured BFD peer state must be unknown")
        return self


class RouteTelemetry(_TelemetryModel):
    prefix: str = Field(min_length=3, max_length=64)
    present: StrictBool
    metric: Counter | None = None
    egress_interface: Identifier | None = None

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, prefix: str) -> str:
        try:
            parsed = ipaddress.ip_network(prefix, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid canonical route prefix: {prefix}") from exc
        if str(parsed) != prefix:
            raise ValueError(f"invalid canonical route prefix: {prefix}")
        return prefix

    @model_validator(mode="after")
    def validate_presence(self) -> RouteTelemetry:
        if self.present and (self.metric is None or self.egress_interface is None):
            raise ValueError("present route requires metric and egress_interface")
        if not self.present and (self.metric is not None or self.egress_interface is not None):
            raise ValueError("absent route cannot define metric or egress_interface")
        return self


class RouterTelemetry(_TelemetryModel):
    name: Identifier
    collection_success: StrictBool
    state: TelemetryState
    collection_error: str | None = Field(default=None, max_length=256)
    interfaces: tuple[InterfaceTelemetry, ...] = Field(default=(), max_length=64)
    ospf_neighbors: tuple[OspfNeighborTelemetry, ...] = Field(default=(), max_length=128)
    bfd_peers: tuple[BfdPeerTelemetry, ...] = Field(default=(), max_length=128)
    routes: tuple[RouteTelemetry, ...] = Field(default=(), max_length=256)

    @field_validator("collection_error")
    @classmethod
    def validate_collection_error(cls, error: str | None) -> str | None:
        if error is not None and not _ERROR_PATTERN.fullmatch(error):
            raise ValueError("collection_error must be a bounded single-line value")
        return error

    @model_validator(mode="after")
    def validate_observation(self) -> RouterTelemetry:
        if self.collection_success:
            if self.collection_error is not None:
                raise ValueError("successful collection cannot include collection_error")
            if self.state is not TelemetryState.UP:
                raise ValueError("successful collection requires up state")
        else:
            if self.state is not TelemetryState.UNKNOWN or self.collection_error is None:
                raise ValueError("failed collection requires unknown state and collection_error")
            if self.interfaces or self.ospf_neighbors or self.bfd_peers or self.routes:
                raise ValueError("failed collection cannot include partial observations")

        interface_names = [interface.name for interface in self.interfaces]
        if len(interface_names) != len(set(interface_names)):
            raise ValueError("router interfaces must be unique")
        known_interfaces = set(interface_names)

        neighbor_keys = [
            (neighbor.neighbor, neighbor.interface) for neighbor in self.ospf_neighbors
        ]
        if len(neighbor_keys) != len(set(neighbor_keys)):
            raise ValueError("OSPF neighbors must be unique")
        peer_keys = [(peer.peer, peer.interface) for peer in self.bfd_peers]
        if len(peer_keys) != len(set(peer_keys)):
            raise ValueError("BFD peers must be unique")
        route_prefixes = [route.prefix for route in self.routes]
        if len(route_prefixes) != len(set(route_prefixes)):
            raise ValueError("router routes must be unique")

        references = [
            *(neighbor.interface for neighbor in self.ospf_neighbors),
            *(peer.interface for peer in self.bfd_peers),
            *(
                route.egress_interface
                for route in self.routes
                if route.egress_interface is not None
            ),
        ]
        unknown_interfaces = sorted(set(references) - known_interfaces)
        if unknown_interfaces:
            raise ValueError(
                "observation references unknown interface: " + ", ".join(unknown_interfaces)
            )
        return self


class TelemetrySnapshot(_TelemetryModel):
    snapshot_id: Identifier
    observed_at: datetime
    topology_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    routers: tuple[RouterTelemetry, ...]

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, observed_at: datetime) -> datetime:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("telemetry observed_at must be timezone-aware")
        return observed_at.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_baseline_routers(self) -> TelemetrySnapshot:
        router_names = [router.name for router in self.routers]
        if len(router_names) != len(set(router_names)):
            raise ValueError("telemetry snapshot routers must be unique")
        if set(router_names) != set(BASELINE_ROUTERS):
            raise ValueError("telemetry snapshot must contain exactly the six baseline routers")
        return self
