from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download, snapshot_download

from semantic_rca_bench.contracts import (
    CaseInput,
    GroundTruth,
    IngestCounts,
    RCAEvalCase,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.protocols.loki import write_logs
from semantic_rca_bench.protocols.otlp import OtlpTraceWriter, TraceSpan
from semantic_rca_bench.protocols.prometheus import prometheus_metric_name, write_series

REPO_ID = "phamquiluan/RCAEval"
DATASET_REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
SOURCE_REVISION = "526cdd5818ea9d8c2a34e869ebd637bc6b4fa4b8"


class RCAEvalError(RuntimeError):
    pass


class RCAEvalRepository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_index(self) -> Path:
        return Path(
            hf_hub_download(
                repo_id=REPO_ID,
                repo_type="dataset",
                filename="cases.parquet",
                revision=DATASET_REVISION,
                local_dir=self.cache_dir,
            )
        )

    def select_case(self, *, dataset: str = "RE2-OB", fault: str = "delay") -> str:
        table = pq.read_table(self.fetch_index())
        selected = table.filter(
            pc.and_(
                pc.equal(table["dataset"], dataset),
                pc.equal(table["fault"], fault),
            )
        )
        if selected.num_rows == 0:
            raise RCAEvalError(f"no case for dataset={dataset}, fault={fault}")
        return str(selected["case"][0].as_py())

    def fetch_case(self, source_case: str) -> RCAEvalCase:
        root = self.cache_dir / source_case
        required_files = [
            self.cache_dir / "cases.parquet",
            root / "metrics.parquet",
            root / "inject_time.txt",
        ]
        if not all(path.is_file() for path in required_files):
            snapshot_download(
                repo_id=REPO_ID,
                repo_type="dataset",
                revision=DATASET_REVISION,
                allow_patterns=[f"{source_case}/*", "cases.parquet"],
                local_dir=self.cache_dir,
            )
        index = pq.read_table(self.cache_dir / "cases.parquet")
        match = index.filter(pc.equal(index["case"], source_case))
        if match.num_rows != 1:
            raise RCAEvalError(f"case index expected one row for {source_case}")
        row = {name: match[name][0].as_py() for name in match.column_names}
        dataset_index = index.filter(pc.equal(index["dataset"], row["dataset"]))
        fault_taxonomy = sorted({str(value) for value in dataset_index["fault"].to_pylist()})
        metrics_path = root / "metrics.parquet"
        logs_path = root / "logs.parquet"
        traces_path = root / "traces.parquet"
        inject_time = int(row["inject_time"])
        time_start = int(row["time_start"])
        time_end = int(row["time_end"])
        if not time_start <= inject_time <= time_end:
            raise RCAEvalError(f"inject_time {inject_time} is outside [{time_start}, {time_end}]")
        return RCAEvalCase(
            source_case=source_case,
            dataset=str(row["dataset"]),
            system=str(row["system"]),
            root=root,
            input=CaseInput(
                case_token=uuid.uuid4().hex,
                time_start=time_start,
                time_end=time_end,
                alert_time=time_end,
                fault_taxonomy=fault_taxonomy,
            ),
            ground_truth=GroundTruth(
                affected_component=str(row["root_cause_service"]),
                fault_type=str(row["fault"]),
                inject_time=inject_time,
            ),
            metrics_path=metrics_path,
            logs_path=logs_path if logs_path.exists() else None,
            traces_path=traces_path if traces_path.exists() else None,
        )


def ingest_case(client: GreptimeClient, case: RCAEvalCase) -> IngestCounts:
    client.create_database(case.input.database)
    counts = IngestCounts()

    metric_table = pq.read_table(case.metrics_path)
    counts.metric_source_rows = metric_table.num_rows
    timestamps = metric_table["time"].to_pylist()
    seen_metric_names: set[str] = set()
    for source_name in metric_table.column_names:
        if source_name == "time":
            continue
        metric_name = prometheus_metric_name(source_name)
        if metric_name in seen_metric_names:
            raise RCAEvalError(f"metric name collision after normalization: {source_name}")
        seen_metric_names.add(metric_name)
        service_name, separator, _ = source_name.partition("_")
        labels = {"source_series_name": source_name}
        if separator and service_name:
            labels["service_name"] = service_name
        values = metric_table[source_name].to_pylist()
        samples = [
            (int(timestamp) * 1_000, float(value))
            for timestamp, value in zip(timestamps, values, strict=True)
            if value is not None and math.isfinite(float(value))
        ]
        write_series(client, case.input.database, metric_name, samples, labels)
        counts.metrics_samples += len(samples)
        values_by_timestamp: dict[int, set[float]] = defaultdict(set)
        for timestamp, value in samples:
            values_by_timestamp[timestamp].add(value)
        counts.metric_unique_samples += len(values_by_timestamp)
        counts.metric_duplicate_samples += len(samples) - len(values_by_timestamp)
        counts.metric_conflicting_timestamps += sum(
            len(values) > 1 for values in values_by_timestamp.values()
        )

    if case.logs_path:
        counts.log_records = write_logs(
            client,
            case.input.database,
            _iter_logs(case.logs_path),
        )

    if case.traces_path:
        writer = OtlpTraceWriter(client, case.input.database)
        counts.trace_spans = writer.write(_trace_span(row) for row in _iter_rows(case.traces_path))
        counts.rejected_trace_spans = writer.rejected_spans
        counts.remapped_trace_ids = writer.stats.remapped_trace_ids
        counts.remapped_span_ids = writer.stats.remapped_span_ids

    return counts


def source_audit(case: RCAEvalCase) -> dict[str, object]:
    metric_file = pq.ParquetFile(case.metrics_path)
    return {
        "window_rows": {
            "metric_rows": metric_file.metadata.num_rows,
            "metric_series": len(metric_file.schema.names) - 1,
            "log_rows": (pq.ParquetFile(case.logs_path).metadata.num_rows if case.logs_path else 0),
            "trace_rows": (
                pq.ParquetFile(case.traces_path).metadata.num_rows if case.traces_path else 0
            ),
        },
        "timestamp_units": {
            "metrics": "seconds",
            "logs": "seconds",
            "trace_start": "microseconds",
            "trace_duration": "microseconds",
        },
        "trace_span_kind_available": False,
        "trace_service_name_available": bool(case.traces_path),
        "graph_applicability": "entity-only",
        "graph_reason": "source traces identify services but do not publish OTel span kinds",
        "source_data_modified": False,
    }


def validate_ingest(
    client: GreptimeClient,
    case: RCAEvalCase,
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
            if case.logs_path
            else 0
        ),
        "trace_spans": (
            _database_table_stats(client, "traces", "timestamp")["row_count"]
            if case.traces_path
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


def _iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=10_000):
        yield from pa.Table.from_batches([batch]).to_pylist()


def _iter_logs(path: Path) -> Iterator[tuple[int, str, str]]:
    for row in _iter_rows(path):
        yield int(row["timestamp"]), str(row["container_name"]), str(row["message"])


def _trace_span(row: dict[str, Any]) -> TraceSpan:
    start_us = int(row.get("startTime") or 0)
    start_ms = int(row.get("startTimeMillis") or 0)
    if start_us // 1_000 != start_ms:
        raise RCAEvalError("startTime is not a microsecond representation of startTimeMillis")
    duration_us = max(int(row.get("duration") or 0), 0)
    status_code = int(row.get("statusCode") or 0)
    attributes: dict[str, object] = {"rcaeval.status_code": status_code}
    method_name = str(row.get("methodName") or "")
    if method_name:
        attributes["rpc.method"] = method_name
    return TraceSpan(
        trace_id=str(row.get("traceID") or ""),
        span_id=str(row.get("spanID") or ""),
        parent_span_id=str(row.get("parentSpanID") or ""),
        name=str(row.get("operationName") or method_name or "unknown"),
        kind=0,
        start_time_unix_nano=start_us * 1_000,
        end_time_unix_nano=(start_us + duration_us) * 1_000,
        service_name=str(row.get("serviceName") or "unknown"),
        status_code=status_code,
        attributes=attributes,
        scope_name="rcaeval-replay",
    )


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
