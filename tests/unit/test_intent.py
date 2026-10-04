from pathlib import Path

import pytest

from telconet_sentinel.intent import DesignRole, load_design_catalog, load_intent

ROOT = Path(__file__).parents[2]


def test_loads_versioned_baseline_and_candidate_design_metadata() -> None:
    baseline = load_intent(ROOT / "lab" / "intent.yml")
    candidate = load_intent(ROOT / "lab" / "intent-dual-homed.yml")
    catalog = load_design_catalog(ROOT / "lab" / "designs.yml")

    assert baseline.version == 1
    assert len(baseline.to_topology().links) == 10
    assert len(candidate.to_topology().links) == 11
    assert catalog.designs["historical-baseline"].role is DesignRole.BASELINE
    assert catalog.designs["service-dual-homed"].role is DesignRole.CANDIDATE
    assert (
        catalog.designs["service-dual-homed"].based_on
        == "historical-baseline"
    )
    assert set(catalog.load_topologies(ROOT / "lab")) == {
        "historical-baseline",
        "service-dual-homed",
    }


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            """version: 1
designs:
  baseline:
    intent: ../intent.yml
    role: baseline
""",
            "intent",
        ),
        (
            """version: 1
designs:
  baseline:
    intent: intent.yml
    role: baseline
  candidate:
    intent: candidate.yml
    role: candidate
    based_on: missing
""",
            "unknown baseline",
        ),
    ],
)
def test_rejects_unsafe_or_dangling_design_catalog(
    tmp_path: Path,
    document: str,
    message: str,
) -> None:
    path = tmp_path / "designs.yml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_design_catalog(path)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            """version: 2
nodes: {}
links: []
""",
            "version",
        ),
        (
            """version: 1
unexpected: true
nodes: {}
links: []
""",
            "unexpected",
        ),
        (
            """version: 1
nodes: {}
links: []
""",
            "nodes",
        ),
        (
            """version: 1
nodes:
  access:
    role: access
    typo: true
  service:
    role: service
links:
  - id: access--service
    endpoints: [access, service]
    cost: 1
""",
            "typo",
        ),
        (
            """version: 1
nodes:
  access:
    role: access
  service:
    role: service
links:
  - id: access--service
    endpoints: [access, service]
    cost: true
""",
            "cost",
        ),
    ],
)
def test_rejects_unsupported_or_non_strict_intent(
    tmp_path: Path,
    document: str,
    message: str,
) -> None:
    path = tmp_path / "intent.yml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_intent(path)


@pytest.mark.parametrize("version", ["true", "1.0"])
def test_rejects_coerced_schema_versions(
    tmp_path: Path,
    version: str,
) -> None:
    intent_path = tmp_path / "intent.yml"
    intent_path.write_text(
        f"""version: {version}
nodes:
  access:
    role: access
  service:
    role: service
links:
  - id: access--service
    endpoints: [access, service]
""",
        encoding="utf-8",
    )
    catalog_path = tmp_path / "designs.yml"
    catalog_path.write_text(
        f"""version: {version}
designs:
  baseline:
    intent: intent.yml
    role: baseline
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="version"):
        load_intent(intent_path)
    with pytest.raises(ValueError, match="version"):
        load_design_catalog(catalog_path)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            """version: 1
nodes:
  access:
    role: access
    prefixes: [10.0.0.0/24]
  service:
    role: service
    prefixes: [10.0.0.0/24]
links:
  - id: access--service
    endpoints: [access, service]
""",
            "duplicate prefix",
        ),
        (
            """version: 1
nodes:
  access:
    role: access
  service:
    role: service
links:
  - id: duplicate
    endpoints: [access, service]
  - id: duplicate
    endpoints: [service, access]
""",
            "duplicate link id",
        ),
        (
            """version: 1
nodes:
  access:
    role: access
  service:
    role: service
links:
  - id: access--missing
    endpoints: [access, missing]
""",
            "unknown endpoint",
        ),
        (
            """version: 1
nodes:
  access:
    role: access
  service:
    role: service
links:
  - id: access--access
    endpoints: [access, access]
""",
            "self link",
        ),
    ],
)
def test_rejects_semantically_invalid_intent(
    tmp_path: Path,
    document: str,
    message: str,
) -> None:
    path = tmp_path / "intent.yml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_intent(path)
