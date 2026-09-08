"""Retain observed Gemini cache usage without guessing missing response fields."""

from collections.abc import Mapping


def gemini_cache_observation(run: Mapping[str, object]) -> dict[str, int]:
    known_cached = known_uncached = unknown = total_input = total_output = 0
    for response in run["responses"]:
        usage = response["usage"]
        tokens = usage["prompt_tokens"]
        total = usage["total_tokens"]
        if type(tokens) is not int or type(total) is not int or not 0 <= tokens <= total:
            raise ValueError("Invalid Gemini response token counts")
        total_input += tokens
        total_output += total - tokens
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
        if cached is None:
            unknown += tokens
        elif type(cached) is not int or not 0 <= cached <= tokens:
            raise ValueError("Invalid Gemini cached token count")
        else:
            known_cached += cached
            known_uncached += tokens - cached
    if total_input != run["usage"]["input_tokens"] or total_output != run["usage"]["output_tokens"]:
        raise ValueError("Gemini response totals differ from normalized usage")
    return {
        "known_cached_input_tokens": known_cached,
        "known_uncached_input_tokens": known_uncached,
        "unknown_cache_input_tokens": unknown,
    }


def cache_cost_bounds(usage: Mapping[str, object], pricing: Mapping[str, object]) -> list[float]:
    observation = usage["cache_observation"]
    values = [
        observation.get(key)
        for key in (
            "known_cached_input_tokens",
            "known_uncached_input_tokens",
            "unknown_cache_input_tokens",
        )
    ]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Invalid partial cache observation")
    cached, uncached, unknown = values
    if sum(values) != usage["provider_visible_input_tokens"]:
        raise ValueError("Partial cache observation does not account for all input")
    ordinary = pricing["input_per_million"]
    hit = pricing["input_cache_hit_per_million"]
    if not 0 <= hit <= ordinary:
        raise ValueError("Invalid frozen cache prices")
    known = (
        cached * hit + uncached * ordinary + usage["output_tokens"] * pricing["output_per_million"]
    ) / 1_000_000
    return [known + unknown * hit / 1_000_000, known + unknown * ordinary / 1_000_000]


def bounded_transfer_costs(runs: list[Mapping[str, object]]) -> dict[str, object]:
    models = {item["model"] for item in runs if "estimated_cost_bounds" in item["run"]["usage"]}
    result = {}
    for model in sorted(models):
        by_treatment = {}
        currency = None
        for item in runs:
            if item["model"] != model:
                continue
            usage = item["run"]["usage"]
            if "estimated_cost_bounds" not in usage:
                raise ValueError(
                    f"Missing estimated_cost_bounds for model {model}, "
                    f"treatment {item['visibility']}"
                )
            if currency is not None and currency != usage["cost_currency"]:
                raise ValueError("Bounded model costs span currencies")
            currency = usage["cost_currency"]
            bounds = usage["estimated_cost_bounds"]
            total = by_treatment.setdefault(item["visibility"], [0.0, 0.0])
            for index in (0, 1):
                total[index] += bounds[index]
        result[model] = {"currency": currency, "by_treatment": by_treatment}
    return result
