from .models import IncidentStatus, RecoveryPlan
from .topology import Topology


def create_recovery_plan(
    topology: Topology,
    incident_id: str,
    action: str,
    target: str,
) -> RecoveryPlan:
    if action != "restore_link":
        raise ValueError("action is not allowed")
    topology.link(target)
    return RecoveryPlan(
        incident_id=incident_id,
        action=action,
        target=target,
    )


def approve_recovery(plan: RecoveryPlan) -> RecoveryPlan:
    if plan.status is not IncidentStatus.AWAITING_APPROVAL:
        raise ValueError("recovery plan is not awaiting approval")
    plan.status = IncidentStatus.APPROVED
    return plan
