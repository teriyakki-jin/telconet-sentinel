import pytest

from telconet_sentinel.models import IncidentStatus, RecoveryPlan
from telconet_sentinel.recovery import approve_recovery, create_recovery_plan
from telconet_sentinel.topology import Topology


def test_creates_allowlisted_restore_plan(redundant_topology: Topology) -> None:
    plan = create_recovery_plan(
        redundant_topology,
        incident_id="inc-001",
        action="restore_link",
        target="access1--agg1",
    )

    assert plan.status is IncidentStatus.AWAITING_APPROVAL
    assert plan.action == "restore_link"
    assert plan.target == "access1--agg1"


def test_rejects_arbitrary_recovery_action(redundant_topology: Topology) -> None:
    with pytest.raises(ValueError, match="action is not allowed"):
        create_recovery_plan(
            redundant_topology,
            incident_id="inc-001",
            action="run_shell",
            target="access1--agg1; rm -rf /",
        )


def test_rejects_unknown_recovery_target(redundant_topology: Topology) -> None:
    with pytest.raises(ValueError, match="unknown link"):
        create_recovery_plan(
            redundant_topology,
            incident_id="inc-001",
            action="restore_link",
            target="unknown-link",
        )


def test_approves_plan_once() -> None:
    plan = RecoveryPlan("inc-001", "restore_link", "access1--agg1")

    approved = approve_recovery(plan)

    assert approved.status is IncidentStatus.APPROVED
    with pytest.raises(ValueError, match="awaiting approval"):
        approve_recovery(plan)
