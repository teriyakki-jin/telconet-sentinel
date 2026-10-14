from __future__ import annotations

import json
from pathlib import Path

import pytest

from telconet_sentinel.runtime_conformance import snapshot_main


def _write_raw_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    ospf = tmp_path / "ospf.json"
    route = tmp_path / "route.json"
    bfd = tmp_path / "bfd.json"
    ospf.write_text('{"neighbors":{}}', encoding="utf-8")
    route.write_text('{"10.20.0.0/24":[]}', encoding="utf-8")
    bfd.write_text('{"peer":"10.0.1.1"}', encoding="utf-8")
    return ospf, route, bfd


def test_snapshot_cli_strictly_composes_raw_frr_json(tmp_path: Path) -> None:
    ospf, route, bfd = _write_raw_inputs(tmp_path)
    output = tmp_path / "runtime-snapshot.json"

    assert (
        snapshot_main(
            [
                "--router",
                "access1",
                "--ospf",
                str(ospf),
                "--route",
                str(route),
                "--bfd",
                str(bfd),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 1
    assert snapshot["router"] == "access1"
    assert snapshot["ospf"] == {"neighbors": {}}
    assert snapshot["route"] == {"10.20.0.0/24": []}
    assert snapshot["bfd"] == {"peer": "10.0.1.1"}
    assert snapshot["captured_at"].endswith("Z")


def test_snapshot_cli_rejects_duplicate_keys_before_composition(tmp_path: Path) -> None:
    ospf, route, bfd = _write_raw_inputs(tmp_path)
    ospf.write_text('{"neighbors":{},"neighbors":{}}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        snapshot_main(
            [
                "--router",
                "access1",
                "--ospf",
                str(ospf),
                "--route",
                str(route),
                "--bfd",
                str(bfd),
                "--output",
                str(tmp_path / "runtime-snapshot.json"),
            ]
        )


def test_snapshot_cli_does_not_overwrite_raw_input(tmp_path: Path) -> None:
    ospf, route, bfd = _write_raw_inputs(tmp_path)
    original = ospf.read_bytes()

    with pytest.raises(ValueError, match="output must not overwrite an input"):
        snapshot_main(
            [
                "--router",
                "access1",
                "--ospf",
                str(ospf),
                "--route",
                str(route),
                "--bfd",
                str(bfd),
                "--output",
                str(ospf),
            ]
        )
    assert ospf.read_bytes() == original
