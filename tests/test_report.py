import json

from semantic_rca_bench.report import (
    MODEL_PRICING,
    _load_case_report,
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
