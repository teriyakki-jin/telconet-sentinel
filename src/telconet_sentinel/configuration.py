from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path

_CANDIDATE_RUNTIME_FILES = (
    "lab/intent-dual-homed.yml",
    "lab/telconet-dual-homed.clab.yml",
    "lab/frr/daemons",
    "lab/frr/access1/frr.conf",
    "lab/frr/access2/frr.conf",
    "lab/frr/agg1/frr.conf",
    "lab/frr/agg2/frr.conf",
    "lab/frr/dual-homed/core1.conf",
    "lab/frr/dual-homed/core2.conf",
    "lab/frr/dual-homed/service-host.conf",
)


def candidate_runtime_configuration_paths(project_root: Path) -> tuple[Path, ...]:
    return tuple(project_root / relative_path for relative_path in _CANDIDATE_RUNTIME_FILES)


def candidate_runtime_configuration_fingerprint(project_root: Path) -> str:
    components: list[bytes] = []
    for relative_path, path in zip(
        _CANDIDATE_RUNTIME_FILES,
        candidate_runtime_configuration_paths(project_root),
        strict=True,
    ):
        content = path.read_bytes().replace(b"\r\n", b"\n")
        components.append(
            relative_path.encode("utf-8")
            + b"\0"
            + sha256(content).hexdigest().encode("ascii")
            + b"\n"
        )
    return sha256(b"".join(components)).hexdigest()


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
