import json

import pytest

from semantic_rca_bench.measurement_summary import summarize_benchmark


def _cell(repetition, visibility, *, success, rows, calls, tokens):
    return {
        "repetition": repetition,
        "evaluation": {
            "success": success,
            "rows_returned_through_evidence": rows,
            "tool_calls_through_evidence": calls,
        },
        "run": {
            "visibility": visibility,
            "usage": {"input_tokens": tokens, "output_tokens": 0},
        },
    }


def _report(source_case, protocol, pairs):
    runs = []
    for repetition, (baseline, treatment) in enumerate(pairs):
        runs.extend(
            [
                _cell(repetition, "raw", **baseline),
                _cell(repetition, "table_semantics", **treatment),
            ]
        )
    return {
        "case_role": "measurement",
        "model": "model",
        "protocol": protocol,
        "repetitions": len(pairs),
        "runner": "runner",
        "runs": runs,
        "source_case": source_case,
    }


def test_summary_separates_run_pairs_from_case_level_inference(tmp_path) -> None:
    protocol = {"version": 1}
    first = _report(
        "case-a",
        protocol,
        [
            (
                {"success": True, "rows": 100, "calls": 4, "tokens": 1000},
                {"success": True, "rows": 90, "calls": 3, "tokens": 900},
            ),
            (
                {"success": True, "rows": 100, "calls": 4, "tokens": 1000},
                {"success": True, "rows": 80, "calls": 2, "tokens": 800},
            ),
        ],
    )
    second = _report(
        "case-b",
        protocol,
        [
            (
                {"success": True, "rows": 100, "calls": 4, "tokens": 1000},
                {"success": True, "rows": 70, "calls": 3, "tokens": 700},
            ),
            (
                {"success": False, "rows": 100, "calls": 4, "tokens": 1000},
                {"success": True, "rows": 60, "calls": 2, "tokens": 600},
            ),
        ],
    )
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path, report in zip(paths, (first, second), strict=True):
        path.write_text(json.dumps(report))

    summary = summarize_benchmark(
        paths,
        expected_protocol=protocol,
        expected_source_cases={"case-a", "case-b"},
        baseline="raw",
        treatment="table_semantics",
        metrics=("rows_returned_through_evidence", "reported_model_tokens"),
    )

    run_rows = summary["run_pair_descriptive"]["successful_pair_efficiency"][
        "rows_returned_through_evidence"
    ]
    case_rows = summary["case_level_inference"]["successful_pair_efficiency"][
        "rows_returned_through_evidence"
    ]
    assert run_rows == {
        "observations": 3,
        "better": 3,
        "worse": 0,
        "ties": 0,
        "median_delta": -20.0,
        "exact_two_sided_sign_test_p_value": 0.25,
    }
    assert case_rows == {
        "observations": 2,
        "better": 2,
        "worse": 0,
        "ties": 0,
        "median_delta": -22.5,
        "exact_two_sided_sign_test_p_value": 0.5,
    }
    assert summary["run_pair_descriptive"]["task_success"] == {
        "paired_observations": 4,
        "improvements": 1,
        "regressions": 0,
        "ties": 3,
        "exact_two_sided_sign_test_p_value": 1.0,
    }
    assert summary["cases"][0]["report_sha256"]
    assert summary["execution_audit"] == {
        "runner_errors": 0,
        "tool_budget_hits": 0,
        "rejected_tool_calls": 0,
    }


def test_summary_rejects_protocol_mismatch(tmp_path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report("case", {"version": 1}, [])))

    with pytest.raises(ValueError, match="protocol mismatch"):
        summarize_benchmark(
            [path],
            expected_protocol={"version": 2},
            baseline="raw",
            treatment="table_semantics",
            metrics=("reported_model_tokens",),
        )


def test_summary_rejects_sources_outside_selection_manifest(tmp_path) -> None:
    protocol = {"version": 1}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report("case-a", protocol, [])))

    with pytest.raises(ValueError, match="do not match the selection manifest"):
        summarize_benchmark(
            [path],
            expected_protocol=protocol,
            expected_source_cases={"case-b"},
            baseline="raw",
            treatment="table_semantics",
            metrics=("reported_model_tokens",),
        )
