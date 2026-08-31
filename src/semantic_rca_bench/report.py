from __future__ import annotations

import html
import json
import math
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from statistics import median

from semantic_rca_bench.protocol import benchmark_protocol

CASE_REPORT_SCHEMA_VERSION = 5
COMBINED_REPORT_SCHEMA_VERSION = 6

MODEL_PRICING = {
    "gpt-5.6-sol": {
        "currency": "USD",
        "input_per_million": 4.0,
        "input_cache_write_per_million": 5.0,
        "input_cache_hit_per_million": 0.4,
        "output_per_million": 20.0,
        "checked_at": "2026-08-30",
        "note": (
            "Promotional pricing is available at least through 2026-11-21. "
            "Cache writes cost 1.25 times uncached input."
        ),
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    },
    "claude-opus-5": {
        "currency": "USD",
        "input_per_million": 5.0,
        "input_cache_write_per_million": 6.25,
        "input_cache_hit_per_million": 0.5,
        "output_per_million": 25.0,
        "checked_at": "2026-08-30",
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
    },
    "claude-fable-5": {
        "currency": "USD",
        "input_per_million": 10.0,
        "input_cache_write_per_million": 12.5,
        "input_cache_hit_per_million": 1.0,
        "output_per_million": 50.0,
        "checked_at": "2026-08-30",
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
    },
    "claude-sonnet-5": {
        "currency": "USD",
        "input_per_million": 2.0,
        "input_cache_write_per_million": 2.5,
        "input_cache_hit_per_million": 0.2,
        "output_per_million": 10.0,
        "effective_from": "2026-08-10",
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
    },
    "deepseek-v4-flash": {
        "currency": "USD",
        "input_per_million": 0.44,
        "input_cache_write_per_million": 0.44,
        "input_cache_hit_per_million": 0.014,
        "output_per_million": 1.32,
        "off_peak_multiplier": 0.5,
        "checked_at": "2026-08-28",
        "note": (
            "Peak-rate upper bound; off-peak rates are half. Context caching is automatic. "
            "Cost is unavailable unless the provider returns a cache breakdown."
        ),
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
    "deepseek-v4-pro": {
        "currency": "USD",
        "input_per_million": 1.32,
        "input_cache_write_per_million": 1.32,
        "input_cache_hit_per_million": 0.044,
        "output_per_million": 3.96,
        "off_peak_multiplier": 0.5,
        "checked_at": "2026-08-28",
        "note": (
            "Peak-rate upper bound; off-peak rates are half. Context caching is automatic. "
            "Cost is unavailable unless the provider returns a cache breakdown."
        ),
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
    "glm-5.3": {
        "currency": "CNY",
        "cost_available": False,
        "checked_at": "2026-08-30",
        "note": (
            "The BigModel China pricing page did not yet list GLM-5.3 rates when the "
            "protocol was prepared. Token usage remains auditable, but estimated cost is "
            "unavailable until an official model-specific rate is frozen."
        ),
        "source": "https://bigmodel.cn/pricing",
    },
    "qwen3.8-max": {
        "currency": "CNY",
        "input_per_million": 12.0,
        "input_cache_hit_per_million": 1.5,
        "output_per_million": 36.0,
        "checked_at": "2026-08-31",
        "note": (
            "Alibaba Cloud Model Studio China (Beijing) workspace deployment. Automatic "
            "cache hits cost CNY 1.5 per million tokens. Explicit cache creation is not used "
            "by this runner; cost fails closed if a cache-write field is nevertheless returned."
        ),
        "source": "https://help.aliyun.com/zh/model-studio/qwen3-8-max",
    },
}

TOKEN_ACCOUNTING = {
    "api": {
        "scope": "sum of provider usage across all responses in one run",
        "input_tokens": (
            "provider-reported total input; Anthropic totals are reconstructed from input, "
            "cache creation, and cache-read fields"
        ),
        "cached_input": "included in input_tokens and retained separately in raw response usage",
        "cached_input_included_in_input_tokens": True,
        "context": "system prompt, tool schemas, and prior tool results are sent to the provider",
        "output_tokens": (
            "normalized provider-reported output_tokens or completion_tokens, including "
            "reasoning where the provider includes it"
        ),
        "reasoning_tokens": (
            "reasoning_tokens or thinking_tokens is recorded as a subset of output tokens when "
            "the provider returns an output-token breakdown; providers without that breakdown "
            "still include reasoning in output tokens"
        ),
        "comparability": "paired comparisons only within the same provider and runner contract",
    },
    "codex-subscription": {
        "scope": "the single cumulative codex exec turn.completed usage event",
        "input_tokens": "includes cached input; cached breakdown is not persisted",
        "cached_input": "included in input_tokens",
        "cached_input_included_in_input_tokens": True,
        "context": "includes Codex runner context, MCP schemas/results, and output-schema handling",
        "output_tokens": "includes reasoning output; reasoning breakdown is not persisted",
        "comparability": "paired comparisons only within the same Codex CLI and runner contract",
    },
    "claude-subscription": {
        "scope": "Claude result usage for one CLI run",
        "input_tokens": "input plus cache creation plus cache reads",
        "cached_input": "included in input_tokens after runner aggregation",
        "cached_input_included_in_input_tokens": True,
        "context": "includes Claude runner context, MCP schemas/results, and structured output",
        "output_tokens": "Claude result output_tokens",
        "comparability": "paired comparisons only within the same Claude CLI and runner contract",
    },
}


def _raw_input_breakdown(run: Mapping[str, object]) -> tuple[int, int, int, bool]:
    uncached = 0
    cache_read = 0
    cache_creation = 0
    usage_responses = 0
    breakdown_responses = 0
    responses = run.get("responses")
    for response in responses if isinstance(responses, list) else []:
        if not isinstance(response, Mapping):
            continue
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            continue
        usage_responses += 1
        if "prompt_cache_hit_tokens" in usage and "prompt_cache_miss_tokens" in usage:
            cache_read += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            uncached += int(usage.get("prompt_cache_miss_tokens", 0) or 0)
            breakdown_responses += 1
        elif "cache_read_input_tokens" in usage and "cache_creation_input_tokens" in usage:
            cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
            cache_creation += int(usage.get("cache_creation_input_tokens", 0) or 0)
            uncached += int(usage.get("input_tokens", 0) or 0)
            breakdown_responses += 1
        else:
            input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
            details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
            if isinstance(details, Mapping):
                cached = int(details.get("cached_tokens", 0) or 0)
                cache_write = int(
                    details.get("cache_write_tokens", 0)
                    or details.get("cache_creation_input_tokens", 0)
                    or usage.get("cache_creation_input_tokens", 0)
                    or 0
                )
                if (
                    input_tokens >= 0
                    and cached >= 0
                    and cache_write >= 0
                    and cached + cache_write <= input_tokens
                ):
                    uncached += input_tokens - cached - cache_write
                    cache_read += cached
                    cache_creation += cache_write
                    breakdown_responses += 1
    return (
        uncached,
        cache_read,
        cache_creation,
        usage_responses > 0 and breakdown_responses == usage_responses,
    )


def _raw_cached_input(run: Mapping[str, object]) -> tuple[int, int]:
    _, cache_read, cache_creation, _ = _raw_input_breakdown(run)
    return cache_read, cache_creation


def _runner_reported_token_total(
    run: Mapping[str, object], accounting: Mapping[str, object] | None
) -> int:
    usage = run.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    cache_read, cache_creation = _raw_cached_input(run)
    cached_input = 0
    if not accounting or accounting.get("cached_input_included_in_input_tokens") is not True:
        cached_input = cache_read + cache_creation
    return (
        int(usage.get("input_tokens", 0) or 0)
        + cached_input
        + int(usage.get("output_tokens", 0) or 0)
    )


def _estimated_api_cost(run: Mapping[str, object], pricing: Mapping[str, object]) -> float | None:
    if pricing.get("cost_available") is False:
        return None
    usage = run.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    uncached_input, cache_read, cache_creation, cache_breakdown_available = _raw_input_breakdown(
        run
    )
    cache_read_rate = pricing.get("input_cache_hit_per_million")
    if cache_read_rate is not None and not cache_breakdown_available:
        return None
    if cache_read and cache_read_rate is None:
        return None
    if not cache_breakdown_available:
        uncached_input = int(usage.get("input_tokens", 0) or 0)
    cache_write_rate = pricing.get("input_cache_write_per_million")
    if cache_creation and cache_write_rate is None:
        return None
    output = int(usage.get("output_tokens", 0) or 0)
    return (
        uncached_input * float(pricing["input_per_million"])
        + cache_creation * float(cache_write_rate or 0)
        + cache_read * float(cache_read_rate or 0)
        + output * float(pricing["output_per_million"])
    ) / 1_000_000


def _strip_signatures(value: object) -> object:
    if isinstance(value, dict):
        return {key: _strip_signatures(item) for key, item in value.items() if key != "signature"}
    if isinstance(value, list):
        return [_strip_signatures(item) for item in value]
    return value


def case_context(
    case: Mapping[str, object], ground_truth: Mapping[str, object]
) -> dict[str, object]:
    def integer(source: Mapping[str, object], key: str) -> int | None:
        value = source.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    start = integer(case, "time_start")
    end = integer(case, "time_end")
    alert = integer(case, "alert_time")
    inject = integer(ground_truth, "inject_time")
    valid_window = start is not None and end is not None and start <= end
    known_injection = valid_window and inject is not None and start <= inject <= end
    taxonomy = case.get("fault_taxonomy")
    return {
        "window_seconds": end - start if valid_window else None,
        "telemetry_before_alert_seconds": (
            max(0, min(alert, end) - start) if valid_window and alert is not None else None
        ),
        "known_pre_fault_seconds": inject - start if known_injection else None,
        "known_post_fault_seconds": end - inject if known_injection else None,
        "baseline_status": "known" if known_injection else "unknown",
        "fault_taxonomy_size": len(taxonomy) if isinstance(taxonomy, list) else 0,
    }


def _position_balanced(report: Mapping[str, object]) -> bool:
    runs = [item for item in report.get("runs", []) if isinstance(item, dict)]
    levels = sorted(
        {
            str(run["visibility"])
            for item in runs
            if isinstance((run := item.get("run")), dict) and run.get("visibility")
        }
    )
    if len(levels) <= 1:
        return True
    counts = {
        level: [
            sum(
                isinstance(item.get("run"), dict)
                and item["run"].get("visibility") == level
                and item.get("position") == position
                for item in runs
            )
            for position in range(len(levels))
        ]
        for level in levels
    }
    return all(len(set(level_counts)) == 1 for level_counts in counts.values())


def _load_case_report(source: Path) -> dict[str, object]:
    report = json.loads(source.read_text())
    if not isinstance(report, dict):
        raise ValueError("RCA report must be a JSON object")
    if report.get("report_schema_version") != CASE_REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported RCA report schema")
    if report.get("protocol") != benchmark_protocol():
        raise ValueError("RCA report protocol does not match the current benchmark protocol")
    if not isinstance(report.get("case"), dict) or not isinstance(report.get("runs"), list):
        raise ValueError("RCA report is missing its case or runs")
    report["eval_report"] = str(source)
    return report


def _case_identity(report: Mapping[str, object]) -> tuple[str, str]:
    case = report.get("case")
    if isinstance(case, Mapping):
        source_case = case.get("source_case")
        if isinstance(source_case, str) and source_case:
            return str(case.get("dataset", "")), source_case
    source_report = report.get("source_report")
    if isinstance(source_report, str) and source_report:
        return "source_report", source_report
    return "eval_report", str(report.get("eval_report", ""))


def _exact_sign_p_value(wins: int, losses: int) -> float | None:
    observations = wins + losses
    if observations == 0:
        return None
    tail = sum(math.comb(observations, value) for value in range(min(wins, losses) + 1))
    return min(1.0, 2 * tail / (2**observations))


def _primary_metric_value(item: Mapping[str, object], metric: str) -> float | None:
    run = item.get("run")
    evaluation = item.get("evaluation")
    if not isinstance(run, Mapping) or not isinstance(evaluation, Mapping):
        return None
    if "valid_completion" not in evaluation:
        raise ValueError("RCA evaluation is missing the canonical valid_completion field")
    if evaluation.get("valid_completion") is not True:
        return None
    if metric == "rows_returned":
        load = item.get("database_load")
        if not isinstance(load, Mapping) or load.get("rows_returned") is None:
            return None
        return float(load["rows_returned"])
    if metric == "correct_completion_tool_calls":
        value = evaluation.get("correct_completion_tool_calls")
        return float(value) if value is not None else None
    raise ValueError(f"unknown primary metric: {metric}")


def _paired_primary_comparisons(
    case_reports: list[dict[str, object]],
) -> list[dict[str, object]]:
    output = []
    for metric in ("rows_returned", "correct_completion_tool_calls"):
        for candidate, baseline in (("semantic_graph", "raw"),):
            run_pair_deltas = []
            case_deltas = []
            for report in case_reports:
                by_key = {}
                for item in report.get("runs", []):
                    if not isinstance(item, Mapping):
                        continue
                    run = item.get("run")
                    if isinstance(run, Mapping):
                        by_key[(int(item.get("repetition", 0)), str(run.get("visibility")))] = item
                per_case = []
                for repetition in sorted({key[0] for key in by_key}):
                    candidate_item = by_key.get((repetition, candidate))
                    baseline_item = by_key.get((repetition, baseline))
                    if candidate_item is None or baseline_item is None:
                        continue
                    candidate_value = _primary_metric_value(candidate_item, metric)
                    baseline_value = _primary_metric_value(baseline_item, metric)
                    if candidate_value is None or baseline_value is None:
                        continue
                    delta = candidate_value - baseline_value
                    run_pair_deltas.append(delta)
                    per_case.append(delta)
                if per_case:
                    case_deltas.append(median(per_case))
            output.append(
                {
                    "metric": metric,
                    "candidate": candidate,
                    "baseline": baseline,
                    "run_pair_descriptive": _direction_summary(run_pair_deltas, inference=False),
                    "case_level_inference": _direction_summary(case_deltas, inference=True),
                }
            )
    _add_holm_adjustment(output)
    return output


def _direction_summary(deltas: list[float], *, inference: bool) -> dict[str, object]:
    better = sum(delta < 0 for delta in deltas)
    worse = sum(delta > 0 for delta in deltas)
    ties = sum(delta == 0 for delta in deltas)
    summary: dict[str, object] = {
        "observations": len(deltas),
        "better": better,
        "worse": worse,
        "ties": ties,
        "median_delta": median(deltas) if deltas else None,
    }
    if inference:
        summary["exact_two_sided_sign_test_p_value"] = _exact_sign_p_value(better, worse)
    return summary


def _add_holm_adjustment(comparisons: list[dict[str, object]]) -> None:
    observed = []
    for index, comparison in enumerate(comparisons):
        inference = comparison["case_level_inference"]
        if not isinstance(inference, dict):
            continue
        value = inference.get("exact_two_sided_sign_test_p_value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            observed.append((float(value), index))
    previous = 0.0
    family_size = len(comparisons)
    for rank, (value, index) in enumerate(sorted(observed), start=1):
        adjusted = max(previous, min(1.0, value * (family_size - rank + 1)))
        inference = comparisons[index]["case_level_inference"]
        if not isinstance(inference, dict):
            raise TypeError("case-level inference must be an object")
        inference["holm_adjusted_p_value"] = adjusted
        inference["multiplicity_family_size"] = family_size
        previous = adjusted


def render_reports(sources: list[Path], output: Path) -> None:
    if not sources:
        raise ValueError("at least one eval report is required")
    case_reports = [_load_case_report(source) for source in sources]
    runners = {str(report.get("runner", "api")) for report in case_reports}
    models = {str(report.get("model", "")) for report in case_reports}
    protocols = {json.dumps(report.get("protocol", {}), sort_keys=True) for report in case_reports}
    budgets = {
        int(report.get("max_tool_calls", report.get("max_queries", 0))) for report in case_reports
    }
    repetitions = {int(report.get("repetitions", 1)) for report in case_reports}
    case_roles = {str(report.get("case_role", "development")) for report in case_reports}
    token_accounting_contracts = {
        json.dumps(report["token_accounting"], sort_keys=True)
        for report in case_reports
        if isinstance(report.get("token_accounting"), Mapping)
    }
    if len(runners) != 1:
        raise ValueError(f"pilot reports use different runners: {sorted(runners)}")
    if len(models) != 1:
        raise ValueError(f"pilot reports use different models: {sorted(models)}")
    if len(protocols) != 1:
        raise ValueError("pilot reports use different benchmark protocols")
    if len(budgets) != 1:
        raise ValueError("pilot reports use different tool budgets")
    if len(repetitions) != 1:
        raise ValueError("pilot reports use different repetition counts")
    if len(case_roles) != 1:
        raise ValueError("pilot reports mix development and measurement cases")
    if len(token_accounting_contracts) > 1:
        raise ValueError("pilot reports use different token-accounting contracts")
    case_identities = [_case_identity(report) for report in case_reports]
    if len(set(case_identities)) != len(case_identities):
        raise ValueError("pilot reports contain duplicate case identities")
    datasets = {
        str(report.get("case", {}).get("dataset", ""))
        for report in case_reports
        if isinstance(report.get("case"), dict)
    }
    taxonomies = {
        tuple(str(value) for value in report.get("case", {}).get("fault_taxonomy", []))
        for report in case_reports
        if isinstance(report.get("case"), dict)
    }
    runner = next(iter(runners))
    model = next(iter(models))
    token_accounting = (
        json.loads(next(iter(token_accounting_contracts)))
        if token_accounting_contracts
        else TOKEN_ACCOUNTING.get(runner)
    )
    pricing = MODEL_PRICING.get(model) if runner == "api" else None
    for case_report in case_reports:
        for item in case_report.get("runs", []):
            if not isinstance(item, dict) or not isinstance(item.get("run"), dict):
                continue
            run = item["run"]
            run["runner_reported_token_total"] = _runner_reported_token_total(run, token_accounting)
            if pricing is not None:
                run["estimated_api_cost"] = _estimated_api_cost(run, pricing)
    report = {
        "report_schema_version": COMBINED_REPORT_SCHEMA_VERSION,
        "pilot_id": output.stem,
        "runner": runner,
        "model": model,
        "protocol": case_reports[0].get("protocol", {}),
        "max_tool_calls": next(iter(budgets)),
        "pricing": pricing,
        "token_accounting": token_accounting,
        "case_role": next(iter(case_roles)),
        "latency_comparable": all(_position_balanced(item) for item in case_reports),
        "correctness_aggregation_comparable": len(datasets) == 1 and len(taxonomies) == 1,
        "paired_primary_comparisons": _paired_primary_comparisons(case_reports),
        "primary_multiplicity": (
            "Holm adjustment over the fixed family of two planned primary comparisons: "
            "two metrics by the Semantic Graph minus Raw contrast, including hypotheses "
            "without an observed p-value"
        ),
        "cases": case_reports,
    }
    template = (
        files("semantic_rca_bench").joinpath("assets/report.html").read_text(encoding="utf-8")
    )
    title = f"Semantic RCA Bench — {output.stem}"
    hidden_columns = []
    if not report["correctness_aggregation_comparable"]:
        hidden_columns.append(".aggregate-correctness { display: none; }")
    if report["pricing"] is None:
        hidden_columns.append(".api-cost { display: none; }")
    serialized = json.dumps(_strip_signatures(report), ensure_ascii=False, separators=(",", ":"))
    serialized = serialized.replace("</", "<\\/")
    document = (
        template.replace("__REPORT_TITLE__", html.escape(title))
        .replace("__REPORT_COLUMN_CSS__", "\n    ".join(hidden_columns))
        .replace("__REPORT_DATA__", serialized)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def render_report(source: Path, output: Path) -> None:
    render_reports([source], output)
