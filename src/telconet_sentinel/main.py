from __future__ import annotations

import os
from pathlib import Path

from .api import create_app
from .config import load_topology
from .metrics import load_experiment_evidence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTENT_PATH = Path(os.environ.get("TELCONET_INTENT", PROJECT_ROOT / "lab" / "intent.yml"))
EXPERIMENT_PATH = Path(
    os.environ.get(
        "TELCONET_EXPERIMENT",
        PROJECT_ROOT / "evidence" / "bfd-comparison.json",
    )
)

experiment_evidence = (
    load_experiment_evidence(EXPERIMENT_PATH) if EXPERIMENT_PATH.is_file() else None
)
app = create_app(load_topology(INTENT_PATH), experiment_evidence)
