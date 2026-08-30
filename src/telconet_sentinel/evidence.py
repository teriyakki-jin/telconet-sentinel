from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .impact import analyze_link_down
from .models import NetworkEvent
from .topology import Topology


def build_simulated_evidence(topology: Topology, event: NetworkEvent) -> dict[str, Any]:
    analysis = analyze_link_down(topology, event)
    return {
        "scenario": "access_uplink_failure",
        "source": "scenario_injected_event",
        "measured": False,
        "failed_link": event.link_id,
        "observed_at": event.observed_at.isoformat(),
        "analysis": {
            "failed_component": analysis.failed_component,
            "evidence": list(analysis.evidence),
            "affected_nodes": list(analysis.affected_nodes),
            "affected_prefixes": list(analysis.affected_prefixes),
            "service_impact": analysis.service_impact.value,
        },
        "recovery": {
            "action": "restore_link",
            "target": analysis.failed_component,
            "status": "awaiting_approval",
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
