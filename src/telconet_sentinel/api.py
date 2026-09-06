from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, IPvAnyAddress

from .convergence import (
    ConvergenceEvent,
    ConvergenceEventKind,
    ConvergenceRun,
    ConvergenceStore,
    render_live_metrics,
)
from .metrics import (
    render_experiment_metrics,
    validate_experiment_evidence,
    validate_repeated_experiment_evidence,
)
from .models import Incident, NetworkEvent
from .service import IncidentService
from .topology import Topology


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=32, pattern=r"^[a-z_]+$")
    link_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9-]+$")
    observed_at: AwareDatetime | None = None


class RecoveryActionResponse(BaseModel):
    action: str
    target: str


class IncidentResponse(BaseModel):
    id: str
    failed_component: str
    evidence: list[str]
    affected_nodes: list[str]
    affected_prefixes: list[str]
    service_impact: str
    recommended_action: RecoveryActionResponse
    status: str
    created_at: datetime


class ConvergenceRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["ospf_only", "bfd_100x3"]
    source: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    target: IPvAnyAddress
    started_at: AwareDatetime | None = None


class ConvergenceEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: ConvergenceEventKind
    offset_ms: int = Field(ge=0, le=60_000)
    observed_at: AwareDatetime | None = None
    route_metric: int | None = Field(default=None, ge=1, le=65_535)
    icmp_sequence: int | None = Field(default=None, ge=0)


class ConvergenceEventResponse(BaseModel):
    event: ConvergenceEventKind
    offset_ms: int
    observed_at: datetime
    route_metric: int | None
    icmp_sequence: int | None


class ConvergenceRunResponse(BaseModel):
    id: str
    profile: str
    source: str
    target: str
    started_at: datetime
    status: str
    events: list[ConvergenceEventResponse]


def _incident_response(incident: Incident) -> IncidentResponse:
    return IncidentResponse(
        id=incident.id,
        failed_component=incident.analysis.failed_component,
        evidence=list(incident.analysis.evidence),
        affected_nodes=list(incident.analysis.affected_nodes),
        affected_prefixes=list(incident.analysis.affected_prefixes),
        service_impact=incident.analysis.service_impact.value,
        recommended_action=RecoveryActionResponse(
            action=incident.recovery_plan.action,
            target=incident.recovery_plan.target,
        ),
        status=incident.recovery_plan.status.value,
        created_at=incident.created_at,
    )


def _convergence_response(run: ConvergenceRun) -> ConvergenceRunResponse:
    return ConvergenceRunResponse(
        id=run.id,
        profile=run.profile,
        source=run.source,
        target=run.target,
        started_at=run.started_at,
        status=run.status,
        events=[
            ConvergenceEventResponse(
                event=event.kind,
                offset_ms=event.offset_ms,
                observed_at=event.observed_at,
                route_metric=event.route_metric,
                icmp_sequence=event.icmp_sequence,
            )
            for event in run.events
        ],
    )


def create_app(
    topology: Topology,
    experiment_evidence: dict[str, Any] | None = None,
    repeated_experiment_evidence: dict[str, Any] | None = None,
    convergence_store: ConvergenceStore | None = None,
) -> FastAPI:
    if experiment_evidence is not None:
        validate_experiment_evidence(experiment_evidence)
    if repeated_experiment_evidence is not None:
        validate_repeated_experiment_evidence(repeated_experiment_evidence)
    app = FastAPI(
        title="TelcoNet Sentinel",
        version="0.1.0",
        description="Topology-aware incident analysis for a simulated IP transport network.",
    )
    service = IncidentService(topology)
    live_store = convergence_store or ConvergenceStore()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> PlainTextResponse:
        if experiment_evidence is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="experiment evidence is unavailable",
            )
        rendered = render_experiment_metrics(
            experiment_evidence, repeated_experiment_evidence
        )
        rendered += render_live_metrics(live_store.latest())
        return PlainTextResponse(rendered, media_type="text/plain; version=0.0.4")

    @app.get("/api/topology")
    def get_topology() -> dict[str, list[dict[str, Any]]]:
        return {
            "nodes": [
                {
                    "name": node.name,
                    "role": node.role.value,
                    "prefixes": list(node.prefixes),
                }
                for node in topology.nodes
            ],
            "links": [
                {
                    "id": link.id,
                    "endpoint_a": link.endpoint_a,
                    "endpoint_b": link.endpoint_b,
                    "cost": link.cost,
                }
                for link in topology.links
            ],
        }

    @app.post(
        "/api/convergence-runs",
        response_model=ConvergenceRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_convergence_run(request: ConvergenceRunRequest) -> ConvergenceRunResponse:
        run = live_store.create_run(
            profile=request.profile,
            source=request.source,
            target=str(request.target),
            started_at=request.started_at,
        )
        return _convergence_response(run)

    @app.get(
        "/api/convergence-runs/latest",
        response_model=ConvergenceRunResponse,
    )
    def get_latest_convergence_run() -> ConvergenceRunResponse:
        run = live_store.latest()
        if run is None:
            raise HTTPException(status_code=404, detail="convergence run not found")
        return _convergence_response(run)

    @app.get(
        "/api/convergence-runs/{run_id}",
        response_model=ConvergenceRunResponse,
    )
    def get_convergence_run(run_id: str) -> ConvergenceRunResponse:
        try:
            return _convergence_response(live_store.get(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/convergence-runs/{run_id}/events",
        response_model=ConvergenceRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def record_convergence_event(
        run_id: str,
        request: ConvergenceEventRequest,
    ) -> ConvergenceRunResponse:
        observed_at = request.observed_at or datetime.now(timezone.utc)
        try:
            run = live_store.append_event(
                run_id,
                ConvergenceEvent(
                    kind=request.event,
                    offset_ms=request.offset_ms,
                    observed_at=observed_at,
                    route_metric=request.route_metric,
                    icmp_sequence=request.icmp_sequence,
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _convergence_response(run)

    @app.post(
        "/api/events",
        response_model=IncidentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def ingest_event(request: EventRequest) -> IncidentResponse:
        kwargs = {}
        if request.observed_at is not None:
            kwargs["observed_at"] = request.observed_at
        try:
            incident = service.ingest(
                NetworkEvent(
                    link_id=request.link_id,
                    event_type=request.event_type,
                    **kwargs,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _incident_response(incident)

    @app.get("/api/incidents/{incident_id}", response_model=IncidentResponse)
    def get_incident(incident_id: str) -> IncidentResponse:
        try:
            return _incident_response(service.get(incident_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/approve", response_model=IncidentResponse)
    def approve_incident(incident_id: str) -> IncidentResponse:
        try:
            return _incident_response(service.approve(incident_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
