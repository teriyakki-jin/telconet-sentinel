import json
from pathlib import Path

from telconet_sentinel.demo import main

ROOT = Path(__file__).parents[2]


def test_demo_cli_writes_explainable_evidence(tmp_path: Path) -> None:
    output = tmp_path / "demo.json"

    exit_code = main(
        [
            "--intent",
            str(ROOT / "lab" / "intent.yml"),
            "--link",
            "access1--agg1",
            "--output",
            str(output),
        ]
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert document["analysis"]["failed_component"] == "access1--agg1"
    assert document["source"] == "scenario_injected_event"
