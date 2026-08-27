import csv
from pathlib import Path

import pytest

from semantic_rca_bench.datasets.openrca import (
    OpenRCAError,
    OpenRCARepository,
    _iter_traces,
    _metric_series,
    _parse_case_id,
    _task_window,
    source_audit,
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
    assert case.ground_truth.component == "Redis02"
    assert case.ground_truth.fault_type == "high memory usage"
    assert case.ground_truth.inject_time == 1614852540


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
    assert next(labels for name, _, labels in series if name == "rr") == {
        "tc": "ServiceTest1"
    }
    assert audit["window_rows"] == {
        "metric_app_rows": 1,
        "metric_container_rows": 1,
        "log_rows": 1,
        "trace_rows": 1,
    }
    assert audit["trace_duration_unit"] == "unknown"
    assert audit["source_data_modified"] is False


def test_bank_case_selector_rejects_an_implicit_timezone() -> None:
    with pytest.raises(OpenRCAError, match="task_N@YYYY-MM-DDTHH:MM"):
        _parse_case_id("task_6")

    with pytest.raises(OpenRCAError, match="must not include a timezone offset"):
        _parse_case_id("task_6@2021-03-04T18:00+08:00")


def test_task_window_accepts_official_instruction_variants() -> None:
    assert _task_window(
        "On March 23, 2021, during the time range of 00:00 to 00:30, a failure occurred."
    ) == (1616428800, 1616430600)
