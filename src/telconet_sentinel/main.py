from __future__ import annotations

import os
from pathlib import Path

from .api import create_app
from .config import load_topology
from .convergence import ConvergenceStore, SQLiteConvergenceStore
from .failure_domain import load_failure_domains, validate_failure_domain_design
from .intent import load_design_catalog
from .metrics import load_experiment_evidence, load_repeated_experiment_evidence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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

experiment_evidence = (
    load_experiment_evidence(EXPERIMENT_PATH) if EXPERIMENT_PATH.is_file() else None
)
repeated_experiment_evidence = (
    load_repeated_experiment_evidence(REPEATED_EXPERIMENT_PATH)
    if REPEATED_EXPERIMENT_PATH.is_file()
    else None
)
state_database = os.environ.get("TELCONET_STATE_DB")
convergence_store = (
    SQLiteConvergenceStore(Path(state_database))
    if state_database
    else ConvergenceStore()
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
)
