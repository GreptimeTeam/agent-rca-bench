from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_rca_bench.contracts import AgentRunner, DatabaseLoad, Visibility
from agent_rca_bench.datasets.openrca import (
    DATASET_REVISION as OPENRCA_DATASET_REVISION,
)
from agent_rca_bench.datasets.openrca import (
    MIRROR_REVISION as OPENRCA_MIRROR_REVISION,
)
from agent_rca_bench.datasets.openrca import (
    SOURCE_REVISION as OPENRCA_SOURCE_REVISION,
)
from agent_rca_bench.datasets.openrca import OpenRCARepository
from agent_rca_bench.datasets.openrca import ingest_case as ingest_openrca_case
from agent_rca_bench.datasets.openrca import source_audit as source_audit_openrca
from agent_rca_bench.datasets.openrca import validate_ingest as validate_openrca_ingest
from agent_rca_bench.datasets.openrca2 import (
    DATASET_REVISION as OPENRCA2_DATASET_REVISION,
)
from agent_rca_bench.datasets.openrca2 import (
    SOURCE_REVISION as OPENRCA2_SOURCE_REVISION,
)
from agent_rca_bench.datasets.openrca2 import OpenRCA2Repository
from agent_rca_bench.datasets.openrca2 import ingest_case as ingest_openrca2_case
from agent_rca_bench.datasets.openrca2 import source_audit as source_audit_openrca2
from agent_rca_bench.datasets.openrca2 import validate_ingest as validate_openrca2_ingest
from agent_rca_bench.discovery import (
    DISCOVERY_MAX_TOOL_CALLS,
    DiscoveryAgentRun,
    DiscoveryAudit,
    DiscoveryFixture,
    audit_discovery_fixture,
    evaluate_discovery_run,
    failed_discovery_run,
    run_discovery_agent,
)
from agent_rca_bench.formal_suite_protocol import (
    FormalSuiteProtocolFixture,
    MicroCaseContract,
    load_formal_suite_protocol,
    micro_schedule,
    repository_path,
    sha256_file,
)
from agent_rca_bench.graph_benchmark import (
    GRAPH_MAX_TOOL_CALLS,
    GraphAgentRun,
    GraphAudit,
    GraphFixture,
    audit_graph_fixture,
    canonical_edge_set,
    evaluate_graph_run,
    failed_graph_run,
    run_graph_agent,
    validate_source_window,
)
from agent_rca_bench.greptimedb.client import GreptimeClient
from agent_rca_bench.greptimedb.server import ManagedGreptime, inspect_checkout, write_json
from agent_rca_bench.greptimedb.visibility import QueryGateway
from agent_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
    summarize_semantic_surfaces,
)
from agent_rca_bench.report import MODEL_PRICING
from agent_rca_bench.transfer_protocol import TransferProtocolFixture

MICRO_REPORT_SCHEMA_VERSION = 1
MICRO_REPORT_MODE = "semantic-rca-micro-api-run"

ReportUpdate = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class MicroEnvironmentConfig:
    openrca_cache_dir: Path
    openrca2_cache_dir: Path
    greptimedb_repo: Path
    run_dir: Path
    database: str


@dataclass
class PreparedMicroEnvironment:
    client: GreptimeClient
    case_contract: MicroCaseContract
    fixture: DiscoveryFixture | GraphFixture
    audit: DiscoveryAudit | GraphAudit
    source_audit: dict[str, object]
    semantic_coverage: dict[str, object]


@contextmanager
def prepare_micro_environment(
    suite: FormalSuiteProtocolFixture,
    suite_path: Path,
    case_contract: MicroCaseContract,
    config: MicroEnvironmentConfig,
) -> Iterator[PreparedMicroEnvironment]:
    if config.run_dir.exists():
        raise ValueError(f"exclusive GreptimeDB run directory already exists: {config.run_dir}")
    checkout = inspect_checkout(
        config.greptimedb_repo,
        build_profile=suite.greptimedb_build_profile,
    )
    if checkout["head"] != suite.greptimedb_revision:
        raise ValueError("GreptimeDB HEAD does not match the formal suite revision")
    fixture_path = repository_path(suite_path, case_contract.fixture)
    fixture: DiscoveryFixture | GraphFixture
    if case_contract.benchmark == "discovery":
        fixture = DiscoveryFixture.model_validate_json(fixture_path.read_text())
        case = OpenRCARepository(config.openrca_cache_dir).fetch_case(case_contract.source_case)
        dataset_revision = OPENRCA_DATASET_REVISION
        adapter_revision = OPENRCA_SOURCE_REVISION
        source = source_audit_openrca(case)
    else:
        fixture = GraphFixture.model_validate_json(fixture_path.read_text())
        case = OpenRCA2Repository(config.openrca2_cache_dir).fetch_case(case_contract.source_case)
        dataset_revision = OPENRCA2_DATASET_REVISION
        adapter_revision = OPENRCA2_SOURCE_REVISION
        source = source_audit_openrca2(case)
        validate_source_window(fixture, case.input.time_start, case.input.time_end)
    if case.source_case != case_contract.source_case or fixture.source_case != case.source_case:
        raise ValueError("live source case does not match the formal suite case contract")
    _assert_neutral_database(config.database, case_contract, fixture)
    case = case.model_copy(
        update={"input": case.input.model_copy(update={"database": config.database})}
    )

    managed = ManagedGreptime(Path(str(checkout["binary"])), config.run_dir)
    report: dict[str, object] | None = None
    try:
        managed.start()
        with GreptimeClient(managed.endpoint, database=config.database, timeout=120) as client:
            server_status = client.status()
            client.create_database(config.database)
            assert_semantic_graph_isolated(client, config.database)
            empty_before_ingest = assert_semantic_graph_window_empty(client, case.input)
            if case_contract.benchmark == "discovery":
                counts = ingest_openrca_case(client, case)
                validation = validate_openrca_ingest(client, case, counts)
            else:
                counts = ingest_openrca2_case(client, case)
                validation = validate_openrca2_ingest(client, case, counts)
            surfaces = inspect_semantic_surfaces(client, case.input)
            coverage = surfaces.get("coverage")
            if not isinstance(coverage, dict):
                coverage = summarize_semantic_surfaces(surfaces)
            if case_contract.benchmark == "discovery":
                audit = audit_discovery_fixture(client, fixture)
                gates = {
                    "source_case_match": True,
                    "exclusive_instance_empty_before_ingest": _empty_graph_window(
                        empty_before_ingest
                    ),
                    "discovery_predicate_match": audit.predicate_match,
                    "catalog_target_in_top_five": audit.catalog_target_in_top_five,
                }
            else:
                audit = audit_graph_fixture(client, fixture)
                graph = coverage.get("graph")
                gates = {
                    "source_case_match": True,
                    "exclusive_instance_empty_before_ingest": _empty_graph_window(
                        empty_before_ingest
                    ),
                    "relational_semantic_coverage": (
                        isinstance(graph, dict) and graph.get("status") == "relational"
                    ),
                    "raw_graph_edge_sets_match": audit.edge_sets_match,
                    "unique_winner": audit.unique_winner,
                    "expected_winner_match": audit.expected_winner_match,
                }
            gates["all_passed"] = all(gates.values())
            if gates["all_passed"] is not True:
                raise ValueError(
                    f"formal micro no-model gate failed for {case_contract.source_case}: {gates}"
                )
            report = {
                "source_audit_schema_version": 1,
                "mode": "semantic-rca-micro-no-model-audit",
                "benchmark": case_contract.benchmark,
                "source_case": case_contract.source_case,
                "fixture": case_contract.fixture,
                "fixture_sha256": case_contract.fixture_sha256,
                "dataset_revision": dataset_revision,
                "adapter_revision": adapter_revision,
                "mirror_revision": (
                    OPENRCA_MIRROR_REVISION if case_contract.adapter == "openrca" else None
                ),
                "greptimedb": {
                    "head": checkout["head"],
                    "branch": checkout["branch"],
                    "build_profile": checkout["build_profile"],
                    "binary_version": checkout["binary_version"],
                    "binary_sha256": sha256_file(Path(str(checkout["binary"]))),
                },
                "exclusive_instance": {
                    "loopback_only": True,
                    "database": config.database,
                    "empty_before_ingest": empty_before_ingest,
                    "status": {
                        key: server_status[key]
                        for key in ("version", "branch", "commit", "rustc_version")
                        if key in server_status
                    },
                    "process_stopped_by_command": False,
                },
                "source": source,
                "ingestion": counts.model_dump(mode="json"),
                "validation": validation,
                "semantic_coverage": coverage,
                "task_audit": audit.model_dump(mode="json"),
                "no_model_gates": gates,
            }
            yield PreparedMicroEnvironment(
                client=client,
                case_contract=case_contract,
                fixture=fixture,
                audit=audit,
                source_audit=report,
                semantic_coverage=coverage,
            )
    finally:
        managed.stop()
        if report is not None:
            exclusive = report.get("exclusive_instance")
            if isinstance(exclusive, dict):
                exclusive["process_stopped_by_command"] = (
                    managed.process is not None and managed.process.poll() is not None
                )


def build_micro_preflight_report(
    suite: FormalSuiteProtocolFixture,
    transfer: TransferProtocolFixture,
    suite_path: Path,
    source_audits: list[dict[str, object]],
) -> dict[str, object]:
    _validate_source_audits(suite, source_audits)
    pricing_snapshot = {model.model: dict(MODEL_PRICING[model.model]) for model in transfer.models}
    schedule = micro_schedule(suite, transfer)
    report = {
        "report_schema_version": MICRO_REPORT_SCHEMA_VERSION,
        "mode": MICRO_REPORT_MODE,
        "publication_status": "private raw run artifact; contains provider responses",
        "case_role": "measurement",
        "suite_protocol": json.loads(suite_path.read_text()),
        "bindings": {
            "suite_protocol_sha256": sha256_file(suite_path),
            "pricing_snapshot_sha256": canonical_sha256(pricing_snapshot),
            "source_semantic_sha256": {
                str(source["source_case"]): micro_source_semantic_sha256(source)
                for source in source_audits
            },
        },
        "pricing_snapshot": pricing_snapshot,
        "source_audits": source_audits,
        "schedule": schedule,
        "runs": [],
        "execution": _execution_summary([], len(schedule)),
        "authorization": {
            "paid_api_required": True,
            "reusable_confirmation_stored": False,
            "confirmation_scope": "per invocation; supplied out of band and never persisted",
            "preflight_calls_provider": False,
        },
    }
    validate_micro_report(report, suite, transfer, suite_path)
    return report


def bind_live_micro_environment(
    report: dict[str, object],
    prepared: PreparedMicroEnvironment,
) -> None:
    source_case = prepared.case_contract.source_case
    expected = _mapping(_mapping(report, "bindings"), "source_semantic_sha256").get(source_case)
    if micro_source_semantic_sha256(prepared.source_audit) != expected:
        raise ValueError("live micro source audit differs from the frozen preflight semantics")
    gates = prepared.source_audit.get("no_model_gates")
    if not isinstance(gates, dict) or gates.get("all_passed") is not True:
        raise ValueError("live micro no-model gates did not pass")


def execute_micro_case_runs(
    report: dict[str, object],
    suite: FormalSuiteProtocolFixture,
    transfer: TransferProtocolFixture,
    suite_path: Path,
    prepared: PreparedMicroEnvironment,
    *,
    paid_api_confirmed: bool,
    max_new_runs: int | None = None,
    on_update: ReportUpdate | None = None,
) -> dict[str, object]:
    validate_micro_report(report, suite, transfer, suite_path)
    if paid_api_confirmed is not True:
        raise ValueError("formal paid API execution has not been explicitly confirmed")
    if max_new_runs is not None and max_new_runs < 1:
        raise ValueError("max_new_runs must be positive")
    bind_live_micro_environment(report, prepared)
    schedule = _list_of_mappings(report, "schedule")
    runs = _list_of_mappings(report, "runs")
    pending = schedule[len(runs) :]
    if pending and pending[0]["source_case"] != prepared.case_contract.source_case:
        raise ValueError("live micro environment is not the next scheduled case")
    pending = [
        cell for cell in pending if cell["source_case"] == prepared.case_contract.source_case
    ]
    if max_new_runs is not None:
        pending = pending[:max_new_runs]
    model_contracts = {model.model: model for model in transfer.models}
    for cell in pending:
        model = model_contracts[str(cell["model"])]
        visibility = Visibility(str(cell["visibility"]))
        gateway = QueryGateway(prepared.client, visibility)
        with prepared.client.measure_query_load() as database_load:
            try:
                if prepared.case_contract.benchmark == "discovery":
                    if not isinstance(prepared.fixture, DiscoveryFixture) or not isinstance(
                        prepared.audit, DiscoveryAudit
                    ):
                        raise ValueError("prepared Discovery environment is malformed")
                    run = run_discovery_agent(
                        gateway,
                        prepared.fixture,
                        visibility,
                        runner=AgentRunner.API,
                        model=model.model,
                        api_transport=model.api_transport,
                        reasoning_effort=model.reasoning_effort,
                        max_output_tokens=model.max_output_tokens,
                    )
                    evaluation = evaluate_discovery_run(
                        run,
                        prepared.fixture,
                        prepared.audit.canonical_result,
                        database=prepared.client.database,
                    )
                else:
                    if not isinstance(prepared.fixture, GraphFixture) or not isinstance(
                        prepared.audit, GraphAudit
                    ):
                        raise ValueError("prepared Graph environment is malformed")
                    run = run_graph_agent(
                        gateway,
                        prepared.fixture,
                        visibility,
                        prepared.semantic_coverage,
                        runner=AgentRunner.API,
                        model=model.model,
                        api_transport=model.api_transport,
                        reasoning_effort=model.reasoning_effort,
                        max_output_tokens=model.max_output_tokens,
                    )
                    evaluation = evaluate_graph_run(
                        run,
                        prepared.fixture,
                        prepared.audit.graph_result,
                        database=prepared.client.database,
                    )
            except Exception as error:
                if prepared.case_contract.benchmark == "discovery":
                    run = failed_discovery_run(
                        visibility,
                        AgentRunner.API,
                        model.model,
                        f"runner failed: {error}",
                        max_tool_calls=DISCOVERY_MAX_TOOL_CALLS,
                    ).model_copy(
                        update={
                            "api_transport": model.api_transport,
                            "reasoning_effort": model.reasoning_effort,
                            "max_output_tokens": model.max_output_tokens,
                        }
                    )
                    evaluation = evaluate_discovery_run(
                        run,
                        prepared.fixture,
                        prepared.audit.canonical_result,
                        database=prepared.client.database,
                    )
                else:
                    run = failed_graph_run(
                        visibility,
                        AgentRunner.API,
                        model.model,
                        f"runner failed: {error}",
                        max_tool_calls=GRAPH_MAX_TOOL_CALLS,
                    ).model_copy(
                        update={
                            "api_transport": model.api_transport,
                            "reasoning_effort": model.reasoning_effort,
                            "max_output_tokens": model.max_output_tokens,
                        }
                    )
                    evaluation = evaluate_graph_run(
                        run,
                        prepared.fixture,
                        prepared.audit.graph_result,
                        database=prepared.client.database,
                    )
        result = {
            **cell,
            "run": run.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
            "database_load": database_load.model_dump(mode="json"),
            "source_semantic_sha256": micro_source_semantic_sha256(prepared.source_audit),
        }
        report_runs = report.get("runs")
        if not isinstance(report_runs, list):
            raise ValueError("formal micro report runs are malformed")
        _validate_run_contract(result, model.api_transport.value, model.reasoning_effort)
        report_runs.append(result)
        report["execution"] = _execution_summary(report_runs, len(schedule))
        if on_update is not None:
            on_update(report)
    return report


def validate_micro_report(
    report: dict[str, object],
    suite: FormalSuiteProtocolFixture,
    transfer: TransferProtocolFixture,
    suite_path: Path,
    *,
    require_complete: bool = False,
) -> None:
    loaded_suite, loaded_transfer = load_formal_suite_protocol(suite_path)
    if loaded_suite != suite or loaded_transfer != transfer:
        raise ValueError("formal micro protocol object differs from its bound fixture")
    if (
        report.get("report_schema_version") != MICRO_REPORT_SCHEMA_VERSION
        or report.get("mode") != MICRO_REPORT_MODE
        or report.get("case_role") != "measurement"
    ):
        raise ValueError("unsupported formal micro report")
    if report.get("suite_protocol") != json.loads(suite_path.read_text()):
        raise ValueError("formal micro suite protocol drifted")
    if report.get("authorization") != {
        "paid_api_required": True,
        "reusable_confirmation_stored": False,
        "confirmation_scope": "per invocation; supplied out of band and never persisted",
        "preflight_calls_provider": False,
    }:
        raise ValueError("formal micro authorization boundary drifted")
    bindings = _mapping(report, "bindings")
    if bindings.get("suite_protocol_sha256") != sha256_file(suite_path):
        raise ValueError("formal micro suite fixture binding drifted")
    pricing = _mapping(report, "pricing_snapshot")
    if set(pricing) != {model.model for model in transfer.models}:
        raise ValueError("formal micro pricing snapshot is malformed")
    if bindings.get("pricing_snapshot_sha256") != canonical_sha256(pricing):
        raise ValueError("formal micro pricing snapshot binding drifted")
    sources = _list_of_mappings(report, "source_audits")
    _validate_source_audits(suite, sources)
    expected_source_hashes = {
        str(source["source_case"]): micro_source_semantic_sha256(source) for source in sources
    }
    if bindings.get("source_semantic_sha256") != expected_source_hashes:
        raise ValueError("formal micro source audit binding drifted")
    schedule = _list_of_mappings(report, "schedule")
    if schedule != micro_schedule(suite, transfer):
        raise ValueError("formal micro schedule drifted")
    runs = _list_of_mappings(report, "runs")
    if len(runs) > len(schedule):
        raise ValueError("formal micro report contains more runs than scheduled cells")
    source_by_case = {str(source["source_case"]): source for source in sources}
    case_by_source = {case.source_case: case for case in suite.micro_cases}
    model_by_name = {model.model: model for model in transfer.models}
    for index, item in enumerate(runs):
        cell = {key: item.get(key) for key in schedule[index]}
        if cell != schedule[index]:
            raise ValueError("formal micro completed runs are not an exact schedule prefix")
        source_case = str(item["source_case"])
        source = source_by_case[source_case]
        if item.get("source_semantic_sha256") != micro_source_semantic_sha256(source):
            raise ValueError("formal micro completed cell source binding drifted")
        case_contract = case_by_source[source_case]
        fixture_path = repository_path(suite_path, case_contract.fixture)
        audit_payload = _mapping(source, "task_audit")
        if case_contract.benchmark == "discovery":
            fixture = DiscoveryFixture.model_validate_json(fixture_path.read_text())
            audit = DiscoveryAudit.model_validate(audit_payload)
            run = DiscoveryAgentRun.model_validate(item.get("run"))
            recorded = evaluate_discovery_run(
                run,
                fixture,
                audit.canonical_result,
                database=_source_database(source),
            )
        else:
            fixture = GraphFixture.model_validate_json(fixture_path.read_text())
            audit = GraphAudit.model_validate(audit_payload)
            run = GraphAgentRun.model_validate(item.get("run"))
            recorded = evaluate_graph_run(
                run,
                fixture,
                audit.graph_result,
                database=_source_database(source),
            )
        if recorded.model_dump(mode="json") != item.get("evaluation"):
            raise ValueError("formal micro stored evaluation differs from deterministic rescoring")
        model = model_by_name[str(item["model"])]
        _validate_run_contract(item, model.api_transport.value, model.reasoning_effort)
        DatabaseLoad.model_validate(item.get("database_load"))
    if report.get("execution") != _execution_summary(runs, len(schedule)):
        raise ValueError("formal micro execution summary drifted")
    if require_complete and _mapping(report, "execution").get("complete") is not True:
        raise ValueError("formal micro report is incomplete")


def micro_source_semantic_sha256(source: Mapping[str, object]) -> str:
    payload = {
        key: source.get(key)
        for key in (
            "source_audit_schema_version",
            "mode",
            "benchmark",
            "source_case",
            "fixture",
            "fixture_sha256",
            "dataset_revision",
            "adapter_revision",
            "mirror_revision",
            "greptimedb",
            "source",
            "ingestion",
            "validation",
            "semantic_coverage",
            "no_model_gates",
        )
    }
    task_audit = source.get("task_audit")
    payload["task_audit"] = (
        _graph_task_audit_semantics(task_audit)
        if source.get("benchmark") == "graph"
        else _without_runtime_query_metadata(task_audit)
    )
    return canonical_sha256(payload)


def _graph_task_audit_semantics(value: object) -> dict[str, object]:
    audit = GraphAudit.model_validate(value)
    trace_edges = canonical_edge_set(audit.trace_result)
    graph_edges = canonical_edge_set(audit.graph_result)
    if trace_edges is None or graph_edges is None:
        raise ValueError("formal Graph audit has no canonical edge set")
    return {
        "fixture_id": audit.fixture_id,
        "trace_query": audit.trace_query,
        "trace_edges": [list(edge) for edge in sorted(trace_edges)],
        "trace_result_truncated": audit.trace_result.truncated,
        "graph_query": audit.graph_query,
        "graph_edges": [list(edge) for edge in sorted(graph_edges)],
        "graph_result_truncated": audit.graph_result.truncated,
        "edge_sets_match": audit.edge_sets_match,
        "unique_winner": audit.unique_winner,
        "expected_winner_match": audit.expected_winner_match,
    }


def write_micro_report(path: Path, report: dict[str, object]) -> None:
    write_json(path, report)


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_source_audits(
    suite: FormalSuiteProtocolFixture,
    source_audits: list[dict[str, object]],
) -> None:
    if [source.get("source_case") for source in source_audits] != [
        case.source_case for case in suite.micro_cases
    ]:
        raise ValueError("formal micro source audits do not match the suite case order")
    for case, source in zip(suite.micro_cases, source_audits, strict=True):
        if (
            source.get("benchmark") != case.benchmark
            or source.get("fixture") != case.fixture
            or source.get("fixture_sha256") != case.fixture_sha256
        ):
            raise ValueError("formal micro source audit case binding drifted")
        greptimedb = source.get("greptimedb")
        gates = source.get("no_model_gates")
        if (
            not isinstance(greptimedb, dict)
            or greptimedb.get("head") != suite.greptimedb_revision
            or greptimedb.get("build_profile") != suite.greptimedb_build_profile
            or not isinstance(gates, dict)
            or gates.get("all_passed") is not True
        ):
            raise ValueError("formal micro source audit did not pass its frozen gates")


def _execution_summary(runs: list[dict[str, object]], expected: int) -> dict[str, object]:
    runner_errors = budget_exhaustions = 0
    for item in runs:
        run = item.get("run")
        if isinstance(run, dict):
            runner_errors += run.get("error") is not None
            budget_exhaustions += run.get("tool_budget_exhausted") is True
    return {
        "completed_runs": len(runs),
        "expected_runs": expected,
        "remaining_runs": expected - len(runs),
        "runner_errors": runner_errors,
        "budget_exhaustions": budget_exhaustions,
        "complete": len(runs) == expected,
    }


def _validate_run_contract(
    item: Mapping[str, object],
    api_transport: str,
    reasoning_effort: str | None,
) -> None:
    run = item.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("formal micro run payload is malformed")
    benchmark = item.get("benchmark")
    max_calls = DISCOVERY_MAX_TOOL_CALLS if benchmark == "discovery" else GRAPH_MAX_TOOL_CALLS
    if (
        run.get("runner") != AgentRunner.API.value
        or run.get("model") != item.get("model")
        or run.get("visibility") != item.get("visibility")
        or run.get("api_transport") != api_transport
        or run.get("reasoning_effort") != reasoning_effort
        or run.get("max_output_tokens") != item.get("max_output_tokens")
        or run.get("turn_limit") != max_calls + 10
        or run.get("turn_limit_enforced") is not True
    ):
        raise ValueError("formal micro runner contract drifted")
    calls = run.get("tool_calls")
    if not isinstance(calls, list) or len(calls) > max_calls:
        raise ValueError("formal micro tool-call execution contract drifted")


def _source_database(source: Mapping[str, object]) -> str:
    exclusive = source.get("exclusive_instance")
    if not isinstance(exclusive, Mapping) or not isinstance(exclusive.get("database"), str):
        raise ValueError("formal micro source audit has no database binding")
    return str(exclusive["database"])


def _empty_graph_window(value: Mapping[str, object]) -> bool:
    return (
        value.get("mode") == "empty-window-before-ingest"
        and value.get("entity_rows") == 0
        and value.get("relationship_rows") == 0
    )


def _without_runtime_query_metadata(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_runtime_query_metadata(item)
            for key, item in value.items()
            if key not in {"query_id", "elapsed_seconds"}
        }
    if isinstance(value, list):
        return [_without_runtime_query_metadata(item) for item in value]
    return value


def _assert_neutral_database(
    database: str,
    case: MicroCaseContract,
    fixture: DiscoveryFixture | GraphFixture,
) -> None:
    text = database.lower()
    forbidden = {case.source_case.lower()}
    if isinstance(fixture, DiscoveryFixture):
        forbidden.update((fixture.component.lower(), fixture.signal.lower()))
    else:
        forbidden.update((fixture.caller.lower(), fixture.expected_callee.lower()))
    if any(value and value in text for value in forbidden):
        raise ValueError("formal micro database name leaks fixture labels")


def _mapping(source: Mapping[str, object], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"expected object at {key}")
    return value


def _list_of_mappings(source: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    value = source.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"expected object list at {key}")
    return value
