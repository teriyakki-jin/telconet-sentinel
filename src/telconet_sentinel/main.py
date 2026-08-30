from __future__ import annotations

import os
from pathlib import Path

from .api import create_app
from .config import load_topology

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTENT_PATH = Path(os.environ.get("TELCONET_INTENT", PROJECT_ROOT / "lab" / "intent.yml"))

app = create_app(load_topology(INTENT_PATH))
