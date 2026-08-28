import json

import pytest

from semantic_rca_bench.report import (
    MODEL_PRICING,
    _exact_sign_p_value,
    _load_case_report,
    _paired_primary_comparisons,
    _primary_metric_value,
    case_context,
    render_report,
    render_reports,
)


def test_deepseek_pricing_uses_cache_miss_rate() -> None:
    pricing = MODEL_PRICING["deepseek-v4-flash"]

    assert pricing["input_per_million"] == 0.14
    assert pricing["input_cache_hit_per_million"] == 0.0028
    assert pricing["output_per_million"] == 0.28
    assert "cache misses" in pricing["note"]


def test_render_report_embeds_data_and_escapes_script_end(tmp_path) -> None:
    source = tmp_path / "pilot.json"
    output = tmp_path / "pilot.html"
    source.write_text(
        json.dumps(
            {
                "model": "test-model",
                "ground_truth": {"component": "checkout</script>", "fault_type": "delay"},
                "runs": [
                    {
                        "run": {
                            "responses": [
                                {
                                    "content": [
                                        {
                                            "type": "thinking",
                                            "thinking": "inspect telemetry",
                                            "signature": "opaque-provider-value",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ],
            }
        )
    )

    render_report(source, output)

    document = output.read_text()
    assert "<!doctype html>" in document
    assert "test-model" in document
    assert "checkout<\\/script>" in document
    assert "inspect telemetry" in document
    assert "opaque-provider-value" not in document
    assert "__REPORT_DATA__" not in document
    assert "const budgetHit = run => Boolean(run.tool_budget_exhausted);" in document
    assert "const correctItems = budgetHits ? []" not in document


def test_subscription_combined_report_hides_inapplicable_summary_columns(tmp_path) -> None:
    sources = []
    for index, dataset in enumerate(("dataset-a", "dataset-b")):
        source = tmp_path / f"pilot-{index}.json"
        source.write_text(
            json.dumps(
                {
                    "runner": "codex-subscription",
                    "model": "test-model",
                    "protocol": {"version": 17},
                    "max_tool_calls": 48,
                    "repetitions": 1,
                    "case": {"dataset": dataset, "fault_taxonomy": [dataset]},
                    "ground_truth": {},
                    "runs": [],
                }
            )
        )
        sources.append(source)

    output = tmp_path / "combined.html"
    render_reports(sources, output)

    document = output.read_text()
    assert ".aggregate-correctness { display: none; }" in document
    assert ".api-cost { display: none; }" in document
    assert "__REPORT_COLUMN_CSS__" not in document


def test_render_reports_rejects_mixed_protocols(tmp_path) -> None:
    sources = []
    for version in (1, 2):
        source = tmp_path / f"pilot-{version}.json"
        source.write_text(
            json.dumps(
                {
                    "model": "test-model",
                    "protocol": {"version": version},
                    "ground_truth": {},
                    "runs": [],
                }
            )
        )
        sources.append(source)

    try:
        render_reports(sources, tmp_path / "combined.html")
    except ValueError as error:
        assert "different benchmark protocols" in str(error)
    else:
        raise AssertionError("mixed protocols should be rejected")


def test_case_context_distinguishes_alert_history_from_known_baseline() -> None:
    unknown = case_context(
        {
            "time_start": 100,
            "time_end": 700,
            "alert_time": 234,
            "fault_taxonomy": ["cpu", "delay"],
        },
        {"inject_time": None},
    )
    known = case_context(
        {"time_start": 100, "time_end": 700, "alert_time": 700},
        {"inject_time": 220},
    )

    assert unknown["telemetry_before_alert_seconds"] == 134
    assert unknown["known_pre_fault_seconds"] is None
    assert unknown["baseline_status"] == "unknown"
    assert unknown["fault_taxonomy_size"] == 2
    assert known["known_pre_fault_seconds"] == 120
    assert known["known_post_fault_seconds"] == 480
    assert known["baseline_status"] == "known"


def test_legacy_report_derives_budget_exhaustion_and_execution_position(tmp_path) -> None:
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps(
            {
                "max_tool_calls": 2,
                "orders": [
                    {
                        "repetition": 0,
                        "levels": ["semantic_graph", "raw", "table_semantics"],
                    }
                ],
                "runs": [
                    {
                        "repetition": 0,
                        "run": {
                            "visibility": "raw",
                            "responses": [
                                {
                                    "content": [
                                        {"type": "tool_use", "name": "execute_sql"},
                                        {"type": "tool_use", "name": "describe_table"},
                                        {"type": "tool_use", "name": "execute_sql"},
                                        {"type": "tool_use", "name": "submit_diagnosis"},
                                    ]
                                }
                            ],
                        },
                    }
                ],
            }
        )
    )

    report = _load_case_report(source)
    item = report["runs"][0]

    assert item["position"] == 1
    assert item["run"]["tool_calls_requested"] == 3
    assert item["run"]["tool_budget_exhausted"] is True


def test_primary_metrics_use_paired_directions_and_exact_sign_test() -> None:
    reports = [
        {
            "runs": [
                {
                    "repetition": 0,
                    "run": {
                        "visibility": "raw",
                        "tool_budget_exhausted": False,
                        "diagnosis": {},
                    },
                    "evaluation": {"joint_match": True, "correct_completion_tool_calls": 12},
                    "database_load": {"rows_returned": 100},
                },
                {
                    "repetition": 0,
                    "run": {
                        "visibility": "semantic_graph",
                        "tool_budget_exhausted": False,
                        "diagnosis": {},
                    },
                    "evaluation": {"joint_match": True, "correct_completion_tool_calls": 8},
                    "database_load": {"rows_returned": 40},
                },
            ]
        }
    ]

    comparisons = _paired_primary_comparisons(reports)
    graph_rows = next(
        item
        for item in comparisons
        if item["metric"] == "rows_returned"
        and item["candidate"] == "semantic_graph"
        and item["baseline"] == "raw"
    )

    assert graph_rows["better"] == 1
    assert graph_rows["worse"] == 0
    assert graph_rows["median_delta"] == -60
    assert _exact_sign_p_value(19, 5) == pytest.approx(0.00661075)


@pytest.mark.parametrize(
    "run",
    [
        {"diagnosis": None, "error": "turn limit exhausted"},
        {"diagnosis": None, "error": None},
    ],
)
def test_failed_run_is_excluded_from_rows_returned_efficiency(run) -> None:
    item = {
        "run": run,
        "evaluation": {"joint_match": False},
        "database_load": {"rows_returned": 500},
    }

    assert _primary_metric_value(item, "rows_returned") is None
