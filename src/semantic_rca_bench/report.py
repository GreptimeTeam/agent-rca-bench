from __future__ import annotations

import html
import json
import math
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from statistics import median

MODEL_PRICING = {
    "claude-sonnet-5": {
        "currency": "USD",
        "input_per_million": 2.0,
        "output_per_million": 10.0,
        "effective_from": "2026-08-10",
        "source": "https://www.anthropic.com/news/claude-sonnet-5",
    },
    "deepseek-v4-flash": {
        "currency": "USD",
        "input_per_million": 0.14,
        "input_cache_hit_per_million": 0.0028,
        "output_per_million": 0.28,
        "checked_at": "2026-08-27",
        "note": (
            "Cost is unavailable unless the provider response exposes the Anthropic-compatible "
            "cache creation and cache-read fields."
        ),
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
    "deepseek-v4-pro": {
        "currency": "USD",
        "input_per_million": 0.435,
        "input_cache_hit_per_million": 0.003625,
        "output_per_million": 0.87,
        "checked_at": "2026-08-27",
        "note": (
            "Cost is unavailable unless the provider response exposes the Anthropic-compatible "
            "cache creation and cache-read fields."
        ),
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
}

TOKEN_ACCOUNTING = {
    "api": {
        "scope": "sum of provider usage across all responses in one run",
        "input_tokens": "uncached input only",
        "cached_input": "separate raw response fields; omitted from legacy run.usage",
        "cached_input_included_in_input_tokens": False,
        "context": "system prompt, tool schemas, and prior tool results are sent to the provider",
        "output_tokens": "provider-reported output including structured tool output",
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


def _raw_cached_input(run: Mapping[str, object]) -> tuple[int, int]:
    cache_read = 0
    cache_creation = 0
    responses = run.get("responses")
    for response in responses if isinstance(responses, list) else []:
        if not isinstance(response, Mapping):
            continue
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            continue
        cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_creation += int(usage.get("cache_creation_input_tokens", 0) or 0)
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
    usage = run.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    cache_read, cache_creation = _raw_cached_input(run)
    cache_read_rate = pricing.get("input_cache_hit_per_million")
    responses = run.get("responses")
    response_items = responses if isinstance(responses, list) else []
    cache_breakdown_available = any(
        isinstance(response, Mapping)
        and isinstance((response_usage := response.get("usage")), Mapping)
        and "cache_read_input_tokens" in response_usage
        and "cache_creation_input_tokens" in response_usage
        for response in response_items
    )
    if cache_read_rate is not None and not cache_breakdown_available:
        return None
    if cache_read and cache_read_rate is None:
        return None
    uncached_input = int(usage.get("input_tokens", 0) or 0) + cache_creation
    output = int(usage.get("output_tokens", 0) or 0)
    return (
        uncached_input * float(pricing["input_per_million"])
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


def _normalize_run_metadata(report: dict[str, object]) -> None:
    budget = int(report.get("max_tool_calls", report.get("max_queries", 0)) or 0)
    orders = {
        int(order.get("repetition", 0)): list(order.get("levels", []))
        for order in report.get("orders", [])
        if isinstance(order, dict)
    }
    for item in report.get("runs", []):
        if not isinstance(item, dict):
            continue
        run = item.get("run")
        if not isinstance(run, dict):
            continue
        requested = 0
        for response in run.get("responses", []):
            if not isinstance(response, dict):
                continue
            requested += sum(
                block.get("type") == "tool_use" and block.get("name") != "submit_diagnosis"
                for block in response.get("content", [])
                if isinstance(block, dict)
            )
        run.setdefault("tool_calls_requested", requested)
        if "tool_budget_exhausted" not in run:
            run["tool_budget_exhausted"] = budget > 0 and requested > budget
        if "position" not in item:
            repetition = int(item.get("repetition", 0))
            visibility = run.get("visibility")
            levels = orders.get(repetition, [])
            if visibility in levels:
                item["position"] = levels.index(visibility)


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
    if not report.get("case") and report.get("source_report"):
        source_report = Path(str(report["source_report"]))
        candidates = [source_report, source.parent / source_report.name]
        original = next((path for path in candidates if path.is_file()), None)
        if original:
            ingestion = json.loads(original.read_text())
            report["case"] = ingestion.get("case", {})
            report["ingest"] = ingestion.get("ingest", {})
            surfaces = ingestion.get("semantic_surfaces", {})
            if isinstance(surfaces, dict):
                report["semantic_coverage"] = surfaces.get("coverage")
    report["eval_report"] = str(source)
    report.setdefault("case_role", "development")
    case = report.get("case") if isinstance(report.get("case"), dict) else {}
    truth = report.get("ground_truth") if isinstance(report.get("ground_truth"), dict) else {}
    report.setdefault("case_context", case_context(case, truth))
    _normalize_run_metadata(report)
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
        for candidate, baseline in (
            ("table_semantics", "raw"),
            ("semantic_graph", "table_semantics"),
        ):
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
        "report_schema_version": 6,
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
            "Holm adjustment over the fixed family of four planned primary comparisons: "
            "two metrics by two adjacent treatment contrasts, including hypotheses without "
            "an observed p-value"
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
