import json

import pytest

from semantic_rca_bench.report import (
    MODEL_PRICING,
    TOKEN_ACCOUNTING,
    _estimated_api_cost,
    _exact_sign_p_value,
    _load_case_report,
    _paired_primary_comparisons,
    _primary_metric_value,
    _runner_reported_token_total,
    case_context,
    render_report,
    render_reports,
)


def test_deepseek_pricing_separates_uncached_and_cache_read_input() -> None:
    pricing = MODEL_PRICING["deepseek-v4-flash"]

    assert pricing["input_per_million"] == 0.44
    assert pricing["input_cache_hit_per_million"] == 0.014
    assert pricing["output_per_million"] == 1.32
    assert "Peak-rate upper bound" in pricing["note"]
    assert "Cost is unavailable unless" in pricing["note"]


def test_anthropic_api_usage_reconstructs_cached_tokens_and_cost() -> None:
    run = {
        "usage": {"input_tokens": 120, "output_tokens": 150},
        "responses": [
            {
                "usage": {
                    "input_tokens": 120,
                    "cache_creation_input_tokens": 40,
                    "cache_read_input_tokens": 2_176,
                }
            }
        ],
    }

    assert _runner_reported_token_total(run, TOKEN_ACCOUNTING["api"]) == 2_486
    assert _estimated_api_cost(run, MODEL_PRICING["claude-sonnet-5"]) == pytest.approx(
        (120 * 2 + 40 * 2.5 + 2_176 * 0.2 + 150 * 10) / 1_000_000
    )


def test_deepseek_api_usage_uses_native_automatic_cache_breakdown() -> None:
    run = {
        "usage": {"input_tokens": 120, "output_tokens": 150},
        "responses": [
            {
                "usage": {
                    "input_tokens": 2_296,
                    "prompt_cache_hit_tokens": 2_176,
                    "prompt_cache_miss_tokens": 120,
                }
            }
        ],
    }

    assert _runner_reported_token_total(run, TOKEN_ACCOUNTING["api"]) == 2_446
    assert _estimated_api_cost(run, MODEL_PRICING["deepseek-v4-flash"]) == pytest.approx(
        (120 * 0.44 + 2_176 * 0.014 + 150 * 1.32) / 1_000_000
    )


def test_deepseek_cost_is_unavailable_without_provider_cache_breakdown() -> None:
    run = {
        "usage": {"input_tokens": 120, "output_tokens": 20},
        "responses": [{"usage": {"input_tokens": 120, "output_tokens": 20}}],
    }

    assert _estimated_api_cost(run, MODEL_PRICING["deepseek-v4-flash"]) is None


def test_api_cost_is_unavailable_when_any_response_lacks_cache_breakdown() -> None:
    run = {
        "usage": {"input_tokens": 240, "output_tokens": 20},
        "responses": [
            {
                "usage": {
                    "input_tokens": 120,
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 20,
                }
            },
            {"usage": {"input_tokens": 120, "output_tokens": 20}},
        ],
    }

    assert _estimated_api_cost(run, MODEL_PRICING["deepseek-v4-flash"]) is None


def test_claude_subscription_does_not_double_count_cached_input() -> None:
    run = {
        "usage": {"input_tokens": 280, "output_tokens": 20},
        "responses": [
            {
                "usage": {
                    "input_tokens": 80,
                    "cache_creation_input_tokens": 40,
                    "cache_read_input_tokens": 160,
                    "output_tokens": 20,
                }
            }
        ],
    }
    accounting = {
        "cached_input_included_in_input_tokens": True,
    }

    assert _runner_reported_token_total(run, accounting) == 300


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
    assert "hasOwnProperty.call(run, 'estimated_api_cost')" in document
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


def test_render_reports_rejects_duplicate_case_identity(tmp_path) -> None:
    source = tmp_path / "pilot.json"
    source.write_text(
        json.dumps(
            {
                "model": "test-model",
                "protocol": {"version": 1},
                "case": {"dataset": "dataset-a", "source_case": "case-a"},
                "ground_truth": {},
                "runs": [],
            }
        )
    )

    with pytest.raises(ValueError, match="duplicate case identities"):
        render_reports([source, source], tmp_path / "combined.html")


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


def _primary_item(repetition: int, visibility: str, rows: int, calls: int) -> dict:
    return {
        "repetition": repetition,
        "run": {
            "visibility": visibility,
            "tool_budget_exhausted": False,
            "diagnosis": {},
        },
        "evaluation": {
            "valid_completion": True,
            "joint_match": True,
            "cited_evidence_count": 1,
            "valid_evidence_count": 1,
            "correct_completion_tool_calls": calls,
        },
        "database_load": {"rows_returned": rows},
    }


def test_primary_metrics_infer_over_case_medians_not_run_pairs() -> None:
    reports = [
        {
            "runs": [
                _primary_item(0, "raw", 100, 12),
                _primary_item(0, "table_semantics", 70, 10),
                _primary_item(0, "semantic_graph", 40, 8),
                _primary_item(1, "raw", 100, 12),
                _primary_item(1, "table_semantics", 80, 10),
                _primary_item(1, "semantic_graph", 60, 8),
            ]
        },
        {
            "runs": [
                _primary_item(0, "raw", 100, 12),
                _primary_item(0, "table_semantics", 130, 14),
                _primary_item(0, "semantic_graph", 100, 12),
            ]
        },
    ]

    comparisons = _paired_primary_comparisons(reports)
    graph_rows = next(
        item
        for item in comparisons
        if item["metric"] == "rows_returned"
        and item["candidate"] == "semantic_graph"
        and item["baseline"] == "table_semantics"
    )

    assert graph_rows["run_pair_descriptive"] == {
        "observations": 3,
        "better": 3,
        "worse": 0,
        "ties": 0,
        "median_delta": -30.0,
    }
    assert graph_rows["case_level_inference"] == {
        "observations": 2,
        "better": 2,
        "worse": 0,
        "ties": 0,
        "median_delta": -27.5,
        "exact_two_sided_sign_test_p_value": 0.5,
        "holm_adjusted_p_value": 1.0,
        "multiplicity_family_size": 4,
    }
    assert not any(
        item["candidate"] == "semantic_graph" and item["baseline"] == "raw" for item in comparisons
    )
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


def test_primary_metrics_consume_canonical_valid_completion() -> None:
    item = {
        "run": {"diagnosis": {}, "tool_budget_exhausted": False},
        "evaluation": {
            "valid_completion": False,
            "correct_completion_tool_calls": 3,
        },
        "database_load": {"rows_returned": 10},
    }

    assert _primary_metric_value(item, "rows_returned") is None
    assert _primary_metric_value(item, "correct_completion_tool_calls") is None

    item["evaluation"]["valid_completion"] = True

    assert _primary_metric_value(item, "rows_returned") == 10
    assert _primary_metric_value(item, "correct_completion_tool_calls") == 3
