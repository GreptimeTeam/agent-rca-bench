from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from semantic_rca_bench.datasets.aegis import audit_cohort as audit_aegis_cohort
from semantic_rca_bench.datasets.aegis_transfer import (
    ADAPTER_REVISION,
    DATASET_REVISION,
    AegisTransferCase,
    archive_checksum_status,
    exact_edge_equality_audit,
    ingest_case,
    load_selected_case,
    mechanism_evidence_audit,
    no_model_gates,
    source_audit,
    validate_stored_rows,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.greptimedb.server import ManagedGreptime, inspect_checkout
from semantic_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
)


@dataclass(frozen=True)
class TransferEnvironmentConfig:
    cases_dir: Path
    meta_dir: Path
    archive: Path
    selection: Path
    greptimedb_repo: Path
    run_dir: Path
    database: str


@dataclass
class PreparedTransferEnvironment:
    client: GreptimeClient
    case: AegisTransferCase
    source_audit: dict[str, object]
    semantic_coverage: dict[str, object]


@contextmanager
def prepare_transfer_environment(
    config: TransferEnvironmentConfig,
) -> Iterator[PreparedTransferEnvironment]:
    if config.run_dir.exists():
        raise ValueError(f"exclusive GreptimeDB run directory already exists: {config.run_dir}")
    archive = archive_checksum_status(config.archive)
    cohort_audit = audit_aegis_cohort(
        config.cases_dir,
        config.meta_dir,
        selection_path=config.selection,
    )
    checkout = inspect_checkout(
        config.greptimedb_repo,
        expected_branch="feat/semantic-graph-declaration-visibility",
    )
    case = load_selected_case(
        config.cases_dir,
        config.meta_dir,
        config.selection,
        database=config.database,
    )
    _assert_neutral_database(case, config.database)
    source = source_audit(case)
    managed = ManagedGreptime(Path(str(checkout["binary"])), config.run_dir)
    report = None
    try:
        managed.start()
        with GreptimeClient(managed.endpoint, database=config.database, timeout=120) as client:
            server_status = client.status()
            client.create_database(config.database)
            assert_semantic_graph_isolated(client, config.database)
            empty_before_ingest = assert_semantic_graph_window_empty(client, case.input)
            counts = ingest_case(client, case, source)
            stored = validate_stored_rows(client, case, counts, source)
            equality = exact_edge_equality_audit(client, case, source)
            mechanism = mechanism_evidence_audit(client, case)
            graph_window = equality["window_contract"]["graph_observed_window"]
            graph_case_input = case.input.model_copy(
                update={"time_start": graph_window[0], "time_end": graph_window[1]}
            )
            surfaces = inspect_semantic_surfaces(client, graph_case_input)
            coverage = surfaces["coverage"]
            if not isinstance(coverage, dict):
                raise ValueError("Aegis transfer semantic coverage is malformed")
            surface_contract = coverage.get("surface_contract")
            gates = no_model_gates(
                case,
                archive,
                source,
                stored,
                equality,
                mechanism,
                isolated=True,
                frozen_selection=cohort_audit["frozen_selection_gate"]["pass"] is True,
                semantic_surface_contract=(
                    isinstance(surface_contract, dict) and surface_contract.get("current") is True
                ),
            )
            report = _source_audit_report(
                case=case,
                archive=archive,
                cohort_audit=cohort_audit,
                checkout=checkout,
                server_status=server_status,
                managed=managed,
                database=config.database,
                empty_before_ingest=empty_before_ingest,
                source=source,
                stored=stored,
                equality=equality,
                mechanism=mechanism,
                surfaces=surfaces,
                gates=gates,
            )
            yield PreparedTransferEnvironment(
                client=client,
                case=case,
                source_audit=report,
                semantic_coverage=coverage,
            )
    finally:
        managed.stop()
        if report is not None:
            report["exclusive_instance"]["process_stopped_by_command"] = (
                managed.process is not None and managed.process.poll() is not None
            )


def _source_audit_report(
    *,
    case: AegisTransferCase,
    archive: dict[str, object],
    cohort_audit: dict[str, object],
    checkout: dict[str, object],
    server_status: dict[str, object],
    managed: ManagedGreptime,
    database: str,
    empty_before_ingest: dict[str, int | str],
    source: dict[str, object],
    stored: dict[str, object],
    equality: dict[str, object],
    mechanism: dict[str, object],
    surfaces: dict[str, object],
    gates: dict[str, bool],
) -> dict[str, object]:
    sanitized_server_status = {
        key: server_status[key]
        for key in ("version", "branch", "commit", "rustc_version")
        if key in server_status
    }
    return {
        "audit_schema_version": 1,
        "mode": "aegis-transfer-no-model-audit",
        "dataset_revision": DATASET_REVISION,
        "adapter_revision": ADAPTER_REVISION,
        "license": {
            "benchmark_code": "Apache-2.0",
            "source_dataset_record": "CC-BY-4.0",
            "reviewer_artifact_data_license": ("not explicitly covered by root Apache-2.0"),
            "publication_mode": "pinned-downloader; telemetry not redistributed",
        },
        "pinned_source": archive,
        "selection_audit": {
            "base_ranking": cohort_audit["selection"],
            "active_frozen_selection": cohort_audit["frozen_selection_gate"]["observed"][
                "selected_case"
            ],
            "frozen_selection_gate": cohort_audit["frozen_selection_gate"],
        },
        "case": {
            "agent_facing": {
                "case_id": case.agent_case_id,
                "time_start": case.input.time_start,
                "time_end": case.input.time_end,
                "alert_time": case.input.alert_time,
                "fault_taxonomy": case.input.fault_taxonomy,
            },
            "source_mapping": {
                "agent_case_id": case.agent_case_id,
                "source_case": case.source_case,
            },
            "normal_window": list(case.normal_window),
            "abnormal_window": list(case.abnormal_window),
            "ground_truth_services": list(case.ground_truth.services),
            "declared_edge": (
                list(case.ground_truth.declared_edge)
                if case.ground_truth.declared_edge is not None
                else None
            ),
            "fault_type": case.ground_truth.fault_type,
        },
        "greptimedb": {
            "branch": checkout["branch"],
            "head": checkout["head"],
            "binary_version": checkout["binary_version"],
        },
        "exclusive_instance": {
            "loopback_only": True,
            "ports": {
                "http": managed.http_port,
                "grpc": managed.grpc_port,
                "mysql": managed.mysql_port,
                "postgres": managed.postgres_port,
            },
            "database": database,
            "empty_before_ingest": empty_before_ingest,
            "status": sanitized_server_status,
            "process_stopped_by_command": False,
        },
        "source_audit": source,
        "ingestion": stored,
        "edge_equality": equality,
        "mechanism_evidence": mechanism,
        "semantic_surfaces": surfaces,
        "no_model_gates": gates,
    }


def _assert_neutral_database(case: AegisTransferCase, database: str) -> None:
    database_text = database.lower()
    forbidden = {
        case.source_case.lower(),
        case.ground_truth.fault_type.lower(),
        *(service.lower() for service in case.ground_truth.services),
    }
    if any(value and value in database_text for value in forbidden):
        raise ValueError("Aegis transfer database name leaks source labels")
