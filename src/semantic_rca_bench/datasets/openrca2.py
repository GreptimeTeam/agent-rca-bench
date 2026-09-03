from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from semantic_rca_bench.contracts import (
    CaseInput,
    FaultCategory,
    GroundTruth,
    IngestCounts,
    OpenRCA2Case,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.protocols.loki import LogRecord, write_logs
from semantic_rca_bench.protocols.otlp import (
    HistogramMetricPoint,
    NumberMetricPoint,
    OtlpMetricWriter,
    OtlpTraceWriter,
    TraceSpan,
)
from semantic_rca_bench.protocols.prometheus import prometheus_metric_name

REPOSITORY_ID = "anon-ops/ops-lite"
SOURCE_REVISION = "9ac09981c08ab02a0b923eab7830d778934851a8"
DATASET_REVISION = f"ops-lite@{SOURCE_REVISION}"
DEFAULT_CASE = "hs1-geo-pod-failure-drdmjj"
# GreptimeDB creates these itself: the metric engine's physical table and the
# trace engine's derived operation and service tables. They carry no semantic
# options, so the leaked-label roster has to allow them by name.
ENGINE_MANAGED_TABLES = frozenset(
    {"greptime_physical_table", "traces_operations", "traces_services"}
)
PERIODS = ("normal", "abnormal")
CASE_FILES = tuple(
    f"{period}_{suffix}"
    for period in PERIODS
    for suffix in (
        "logs.parquet",
        "metrics.parquet",
        "metrics_sum.parquet",
        "metrics_histogram.parquet",
        "traces.parquet",
    )
) + (
    "injection.json",
    "causal_graph.json",
    "env.json",
    "conclusion.parquet",
)

_SPAN_KINDS = {
    "Unspecified": Span.SPAN_KIND_UNSPECIFIED,
    "Internal": Span.SPAN_KIND_INTERNAL,
    "Server": Span.SPAN_KIND_SERVER,
    "Client": Span.SPAN_KIND_CLIENT,
    "Producer": Span.SPAN_KIND_PRODUCER,
    "Consumer": Span.SPAN_KIND_CONSUMER,
}
_STATUS_CODES = {
    "Unset": Status.STATUS_CODE_UNSET,
    "Ok": Status.STATUS_CODE_OK,
    "Error": Status.STATUS_CODE_ERROR,
}


class OpenRCA2Error(RuntimeError):
    pass


class OpenRCA2Repository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_case(
        self,
        case_name: str = DEFAULT_CASE,
        *,
        require_observable_alert: bool = True,
    ) -> OpenRCA2Case:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", case_name):
            raise OpenRCA2Error(f"invalid ops-lite case name: {case_name}")
        manifest_path = self._download("manifest.jsonl")
        root = self.cache_dir / "cases" / case_name
        for filename in CASE_FILES:
            self._download(f"cases/{case_name}/{filename}")
        return _load_case(
            root,
            manifest_path,
            require_observable_alert=require_observable_alert,
        )

    def _download(self, filename: str) -> Path:
        target = self.cache_dir / filename
        if target.is_file() and target.stat().st_size:
            return target
        downloaded = hf_hub_download(
            repo_id=REPOSITORY_ID,
            repo_type="dataset",
            revision=SOURCE_REVISION,
            filename=filename,
            local_dir=self.cache_dir,
        )
        return Path(downloaded)


def _load_case(
    root: Path,
    manifest_path: Path,
    *,
    require_observable_alert: bool = True,
) -> OpenRCA2Case:
    manifest = _manifest_entry(manifest_path, root.name)
    injection = _read_json(root / "injection.json")
    env = _read_json(root / "env.json")
    primary_kind = str(manifest.get("primary_kind") or "")
    if manifest.get("hybrid") is not False or not primary_kind:
        raise OpenRCA2Error(f"{root.name} is not a single-fault case")
    if injection.get("state") != "inject_success":
        raise OpenRCA2Error(f"{root.name} does not contain a successful injection")
    if injection.get("fault_type") != primary_kind:
        raise OpenRCA2Error(f"{root.name} manifest and injection fault types disagree")

    roots = manifest.get("root_services")
    if not isinstance(roots, list) or len(roots) != 1:
        raise OpenRCA2Error(f"{root.name} does not have one root service")
    injection_services = _injection_ground_truth_services(injection)
    endpoint = _injection_fault_endpoint(injection)
    expected_services = set(endpoint) if endpoint is not None else {str(roots[0])}
    root_matches = endpoint is None or str(roots[0]) == endpoint[0]
    if not root_matches or injection_services != expected_services:
        raise OpenRCA2Error(
            f"{root.name} manifest roots {roots} disagree with injection ground truth "
            f"{sorted(injection_services)}"
        )
    normal_start = _env_epoch(env, "NORMAL_START")
    normal_end = _env_epoch(env, "NORMAL_END")
    abnormal_start = _env_epoch(env, "ABNORMAL_START")
    abnormal_end = _env_epoch(env, "ABNORMAL_END")
    if not normal_start < normal_end == abnormal_start < abnormal_end:
        raise OpenRCA2Error(f"{root.name} does not have contiguous normal/abnormal windows")
    inject_time = int(datetime.fromisoformat(str(injection["start_time"])).timestamp())
    if inject_time != abnormal_start:
        raise OpenRCA2Error(f"{root.name} injection time disagrees with abnormal window")

    system = _system_name(str(manifest.get("system") or ""))
    return OpenRCA2Case(
        source_case=root.name,
        dataset=DATASET_REVISION,
        system=system,
        root=root,
        input=CaseInput(
            case_token=root.name,
            time_start=normal_start,
            time_end=abnormal_end,
            alert_time=abnormal_start,
            alert_text=_alert_text(
                root / "conclusion.parquet",
                system,
                required=require_observable_alert,
            ),
            fault_taxonomy=_fault_taxonomy(manifest_path, str(manifest["system"])),
        ),
        ground_truth=GroundTruth(
            causal_component=str(roots[0]),
            fault_type=primary_kind,
            fault_category=_fault_category(primary_kind),
            inject_time=inject_time,
        ),
        gauge_paths=tuple(root / f"{period}_metrics.parquet" for period in PERIODS),
        sum_paths=tuple(root / f"{period}_metrics_sum.parquet" for period in PERIODS),
        histogram_paths=tuple(root / f"{period}_metrics_histogram.parquet" for period in PERIODS),
        logs_paths=tuple(root / f"{period}_logs.parquet" for period in PERIODS),
        traces_paths=tuple(root / f"{period}_traces.parquet" for period in PERIODS),
        injection_path=root / "injection.json",
        causal_graph_path=root / "causal_graph.json",
    )


def ingest_case(client: GreptimeClient, case: OpenRCA2Case) -> IngestCounts:
    client.create_database(case.input.database)
    audit = _metric_sample_audit(case)
    metric_writer = OtlpMetricWriter(
        client,
        case.input.database,
        scope_name="openrca2-replay",
        scope_version=SOURCE_REVISION,
    )
    counts = IngestCounts(
        metric_source_rows=audit["source_rows"],
        metric_unique_samples=audit["unique_samples"],
        metric_duplicate_samples=audit["duplicate_samples"],
        metric_conflicting_timestamps=audit["conflicting_timestamps"],
        metric_protocol_rows=audit["protocol_rows"],
    )
    counts.metrics_samples += metric_writer.write_gauges(_iter_number_metrics(case.gauge_paths))
    counts.metrics_samples += metric_writer.write_sums(_iter_number_metrics(case.sum_paths))
    counts.metrics_samples += metric_writer.write_histograms(_iter_histograms(case.histogram_paths))
    counts.rejected_metric_points = metric_writer.rejected_data_points
    counts.log_records = write_logs(
        client,
        case.input.database,
        _iter_logs(case.logs_paths),
    )

    trace_writer = OtlpTraceWriter(client, case.input.database)
    counts.trace_spans = trace_writer.write(_iter_traces(case.traces_paths))
    counts.rejected_trace_spans = trace_writer.rejected_spans
    counts.remapped_trace_ids = trace_writer.stats.remapped_trace_ids
    counts.remapped_span_ids = trace_writer.stats.remapped_span_ids
    return counts


def source_audit(case: OpenRCA2Case) -> dict[str, object]:
    metric_audit = _metric_sample_audit(case)
    path_rows = {
        path.name: pq.ParquetFile(path).metadata.num_rows
        for path in (
            *case.gauge_paths,
            *case.sum_paths,
            *case.histogram_paths,
            *case.logs_paths,
            *case.traces_paths,
        )
    }
    trace_kinds: set[str] = set()
    trace_statuses: set[str] = set()
    for row in _iter_rows(case.traces_paths):
        trace_kinds.add(str(row["attr.span_kind"]))
        trace_statuses.add(str(row["attr.status_code"]))
    unknown_kinds = trace_kinds - _SPAN_KINDS.keys()
    unknown_statuses = trace_statuses - _STATUS_CODES.keys()
    if unknown_kinds or unknown_statuses:
        raise OpenRCA2Error(
            f"unknown trace semantics: kinds={sorted(unknown_kinds)}, "
            f"statuses={sorted(unknown_statuses)}"
        )
    causal_graph = _read_json(case.causal_graph_path)
    return {
        "window_rows": path_rows,
        "timestamp_unit": "nanoseconds",
        "trace_duration_unit": "nanoseconds",
        "trace_span_kinds": sorted(trace_kinds),
        "trace_status_codes": sorted(trace_statuses),
        "metric_aggregation_types": ["Gauge", "Sum", "Histogram"],
        "metric_source_rows": metric_audit["source_rows"],
        "metric_unique_identities": metric_audit["unique_samples"],
        "metric_duplicate_rows": metric_audit["duplicate_samples"],
        "metric_conflicting_identities": metric_audit["conflicting_timestamps"],
        "metric_protocol_rows_after_primary_key_dedup": metric_audit["protocol_rows"],
        "metric_type_name_collisions": metric_audit["type_name_collisions"],
        "sum_temporality_available": False,
        "sum_monotonicity_available": False,
        "sum_protocol_representation": (
            "AGGREGATION_TEMPORALITY_UNSPECIFIED with is_monotonic=false; "
            "OTLP Sum has no unknown monotonicity value"
        ),
        "histogram_bucket_boundaries_available": False,
        "histogram_min_max_stored_by_greptimedb": False,
        "histogram_protocol_representation": (
            "one exhaustive +Inf bucket plus source count/sum/min/max"
        ),
        "attribute_scope_available": False,
        "attribute_scope_representation": (
            "service_name is an OTLP resource attribute; attr.* columns remain signal attributes"
        ),
        "normal_window": _window_summary(case, "normal"),
        "abnormal_window": _window_summary(case, "abnormal"),
        "graph_applicability": "relational",
        "reference_causal_graph": {
            "validation_only": True,
            "ingested": False,
            "node_count": len(causal_graph.get("nodes") or []),
            "edge_count": len(causal_graph.get("edges") or []),
        },
        "source_data_modified": False,
    }


def validate_ingest(
    client: GreptimeClient,
    case: OpenRCA2Case,
    counts: IngestCounts,
) -> dict[str, object]:
    database = case.input.database.replace("'", "''")
    semantics = client.query(
        f"""
        SELECT table_name, signal_type, source
        FROM information_schema.table_semantics
        WHERE table_schema = '{database}'
        ORDER BY table_name
        """,
        max_rows=None,
    )
    metric_tables = [
        str(row[0])
        for row in semantics.rows
        if row[1] == "metric" and row[0] != "greptime_otel_resource_info"
    ]
    resource_descriptor_rows = (
        _table_count(client, "greptime_otel_resource_info")
        if any(row[0] == "greptime_otel_resource_info" for row in semantics.rows)
        else 0
    )
    metric_rows = sum(_table_count(client, table) for table in metric_tables)
    trace_rows = _table_count(client, "traces")
    log_tables = [str(row[0]) for row in semantics.rows if row[1] == "log"]
    log_rows = sum(_table_count(client, table) for table in log_tables)
    # Measured, not declared: the reference causal graph and the answer key are
    # labels, and only a post-ingestion query can show whether one reached the
    # database. The roster comes from `tables`, not `table_semantics`: a plain
    # CREATE TABLE carries no `greptime.semantic.*` options and never appears in
    # the semantic view, so enumerating that view would miss the exact shape a
    # leaked label takes while the agent can still query it.
    stored_tables = client.query(
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{database}'",
        max_rows=None,
    )
    if stored_tables.truncated:
        raise OpenRCA2Error("stored table roster was truncated")
    expected_tables = _expected_tables(case)
    unexpected_tables = sorted(
        str(row[0]) for row in stored_tables.rows if str(row[0]) not in expected_tables
    )
    derived_calls = _derived_service_calls(client, case.input)
    expected_fault_call = _fault_endpoint_call(case.injection_path)
    database_counts = {
        "metric_rows": metric_rows,
        "log_records": log_rows,
        "trace_spans": trace_rows,
    }
    expected_counts = {
        "metric_rows": counts.metric_protocol_rows,
        "log_records": counts.log_records,
        "trace_spans": counts.trace_spans,
    }
    return {
        "database_counts": database_counts,
        "expected_protocol_counts": expected_counts,
        "protocol_row_counts_match": database_counts == expected_counts,
        "metric_data_points_accepted": counts.metrics_samples,
        "metric_data_points_rejected": counts.rejected_metric_points,
        "trace_spans_rejected": counts.rejected_trace_spans,
        "metric_table_count": len(metric_tables),
        "resource_descriptor_rows": resource_descriptor_rows,
        "semantic_signal_sources": [
            {"signal_type": signal_type, "source": source}
            for signal_type, source in sorted(
                {(str(row[1]), str(row[2])) for row in semantics.rows}
            )
        ],
        "derived_service_calls": [list(pair) for pair in sorted(derived_calls)],
        "fault_endpoint_call": list(expected_fault_call) if expected_fault_call else None,
        "fault_endpoint_call_found": (
            expected_fault_call in derived_calls if expected_fault_call else None
        ),
        "derived_calls_nonempty": bool(derived_calls),
        "unexpected_tables": unexpected_tables,
        "reference_labels_not_ingested": not unexpected_tables,
    }


def _expected_tables(case: OpenRCA2Case) -> set[str]:
    """Every table the replay is allowed to have written.

    Derived from the case's own metric names rather than from the database's
    `table_semantics`: that view lists whatever carries `greptime.semantic.*`
    options, so a label written through the same OTLP or Loki path the replay
    uses would appear there and clear itself. OTLP names a table after the
    metric with dots replaced, and expands a histogram into bucket, count, and
    sum tables.
    """
    expected = {"traces", "logs", "greptime_otel_resource_info", *ENGINE_MANAGED_TABLES}
    for point in _iter_number_metrics(case.gauge_paths + case.sum_paths):
        expected.add(prometheus_metric_name(point.name))
    for histogram in _iter_histograms(case.histogram_paths):
        base = prometheus_metric_name(histogram.name)
        expected |= {f"{base}_bucket", f"{base}_count", f"{base}_sum"}
    return expected


def _iter_number_metrics(paths: tuple[Path, ...]) -> Iterator[NumberMetricPoint]:
    for row in _iter_rows(paths):
        value = float(row["value"])
        if not math.isfinite(value):
            raise OpenRCA2Error("non-finite metric value")
        yield NumberMetricPoint(
            name=str(row["metric"]),
            time_unix_nano=int(row["time"]),
            value=value,
            service_name=_optional_string(row.get("service_name")),
            attributes=_attributes(row),
        )


def _iter_histograms(paths: tuple[Path, ...]) -> Iterator[HistogramMetricPoint]:
    for row in _iter_rows(paths):
        raw_count = float(row["count"])
        if not math.isfinite(raw_count) or raw_count < 0 or not raw_count.is_integer():
            raise OpenRCA2Error(f"invalid histogram count: {raw_count}")
        yield HistogramMetricPoint(
            name=str(row["metric"]),
            time_unix_nano=int(row["time"]),
            count=int(raw_count),
            sum=_optional_finite(row.get("sum"), "histogram sum"),
            min=_optional_finite(row.get("min"), "histogram min"),
            max=_optional_finite(row.get("max"), "histogram max"),
            service_name=_optional_string(row.get("service_name")),
            attributes=_attributes(row),
        )


def _iter_logs(paths: tuple[Path, ...]) -> Iterator[LogRecord]:
    for row in _iter_rows(paths):
        labels = {
            "service_name": str(row["service_name"]),
            **{key: str(value) for key, value in _attributes(row).items()},
        }
        for key in ("trace_id", "span_id", "level"):
            if row.get(key) not in (None, ""):
                labels[key] = str(row[key])
        yield LogRecord(
            timestamp=int(row["time"]),
            message=str(row.get("message") or ""),
            labels=labels,
        )


def _iter_traces(paths: tuple[Path, ...]) -> Iterator[TraceSpan]:
    for row in _iter_rows(paths):
        kind_name = str(row["attr.span_kind"])
        status_name = str(row["attr.status_code"])
        try:
            kind = _SPAN_KINDS[kind_name]
            status = _STATUS_CODES[status_name]
        except KeyError as error:
            raise OpenRCA2Error(f"unknown trace enum: {error.args[0]}") from error
        start = int(row["time"])
        duration = int(row["duration"])
        if duration < 0:
            raise OpenRCA2Error(f"negative trace duration: {duration}")
        service_name = _optional_string(row.get("service_name"))
        if service_name is None:
            raise OpenRCA2Error("trace span has no service_name")
        yield TraceSpan(
            trace_id=str(row["trace_id"]),
            span_id=str(row["span_id"]),
            parent_span_id=str(row.get("parent_span_id") or ""),
            name=str(row["span_name"]),
            kind=kind,
            start_time_unix_nano=start,
            end_time_unix_nano=start + duration,
            service_name=service_name,
            status_code=status,
            attributes=_attributes(
                row,
                excluded={"span_kind", "status_code"},
            ),
            scope_name="openrca2-replay",
            scope_version=SOURCE_REVISION,
        )


def _metric_sample_audit(case: OpenRCA2Case) -> dict[str, Any]:
    values: dict[tuple[object, ...], set[tuple[object, ...]]] = defaultdict(set)
    protocol_keys: set[tuple[object, ...]] = set()
    names_by_kind: dict[str, set[str]] = {"gauge": set(), "sum": set(), "histogram": set()}
    source_rows = 0
    for kind, points in (
        ("gauge", _iter_number_metrics(case.gauge_paths)),
        ("sum", _iter_number_metrics(case.sum_paths)),
        ("histogram", _iter_histograms(case.histogram_paths)),
    ):
        for point in points:
            source_rows += 1
            names_by_kind[kind].add(point.name)
            identity = (
                prometheus_metric_name(point.name),
                point.time_unix_nano,
                point.service_name,
                tuple(sorted(point.attributes.items())),
            )
            if isinstance(point, HistogramMetricPoint):
                key = ("histogram", *identity)
                value = (point.count, point.sum, point.min, point.max)
                protocol_keys.add((f"{identity[0]}_bucket", *identity[1:]))
                protocol_keys.add((f"{identity[0]}_count", *identity[1:]))
                if point.sum is not None:
                    protocol_keys.add((f"{identity[0]}_sum", *identity[1:]))
            else:
                key = ("number", *identity)
                value = (point.value,)
                protocol_keys.add(identity)
            values[key].add(value)
    unique_samples = len(values)
    return {
        "source_rows": source_rows,
        "unique_samples": unique_samples,
        "duplicate_samples": source_rows - unique_samples,
        "conflicting_timestamps": sum(len(observed) > 1 for observed in values.values()),
        "protocol_rows": len(protocol_keys),
        "type_name_collisions": sorted(
            (names_by_kind["gauge"] & names_by_kind["sum"])
            | (names_by_kind["gauge"] & names_by_kind["histogram"])
            | (names_by_kind["sum"] & names_by_kind["histogram"])
        ),
    }


def _attributes(
    row: Mapping[str, object],
    *,
    excluded: set[str] | None = None,
) -> dict[str, object]:
    excluded = excluded or set()
    return {
        key.removeprefix("attr."): value
        for key, value in row.items()
        if key.startswith("attr.")
        and key.removeprefix("attr.") not in excluded
        and value not in (None, "")
    }


def _iter_rows(paths: tuple[Path, ...]) -> Iterator[dict[str, Any]]:
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=10_000):
            names = batch.schema.names
            for index in range(batch.num_rows):
                row: dict[str, Any] = {}
                for column_index, name in enumerate(names):
                    scalar = batch.column(column_index)[index]
                    if not scalar.is_valid:
                        row[name] = None
                    elif name == "time":
                        row[name] = scalar.value
                    else:
                        row[name] = scalar.as_py()
                yield row


def _manifest_entry(path: Path, case_name: str) -> dict[str, object]:
    for line in path.read_text().splitlines():
        value = json.loads(line)
        if isinstance(value, dict) and value.get("name") == case_name:
            return value
    raise OpenRCA2Error(f"case not found in manifest: {case_name}")


def _fault_taxonomy(path: Path, system: str) -> list[str]:
    values = set()
    for line in path.read_text().splitlines():
        item = json.loads(line)
        if (
            isinstance(item, dict)
            and item.get("system") == system
            and item.get("hybrid") is False
            and item.get("primary_kind")
        ):
            values.add(str(item["primary_kind"]))
    if not values:
        raise OpenRCA2Error(f"empty fault taxonomy for system: {system}")
    return sorted(values)


def _alert_text(path: Path, system: str, *, required: bool = True) -> str | None:
    table = pq.read_table(path, columns=["Issues"])
    issues = set()
    for raw in table["Issues"].to_pylist():
        if not raw:
            continue
        value = json.loads(str(raw))
        if isinstance(value, dict):
            issues.update(str(key) for key in value)
    if not issues:
        if required:
            raise OpenRCA2Error("case conclusion has no observable alert condition")
        return None
    return f"{system} alert: {', '.join(sorted(issues))}"


def _window_summary(case: OpenRCA2Case, period: str) -> dict[str, object]:
    if period == "normal":
        paths = (
            case.gauge_paths[0],
            case.sum_paths[0],
            case.histogram_paths[0],
            case.logs_paths[0],
            case.traces_paths[0],
        )
        expected_start = case.input.time_start * 1_000_000_000
        expected_end = case.input.alert_time * 1_000_000_000
    else:
        paths = (
            case.gauge_paths[1],
            case.sum_paths[1],
            case.histogram_paths[1],
            case.logs_paths[1],
            case.traces_paths[1],
        )
        expected_start = case.input.alert_time * 1_000_000_000
        expected_end = case.input.time_end * 1_000_000_000
    observed = []
    for path in paths:
        column = pq.read_table(path, columns=["time"])["time"]
        if len(column) == 0:
            continue
        observed.extend((pc.min(column).value, pc.max(column).value))
    minimum = min(observed) if observed else None
    maximum = max(observed) if observed else None
    in_window = (
        minimum is not None
        and maximum is not None
        and minimum >= expected_start
        and maximum < expected_end
    )
    if observed and not in_window:
        raise OpenRCA2Error(f"{period} telemetry falls outside its declared window")
    return {
        "expected_start_ns": expected_start,
        "expected_end_ns": expected_end,
        "observed_min_ns": minimum,
        "observed_max_ns": maximum,
        "all_rows_in_declared_window": in_window,
    }


def _fault_endpoint_call(path: Path) -> tuple[str, str] | None:
    return _injection_fault_endpoint(_read_json(path))


def _injection_fault_endpoint(injection: Mapping[str, object]) -> tuple[str, str] | None:
    configs = injection.get("engine_config")
    if not isinstance(configs, list) or len(configs) != 1:
        raise OpenRCA2Error("expected one injection engine config")
    config = configs[0]
    if not isinstance(config, Mapping):
        raise OpenRCA2Error("invalid injection engine config")
    if config.get("direction") != "to":
        return None
    if not config.get("app") or not config.get("target_service"):
        raise OpenRCA2Error("directed injection has no service endpoints")
    return str(config["app"]), str(config["target_service"])


def _injection_ground_truth_services(injection: Mapping[str, object]) -> set[str]:
    ground_truth = injection.get("ground_truth")
    if not isinstance(ground_truth, list) or not ground_truth:
        raise OpenRCA2Error("injection has no service ground truth")
    services: set[str] = set()
    for item in ground_truth:
        if not isinstance(item, Mapping):
            raise OpenRCA2Error("invalid injection ground truth entry")
        values = item.get("service")
        if not isinstance(values, list):
            raise OpenRCA2Error("injection ground truth has no service list")
        services.update(str(value) for value in values if value not in (None, ""))
    if not services:
        raise OpenRCA2Error("injection service ground truth is empty")
    return services


def _derived_service_calls(client: GreptimeClient, case: CaseInput) -> set[tuple[str, str]]:
    start = datetime.fromtimestamp(case.time_start, UTC).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(case.time_end, UTC).strftime("%Y-%m-%d %H:%M:%S")
    result = client.query(
        f"""
        SELECT DISTINCT src_id, dst_id
        FROM greptime_private.semantic_relationships
        WHERE observed_at >= '{start}' AND observed_at < '{end}'
          AND src_type = 'service' AND dst_type = 'service' AND rel_type = 'calls'
        ORDER BY src_id, dst_id
        """,
        max_rows=None,
    )
    return {(str(row[0]), str(row[1])) for row in result.rows}


def _table_count(client: GreptimeClient, table: str) -> int:
    identifier = table.replace('"', '""')
    result = client.query(f'SELECT COUNT(*) FROM "{identifier}"')
    return int(result.rows[0][0])


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise OpenRCA2Error(f"expected JSON object in {path}")
    return value


def _env_epoch(env: Mapping[str, object], key: str) -> int:
    try:
        return int(str(env[key]))
    except (KeyError, ValueError) as error:
        raise OpenRCA2Error(f"invalid {key} in env.json") from error


def _optional_string(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_finite(value: object, field: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise OpenRCA2Error(f"non-finite {field}")
    return number


def _fault_category(fault_type: str) -> FaultCategory:
    normalized = fault_type.lower()
    if "delay" in normalized or "latency" in normalized or "time" in normalized:
        return FaultCategory.DELAY
    if "loss" in normalized or "abort" in normalized or "disconnect" in normalized:
        return FaultCategory.LOSS
    if "cpu" in normalized:
        return FaultCategory.CPU
    if "memory" in normalized or "oom" in normalized or "gc" in normalized:
        return FaultCategory.MEMORY
    if "disk" in normalized or "io" in normalized:
        return FaultCategory.DISK
    return FaultCategory.OTHER


def _system_name(system: str) -> str:
    names = {
        "otel-demo": "OpenTelemetry Demo",
        "ts": "Train-Ticket",
        "hs": "Hotel Reservation",
    }
    try:
        return names[system]
    except KeyError as error:
        raise OpenRCA2Error(f"unknown ops-lite system: {system}") from error
