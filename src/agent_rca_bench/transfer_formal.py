from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from agent_rca_bench.agent import run_agent, run_split_agent
from agent_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    ApiTransport,
    CausalScope,
    DatabaseLoad,
    Visibility,
)
from agent_rca_bench.datasets.openrca2 import _iter_logs as openrca2_logs
from agent_rca_bench.datasets.openrca2 import _iter_traces as openrca2_traces
from agent_rca_bench.datasets.openrca2 import ingest_case
from agent_rca_bench.datasets.openrca2_transfer import (
    TransferCaseSpec,
    exact_edge_equality_audit,
    load_selected_case,
    mechanism_evidence_audit,
    no_model_gates,
    source_telemetry_audit,
    validate_transfer_ingest,
)
from agent_rca_bench.datasets.rca100 import RCA100Repository
from agent_rca_bench.datasets.rca100 import _iter_alerts as rca100_alerts
from agent_rca_bench.datasets.rca100 import _iter_events as rca100_events
from agent_rca_bench.datasets.rca100 import _iter_logs as rca100_logs
from agent_rca_bench.datasets.rca100 import _iter_traces as rca100_traces
from agent_rca_bench.datasets.rca100 import _metric_series as rca100_metric_series
from agent_rca_bench.datasets.rca100 import _spans_within_window as rca100_spans_within_window
from agent_rca_bench.datasets.rca100 import _within_window as rca100_within_window
from agent_rca_bench.datasets.rca100 import ingest_case as ingest_rca100_case
from agent_rca_bench.datasets.rca100_audit import (
    exact_edge_equality_audit as node_exact_edge_equality_audit,
)
from agent_rca_bench.datasets.rca100_audit import no_model_gates as node_no_model_gates
from agent_rca_bench.datasets.rca100_audit import (
    source_telemetry_audit as node_source_telemetry_audit,
)
from agent_rca_bench.datasets.rca100_audit import (
    validate_transfer_ingest as node_validate_transfer_ingest,
)
from agent_rca_bench.datasets.rca100_transfer import load_selected_case as load_node_case
from agent_rca_bench.greptimedb.client import GreptimeClient, GreptimeError
from agent_rca_bench.greptimedb.server import ManagedGreptime, inspect_checkout, write_json
from agent_rca_bench.greptimedb.visibility import QueryGateway, QueryRejected
from agent_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
    summarize_semantic_surfaces,
)
from agent_rca_bench.protocol import benchmark_protocol
from agent_rca_bench.report import MODEL_PRICING
from agent_rca_bench.split_audit import _audit_traces, audit_split_storage
from agent_rca_bench.split_client import FanoutIngestClient, ProtocolHttpClient
from agent_rca_bench.split_ingest import (
    MetricSeries,
    collapse_series,
    load_prometheus_metrics,
    openrca2_case_series,
)
from agent_rca_bench.split_query import NativeQueryError, SplitQueryGateway
from agent_rca_bench.split_stack import ManagedSplitStack, SplitStackImages
from agent_rca_bench.transfer_protocol import (
    TransferCohort,
    TransferProtocolFixture,
    formal_schedule,
    sha256_file,
)
from agent_rca_bench.transfer_scorer import evaluate_transfer_run

REPORT_SCHEMA_VERSION = 1
REPORT_MODE = "semantic-rca-openrca2-transfer-api-run"

# One agent-facing query timeout for every arm. A shorter budget on one store
# would turn a slow query into a tool failure there and a citation elsewhere.
AGENT_QUERY_TIMEOUT_SECONDS = 120.0

ReportUpdate = Callable[[dict[str, object]], None]
RunAgent = Callable[..., AgentRun]

# Selected source files are shared by every case worker. RCA100 can materialize
# a missing file on demand, including one shared taxonomy file, so case loading
# must not race through the same cache. GreptimeDB startup is also serialized so
# a port selected by one worker is bound before another worker or Docker asks
# the kernel for its own ephemeral ports.
_SOURCE_CACHE_LOCK = Lock()
_ENVIRONMENT_START_LOCK = Lock()


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
    # The split stack cannot take the same protocol bytes for metrics or logs,
    # so each adapter also hands over its source telemetry in the shape the
    # split loader and audit need.
    split_source: Callable[..., SplitSource]


@dataclass(frozen=True)
class SplitSource:
    """One case's source telemetry, as the split stack has to receive it."""

    metric_series: list[object]
    metric_projection: dict[str, object]
    logs_by_table: dict[str, list[object]]
    spans: list[object]
    trace_scope_name: str


def _openrca2_split_source(case: object, spec: TransferCaseSpec) -> SplitSource:
    series, projection = openrca2_case_series(case)
    return SplitSource(
        metric_series=series,
        metric_projection=dict(projection),
        logs_by_table={"logs": list(openrca2_logs(case.logs_paths))},
        spans=list(openrca2_traces(case.traces_paths)),
        trace_scope_name="openrca2-replay",
    )


def _rca100_split_source(case: object, spec: TransferCaseSpec) -> SplitSource:
    window = (spec.normal_window[0], spec.abnormal_window[1])
    series, projection = collapse_series(rca100_metric_series(case.metrics_path, window=window))
    return SplitSource(
        metric_series=series,
        metric_projection={
            "protocol": "prometheus-remote-write-0.1",
            "source": "rca100 native remote-write series",
            **projection,
        },
        logs_by_table={
            "logs": list(rca100_within_window(rca100_logs(case.logs_path), window)),
            "events": list(rca100_within_window(rca100_events(case.events_path), window)),
            "alerts": list(rca100_within_window(rca100_alerts(case.alerts_path), window)),
        },
        spans=list(rca100_spans_within_window(rca100_traces(case.traces_path), window)),
        trace_scope_name="rca100-replay",
    )


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
    split_source=_openrca2_split_source,
)

RCA100_ADAPTER = CaseAdapter(
    name="rca100",
    load_case=_load_rca100_case,
    ingest=_ingest_rca100_case,
    source_audit=node_source_telemetry_audit,
    validate_ingest=node_validate_transfer_ingest,
    edge_equality=node_exact_edge_equality_audit,
    gates=node_no_model_gates,
    split_source=_rca100_split_source,
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
    split_stack: ManagedSplitStack


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
    with _SOURCE_CACHE_LOCK:
        case = adapter.load_case(config, spec)
    managed: ManagedGreptime | None = None
    split_stack = ManagedSplitStack(
        config.run_dir / "split",
        images=SplitStackImages(**protocol.split_stack_images.model_dump()),
    )
    report: dict[str, object] | None = None
    try:
        with _ENVIRONMENT_START_LOCK:
            managed = ManagedGreptime(Path(str(checkout["binary"])), config.run_dir)
            managed.start()
            # A split stack that will not start is an environment failure, not a
            # model failure: it must stop the case before any provider is called.
            split_stack.start()
        with (
            GreptimeClient(
                managed.endpoint,
                database=config.database,
                timeout=AGENT_QUERY_TIMEOUT_SECONDS,
            ) as client,
            ProtocolHttpClient(split_stack.prometheus_endpoint) as prometheus,
            ProtocolHttpClient(split_stack.loki_endpoint) as loki,
            ProtocolHttpClient(split_stack.tempo_otlp_endpoint) as tempo,
        ):
            server_status = client.status()
            client.create_database(config.database)
            assert_semantic_graph_isolated(client, config.database)
            empty = assert_semantic_graph_window_empty(client, case.input)
            source = adapter.source_audit(case, spec)
            # Logs and traces are teed as the same bytes; metrics reach
            # Prometheus through their own projection because it rejects the
            # source's unspecified-temporality sums and histograms.
            fanout = FanoutIngestClient(client, prometheus=prometheus, loki=loki, tempo=tempo)
            counts = adapter.ingest(fanout, case, spec)
            split_source = adapter.split_source(case, spec)
            metric_load = load_prometheus_metrics(
                prometheus,
                split_source.metric_series,
                split_source.metric_projection,
            )
            greptimedb_promql = _audit_greptimedb_promql(
                client,
                split_source.metric_series,
                window=(spec.normal_window[0], spec.abnormal_window[1]),
            )
            split_storage = audit_split_storage(
                prometheus_endpoint=split_stack.prometheus_endpoint,
                loki_endpoint=split_stack.loki_endpoint,
                tempo_endpoint=split_stack.tempo_endpoint,
                window=(spec.normal_window[0], spec.abnormal_window[1]),
                expected_metrics=split_source.metric_series,
                expected_logs=split_source.logs_by_table,
                expected_spans=split_source.spans,
                trace_scope_name=split_source.trace_scope_name,
                causal_services=_causal_services(spec),
                causal_trace_ids=_causal_trace_ids(spec, split_source.spans),
                mechanism=_split_mechanism(spec),
                ingestion_audit={**fanout.audit(), "metrics": metric_load},
            )
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
            gates = {
                **gates,
                "greptimedb_prometheus_api_agent_path_works": (greptimedb_promql["pass"] is True),
                "split_storage_equivalent": split_storage["pass"] is True,
            }
            gates["all_passed"] = all(value for key, value in gates.items() if key != "all_passed")
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
                "exclusive_split_stack": split_stack.metadata(),
                "source": source,
                "ingestion": stored,
                "semantic_coverage": coverage,
                "edge_equality": equality,
                "mechanism_evidence": mechanism,
                "greptimedb_promql": greptimedb_promql,
                "split_storage": split_storage,
                "no_model_gates": gates,
            }
            if gates["all_passed"] is not True:
                failed = {
                    key: value
                    for key, value in gates.items()
                    if key != "all_passed" and value is not True
                }
                detail = {
                    key: value for key, value in split_storage["gates"].items() if value is not True
                }
                raise ValueError(
                    f"transfer no-model gate failed for {spec.opaque_case_id}: "
                    f"{failed}; split storage: {detail or 'all split gates passed'}; "
                    f"GreptimeDB PromQL: {json.dumps(greptimedb_promql)}; "
                    f"split detail: {json.dumps(_split_failure_detail(split_storage))}"
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
                split_stack=split_stack,
            )
    finally:
        split_stack.stop()
        if managed is not None:
            managed.stop()
        if report is not None:
            exclusive = report.get("exclusive_instance")
            if isinstance(exclusive, dict):
                exclusive["process_stopped_by_command"] = (
                    managed is not None
                    and managed.process is not None
                    and managed.process.poll() is not None
                )


def _split_failure_detail(split_storage: dict[str, object]) -> dict[str, object]:
    """The measured numbers behind a split-storage gate failure."""
    metrics = _mapping(split_storage, "metrics")
    logs = _mapping(split_storage, "logs")
    traces = _mapping(split_storage, "traces")
    return {
        "metrics": {
            key: metrics.get(key)
            for key in ("source_samples", "stored_samples", "missing_samples", "extra_samples")
        },
        "logs": {
            table: {
                key: detail.get(key)
                for key in ("source_records", "stored_records", "missing_records", "extra_records")
            }
            for table, detail in _mapping(logs, "tables").items()
            if isinstance(detail, Mapping)
        },
        "traces": {
            key: traces.get(key)
            for key in (
                "fidelity_sample_traces",
                "missing_sample_spans",
                "extra_sample_spans",
                "absent_sample_traces",
                "returned_traces",
                "all_trace_ids_from_source",
                "all_trace_starts_in_source_window",
            )
        },
    }


def _audit_greptimedb_promql(
    client: GreptimeClient,
    series: Sequence[MetricSeries],
    *,
    window: tuple[int, int],
) -> dict[str, object]:
    """Exercises every Prometheus API operation the raw agent can call."""
    candidate = next(
        (
            (metric, timestamp_ms)
            for metric, samples, _labels in series
            for timestamp_ms, _value in samples
            if window[0] * 1000 <= timestamp_ms and timestamp_ms + 1000 < window[1] * 1000
        ),
        None,
    )
    if candidate is None:
        return {"pass": False, "error": "source has no metric sample in the case window"}

    metric, timestamp_ms = candidate
    start, end = (str(value) for value in window)
    evaluation_timestamp_ms = timestamp_ms + 1000
    evaluation_time = f"{evaluation_timestamp_ms / 1000:.3f}"
    calls = {
        "labels": {
            "operation": "labels",
            "match": metric,
            "start": start,
            "end": end,
            "max_items": 20,
        },
        "label_values": {
            "operation": "label_values",
            "label": "__name__",
            "match": metric,
            "start": start,
            "end": end,
            "max_items": 20,
        },
        "series": {
            "operation": "series",
            "match": metric,
            "start": start,
            "end": end,
            "max_items": 20,
        },
        "query": {
            "operation": "query",
            "query": metric,
            "time": evaluation_time,
            "max_items": 20,
        },
        "query_range": {
            "operation": "query_range",
            "query": metric,
            "start": evaluation_time,
            "end": evaluation_time,
            "step": "1s",
            "max_items": 20,
        },
    }
    gateway = QueryGateway(client, Visibility.RAW)
    operations: dict[str, object] = {}
    for operation, arguments in calls.items():
        try:
            result = gateway.execute_metrics(arguments)
        except (GreptimeError, NativeQueryError, QueryRejected) as error:
            operations[operation] = {"pass": False, "error": str(error)}
            continue
        rows = result.get("rows")
        returned = len(rows) if isinstance(rows, list) else 0
        operations[operation] = {
            "pass": _promql_result_matches_source(
                operation,
                result.get("columns"),
                rows,
                metric=metric,
                expected_timestamp_ms=evaluation_timestamp_ms,
            ),
            "returned_items": returned,
        }
    return {
        "metric": metric,
        "sample_timestamp_ms": timestamp_ms,
        "operations": operations,
        "pass": all(
            isinstance(result, Mapping) and result.get("pass") is True
            for result in operations.values()
        ),
    }


def _promql_result_matches_source(
    operation: str,
    columns: object,
    rows: object,
    *,
    metric: str,
    expected_timestamp_ms: int,
) -> bool:
    if not isinstance(rows, list) or not isinstance(columns, list):
        return False
    if operation == "labels":
        return ["__name__"] in rows
    if operation == "label_values":
        return [metric] in rows
    if "__name__" not in columns:
        return False
    name = columns.index("__name__")
    if operation == "series":
        return any(
            isinstance(row, list) and len(row) > name and row[name] == metric for row in rows
        )
    # query and query_range append timestamp and value after the labels. Taking
    # the timestamp by position survives a label that forces those names to be
    # escaped.
    time_index = len(columns) - 2
    expected_time = expected_timestamp_ms / 1000
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns) or time_index <= name:
            continue
        try:
            returned_time = float(row[time_index])
        except (TypeError, ValueError):
            continue
        if row[name] == metric and abs(returned_time - expected_time) <= 0.002:
            return True
    return False


def _causal_services(spec: TransferCaseSpec) -> frozenset[str]:
    """The services the case is scored on."""
    return frozenset(
        name
        for name in (spec.causal_component, spec.edge_source, spec.edge_destination)
        if isinstance(name, str) and name
    )


def _causal_trace_ids(spec: TransferCaseSpec, spans: list[object]) -> frozenset[str]:
    """The traces that carry the case's causal evidence.

    An edge case is scored on a directed call, so a trace merely mentioning one
    endpoint is not the evidence: the trace has to contain the Client span in
    the source service whose child Server span is in the destination service.
    Aiming the fidelity sample at "either service" left 2% of a delay case's
    selected traces without the edge at all.
    """
    if spec.edge_source and spec.edge_destination:
        by_trace: dict[str, list[object]] = {}
        for span in spans:
            by_trace.setdefault(str(span.trace_id), []).append(span)
        causal = set()
        for trace_id, trace_spans in by_trace.items():
            by_id = {str(span.span_id): span for span in trace_spans}
            for span in trace_spans:
                parent = by_id.get(str(span.parent_span_id))
                if (
                    str(span.service_name) == spec.edge_destination
                    and int(span.kind) == 2
                    and parent is not None
                    and str(parent.service_name) == spec.edge_source
                    and int(parent.kind) == 3
                ):
                    causal.add(trace_id)
                    break
        return frozenset(causal)
    services = _causal_services(spec)
    return frozenset(str(span.trace_id) for span in spans if str(span.service_name) in services)


def _split_mechanism(spec: TransferCaseSpec) -> dict[str, object] | None:
    """The case's frozen metric oracle, restated for the Prometheus replay."""
    evidence = spec.mechanism_evidence
    if evidence is None or evidence.source_table == "traces":
        return None
    return {
        "source_table": evidence.source_table,
        "identity_column": evidence.identity_column,
        "identity_value": evidence.identity_value,
        "threshold": evidence.threshold,
        "minimum_anomalous_observations": evidence.minimum_anomalous_observations,
        "normal_window": spec.normal_window,
        "abnormal_window": spec.abnormal_window,
    }


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
    run_split_agent_fn: RunAgent = run_split_agent,
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
    for cell in pending:
        item = execute_transfer_cell(
            protocol,
            prepared,
            cell,
            source_semantic_hash=str(expected_hash),
            run_agent_fn=run_agent_fn,
            run_split_agent_fn=run_split_agent_fn,
        )
        report_runs = report.get("runs")
        if not isinstance(report_runs, list):
            raise ValueError("transfer report runs are malformed")
        report_runs.append(item)
        report["execution"] = _execution_summary(report_runs, len(schedule))
        if on_update is not None:
            on_update(report)
    return report


def execute_transfer_cell(
    protocol: TransferProtocolFixture,
    prepared: PreparedTransferEnvironment,
    cell: Mapping[str, object],
    *,
    source_semantic_hash: str,
    run_agent_fn: RunAgent = run_agent,
    run_split_agent_fn: RunAgent = run_split_agent,
) -> dict[str, object]:
    if cell.get("case_id") != prepared.spec.opaque_case_id:
        raise ValueError("transfer cell and live environment case differ")
    models = {model.model: model for model in protocol.models}
    model = models.get(str(cell.get("model")))
    if model is None:
        raise ValueError("transfer cell model is outside the protocol")
    visibility = Visibility(str(cell["visibility"]))
    if visibility is Visibility.SPLIT_PILLARS:
        before = audit_transfer_trace_visibility(prepared)
        run, database_load = _run_split_cell(
            prepared,
            protocol,
            model,
            run_split_agent_fn=run_split_agent_fn,
        )
    else:
        run, database_load = _run_greptimedb_cell(
            prepared,
            protocol,
            model,
            visibility,
            run_agent_fn=run_agent_fn,
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
        "source_semantic_sha256": source_semantic_hash,
    }
    if visibility is Visibility.SPLIT_PILLARS:
        # Preserve paid output even if the environment failed during the investigation.
        audit_path = prepared.split_stack.run_dir / f"cell-{cell['cell_index']}-trace-health.json"
        write_json(audit_path, {"before": before, "item": item, "after": None})
        after = audit_transfer_trace_visibility(prepared)
        write_json(audit_path, {"before": before, "item": item, "after": after})
    return item


def audit_transfer_trace_visibility(prepared: PreparedTransferEnvironment) -> dict[str, object]:
    import httpx

    spec = prepared.spec
    adapter = _CASE_ADAPTERS[spec.causal_scope is CausalScope.INFRASTRUCTURE_NODE]
    source = adapter.split_source(prepared.case, spec)
    with httpx.Client(timeout=AGENT_QUERY_TIMEOUT_SECONDS, trust_env=False) as client:
        observed = _audit_traces(
            client,
            prepared.split_stack.tempo_endpoint,
            window=(spec.normal_window[0], spec.abnormal_window[1]),
            expected=source.spans,
            scope_name=source.trace_scope_name,
            causal_services=_causal_services(spec),
            causal_trace_ids=_causal_trace_ids(spec, source.spans),
            search_timeout=0,
        )
    expected = prepared.source_audit["split_storage"]["traces"]
    if (
        observed["sample_equal"] is not True
        or observed["source_window_traceql_search"] is not True
        or observed["causal_service_search"]["reachable"] is not True
        or observed["stored_sample_sha256"] != expected["fidelity_sample_sha256"]
    ):
        raise ValueError(f"live Tempo evidence changed for {spec.opaque_case_id}: {observed}")
    return observed


def validate_private_report(
    report: Mapping[str, object],
    protocol: TransferProtocolFixture,
    protocol_path: Path,
    selection: TransferCohort,
    *,
    require_complete: bool = False,
    allow_recorded_concurrency_deviation: bool = False,
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
    runs = _mapping_list(report, "runs")
    if len(runs) > len(expected_schedule):
        raise ValueError("transfer report has more runs than scheduled")
    for index, item in enumerate(runs):
        validate_transfer_run_item(
            item,
            expected_schedule[index],
            protocol,
            specs[str(item.get("case_id"))],
            expected_source_hashes[str(item.get("case_id"))],
            allow_recorded_concurrency_deviation=allow_recorded_concurrency_deviation,
        )
    if report.get("execution") != _execution_summary(runs, len(expected_schedule)):
        raise ValueError("transfer execution summary drifted")
    if require_complete and not _mapping(report, "execution").get("complete"):
        raise ValueError("transfer report is incomplete")


def validate_transfer_run_item(
    item: Mapping[str, object],
    expected: Mapping[str, object],
    protocol: TransferProtocolFixture,
    spec: TransferCaseSpec,
    expected_source_hash: str,
    *,
    allow_recorded_concurrency_deviation: bool = False,
) -> None:
    if any(item.get(key) != value for key, value in expected.items()):
        raise ValueError("transfer completed run does not match its scheduled cell")
    concurrency = item.get("provider_concurrency_limit", protocol.max_parallel_runs_per_provider)
    limit = (
        # Historical exports retain deviations; execution uses the frozen limit.
        protocol.parallel_runs
        if allow_recorded_concurrency_deviation
        else protocol.max_parallel_runs_per_provider
    )
    if type(concurrency) is not int or not 1 <= concurrency <= limit:
        raise ValueError("transfer provider concurrency override is invalid")
    run = AgentRun.model_validate(item.get("run"))
    models = {model.model: model for model in protocol.models}
    model = models[str(item["model"])]
    evaluated = evaluate_transfer_run(
        run,
        spec,
        expected_model=model.model,
        expected_transport=model.api_transport,
        expected_reasoning_effort=model.reasoning_effort,
        expected_max_output_tokens=model.max_output_tokens,
        max_tool_calls=protocol.max_tool_calls,
    )
    if item.get("evaluation") != evaluated.model_dump(mode="json"):
        raise ValueError("transfer stored evaluation does not rescore")
    DatabaseLoad.model_validate(item.get("database_load"))
    if item.get("source_semantic_sha256") != expected_source_hash:
        raise ValueError("transfer completed cell source binding drifted")


def source_semantic_sha256(audit: Mapping[str, object]) -> str:
    source_audit = dict(audit)
    split_stack = source_audit.get("exclusive_split_stack")
    if isinstance(split_stack, Mapping):
        source_audit["exclusive_split_stack"] = {
            key: value for key, value in split_stack.items() if key != "ports"
        }
    return canonical_sha256(_stable(source_audit))


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _run_greptimedb_cell(
    prepared: PreparedTransferEnvironment,
    protocol: TransferProtocolFixture,
    model: object,
    visibility: Visibility,
    *,
    run_agent_fn: RunAgent,
) -> tuple[AgentRun, DatabaseLoad]:
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
    return run, database_load


def _run_split_cell(
    prepared: PreparedTransferEnvironment,
    protocol: TransferProtocolFixture,
    model: object,
    *,
    run_split_agent_fn: RunAgent,
) -> tuple[AgentRun, DatabaseLoad]:
    """One split-pillars cell, measured on its own gateway.

    `rows_returned` stays null here: a Prometheus sample, a Loki entry and a
    Tempo trace are not the same unit as a GreptimeDB row, so summing them into
    the registered load endpoint would compare incommensurable counts.
    """
    stack = prepared.split_stack
    with (
        SplitQueryGateway(
            prometheus_endpoint=stack.prometheus_endpoint,
            loki_endpoint=stack.loki_endpoint,
            tempo_endpoint=stack.tempo_endpoint,
            timeout=AGENT_QUERY_TIMEOUT_SECONDS,
        ) as gateway,
        gateway.measure_query_load() as database_load,
    ):
        try:
            run = run_split_agent_fn(
                gateway,
                prepared.case.input,
                model=model.model,
                api_transport=model.api_transport,
                reasoning_effort=model.reasoning_effort,
                max_tool_calls=protocol.max_tool_calls,
                max_turns=protocol.max_turns,
                max_output_tokens=model.max_output_tokens,
            )
        except Exception as error:
            run = _failed_run(
                Visibility.SPLIT_PILLARS,
                model.model,
                model.api_transport,
                model.reasoning_effort,
                model.max_output_tokens,
                str(error),
            )
    return run, database_load


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
