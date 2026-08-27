import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from semantic_rca_bench.contracts import IngestCounts, QueryResult
from semantic_rca_bench.datasets.openrca2 import (
    _iter_histograms,
    _iter_traces,
    _load_case,
    _metric_sample_audit,
    source_audit,
    validate_ingest,
)


def _write_parquet(path: Path, values: dict[str, list[object]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pydict(values, schema=schema), path)


def _fixture(root: Path) -> Path:
    normal_start = 1_700_000_000
    abnormal_start = normal_start + 300
    abnormal_end = abnormal_start + 300
    manifest = root.parent.parent / "manifest.jsonl"
    entries = [
        {
            "name": root.name,
            "system": "otel-demo",
            "root_services": ["shipping"],
            "primary_kind": "NetworkDelay",
            "hybrid": False,
        },
        {
            "name": "otel-demo-pod-kill",
            "system": "otel-demo",
            "root_services": ["payment"],
            "primary_kind": "PodKill",
            "hybrid": False,
        },
        {
            "name": "train-ticket-fault",
            "system": "ts",
            "root_services": ["order"],
            "primary_kind": "JVMCPUStress",
            "hybrid": False,
        },
        {
            "name": "otel-demo-hybrid",
            "system": "otel-demo",
            "root_services": ["frontend"],
            "primary_kind": "hybrid",
            "hybrid": True,
        },
    ]
    manifest.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    (root / "env.json").write_text(
        json.dumps(
            {
                "NORMAL_START": str(normal_start),
                "NORMAL_END": str(abnormal_start),
                "ABNORMAL_START": str(abnormal_start),
                "ABNORMAL_END": str(abnormal_end),
            }
        )
    )
    (root / "injection.json").write_text(
        json.dumps(
            {
                "fault_type": "NetworkDelay",
                "state": "inject_success",
                "start_time": "2023-11-14T22:18:20+00:00",
                "engine_config": [
                    {
                        "app": "shipping",
                        "target_service": "quote",
                        "direction": "to",
                    }
                ],
            }
        )
    )
    (root / "causal_graph.json").write_text(
        json.dumps({"nodes": [{"component": "service|shipping"}], "edges": []})
    )
    _write_parquet(
        root / "conclusion.parquet",
        {"Issues": [json.dumps({"entrance_unreachable": {}})]},
        pa.schema([("Issues", pa.large_string())]),
    )

    metric_schema = pa.schema(
        [
            ("time", pa.timestamp("ns", tz="UTC")),
            ("metric", pa.large_string()),
            ("value", pa.float64()),
            ("service_name", pa.large_string()),
            ("attr.destination", pa.large_string()),
        ]
    )
    sum_schema = metric_schema
    histogram_schema = pa.schema(
        [
            ("time", pa.timestamp("ns", tz="UTC")),
            ("metric", pa.large_string()),
            ("service_name", pa.large_string()),
            ("count", pa.float64()),
            ("sum", pa.float64()),
            ("min", pa.float64()),
            ("max", pa.float64()),
            ("attr.destination", pa.large_string()),
        ]
    )
    log_schema = pa.schema(
        [
            ("time", pa.timestamp("ns", tz="UTC")),
            ("service_name", pa.large_string()),
            ("message", pa.large_string()),
        ]
    )
    trace_schema = pa.schema(
        [
            ("time", pa.timestamp("ns", tz="UTC")),
            ("trace_id", pa.large_string()),
            ("span_id", pa.large_string()),
            ("parent_span_id", pa.large_string()),
            ("span_name", pa.large_string()),
            ("attr.span_kind", pa.large_string()),
            ("service_name", pa.large_string()),
            ("duration", pa.uint64()),
            ("attr.status_code", pa.large_string()),
            ("attr.http.request.method", pa.large_string()),
        ]
    )
    for period, timestamp in (
        ("normal", normal_start * 1_000_000_000 + 1),
        ("abnormal", abnormal_start * 1_000_000_000 + 1),
    ):
        gauge_values = [1.0, 2.0] if period == "normal" else [3.0]
        gauge_times = [timestamp] * len(gauge_values)
        _write_parquet(
            root / f"{period}_metrics.parquet",
            {
                "time": gauge_times,
                "metric": ["system.memory.usage"] * len(gauge_values),
                "value": gauge_values,
                "service_name": ["shipping"] * len(gauge_values),
                "attr.destination": ["quote"] * len(gauge_values),
            },
            metric_schema,
        )
        _write_parquet(
            root / f"{period}_metrics_sum.parquet",
            {
                "time": [timestamp + 2],
                "metric": ["system.memory.usage"],
                "value": [4.0],
                "service_name": ["shipping"],
                "attr.destination": ["quote"],
            },
            sum_schema,
        )
        _write_parquet(
            root / f"{period}_metrics_histogram.parquet",
            {
                "time": [timestamp + 3],
                "metric": ["request.duration"],
                "service_name": ["shipping"],
                "count": [2.0],
                "sum": [8.0],
                "min": [1.0],
                "max": [7.0],
                "attr.destination": ["quote"],
            },
            histogram_schema,
        )
        _write_parquet(
            root / f"{period}_logs.parquet",
            {"time": [], "service_name": [], "message": []},
            log_schema,
        )
        _write_parquet(
            root / f"{period}_traces.parquet",
            {
                "time": [timestamp + 4],
                "trace_id": ["01" * 16],
                "span_id": ["02" * 8],
                "parent_span_id": [""],
                "span_name": ["POST quote"],
                "attr.span_kind": ["Client"],
                "service_name": ["shipping"],
                "duration": [800_000_000],
                "attr.status_code": ["Ok"],
                "attr.http.request.method": ["POST"],
            },
            trace_schema,
        )
    return manifest


def test_openrca2_case_uses_system_scoped_taxonomy_and_native_alert(tmp_path: Path) -> None:
    root = tmp_path / "cases" / "otel-demo3-shipping-delay-m6fhpx"
    root.mkdir(parents=True)
    manifest = _fixture(root)

    case = _load_case(root, manifest)

    assert case.input.time_end - case.input.time_start == 600
    assert case.input.alert_time - case.input.time_start == 300
    assert case.input.alert_text == "OpenTelemetry Demo alert: entrance_unreachable"
    assert case.input.fault_taxonomy == ["NetworkDelay", "PodKill"]
    assert case.ground_truth.component == "shipping"
    assert case.ground_truth.fault_type == "NetworkDelay"


def test_openrca2_audit_preserves_source_defects_and_native_trace_semantics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cases" / "otel-demo3-shipping-delay-m6fhpx"
    root.mkdir(parents=True)
    case = _load_case(root, _fixture(root))

    metric_audit = _metric_sample_audit(case)
    audit = source_audit(case)
    histogram = next(_iter_histograms(case.histogram_paths))
    span = next(_iter_traces(case.traces_paths))

    assert metric_audit["source_rows"] == 7
    assert metric_audit["duplicate_samples"] == 1
    assert metric_audit["conflicting_timestamps"] == 1
    assert metric_audit["type_name_collisions"] == ["system.memory.usage"]
    assert histogram.count == 2
    assert histogram.attributes == {"destination": "quote"}
    assert span.kind == 3
    assert span.status_code == 1
    assert span.end_time_unix_nano - span.start_time_unix_nano == 800_000_000
    assert span.attributes == {
        "http.request.method": "POST",
    }
    assert audit["source_data_modified"] is False
    assert audit["graph_applicability"] == "relational"


class _ValidationClient:
    def query(self, statement: str, *, max_rows: int | None = 200) -> QueryResult:
        if "information_schema.table_semantics" in statement:
            columns = ["table_name", "signal_type", "source"]
            rows = [
                ["greptime_otel_resource_info", "metric", "opentelemetry"],
                ["request_duration", "metric", "opentelemetry"],
                ["traces", "trace", "opentelemetry"],
            ]
        elif "greptime_otel_resource_info" in statement:
            columns, rows = ["count"], [[7]]
        elif '"request_duration"' in statement:
            columns, rows = ["count"], [[3]]
        elif '"traces"' in statement:
            columns, rows = ["count"], [[2]]
        elif "semantic_relationships" in statement:
            columns, rows = ["src_id", "dst_id"], [["shipping", "quote"]]
        else:
            raise AssertionError(statement)
        return QueryResult(
            query_id="q",
            columns=columns,
            rows=rows,
            elapsed_seconds=0,
        )


def test_openrca2_validation_excludes_generated_resource_descriptor_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cases" / "otel-demo3-shipping-delay-m6fhpx"
    root.mkdir(parents=True)
    case = _load_case(root, _fixture(root))

    result = validate_ingest(
        _ValidationClient(),  # type: ignore[arg-type]
        case,
        IngestCounts(metric_protocol_rows=3, trace_spans=2),
    )

    assert result["database_counts"]["metric_rows"] == 3
    assert result["resource_descriptor_rows"] == 7
    assert result["protocol_row_counts_match"] is True
    assert result["fault_endpoint_call_found"] is True
