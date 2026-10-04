from pathlib import Path

import pytest

from telconet_sentinel.config import load_topology
from telconet_sentinel.failure_domain import (
    FailureDomainType,
    audit_failure_domains,
    load_failure_domains,
    render_failure_domain_metrics,
    topology_fingerprint,
    validate_failure_domain_design,
)
from telconet_sentinel.intent import load_design_catalog
from telconet_sentinel.models import ServiceImpact
from telconet_sentinel.topology import Topology

ROOT = Path(__file__).parents[2]


def test_loads_and_audits_candidate_failure_domains() -> None:
    topology = load_topology(ROOT / "lab" / "intent-dual-homed.yml")
    catalog = load_failure_domains(ROOT / "lab" / "failure-domains.yml")
    designs = load_design_catalog(ROOT / "lab" / "designs.yml")

    validate_failure_domain_design(catalog, designs, ROOT / "lab")
    audit = audit_failure_domains(topology, catalog)

    assert catalog.design == "service-dual-homed"
    assert catalog.topology_sha256 == topology_fingerprint(topology)
    assert audit.total_scenarios == 2
    assert audit.passes_all is False
    assert audit.count(ServiceImpact.OUTAGE) == 1
    assert audit.count(ServiceImpact.DEGRADED) == 1
    assert [result.domain_id for result in audit.scenarios] == [
        "primary-site-power",
        "service-entry",
    ]

    power = audit.scenarios[0]
    assert power.domain_type is FailureDomainType.POWER_DOMAIN
    assert power.excluded_nodes == ("agg1", "core1")
    assert power.excluded_links == ()
    assert power.service_impact is ServiceImpact.DEGRADED
    assert power.affected_nodes == ("access1",)
    path_costs = [
        (path.access_node, path.baseline_cost, path.post_fault_cost)
        for path in power.paths
    ]
    assert path_costs == [
        ("access1", 30, 140),
        ("access2", 50, 50),
    ]

    service_entry = audit.scenarios[1]
    assert service_entry.domain_type is FailureDomainType.SHARED_CONDUIT
    assert service_entry.service_impact is ServiceImpact.OUTAGE
    assert service_entry.affected_nodes == ("access1", "access2")
    assert all(path.post_fault_cost is None for path in service_entry.paths)

    metrics = render_failure_domain_metrics(audit)
    assert "telconet_srlg_design_pass 0" in metrics
    assert 'telconet_srlg_scenarios_total{impact="outage"} 1' in metrics
    assert (
        'telconet_srlg_impact{domain_id="service-entry",'
        'domain_type="shared_conduit",impact="outage"} 1'
        in metrics
    )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            """version: true
design: service-dual-homed
topology_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
failure_domains: {}
""",
            "version",
        ),
        (
            """version: 1
design: service-dual-homed
topology_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
failure_domains:
  one-link:
    type: shared_conduit
    links: [core1--service-host]
""",
            "at least two",
        ),
        (
            """version: 1
design: service-dual-homed
topology_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
failure_domains:
  duplicate:
    type: shared_conduit
    links: [core1--service-host, core1--service-host]
""",
            "unique",
        ),
        (
            """version: 1
design: service-dual-homed
topology_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
failure_domains:
  unknown-field:
    type: site
    nodes: [core1, core2]
    note: forbidden
""",
            "note",
        ),
    ],
)
def test_rejects_invalid_failure_domain_catalog(
    tmp_path: Path,
    document: str,
    message: str,
) -> None:
    path = tmp_path / "failure-domains.yml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_failure_domains(path)


def test_rejects_unknown_topology_components(tmp_path: Path) -> None:
    path = tmp_path / "failure-domains.yml"
    path.write_text(
        """version: 1
design: service-dual-homed
topology_sha256: 38f18b126ce6c1131a5dd96ad03628b8504d86ed8a3634e40cbdb9295fc9566f
failure_domains:
  unknown-link:
    type: shared_conduit
    links: [core1--service-host, missing]
""",
        encoding="utf-8",
    )
    topology = load_topology(ROOT / "lab" / "intent-dual-homed.yml")

    with pytest.raises(ValueError, match="unknown link"):
        audit_failure_domains(topology, load_failure_domains(path))


def test_rejects_catalog_bound_to_a_different_topology() -> None:
    topology = load_topology(ROOT / "lab" / "intent-dual-homed.yml")
    catalog = load_failure_domains(ROOT / "lab" / "failure-domains.yml")
    changed = Topology(topology.nodes, topology.links[:-1])

    with pytest.raises(ValueError, match="topology fingerprint"):
        audit_failure_domains(changed, catalog)


def test_rejects_failure_domain_design_mislabeled_against_catalog() -> None:
    catalog = load_failure_domains(ROOT / "lab" / "failure-domains.yml")
    designs = load_design_catalog(ROOT / "lab" / "designs.yml")
    mislabeled = catalog.model_copy(update={"design": "historical-baseline"})

    with pytest.raises(ValueError, match="candidate design"):
        validate_failure_domain_design(mislabeled, designs, ROOT / "lab")


def test_rejects_unbounded_or_duplicate_failure_domain_input(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.yml"
    duplicate.write_text(
        """version: 1
design: service-dual-homed
topology_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
failure_domains:
  repeated:
    type: site
    nodes: [agg1, core1]
  repeated:
    type: site
    nodes: [agg2, core2]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate mapping key"):
        load_failure_domains(duplicate)

    excessive_components = tmp_path / "excessive-components.yml"
    nodes = ", ".join(f"n{index}" for index in range(33))
    excessive_components.write_text(
        "\n".join(
            [
                "version: 1",
                "design: service-dual-homed",
                "topology_sha256: " + "a" * 64,
                "failure_domains:",
                "  too-large:",
                "    type: site",
                f"    nodes: [{nodes}]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="nodes"):
        load_failure_domains(excessive_components)

    excessive_domains = tmp_path / "excessive-domains.yml"
    domains = "\n".join(
        f"  domain-{index}:\n    type: site\n    nodes: [agg1, core1]"
        for index in range(65)
    )
    excessive_domains.write_text(
        "\n".join(
            [
                "version: 1",
                "design: service-dual-homed",
                "topology_sha256: " + "a" * 64,
                "failure_domains:",
                domains,
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failure_domains"):
        load_failure_domains(excessive_domains)
