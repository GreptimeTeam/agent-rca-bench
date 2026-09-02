from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from semantic_rca_bench.agent import run_agent
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    ApiTransport,
    CausalScope,
    DatabaseLoad,
    Visibility,
)
from semantic_rca_bench.datasets.openrca2 import ingest_case
from semantic_rca_bench.datasets.openrca2_transfer import (
    TransferCaseSpec,
    exact_edge_equality_audit,
    load_selected_case,
    mechanism_evidence_audit,
    no_model_gates,
    source_telemetry_audit,
    validate_transfer_ingest,
)
from semantic_rca_bench.datasets.rca100 import RCA100Repository
from semantic_rca_bench.datasets.rca100 import ingest_case as ingest_rca100_case
from semantic_rca_bench.datasets.rca100_audit import (
    exact_edge_equality_audit as node_exact_edge_equality_audit,
)
from semantic_rca_bench.datasets.rca100_audit import no_model_gates as node_no_model_gates
from semantic_rca_bench.datasets.rca100_audit import (
    source_telemetry_audit as node_source_telemetry_audit,
)
from semantic_rca_bench.datasets.rca100_audit import (
    validate_transfer_ingest as node_validate_transfer_ingest,
)
from semantic_rca_bench.datasets.rca100_transfer import load_selected_case as load_node_case
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.greptimedb.server import ManagedGreptime, inspect_checkout
from semantic_rca_bench.greptimedb.visibility import QueryGateway
from semantic_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
    summarize_semantic_surfaces,
)
from semantic_rca_bench.protocol import benchmark_protocol
from semantic_rca_bench.report import MODEL_PRICING
from semantic_rca_bench.transfer_protocol import (
    TransferCohort,
    TransferProtocolFixture,
    formal_schedule,
    sha256_file,
)
from semantic_rca_bench.transfer_scorer import evaluate_transfer_run

REPORT_SCHEMA_VERSION = 1
REPORT_MODE = "semantic-rca-openrca2-transfer-api-run"

ReportUpdate = Callable[[dict[str, object]], None]
RunAgent = Callable[..., AgentRun]


@dataclass(frozen=True)
class TransferEnvironmentConfig:
    cache_dir: Path
    manifest_path: Path
    greptimedb_repo: Path
    run_dir: Path
    database: str
    node_cache_dir: Path = Path(".data/rca100")


@dataclass(frozen=True)
class CaseAdapter:
    """The source-specific half of preparing one measurement case."""

    name: str
    load_case: Callable[[TransferEnvironmentConfig, TransferCaseSpec], object]
    ingest: Callable[..., object]
    source_audit: Callable[..., dict[str, object]]
    validate_ingest: Callable[..., dict[str, object]]
    edge_equality: Callable[..., dict[str, object]]
    gates: Callable[..., dict[str, bool]]


def _load_openrca2_case(config: TransferEnvironmentConfig, spec: TransferCaseSpec) -> object:
    return load_selected_case(
        config.cache_dir,
        config.manifest_path,
        spec,
        database=config.database,
    )


def _load_rca100_case(config: TransferEnvironmentConfig, spec: TransferCaseSpec) -> object:
    return load_node_case(
        RCA100Repository(config.node_cache_dir),
        spec,
        database=config.database,
    )


def _ingest_rca100_case(client: GreptimeClient, case: object, spec: TransferCaseSpec) -> object:
    # The archives carry about an hour around each alert, so the database is
    # held to the same extent as the window the agent is given.
    return ingest_rca100_case(
        client,
        case,
        window=(spec.normal_window[0], spec.abnormal_window[1]),
    )


OPENRCA2_ADAPTER = CaseAdapter(
    name="openrca2",
    load_case=_load_openrca2_case,
    ingest=lambda client, case, spec: ingest_case(client, case),
    source_audit=source_telemetry_audit,
    validate_ingest=validate_transfer_ingest,
    edge_equality=exact_edge_equality_audit,
    gates=no_model_gates,
)

RCA100_ADAPTER = CaseAdapter(
    name="rca100",
    load_case=_load_rca100_case,
    ingest=_ingest_rca100_case,
    source_audit=node_source_telemetry_audit,
    validate_ingest=node_validate_transfer_ingest,
    edge_equality=node_exact_edge_equality_audit,
    gates=node_no_model_gates,
)

_CASE_ADAPTERS = {False: OPENRCA2_ADAPTER, True: RCA100_ADAPTER}


@dataclass
class PreparedTransferEnvironment:
    client: GreptimeClient
    case: object
    spec: TransferCaseSpec
    source_audit: dict[str, object]
    semantic_coverage: dict[str, object]
    graph_window: tuple[int, int]


@contextmanager
def prepare_transfer_environment(
    protocol: TransferProtocolFixture,
    spec: TransferCaseSpec,
    config: TransferEnvironmentConfig,
) -> Iterator[PreparedTransferEnvironment]:
    if config.run_dir.exists():
        raise ValueError(f"exclusive GreptimeDB run directory already exists: {config.run_dir}")
    expected_database = spec.opaque_case_id.replace("-", "_")
    if config.database != expected_database:
        raise ValueError(
            f"transfer database must be the opaque case ID with underscores: {expected_database}"
        )
    # The node cases come from a different source archive, so their loader,
    # window restriction, and fidelity audits are the RCA100 ones. Replaying
    # them through the OpenRCA2 path would ingest the wrong extent and audit
    # the wrong contract.
    adapter = _CASE_ADAPTERS[spec.causal_scope is CausalScope.INFRASTRUCTURE_NODE]
    checkout = inspect_checkout(
        config.greptimedb_repo,
        build_profile=protocol.greptimedb_build_profile,
    )
    if checkout["head"] != protocol.greptimedb_revision:
        raise ValueError("GreptimeDB HEAD does not match the transfer protocol")
    case = adapter.load_case(config, spec)
    managed = ManagedGreptime(Path(str(checkout["binary"])), config.run_dir)
    report: dict[str, object] | None = None
    try:
        managed.start()
        with GreptimeClient(managed.endpoint, database=config.database, timeout=120) as client:
            server_status = client.status()
            client.create_database(config.database)
            assert_semantic_graph_isolated(client, config.database)
            empty = assert_semantic_graph_window_empty(client, case.input)
            source = adapter.source_audit(case, spec)
            counts = adapter.ingest(client, case, spec)
            stored = adapter.validate_ingest(client, case, counts, source)
            surfaces = inspect_semantic_surfaces(client, case.input)
            coverage = surfaces.get("coverage")
            if not isinstance(coverage, dict):
                coverage = summarize_semantic_surfaces(surfaces)
            equality = adapter.edge_equality(client, spec, source)
            mechanism = mechanism_evidence_audit(client, spec)
            surface_contract = _mapping(coverage, "surface_contract").get("current") is True
            gates = adapter.gates(
                case,
                spec,
                source,
                stored,
                equality,
                mechanism,
                isolated=_empty_before_ingest(empty),
                semantic_surface_contract=surface_contract,
            )
            report = {
                "audit_schema_version": 1,
                "mode": f"semantic-rca-{adapter.name}-transfer-no-model-audit",
                "dataset_revision": case.dataset,
                "case": spec.model_dump(mode="json"),
                "agent_facing_case": case.input.model_dump(mode="json"),
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
                    "empty_before_ingest": empty,
                    "status": {
                        key: server_status[key]
                        for key in ("version", "branch", "commit", "rustc_version")
                        if key in server_status
                    },
                    "process_stopped_by_command": False,
                },
                "source": source,
                "ingestion": stored,
                "semantic_coverage": coverage,
                "edge_equality": equality,
                "mechanism_evidence": mechanism,
                "no_model_gates": gates,
            }
            if gates["all_passed"] is not True:
                raise ValueError(
                    f"transfer no-model gate failed for {spec.opaque_case_id}: {gates}"
                )
            observed_start, observed_end = _mapping(equality, "window_contract")[
                "graph_observed_window"
            ]
            yield PreparedTransferEnvironment(
                client=client,
                case=case,
                spec=spec,
                source_audit=report,
                semantic_coverage=coverage,
                graph_window=(int(observed_start), int(observed_end)),
            )
    finally:
        managed.stop()
        if report is not None:
            exclusive = report.get("exclusive_instance")
            if isinstance(exclusive, dict):
                exclusive["process_stopped_by_command"] = (
                    managed.process is not None and managed.process.poll() is not None
                )


def build_preflight_report(
    protocol: TransferProtocolFixture,
    protocol_path: Path,
    selection: TransferCohort,
    source_audits: Sequence[dict[str, object]],
) -> dict[str, object]:
    specs = list(selection.selected_cases)
    if [audit.get("case", {}).get("opaque_case_id") for audit in source_audits] != [
        spec.opaque_case_id for spec in specs
    ]:
        raise ValueError("transfer source audits do not match the selected case order")
    for audit in source_audits:
        gates = audit.get("no_model_gates")
        if not isinstance(gates, dict) or gates.get("all_passed") is not True:
            raise ValueError("transfer preflight contains a failed no-model audit")
    schedule = formal_schedule(protocol, selection)
    pricing = {model.model: dict(MODEL_PRICING[model.model]) for model in protocol.models}
    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "mode": REPORT_MODE,
        "phase": "measurement",
        "publication_status": "private raw run artifact; contains provider responses",
        "benchmark_protocol": benchmark_protocol(),
        "formal_protocol": json.loads(protocol_path.read_text()),
        "bindings": {
            "protocol_fixture_sha256": sha256_file(protocol_path),
            "selection_semantic_sha256": canonical_sha256(selection.model_dump(mode="json")),
            "source_semantic_sha256": {
                str(audit["case"]["opaque_case_id"]): source_semantic_sha256(audit)
                for audit in source_audits
            },
            "pricing_snapshot_sha256": canonical_sha256(pricing),
        },
        "pricing_snapshot": pricing,
        "source_audits": list(source_audits),
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
    validate_private_report(report, protocol, protocol_path, selection)
    return report


def execute_case_runs(
    report: dict[str, object],
    protocol: TransferProtocolFixture,
    protocol_path: Path,
    selection: TransferCohort,
    prepared: PreparedTransferEnvironment,
    *,
    paid_api_confirmed: bool,
    max_new_runs: int | None = None,
    run_agent_fn: RunAgent = run_agent,
    on_update: ReportUpdate | None = None,
) -> dict[str, object]:
    validate_private_report(report, protocol, protocol_path, selection)
    if paid_api_confirmed is not True:
        raise ValueError("transfer paid API execution has not been explicitly confirmed")
    if max_new_runs is not None and max_new_runs < 1:
        raise ValueError("max_new_runs must be positive")
    expected_hash = _mapping(_mapping(report, "bindings"), "source_semantic_sha256").get(
        prepared.spec.opaque_case_id
    )
    if source_semantic_sha256(prepared.source_audit) != expected_hash:
        raise ValueError("live transfer environment differs from preflight")
    schedule = _mapping_list(report, "schedule")
    runs = _mapping_list(report, "runs")
    pending = schedule[len(runs) :]
    if pending and pending[0]["case_id"] != prepared.spec.opaque_case_id:
        raise ValueError("live transfer environment is not the next scheduled case")
    pending = [cell for cell in pending if cell["case_id"] == prepared.spec.opaque_case_id]
    if max_new_runs is not None:
        pending = pending[:max_new_runs]
    model_contracts = {model.model: model for model in protocol.models}
    for cell in pending:
        model = model_contracts[str(cell["model"])]
        visibility = Visibility(str(cell["visibility"]))
        gateway = QueryGateway(
            prepared.client,
            visibility,
            semantic_graph_window=(
                prepared.graph_window if visibility is Visibility.SEMANTIC_GRAPH else None
            ),
        )
        with prepared.client.measure_query_load() as database_load:
            try:
                run = run_agent_fn(
                    gateway,
                    prepared.case.input,
                    visibility,
                    model=model.model,
                    api_transport=model.api_transport,
                    reasoning_effort=model.reasoning_effort,
                    max_tool_calls=protocol.max_tool_calls,
                    max_turns=protocol.max_turns,
                    max_output_tokens=model.max_output_tokens,
                    semantic_coverage=prepared.semantic_coverage,
                )
            except Exception as error:
                run = _failed_run(
                    visibility,
                    model.model,
                    model.api_transport,
                    model.reasoning_effort,
                    model.max_output_tokens,
                    str(error),
                )
        evaluation = evaluate_transfer_run(
            run,
            prepared.spec,
            expected_model=model.model,
            expected_transport=model.api_transport,
            expected_reasoning_effort=model.reasoning_effort,
            expected_max_output_tokens=model.max_output_tokens,
            max_tool_calls=protocol.max_tool_calls,
        )
        item = {
            **cell,
            "run": run.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
            "database_load": database_load.model_dump(mode="json"),
            "source_semantic_sha256": expected_hash,
        }
        report_runs = report.get("runs")
        if not isinstance(report_runs, list):
            raise ValueError("transfer report runs are malformed")
        report_runs.append(item)
        report["execution"] = _execution_summary(report_runs, len(schedule))
        if on_update is not None:
            on_update(report)
    return report


def validate_private_report(
    report: Mapping[str, object],
    protocol: TransferProtocolFixture,
    protocol_path: Path,
    selection: TransferCohort,
    *,
    require_complete: bool = False,
) -> None:
    if (
        report.get("report_schema_version") != REPORT_SCHEMA_VERSION
        or report.get("mode") != REPORT_MODE
        or report.get("benchmark_protocol") != benchmark_protocol()
        or report.get("formal_protocol") != json.loads(protocol_path.read_text())
    ):
        raise ValueError("unsupported or drifted transfer report")
    if report.get("phase") != "measurement":
        raise ValueError("transfer report phase drifted")
    expected_schedule = formal_schedule(protocol, selection)
    if report.get("authorization") != {
        "paid_api_required": True,
        "reusable_confirmation_stored": False,
        "confirmation_scope": "per invocation; supplied out of band and never persisted",
        "preflight_calls_provider": False,
    }:
        raise ValueError("transfer authorization boundary drifted")
    bindings = _mapping(report, "bindings")
    if (
        bindings.get("protocol_fixture_sha256") != sha256_file(protocol_path)
        or bindings.get("selection_semantic_sha256")
        != canonical_sha256(selection.model_dump(mode="json"))
        or bindings.get("pricing_snapshot_sha256")
        != canonical_sha256(report.get("pricing_snapshot"))
    ):
        raise ValueError("transfer report bindings drifted")
    if _mapping_list(report, "schedule") != expected_schedule:
        raise ValueError("transfer schedule drifted")
    source_audits = _mapping_list(report, "source_audits")
    expected_source_hashes = {
        str(audit["case"]["opaque_case_id"]): source_semantic_sha256(dict(audit))
        for audit in source_audits
    }
    if bindings.get("source_semantic_sha256") != expected_source_hashes:
        raise ValueError("transfer source audit binding drifted")
    specs = {case.opaque_case_id: case for case in selection.selected_cases}
    models = {model.model: model for model in protocol.models}
    runs = _mapping_list(report, "runs")
    if len(runs) > len(expected_schedule):
        raise ValueError("transfer report has more runs than scheduled")
    for index, item in enumerate(runs):
        expected = expected_schedule[index]
        if any(item.get(key) != value for key, value in expected.items()):
            raise ValueError("transfer completed runs are not an exact schedule prefix")
        run = AgentRun.model_validate(item.get("run"))
        model = models[str(item["model"])]
        evaluated = evaluate_transfer_run(
            run,
            specs[str(item["case_id"])],
            expected_model=model.model,
            expected_transport=model.api_transport,
            expected_reasoning_effort=model.reasoning_effort,
            expected_max_output_tokens=model.max_output_tokens,
            max_tool_calls=protocol.max_tool_calls,
        )
        if item.get("evaluation") != evaluated.model_dump(mode="json"):
            raise ValueError("transfer stored evaluation does not rescore")
        DatabaseLoad.model_validate(item.get("database_load"))
        if item.get("source_semantic_sha256") != expected_source_hashes[item["case_id"]]:
            raise ValueError("transfer completed cell source binding drifted")
    if report.get("execution") != _execution_summary(runs, len(expected_schedule)):
        raise ValueError("transfer execution summary drifted")
    if require_complete and not _mapping(report, "execution").get("complete"):
        raise ValueError("transfer report is incomplete")


def source_semantic_sha256(audit: Mapping[str, object]) -> str:
    return canonical_sha256(_stable(audit))


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _failed_run(
    visibility: Visibility,
    model: str,
    transport: ApiTransport,
    reasoning_effort: str | None,
    max_output_tokens: int,
    error: str,
) -> AgentRun:
    return AgentRun(
        run_id="runner-failure",
        visibility=visibility,
        model=model,
        runner=AgentRunner.API,
        api_transport=transport,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        diagnosis=None,
        error=f"runner failed: {error}",
        tool_calls=[],
        tool_calls_requested=0,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def _execution_summary(runs: Sequence[Mapping[str, object]], expected: int) -> dict[str, object]:
    return {
        "expected_runs": expected,
        "completed_runs": len(runs),
        "runner_errors": sum(_mapping(run, "run").get("error") is not None for run in runs),
        "budget_exhaustions": sum(
            _mapping(run, "run").get("tool_budget_exhausted") is True for run in runs
        ),
        "complete": len(runs) == expected,
    }


def _stable(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _stable(item)
            for key, item in value.items()
            if key
            not in {
                "query_id",
                "elapsed_seconds",
                "process_stopped_by_command",
            }
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _empty_before_ingest(value: Mapping[str, object]) -> bool:
    return value.get("entity_rows") == 0 and value.get("relationship_rows") == 0


def _mapping(value: Mapping[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"transfer report field is not an object: {key}")
    return dict(item)


def _mapping_list(value: Mapping[str, object], key: str) -> list[dict[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"transfer report field is not an object list: {key}")
    return [dict(item) for item in items]
