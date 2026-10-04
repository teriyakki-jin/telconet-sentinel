from pathlib import Path

from .intent import load_intent
from .topology import Topology


def load_topology(path: Path) -> Topology:
    return load_intent(path).to_topology()
