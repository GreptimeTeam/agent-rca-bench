from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from semantic_rca_bench.aegis_transfer_protocol import (
    AegisTransferProtocolFixture,
    audit_transfer_protocol,
    evaluate_transfer_protocol_run,
    load_transfer_protocol_fixture,
)
from semantic_rca_bench.aegis_transfer_scorer import (
    AegisTransferEvaluation,
    AegisTransferScorerFixture,
    load_transfer_scorer_fixture,
    sha256_file,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.agent import run_agent
from semantic_rca_bench.contracts import AgentRun, DatabaseLoad, Visibility
from semantic_rca_bench.datasets.aegis_transfer import AegisTransferCase
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.greptimedb.visibility import QueryGateway
from semantic_rca_bench.protocol import benchmark_protocol, require_current_protocol, run_orders
from semantic_rca_bench.report import MODEL_PRICING

FORMAL_REPORT_SCHEMA_VERSION = 2
FORMAL_REPORT_MODE = "aegis-transfer-three-model-api-run"

FormalAgent = Callable[..., AgentRun]
ReportUpdate = Callable[[dict[str, object]], None]


class FormalRunError(RuntimeError):
    pass


def build_formal_preflight_report(
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
    protocol_audit: dict[str, object],
    scorer_fixture: AegisTransferScorerFixture,
    scorer_path: Path,
    protocol_fixture: AegisTransferProtocolFixture,
    protocol_path: Path,
) -> dict[str, object]:
    recomputed = audit_transfer_protocol(
        protocol_fixture,
        protocol_path,
        scorer_fixture,
        scorer_path,
        source_audit,
        scorer_audit,
    )
    if protocol_audit != recomputed:
        raise ValueError("stored formal protocol audit does not match deterministic recomputation")
    gates = protocol_audit.get("no_model_gates")
    if not isinstance(gates, dict) or gates.get("all_passed") is not True:
        raise ValueError("formal protocol no-model gates did not pass")
    schedule = formal_schedule(protocol_fixture)
    case = _mapping(source_audit, "case")
    agent_facing = _mapping(case, "agent_facing")
    source_semantic_sha256 = formal_source_semantic_sha256(source_audit)
    pricing_snapshot = {
        model.model: dict(MODEL_PRICING[model.model]) for model in protocol_fixture.models
    }
    return {
        "report_schema_version": FORMAL_REPORT_SCHEMA_VERSION,
        "mode": FORMAL_REPORT_MODE,
        "publication_status": (
            "local raw run artifact; contains provider responses and is not a release artifact"
        ),
        "case_role": protocol_fixture.case_role,
        "case": agent_facing,
        "benchmark_protocol": benchmark_protocol(),
        "formal_protocol": _json_object_file(protocol_path),
        "bindings": {
            "source_semantic_sha256": source_semantic_sha256,
            "scorer_fixture_sha256": sha256_file(scorer_path),
            "protocol_fixture_sha256": sha256_file(protocol_path),
            "preflight_source_transfer_audit_sha256": source_transfer_audit_sha256(source_audit),
            "preflight_scorer_audit_sha256": _canonical_sha256(scorer_audit),
            "preflight_protocol_audit_sha256": _canonical_sha256(protocol_audit),
            "pricing_snapshot_sha256": _canonical_sha256(pricing_snapshot),
        },
        "pricing_snapshot": pricing_snapshot,
        "schedule": schedule,
        "runs": [],
        "execution": {
            "expected_runs": len(schedule),
            "completed_runs": 0,
            "runner_errors": 0,
            "budget_exhaustions": 0,
            "complete": False,
        },
        "authorization": {
            "paid_api_required": True,
            "reusable_confirmation_stored": False,
            "confirmation_scope": "per invocation; supplied out of band and never persisted",
            "preflight_calls_provider": False,
        },
        "execution_bindings": None,
    }


def formal_schedule(fixture: AegisTransferProtocolFixture) -> list[dict[str, object]]:
    orders = run_orders(
        list(fixture.visibility_levels),
        fixture.repetitions_per_model,
        fixture.treatment_order_seed,
    )
    schedule = []
    for model_index, model in enumerate(fixture.models):
        for repetition, order in enumerate(orders):
            for position, visibility in enumerate(order):
                schedule.append(
                    {
                        "cell_index": len(schedule),
                        "model_index": model_index,
                        "model": model.model,
                        "provider": model.provider,
                        "api_transport": model.api_transport,
                        "prompt_cache": model.prompt_cache,
                        "repetition": repetition,
                        "position": position,
                        "visibility": visibility.value,
                    }
                )
    return schedule


def bind_formal_execution(
    report: dict[str, object],
    case: AegisTransferCase,
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
    protocol_audit: dict[str, object],
    semantic_coverage: dict[str, object],
    scorer_fixture: AegisTransferScorerFixture,
    scorer_path: Path,
    protocol_fixture: AegisTransferProtocolFixture,
    protocol_path: Path,
) -> tuple[int, int]:
    validate_formal_report(
        report,
        scorer_fixture,
        scorer_path,
        protocol_fixture,
        protocol_path,
    )
    if formal_source_semantic_sha256(source_audit) != _binding(report, "source_semantic_sha256"):
        raise ValueError("live source audit differs from the frozen preflight semantics")
    if case.input.case_token != protocol_fixture.agent_case_id or case.input.fault_taxonomy:
        raise ValueError("live formal case input is not the frozen opaque measurement case")
    if [case.input.time_start, case.input.alert_time, case.input.time_end] != [
        scorer_fixture.normal_window[0],
        scorer_fixture.normal_window[1],
        scorer_fixture.abnormal_window[1],
    ]:
        raise ValueError("live formal case windows drifted from the frozen scorer")
    source_gates = source_audit.get("no_model_gates")
    scorer_gates = scorer_audit.get("no_model_gates")
    protocol_gates = protocol_audit.get("no_model_gates")
    if any(
        not isinstance(gates, dict) or gates.get("all_passed") is not True
        for gates in (source_gates, scorer_gates, protocol_gates)
    ):
        raise ValueError("live formal no-model gates did not pass")
    graph = semantic_coverage.get("graph")
    if not isinstance(graph, dict) or graph.get("status") != "relational":
        raise ValueError("live formal Semantic Graph coverage is not relational")
    equality = _mapping(source_audit, "edge_equality")
    window_contract = _mapping(equality, "window_contract")
    graph_window = window_contract.get("graph_observed_window")
    if (
        equality.get("exact_edge_set_equality") is not True
        or not isinstance(graph_window, list)
        or len(graph_window) != 2
        or not all(isinstance(value, int) for value in graph_window)
    ):
        raise ValueError("live formal raw/Graph equality or window contract is invalid")
    report["execution_bindings"] = {
        "source_transfer_audit_sha256": source_transfer_audit_sha256(source_audit),
        "source_semantic_sha256": formal_source_semantic_sha256(source_audit),
        "scorer_audit_sha256": _canonical_sha256(scorer_audit),
        "protocol_audit_sha256": _canonical_sha256(protocol_audit),
        "scorer_fixture_sha256": sha256_file(scorer_path),
        "protocol_fixture_sha256": sha256_file(protocol_path),
    }
    report["semantic_coverage"] = semantic_coverage
    report["graph_window_contract"] = window_contract
    return int(graph_window[0]), int(graph_window[1])


def execute_formal_runs(
    client: GreptimeClient,
    case: AegisTransferCase,
    scorer_fixture: AegisTransferScorerFixture,
    scorer_path: Path,
    protocol_fixture: AegisTransferProtocolFixture,
    protocol_path: Path,
    report: dict[str, object],
    *,
    paid_api_confirmed: bool,
    run_agent_fn: FormalAgent = run_agent,
    on_update: ReportUpdate | None = None,
) -> dict[str, object]:
    validate_formal_report(
        report,
        scorer_fixture,
        scorer_path,
        protocol_fixture,
        protocol_path,
    )
    if len(_list_of_mappings(report, "runs")) < len(_list_of_mappings(report, "schedule")):
        require_current_protocol(protocol_fixture.benchmark_protocol_version)
    if paid_api_confirmed is not True:
        raise ValueError("formal paid API execution has not been explicitly confirmed")
    if report.get("execution_bindings") is None:
        raise ValueError("formal report is not bound to live no-model audits")
    execution_bindings = _mapping(report, "execution_bindings")
    _validate_execution_binding(report, execution_bindings)
    if client.database != case.input.database:
        raise ValueError("formal client database does not match the opaque case input")
    coverage = _mapping(report, "semantic_coverage")
    graph_window = _graph_window(report)
    schedule = _list_of_mappings(report, "schedule")
    runs = _list_of_mappings(report, "runs")
    for cell in schedule[len(runs) :]:
        visibility = Visibility(str(cell["visibility"]))
        gateway = QueryGateway(
            client,
            visibility,
            semantic_graph_window=(
                graph_window if visibility is Visibility.SEMANTIC_GRAPH else None
            ),
        )
        with client.measure_query_load() as database_load:
            run = run_agent_fn(
                gateway,
                case.input,
                visibility,
                model=str(cell["model"]),
                max_tool_calls=protocol_fixture.max_tool_calls,
                max_turns=protocol_fixture.max_turns,
                max_tokens=protocol_fixture.max_tokens,
                semantic_coverage=coverage,
            )
        evaluation = evaluate_transfer_protocol_run(
            run,
            scorer_fixture,
            protocol_fixture,
            expected_model=str(cell["model"]),
        )
        result = {
            **cell,
            "run": run.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
            "database_load": database_load.model_dump(mode="json"),
            "execution_bindings": dict(execution_bindings),
        }
        cast_runs = report["runs"]
        if not isinstance(cast_runs, list):
            raise ValueError("formal report runs are malformed")
        cast_runs.append(result)
        _update_execution(report)
        if on_update is not None:
            on_update(report)
        if not evaluation.runner_contract_match or not evaluation.tool_budget_contract_match:
            raise FormalRunError("formal runner violated its frozen contract")
    return report


def validate_formal_report(
    report: dict[str, object],
    scorer_fixture: AegisTransferScorerFixture,
    scorer_path: Path,
    protocol_fixture: AegisTransferProtocolFixture,
    protocol_path: Path,
    *,
    require_complete: bool = False,
) -> None:
    if load_transfer_scorer_fixture(scorer_path) != scorer_fixture:
        raise ValueError("formal scorer object does not match its bound fixture file")
    if load_transfer_protocol_fixture(protocol_path) != protocol_fixture:
        raise ValueError("formal protocol object does not match its bound fixture file")
    if (
        report.get("report_schema_version") != FORMAL_REPORT_SCHEMA_VERSION
        or report.get("mode") != FORMAL_REPORT_MODE
        or report.get("case_role") != "measurement"
    ):
        raise ValueError("unsupported formal Aegis transfer report")
    if report.get("benchmark_protocol") != benchmark_protocol():
        raise ValueError("formal report benchmark protocol drifted")
    if report.get("formal_protocol") != _json_object_file(protocol_path):
        raise ValueError("formal report execution protocol drifted")
    if report.get("authorization") != {
        "paid_api_required": True,
        "reusable_confirmation_stored": False,
        "confirmation_scope": "per invocation; supplied out of band and never persisted",
        "preflight_calls_provider": False,
    }:
        raise ValueError("formal report authorization boundary drifted")
    case = _mapping(report, "case")
    if case.get("case_id") != protocol_fixture.agent_case_id or case.get("fault_taxonomy") != []:
        raise ValueError("formal report case is not the frozen opaque case")
    bindings = _mapping(report, "bindings")
    expected_fixture_bindings = {
        "scorer_fixture_sha256": sha256_file(scorer_path),
        "protocol_fixture_sha256": sha256_file(protocol_path),
    }
    if any(bindings.get(key) != value for key, value in expected_fixture_bindings.items()):
        raise ValueError("formal report fixture binding drifted")
    pricing_snapshot = report.get("pricing_snapshot")
    if not isinstance(pricing_snapshot, dict) or set(pricing_snapshot) != {
        model.model for model in protocol_fixture.models
    }:
        raise ValueError("formal report pricing snapshot is malformed")
    if bindings.get("pricing_snapshot_sha256") != _canonical_sha256(pricing_snapshot):
        raise ValueError("formal report pricing snapshot binding drifted")
    schedule = _list_of_mappings(report, "schedule")
    if schedule != formal_schedule(protocol_fixture):
        raise ValueError("formal report schedule drifted")
    runs = _list_of_mappings(report, "runs")
    if len(runs) > len(schedule):
        raise ValueError("formal report contains more runs than scheduled cells")
    execution_bindings = report.get("execution_bindings")
    if execution_bindings is not None:
        if not isinstance(execution_bindings, dict):
            raise ValueError("formal report execution bindings are malformed")
        _validate_execution_binding(report, execution_bindings)
    for index, item in enumerate(runs):
        cell = {key: item.get(key) for key in schedule[index]}
        if cell != schedule[index]:
            raise ValueError("formal completed runs are not an exact schedule prefix")
        run = AgentRun.model_validate(item.get("run"))
        recorded = AegisTransferEvaluation.model_validate(item.get("evaluation"))
        evaluated = evaluate_transfer_protocol_run(
            run,
            scorer_fixture,
            protocol_fixture,
            expected_model=str(schedule[index]["model"]),
        )
        if recorded.model_dump(mode="json") != evaluated.model_dump(mode="json"):
            raise ValueError("formal stored evaluation does not match deterministic rescoring")
        if not recorded.runner_contract_match or not recorded.tool_budget_contract_match:
            raise ValueError("formal completed cell violates the frozen runner contract")
        DatabaseLoad.model_validate(item.get("database_load"))
        cell_bindings = item.get("execution_bindings")
        if not isinstance(cell_bindings, dict):
            raise ValueError("formal completed cell has no live audit binding")
        _validate_execution_binding(report, cell_bindings)
    execution = _mapping(report, "execution")
    expected_execution = _execution_summary(runs, len(schedule))
    if execution != expected_execution:
        raise ValueError("formal report execution summary drifted")
    if require_complete and execution.get("complete") is not True:
        raise ValueError("formal report is incomplete")


def formal_source_semantic_sha256(source_audit: dict[str, object]) -> str:
    case = _mapping(source_audit, "case")
    source = _mapping(source_audit, "source_audit")
    ingestion = _mapping(source_audit, "ingestion")
    equality = _mapping(source_audit, "edge_equality")
    mechanism = _mapping(source_audit, "mechanism_evidence")
    selection = _mapping(source_audit, "selection_audit")
    surfaces = _mapping(source_audit, "semantic_surfaces")
    payload = {
        "audit_schema_version": source_audit.get("audit_schema_version"),
        "mode": source_audit.get("mode"),
        "dataset_revision": source_audit.get("dataset_revision"),
        "adapter_revision": source_audit.get("adapter_revision"),
        "pinned_source": source_audit.get("pinned_source"),
        "selection": selection,
        "case": case,
        "greptimedb": source_audit.get("greptimedb"),
        "source": {
            key: source.get(key)
            for key in (
                "source_row_counts",
                "metric_representation",
                "trace_windows",
                "combined_trace_identity_unique",
                "frozen_edge_checks",
                "frozen_edge_sets_match",
                "signal_windows",
                "all_signal_timestamps_in_declared_windows",
                "trace_windows_exact",
                "source_window_boundaries_accounted",
                "source_window_boundary_anomalies",
                "source_identity_valid",
                "reference_causal_graph_read",
                "reference_causal_graph_ingested",
                "source_data_modified",
            )
        },
        "ingestion": ingestion,
        "edge_equality": {
            key: equality.get(key)
            for key in (
                "raw_edge_query",
                "graph_edge_query",
                "period_raw_replay_exact",
                "period_graph_replay",
                "period_graph_replay_exact",
                "graph_window_strategy_proof",
                "normalized_raw_edges",
                "normalized_graph_edges",
                "raw_edge_set_sha256",
                "graph_edge_set_sha256",
                "exact_edge_set_equality",
                "window_contract",
            )
        },
        "mechanism_evidence": {
            key: mechanism.get(key)
            for key in (
                "predicate",
                "declared_edge",
                "declared_edge_match",
                "original_method",
                "replacement_method",
                "span_name",
                "declared_delay_ns",
                "observable",
                "service_name",
                "method_name",
                "query",
                "normalized_result",
                "expected_result",
                "evidence_match",
                "pass",
            )
        },
        "semantic_coverage": surfaces.get("coverage"),
        "no_model_gates": source_audit.get("no_model_gates"),
    }
    payload["edge_equality"]["period_raw_replay"] = period_raw_replay_semantics(equality)
    payload["edge_equality"]["period_graph_replay"] = period_graph_replay_semantics(equality)
    return _canonical_sha256(payload)


def period_raw_replay_semantics(equality: dict[str, object]) -> dict[str, object]:
    replay = equality.get("period_raw_replay")
    if not isinstance(replay, dict):
        return {}
    result = {}
    for period in ("normal", "abnormal"):
        item = replay.get(period)
        if not isinstance(item, dict):
            continue
        result[period] = {
            key: item.get(key)
            for key in (
                "source_window",
                "raw_edge_query",
                "normalized_stored_raw_edges",
                "normalized_source_raw_edges",
                "stored_raw_edge_set_sha256",
                "source_raw_edge_set_sha256",
                "exact_source_stored_edge_set_equality",
            )
        }
    return result


def period_graph_replay_semantics(equality: dict[str, object]) -> dict[str, object]:
    replay = equality.get("period_graph_replay")
    if not isinstance(replay, dict):
        return {}
    result = {}
    for period in ("normal", "abnormal"):
        item = replay.get(period)
        if not isinstance(item, dict):
            continue
        result[period] = {
            key: item.get(key)
            for key in (
                "graph_observed_window",
                "graph_edge_query",
                "normalized_graph_edges",
                "normalized_raw_edges",
                "graph_edge_set_sha256",
                "raw_edge_set_sha256",
                "exact_raw_graph_edge_set_equality",
            )
        }
    return result


def write_formal_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as output:
        output.write(serialized)
        output.flush()
        temporary = Path(output.name)
    temporary.replace(path)


def _update_execution(report: dict[str, object]) -> None:
    runs = _list_of_mappings(report, "runs")
    schedule = _list_of_mappings(report, "schedule")
    report["execution"] = _execution_summary(runs, len(schedule))


def _execution_summary(runs: list[dict[str, object]], expected: int) -> dict[str, object]:
    runner_errors = 0
    budget_exhaustions = 0
    for item in runs:
        run = item.get("run")
        if not isinstance(run, dict):
            raise ValueError("formal report contains a malformed run")
        runner_errors += run.get("error") is not None
        budget_exhaustions += run.get("tool_budget_exhausted") is True
    return {
        "expected_runs": expected,
        "completed_runs": len(runs),
        "runner_errors": runner_errors,
        "budget_exhaustions": budget_exhaustions,
        "complete": len(runs) == expected,
    }


def _graph_window(report: dict[str, object]) -> tuple[int, int]:
    contract = _mapping(report, "graph_window_contract")
    window = contract.get("graph_observed_window")
    if not isinstance(window, list) or len(window) != 2:
        raise ValueError("formal report has no audited Semantic Graph window")
    return int(window[0]), int(window[1])


def _binding(report: dict[str, object], key: str) -> str:
    value = _mapping(report, "bindings").get(key)
    if not isinstance(value, str):
        raise ValueError(f"formal report binding is missing: {key}")
    return value


def _validate_execution_binding(report: dict[str, object], binding: dict[str, Any]) -> None:
    frozen = _mapping(report, "bindings")
    expected = {
        "source_semantic_sha256": frozen.get("source_semantic_sha256"),
        "scorer_fixture_sha256": frozen.get("scorer_fixture_sha256"),
        "protocol_fixture_sha256": frozen.get("protocol_fixture_sha256"),
    }
    if any(binding.get(key) != value for key, value in expected.items()):
        raise ValueError("formal live audit binding drifted from frozen semantics")
    for key in (
        "source_transfer_audit_sha256",
        "scorer_audit_sha256",
        "protocol_audit_sha256",
    ):
        value = binding.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"formal live audit binding is missing: {key}")


def _mapping(source: dict[str, object], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"expected object at {key}")
    return value


def _list_of_mappings(source: dict[str, object], key: str) -> list[dict[str, Any]]:
    value = source.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"expected object array at {key}")
    return value


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json_object_file(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value
