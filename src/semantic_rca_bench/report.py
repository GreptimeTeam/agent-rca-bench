from __future__ import annotations

import html
import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

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
        "note": "Input cost assumes cache misses; cache-hit discounts are not subtracted.",
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
    "deepseek-v4-pro": {
        "currency": "USD",
        "input_per_million": 0.435,
        "input_cache_hit_per_million": 0.003625,
        "output_per_million": 0.87,
        "checked_at": "2026-08-27",
        "note": "Input cost assumes cache misses; cache-hit discounts are not subtracted.",
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
}


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
            max(0, min(alert, end) - start)
            if valid_window and alert is not None
            else None
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


def render_reports(sources: list[Path], output: Path) -> None:
    if not sources:
        raise ValueError("at least one eval report is required")
    case_reports = [_load_case_report(source) for source in sources]
    models = {str(report.get("model", "")) for report in case_reports}
    protocols = {json.dumps(report.get("protocol", {}), sort_keys=True) for report in case_reports}
    budgets = {
        int(report.get("max_tool_calls", report.get("max_queries", 0))) for report in case_reports
    }
    repetitions = {int(report.get("repetitions", 1)) for report in case_reports}
    case_roles = {str(report.get("case_role", "development")) for report in case_reports}
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
    report = {
        "report_schema_version": 2,
        "pilot_id": output.stem,
        "model": next(iter(models)),
        "protocol": case_reports[0].get("protocol", {}),
        "pricing": MODEL_PRICING.get(next(iter(models))),
        "case_role": next(iter(case_roles)),
        "latency_comparable": all(_position_balanced(item) for item in case_reports),
        "correctness_aggregation_comparable": len(datasets) == 1 and len(taxonomies) == 1,
        "cases": case_reports,
    }
    template = (
        files("semantic_rca_bench").joinpath("assets/report.html").read_text(encoding="utf-8")
    )
    title = f"Semantic RCA Bench — {output.stem}"
    serialized = json.dumps(_strip_signatures(report), ensure_ascii=False, separators=(",", ":"))
    serialized = serialized.replace("</", "<\\/")
    document = template.replace("__REPORT_TITLE__", html.escape(title)).replace(
        "__REPORT_DATA__", serialized
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def render_report(source: Path, output: Path) -> None:
    render_reports([source], output)
