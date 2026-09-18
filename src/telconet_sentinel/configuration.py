from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path


def configuration_fingerprint(project_root: Path) -> str:
    frr_root = project_root / "lab" / "frr"
    files = [
        project_root / "lab" / "telconet.clab.yml",
        project_root / "lab" / "intent.yml",
    ]
    files.extend(
        sorted(
            [
                frr_root / "daemons",
                *(frr_root / router / "frr.conf" for router in (
                    "access1", "access2", "agg1", "agg2", "core1", "core2"
                )),
            ],
            key=lambda path: path.as_posix(),
        )
    )
    component_hashes = "".join(f"{sha256(path.read_bytes()).hexdigest()}\n" for path in files)
    return sha256(component_hashes.encode("ascii")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fingerprint lab configuration files")
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args(argv)
    print(configuration_fingerprint(args.project_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
