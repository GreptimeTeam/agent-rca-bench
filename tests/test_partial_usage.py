import copy

import pytest

from agent_rca_bench.partial_usage import (
    bounded_transfer_costs,
    cache_cost_bounds,
    gemini_cache_observation,
)


def test_partial_cache_cost_keeps_observed_discounts_and_counts_output_once():
    run = {
        "responses": [
            {
                "usage": {
                    "prompt_tokens": 1000,
                    "total_tokens": 1100,
                    "prompt_tokens_details": {"cached_tokens": 800},
                }
            },
            {"usage": {"prompt_tokens": 500, "total_tokens": 550}},
        ],
        "usage": {"input_tokens": 1500, "output_tokens": 150},
    }
    observed = gemini_cache_observation(run)
    usage = {
        "cache_observation": observed,
        "provider_visible_input_tokens": 1500,
        "output_tokens": 150,
        "reasoning_output_tokens": 100,
    }
    pricing = {
        "input_per_million": 0.75,
        "input_cache_hit_per_million": 0.075,
        "output_per_million": 3.75,
    }
    assert cache_cost_bounds(usage, pricing) == pytest.approx([0.00081, 0.0011475])
    invalid = copy.deepcopy(run)
    invalid["usage"]["output_tokens"] += 1
    with pytest.raises(ValueError, match="normalized usage"):
        gemini_cache_observation(invalid)
    invalid = copy.deepcopy(run)
    invalid["responses"][0]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1001
    with pytest.raises(ValueError, match="cached token count"):
        gemini_cache_observation(invalid)
    usage["provider_visible_input_tokens"] += 1
    with pytest.raises(ValueError, match="all input"):
        cache_cost_bounds(usage, pricing)


def test_bounded_model_requires_bounds_on_every_run():
    runs = [
        {
            "model": "bounded-model",
            "visibility": "raw",
            "run": {"usage": {"cost_currency": "USD", "estimated_cost_bounds": [1.0, 2.0]}},
        },
        {
            "model": "bounded-model",
            "visibility": "split_pillars",
            "run": {"usage": {"cost_currency": "USD"}},
        },
    ]
    with pytest.raises(
        ValueError, match="Missing estimated_cost_bounds.*bounded-model.*split_pillars"
    ):
        bounded_transfer_costs(runs)
