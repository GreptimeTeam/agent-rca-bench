from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from pydantic import BaseModel, ConfigDict

from semantic_rca_bench.contracts import CaseInput, IngestCounts, QueryResult
from semantic_rca_bench.datasets.aegis import (
    ARTIFACT_FILENAME,
    ARTIFACT_MD5,
    ARTIFACT_RECORD,
    ARTIFACT_SIZE,
    SOURCE_DATASET_RECORD,
    TRANSFER_AGENT_CASE_ID,
    AegisAuditError,
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

AGENT_CASE_ID = TRANSFER_AGENT_CASE_ID
SELECTED_SOURCE_CASE = "ts0-ts-security-service-request-replace-method-j6gpxx"
DELAY_AGENT_CASE_ID = "aegis-transfer-002"
DELAY_SOURCE_CASE = "ts8-ts-route-plan-service-request-delay-5dmjfm"
FROZEN_TRANSFER_CASES = {
    AGENT_CASE_ID: SELECTED_SOURCE_CASE,
    DELAY_AGENT_CASE_ID: DELAY_SOURCE_CASE,
}
DATASET_REVISION = "aegis-fse-2026-reviewer@zenodo-19522409"
ADAPTER_REVISION = "aegis-transfer-v1"
PERIODS = ("normal", "abnormal")

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
_EDGE_COLUMNS = (
    "src_type",
    "src_id",
    "dst_type",
    "dst_id",
    "rel_type",
    "provenance",
    "request_count",
    "error_count",
)


class AegisTransferGroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True)

    services: tuple[str, str]
    declared_edge: tuple[str, str]
    fault_type: str


class AegisTransferCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_case_id: str
    source_case: str
    dataset: str
    system: str
    root: Path
    input: CaseInput
    ground_truth: AegisTransferGroundTruth
    normal_window: tuple[int, int]
    abnormal_window: tuple[int, int]
    gauge_paths: tuple[Path, Path]
    sum_paths: tuple[Path, Path]
    histogram_paths: tuple[Path, Path]
    log_paths: tuple[Path, Path]
    trace_paths: tuple[Path, Path]
    selected_manifest: dict[str, object]


def archive_checksum_status(path: Path) -> dict[str, object]:
    segments = (
        sorted(item for item in path.iterdir() if item.is_file()) if path.is_dir() else [path]
    )
    segment_names = [item.name for item in segments]
    segment_sequence_valid = not path.is_dir() or segment_names == [
        f"{index:02d}" for index in range(32)
    ]
    readable_segments = (
        segments if segment_sequence_valid and all(item.is_file() for item in segments) else []
    )
    observed_size = sum(item.stat().st_size for item in readable_segments) or None
    observed_md5 = None
    if observed_size is not None:
        digest = hashlib.md5(usedforsecurity=False)
        for segment in readable_segments:
            with segment.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        observed_md5 = digest.hexdigest()
    return {
        "artifact_record": ARTIFACT_RECORD,
        "artifact_filename": ARTIFACT_FILENAME,
        "source_dataset_record": SOURCE_DATASET_RECORD,
        "expected_size": ARTIFACT_SIZE,
        "observed_size": observed_size,
        "size_match": observed_size == ARTIFACT_SIZE,
        "expected_md5": ARTIFACT_MD5,
        "observed_md5": observed_md5,
        "md5_match": observed_md5 == ARTIFACT_MD5,
        "segment_count": len(readable_segments),
        "segment_sequence_valid": segment_sequence_valid,
        "verified": (
            segment_sequence_valid
            and observed_size == ARTIFACT_SIZE
            and observed_md5 == ARTIFACT_MD5
        ),
        "data_redistributed": False,
    }


def load_selected_case(
    cases_dir: Path,
    meta_dir: Path,
    selection_path: Path,
    *,
    database: str,
) -> AegisTransferCase:
    selection = _read_json(selection_path)
    agent_case_id = str(selection.get("agent_case_id") or "")
    if agent_case_id not in FROZEN_TRANSFER_CASES:
        raise AegisAuditError("frozen selection has an invalid agent case ID")
    frozen_source = selection.get("source")
    if not isinstance(frozen_source, dict) or any(
        (
            frozen_source.get(field) != expected
            for field, expected in (
                ("artifact_filename", ARTIFACT_FILENAME),
                ("artifact_md5", ARTIFACT_MD5),
                ("artifact_record", ARTIFACT_RECORD),
                ("source_dataset_record", SOURCE_DATASET_RECORD),
                ("data_redistributed", False),
            )
        )
    ):
        raise AegisAuditError("frozen selection has unexpected source provenance")
    selected = selection.get("selected_case")
    if not isinstance(selected, dict) or not isinstance(selected.get("source_case"), str):
        raise AegisAuditError("frozen selection does not name an Aegis source case")
    source_case = str(selected["source_case"])
    if source_case != FROZEN_TRANSFER_CASES[agent_case_id]:
        raise AegisAuditError("frozen selection case mapping is not supported")

    index_rows = _meta_rows(meta_dir / "index.parquet")
    matching_index = [
        row
        for row in index_rows
        if row.get("dataset") == "rcabench" and row.get("datapack") == source_case
    ]
    if len(matching_index) != 1:
        raise AegisAuditError("publisher index does not uniquely identify the selected case")
    attributes = _unique_meta_row(meta_dir / "attributes.parquet", source_case)
    label_rows = [
        row for row in _meta_rows(meta_dir / "labels.parquet") if row.get("datapack") == source_case
    ]
    if not label_rows or any(row.get("gt.level") != "service" for row in label_rows):
        raise AegisAuditError("publisher labels are missing or are not service labels")
    published_services = {str(row.get("gt.name") or "") for row in label_rows}

    root = cases_dir / source_case
    env = _read_json(root / "env.json")
    injection = _read_json(root / "injection.json")
    if injection.get("injection_name") != source_case:
        raise AegisAuditError("injection identity disagrees with the frozen selection")
    if injection.get("status") != 2 or not (root / ".finished").is_file():
        raise AegisAuditError("selected injection is not publisher-complete")
    if (root / ".invalid").exists():
        raise AegisAuditError("selected injection has a publisher invalid marker")

    normal_window = (_env_epoch(env, "NORMAL_START"), _env_epoch(env, "NORMAL_END"))
    abnormal_window = (_env_epoch(env, "ABNORMAL_START"), _env_epoch(env, "ABNORMAL_END"))
    if not normal_window[0] < normal_window[1] == abnormal_window[0] < abnormal_window[1]:
        raise AegisAuditError("selected source windows are not contiguous")
    if int(datetime.fromisoformat(str(injection["start_time"])).timestamp()) != abnormal_window[0]:
        raise AegisAuditError("injection time disagrees with the abnormal window")

    fault_type = str(attributes.get("injection.fault_type") or "")
    if fault_type != selected.get("fault_type"):
        raise AegisAuditError("publisher fault type disagrees with the frozen selection")
    ground_truth = injection.get("ground_truth")
    if not isinstance(ground_truth, dict) or not isinstance(ground_truth.get("service"), list):
        raise AegisAuditError("selected injection has no service ground truth")
    injection_services = {str(item) for item in ground_truth["service"]}
    selected_services = {str(item) for item in selected.get("ground_truth_services") or []}
    if published_services != injection_services or injection_services != selected_services:
        raise AegisAuditError("publisher, injection, and frozen service labels disagree")
    if len(selected_services) != 2:
        raise AegisAuditError("selected Aegis case must retain two service labels")
    if attributes.get("ground_truth.service_count") != 2:
        raise AegisAuditError("publisher service count disagrees with the selected labels")

    display_config = _json_object(injection.get("display_config"), "display_config")
    injection_point = display_config.get("injection_point")
    if not isinstance(injection_point, dict):
        raise AegisAuditError("selected injection has no structured injection point")
    declared_edge = (
        str(injection_point.get("app_name") or ""),
        str(injection_point.get("server_address") or ""),
    )
    frozen_edge = tuple(str(item) for item in selected.get("declared_edge") or [])
    if declared_edge != frozen_edge or len(frozen_edge) != 2:
        raise AegisAuditError("source-declared endpoint disagrees with the frozen edge")
    _validate_frozen_mechanism(fault_type, display_config, injection_point, selected)

    paths = {
        kind: tuple(root / f"{period}_{suffix}" for period in PERIODS)
        for kind, suffix in {
            "gauge": "metrics.parquet",
            "sum": "metrics_sum.parquet",
            "histogram": "metrics_histogram.parquet",
            "log": "logs.parquet",
            "trace": "traces.parquet",
        }.items()
    }
    missing = [path.name for values in paths.values() for path in values if not path.is_file()]
    if missing:
        raise AegisAuditError(f"selected case is missing source telemetry: {sorted(missing)}")

    expected_windows = {
        "normal_window": list(normal_window),
        "abnormal_window": list(abnormal_window),
    }
    for field, value in expected_windows.items():
        if selected.get(field) != value:
            raise AegisAuditError(f"source {field} disagrees with the frozen selection")

    return AegisTransferCase(
        agent_case_id=agent_case_id,
        source_case=source_case,
        dataset=DATASET_REVISION,
        system="Train Ticket",
        root=root,
        input=CaseInput(
            case_token=agent_case_id,
            time_start=normal_window[0],
            time_end=abnormal_window[1],
            alert_time=abnormal_window[0],
            database=database,
            fault_taxonomy=[],
        ),
        ground_truth=AegisTransferGroundTruth(
            services=tuple(sorted(selected_services)),
            declared_edge=declared_edge,
            fault_type=fault_type,
        ),
        normal_window=normal_window,
        abnormal_window=abnormal_window,
        gauge_paths=paths["gauge"],
        sum_paths=paths["sum"],
        histogram_paths=paths["histogram"],
        log_paths=paths["log"],
        trace_paths=paths["trace"],
        selected_manifest=selected,
    )


def _validate_frozen_mechanism(
    fault_type: str,
    display_config: dict[str, object],
    injection_point: dict[str, object],
    selected: dict[str, object],
) -> None:
    frozen = selected.get("mechanism_evidence")
    if not isinstance(frozen, dict):
        raise AegisAuditError("frozen mechanism evidence is missing")
    if fault_type == "HTTPRequestReplaceMethod":
        valid = (
            frozen.get("predicate") == "source_declared_http_method_replacement"
            and frozen.get("original_method") == injection_point.get("method")
            and frozen.get("replacement_method") == display_config.get("replace_method")
        )
    elif fault_type == "HTTPRequestDelay":
        declared_delay_ns = int(display_config.get("delay_duration") or 0) * 1_000_000
        valid = (
            frozen.get("predicate") == "source_declared_http_delay_threshold"
            and frozen.get("span_name")
            == f"{injection_point.get('method')} {injection_point.get('route')}"
            and frozen.get("declared_delay_ns") == declared_delay_ns
        )
    else:
        raise AegisAuditError(f"unsupported frozen Aegis transfer mechanism: {fault_type}")
    if not valid:
        raise AegisAuditError("source injection disagrees with frozen mechanism evidence")


def source_audit(case: AegisTransferCase) -> dict[str, object]:
    metric_audit = _metric_sample_audit(case)
    trace_windows = {
        period: _source_trace_window_audit(path, window)
        for period, path, window in zip(
            PERIODS,
            case.trace_paths,
            (case.normal_window, case.abnormal_window),
            strict=True,
        )
    }
    combined_trace_identity_unique = _combined_trace_identity_unique(case.trace_paths)
    frozen_checks = {}
    for period in PERIODS:
        expected = case.selected_manifest[f"{period}_raw_edge_set"]
        observed = trace_windows[period]
        frozen_checks[period] = {
            "edge_count_match": observed["edge_count"] == expected["edge_count"],
            "witness_count_match": observed["witness_count"] == expected["witness_count"],
            "sha256_match": observed["edge_set_sha256"] == expected["sha256"],
        }
    source_counts = {
        "metric_rows": metric_audit["source_rows"],
        "log_rows": sum(pq.ParquetFile(path).metadata.num_rows for path in case.log_paths),
        "trace_rows": sum(window["source_rows"] for window in trace_windows.values()),
    }
    signal_windows = {
        period: _source_signal_window_audit(
            (
                case.gauge_paths[index],
                case.sum_paths[index],
                case.histogram_paths[index],
                case.log_paths[index],
                case.trace_paths[index],
            ),
            window,
        )
        for index, (period, window) in enumerate(
            zip(PERIODS, (case.normal_window, case.abnormal_window), strict=True)
        )
    }
    boundary_anomalies = [
        {
            "period": period,
            "file": name,
            "rows_before_start": item["rows_before_start"],
            "rows_at_or_after_end": item["rows_at_or_after_end"],
            "rows_at_exact_end": item["rows_at_exact_end"],
            "rows_after_end": item["rows_after_end"],
            "null_timestamps": item["null_timestamps"],
        }
        for period, signal_window in signal_windows.items()
        for name, item in signal_window["files"].items()
        if item["rows_before_start"] or item["rows_at_or_after_end"] or item["null_timestamps"]
    ]
    boundary_accounted = all(
        item["period"] == "normal"
        and item["rows_before_start"] == 0
        and item["rows_at_or_after_end"] == item["rows_at_exact_end"]
        and item["rows_after_end"] == 0
        and item["null_timestamps"] == 0
        for item in boundary_anomalies
    )
    return {
        "source_row_counts": source_counts,
        "metric_representation": metric_audit,
        "signal_windows": signal_windows,
        "trace_windows": trace_windows,
        "source_window_boundary_anomalies": boundary_anomalies,
        "frozen_edge_checks": frozen_checks,
        "source_identity_valid": all(
            bool(window["identity_valid"]) and bool(window["span_identity_unique"])
            for window in trace_windows.values()
        )
        and combined_trace_identity_unique,
        "combined_trace_identity_unique": combined_trace_identity_unique,
        "all_signal_timestamps_in_declared_windows": all(
            bool(window["all_rows_in_declared_window"]) for window in signal_windows.values()
        ),
        "trace_windows_exact": all(
            bool(window["all_rows_in_declared_window"]) for window in trace_windows.values()
        ),
        "source_window_boundaries_accounted": boundary_accounted,
        "frozen_edge_sets_match": all(all(check.values()) for check in frozen_checks.values()),
        "reference_causal_graph_read": False,
        "reference_causal_graph_ingested": False,
        "source_data_modified": False,
    }


def ingest_case(
    client: GreptimeClient,
    case: AegisTransferCase,
    source: dict[str, object],
) -> IngestCounts:
    metric = source["metric_representation"]
    if not isinstance(metric, dict):
        raise AegisAuditError("source metric audit is malformed")
    counts = IngestCounts(
        metric_source_rows=int(metric["source_rows"]),
        metric_unique_samples=int(metric["unique_samples"]),
        metric_duplicate_samples=int(metric["duplicate_samples"]),
        metric_conflicting_timestamps=int(metric["conflicting_timestamps"]),
        metric_protocol_rows=int(metric["protocol_rows"]),
    )
    metric_writer = OtlpMetricWriter(
        client,
        case.input.database,
        scope_name="aegis-replay",
        scope_version=ADAPTER_REVISION,
    )
    counts.metrics_samples += metric_writer.write_gauges(_iter_number_metrics(case.gauge_paths))
    counts.metrics_samples += metric_writer.write_sums(_iter_number_metrics(case.sum_paths))
    counts.metrics_samples += metric_writer.write_histograms(_iter_histograms(case.histogram_paths))
    counts.rejected_metric_points = metric_writer.rejected_data_points
    counts.log_records = write_logs(
        client,
        case.input.database,
        _iter_logs(case.log_paths),
    )
    trace_writer = OtlpTraceWriter(client, case.input.database)
    counts.trace_spans = trace_writer.write(_iter_traces(case.trace_paths))
    counts.rejected_trace_spans = trace_writer.rejected_spans
    counts.remapped_trace_ids = trace_writer.stats.remapped_trace_ids
    counts.remapped_span_ids = trace_writer.stats.remapped_span_ids
    return counts


def validate_stored_rows(
    client: GreptimeClient,
    case: AegisTransferCase,
    counts: IngestCounts,
    source: dict[str, object],
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
    log_tables = [str(row[0]) for row in semantics.rows if row[1] == "log"]
    stored = {
        "metric_rows": sum(_table_count(client, table) for table in metric_tables),
        "log_rows": sum(_table_count(client, table) for table in log_tables),
        "trace_rows": _table_count(client, "traces"),
    }
    expected = {
        "metric_rows": counts.metric_protocol_rows,
        "log_rows": counts.log_records,
        "trace_rows": counts.trace_spans,
    }
    source_counts = source["source_row_counts"]
    protocol = {
        "metrics": {
            "source_rows": counts.metric_source_rows,
            "submitted_points": counts.metrics_samples,
            "accepted_points": counts.metrics_samples - counts.rejected_metric_points,
            "rejected_points": counts.rejected_metric_points,
            "unique_source_identities": counts.metric_unique_samples,
            "duplicate_source_rows": counts.metric_duplicate_samples,
            "conflicting_source_identities": counts.metric_conflicting_timestamps,
            "represented_rows_after_primary_key_semantics": counts.metric_protocol_rows,
        },
        "logs": {
            "source_rows": source_counts["log_rows"],
            "submitted_rows": counts.log_records,
            "accepted_rows": counts.log_records,
            "rejected_rows": 0,
        },
        "traces": {
            "source_rows": source_counts["trace_rows"],
            "submitted_rows": counts.trace_spans,
            "accepted_rows": counts.trace_spans - counts.rejected_trace_spans,
            "rejected_rows": counts.rejected_trace_spans,
        },
    }
    stored_services = _group_counts(client, "service_name")
    stored_roles = _group_counts(client, "span_kind")
    stored_statuses = _group_counts(client, "span_status_code")
    expected_services = _combined_trace_distribution(source, "service_counts")
    expected_roles = {
        f"SPAN_KIND_{key.upper()}": value
        for key, value in _combined_trace_distribution(source, "span_kind_counts").items()
    }
    expected_statuses = {
        f"STATUS_CODE_{key.upper()}": value
        for key, value in _combined_trace_distribution(source, "status_code_counts").items()
    }
    return {
        "source_row_counts": source_counts,
        "protocol_counts": protocol,
        "stored_row_counts": stored,
        "expected_stored_row_counts": expected,
        "stored_row_counts_match": stored == expected,
        "protocol_rejections_zero": (
            counts.rejected_metric_points == 0 and counts.rejected_trace_spans == 0
        ),
        "id_remapping": {
            "trace_ids": counts.remapped_trace_ids,
            "span_ids": counts.remapped_span_ids,
            "pass": counts.remapped_trace_ids == 0 and counts.remapped_span_ids == 0,
        },
        "source_identity": {
            "source_service_counts": expected_services,
            "stored_service_counts": stored_services,
            "service_counts_match": expected_services == stored_services,
            "source_span_kind_counts": expected_roles,
            "stored_span_kind_counts": stored_roles,
            "span_kind_counts_match": expected_roles == stored_roles,
            "source_status_code_counts": expected_statuses,
            "stored_status_code_counts": stored_statuses,
            "status_code_counts_match": expected_statuses == stored_statuses,
        },
        "semantic_signal_sources": [
            {"table_name": str(row[0]), "signal_type": str(row[1]), "source": str(row[2])}
            for row in semantics.rows
        ],
    }


def exact_edge_equality_audit(
    client: GreptimeClient,
    case: AegisTransferCase,
) -> dict[str, object]:
    window = graph_audit_window(case.normal_window[0], case.abnormal_window[1])
    observed_start, observed_end = window["graph_observed_window"]
    raw_query = canonical_raw_edge_query(observed_start, observed_end)
    graph_query = canonical_graph_edge_query(observed_start, observed_end)
    raw_result = client.query(raw_query, max_rows=None)
    graph_result = client.query(graph_query, max_rows=None)
    raw_edges = normalize_edge_result(raw_result)
    graph_edges = normalize_edge_result(graph_result)
    exact = edge_results_equal(raw_result, graph_result)
    return {
        "window_contract": window,
        "raw_edge_query": raw_query,
        "raw_edge_result": raw_result.model_dump(mode="json"),
        "graph_edge_query": graph_query,
        "graph_edge_result": graph_result.model_dump(mode="json"),
        "normalized_raw_edges": raw_edges,
        "normalized_graph_edges": graph_edges,
        "raw_edge_set_sha256": _edge_hash(raw_edges),
        "graph_edge_set_sha256": _edge_hash(graph_edges),
        "exact_edge_set_equality": exact,
    }


def graph_audit_window(source_start: int, source_end: int) -> dict[str, object]:
    if source_start >= source_end:
        raise ValueError("Aegis graph source window must be non-empty")
    observed_start = _floor_minute(source_start)
    observed_end = _ceil_minute(source_end)
    return {
        "source_window": [source_start, source_end],
        "source_window_semantics": "publisher half-open normal+abnormal envelope",
        "graph_observed_window": [observed_start, observed_end],
        "client_scan_window": [observed_start, observed_end],
        "server_scan_window": [observed_start - 5 * 60, observed_end + 60 * 60],
        "observed_at_bin": "date_bin(60s, client.timestamp)",
        "join_time_bounds": (
            "server.timestamp >= client.timestamp - 5m and <= client.timestamp + 1h"
        ),
        "transform": "minimal whole-minute envelope containing the complete source window",
        "boundary_proof": (
            "source audit requires every stored source span timestamp to remain inside the "
            "publisher half-open envelope, so floor/ceil adds no source rows"
        ),
    }


def canonical_raw_edge_query(window_start: int, window_end: int) -> str:
    start = _time_literal(window_start)
    end = _time_literal(window_end)
    server_start = _time_literal(window_start - 5 * 60)
    server_end = _time_literal(window_end + 60 * 60)
    return f"""SELECT 'service' AS src_type, c.service_name AS src_id,
       'service' AS dst_type, s.service_name AS dst_id,
       'calls' AS rel_type, 'trace' AS provenance,
       COUNT(*) AS request_count,
       SUM(CASE WHEN s.span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END) AS error_count
FROM traces c
JOIN traces s
  ON c.trace_id = s.trace_id
 AND s.parent_span_id = c.span_id
 AND s.timestamp >= c.timestamp - INTERVAL '5 minutes'
 AND s.timestamp <= c.timestamp + INTERVAL '1 hour'
WHERE c.span_kind = 'SPAN_KIND_CLIENT'
  AND s.span_kind = 'SPAN_KIND_SERVER'
  AND c.service_name <> s.service_name
  AND c.timestamp >= {start} AND c.timestamp < {end}
  AND s.timestamp >= {server_start} AND s.timestamp < {server_end}
GROUP BY c.service_name, s.service_name
ORDER BY src_id, dst_id"""


def canonical_graph_edge_query(window_start: int, window_end: int) -> str:
    start = _time_literal(window_start)
    end = _time_literal(window_end)
    return f"""SELECT src_type, src_id, dst_type, dst_id, rel_type, provenance,
       SUM(request_count) AS request_count,
       SUM(error_count) AS error_count
FROM greptime_private.semantic_relationships
WHERE observed_at >= {start} AND observed_at < {end}
  AND src_type = 'service' AND dst_type = 'service'
  AND rel_type = 'calls' AND provenance = 'trace'
GROUP BY src_type, src_id, dst_type, dst_id, rel_type, provenance
ORDER BY src_id, dst_id"""


def normalize_edge_result(result: QueryResult) -> list[dict[str, object]] | None:
    if result.truncated:
        return None
    columns = [column.lower() for column in result.columns]
    if any(columns.count(column) != 1 for column in _EDGE_COLUMNS):
        return None
    indexes = [columns.index(column) for column in _EDGE_COLUMNS]
    edges = []
    identities = set()
    for row in result.rows:
        if len(row) <= max(indexes):
            return None
        values = [row[index] for index in indexes]
        if (
            not isinstance(values[6], int)
            or isinstance(values[6], bool)
            or not isinstance(values[7], int)
            or isinstance(values[7], bool)
        ):
            return None
        identity = tuple(str(value) for value in values[:6])
        if identity in identities:
            return None
        identities.add(identity)
        edges.append(
            {
                **dict(zip(_EDGE_COLUMNS[:6], identity, strict=True)),
                "request_count": values[6],
                "error_count": values[7],
            }
        )
    return sorted(edges, key=lambda edge: tuple(edge[column] for column in _EDGE_COLUMNS))


def edge_results_equal(left: QueryResult, right: QueryResult) -> bool:
    left_edges = normalize_edge_result(left)
    return left_edges is not None and left_edges == normalize_edge_result(right)


def mechanism_evidence_audit(
    client: GreptimeClient,
    case: AegisTransferCase,
) -> dict[str, object]:
    query = canonical_mechanism_evidence_query(case)
    result = client.query(query, max_rows=None)
    expected = case.selected_manifest.get("mechanism_evidence")
    if not isinstance(expected, dict):
        raise AegisAuditError("frozen mechanism evidence is missing")
    predicate = expected.get("predicate")
    if predicate == "source_declared_http_method_replacement":
        normalized = normalize_mechanism_evidence(result)
        expected_result = {
            "normal_server_methods": expected.get("normal_server_methods"),
            "abnormal_client_methods": expected.get("abnormal_client_methods"),
            "abnormal_server_methods": expected.get("abnormal_server_methods"),
        }
        mechanism_fields = {
            "original_method": expected.get("original_method"),
            "replacement_method": expected.get("replacement_method"),
        }
    elif predicate == "source_declared_http_delay_threshold":
        normalized = normalize_delay_evidence(result)
        expected_result = {
            "normal": {
                "count": expected.get("normal_count"),
                "max_duration_ns": expected.get("normal_max_duration_ns"),
            },
            "abnormal": {
                "count": expected.get("abnormal_count"),
                "max_duration_ns": expected.get("abnormal_max_duration_ns"),
            },
        }
        mechanism_fields = {
            "span_name": expected.get("span_name"),
            "declared_delay_ns": expected.get("declared_delay_ns"),
        }
    else:
        raise AegisAuditError(f"unsupported stored mechanism evidence predicate: {predicate}")
    declared_edge_match = list(case.ground_truth.declared_edge) == case.selected_manifest.get(
        "declared_edge"
    )
    evidence_match = mechanism_evidence_matches(normalized, expected_result)
    return {
        "predicate": predicate,
        "declared_edge": list(case.ground_truth.declared_edge),
        "declared_edge_match": declared_edge_match,
        **mechanism_fields,
        "query": query,
        "result": result.model_dump(mode="json"),
        "normalized_result": normalized,
        "expected_result": expected_result,
        "evidence_match": evidence_match,
        "pass": declared_edge_match and evidence_match,
    }


def canonical_mechanism_evidence_query(case: AegisTransferCase) -> str:
    normal_start = _time_literal(case.normal_window[0])
    normal_end = _time_literal(case.normal_window[1])
    abnormal_start = _time_literal(case.abnormal_window[0])
    abnormal_end = _time_literal(case.abnormal_window[1])
    source = _literal(case.ground_truth.declared_edge[0])
    destination = _literal(case.ground_truth.declared_edge[1])
    evidence = case.selected_manifest.get("mechanism_evidence")
    if not isinstance(evidence, dict):
        raise AegisAuditError("frozen mechanism evidence is missing")
    if evidence.get("predicate") == "source_declared_http_delay_threshold":
        span_name = _literal(str(evidence.get("span_name") or ""))
        return f"""WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= {normal_start} AND c.timestamp < {normal_end} THEN 'normal'
           WHEN c.timestamp >= {abnormal_start} AND c.timestamp < {abnormal_end} THEN 'abnormal'
         END AS period,
         s.duration_nano AS server_duration_ns
  FROM traces c
  JOIN traces s
    ON c.trace_id = s.trace_id
   AND s.parent_span_id = c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = {source}
    AND s.service_name = {destination}
    AND s.span_name = {span_name}
    AND c.timestamp >= {normal_start} AND c.timestamp < {abnormal_end}
)
SELECT period, COUNT(*) AS span_count, MAX(server_duration_ns) AS max_duration_ns
FROM paired
GROUP BY period
ORDER BY period"""
    if evidence.get("predicate") != "source_declared_http_method_replacement":
        raise AegisAuditError("unsupported canonical mechanism evidence predicate")
    return f"""WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= {normal_start} AND c.timestamp < {normal_end} THEN 'normal'
           WHEN c.timestamp >= {abnormal_start} AND c.timestamp < {abnormal_end} THEN 'abnormal'
         END AS period,
         c."span_attributes.http.request.method" AS client_method,
         s."span_attributes.http.request.method" AS server_method
  FROM traces c
  JOIN traces s
    ON c.trace_id = s.trace_id
   AND s.parent_span_id = c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = {source}
    AND s.service_name = {destination}
    AND c.timestamp >= {normal_start} AND c.timestamp < {abnormal_end}
)
SELECT period, side, method, COUNT(*) AS span_count
FROM (
  SELECT period, 'server' AS side, server_method AS method FROM paired WHERE period = 'normal'
  UNION ALL
  SELECT period, 'client' AS side, client_method AS method FROM paired WHERE period = 'abnormal'
  UNION ALL
  SELECT period, 'server' AS side, server_method AS method FROM paired WHERE period = 'abnormal'
) evidence
GROUP BY period, side, method
ORDER BY period, side, method"""


def normalize_mechanism_evidence(result: QueryResult) -> dict[str, dict[str, int]] | None:
    required = ("period", "side", "method", "span_count")
    columns = [column.lower() for column in result.columns]
    if result.truncated or any(columns.count(column) != 1 for column in required):
        return None
    indexes = [columns.index(column) for column in required]
    normalized = {
        "normal_server_methods": {},
        "abnormal_client_methods": {},
        "abnormal_server_methods": {},
    }
    for row in result.rows:
        period, side, method, count = (row[index] for index in indexes)
        key = f"{period}_{side}_methods"
        if key not in normalized or not isinstance(count, int) or isinstance(count, bool):
            return None
        method_name = str(method)
        if method_name in normalized[key]:
            return None
        normalized[key][method_name] = count
    return {key: dict(sorted(value.items())) for key, value in normalized.items()}


def normalize_delay_evidence(result: QueryResult) -> dict[str, dict[str, int]] | None:
    required = ("period", "span_count", "max_duration_ns")
    columns = [column.lower() for column in result.columns]
    if result.truncated or any(columns.count(column) != 1 for column in required):
        return None
    indexes = [columns.index(column) for column in required]
    normalized = {}
    for row in result.rows:
        period, count, max_duration = (row[index] for index in indexes)
        if (
            period not in PERIODS
            or period in normalized
            or not isinstance(count, int)
            or isinstance(count, bool)
            or not isinstance(max_duration, int)
            or isinstance(max_duration, bool)
        ):
            return None
        normalized[str(period)] = {
            "count": count,
            "max_duration_ns": max_duration,
        }
    return {period: normalized[period] for period in PERIODS if period in normalized}


def mechanism_evidence_matches(
    observed: dict[str, dict[str, int]] | None,
    expected: dict[str, object],
) -> bool:
    return observed is not None and observed == expected


def no_model_gates(
    case: AegisTransferCase,
    archive: dict[str, object],
    source: dict[str, object],
    stored: dict[str, object],
    equality: dict[str, object],
    mechanism: dict[str, object],
    *,
    isolated: bool,
    frozen_selection: bool,
) -> dict[str, bool]:
    identity = stored["source_identity"]
    if not isinstance(identity, dict):
        raise AegisAuditError("stored identity audit is malformed")
    gates = {
        "pinned_archive_verified": archive.get("verified") is True,
        "frozen_selection_match": frozen_selection,
        "frozen_source_edge_sets_match": source.get("frozen_edge_sets_match") is True,
        "trace_windows_exact": source.get("trace_windows_exact") is True,
        "source_window_boundaries_accounted": (
            source.get("source_window_boundaries_accounted") is True
        ),
        "source_identity_valid": source.get("source_identity_valid") is True,
        "reference_causal_graph_excluded": (
            source.get("reference_causal_graph_read") is False
            and source.get("reference_causal_graph_ingested") is False
        ),
        "exclusive_graph_source": isolated,
        "protocol_rejections_zero": stored.get("protocol_rejections_zero") is True,
        "stored_row_counts_match": stored.get("stored_row_counts_match") is True,
        "id_remapping_zero": stored.get("id_remapping", {}).get("pass") is True,
        "stored_source_identity_match": all(
            identity.get(field) is True
            for field in (
                "service_counts_match",
                "span_kind_counts_match",
                "status_code_counts_match",
            )
        ),
        "raw_graph_exact_edge_set_equality": equality.get("exact_edge_set_equality") is True,
        "mechanism_evidence": mechanism.get("pass") is True,
        "opaque_agent_case_id": (
            case.input.case_token == case.agent_case_id
            and case.source_case not in case.input.model_dump_json()
            and case.ground_truth.fault_type not in case.input.model_dump_json()
            and not case.input.fault_taxonomy
        ),
    }
    gates["all_passed"] = all(gates.values())
    return gates


def _source_trace_window_audit(path: Path, window: tuple[int, int]) -> dict[str, object]:
    rows = list(_iter_rows((path,)))
    identities = [(str(row["trace_id"]), str(row["span_id"])) for row in rows]
    index = {identity: row for identity, row in zip(identities, rows, strict=True)}
    edges: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    witnesses = 0
    for server in rows:
        if server.get("attr.span_kind") != "Server" or not server.get("parent_span_id"):
            continue
        client = index.get((str(server["trace_id"]), str(server["parent_span_id"])))
        if client is None or client.get("attr.span_kind") != "Client":
            continue
        pair = (str(client["service_name"]), str(server["service_name"]))
        edges[pair][0] += 1
        edges[pair][1] += server.get("attr.status_code") == "Error"
        witnesses += 1
    edge_set = _edge_dicts(edges)
    start_ns, end_ns = window[0] * 1_000_000_000, window[1] * 1_000_000_000
    times = [int(row["time"]) for row in rows]
    kinds = Counter(str(row.get("attr.span_kind")) for row in rows)
    statuses = Counter(str(row.get("attr.status_code")) for row in rows)
    services = Counter(str(row.get("service_name")) for row in rows)
    identity_valid = all(
        _valid_hex_id(str(row.get("trace_id") or ""), 16)
        and _valid_hex_id(str(row.get("span_id") or ""), 8)
        and (not row.get("parent_span_id") or _valid_hex_id(str(row["parent_span_id"]), 8))
        and row.get("service_name") not in (None, "")
        and row.get("attr.span_kind") in _SPAN_KINDS
        and row.get("attr.status_code") in _STATUS_CODES
        for row in rows
    )
    return {
        "source_rows": len(rows),
        "observed_min_ns": min(times) if times else None,
        "observed_max_ns": max(times) if times else None,
        "declared_start_ns": start_ns,
        "declared_end_ns": end_ns,
        "all_rows_in_declared_window": bool(times)
        and min(times) >= start_ns
        and max(times) < end_ns,
        "identity_valid": identity_valid,
        "span_identity_unique": len(identities) == len(set(identities)),
        "service_counts": dict(sorted(services.items())),
        "span_kind_counts": dict(sorted(kinds.items())),
        "status_code_counts": dict(sorted(statuses.items())),
        "edge_count": len(edge_set),
        "witness_count": witnesses,
        "edge_set_sha256": _edge_hash(edge_set),
        "edge_set": edge_set,
    }


def _source_signal_window_audit(
    paths: tuple[Path, ...],
    window: tuple[int, int],
) -> dict[str, object]:
    start_ns, end_ns = window[0] * 1_000_000_000, window[1] * 1_000_000_000
    files = {}
    for path in paths:
        parquet = pq.ParquetFile(path)
        minimum = None
        maximum = None
        rows_before_start = 0
        rows_at_or_after_end = 0
        rows_at_exact_end = 0
        rows_after_end = 0
        null_timestamps = 0
        for batch in parquet.iter_batches(columns=["time"], batch_size=100_000):
            if not batch.num_rows:
                continue
            batch_min = pc.min(batch.column(0)).value
            batch_max = pc.max(batch.column(0)).value
            minimum = batch_min if minimum is None else min(minimum, batch_min)
            maximum = batch_max if maximum is None else max(maximum, batch_max)
            for scalar in batch.column(0):
                if not scalar.is_valid:
                    null_timestamps += 1
                    continue
                value = scalar.value
                rows_before_start += value < start_ns
                rows_at_or_after_end += value >= end_ns
                rows_at_exact_end += value == end_ns
                rows_after_end += value > end_ns
        files[path.name] = {
            "source_rows": parquet.metadata.num_rows,
            "observed_min_ns": minimum,
            "observed_max_ns": maximum,
            "all_rows_in_declared_window": (
                null_timestamps == 0
                and (minimum is None or minimum >= start_ns and maximum < end_ns)
            ),
            "rows_before_start": rows_before_start,
            "rows_at_or_after_end": rows_at_or_after_end,
            "rows_at_exact_end": rows_at_exact_end,
            "rows_after_end": rows_after_end,
            "null_timestamps": null_timestamps,
        }
    return {
        "declared_start_ns": start_ns,
        "declared_end_ns": end_ns,
        "all_rows_in_declared_window": all(
            bool(item["all_rows_in_declared_window"]) for item in files.values()
        ),
        "files": files,
    }


def _edge_dicts(edges: Mapping[tuple[str, str], list[int]]) -> list[dict[str, object]]:
    return [
        {
            "src_type": "service",
            "src_id": source,
            "dst_type": "service",
            "dst_id": destination,
            "rel_type": "calls",
            "provenance": "trace",
            "request_count": counts[0],
            "error_count": counts[1],
        }
        for (source, destination), counts in sorted(edges.items())
    ]


def _metric_sample_audit(case: AegisTransferCase) -> dict[str, object]:
    values: dict[tuple[object, ...], set[tuple[object, ...]]] = defaultdict(set)
    protocol_keys: set[tuple[object, ...]] = set()
    names_by_kind: dict[str, set[str]] = {"gauge": set(), "sum": set(), "histogram": set()}
    source_rows = 0
    nonfinite_values = 0
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
                value = tuple(
                    _canonical_metric_value(item)
                    for item in (point.count, point.sum, point.min, point.max)
                )
                nonfinite_values += sum(
                    isinstance(item, float) and not math.isfinite(item)
                    for item in (point.sum, point.min, point.max)
                )
                protocol_keys.add((f"{identity[0]}_bucket", *identity[1:]))
                protocol_keys.add((f"{identity[0]}_count", *identity[1:]))
                if point.sum is not None:
                    protocol_keys.add((f"{identity[0]}_sum", *identity[1:]))
            else:
                key = ("number", *identity)
                value = (_canonical_metric_value(point.value),)
                nonfinite_values += not math.isfinite(point.value)
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
        "nonfinite_source_values": nonfinite_values,
        "nonfinite_protocol_representation": "source IEEE-754 double preserved in OTLP",
        "sum_temporality_available": False,
        "sum_monotonicity_available": False,
        "sum_protocol_representation": (
            "AGGREGATION_TEMPORALITY_UNSPECIFIED with is_monotonic=false because the source "
            "publishes neither field"
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
    }


def _iter_number_metrics(paths: tuple[Path, Path]) -> Iterator[NumberMetricPoint]:
    for row in _iter_rows(paths):
        value = float(row["value"])
        yield NumberMetricPoint(
            name=str(row["metric"]),
            time_unix_nano=int(row["time"]),
            value=value,
            service_name=_optional_string(row.get("service_name")),
            attributes=_attributes(row),
        )


def _combined_trace_identity_unique(paths: tuple[Path, Path]) -> bool:
    identities = [(str(row["trace_id"]), str(row["span_id"])) for row in _iter_rows(paths)]
    return len(identities) == len(set(identities))


def _iter_histograms(paths: tuple[Path, Path]) -> Iterator[HistogramMetricPoint]:
    for row in _iter_rows(paths):
        raw_count = float(row["count"])
        if not math.isfinite(raw_count) or raw_count < 0 or not raw_count.is_integer():
            raise AegisAuditError(f"invalid Aegis histogram count: {raw_count}")
        yield HistogramMetricPoint(
            name=str(row["metric"]),
            time_unix_nano=int(row["time"]),
            count=int(raw_count),
            sum=_optional_number(row.get("sum")),
            min=_optional_number(row.get("min")),
            max=_optional_number(row.get("max")),
            service_name=_optional_string(row.get("service_name")),
            attributes=_attributes(row),
        )


def _iter_logs(paths: tuple[Path, Path]) -> Iterator[LogRecord]:
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


def _iter_traces(paths: tuple[Path, Path]) -> Iterator[TraceSpan]:
    for row in _iter_rows(paths):
        kind_name = str(row["attr.span_kind"])
        status_name = str(row["attr.status_code"])
        try:
            kind = _SPAN_KINDS[kind_name]
            status = _STATUS_CODES[status_name]
        except KeyError as error:
            raise AegisAuditError(f"unknown Aegis trace enum: {error.args[0]}") from error
        start = int(row["time"])
        duration = int(row["duration"])
        if duration < 0:
            raise AegisAuditError(f"negative Aegis trace duration: {duration}")
        service_name = _optional_string(row.get("service_name"))
        if service_name is None:
            raise AegisAuditError("Aegis trace span has no service_name")
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
            attributes=_attributes(row, excluded={"span_kind", "status_code"}),
            scope_name="aegis-replay",
            scope_version=ADAPTER_REVISION,
        )


def _iter_rows(paths: tuple[Path, ...]) -> Iterator[dict[str, Any]]:
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=10_000):
            names = batch.schema.names
            for row_index in range(batch.num_rows):
                row: dict[str, Any] = {}
                for column_index, name in enumerate(names):
                    scalar = batch.column(column_index)[row_index]
                    if not scalar.is_valid:
                        row[name] = None
                    elif name == "time":
                        row[name] = scalar.value
                    else:
                        row[name] = scalar.as_py()
                yield row


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


def _combined_trace_distribution(source: dict[str, object], field: str) -> dict[str, int]:
    result: Counter[str] = Counter()
    windows = source["trace_windows"]
    if not isinstance(windows, dict):
        raise AegisAuditError("source trace audit is malformed")
    for window in windows.values():
        if not isinstance(window, dict) or not isinstance(window.get(field), dict):
            raise AegisAuditError("source trace distribution is malformed")
        result.update({str(key): int(value) for key, value in window[field].items()})
    return dict(sorted(result.items()))


def _group_counts(client: GreptimeClient, column: str) -> dict[str, int]:
    result = client.query(
        f'SELECT "{column}", COUNT(*) FROM traces GROUP BY "{column}" ORDER BY "{column}"',
        max_rows=None,
    )
    return {str(row[0]): int(row[1]) for row in result.rows}


def _table_count(client: GreptimeClient, table: str) -> int:
    identifier = table.replace('"', '""')
    result = client.query(f'SELECT COUNT(*) FROM "{identifier}"')
    return int(result.rows[0][0])


def _edge_hash(edges: list[dict[str, object]] | None) -> str | None:
    if edges is None:
        return None
    return hashlib.sha256(
        json.dumps(edges, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _valid_hex_id(value: str, byte_length: int) -> bool:
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return False
    return len(decoded) == byte_length and any(decoded)


def _meta_rows(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise AegisAuditError(f"missing publisher metadata: {path.name}")
    return pq.read_table(path).to_pylist()


def _unique_meta_row(path: Path, source_case: str) -> dict[str, object]:
    rows = [row for row in _meta_rows(path) if row.get("datapack") == source_case]
    if len(rows) != 1:
        raise AegisAuditError(f"publisher metadata does not uniquely identify {source_case}")
    return rows[0]


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AegisAuditError(f"cannot read source JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise AegisAuditError(f"expected JSON object in {path.name}")
    return value


def _json_object(value: object, field: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise AegisAuditError(f"invalid {field}") from error
    if not isinstance(decoded, dict):
        raise AegisAuditError(f"invalid {field}")
    return decoded


def _env_epoch(env: Mapping[str, object], key: str) -> int:
    try:
        return int(str(env[key]))
    except (KeyError, ValueError) as error:
        raise AegisAuditError(f"invalid {key} in source env") from error


def _optional_string(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _canonical_metric_value(value: object) -> object:
    if not isinstance(value, float) or math.isfinite(value):
        return value
    if math.isnan(value):
        return "NaN"
    return "+Inf" if value > 0 else "-Inf"


def _floor_minute(epoch: int) -> int:
    return epoch // 60 * 60


def _ceil_minute(epoch: int) -> int:
    return (epoch + 59) // 60 * 60


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _time_literal(epoch: int) -> str:
    return _literal(datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S"))
