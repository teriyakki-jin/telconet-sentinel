from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_topology
from .evidence import build_simulated_evidence, write_evidence
from .models import NetworkEvent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze an injected link failure and write reproducible JSON evidence."
    )
    parser.add_argument("--intent", type=Path, default=Path("lab/intent.yml"))
    parser.add_argument("--link", default="access1--agg1")
    parser.add_argument("--output", type=Path, default=Path("evidence/simulated-link-failure.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    topology = load_topology(args.intent)
    evidence = build_simulated_evidence(topology, NetworkEvent(args.link))
    write_evidence(args.output, evidence)
    print(f"wrote scenario evidence to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
