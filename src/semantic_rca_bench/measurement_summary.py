from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median

DISCOVERY_REPORT_FILES = (
    "discovery-v2-measurement-codex-luna-market-c1-email-read.json",
    "discovery-v2-measurement-codex-luna-market-c2-email-write.json",
    "discovery-v2-measurement-codex-luna-bank-redis02-cpu-user-time.json",
    "discovery-v2-measurement-codex-luna-bank-tomcat01-disk-read.json",
    "discovery-v2-measurement-codex-luna-telecom-docker001-cpu.json",
    "discovery-v2-measurement-codex-luna-telecom-docker006-cpu.json",
)

GRAPH_REPORT_FILES = (
    "graph-v3-measurement-codex-luna-hs4-geo.json",
    "graph-v3-measurement-codex-luna-hs1-rate.json",
)

# These contracts belong to the immutable v2/v3 input reports named above. They
# are artifact inputs, not runtime support for retired benchmark protocols.
FROZEN_DISCOVERY_PROTOCOL = {
    "version": 2,
    "treatments": ["raw", "table_semantics"],
    "task": "frozen-table-localization-and-temporal-evidence-v1",
    "scorer": "current-database-qualified-cited-query-canonical-result-v2",
    "tool_budget": 12,
    "api_turn_limit": 22,
    "catalog": "token-safe-io-direction-aware-v1",
    "prompt": "case-preserving-double-quoted-identifiers-v2",
    "case_role": "explicit-development-or-measurement-v1",
    "fixture_binding": "source-case-matched-external-fixture-v1",
}
FROZEN_GRAPH_PROTOCOL = {
    "version": 3,
    "treatments": ["table_semantics", "semantic_graph"],
    "task": "direct-callee-max-error-red-evidence-v1",
    "scorer": "result-proven-destination-type-canonical-edge-set-v2",
    "window": "minute-aligned-half-open-v1",
    "tool_budget": 12,
    "api_turn_limit": 22,
    "case_role": "explicit-development-or-measurement-v1",
    "fixture_binding": "source-case-matched-external-fixture-v1",
    "selection": "manifest-ranked-prior-trajectory-excluded-v1",
}


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _exact_sign_p_value(better: int, worse: int) -> float | None:
    observations = better + worse
    if observations == 0:
        return None
    tail = sum(math.comb(observations, value) for value in range(min(better, worse) + 1))
    return min(1.0, 2 * tail / (2**observations))


def _direction_summary(deltas: Sequence[float]) -> dict[str, int | float | None]:
    better = sum(value < 0 for value in deltas)
    worse = sum(value > 0 for value in deltas)
    ties = sum(value == 0 for value in deltas)
    return {
        "observations": len(deltas),
        "better": better,
        "worse": worse,
        "ties": ties,
        "median_delta": median(deltas) if deltas else None,
        "exact_two_sided_sign_test_p_value": _exact_sign_p_value(better, worse),
    }


def _success(item: Mapping[str, object]) -> bool:
    evaluation = item.get("evaluation")
    return isinstance(evaluation, Mapping) and evaluation.get("success") is True


def _metric_value(item: Mapping[str, object], metric: str) -> float:
    if metric == "reported_model_tokens":
        run = item.get("run")
        usage = run.get("usage") if isinstance(run, Mapping) else None
        if not isinstance(usage, Mapping):
            raise ValueError("run is missing model usage")
        values = [usage.get("input_tokens"), usage.get("output_tokens")]
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
        ):
            raise ValueError("run has invalid model token usage")
        return float(sum(values))

    evaluation = item.get("evaluation")
    value = evaluation.get(metric) if isinstance(evaluation, Mapping) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"evaluation is missing metric: {metric}")
    return float(value)


def _paired_cells(
    report: Mapping[str, object], baseline: str, treatment: str
) -> list[tuple[Mapping[str, object], Mapping[str, object]]]:
    runs = report.get("runs")
    if not isinstance(runs, list):
        raise ValueError("measurement report is missing runs")
    by_key: dict[tuple[int, str], Mapping[str, object]] = {}
    for item in runs:
        if not isinstance(item, Mapping):
            raise ValueError("measurement run must be an object")
        run = item.get("run")
        if not isinstance(run, Mapping):
            raise ValueError("measurement cell is missing run metadata")
        key = (int(item.get("repetition", 0)), str(run.get("visibility")))
        if key in by_key:
            raise ValueError(f"duplicate measurement cell: {key}")
        by_key[key] = item

    pairs = []
    for repetition in sorted({key[0] for key in by_key}):
        baseline_cell = by_key.get((repetition, baseline))
        treatment_cell = by_key.get((repetition, treatment))
        if baseline_cell is None or treatment_cell is None:
            raise ValueError(f"incomplete treatment pair for repetition {repetition}")
        pairs.append((baseline_cell, treatment_cell))
    return pairs


def summarize_benchmark(
    report_paths: Sequence[Path],
    *,
    expected_protocol: Mapping[str, object],
    expected_source_cases: set[str] | None = None,
    baseline: str,
    treatment: str,
    metrics: Sequence[str],
    report_prefix: str = ".reports",
) -> dict[str, object]:
    case_records = []
    run_deltas: dict[str, list[float]] = {metric: [] for metric in metrics}
    case_deltas: dict[str, list[float]] = {metric: [] for metric in metrics}
    improvements = regressions = task_ties = 0
    runner: str | None = None
    model: str | None = None
    repetitions: int | None = None
    observed_source_cases: set[str] = set()
    execution_audit = {
        "runner_errors": 0,
        "tool_budget_hits": 0,
        "rejected_tool_calls": 0,
    }

    for path in report_paths:
        report = json.loads(path.read_text())
        if report.get("protocol") != expected_protocol:
            raise ValueError(f"protocol mismatch: {path}")
        if report.get("case_role") != "measurement":
            raise ValueError(f"report is not a measurement case: {path}")
        for field, expected in (
            ("runner", runner),
            ("model", model),
            ("repetitions", repetitions),
        ):
            value = report.get(field)
            if expected is not None and value != expected:
                raise ValueError(f"inconsistent {field}: {path}")
        runner = str(report["runner"])
        model = str(report["model"])
        repetitions = int(report["repetitions"])
        source_case = str(report["source_case"])
        if source_case in observed_source_cases:
            raise ValueError(f"duplicate source case: {source_case}")
        observed_source_cases.add(source_case)

        pairs = _paired_cells(report, baseline, treatment)
        per_case: dict[str, list[float]] = {metric: [] for metric in metrics}
        successes = {baseline: 0, treatment: 0}
        for baseline_cell, treatment_cell in pairs:
            baseline_success = _success(baseline_cell)
            treatment_success = _success(treatment_cell)
            successes[baseline] += baseline_success
            successes[treatment] += treatment_success
            if treatment_success and not baseline_success:
                improvements += 1
            elif baseline_success and not treatment_success:
                regressions += 1
            else:
                task_ties += 1
            if not baseline_success or not treatment_success:
                continue
            for metric in metrics:
                delta = _metric_value(treatment_cell, metric) - _metric_value(baseline_cell, metric)
                per_case[metric].append(delta)
                run_deltas[metric].append(delta)

        case_medians = {
            metric: median(values) if values else None for metric, values in per_case.items()
        }
        for item in report["runs"]:
            run = item["run"]
            execution_audit["runner_errors"] += bool(run.get("error"))
            execution_audit["tool_budget_hits"] += bool(run.get("tool_budget_exhausted"))
            rejected = run.get("rejected_tool_calls", [])
            if not isinstance(rejected, list):
                raise ValueError(f"invalid rejected_tool_calls: {path}")
            execution_audit["rejected_tool_calls"] += len(rejected)
        for metric, value in case_medians.items():
            if value is not None:
                case_deltas[metric].append(value)
        case_records.append(
            {
                "source_case": source_case,
                "report_file": f"{report_prefix}/{path.name}",
                "report_sha256": _sha256(path),
                "report_bytes": path.stat().st_size,
                "cells": len(report["runs"]),
                "successes": successes,
                "jointly_successful_pairs": len(per_case[metrics[0]]),
                "run_pair_deltas": per_case,
                "case_median_deltas": case_medians,
            }
        )

    if expected_source_cases is not None and observed_source_cases != expected_source_cases:
        raise ValueError(
            "formal report sources do not match the selection manifest: "
            f"expected={sorted(expected_source_cases)}, observed={sorted(observed_source_cases)}"
        )

    return {
        "runner": runner,
        "model": model,
        "repetitions_per_case": repetitions,
        "baseline": baseline,
        "treatment": treatment,
        "protocol": dict(expected_protocol),
        "execution_audit": execution_audit,
        "cases": case_records,
        "run_pair_descriptive": {
            "task_success": {
                "paired_observations": improvements + regressions + task_ties,
                "improvements": improvements,
                "regressions": regressions,
                "ties": task_ties,
                "exact_two_sided_sign_test_p_value": _exact_sign_p_value(improvements, regressions),
            },
            "successful_pair_efficiency": {
                metric: _direction_summary(values) for metric, values in run_deltas.items()
            },
        },
        "case_level_inference": {
            "aggregation": "median run-pair delta within each case",
            "successful_pair_efficiency": {
                metric: _direction_summary(values) for metric, values in case_deltas.items()
            },
        },
    }


def _selected_sources(path: Path, field: str) -> set[str]:
    manifest = json.loads(path.read_text())
    strata = manifest.get("strata")
    if not isinstance(strata, Mapping):
        raise ValueError(f"selection manifest is missing strata: {path}")
    selected = []
    for stratum in strata.values():
        values = stratum.get(field) if isinstance(stratum, Mapping) else None
        if not isinstance(values, list):
            raise ValueError(f"selection stratum is missing {field}: {path}")
        selected.extend(str(value) for value in values)
    if len(selected) != len(set(selected)):
        raise ValueError(f"selection manifest contains duplicate source cases: {path}")
    return set(selected)


def build_formal_measurement_summary(repo_root: Path) -> dict[str, object]:
    reports = repo_root / ".reports"
    discovery_selection = repo_root / "fixtures/measurement/discovery-selection.json"
    graph_selection = repo_root / "fixtures/measurement/graph-v3-selection.json"
    return {
        "schema_version": 1,
        "statistical_units": {
            "primary_cross_case_inference": "case",
            "run_pair_role": "describes model variation within the frozen case set",
            "case_aggregation": "median jointly-successful run-pair delta within each case",
        },
        "metric_status": {
            "rows_returned_through_evidence": "pre-registered primary efficiency metric",
            "tool_calls_through_evidence": "pre-registered primary efficiency metric",
            "discovery_calls_through_evidence": "exploratory trajectory metric",
            "reported_model_tokens": "post-hoc recorded efficiency metric",
        },
        "benchmarks": {
            "discovery_v2": {
                "selection_manifest": "fixtures/measurement/discovery-selection.json",
                "selection_manifest_sha256": _sha256(discovery_selection),
                **summarize_benchmark(
                    [reports / name for name in DISCOVERY_REPORT_FILES],
                    expected_protocol=FROZEN_DISCOVERY_PROTOCOL,
                    expected_source_cases=_selected_sources(
                        discovery_selection, "selected_after_no_model_gate"
                    ),
                    baseline="raw",
                    treatment="table_semantics",
                    metrics=(
                        "rows_returned_through_evidence",
                        "tool_calls_through_evidence",
                        "discovery_calls_through_evidence",
                        "reported_model_tokens",
                    ),
                ),
            },
            "graph_v3": {
                "selection_manifest": "fixtures/measurement/graph-v3-selection.json",
                "selection_manifest_sha256": _sha256(graph_selection),
                **summarize_benchmark(
                    [reports / name for name in GRAPH_REPORT_FILES],
                    expected_protocol=FROZEN_GRAPH_PROTOCOL,
                    expected_source_cases=_selected_sources(
                        graph_selection, "selected_after_source_gate"
                    ),
                    baseline="table_semantics",
                    treatment="semantic_graph",
                    metrics=(
                        "rows_returned_through_evidence",
                        "tool_calls_through_evidence",
                        "reported_model_tokens",
                    ),
                ),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize frozen micro-benchmark reports")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("fixtures/measurement/results-summary.json"),
    )
    args = parser.parse_args()
    root = args.repo_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    summary = build_formal_measurement_summary(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(output)


if __name__ == "__main__":
    main()
