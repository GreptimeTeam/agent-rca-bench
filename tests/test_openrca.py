import csv
from pathlib import Path

import pytest

import semantic_rca_bench.datasets.openrca as openrca_module
from semantic_rca_bench.contracts import IngestCounts, QueryResult
from semantic_rca_bench.datasets.openrca import (
    DEFAULT_MARKET_CASE,
    DEFAULT_TELECOM_CASE,
    MARKET_LOG_FILES,
    MARKET_METRIC_FILES,
    TELECOM_METRIC_FILES,
    OpenRCAError,
    OpenRCARepository,
    _iter_legacy_traces,
    _iter_traces,
    _legacy_metric_series,
    _metric_series,
    _parse_case_id,
    _task_window,
    source_audit,
    validate_ingest,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _bank_fixture(root: Path) -> None:
    bank = root / "Bank"
    _write_csv(
        bank / "query.csv",
        ["task_index", "instruction", "scoring_points"],
        [
            {
                "task_index": "task_6",
                "instruction": (
                    "On March 4, 2021, between 18:00 and 18:30, there was a single "
                    "failure observed in the system."
                ),
                "scoring_points": "hidden ground truth",
            },
            {
                "task_index": "task_6",
                "instruction": (
                    "On March 6, 2021, between 06:00 and 06:30, there was a single "
                    "failure observed in the system."
                ),
                "scoring_points": "different hidden ground truth",
            },
        ],
    )
    _write_csv(
        bank / "record.csv",
        ["level", "component", "timestamp", "datetime", "reason"],
        [
            {
                "level": "pod",
                "component": "Redis02",
                "timestamp": "1614852540.0",
                "datetime": "2021-03-04 18:09:00",
                "reason": "high memory usage",
            }
        ],
    )
    telemetry = bank / "telemetry" / "2021_03_04"
    _write_csv(
        telemetry / "metric" / "metric_app.csv",
        ["timestamp", "rr", "sr", "cnt", "mrt", "tc"],
        [
            {
                "timestamp": 1614852000,
                "rr": 100,
                "sr": 99,
                "cnt": 5,
                "mrt": 20,
                "tc": "ServiceTest1",
            }
        ],
    )
    _write_csv(
        telemetry / "metric" / "metric_container.csv",
        ["timestamp", "cmdb_id", "kpi_name", "value"],
        [
            {
                "timestamp": 1614852540,
                "cmdb_id": "Redis02",
                "kpi_name": "MEMUsedMemPerc",
                "value": 98,
            }
        ],
    )
    _write_csv(
        telemetry / "log" / "log_service.csv",
        ["log_id", "timestamp", "cmdb_id", "log_name", "value"],
        [
            {
                "log_id": "log-1",
                "timestamp": 1614852540,
                "cmdb_id": "Redis02",
                "log_name": "redis",
                "value": "memory pressure",
            }
        ],
    )
    _write_csv(
        telemetry / "trace" / "trace_span.csv",
        ["timestamp", "cmdb_id", "parent_id", "span_id", "trace_id", "duration"],
        [
            {
                "timestamp": 1614852540000,
                "cmdb_id": "Redis02",
                "parent_id": "",
                "span_id": "span-1",
                "trace_id": "trace-1",
                "duration": 19,
            }
        ],
    )


def test_bank_case_selector_uses_task_and_official_local_window(tmp_path: Path) -> None:
    _bank_fixture(tmp_path)

    case = OpenRCARepository(tmp_path).fetch_bank_case("task_6@2021-03-04T18:00")

    assert case.source_case == "Bank/task_6@2021-03-04T18:00"
    assert case.input.time_start == 1614852000
    assert case.input.time_end == 1614853800
    assert case.ground_truth.affected_component == "Redis02"
    assert case.ground_truth.fault_type == "high memory usage"
    assert case.ground_truth.inject_time == 1614852540


def test_unified_selector_accepts_canonical_bank_case_id(tmp_path: Path) -> None:
    _bank_fixture(tmp_path)

    case = OpenRCARepository(tmp_path).fetch_case("Bank/task_6@2021-03-04T18:00")

    assert case.source_case == "Bank/task_6@2021-03-04T18:00"


def test_bank_adapter_preserves_unknown_trace_semantics(tmp_path: Path) -> None:
    _bank_fixture(tmp_path)
    case = OpenRCARepository(tmp_path).fetch_bank_case("task_6@2021-03-04T18:00")

    span = next(_iter_traces(case))

    assert span.name == ""
    assert span.kind == 0
    assert span.status_code == 0
    assert span.start_time_unix_nano == span.end_time_unix_nano
    assert span.service_name == ""
    assert span.resource_attributes == {}
    assert span.attributes == {
        "openrca.cmdb_id": "Redis02",
        "openrca.duration": 19,
    }


def test_bank_metric_and_source_audit_count_protocol_expansion(tmp_path: Path) -> None:
    _bank_fixture(tmp_path)
    case = OpenRCARepository(tmp_path).fetch_bank_case("task_6@2021-03-04T18:00")

    series, source_rows = _metric_series(case)
    audit = source_audit(case)

    assert source_rows == 2
    assert sum(len(samples) for _, samples, _ in series) == 5
    assert next(labels for name, _, labels in series if name == "rr") == {"tc": "ServiceTest1"}
    assert audit["window_rows"] == {
        "metric_app_rows": 1,
        "metric_container_rows": 1,
        "log_rows": 1,
        "trace_rows": 1,
    }
    assert audit["trace_duration_unit"] == "unknown"
    assert audit["source_data_modified"] is False


def test_validate_ingest_does_not_query_absent_zero_row_signal_tables(
    tmp_path: Path, monkeypatch
) -> None:
    _bank_fixture(tmp_path)
    case = OpenRCARepository(tmp_path).fetch_bank_case("task_6@2021-03-04T18:00")
    queried_tables = []

    def fake_table_stats(client, table, time_column):
        queried_tables.append(table)
        return {"row_count": 1, "min_time": "start", "max_time": "end"}

    class FakeClient:
        def query(self, sql, max_rows=1_000):
            if "information_schema.table_semantics" in sql:
                return QueryResult(
                    query_id="semantics",
                    columns=["table_name", "signal_type", "source"],
                    rows=[["metric_table", "metric", "prometheus"]],
                    elapsed_seconds=0.0,
                )
            return QueryResult(
                query_id="relationships",
                columns=["relationship_count"],
                rows=[[0]],
                elapsed_seconds=0.0,
            )

    monkeypatch.setattr(openrca_module, "_database_table_stats", fake_table_stats)

    validation = validate_ingest(
        FakeClient(),
        case,
        IngestCounts(metric_unique_samples=1, log_records=0, trace_spans=0),
    )

    assert "logs" not in queried_tables
    assert "traces" not in queried_tables
    assert validation["database_counts"]["log_records"] == 0
    assert validation["database_counts"]["trace_spans"] == 0


def test_bank_case_selector_rejects_an_implicit_timezone() -> None:
    with pytest.raises(OpenRCAError, match="task_N@YYYY-MM-DDTHH:MM"):
        _parse_case_id("task_6")

    with pytest.raises(OpenRCAError, match="must not include a timezone offset"):
        _parse_case_id("task_6@2021-03-04T18:00+08:00")


def test_task_window_accepts_official_instruction_variants() -> None:
    assert _task_window(
        "On March 23, 2021, during the time range of 00:00 to 00:30, a failure occurred."
    ) == (1616428800, 1616430600)


def _legacy_index(root: Path, system: str, instruction: str, truth: dict[str, object]) -> Path:
    base = root / system
    _write_csv(
        base / "query.csv",
        ["task_index", "instruction", "scoring_points"],
        [{"task_index": "task_1", "instruction": instruction, "scoring_points": "hidden"}],
    )
    _write_csv(
        base / "record.csv",
        ["timestamp", "level", "component", "reason", "datetime"],
        [truth],
    )
    return base


def test_market_adapter_preserves_component_kinds_and_unknown_span_roles(
    tmp_path: Path,
) -> None:
    base = _legacy_index(
        tmp_path,
        "Market/cloudbed-1",
        "The system failed on March 21, 2022, from 03:30 to 04:00.",
        {
            "timestamp": 1647805154,
            "level": "node",
            "component": "node-6",
            "reason": "node disk write I/O consumption",
            "datetime": "2022-03-21 03:39:14",
        },
    )
    telemetry = base / "telemetry/2022_03_21"
    for name in MARKET_METRIC_FILES:
        path = telemetry / "metric" / name
        if name == "metric_service.csv":
            _write_csv(
                path,
                ["service", "timestamp", "rr", "sr", "mrt", "count"],
                [
                    {
                        "service": "adservice-grpc",
                        "timestamp": 1647805000,
                        "rr": 1,
                        "sr": 1,
                        "mrt": 2,
                        "count": 3,
                    }
                ],
            )
        else:
            _write_csv(
                path,
                ["timestamp", "cmdb_id", "kpi_name", "value"],
                [
                    {
                        "timestamp": 1647805000,
                        "cmdb_id": "node-6.adservice2-0",
                        "kpi_name": f"{path.stem}.value",
                        "value": 1,
                    }
                ],
            )
    for name in MARKET_LOG_FILES:
        _write_csv(
            telemetry / "log" / name,
            ["log_id", "timestamp", "cmdb_id", "log_name", "value"],
            [
                {
                    "log_id": name,
                    "timestamp": 1647805000,
                    "cmdb_id": "adservice2-0",
                    "log_name": "app",
                    "value": "message",
                }
            ],
        )
    _write_csv(
        telemetry / "trace/trace_span.csv",
        [
            "timestamp",
            "cmdb_id",
            "span_id",
            "trace_id",
            "duration",
            "type",
            "status_code",
            "operation_name",
            "parent_span",
        ],
        [
            {
                "timestamp": 1647805000000,
                "cmdb_id": "adservice2-0",
                "span_id": "1" * 16,
                "trace_id": "2" * 32,
                "duration": 25,
                "type": "rpc",
                "status_code": 0,
                "operation_name": "Call",
                "parent_span": "3" * 16,
            }
        ],
    )

    case = OpenRCARepository(tmp_path).fetch_case(DEFAULT_MARKET_CASE)
    series, source_rows = _legacy_metric_series(case)
    span = next(_iter_legacy_traces(case))

    assert case.variant == "market"
    assert case.ground_truth.affected_component == "node-6"
    assert source_rows == 5
    assert sum(len(samples) for _, samples, _ in series) == 8
    assert span.name == "Call"
    assert span.kind == 0
    assert span.service_name == ""
    assert span.start_time_unix_nano == span.end_time_unix_nano
    assert span.attributes["openrca.cmdb_id"] == "adservice2-0"


def test_telecom_adapter_keeps_millisecond_timestamps_and_no_log_modality(
    tmp_path: Path,
) -> None:
    base = _legacy_index(
        tmp_path,
        "Telecom",
        "The system failed on May 27, 2020, from 05:00 to 05:30.",
        {
            "timestamp": 1590527340,
            "level": "pod",
            "component": "docker_001",
            "reason": "CPU fault",
            "datetime": "2020-05-27 05:09:00",
        },
    )
    telemetry = base / "telemetry/2020_05_27"
    for name in TELECOM_METRIC_FILES:
        path = telemetry / "metric" / name
        if name == "metric_app.csv":
            _write_csv(
                path,
                ["serviceName", "startTime", "avg_time", "num", "succee_num", "succee_rate"],
                [
                    {
                        "serviceName": "osb_001",
                        "startTime": 1590527100000,
                        "avg_time": 1,
                        "num": 2,
                        "succee_num": 2,
                        "succee_rate": 1,
                    }
                ],
            )
        else:
            _write_csv(
                path,
                ["itemid", "name", "bomc_id", "timestamp", "value", "cmdb_id"],
                [
                    {
                        "itemid": "1",
                        "name": f"{path.stem}_value",
                        "bomc_id": "bomc",
                        "timestamp": 1590527100000,
                        "value": 1,
                        "cmdb_id": "docker_001",
                    }
                ],
            )
    _write_csv(
        telemetry / "trace/trace_span.csv",
        [
            "callType",
            "startTime",
            "elapsedTime",
            "success",
            "traceId",
            "id",
            "pid",
            "cmdb_id",
            "dsName",
            "serviceName",
        ],
        [
            {
                "callType": "JDBC",
                "startTime": 1590527100092,
                "elapsedTime": 2.0,
                "success": "True",
                "traceId": "trace",
                "id": "span",
                "pid": "None",
                "cmdb_id": "docker_001",
                "dsName": "db_003",
                "serviceName": "",
            }
        ],
    )

    case = OpenRCARepository(tmp_path).fetch_case(DEFAULT_TELECOM_CASE)
    series, source_rows = _legacy_metric_series(case)
    span = next(_iter_legacy_traces(case))

    assert case.variant == "telecom"
    assert case.log_paths == ()
    assert case.ground_truth.fault_type == "CPU fault"
    assert source_rows == 5
    assert sum(len(samples) for _, samples, _ in series) == 8
    assert span.start_time_unix_nano == 1590527100092 * 1_000_000
    assert span.end_time_unix_nano == span.start_time_unix_nano
    assert span.kind == 0
    assert span.attributes["openrca.elapsed_time"] == 2.0
