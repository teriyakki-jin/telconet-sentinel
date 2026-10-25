from __future__ import annotations

import os
from pathlib import Path

from .api import create_app
from .config import load_topology
from .configuration import candidate_runtime_configuration_fingerprint
from .convergence import ConvergenceStore, SQLiteConvergenceStore
from .failure_domain import load_failure_domains, validate_failure_domain_design
from .intent import load_design_catalog
from .metrics import load_experiment_evidence, load_repeated_experiment_evidence
from .runtime_resilience import load_runtime_n1_published_evidence
from .telemetry_config import load_telemetry_binding
from .telemetry_store import SQLiteTelemetryStore, TelemetryStore

PROJECT_ROOT = Path(
    os.environ.get("TELCONET_PROJECT_ROOT", Path(__file__).resolve().parents[2])
)
INTENT_PATH = Path(os.environ.get("TELCONET_INTENT", PROJECT_ROOT / "lab" / "intent.yml"))
CANDIDATE_INTENT_PATH = Path(
    os.environ.get(
        "TELCONET_CANDIDATE_INTENT", PROJECT_ROOT / "lab" / "intent-dual-homed.yml"
    )
)
FAILURE_DOMAINS_PATH = Path(
    os.environ.get(
        "TELCONET_FAILURE_DOMAINS",
        PROJECT_ROOT / "lab" / "failure-domains.yml",
    )
)
DESIGN_CATALOG_PATH = Path(
    os.environ.get(
        "TELCONET_DESIGN_CATALOG",
        PROJECT_ROOT / "lab" / "designs.yml",
    )
)
EXPERIMENT_PATH = Path(
    os.environ.get(
        "TELCONET_EXPERIMENT",
        PROJECT_ROOT / "evidence" / "bfd-comparison.json",
    )
)
REPEATED_EXPERIMENT_PATH = Path(
    os.environ.get(
        "TELCONET_REPEATED_EXPERIMENT",
        PROJECT_ROOT / "evidence" / "bfd-repeated-trials.json",
    )
)
RUNTIME_N1_EVIDENCE_PATH = Path(
    os.environ.get(
        "TELCONET_RUNTIME_N1_EVIDENCE",
        PROJECT_ROOT / "evidence" / "runtime-n1-dashboard.json",
    )
)
TELEMETRY_TARGETS_PATH = Path(
    os.environ.get(
        "TELCONET_TELEMETRY_TARGETS",
        PROJECT_ROOT / "lab" / "telemetry-targets.yml",
    )
)
TELEMETRY_CLAB_PATH = Path(
    os.environ.get(
        "TELCONET_TELEMETRY_CLAB",
        PROJECT_ROOT / "lab" / "telconet.clab.yml",
    )
)

experiment_evidence = (
    load_experiment_evidence(EXPERIMENT_PATH) if EXPERIMENT_PATH.is_file() else None
)
repeated_experiment_evidence = (
    load_repeated_experiment_evidence(REPEATED_EXPERIMENT_PATH)
    if REPEATED_EXPERIMENT_PATH.is_file()
    else None
)
runtime_n1_evidence = load_runtime_n1_published_evidence(RUNTIME_N1_EVIDENCE_PATH)
runtime_n1_configuration_sha256 = candidate_runtime_configuration_fingerprint(
    PROJECT_ROOT
)
state_database = os.environ.get("TELCONET_STATE_DB")
convergence_store = (
    SQLiteConvergenceStore(Path(state_database))
    if state_database
    else ConvergenceStore()
)
telemetry_binding = load_telemetry_binding(
    TELEMETRY_TARGETS_PATH,
    INTENT_PATH,
    TELEMETRY_CLAB_PATH,
)
telemetry_database = os.environ.get("TELCONET_TELEMETRY_DB")
telemetry_store = (
    SQLiteTelemetryStore(Path(telemetry_database))
    if telemetry_database
    else TelemetryStore()
)
candidate_topology = (
    load_topology(CANDIDATE_INTENT_PATH)
    if CANDIDATE_INTENT_PATH.is_file()
    else None
)
failure_domains = (
    load_failure_domains(FAILURE_DOMAINS_PATH)
    if FAILURE_DOMAINS_PATH.is_file()
    else None
)
if failure_domains is not None:
    designs = load_design_catalog(DESIGN_CATALOG_PATH)
    validate_failure_domain_design(
        failure_domains,
        designs,
        DESIGN_CATALOG_PATH.parent,
    )
app = create_app(
    load_topology(INTENT_PATH),
    experiment_evidence,
    repeated_experiment_evidence,
    convergence_store,
    candidate_topology=candidate_topology,
    failure_domains=failure_domains,
    runtime_n1_evidence=runtime_n1_evidence,
    runtime_n1_configuration_sha256=runtime_n1_configuration_sha256,
    telemetry_binding=telemetry_binding,
    telemetry_store=telemetry_store,
)
