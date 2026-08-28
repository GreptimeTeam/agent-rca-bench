from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from semantic_rca_bench.aegis_transfer_scorer import (
    AegisTransferScorerFixture,
    evaluate_aegis_transfer_run,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.agent import run_agent
from semantic_rca_bench.contracts import AgentRun, AgentRunner, Visibility
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
from semantic_rca_bench.greptimedb.visibility import QueryGateway
from semantic_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
)
from semantic_rca_bench.protocol import benchmark_protocol, run_orders

TransferAgent = Callable[..., AgentRun]
ReportUpdate = Callable[[dict[str, object]], None]


class TransferRunError(RuntimeError):
    pass


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
            equality = exact_edge_equality_audit(client, case)
            mechanism = mechanism_evidence_audit(client, case)
            graph_window = equality["window_contract"]["graph_observed_window"]
            graph_case_input = case.input.model_copy(
                update={"time_start": graph_window[0], "time_end": graph_window[1]}
            )
            surfaces = inspect_semantic_surfaces(client, graph_case_input)
            coverage = surfaces["coverage"]
            if not isinstance(coverage, dict):
                raise ValueError("Aegis transfer semantic coverage is malformed")
            gates = no_model_gates(
                case,
                archive,
                source,
                stored,
                equality,
                mechanism,
                isolated=True,
                frozen_selection=cohort_audit["frozen_selection_gate"]["pass"] is True,
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
            "selection": cohort_audit["selection"],
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
            "declared_edge": list(case.ground_truth.declared_edge),
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


def build_transfer_run_report(
    case: AegisTransferCase,
    fixture: AegisTransferScorerFixture,
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
    semantic_coverage: dict[str, object],
) -> dict[str, object]:
    graph_window = validate_transfer_run_preflight(
        case,
        fixture,
        source_audit,
        scorer_audit,
        semantic_coverage,
    )
    contract = fixture.canonical_api_runner
    orders = run_orders(
        list(contract.visibility_levels),
        contract.repetitions,
        contract.treatment_order_seed,
    )
    return {
        "report_schema_version": 1,
        "mode": "aegis-transfer-canonical-api-run",
        "publication_status": (
            "local raw run artifact; contains provider responses and is not a release artifact"
        ),
        "case_role": fixture.case_role,
        "case": {
            "case_id": case.input.case_token,
            "database": case.input.database,
            "time_start": case.input.time_start,
            "time_end": case.input.time_end,
            "alert_time": case.input.alert_time,
            "alert_text": case.input.alert_text,
            "fault_taxonomy": case.input.fault_taxonomy,
        },
        "protocol": benchmark_protocol(),
        "scorer_revision": fixture.scorer_revision,
        "scorer_fixture_sha256": _canonical_json_sha256(fixture.model_dump(mode="json")),
        "source_transfer_audit_sha256": source_transfer_audit_sha256(source_audit),
        "scorer_audit_sha256": _canonical_json_sha256(scorer_audit),
        "no_model_gates": {
            "source": source_audit["no_model_gates"],
            "scorer": scorer_audit["no_model_gates"],
        },
        "canonical_api_runner": contract.model_dump(mode="json"),
        "graph_window_contract": {
            "source_window": [case.input.time_start, case.input.time_end],
            "semantic_graph_window": list(graph_window),
            "raw_sql_window": [case.input.time_start, case.input.time_end],
            "reason": (
                "Semantic Graph observed_at is minute-binned; the source audit proves the minimal "
                "whole-minute envelope adds no source spans"
            ),
        },
        "semantic_coverage": semantic_coverage,
        "orders": [
            {
                "repetition": repetition,
                "levels": [level.value for level in order],
            }
            for repetition, order in enumerate(orders)
        ],
        "runs": [],
        "execution": {
            "expected_runs": contract.repetitions * len(contract.visibility_levels),
            "completed_runs": 0,
            "runner_errors": 0,
            "budget_exhaustions": 0,
            "complete": False,
        },
    }


def execute_transfer_runs(
    client: GreptimeClient,
    case: AegisTransferCase,
    fixture: AegisTransferScorerFixture,
    report: dict[str, object],
    *,
    run_agent_fn: TransferAgent = run_agent,
    on_update: ReportUpdate | None = None,
) -> dict[str, object]:
    contract = fixture.canonical_api_runner
    if contract.runner is not AgentRunner.API:
        raise ValueError("Aegis transfer canonical runner must use the API runner")
    if client.database != case.input.database:
        raise ValueError("Aegis transfer client database does not match the opaque case input")
    runs = report.get("runs")
    if not isinstance(runs, list) or runs:
        raise ValueError("Aegis transfer report must start with an empty runs list")
    coverage = report.get("semantic_coverage")
    if not isinstance(coverage, dict):
        raise ValueError("Aegis transfer report is missing semantic coverage")
    graph_window = _graph_window_from_report(report)
    orders = run_orders(
        list(contract.visibility_levels),
        contract.repetitions,
        contract.treatment_order_seed,
    )
    for repetition, order in enumerate(orders):
        for position, level in enumerate(order):
            gateway = QueryGateway(
                client,
                level,
                semantic_graph_window=(
                    graph_window if level is Visibility.SEMANTIC_GRAPH else None
                ),
            )
            with client.measure_query_load() as database_load:
                agent_run = run_agent_fn(
                    gateway,
                    case.input,
                    level,
                    model=contract.model,
                    max_tool_calls=contract.max_tool_calls,
                    max_turns=contract.max_turns,
                    max_tokens=contract.max_tokens,
                    semantic_coverage=coverage,
                )
            evaluation = evaluate_aegis_transfer_run(agent_run, fixture)
            runs.append(
                {
                    "repetition": repetition,
                    "position": position,
                    "run": agent_run.model_dump(mode="json"),
                    "evaluation": evaluation.model_dump(mode="json"),
                    "database_load": database_load.model_dump(mode="json"),
                }
            )
            _update_execution(report)
            if on_update is not None:
                on_update(report)
            if not evaluation.runner_contract_match or not evaluation.tool_budget_contract_match:
                raise TransferRunError("Aegis transfer runner violated its frozen contract")
            if agent_run.error is not None:
                raise TransferRunError(f"Aegis transfer runner failed: {agent_run.error}")
            if agent_run.tool_budget_exhausted:
                raise TransferRunError("Aegis transfer tool budget was exhausted")
    return report


def validate_transfer_run_preflight(
    case: AegisTransferCase,
    fixture: AegisTransferScorerFixture,
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
    semantic_coverage: dict[str, object],
) -> tuple[int, int]:
    source_gates = source_audit.get("no_model_gates")
    scorer_gates = scorer_audit.get("no_model_gates")
    if not isinstance(source_gates, dict) or source_gates.get("all_passed") is not True:
        raise ValueError("Aegis transfer source no-model gates did not pass")
    if not isinstance(scorer_gates, dict) or scorer_gates.get("all_passed") is not True:
        raise ValueError("Aegis transfer scorer no-model gates did not pass")
    if scorer_audit.get("source_transfer_audit_sha256") != source_transfer_audit_sha256(
        source_audit
    ):
        raise ValueError("Aegis transfer scorer audit is not bound to this source audit")
    if scorer_audit.get("fixture_sha256") != _canonical_json_sha256(
        fixture.model_dump(mode="json")
    ):
        raise ValueError("Aegis transfer scorer audit is not bound to this scorer fixture")
    if case.input.case_token != fixture.agent_case_id or case.input.fault_taxonomy:
        raise ValueError("Aegis transfer agent input is not opaque")
    if (case.input.time_start, case.input.alert_time) != fixture.normal_window:
        raise ValueError("Aegis transfer normal window drifted from the scorer fixture")
    if (case.input.alert_time, case.input.time_end) != fixture.abnormal_window:
        raise ValueError("Aegis transfer abnormal window drifted from the scorer fixture")
    graph = semantic_coverage.get("graph")
    if not isinstance(graph, dict) or graph.get("status") != "relational":
        raise ValueError("Aegis transfer Semantic Graph coverage is not relational")
    equality = source_audit.get("edge_equality")
    if not isinstance(equality, dict) or equality.get("exact_edge_set_equality") is not True:
        raise ValueError("Aegis transfer exact raw/Graph equality gate did not pass")
    window_contract = equality.get("window_contract")
    graph_window = (
        window_contract.get("graph_observed_window") if isinstance(window_contract, dict) else None
    )
    if (
        not isinstance(graph_window, list)
        or len(graph_window) != 2
        or not all(isinstance(value, int) for value in graph_window)
        or graph_window[0] >= graph_window[1]
    ):
        raise ValueError("Aegis transfer source audit has no valid Graph window")
    if window_contract.get("source_window") != [case.input.time_start, case.input.time_end]:
        raise ValueError("Aegis transfer Graph window is not bound to the source window")
    return int(graph_window[0]), int(graph_window[1])


def _graph_window_from_report(report: dict[str, object]) -> tuple[int, int]:
    contract = report.get("graph_window_contract")
    window = contract.get("semantic_graph_window") if isinstance(contract, dict) else None
    if not isinstance(window, list) or len(window) != 2:
        raise ValueError("Aegis transfer run report has no Semantic Graph window")
    return int(window[0]), int(window[1])


def _update_execution(report: dict[str, object]) -> None:
    runs = report["runs"]
    contract = report["canonical_api_runner"]
    if not isinstance(runs, list) or not isinstance(contract, dict):
        raise ValueError("Aegis transfer run report is malformed")
    expected = int(contract["repetitions"]) * len(contract["visibility_levels"])
    runner_errors = sum(bool(item["run"].get("error")) for item in runs)
    budget_exhaustions = sum(bool(item["run"].get("tool_budget_exhausted")) for item in runs)
    report["execution"] = {
        "expected_runs": expected,
        "completed_runs": len(runs),
        "runner_errors": runner_errors,
        "budget_exhaustions": budget_exhaustions,
        "complete": len(runs) == expected,
    }


def _canonical_json_sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
