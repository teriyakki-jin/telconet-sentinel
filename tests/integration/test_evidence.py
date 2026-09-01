import json
from pathlib import Path

from telconet_sentinel.bfd_comparison import build_comparison
from telconet_sentinel.configuration import configuration_fingerprint
from telconet_sentinel.evidence import build_simulated_evidence, write_evidence
from telconet_sentinel.measurement import parse_measurement_log
from telconet_sentinel.models import NetworkEvent
from telconet_sentinel.repeated_trials import build_repeated_evidence
from telconet_sentinel.topology import Topology

ROOT = Path(__file__).parents[2]


def test_simulated_evidence_is_explicitly_not_a_lab_measurement(
    redundant_topology: Topology,
) -> None:
    evidence = build_simulated_evidence(
        redundant_topology,
        NetworkEvent("access1--agg1"),
    )

    assert evidence["scenario"] == "access_uplink_failure"
    assert evidence["source"] == "scenario_injected_event"
    assert evidence["measured"] is False
    assert evidence["failed_link"] == "access1--agg1"
    assert evidence["analysis"]["service_impact"] == "degraded"
    assert evidence["analysis"]["affected_prefixes"] == ["10.10.1.0/24"]
    assert evidence["recovery"]["status"] == "awaiting_approval"
    assert "convergence_ms" not in evidence
    assert "packet_loss_percent" not in evidence


def test_writes_deterministic_json(tmp_path: Path, redundant_topology: Topology) -> None:
    path = tmp_path / "link-failure.json"
    evidence = build_simulated_evidence(
        redundant_topology,
        NetworkEvent("access1--agg1"),
    )

    write_evidence(path, evidence)

    assert json.loads(path.read_text(encoding="utf-8")) == evidence
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_measured_lab_evidence_records_observed_values_and_limits() -> None:
    json_path = ROOT / "evidence" / "measured-link-failure.json"
    log_path = ROOT / "evidence" / "measured-link-failure.log"
    evidence = json.loads(json_path.read_text(encoding="utf-8"))
    recalculated = parse_measurement_log(log_path.read_text(encoding="utf-8"))

    assert evidence == recalculated

    assert evidence["environment"]["configuration_sha256"] == configuration_fingerprint(
        ROOT
    )


def test_bfd_comparison_is_recalculated_from_checked_in_raw_logs() -> None:
    json_path = ROOT / "evidence" / "bfd-comparison.json"
    ospf_log = (ROOT / "evidence" / "remote-blackhole-ospf.log").read_text(
        encoding="utf-8"
    )
    bfd_log = (ROOT / "evidence" / "remote-blackhole-bfd.log").read_text(
        encoding="utf-8"
    )

    evidence = json.loads(json_path.read_text(encoding="utf-8"))

    assert evidence == build_comparison(ospf_log, bfd_log)
    assert evidence["profiles"]["bfd_100x3"]["bfd_peer_up"] is True
    assert evidence["improvement"]["detection_time_reduction_percent"] > 80
    current_fingerprint = configuration_fingerprint(ROOT)
    assert {
        profile["configuration_sha256"] for profile in evidence["profiles"].values()
    } == {current_fingerprint}


def test_repeated_evidence_is_recalculated_from_forty_raw_logs() -> None:
    def logs(profile: str) -> list[tuple[str, str]]:
        directory = ROOT / "evidence" / "repeated" / profile
        return [
            (
                f"{profile}/{path.name}",
                path.read_text(encoding="utf-8"),
            )
            for path in sorted(directory.glob("trial-*.log"))
        ]

    evidence = json.loads(
        (ROOT / "evidence" / "bfd-repeated-trials.json").read_text(
            encoding="utf-8"
        )
    )

    assert evidence == build_repeated_evidence(logs("ospf_only"), logs("bfd_100x3"))
    assert evidence["trial_count_per_profile"] == 20
    assert evidence["environment"]["configuration_sha256"] == (
        configuration_fingerprint(ROOT)
    )
