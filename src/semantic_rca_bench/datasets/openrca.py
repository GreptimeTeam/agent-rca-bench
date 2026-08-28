from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from semantic_rca_bench.contracts import (
    CaseInput,
    FaultCategory,
    GroundTruth,
    IngestCounts,
    OpenRCACase,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.protocols.loki import LogRecord, write_logs
from semantic_rca_bench.protocols.otlp import OtlpTraceWriter, TraceSpan
from semantic_rca_bench.protocols.prometheus import (
    prometheus_metric_name,
    write_series_batch,
)

DATASET_REVISION = "OpenRCA-1.0"
SOURCE_REVISION = "c1bd4af7f635171a1c31cdd567c07d698dff6abc"
MIRROR_REVISION = "07714872ea2cec77c13f9dec17a688e9df9621d1"
MIRROR_BASE_URL = f"https://huggingface.co/datasets/tracer-cloud/opensre/resolve/{MIRROR_REVISION}"
TIMEZONE = ZoneInfo("Asia/Shanghai")
APP_METRICS = ("rr", "sr", "cnt", "mrt")
DEFAULT_BANK_CASE = "task_6@2021-03-04T18:00"
DEFAULT_MARKET_CASE = "Market/cloudbed-1@2022-03-21T03:30"
DEFAULT_TELECOM_CASE = "Telecom@2020-05-27T05:00"
MARKET_METRIC_FILES = (
    "metric_container.csv",
    "metric_mesh.csv",
    "metric_node.csv",
    "metric_runtime.csv",
    "metric_service.csv",
)
MARKET_LOG_FILES = ("log_proxy.csv", "log_service.csv")
TELECOM_METRIC_FILES = (
    "metric_app.csv",
    "metric_container.csv",
    "metric_middleware.csv",
    "metric_node.csv",
    "metric_service.csv",
)


class OpenRCAError(RuntimeError):
    pass


class OpenRCARepository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_bank_case(self, case_id: str) -> OpenRCACase:
        bank = self.cache_dir / "Bank"
        query_path = bank / "query.csv"
        record_path = bank / "record.csv"
        self._download("Bank/query.csv", query_path)
        self._download("Bank/record.csv", record_path)
        task_id, expected_start = _parse_case_id(case_id)
        task = _find_task(query_path, task_id, expected_start)
        time_start, time_end = _task_window(task["instruction"])
        records = _ground_truth_records(record_path, time_start, time_end)
        if len(records) != 1:
            raise OpenRCAError(
                f"{case_id} has {len(records)} root-cause records in its task window"
            )
        truth = records[0]
        day = datetime.fromtimestamp(time_start, TIMEZONE).strftime("%Y_%m_%d")
        root = bank / "telemetry" / day
        paths = {
            "metric_app": root / "metric" / "metric_app.csv",
            "metric_container": root / "metric" / "metric_container.csv",
            "logs": root / "log" / "log_service.csv",
            "traces": root / "trace" / "trace_span.csv",
        }
        for name, path in paths.items():
            self._download(f"Bank/telemetry/{day}/{_relative_telemetry_path(name)}", path)
        return OpenRCACase(
            source_case=f"Bank/{case_id}",
            dataset=DATASET_REVISION,
            system="OpenRCA Bank",
            root=root,
            input=CaseInput(
                case_token=f"Bank/{case_id}",
                time_start=time_start,
                time_end=time_end,
                alert_time=time_end,
                alert_text=task["instruction"],
                fault_taxonomy=_fault_taxonomy(record_path),
            ),
            ground_truth=GroundTruth(
                affected_component=truth["component"],
                fault_type=truth["reason"],
                fault_category=_fault_category(truth["reason"]),
                inject_time=int(float(truth["timestamp"])),
            ),
            variant="bank",
            metric_paths=(paths["metric_app"], paths["metric_container"]),
            log_paths=(paths["logs"],),
            metric_app_path=paths["metric_app"],
            metric_container_path=paths["metric_container"],
            logs_path=paths["logs"],
            traces_path=paths["traces"],
        )

    def fetch_case(self, case_id: str) -> OpenRCACase:
        if case_id.startswith("Market/"):
            return self._fetch_market_case(case_id)
        if case_id.startswith("Telecom@"):
            return self._fetch_telecom_case(case_id)
        if case_id.startswith("Bank/"):
            case_id = case_id.removeprefix("Bank/")
        return self.fetch_bank_case(case_id)

    def _fetch_market_case(self, case_id: str) -> OpenRCACase:
        system, start = _parse_legacy_case_id(case_id)
        if system not in {"Market/cloudbed-1", "Market/cloudbed-2"}:
            raise OpenRCAError(f"unsupported Market system: {system}")
        return self._fetch_legacy_case(
            case_id,
            system=system,
            start=start,
            variant="market",
            metric_files=MARKET_METRIC_FILES,
            log_files=MARKET_LOG_FILES,
        )

    def _fetch_telecom_case(self, case_id: str) -> OpenRCACase:
        system, start = _parse_legacy_case_id(case_id)
        if system != "Telecom":
            raise OpenRCAError(f"unsupported Telecom system: {system}")
        return self._fetch_legacy_case(
            case_id,
            system=system,
            start=start,
            variant="telecom",
            metric_files=TELECOM_METRIC_FILES,
            log_files=(),
        )

    def _fetch_legacy_case(
        self,
        case_id: str,
        *,
        system: str,
        start: int,
        variant: str,
        metric_files: tuple[str, ...],
        log_files: tuple[str, ...],
    ) -> OpenRCACase:
        system_root = self.cache_dir / system
        query_path = system_root / "query.csv"
        record_path = system_root / "record.csv"
        self._download(f"{system}/query.csv", query_path)
        self._download(f"{system}/record.csv", record_path)
        task = _find_window_task(query_path, start)
        time_start, time_end = _task_window(task["instruction"])
        records = _ground_truth_records(record_path, time_start, time_end)
        if len(records) != 1:
            raise OpenRCAError(
                f"{case_id} has {len(records)} root-cause records in its task window"
            )
        truth = records[0]
        day = datetime.fromtimestamp(time_start, TIMEZONE).strftime("%Y_%m_%d")
        telemetry_root = system_root / "telemetry" / day
        metrics = tuple(telemetry_root / "metric" / name for name in metric_files)
        logs = tuple(telemetry_root / "log" / name for name in log_files)
        traces = telemetry_root / "trace" / "trace_span.csv"
        for path in (*metrics, *logs, traces):
            source = path.relative_to(self.cache_dir).as_posix()
            self._download(source, path)
        return OpenRCACase(
            source_case=case_id,
            dataset=DATASET_REVISION,
            system=system.replace("/", " "),
            root=telemetry_root,
            input=CaseInput(
                case_token=case_id,
                time_start=time_start,
                time_end=time_end,
                alert_time=time_end,
                alert_text=task["instruction"],
                fault_taxonomy=_fault_taxonomy(record_path),
            ),
            ground_truth=GroundTruth(
                affected_component=truth["component"],
                fault_type=truth["reason"],
                fault_category=_fault_category(truth["reason"]),
                inject_time=int(float(truth["timestamp"])),
            ),
            variant=variant,
            metric_paths=metrics,
            log_paths=logs,
            traces_path=traces,
        )

    @staticmethod
    def _download(source: str, target: Path) -> None:
        if target.is_file() and target.stat().st_size:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f"{target.suffix}.part")
        with (
            httpx.Client(follow_redirects=True, timeout=600) as client,
            client.stream("GET", f"{MIRROR_BASE_URL}/{source}") as response,
        ):
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        partial.replace(target)


def ingest_case(client: GreptimeClient, case: OpenRCACase) -> IngestCounts:
    client.create_database(case.input.database)
    counts = IngestCounts()
    series, source_rows = (
        _metric_series(case) if case.variant == "bank" else _legacy_metric_series(case)
    )
    audit = _metric_sample_audit(series, source_rows=source_rows)
    write_series_batch(client, case.input.database, series)
    counts.metric_source_rows = audit["source_rows"]
    counts.metrics_samples = audit["valid_samples"]
    counts.metric_unique_samples = audit["unique_samples"]
    counts.metric_duplicate_samples = audit["duplicate_samples"]
    counts.metric_conflicting_timestamps = audit["conflicting_timestamps"]
    if case.log_paths:
        counts.log_records = write_logs(
            client,
            case.input.database,
            _iter_logs(case) if case.variant == "bank" else _iter_legacy_logs(case),
        )

    writer = OtlpTraceWriter(client, case.input.database)
    traces = _iter_traces(case) if case.variant == "bank" else _iter_legacy_traces(case)
    counts.trace_spans = writer.write(traces)
    counts.rejected_trace_spans = writer.rejected_spans
    counts.remapped_trace_ids = writer.stats.remapped_trace_ids
    counts.remapped_span_ids = writer.stats.remapped_span_ids
    return counts


def source_audit(case: OpenRCACase) -> dict[str, object]:
    if case.variant != "bank":
        metric_scale = 1 if case.variant == "market" else 1_000
        return {
            "window_rows": {
                "metrics": {
                    path.name: _count_window_rows(
                        path,
                        case,
                        metric_scale,
                        timestamp_column=(
                            "startTime" if path.name == "metric_app.csv" else "timestamp"
                        ),
                    )
                    for path in case.metric_paths
                },
                "logs": {path.name: _count_window_rows(path, case, 1) for path in case.log_paths},
                "traces": _count_window_rows(
                    case.traces_path,
                    case,
                    1_000,
                    timestamp_column=("timestamp" if case.variant == "market" else "startTime"),
                ),
            },
            "timestamp_units": {
                "metrics": "seconds" if case.variant == "market" else "milliseconds",
                "logs": "seconds" if case.log_paths else "not available",
                "traces": "milliseconds",
            },
            "trace_duration_unit": "unknown",
            "trace_duration_representation": "raw source attribute; zero-length OTLP span",
            "task_timezone": "Asia/Shanghai",
            "trace_span_kind_available": False,
            "trace_status_semantics_available": False,
            "graph_applicability": "not-applicable",
            "graph_reason": (
                "source traces publish parent links but no client/server span roles or "
                "standard OTel entity identity"
            ),
            "trace_component_representation": "raw openrca.cmdb_id span attribute",
            "otel_service_name_representation": (
                "empty protocol placeholder; pod, node, and service component kinds are not "
                "collapsed into service identity"
            ),
            "protocol_metadata_labels": ["openrca_metric_source", "openrca_log_source"],
            "source_data_modified": False,
        }
    counts = {
        "metric_app_rows": _count_window_rows(_required(case.metric_app_path), case, 1),
        "metric_container_rows": _count_window_rows(_required(case.metric_container_path), case, 1),
        "log_rows": _count_window_rows(_required(case.logs_path), case, 1),
        "trace_rows": _count_window_rows(case.traces_path, case, 1_000),
    }
    return {
        "window_rows": counts,
        "timestamp_units": {
            "metrics": "seconds",
            "logs": "seconds",
            "traces": "milliseconds",
        },
        "trace_duration_unit": "unknown",
        "trace_duration_representation": "raw openrca.duration attribute",
        "task_timezone": "Asia/Shanghai",
        "trace_span_kind_available": False,
        "trace_status_available": False,
        "graph_applicability": "not-applicable",
        "graph_reason": (
            "Bank traces do not publish OTel entity identity, span kind, or status fields"
        ),
        "trace_component_representation": "raw openrca.cmdb_id span attribute",
        "otel_service_name_representation": (
            "empty protocol placeholder; no service identity is asserted"
        ),
        "source_data_modified": False,
    }


def validate_ingest(
    client: GreptimeClient,
    case: OpenRCACase,
    counts: IngestCounts,
) -> dict[str, object]:
    semantics = client.query(
        f"""
        SELECT table_name, signal_type, source
        FROM information_schema.table_semantics
        WHERE table_schema = '{case.input.database}'
        ORDER BY table_name
        """,
        max_rows=1_000,
    )
    metric_tables = [str(row[0]) for row in semantics.rows if row[1] == "metric"]
    metric_stats = [
        _database_table_stats(client, table, "greptime_timestamp") for table in metric_tables
    ]
    database_counts = {
        "metric_samples": sum(int(stats["row_count"]) for stats in metric_stats),
        "log_records": (
            _database_table_stats(client, "logs", "greptime_timestamp")["row_count"]
            if counts.log_records
            else 0
        ),
        "trace_spans": (
            _database_table_stats(client, "traces", "timestamp")["row_count"]
            if counts.trace_spans
            else 0
        ),
    }
    expected_counts = {
        "metric_samples": counts.metric_unique_samples,
        "log_records": counts.log_records,
        "trace_spans": counts.trace_spans,
    }
    start = datetime.fromtimestamp(case.input.time_start, UTC).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(case.input.time_end, UTC).strftime("%Y-%m-%d %H:%M:%S")
    relationships = client.query(
        f"""
        SELECT COUNT(*) AS relationship_count
        FROM greptime_private.semantic_relationships
        WHERE observed_at >= '{start}' AND observed_at < '{end}'
        """
    )
    return {
        "database_counts": database_counts,
        "expected_protocol_counts": expected_counts,
        "protocol_row_counts_match": database_counts == expected_counts,
        "metric_table_count": len(metric_tables),
        "semantic_signal_sources": [
            {"signal_type": signal_type, "source": source}
            for signal_type, source in sorted(
                {(str(row[1]), str(row[2])) for row in semantics.rows}
            )
        ],
        "metric_time_range": {
            "min": min(str(stats["min_time"]) for stats in metric_stats),
            "max": max(str(stats["max_time"]) for stats in metric_stats),
        },
        "derived_relationship_count": int(relationships.rows[0][0]),
        "source_relationship_semantics_available": False,
    }


def _metric_series(
    case: OpenRCACase,
) -> tuple[list[tuple[str, list[tuple[int, float]], dict[str, str]]], int]:
    groups: dict[
        tuple[str, tuple[tuple[str, str], ...]],
        list[tuple[int, float]],
    ] = defaultdict(list)
    source_rows = 0
    source_names: dict[str, str] = {}
    with _required(case.metric_container_path).open(newline="") as source:
        for row in csv.DictReader(source):
            timestamp = int(row["timestamp"])
            if not _in_window(timestamp, case):
                continue
            source_rows += 1
            _append_metric(
                groups,
                source_names,
                row["kpi_name"],
                timestamp * 1_000,
                row["value"],
                {"cmdb_id": row["cmdb_id"]},
            )
    with _required(case.metric_app_path).open(newline="") as source:
        for row in csv.DictReader(source):
            timestamp = int(row["timestamp"])
            if not _in_window(timestamp, case):
                continue
            source_rows += 1
            for metric in APP_METRICS:
                _append_metric(
                    groups,
                    source_names,
                    metric,
                    timestamp * 1_000,
                    row[metric],
                    {"tc": row["tc"]},
                )
    return (
        [
            (metric_name, sorted(samples), dict(labels))
            for (metric_name, labels), samples in groups.items()
        ],
        source_rows,
    )


def _append_metric(
    groups: dict[tuple[str, tuple[tuple[str, str], ...]], list[tuple[int, float]]],
    source_names: dict[str, str],
    source_name: str,
    timestamp_ms: int,
    raw_value: str,
    labels: dict[str, str],
) -> None:
    metric_name = prometheus_metric_name(source_name)
    previous = source_names.setdefault(metric_name, source_name)
    if previous != source_name:
        raise OpenRCAError(f"metric name collision after normalization: {previous}, {source_name}")
    value = float(raw_value)
    if math.isfinite(value):
        groups[(metric_name, tuple(sorted(labels.items())))].append((timestamp_ms, value))


def _metric_sample_audit(
    series: list[tuple[str, list[tuple[int, float]], dict[str, str]]],
    *,
    source_rows: int,
) -> dict[str, int]:
    valid_samples = 0
    unique_samples = 0
    conflicting_timestamps = 0
    for _, samples, _ in series:
        values_by_timestamp: dict[int, set[float]] = defaultdict(set)
        for timestamp, value in samples:
            valid_samples += 1
            values_by_timestamp[timestamp].add(value)
        unique_samples += len(values_by_timestamp)
        conflicting_timestamps += sum(len(values) > 1 for values in values_by_timestamp.values())
    return {
        "source_rows": source_rows,
        "valid_samples": valid_samples,
        "unique_samples": unique_samples,
        "duplicate_samples": valid_samples - unique_samples,
        "conflicting_timestamps": conflicting_timestamps,
    }


def _legacy_metric_series(
    case: OpenRCACase,
) -> tuple[list[tuple[str, list[tuple[int, float]], dict[str, str]]], int]:
    groups: dict[
        tuple[str, tuple[tuple[str, str], ...]],
        list[tuple[int, float]],
    ] = defaultdict(list)
    source_names: dict[str, str] = {}
    source_rows = 0
    for path in case.metric_paths:
        with path.open(newline="") as source:
            for row in csv.DictReader(source):
                if case.variant == "market" and path.name == "metric_service.csv":
                    timestamp = int(row["timestamp"])
                    if not _in_window(timestamp, case):
                        continue
                    source_rows += 1
                    for name in ("rr", "sr", "mrt", "count"):
                        _append_metric(
                            groups,
                            source_names,
                            name,
                            timestamp * 1_000,
                            row[name],
                            {
                                "service": row["service"],
                                "openrca_metric_source": path.stem,
                                "openrca_kpi_name": name,
                            },
                        )
                    continue
                if case.variant == "telecom" and path.name == "metric_app.csv":
                    timestamp_ms = int(row["startTime"])
                    if not _in_window(timestamp_ms, case, scale=1_000):
                        continue
                    source_rows += 1
                    for name in ("avg_time", "num", "succee_num", "succee_rate"):
                        _append_metric(
                            groups,
                            source_names,
                            name,
                            timestamp_ms,
                            row[name],
                            {
                                "serviceName": row["serviceName"],
                                "openrca_metric_source": path.stem,
                                "openrca_kpi_name": name,
                            },
                        )
                    continue

                timestamp = int(row["timestamp"])
                scale = 1 if case.variant == "market" else 1_000
                if not _in_window(timestamp, case, scale=scale):
                    continue
                source_rows += 1
                source_name = row["kpi_name"] if case.variant == "market" else row["name"]
                labels = {
                    "cmdb_id": row["cmdb_id"],
                    "openrca_metric_source": path.stem,
                    "openrca_kpi_name": source_name,
                }
                if case.variant == "telecom":
                    labels.update({"bomc_id": row["bomc_id"], "itemid": row["itemid"]})
                _append_metric(
                    groups,
                    source_names,
                    source_name,
                    timestamp if scale == 1_000 else timestamp * 1_000,
                    row["value"],
                    labels,
                )
    return (
        [
            (metric_name, sorted(samples), dict(labels))
            for (metric_name, labels), samples in groups.items()
        ],
        source_rows,
    )


def _iter_legacy_logs(case: OpenRCACase) -> Iterator[LogRecord]:
    for path in case.log_paths:
        with path.open(newline="") as source:
            for row in csv.DictReader(source):
                timestamp = int(row["timestamp"])
                if _in_window(timestamp, case):
                    yield LogRecord(
                        timestamp=timestamp,
                        message=row["value"],
                        labels={
                            "cmdb_id": row["cmdb_id"],
                            "log_name": row["log_name"],
                            "openrca_log_source": path.stem,
                        },
                    )


def _iter_legacy_traces(case: OpenRCACase) -> Iterator[TraceSpan]:
    with case.traces_path.open(newline="") as source:
        for row in csv.DictReader(source):
            if case.variant == "market":
                timestamp_ms = int(row["timestamp"])
                if not _in_window(timestamp_ms, case, scale=1_000):
                    continue
                raw_duration: object = int(row["duration"])
                parent_span_id = row["parent_span"]
                name = row["operation_name"]
                attributes = {
                    "openrca.cmdb_id": row["cmdb_id"],
                    "openrca.duration": raw_duration,
                    "openrca.type": row["type"],
                    "openrca.status_code": row["status_code"],
                }
                trace_id = row["trace_id"]
                span_id = row["span_id"]
            else:
                timestamp_ms = int(row["startTime"])
                if not _in_window(timestamp_ms, case, scale=1_000):
                    continue
                raw_duration = float(row["elapsedTime"])
                parent_span_id = "" if row["pid"] in {"", "None", "null"} else row["pid"]
                name = row["serviceName"] or row["callType"]
                attributes = {
                    "openrca.cmdb_id": row["cmdb_id"],
                    "openrca.elapsed_time": raw_duration,
                    "openrca.call_type": row["callType"],
                    "openrca.success": row["success"],
                    "openrca.ds_name": row["dsName"],
                    "openrca.service_name": row["serviceName"],
                }
                trace_id = row["traceId"]
                span_id = row["id"]
            timestamp_ns = timestamp_ms * 1_000_000
            yield TraceSpan(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                name=name,
                kind=Span.SPAN_KIND_UNSPECIFIED,
                start_time_unix_nano=timestamp_ns,
                end_time_unix_nano=timestamp_ns,
                service_name="",
                status_code=Status.STATUS_CODE_UNSET,
                attributes=attributes,
                scope_name="openrca-replay",
                scope_version=DATASET_REVISION,
            )


def _iter_logs(case: OpenRCACase) -> Iterator[LogRecord]:
    with _required(case.logs_path).open(newline="") as source:
        for row in csv.DictReader(source):
            timestamp = int(row["timestamp"])
            if _in_window(timestamp, case):
                yield LogRecord(
                    timestamp=timestamp,
                    message=row["value"],
                    labels={"cmdb_id": row["cmdb_id"], "log_name": row["log_name"]},
                )


def _iter_traces(case: OpenRCACase) -> Iterator[TraceSpan]:
    with case.traces_path.open(newline="") as source:
        for row in csv.DictReader(source):
            timestamp_ms = int(row["timestamp"])
            if not _in_window(timestamp_ms, case, scale=1_000):
                continue
            component = row["cmdb_id"]
            timestamp_ns = timestamp_ms * 1_000_000
            yield TraceSpan(
                trace_id=row["trace_id"],
                span_id=row["span_id"],
                parent_span_id=row["parent_id"],
                name="",
                kind=Span.SPAN_KIND_UNSPECIFIED,
                start_time_unix_nano=timestamp_ns,
                end_time_unix_nano=timestamp_ns,
                service_name="",
                status_code=Status.STATUS_CODE_UNSET,
                attributes={
                    "openrca.cmdb_id": component,
                    "openrca.duration": int(row["duration"]),
                },
                scope_name="openrca-replay",
                scope_version=DATASET_REVISION,
            )


def _parse_case_id(case_id: str) -> tuple[str, int]:
    try:
        task_id, window_start = case_id.split("@", 1)
        parsed = datetime.fromisoformat(window_start)
    except ValueError as error:
        raise OpenRCAError(
            "Bank case must use task_N@YYYY-MM-DDTHH:MM in the official UTC+8 timezone"
        ) from error
    if parsed.tzinfo is not None:
        raise OpenRCAError("Bank case window must not include a timezone offset")
    return task_id, int(parsed.replace(tzinfo=TIMEZONE).timestamp())


def _parse_legacy_case_id(case_id: str) -> tuple[str, int]:
    try:
        system, window_start = case_id.rsplit("@", 1)
        parsed = datetime.fromisoformat(window_start)
    except ValueError as error:
        raise OpenRCAError(
            "Market/Telecom case must use SYSTEM@YYYY-MM-DDTHH:MM in the official UTC+8 timezone"
        ) from error
    if parsed.tzinfo is not None:
        raise OpenRCAError("Market/Telecom case window must not include a timezone offset")
    return system, int(parsed.replace(tzinfo=TIMEZONE).timestamp())


def _find_task(path: Path, task_id: str, expected_start: int) -> dict[str, str]:
    with path.open(newline="") as source:
        rows = [
            row
            for row in csv.DictReader(source)
            if row["task_index"] == task_id
            and _task_window(row["instruction"])[0] == expected_start
        ]
    if len(rows) != 1:
        local_start = datetime.fromtimestamp(expected_start, TIMEZONE).isoformat()
        raise OpenRCAError(
            f"expected one {task_id} row starting at {local_start}, found {len(rows)}"
        )
    return rows[0]


def _find_window_task(path: Path, expected_start: int) -> dict[str, str]:
    with path.open(newline="") as source:
        rows = [
            row
            for row in csv.DictReader(source)
            if _task_window(row["instruction"])[0] == expected_start
        ]
    if not rows:
        local_start = datetime.fromtimestamp(expected_start, TIMEZONE).isoformat()
        raise OpenRCAError(f"no task window starts at {local_start}")
    return rows[0]


def _task_window(instruction: str) -> tuple[int, int]:
    date_match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", instruction)
    times = re.findall(r"\b(\d{2}:\d{2})\b", instruction)
    if not date_match or len(times) != 2:
        raise OpenRCAError(f"cannot parse task window: {instruction}")
    day = datetime.strptime(date_match.group(1), "%B %d, %Y").date()
    start = datetime.strptime(times[0], "%H:%M").time()
    end = datetime.strptime(times[1], "%H:%M").time()
    return (
        int(datetime.combine(day, start, TIMEZONE).timestamp()),
        int(datetime.combine(day, end, TIMEZONE).timestamp()),
    )


def _ground_truth_records(path: Path, start: int, end: int) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return [
            row for row in csv.DictReader(source) if start <= int(float(row["timestamp"])) <= end
        ]


def _fault_taxonomy(path: Path) -> list[str]:
    with path.open(newline="") as source:
        return sorted({row["reason"] for row in csv.DictReader(source)})


def _fault_category(reason: str) -> FaultCategory:
    normalized = reason.lower()
    if "memory" in normalized:
        return FaultCategory.MEMORY
    if "cpu" in normalized:
        return FaultCategory.CPU
    if "i/o" in normalized or "disk" in normalized:
        return FaultCategory.DISK
    if "latency" in normalized or "delay" in normalized or "response time" in normalized:
        return FaultCategory.DELAY
    if "loss" in normalized:
        return FaultCategory.LOSS
    return FaultCategory.OTHER


def _count_window_rows(
    path: Path,
    case: OpenRCACase,
    scale: int,
    *,
    timestamp_column: str = "timestamp",
) -> int:
    count = 0
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            count += _in_window(int(row[timestamp_column]), case, scale=scale)
    return count


def _in_window(timestamp: int, case: OpenRCACase, *, scale: int = 1) -> bool:
    return case.input.time_start * scale <= timestamp <= case.input.time_end * scale


def _relative_telemetry_path(name: str) -> str:
    return {
        "metric_app": "metric/metric_app.csv",
        "metric_container": "metric/metric_container.csv",
        "logs": "log/log_service.csv",
        "traces": "trace/trace_span.csv",
    }[name]


def _database_table_stats(
    client: GreptimeClient,
    table: str,
    time_column: str,
) -> dict[str, object]:
    table_ident = table.replace('"', '""')
    time_ident = time_column.replace('"', '""')
    result = client.query(
        f'SELECT COUNT(*) AS row_count, MIN("{time_ident}") AS min_time, '
        f'MAX("{time_ident}") AS max_time FROM "{table_ident}"'
    )
    row = result.rows[0]
    return {"row_count": int(row[0]), "min_time": row[1], "max_time": row[2]}


def _required(path: Path | None) -> Path:
    if path is None:
        raise OpenRCAError("case is missing a required source path")
    return path
