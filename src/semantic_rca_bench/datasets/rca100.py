from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from semantic_rca_bench.contracts import (
    CaseInput,
    FaultCategory,
    GroundTruth,
    IngestCounts,
    RCA100Case,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.protocols.loki import LogRecord, write_logs
from semantic_rca_bench.protocols.otlp import OtlpTraceWriter, TraceSpan
from semantic_rca_bench.protocols.prometheus import (
    prometheus_metric_name,
    write_series_batch,
)

DATASET_REVISION = "v1.1"
SOURCE_REVISION = "69cf36430b43024d02530c610b1a4738b5c9a7fb"
DATA_BASE_URL = (
    f"https://aiops-benchmark.oss-cn-hongkong.aliyuncs.com/rca/rca100/{DATASET_REVISION}"
)
SOURCE_BASE_URL = (
    "https://www.aiops.cn/gitlab/aiops-live-benchmark/agenticopseval/-/raw/"
    f"{SOURCE_REVISION}/RCA100"
)
CASE_FILES = (
    "task.json",
    "metrics.parquet",
    "logs.parquet",
    "traces.parquet",
    "events.parquet",
    "alerts.parquet",
    "topology.json",
)


class RCA100Error(RuntimeError):
    pass


class RCA100Repository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_case(self, task_id: str) -> RCA100Case:
        if not task_id.startswith("t") or len(task_id) != 4 or not task_id[1:].isdigit():
            raise RCA100Error(f"invalid RCA100 task id: {task_id}")
        root = self.cache_dir / task_id
        root.mkdir(parents=True, exist_ok=True)
        for filename in CASE_FILES:
            self._download(f"{DATA_BASE_URL}/cases/{task_id}/{filename}", root / filename)
        answer_path = self.cache_dir / "answer_key" / f"{task_id}.gt.json"
        taxonomy_path = self.cache_dir / "answer_key" / "taxonomy.json"
        self._download(f"{SOURCE_BASE_URL}/answer_key/{task_id}.gt.json", answer_path)
        self._download(f"{SOURCE_BASE_URL}/answer_key/taxonomy.json", taxonomy_path)
        return _load_case(root, answer_path, taxonomy_path)

    @staticmethod
    def _download(url: str, target: Path) -> None:
        if target.is_file() and target.stat().st_size:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f"{target.suffix}.part")
        with (
            httpx.Client(follow_redirects=True, timeout=120, trust_env=False) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        partial.replace(target)


def _load_case(root: Path, answer_path: Path, taxonomy_path: Path) -> RCA100Case:
    task = _read_json(root / "task.json")
    ground_truth = _read_json(answer_path)
    taxonomy = _read_json(taxonomy_path)
    task_id = str(task.get("task_id") or root.name)
    window = task.get("alert_window")
    if not isinstance(window, Mapping):
        raise RCA100Error(f"missing alert_window in {task_id}")
    time_start = _iso_to_epoch_seconds(str(window.get("start") or ""))
    time_end = _iso_to_epoch_seconds(str(window.get("end") or ""))
    alert_time = _first_alert_time(root / "alerts.parquet")

    components = ground_truth.get("root_cause_entities")
    fault_types = ground_truth.get("root_cause_types")
    if not isinstance(components, list) or len(components) != 1:
        raise RCA100Error(f"{task_id} does not have one root-cause entity")
    if not isinstance(fault_types, list) or len(fault_types) != 1:
        raise RCA100Error(f"{task_id} does not have one root-cause type")
    component_scoreable, component_alternatives = _component_contract(
        str(components[0]),
        ground_truth.get("raw_ground_truth"),
    )
    fault_type = str(fault_types[0])
    return RCA100Case(
        source_case=task_id,
        dataset="RCA100-v1.1",
        system="OpenTelemetry Demo Store",
        root=root,
        input=CaseInput(
            case_token=task_id,
            time_start=time_start,
            time_end=time_end,
            alert_time=alert_time,
            alert_text=str(task.get("prompt_text") or task.get("alert_title") or ""),
            fault_taxonomy=_fault_taxonomy(taxonomy),
        ),
        ground_truth=GroundTruth(
            causal_component=str(components[0]),
            component_scoreable=component_scoreable,
            component_alternatives=component_alternatives,
            fault_type=fault_type,
            fault_category=_fault_category(fault_type),
            inject_time=None,
        ),
        metrics_path=root / "metrics.parquet",
        logs_path=root / "logs.parquet",
        traces_path=root / "traces.parquet",
        events_path=root / "events.parquet",
        alerts_path=root / "alerts.parquet",
        topology_path=root / "topology.json",
    )


def ingest_case(client: GreptimeClient, case: RCA100Case) -> IngestCounts:
    client.create_database(case.input.database)
    counts = IngestCounts()
    metric_series = _metric_series(case.metrics_path)
    metric_audit = _metric_sample_audit(
        metric_series,
        source_rows=pq.ParquetFile(case.metrics_path).metadata.num_rows,
    )
    write_series_batch(client, case.input.database, metric_series)
    counts.metric_source_rows = metric_audit["source_rows"]
    counts.metrics_samples = metric_audit["valid_samples"]
    counts.metric_unique_samples = metric_audit["unique_samples"]
    counts.metric_duplicate_samples = metric_audit["duplicate_samples"]
    counts.metric_conflicting_timestamps = metric_audit["conflicting_timestamps"]

    counts.log_records = write_logs(
        client,
        case.input.database,
        _iter_logs(case.logs_path),
    )
    counts.event_records = write_logs(
        client,
        case.input.database,
        _iter_events(case.events_path),
        table="events",
    )
    counts.alert_records = write_logs(
        client,
        case.input.database,
        _iter_alerts(case.alerts_path),
        table="alerts",
    )

    writer = OtlpTraceWriter(client, case.input.database)
    counts.trace_spans = writer.write(_iter_traces(case.traces_path))
    counts.rejected_trace_spans = writer.rejected_spans
    counts.remapped_trace_ids = writer.stats.remapped_trace_ids
    counts.remapped_span_ids = writer.stats.remapped_span_ids
    return counts


def validate_ingest(
    client: GreptimeClient,
    case: RCA100Case,
    counts: IngestCounts,
) -> dict[str, object]:
    semantics = client.query(
        f"""
        SELECT table_name, signal_type
        FROM information_schema.table_semantics
        WHERE table_schema = '{case.input.database}'
        ORDER BY table_name
        """,
        max_rows=500,
    )
    metric_tables = [str(row[0]) for row in semantics.rows if row[1] == "metric"]
    metric_stats = [
        _database_table_stats(client, table, "greptime_timestamp") for table in metric_tables
    ]
    database_counts = {
        "metric_samples": sum(int(stats["row_count"]) for stats in metric_stats),
        "log_records": _database_table_stats(client, "logs", "greptime_timestamp")["row_count"],
        "event_records": _database_table_stats(client, "events", "greptime_timestamp")["row_count"],
        "alert_records": _database_table_stats(client, "alerts", "greptime_timestamp")["row_count"],
        "trace_spans": _database_table_stats(client, "traces", "timestamp")["row_count"],
    }
    expected_counts = {
        "metric_samples": counts.metric_unique_samples,
        "log_records": counts.log_records,
        "event_records": counts.event_records,
        "alert_records": counts.alert_records,
        "trace_spans": counts.trace_spans,
    }
    graph_pairs = _derived_service_calls(client, case.input)
    reference_pairs = _reference_service_calls(case.topology_path)
    return {
        "database_counts": database_counts,
        "expected_protocol_counts": expected_counts,
        "protocol_row_counts_match": database_counts == expected_counts,
        "metric_table_count": len(metric_tables),
        "metric_time_range": {
            "min": min(int(stats["min_time"]) for stats in metric_stats),
            "max": max(int(stats["max_time"]) for stats in metric_stats),
        },
        "derived_service_calls": [list(pair) for pair in sorted(graph_pairs)],
        "reference_service_calls": [list(pair) for pair in sorted(reference_pairs)],
        "reference_service_calls_found": [
            list(pair) for pair in sorted(graph_pairs & reference_pairs)
        ],
        "reference_service_calls_missing": [
            list(pair) for pair in sorted(reference_pairs - graph_pairs)
        ],
        "derived_service_calls_not_in_reference": [
            list(pair) for pair in sorted(graph_pairs - reference_pairs)
        ],
        "derived_calls_nonempty": bool(graph_pairs),
        "reference_topology_ingested": False,
    }


def reference_topology_summary(path: Path) -> dict[str, object]:
    topology = _read_json(path)
    entities = topology.get("entities")
    edges = topology.get("edges")
    if not isinstance(entities, list) or not isinstance(edges, list):
        raise RCA100Error("invalid topology snapshot")
    return {
        "validation_only": True,
        "ingested": False,
        "entity_count": len(entities),
        "edge_count": len(edges),
        "calls_edge_count": sum(edge.get("relation") == "calls" for edge in edges),
    }


def _metric_series(
    path: Path,
) -> list[tuple[str, list[tuple[int, float]], dict[str, str]]]:
    groups: dict[
        tuple[str, tuple[tuple[str, str], ...]],
        list[tuple[int, float]],
    ] = defaultdict(list)
    source_names: dict[str, str] = {}
    for row in _iter_rows(path):
        source_name = str(row.get("metric") or "")
        metric_name = prometheus_metric_name(source_name)
        previous = source_names.setdefault(metric_name, source_name)
        if previous != source_name:
            raise RCA100Error(
                f"metric name collision after normalization: {previous}, {source_name}"
            )
        value = row.get("value")
        if value is None or not math.isfinite(float(value)):
            continue
        labels = {
            key: str(row[key])
            for key in (
                "domain",
                "entity_set",
                "entity_id",
                "entity_name",
                "metric_set_id",
                "service",
            )
            if row.get(key) not in (None, "")
        }
        key = metric_name, tuple(sorted(labels.items()))
        groups[key].append((int(row["time"]) // 1_000, float(value)))
    return [
        (metric_name, sorted(samples), dict(labels))
        for (metric_name, labels), samples in groups.items()
    ]


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


def _derived_service_calls(client: GreptimeClient, case_input: CaseInput) -> set[tuple[str, str]]:
    start = datetime.fromtimestamp(case_input.time_start, UTC).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(case_input.time_end, UTC).strftime("%Y-%m-%d %H:%M:%S")
    result = client.query(
        f"""
        SELECT DISTINCT src_id, dst_id
        FROM greptime_private.semantic_relationships
        WHERE observed_at >= '{start}' AND observed_at < '{end}'
          AND src_type = 'service' AND dst_type = 'service' AND rel_type = 'calls'
        ORDER BY src_id, dst_id
        """,
        max_rows=500,
    )
    return {(str(row[0]), str(row[1])) for row in result.rows}


def _reference_service_calls(path: Path) -> set[tuple[str, str]]:
    topology = _read_json(path)
    entities = topology.get("entities")
    edges = topology.get("edges")
    if not isinstance(entities, list) or not isinstance(edges, list):
        raise RCA100Error("invalid topology snapshot")
    entity_by_id = {
        str(entity["id"]): entity
        for entity in entities
        if isinstance(entity, Mapping) and entity.get("id")
    }
    calls: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, Mapping) or edge.get("relation") != "calls":
            continue
        source = entity_by_id.get(str(edge.get("src")))
        target = entity_by_id.get(str(edge.get("dst")))
        if not source or not target:
            continue
        if source.get("type") == "apm.service" and target.get("type") == "apm.service":
            calls.add((str(source.get("name")), str(target.get("name"))))
    return calls


def _iter_logs(path: Path) -> Iterator[LogRecord]:
    for row in _iter_rows(path):
        labels = _labels(
            row,
            {
                "container_name": "_container_name_",
                "pod_name": "_pod_name_",
                "pod_uid": "_pod_uid_",
                "namespace": "_namespace_",
                "node_name": "__tag__:_node_name_",
                "node_ip": "__tag__:_node_ip_",
                "source": "_source_",
            },
        )
        yield LogRecord(
            timestamp=_iso_to_nanoseconds(str(row["_time_"])),
            message=str(row.get("content") or ""),
            labels=labels,
        )


def _iter_events(path: Path) -> Iterator[LogRecord]:
    for row in _iter_rows(path):
        payload = _parse_json(str(row.get("eventId") or "{}"), "eventId")
        timestamp = payload.get("lastTimestamp") or payload.get("eventTime")
        if not timestamp:
            metadata = payload.get("metadata")
            if isinstance(metadata, Mapping):
                timestamp = metadata.get("creationTimestamp")
        if not timestamp:
            raise RCA100Error("event has no timestamp")
        yield LogRecord(
            timestamp=_iso_to_nanoseconds(str(timestamp)),
            message=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            labels=_labels(
                row,
                {
                    "pod_name": "pod_name",
                    "pod_uid": "pod_id",
                    "host_name": "hostname",
                    "cluster_id": "clusterId",
                    "cluster_name": "clusterName",
                    "level": "level",
                },
            ),
        )


def _iter_alerts(path: Path) -> Iterator[LogRecord]:
    for row in _iter_rows(path):
        message = {
            key: row.get(key)
            for key in ("subject", "status", "severity", "resource", "labels", "annotations")
        }
        yield LogRecord(
            timestamp=int(row["time_s"]),
            message=json.dumps(message, separators=(",", ":"), ensure_ascii=False),
            labels=_labels(
                row,
                {
                    "subject": "subject",
                    "status": "status",
                    "severity": "severity",
                    "source_type": "sourcetype",
                },
            ),
        )


def _iter_traces(path: Path) -> Iterator[TraceSpan]:
    for row in _iter_rows(path):
        resources = _parse_json_object(row.get("resources"), "resources")
        attributes = _parse_json_object(row.get("attributes"), "attributes")
        raw_events = row.get("events")
        events: tuple[Mapping[str, object], ...] = ()
        if raw_events:
            parsed_events = _parse_json(str(raw_events), "events")
            if not isinstance(parsed_events, list) or not all(
                isinstance(event, Mapping) for event in parsed_events
            ):
                raise RCA100Error("trace events must be a list of objects")
            events = tuple(parsed_events)
        kind = int(row.get("kind") or 0)
        if kind not in range(6):
            raise RCA100Error(f"invalid OTel span kind: {kind}")
        start_ns = int(row.get("startTime") or 0)
        end_ns = int(row.get("endTime") or 0)
        yield TraceSpan(
            trace_id=str(row.get("traceId") or ""),
            span_id=str(row.get("spanId") or ""),
            parent_span_id=str(row.get("parentSpanId") or ""),
            name=str(row.get("spanName") or "unknown"),
            kind=kind,
            start_time_unix_nano=start_ns,
            end_time_unix_nano=end_ns,
            service_name=str(row.get("serviceName") or "unknown"),
            status_code=int(row.get("statusCode") or 0),
            status_message=str(row.get("statusMessage") or ""),
            resource_attributes=resources,
            attributes=attributes,
            events=events,
            scope_name="rca100-replay",
            scope_version=DATASET_REVISION,
        )


def _iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=10_000):
        yield from pa.Table.from_batches([batch]).to_pylist()


def _labels(row: Mapping[str, object], fields: Mapping[str, str]) -> dict[str, str]:
    return {
        label: str(row[source])
        for label, source in fields.items()
        if row.get(source) not in (None, "")
    }


def _first_alert_time(path: Path) -> int:
    values = pq.read_table(path, columns=["time_s"])["time_s"]
    value = pc.min(values).as_py()
    if value is None:
        raise RCA100Error("case has no alert timestamp")
    return int(value)


def _fault_taxonomy(taxonomy: Mapping[str, object]) -> list[str]:
    definitions = taxonomy.get("fault_definitions")
    if not isinstance(definitions, Mapping):
        raise RCA100Error("invalid RCA100 taxonomy")
    return sorted({str(key).split("-", 1)[-1] for key in definitions})


def _component_contract(component: str, raw_ground_truth: object) -> tuple[bool, list[str]]:
    if not isinstance(raw_ground_truth, str) or not raw_ground_truth:
        return True, []
    raw = _parse_json_object(raw_ground_truth, "raw_ground_truth")
    outcome = raw.get("outcome")
    if not isinstance(outcome, Mapping):
        return True, []
    targets = outcome.get("target_entities")
    if not isinstance(targets, list):
        return True, []
    names = [
        str(target["entity_name"])
        for target in targets
        if isinstance(target, Mapping) and target.get("entity_name") not in (None, "")
    ]
    component_key = re.sub(r"[^a-z0-9]", "", component.lower())
    alternatives = list(
        dict.fromkeys(
            name for name in names if re.sub(r"[^a-z0-9]", "", name.lower()) != component_key
        )
    )
    return not alternatives, alternatives


def _fault_category(fault_type: str) -> FaultCategory:
    normalized = fault_type.lower()
    if "cpu" in normalized:
        return FaultCategory.CPU
    if "memory" in normalized or "oom" in normalized or "gc" in normalized:
        return FaultCategory.MEMORY
    if "disk" in normalized or "iohigh" in normalized:
        return FaultCategory.DISK
    if "latency" in normalized or normalized in {"slowsql", "slowresource"}:
        return FaultCategory.DELAY
    return FaultCategory.OTHER


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RCA100Error(f"expected JSON object in {path}")
    return value


def _parse_json(value: str, field: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise RCA100Error(f"invalid JSON in {field}: {error}") from error


def _parse_json_object(value: object, field: str) -> dict[str, object]:
    if value in (None, ""):
        return {}
    parsed = _parse_json(str(value), field)
    if not isinstance(parsed, dict):
        raise RCA100Error(f"{field} must be a JSON object")
    return parsed


def _iso_to_epoch_seconds(value: str) -> int:
    return _iso_to_nanoseconds(value) // 1_000_000_000


def _iso_to_nanoseconds(value: str) -> int:
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?",
        value,
    )
    if not match:
        raise RCA100Error(f"invalid ISO timestamp: {value}")
    base, fraction, zone = match.groups()
    zone = "+00:00" if zone in (None, "Z") else zone
    if len(zone) == 5:
        zone = f"{zone[:3]}:{zone[3:]}"
    parsed = datetime.fromisoformat(f"{base}{zone}")
    seconds = int(parsed.timestamp())
    fraction_ns = int(((fraction or "") + "000000000")[:9])
    return seconds * 1_000_000_000 + fraction_ns
