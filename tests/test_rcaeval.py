from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from semantic_rca_bench.contracts import CaseInput, GroundTruth, RCAEvalCase
from semantic_rca_bench.datasets import rcaeval
from semantic_rca_bench.datasets.rcaeval import RCAEvalError, _trace_span


class _Client:
    def create_database(self, database: str) -> None:
        assert database == "rcaeval_test"


def test_trace_mapping_requires_the_source_microsecond_invariant() -> None:
    row = {
        "traceID": "trace-1",
        "spanID": "span-1",
        "parentSpanID": "",
        "operationName": "GET /cart",
        "methodName": "GET",
        "serviceName": "cartservice",
        "startTimeMillis": 1_705_353_846_065,
        "startTime": 1_705_353_846_065_999,
        "duration": 4_133,
        "statusCode": 0,
    }

    span = _trace_span(row)

    assert span.start_time_unix_nano == 1_705_353_846_065_999_000
    assert span.end_time_unix_nano == 1_705_353_846_070_132_000

    row["startTimeMillis"] += 1
    with pytest.raises(RCAEvalError, match="microsecond representation"):
        _trace_span(row)


def test_metric_ingest_audits_duplicate_and_conflicting_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = tmp_path / "metrics.parquet"
    pq.write_table(
        pa.table({"time": [100, 100, 101], "cartservice_cpu": [1.0, 2.0, None]}),
        metrics,
    )
    case = RCAEvalCase(
        source_case="case",
        dataset="RE2-OB",
        system="ob",
        root=tmp_path,
        input=CaseInput(
            case_token="case",
            database="rcaeval_test",
            time_start=100,
            time_end=101,
            alert_time=101,
        ),
        ground_truth=GroundTruth(
            affected_component="cartservice", fault_type="cpu", inject_time=101
        ),
        metrics_path=metrics,
        logs_path=None,
        traces_path=None,
    )
    monkeypatch.setattr(rcaeval, "write_series", lambda *args, **kwargs: None)

    counts = rcaeval.ingest_case(_Client(), case)  # type: ignore[arg-type]

    assert counts.metric_source_rows == 3
    assert counts.metrics_samples == 2
    assert counts.metric_unique_samples == 1
    assert counts.metric_duplicate_samples == 1
    assert counts.metric_conflicting_timestamps == 1


def test_fault_taxonomy_is_scoped_to_the_selected_dataset(tmp_path: Path) -> None:
    pq.write_table(
        pa.table(
            {
                "case": ["re2_cpu", "re2_delay", "re3_f1"],
                "dataset": ["RE2-OB", "RE2-OB", "RE3"],
                "fault": ["cpu", "delay", "f1"],
                "system": ["ob", "ob", "re3"],
                "inject_time": [150, 150, 150],
                "time_start": [100, 100, 100],
                "time_end": [200, 200, 200],
                "root_cause_service": ["checkout", "checkout", "frontend"],
            }
        ),
        tmp_path / "cases.parquet",
    )
    case_root = tmp_path / "re2_cpu"
    case_root.mkdir()
    pq.write_table(pa.table({"time": [100]}), case_root / "metrics.parquet")
    (case_root / "inject_time.txt").write_text("150")

    case = rcaeval.RCAEvalRepository(tmp_path).fetch_case("re2_cpu")

    assert case.input.fault_taxonomy == ["cpu", "delay"]
