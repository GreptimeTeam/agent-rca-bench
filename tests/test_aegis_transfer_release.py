from semantic_rca_bench.aegis_transfer_release import _measurement_model_summary


def test_efficiency_pairs_do_not_require_unrelated_citation_integrity() -> None:
    runs = []
    for position, visibility in enumerate(("raw", "semantic_graph")):
        auditable = visibility == "semantic_graph"
        runs.append(
            {
                "repetition": 0,
                "position": position,
                "visibility": visibility,
                "evaluation": {
                    "success": auditable,
                    "auditable_completion": auditable,
                    "efficiency_eligible": True,
                    "correct_completion_tool_calls": 10 - position,
                },
                "execution": {"database_load": {"rows_returned": 100 - position * 10}},
                "usage": {
                    "uncached_input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 10 - position,
                    "reasoning_output_tokens": position,
                    "estimated_cost": 0.1,
                    "cost_currency": "USD",
                },
            }
        )

    summary = _measurement_model_summary(runs, "deepseek-v4-flash")

    assert summary["successful_runs"] == 1
    assert summary["efficiency_eligible_runs"] == 2
    assert summary["usage"]["reasoning_output_tokens"] == 1
    assert summary["paired_treatment_deltas"]["semantic_graph_minus_raw"]["eligible_pairs"] == 1
