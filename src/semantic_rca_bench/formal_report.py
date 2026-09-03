from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

from semantic_rca_bench.formal_suite import canonical_sha256
from semantic_rca_bench.formal_suite_protocol import (
    load_formal_suite_protocol,
    load_transfer_cohort,
    sha256_file,
)
from semantic_rca_bench.formal_suite_release import validate_micro_measurement_artifact
from semantic_rca_bench.report import MODEL_PRICING
from semantic_rca_bench.transfer_release import validate_measurement_artifact

FORMAL_MEASUREMENT_REPORT_SCHEMA_VERSION = 5

# Display names, upstream links, and the license statement each source declares.
# The published prose enumerates only the datasets the bound cohort actually uses.
DATASET_ATTRIBUTION = {
    "openrca": {
        "label": "OpenRCA 1.0",
        "url": "https://github.com/microsoft/OpenRCA",
        "en": "OpenRCA 1.0 declares CC BY-NC 4.0.",
        "zh": "OpenRCA 1.0 声明 CC BY-NC 4.0。",
    },
    "openrca2": {
        "label": "OpenRCA2 ops-lite",
        "url": "https://huggingface.co/datasets/anon-ops/ops-lite",
        "en": "The OpenRCA2 dataset card says Apache-2.0 while its paper says CC-BY-SA 4.0.",
        "zh": "OpenRCA2 的 dataset card 声明 Apache-2.0，论文声明 CC-BY-SA 4.0。",
    },
    "rca100": {
        "label": "RCA100",
        "url": "https://arxiv.org/abs/2606.29193",
        "en": (
            "RCA100 v1.1 declares CC BY-NC-SA 4.0 and requires attribution to its dataset paper."
        ),
        "zh": "RCA100 v1.1 声明 CC BY-NC-SA 4.0，并要求引用其数据集论文。",
    },
}

MICRO_BENCHMARK_LABELS = {
    "discovery": {"en": "Discovery", "zh": "Discovery"},
    "graph": {"en": "Graph-retrieval", "zh": "Graph retrieval"},
}

CAUSAL_SCOPE_LABELS = {
    "component": {"en": "component", "zh": "组件"},
    "dependency_edge": {"en": "dependency edge", "zh": "依赖边"},
    "infrastructure_node": {"en": "infrastructure node", "zh": "基础设施节点"},
}


def build_formal_measurement_report_from_files(
    micro_artifact_path: Path,
    transfer_artifact_path: Path,
    suite_protocol_path: Path,
    transfer_protocol_path: Path,
) -> dict[str, object]:
    micro = _load_object(micro_artifact_path)
    transfer = _load_object(transfer_artifact_path)
    validate_micro_measurement_artifact(micro, suite_protocol_path)
    validate_measurement_artifact(transfer, transfer_protocol_path)
    return build_formal_measurement_report(
        micro,
        transfer,
        suite_protocol_path,
        source_artifacts={
            "micro": _artifact_binding(micro, micro_artifact_path),
            "transfer": _artifact_binding(transfer, transfer_artifact_path),
        },
    )


def build_formal_measurement_report(
    micro: dict[str, object],
    transfer: dict[str, object],
    suite_protocol_path: Path,
    *,
    source_artifacts: dict[str, object],
) -> dict[str, object]:
    suite, protocol = load_formal_suite_protocol(suite_protocol_path)
    if (
        _mapping(transfer, "bindings").get("protocol_fixture_sha256")
        != suite.transfer_protocol_fixture_sha256
    ):
        raise ValueError("suite and transfer protocol bindings differ")
    names = [model.model for model in protocol.models]
    micro_reports = _mapping(micro, "model_reports")
    transfer_reports = _mapping(transfer, "model_reports")
    if set(micro_reports) != set(names) or set(transfer_reports) != set(names):
        raise ValueError("public artifacts do not share the frozen model roster")
    micro_runs = _mapping_list(micro, "runs")
    transfer_runs = _mapping_list(transfer, "runs")
    micro_resource_effects = _paired_resource_effects(micro_runs, transfer=False)
    transfer_families = {
        family.goal: _paired_resource_effects(
            transfer_runs,
            transfer=True,
            baseline=family.baseline.value,
            treatment=family.treatment.value,
        )
        for family in protocol.inference.confirmatory_families
        if {family.baseline.value, family.treatment.value}
        <= set(_treatments_present(transfer_runs))
    }
    # The per-model resource view is the semantic-layer family, taken from the
    # family map rather than computed a second time.
    transfer_resource_effects = transfer_families["semantic_layer"]
    case_context = {
        str(source["opaque_case_id"]): _case_context(source)
        for source in _mapping_list(transfer, "sources")
    }
    model_reports = {
        name: _combined_model_report(
            next(model.model_dump(mode="json") for model in protocol.models if model.model == name),
            _mapping(micro_reports, name),
            _mapping(transfer_reports, name),
            case_context,
            _mapping(micro_resource_effects, name),
            _mapping(transfer_resource_effects, name),
        )
        for name in names
    }
    execution = _execution(suite, protocol, micro, transfer, micro_runs, transfer_runs)
    if execution["completed_cells"] != execution["expected_cells"]:
        raise ValueError("formal measurement artifacts are incomplete")
    cohort_provenance = _cohort_provenance(suite, load_transfer_cohort(suite, suite_protocol_path))
    payload = {
        "report_schema_version": FORMAL_MEASUREMENT_REPORT_SCHEMA_VERSION,
        "report_type": "semantic-rca-measurement-report",
        "publication_status": "public measurement report",
        "research_questions": [
            {
                "goal": "semantic_layer",
                "status": "primary confirmatory",
                "question": (
                    "Does the complete GreptimeDB Semantic Graph interface reduce RCA "
                    "investigation work while preserving diagnosis and evidence validity?"
                ),
            },
            {
                "goal": "storage_shape",
                "status": "secondary confirmatory",
                "question": (
                    "How does the GreptimeDB all-in-one agent-facing interface compare with a "
                    "Prometheus, Loki and Tempo native interface bundle on the same incidents? "
                    "The arms differ in store, query languages and tool surface together, so "
                    "this is an interface-bundle comparison, not an isolated storage effect."
                ),
            },
            {
                "goal": "model_ranking",
                "status": "descriptive",
                "question": "How do the models differ at RCA under each interface?",
            },
        ],
        "license": {
            "report_schema_and_derived_aggregates": "Apache-2.0",
            "source_telemetry_redistributed": False,
            "micro_source_terms": micro.get("license"),
            "transfer_source_terms": transfer.get("license"),
        },
        "scope": {
            "protocol_revision": suite.protocol_revision,
            "benchmark_protocol_version": suite.benchmark_protocol_version,
            "greptimedb_revision": suite.greptimedb_revision,
            "greptimedb_build_profile": suite.greptimedb_build_profile,
            "provider_execution_provenance": (
                "provider trajectories executed before the public release tag; the tagged "
                "code reproduces scoring and report generation from sanitized artifacts"
            ),
            "treatment_estimand": _mapping(transfer, "benchmark_protocol").get(
                "treatment_estimand"
            ),
            "treatment_components": _mapping(transfer, "benchmark_protocol").get(
                "treatment_components"
            ),
            "inference": transfer.get("inference"),
        },
        "execution": execution,
        "cohort_provenance": cohort_provenance,
        "case_catalog": list(case_context.values()),
        "source_artifacts": source_artifacts,
        "model_order": names,
        "model_reports": model_reports,
        "case_outcomes": _case_outcomes(transfer_runs, case_context),
        "capability_scores": _capability_scores(transfer_runs, names),
        "confirmatory_family_resource_effects": transfer_families,
        "semantic_layer_findings": _semantic_findings(model_reports),
        "resource_effect_contract": {
            "role": "descriptive; not a pre-registered endpoint",
            "delta": "semantic_graph minus raw within the same model, case, and repetition",
            "eligibility": "the same paired eligibility used by the corresponding benchmark",
            "provider_visible_input_tokens": "all input reported by the provider",
            "output_tokens": "all output reported by the provider",
            "reasoning_output_tokens": "a subset of output_tokens; never added twice",
            "estimated_cost": (
                "per-run frozen provider pricing, including distinct uncached, cache-read, "
                "cache-write, and output rates when available"
            ),
            "actual_cost_by_treatment": (
                "sum over all executed runs in that treatment, including unsuccessful runs"
            ),
            "missing_cost": "not estimated when either side of a pair is not priceable",
        },
        "costs": _cost_report(model_reports),
        "audit": _audit(micro, transfer),
        "limitations": [
            (
                f"The {execution['micro_cases']}-case micro cohort is fixed reference data; the "
                f"{execution['transfer_cases']}-case end-to-end cohort is source-ranked. Ten "
                "of its cases carry over from the previously published measurement rather "
                "than being reselected."
            ),
            (
                "Case-level within-model estimates apply to these systems and mechanisms; "
                "they are not a universal observability-workload effect."
            ),
            (
                "Null is not estimable, zero means no observed reduction, and a "
                "non-significant test does not establish equivalence."
            ),
            "Provider reasoning settings are frozen configurations, not a common compute scale.",
            (
                "Deterministic evidence sufficiency is reported as a secondary audit and does "
                "not control headline efficiency eligibility."
            ),
            (
                f"The {capability_rubric_label()} capability score is a post-measurement "
                "descriptive index. It is "
                "not a pre-registered endpoint and its evidence component inherits verifier "
                "coverage limits."
            ),
            (
                "Mechanism cohorts are unevenly sized: "
                f"{_mechanism_cohort_phrase(_mechanism_cohort(case_context.values()), 'en')}. "
                "Mechanism-level summaries are descriptive and unevenly supported."
            ),
            "Costs retain provider currencies; currencies are not converted.",
        ],
    }
    return {
        **payload,
        "integrity": {
            "semantic_payload_sha256": canonical_sha256(payload),
            "hash_scope": "all report fields except integrity",
        },
    }


def validate_formal_measurement_report(report: dict[str, object]) -> None:
    if (
        report.get("report_schema_version") != FORMAL_MEASUREMENT_REPORT_SCHEMA_VERSION
        or report.get("report_type") != "semantic-rca-measurement-report"
    ):
        raise ValueError("unsupported formal measurement report")
    execution = _mapping(report, "execution")
    if execution.get("completed_cells") != execution.get("expected_cells"):
        raise ValueError("formal measurement report execution is incomplete")
    order = report.get("model_order")
    models = _mapping(report, "model_reports")
    if not isinstance(order, list) or set(order) != set(models):
        raise ValueError("formal measurement report model roster drifted")
    payload = {key: value for key, value in report.items() if key != "integrity"}
    if _mapping(report, "integrity").get("semantic_payload_sha256") != canonical_sha256(payload):
        raise ValueError("formal measurement report semantic payload hash drifted")


def render_formal_measurement_report(report: dict[str, object], output: Path) -> None:
    validate_formal_measurement_report(report)
    template = (
        files("semantic_rca_bench")
        .joinpath("assets/formal-measurement-report.html")
        .read_text(encoding="utf-8")
    )
    serialized = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    document = (
        template.replace("__REPORT_TITLE__", "Semantic RCA Bench — 2026 report")
        .replace("__REPORT_BODY__", _report_body(report))
        .replace("__REPORT_DATA__", serialized)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def _execution(suite, protocol, micro, transfer, micro_runs, transfer_runs):
    return {
        "expected_cells": suite.expected_total_cells,
        "completed_cells": len(micro_runs) + len(transfer_runs),
        "micro_cells": len(micro_runs),
        "transfer_cells": len(transfer_runs),
        "runner_errors": sum(item.get("runner_error") is True for item in micro_runs)
        + sum(
            _mapping(_mapping(item, "run"), "execution").get("runner_error") is True
            for item in transfer_runs
        ),
        "budget_exhaustions": sum(item.get("tool_budget_exhausted") is True for item in micro_runs)
        + sum(
            _mapping(_mapping(item, "run"), "execution").get("tool_budget_exhausted") is True
            for item in transfer_runs
        ),
        "models": len(protocol.models),
        "micro_cases": len(_mapping_list(micro, "sources")),
        "transfer_cases": len(_mapping_list(transfer, "sources")),
        "treatments": [item.value for item in protocol.visibility_levels],
        "repetitions_per_model_case": protocol.repetitions_per_model,
    }


def _cohort_provenance(suite, cohort) -> dict[str, object]:
    micro = Counter((case.benchmark, case.adapter) for case in suite.micro_cases)
    return {
        "micro": [
            {"benchmark": benchmark, "dataset": adapter, "cases": count}
            for (benchmark, adapter), count in sorted(micro.items())
        ],
        "transfer": [
            {"dataset": source.adapter, "cases": len(source.case_ids)} for source in cohort.sources
        ],
    }


def _mechanism_cohort(cases) -> list[tuple[str, int]]:
    counts = Counter(str(case["mechanism_code"]) for case in cases)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _mechanism_cohort_phrase(counts: list[tuple[str, int]], language: str) -> str:
    separator = "、" if language == "zh" else ", "
    return separator.join(f"{_mechanism_label(code, language)} {count}" for code, count in counts)


def _combined_model_report(
    configuration,
    micro,
    transfer,
    case_context,
    micro_resource_effects,
    transfer_resource_effects,
):
    micro_reliability = _mapping(micro, "reliability")
    transfer_reliability = _mapping(transfer, "reliability")
    micro = _micro_with_resource_effects(micro, micro_resource_effects)
    transfer = _transfer_with_case_context(
        transfer, case_context, _mapping(transfer_resource_effects, "cases")
    )
    transfer["actual_cost_by_treatment"] = _mapping(
        transfer_resource_effects, "actual_cost_by_treatment"
    )
    transfer["cost_currency"] = transfer_resource_effects.get("cost_currency")
    transfer["descriptive_metrics"] = {
        **_mapping(transfer, "descriptive_metrics"),
        **_mapping(transfer_resource_effects, "summary"),
    }
    usage = _combined_usage(_mapping(micro, "usage"), _mapping(transfer, "usage"))
    usage["actual_cost_by_treatment"] = _combined_actual_cost_by_treatment(
        micro_resource_effects, transfer_resource_effects
    )
    usage["cost_currency"] = transfer_resource_effects.get("cost_currency")
    return {
        "configuration": configuration,
        "micro": micro,
        "transfer": transfer,
        "reliability": {
            "runs": int(micro_reliability.get("runs", 0)) + int(transfer.get("runs", 0)),
            "runner_errors": int(micro_reliability.get("runner_errors", 0))
            + int(transfer_reliability.get("runner_errors", 0)),
            "budget_exhaustions": int(micro_reliability.get("budget_exhaustions", 0))
            + int(transfer_reliability.get("budget_exhaustions", 0)),
            "failed_database_queries": int(transfer_reliability.get("failed_database_queries", 0)),
        },
        "usage": {
            "micro": micro.get("usage"),
            "transfer": transfer.get("usage"),
            "combined": usage,
        },
    }


def _combined_actual_cost_by_treatment(
    micro_resource_effects: Mapping[str, object],
    transfer_resource_effects: Mapping[str, object],
) -> dict[str, float | None]:
    result = {}
    for visibility in ("raw", "semantic_graph"):
        values = [_mapping(transfer_resource_effects, "actual_cost_by_treatment").get(visibility)]
        for benchmark in micro_resource_effects.values():
            if not isinstance(benchmark, Mapping):
                raise ValueError("micro resource effect is not an object")
            values.append(_mapping(benchmark, "actual_cost_by_treatment").get(visibility))
        result[visibility] = (
            sum(float(value) for value in values)
            if all(isinstance(value, (int, float)) for value in values)
            else None
        )
    return result


def _case_target(source: Mapping[str, object]) -> str:
    component = source.get("causal_component")
    if isinstance(component, str):
        return component
    edge_source = source.get("edge_source")
    edge_destination = source.get("edge_destination")
    if isinstance(edge_source, str) and isinstance(edge_destination, str):
        return f"{edge_source} -> {edge_destination}"
    raise ValueError("transfer source has no publishable causal target")


def _case_context(source: Mapping[str, object]) -> dict[str, object]:
    # None when the source offers no signal the deterministic oracle can express.
    raw_oracle = source.get("oracle")
    oracle = _mapping(source, "oracle") if raw_oracle is not None else None
    return {
        "case_id": source["opaque_case_id"],
        "source_case": source["source_case"],
        "system": source["system"],
        "mechanism_code": source["mechanism_code"],
        "causal_scope": source["causal_scope"],
        "target": _case_target(source),
        "oracle": oracle,
    }


def _transfer_with_case_context(
    transfer: Mapping[str, object],
    case_context: Mapping[str, Mapping[str, object]],
    resource_effects: Mapping[str, object],
) -> dict[str, object]:
    enriched = dict(transfer)
    case_effects = []
    for effect in _mapping_list(transfer, "case_effects"):
        context = case_context.get(str(effect.get("case_id")))
        if context is None:
            raise ValueError("transfer case effect has no source context")
        resources = resource_effects.get(str(effect.get("case_id")), {})
        if not isinstance(resources, Mapping):
            raise ValueError("transfer resource effect is not an object")
        case_effects.append({**effect, **resources, **context})
    enriched["case_effects"] = case_effects
    enriched["mechanism_effects"] = _mechanism_effects(case_effects)
    return enriched


def _mechanism_effects(case_effects: list[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for effect in case_effects:
        grouped[str(effect["mechanism_code"])].append(effect)
    rows = []
    for mechanism, effects in sorted(grouped.items()):
        metrics = {
            metric: _descriptive_effect(
                [effect.get(metric) for effect in effects if effect.get(metric) is not None]
            )
            for metric in (
                "rows_returned",
                "correct_completion_tool_calls",
                "reported_total_tokens",
                "provider_visible_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "estimated_cost",
            )
        }
        currencies = {
            str(effect["estimated_cost_currency"])
            for effect in effects
            if isinstance(effect.get("estimated_cost_currency"), str)
        }
        metrics["estimated_cost"]["currency"] = (
            next(iter(currencies)) if len(currencies) == 1 else None
        )
        rows.append(
            {
                "mechanism_code": mechanism,
                "cohort_cases": len(effects),
                "eligible_cases": metrics["rows_returned"]["eligible_cases"],
                "metrics": metrics,
            }
        )
    return rows


def _micro_with_resource_effects(
    micro: Mapping[str, object], resource_effects: Mapping[str, object]
) -> dict[str, object]:
    enriched = dict(micro)
    benchmarks = {}
    for benchmark, value in _mapping(micro, "benchmarks").items():
        if not isinstance(value, Mapping):
            raise ValueError("micro benchmark report is not an object")
        benchmark_resources = resource_effects.get(benchmark)
        if not isinstance(benchmark_resources, Mapping):
            raise ValueError("micro resource effect is not an object")
        benchmarks[benchmark] = {**value, "resource_effects": dict(benchmark_resources)}
    enriched["benchmarks"] = benchmarks
    return enriched


def _descriptive_effect(values: list[object]) -> dict[str, object]:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    return {
        "eligible_cases": len(numbers),
        "case_median_delta": _median(numbers),
        "negative_cases": sum(value < 0 for value in numbers),
        "tied_cases": sum(value == 0 for value in numbers),
        "positive_cases": sum(value > 0 for value in numbers),
        "inference_role": "descriptive only",
    }


RESOURCE_EFFECT_METRICS = (
    "provider_visible_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "estimated_cost",
)


def _paired_resource_effects(
    runs: list[Mapping[str, object]],
    *,
    transfer: bool,
    baseline: str = "raw",
    treatment: str = "semantic_graph",
) -> dict[str, object]:
    """Per-case resource deltas for one named pair of treatments.

    The pair is named rather than inferred from the cell's contents: requiring
    the cell to hold exactly two treatments dropped every pair once a third arm
    existed, and reported no estimable effects instead of failing.
    """
    grouped: dict[str, dict[str, dict[str, dict[int, dict[str, Mapping[str, object]]]]]] = (
        defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(dict))))
    )
    currencies: dict[str, set[str]] = defaultdict(set)
    for item in runs:
        model = str(item["model"])
        cohort = "transfer" if transfer else str(item["benchmark"])
        case = str(item["case_id"] if transfer else item["source_case"])
        grouped[model][cohort][case][int(item["repetition"])][str(item["visibility"])] = item
        currency = _resource_usage(item, transfer).get("cost_currency")
        if isinstance(currency, str):
            currencies[model].add(currency)

    reports = {}
    for model, cohorts in grouped.items():
        cohort_reports = {}
        for cohort, cases in cohorts.items():
            case_reports = {}
            for case, repetitions in cases.items():
                deltas: dict[str, list[float]] = defaultdict(list)
                eligible_repetitions = 0
                for pair in repetitions.values():
                    missing = {baseline, treatment} - set(pair)
                    if missing:
                        raise ValueError(f"resource pair for {case} is missing {sorted(missing)}")
                    raw = pair[baseline]
                    graph = pair[treatment]
                    if not (
                        _resource_run_eligible(raw, transfer)
                        and _resource_run_eligible(graph, transfer)
                    ):
                        continue
                    eligible_repetitions += 1
                    raw_usage = _resource_usage(raw, transfer)
                    graph_usage = _resource_usage(graph, transfer)
                    for metric in RESOURCE_EFFECT_METRICS:
                        raw_value = _resource_usage_value(raw_usage, metric)
                        graph_value = _resource_usage_value(graph_usage, metric)
                        if isinstance(raw_value, (int, float)) and isinstance(
                            graph_value, (int, float)
                        ):
                            deltas[metric].append(float(graph_value) - float(raw_value))
                case_reports[case] = {
                    "eligible_repetitions": eligible_repetitions,
                    **{metric: _median(values) for metric, values in deltas.items()},
                }
                for visibility in (baseline, treatment):
                    costs = [
                        _resource_usage(pair[visibility], transfer).get("estimated_cost")
                        for pair in repetitions.values()
                        if visibility in pair
                    ]
                    case_reports[case][f"actual_cost_{visibility}"] = (
                        sum(float(value) for value in costs)
                        if len(costs) == len(repetitions)
                        and all(isinstance(value, (int, float)) for value in costs)
                        else None
                    )
            summary = {
                metric: _descriptive_effect(
                    [
                        value.get(metric)
                        for value in case_reports.values()
                        if value.get(metric) is not None
                    ]
                )
                for metric in RESOURCE_EFFECT_METRICS
            }
            currency = next(iter(currencies[model])) if len(currencies[model]) == 1 else None
            summary["estimated_cost"]["currency"] = currency
            for value in case_reports.values():
                value["estimated_cost_currency"] = currency
            actual_cost_by_treatment = {}
            for visibility in (baseline, treatment):
                values = [value[f"actual_cost_{visibility}"] for value in case_reports.values()]
                actual_cost_by_treatment[visibility] = (
                    sum(float(value) for value in values)
                    if all(isinstance(value, (int, float)) for value in values)
                    else None
                )
            cohort_reports[cohort] = {
                "summary": summary,
                "cases": case_reports,
                "actual_cost_by_treatment": actual_cost_by_treatment,
                "cost_currency": currency,
            }
        reports[model] = cohort_reports["transfer"] if transfer else cohort_reports
    return reports


def _resource_run_eligible(item: Mapping[str, object], transfer: bool) -> bool:
    if transfer:
        evaluation = _mapping(_mapping(item, "run"), "evaluation")
        return evaluation.get("efficiency_eligible") is True
    return _mapping(item, "evaluation").get("success") is True


def _resource_usage(item: Mapping[str, object], transfer: bool) -> dict[str, object]:
    return _mapping(_mapping(item, "run"), "usage") if transfer else _mapping(item, "usage")


def _resource_usage_value(usage: Mapping[str, object], metric: str) -> int | float | None:
    if metric == "provider_visible_input_tokens":
        return _provider_visible_input(usage)
    value = usage.get(metric)
    return value if isinstance(value, (int, float)) else None


def _combined_usage(
    micro: Mapping[str, object], transfer: Mapping[str, object]
) -> dict[str, object]:
    return {
        "provider_visible_input_tokens": _sum_known(
            _provider_visible_input(micro), _provider_visible_input(transfer)
        ),
        "uncached_input_tokens": _sum_known(
            micro.get("uncached_input_tokens"), transfer.get("uncached_input_tokens")
        ),
        "cache_read_input_tokens": _sum_known(
            micro.get("cache_read_input_tokens"), transfer.get("cache_read_input_tokens")
        ),
        "cache_creation_input_tokens": _sum_known(
            micro.get("cache_creation_input_tokens"),
            transfer.get("cache_creation_input_tokens"),
        ),
        "output_tokens": _sum_known(micro.get("output_tokens"), transfer.get("output_tokens")),
        "reasoning_output_tokens": _sum_known(
            micro.get("reasoning_output_tokens"), transfer.get("reasoning_output_tokens")
        ),
        "input_breakdown_complete": all(
            part.get("cache_breakdown_complete", True) is True for part in (micro, transfer)
        ),
        "reasoning_is_subset_of_output": True,
    }


def _provider_visible_input(usage: Mapping[str, object]) -> int | float | None:
    total = usage.get("provider_visible_input_tokens")
    if isinstance(total, (int, float)):
        return total
    return _sum_known(
        usage.get("uncached_input_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cache_creation_input_tokens"),
    )


def _sum_known(*values: object) -> int | float | None:
    numbers = [value for value in values if isinstance(value, (int, float))]
    if len(numbers) != len(values):
        return None
    return sum(numbers)


def _median(values: list[float]) -> int | float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        value = ordered[midpoint]
    else:
        value = (ordered[midpoint - 1] + ordered[midpoint]) / 2
    return int(value) if value.is_integer() else value


CAPABILITY_SCORE_RUBRIC = {
    "causal_scope_match": {"points": 10, "label": "causal scope", "dimension": "location"},
    "causal_locus_match": {
        "points": 30,
        "label": "declared causal locus",
        "dimension": "location",
    },
    "fault_category_match": {
        "points": 10,
        "label": "fault category",
        "dimension": "root_cause",
    },
    "mechanism_code_match": {
        "points": 30,
        "label": "root-cause mechanism",
        "dimension": "root_cause",
    },
    "has_execution_valid_citation": {
        "points": 5,
        "label": "executed evidence",
        "dimension": "evidence",
    },
}

# Deliberately outside the rubric. The deterministic proof is decidable only for
# SQL evidence, so scoring it would rank a model that cited PromQL above one that
# cited SQL and failed the audit — an availability bias on top of the treatment
# bias of scoring an unmeasured dimension as a failure. It is reported beside the
# score instead.
CAPABILITY_UNSCORED_DIMENSIONS = ("required_evidence_covered",)


def _capability_scores(
    runs: list[Mapping[str, object]], model_names: list[str]
) -> dict[str, object]:
    reports = {}
    for model in model_names:
        model_runs = [item for item in runs if item.get("model") == model]
        treatments = {
            visibility: _score_runs(
                [item for item in model_runs if item.get("visibility") == visibility]
            )
            for visibility in _treatments_present(runs)
        }
        reports[model] = {
            "overall": _score_runs(model_runs),
            "by_treatment": treatments,
        }
    return {
        "role": (
            "post-measurement descriptive capability index; not a pre-registered endpoint "
            "and not used for hypothesis testing"
        ),
        "overall_formula": (
            "sum of rubric points over every end-to-end run for the model divided by the "
            "points that were estimable for those runs; every treatment contributes "
            "equally, failed runs remain in the denominator, and a dimension the "
            "verifier cannot decide for a treatment is excluded from both sides rather "
            "than scored as a failure"
        ),
        "ranking_basis": "normalized_score",
        "method_basis": [
            {
                "benchmark": "RCAEval",
                "use": "reports coarse service and fine-grained root-cause accuracy separately",
                "source": (
                    "https://github.com/phamquiluan/RCAEval/blob/main/"
                    "RCAEval/benchmark/evaluation.py"
                ),
            },
            {
                "benchmark": "RCAgentBench",
                "use": "uses a 40% location, 40% type, and 20% explainability split",
                "source": "https://github.com/CSTCloudOps/RCAgentBench/blob/main/eval.py",
            },
        ],
        "rubric": CAPABILITY_SCORE_RUBRIC,
        "models": reports,
        "rankings": {
            "overall": _score_ranking(reports, None),
            **{
                treatment: _score_ranking(reports, treatment)
                for treatment in _treatments_present(runs)
            },
        },
    }


def _score_runs(runs: list[Mapping[str, object]]) -> dict[str, object]:
    hits = {key: 0 for key in CAPABILITY_SCORE_RUBRIC}
    dimension_points = {"location": 0, "root_cause": 0, "evidence": 0}
    total = 0
    unscored: dict[str, Counter[str]] = {key: Counter() for key in CAPABILITY_UNSCORED_DIMENSIONS}
    for item in runs:
        evaluation = _mapping(_mapping(item, "run"), "evaluation")
        for key in CAPABILITY_UNSCORED_DIMENSIONS:
            value = evaluation.get(key)
            unscored[key][
                "not_estimable" if value is None else "covered" if value is True else "failed"
            ] += 1
        for key, contract in CAPABILITY_SCORE_RUBRIC.items():
            points = int(contract["points"])
            if key == "has_execution_valid_citation":
                matched = int(evaluation.get("valid_evidence_count", 0) or 0) > 0
            else:
                matched = evaluation.get(key) is True
            if matched:
                hits[key] += 1
                total += points
                dimension_points[str(contract["dimension"])] += points
    maximum = len(runs) * sum(int(c["points"]) for c in CAPABILITY_SCORE_RUBRIC.values())
    return {
        "runs": len(runs),
        "score": round(total / len(runs), 2) if runs else None,
        "points": total,
        "maximum_points": maximum,
        "normalized_score": round(100 * total / maximum, 2) if maximum else None,
        "unscored_dimensions": {
            key: dict(sorted(counts.items())) for key, counts in unscored.items()
        },
        "component_hits": hits,
        "average_dimension_points": {
            dimension: round(points / len(runs), 2) if runs else None
            for dimension, points in dimension_points.items()
        },
    }


def _score_ranking(
    reports: Mapping[str, Mapping[str, object]], treatment: str | None
) -> list[dict[str, object]]:
    values = []
    for model, report in reports.items():
        # Ranked on the normalized score: a treatment whose evidence grounding
        # is not estimable would otherwise rank below one that was measured, for
        # the missing instrument rather than for the model.
        source = (
            _mapping(report, "overall")
            if treatment is None
            else _mapping(_mapping(report, "by_treatment"), treatment)
        )
        score = source.get("normalized_score")
        values.append((model, score))
    values.sort(key=lambda item: (-float(item[1]), item[0]))
    return [
        {"rank": index + 1, "model": model, "score": score}
        for index, (model, score) in enumerate(values)
    ]


def _treatments_present(runs: list[Mapping[str, object]]) -> tuple[str, ...]:
    return tuple(sorted({str(item["visibility"]) for item in runs})) or ("raw", "semantic_graph")


def _rows_returned_delta(
    baseline_run: Mapping[str, object], treatment_run: Mapping[str, object]
) -> float | None:
    """`rows_returned` is null in the split arm, where a row is not a row."""
    values = [
        _mapping(run, "database_load").get("rows_returned") for run in (baseline_run, treatment_run)
    ]
    if any(value is None for value in values):
        return None
    return float(values[1]) - float(values[0])


def _case_outcomes(
    runs: list[Mapping[str, object]],
    case_context: Mapping[str, Mapping[str, object]],
    *,
    baseline: str = "raw",
    treatment: str = "semantic_graph",
) -> list[dict[str, object]]:
    """Per-case outcomes for one named pair; see `_paired_resource_effects`."""
    outcomes = []
    for case_id, context in case_context.items():
        case_runs = [item for item in runs if item.get("case_id") == case_id]
        diagnosis = {
            visibility: sum(
                _mapping(_mapping(item, "run"), "evaluation").get("diagnosis_correct") is True
                for item in case_runs
                if item.get("visibility") == visibility
            )
            for visibility in _treatments_present(runs)
        }
        effects = []
        for model in sorted({str(item["model"]) for item in case_runs}):
            model_runs = [item for item in case_runs if item.get("model") == model]
            pair_values: dict[int, dict[str, Mapping[str, object]]] = defaultdict(dict)
            for item in model_runs:
                pair_values[int(item["repetition"])][str(item["visibility"])] = item
            eligible_deltas = []
            for pair in pair_values.values():
                missing = {baseline, treatment} - set(pair)
                if missing:
                    raise ValueError(f"case pair for {case_id} is missing {sorted(missing)}")
                raw = _mapping(pair[baseline], "run")
                graph = _mapping(pair[treatment], "run")
                raw_eval = _mapping(raw, "evaluation")
                graph_eval = _mapping(graph, "evaluation")
                if not (
                    raw_eval.get("efficiency_eligible") is True
                    and graph_eval.get("efficiency_eligible") is True
                ):
                    continue
                eligible_deltas.append(
                    {
                        "rows": _rows_returned_delta(raw, graph),
                        "calls": int(graph_eval["correct_completion_tool_calls"])
                        - int(raw_eval["correct_completion_tool_calls"]),
                        "tokens": _public_reported_tokens(graph) - _public_reported_tokens(raw),
                        "input": _resource_delta(raw, graph, "provider_visible_input_tokens"),
                        "output": _resource_delta(raw, graph, "output_tokens"),
                        "cost": _resource_delta(raw, graph, "estimated_cost"),
                    }
                )
            effects.append(
                {
                    "model": model,
                    "eligible_repetitions": len(eligible_deltas),
                    "rows_returned": _median(
                        [
                            float(item["rows"])
                            for item in eligible_deltas
                            if item["rows"] is not None
                        ]
                    ),
                    "correct_completion_tool_calls": _median(
                        [float(item["calls"]) for item in eligible_deltas]
                    ),
                    "reported_total_tokens": _median(
                        [float(item["tokens"]) for item in eligible_deltas]
                    ),
                    "provider_visible_input_tokens": _median(
                        [
                            float(item["input"])
                            for item in eligible_deltas
                            if item["input"] is not None
                        ]
                    ),
                    "output_tokens": _median(
                        [
                            float(item["output"])
                            for item in eligible_deltas
                            if item["output"] is not None
                        ]
                    ),
                    "estimated_cost": _median(
                        [
                            float(item["cost"])
                            for item in eligible_deltas
                            if item["cost"] is not None
                        ]
                    ),
                }
            )
        outcomes.append(
            {
                **context,
                "diagnosis_correct": diagnosis,
                "eligible_models": sum(item["eligible_repetitions"] > 0 for item in effects),
                "models_with_fewer_rows": sum(
                    isinstance(item["rows_returned"], (int, float)) and item["rows_returned"] < 0
                    for item in effects
                ),
                "models_with_fewer_calls": sum(
                    isinstance(item["correct_completion_tool_calls"], (int, float))
                    and item["correct_completion_tool_calls"] < 0
                    for item in effects
                ),
                "models_with_fewer_tokens": sum(
                    isinstance(item["reported_total_tokens"], (int, float))
                    and item["reported_total_tokens"] < 0
                    for item in effects
                ),
                "models_with_fewer_input_tokens": sum(
                    isinstance(item["provider_visible_input_tokens"], (int, float))
                    and item["provider_visible_input_tokens"] < 0
                    for item in effects
                ),
                "models_with_fewer_output_tokens": sum(
                    isinstance(item["output_tokens"], (int, float)) and item["output_tokens"] < 0
                    for item in effects
                ),
                "models_with_lower_estimated_cost": sum(
                    isinstance(item["estimated_cost"], (int, float)) and item["estimated_cost"] < 0
                    for item in effects
                ),
                "models_with_estimable_cost": sum(
                    isinstance(item["estimated_cost"], (int, float)) for item in effects
                ),
            }
        )
    return outcomes


def _public_reported_tokens(run: Mapping[str, object]) -> int:
    usage = _mapping(run, "usage")
    return int(usage.get("provider_visible_input_tokens", 0) or 0) + int(
        usage.get("output_tokens", 0) or 0
    )


def _resource_delta(
    raw: Mapping[str, object], graph: Mapping[str, object], metric: str
) -> int | float | None:
    raw_value = _resource_usage_value(_mapping(raw, "usage"), metric)
    graph_value = _resource_usage_value(_mapping(graph, "usage"), metric)
    if not isinstance(raw_value, (int, float)) or not isinstance(graph_value, (int, float)):
        return None
    return graph_value - raw_value


def _semantic_findings(reports):
    mechanism_rows: dict[str, dict[str, int]] = defaultdict(
        lambda: {"improved_models": 0, "tied_models": 0, "regressed_models": 0}
    )
    end_to_end_cost = {}
    for model, report in reports.items():
        transfer = _mapping(report, "transfer")
        for mechanism in _mapping_list(transfer, "mechanism_effects"):
            effect = _mapping(_mapping(mechanism, "metrics"), "rows_returned")
            value = effect.get("case_median_delta")
            if not isinstance(value, (int, float)):
                continue
            direction = (
                "improved_models"
                if value < 0
                else "regressed_models"
                if value > 0
                else "tied_models"
            )
            mechanism_rows[str(mechanism["mechanism_code"])][direction] += 1
        costs = _mapping(transfer, "actual_cost_by_treatment")
        raw = costs.get("raw")
        graph = costs.get("semantic_graph")
        end_to_end_cost[model] = {
            "raw": raw,
            "semantic_graph": graph,
            "delta": (
                float(graph) - float(raw)
                if isinstance(raw, (int, float)) and isinstance(graph, (int, float))
                else None
            ),
            "currency": transfer.get("cost_currency"),
        }
    return {
        "end_to_end_transfer": {
            model: _mapping(_mapping(value, "transfer"), "primary_metrics")
            for model, value in reports.items()
        },
        "end_to_end_actual_cost": end_to_end_cost,
        "mechanism_row_direction_by_model": dict(mechanism_rows),
        "interpretation_contract": (
            "Headline efficiency conditions on a correct diagnosis, at least one "
            "execution-valid citation, and a reliable run. Deterministic evidence sufficiency "
            "is a secondary audit. Negative deltas favor Semantic Graph. Null is not "
            "estimable; zero is no observed reduction; non-significance is insufficient "
            "evidence, not equivalence."
        ),
    }


def _cost_report(reports):
    totals = defaultdict(float)
    models = {}
    unavailable = []
    for model, report in reports.items():
        usage = _mapping(report, "usage")
        parts = [_mapping(usage, "micro"), _mapping(usage, "transfer")]
        currencies = {part.get("cost_currency") for part in parts}
        costs = [part.get("estimated_cost") for part in parts]
        currency = next(iter(currencies)) if len(currencies) == 1 else None
        cost = (
            sum(float(value) for value in costs)
            if all(isinstance(value, (int, float)) for value in costs)
            else None
        )
        if cost is not None and isinstance(currency, str):
            status = "available"
            reason = None
            reason_code = None
        else:
            status = "not_estimable"
            pricing = MODEL_PRICING.get(model, {})
            combined_usage = _mapping(usage, "combined")
            if pricing.get("cost_available") is False:
                reason_code = "model_price_not_frozen"
                reason = pricing.get("note") or "No frozen model-specific price is available."
            elif combined_usage.get("input_breakdown_complete") is False:
                reason_code = "cache_breakdown_incomplete"
                reason = (
                    "At least one run lacks a complete cache breakdown; the pricing contract "
                    "rejects a partial estimate."
                )
            else:
                reason_code = "usage_not_priceable"
                reason = "The provider usage cannot be priced under the frozen contract."
        models[model] = {
            "status": status,
            "estimated_cost": cost,
            "currency": currency,
            "unavailable_reason_code": reason_code,
            "unavailable_reason": reason,
        }
        if cost is not None and isinstance(currency, str):
            totals[currency] += cost
        else:
            unavailable.append(model)
    pricing_basis = {}
    for model in reports:
        pricing = MODEL_PRICING.get(model, {})
        pricing_basis[model] = {
            "currency": pricing.get("currency"),
            "uncached_input_per_million": pricing.get("input_per_million"),
            "cache_read_per_million": pricing.get("input_cache_hit_per_million"),
            "cache_write_per_million": pricing.get("input_cache_write_per_million"),
            "output_per_million": pricing.get("output_per_million"),
            "checked_at": pricing.get("checked_at"),
            "source": pricing.get("source"),
        }
    return {
        "models": models,
        "pricing_basis": pricing_basis,
        "known_totals_by_currency": dict(sorted(totals.items())),
        "models_with_unavailable_estimate": unavailable,
        "cross_currency_total": None,
        "aggregation_policy": (
            "Only complete estimates in the same currency are subtotaled. No cross-currency "
            "total is reported, and partial model estimates are rejected."
        ),
    }


def _audit(micro, transfer):
    return {
        "micro_no_model_gates_passed": all(
            _mapping(source, "no_model_gates").get("all_passed") is True
            for source in _mapping_list(micro, "sources")
        ),
        "transfer_no_model_gates_passed": all(
            _mapping(source, "no_model_gates").get("all_passed") is True
            for source in _mapping_list(transfer, "sources")
        ),
        "transfer_raw_graph_exact_edge_equality": all(
            _mapping(source, "edge_equality").get("exact_edge_set_equality") is True
            for source in _mapping_list(transfer, "sources")
        ),
        "private_provider_payloads_included": False,
        "raw_telemetry_rows_included": False,
    }


def _report_body(report):
    return f"""<div class="language-switch" role="group" aria-label="Report language">
<a href="#en" data-language-button="en">English</a>
<a href="#zh" data-language-button="zh">中文</a></div>
<div id="en" data-report-language="en">{_localized_report_body(report, "en")}</div>
<div id="zh" data-report-language="zh" hidden>{_localized_report_body(report, "zh")}</div>"""


def _localized_report_body(report, language):
    execution = _mapping(report, "execution")
    scope = _mapping(report, "scope")
    cards = "".join(
        _model_card(model, _mapping(_mapping(report, "model_reports"), model), language)
        for model in report["model_order"]
    )
    audit = _mapping(report, "audit")
    model_count = len(report["model_order"])
    transfer_cases = int(execution["transfer_cases"])
    repetitions = int(execution["repetitions_per_model_case"])
    transfer_runs_per_treatment = transfer_cases * repetitions
    transfer_runs_per_model = transfer_runs_per_treatment * len(execution["treatments"])
    case_model_pairs = transfer_cases * model_count
    mechanism_counts = _mechanism_cohort(_mapping_list(report, "case_catalog"))
    report_json = f"semantic-rca-v{scope['benchmark_protocol_version']}.json"
    attribution_text, attribution_terms, attribution_links = _attribution(report, language)
    mechanism_cells = sum(
        len(
            _mapping_list(
                _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer"),
                "mechanism_effects",
            )
        )
        for model in report["model_order"]
    )
    if language == "zh":
        eyebrow = "GreptimeDB Semantic Graph · 配对 Agent Benchmark"
        lede = "LLM agent 使用 Raw telemetry 或完整 Semantic Graph 调查真实故障的开放评测。"
        actions = (
            f'<a href="{report_json}" download>下载报告 JSON</a>'
            '<a href="https://github.com/GreptimeTeam/semantic-rca-bench#reproduce-the-published-report">复现报告</a>'
            '<a href="https://github.com/GreptimeTeam/semantic-rca-bench">查看源码</a>'
        )
        labels = {
            "cells": "已完成 runs",
            "models": "模型",
            "cases": "故障场景",
            "result": "主要结论",
        }
        overview = "这是什么"
        overview_text = (
            f"同一个 LLM agent、同一个故障分别在 {len(execution['treatments'])} 种接口下运行："
            "Split 只提供 Prometheus、Loki、Tempo 各自的原生查询 API；"
            "Raw 提供 GreptimeDB 的遥测表和只读 SQL；"
            "Graph 在相同数据上再加表语义、实体、关系和查询工具。"
            "评测关注诊断正确时，一体化接口和语义层各自能否减少调查所需的检索和工具调用。"
        )
        reproducibility_text = (
            f"仓库公开全部 {execution['completed_cells']} 次运行的脱敏记录，"
            "包括工具输入、SQL、结果投影、评分事实和哈希。"
            "对应 tag 的代码无需调用模型即可重新生成所有聚合结果和本页面。"
        )
        glossary = (
            ("Run", "一个模型 × 一个故障 × 一个 treatment × 一次重复。"),
            ("Raw / Graph", "Raw 只含遥测与 SQL；Graph 额外包含完整 Semantic Graph 能力。"),
            ("合格 case", "Raw 和 Graph 均诊断正确、引用有效且执行可靠的配对 case。"),
            ("Case median", "同一模型与 case 的重复差值先取中位数。"),
            ("Δ", "Graph − Raw；负数表示 Graph 使用的资源更少。"),
            ("Holm p", "对同一检验族做多重比较校正后的 p 值。"),
        )
        attribution_title = "数据来源与致谢"
        conclusion = "结论"
        conclusion_text = (
            "Semantic Graph 在多数聚焦任务中明显压缩检索数据，依赖导航场景最稳定；"
            "但本轮没有证明它能普遍改善端到端 RCA 效率或成本。模型和故障机制决定"
            "语义能力能否转化为更少的 rows、calls、input、output 或成本。"
        )
        sections = {
            "cards": "模型结果",
            "scores": "模型能力评分",
            "dimension_scores": "各维度独立排行",
            "model_metrics": "端到端诊断与效率",
            "score_note": (
                f"描述性评分按 {capability_rubric_label()} 分配定位、根因与证据。"
                f"Overall 是 {transfer_runs_per_model} 个端到端 run 的平均分；"
                f"{'、'.join(str(name) for name in execution['treatments'])} "
                f"各 {transfer_runs_per_treatment} 个 run，因此等于各臂的等权平均。"
                "失败 run 不从分母中删除。确定性证据审计只对 SQL 证据可判定，"
                "因此不计入该评分，另行报告。"
                "该评分不是预注册主要指标，也不参与显著性检验。"
            ),
            "catalog": "端到端 Case 特征",
            "catalog_note": (
                "Source oracle 在模型运行前冻结。Normal/abnormal samples "
                "是 oracle 直接使用的观测数。"
            ),
            "case_outcomes": "逐 Case 汇总",
            "case_outcome_note": (
                f"诊断列汇总全部模型、每模型 {repetitions} 次重复。改善模型数不做跨模型推断。"
            ),
            "mechanisms": "故障机制决定收益方向",
            "mechanism_note": (
                "每个单元格是同一模型、同一机制内的 case-median Graph − Raw。负数表示 Graph 更省。"
            ),
            "cases": "逐 case 端到端结果",
            "case_note": (
                "先在每个模型和 case 内对合格重复取中位数。"
                f"Raw/Graph 实际成本汇总 {repetitions} 次重复；任一 run 不可计价则显示 n/a。"
                "机制和目标只在发布报告中显示，不提供给 agent。"
            ),
            "micro": "聚焦检索 Micro-benchmark",
            "benchmark_map": "三个 Benchmark 分别测什么",
            "tokens": "Token 使用明细",
            "token_note": (
                "Input 为 provider-visible input；reasoning 是 output 的子集，不能再次相加。"
            ),
            "execution": "执行与可靠性",
            "eligibility": "端到端入选审计",
            "evidence": "证据充分性审计",
            "evidence_note": (
                "Deterministic verifier 作为独立审计发布，不决定 headline eligibility。"
            ),
            "audit": "数据与协议审计",
            "cost": "Provider 成本",
            "cost_note": (
                "实际成本包含所有执行过的 run，包括失败运行。只对同币种的完整估算求小计；"
                "不转换币种，也不把不可估算模型计入小计。"
            ),
            "limits": "适用边界",
        }
        navigation = (
            ("overview", "这是什么"),
            ("summary", "结论"),
            ("mechanisms", "故障机制"),
            ("models", "模型"),
            ("benchmarks", "Benchmark"),
            ("cases", "Cases"),
            ("resources", "成本与可靠性"),
            ("method", "方法与边界"),
        )
        limit_items = (
            (
                f"Micro-benchmark 使用固定的 {execution['micro_cases']}-case reference cohort；"
                f"端到端测试使用 source-ranked 的 {transfer_cases}-case cohort。"
            ),
            (
                "模型内、case 级估计只适用于本次覆盖的系统和故障机制，不能外推为"
                "所有可观测性工作负载的普遍效应。"
            ),
            "Null 表示无法估计，0 表示未观察到减少；统计不显著不等于两种 treatment 等效。",
            "各 provider 的 reasoning 配置已冻结，但不代表相同的推理算力。",
            "Deterministic evidence sufficiency 作为次级审计报告，不决定主要效率指标的入选集合。",
            (
                f"{capability_rubric_label()} 模型能力分是测量后定义的描述性指标，不是预注册终点；"
                "其中证据分仍受 verifier 覆盖能力限制。"
            ),
            (
                f"机制样本不均衡：{_mechanism_cohort_phrase(mechanism_counts, 'zh')}。"
                "机制级结论只作描述。"
            ),
            "成本保留 provider 原始币种，不进行汇率换算。",
        )
    else:
        eyebrow = "GreptimeDB Semantic Graph · paired agent benchmark"
        lede = (
            "An open evaluation of LLM agents investigating real incidents with raw telemetry "
            "or the complete Semantic Graph."
        )
        actions = (
            f'<a href="{report_json}" download>Download report JSON</a>'
            '<a href="https://github.com/GreptimeTeam/semantic-rca-bench'
            '#reproduce-the-published-report">Reproduce the report</a>'
            '<a href="https://github.com/GreptimeTeam/semantic-rca-bench">View source</a>'
        )
        labels = {
            "cells": "completed runs",
            "models": "models",
            "cases": "incidents",
            "result": "primary result",
        }
        overview = "What this is"
        overview_text = (
            "The same LLM agent investigates the same incident under "
            f"{len(execution['treatments'])} interfaces. Split exposes only the native query "
            "APIs of Prometheus, Loki and Tempo. Raw exposes GreptimeDB telemetry tables and "
            "read-only SQL. Graph adds table semantics, entities, relationships, and query "
            "tools over the same data. The benchmark asks whether the all-in-one interface "
            "and the semantic layer each reduce investigation work when the diagnosis is "
            "correct."
        )
        reproducibility_text = (
            f"The repository publishes sanitized records for all {execution['completed_cells']} "
            "runs, including tool inputs, SQL, result projections, scoring facts, and hashes. "
            "The tagged code regenerates every aggregate and this page without calling a model "
            "provider."
        )
        glossary = (
            ("Run", "One model × incident × treatment × repetition."),
            ("Raw / Graph", "Raw provides telemetry and SQL; Graph adds the full Semantic Graph."),
            (
                "Eligible case",
                "A paired case where Raw and Graph are correct, cited, and reliable.",
            ),
            ("Case median", "The median paired-run difference within one model and case."),
            ("Δ", "Graph − Raw; a negative value means Graph used fewer resources."),
            ("Holm p", "A p value adjusted for multiple comparisons in the same test family."),
        )
        attribution_title = "Datasets and acknowledgements"
        conclusion = "Conclusion"
        conclusion_text = (
            "Semantic Graph compresses retrieval in most focused tasks, with the most stable "
            "effect in dependency navigation. This measurement does not show a general "
            "end-to-end RCA efficiency or cost improvement. The model and fault mechanism "
            "determine whether semantic capabilities reduce rows, calls, input, output, or cost."
        )
        sections = {
            "cards": "Model results",
            "scores": "Model capability score",
            "dimension_scores": "Independent rankings by dimension",
            "model_metrics": "End-to-end diagnosis and efficiency",
            "score_note": (
                f"The descriptive score splits {capability_rubric_label()} across location, "
                "root cause and evidence. Overall is the mean across every end-to-end run "
                f"for the model; {', '.join(str(name) for name in execution['treatments'])} "
                f"each contribute {transfer_runs_per_treatment} runs, so Overall is their "
                "equally weighted mean and failed runs remain in the denominator. The "
                "deterministic evidence audit is decidable only for SQL evidence, so it is "
                "reported separately rather than scored. It is not a pre-registered "
                "endpoint and is not used for hypothesis testing."
            ),
            "catalog": "End-to-end case characteristics",
            "catalog_note": (
                "The source oracle was frozen before model execution. Normal and anomalous "
                "samples are the observations used by that oracle."
            ),
            "case_outcomes": "Case-level summary",
            "case_outcome_note": (
                f"Diagnosis counts aggregate every model with {repetitions} repetitions each. "
                "Improved-model counts are descriptive and are not pooled inference."
            ),
            "mechanisms": "Fault mechanism changes the effect",
            "mechanism_note": (
                "Each cell is a case-median Graph − Raw effect within one model and mechanism. "
                "Negative values favor Graph."
            ),
            "cases": "End-to-end case effects",
            "case_note": (
                "Eligible repetitions are reduced to a median within each model and case. "
                f"Raw and Graph actual cost sum all {repetitions} repetitions and become n/a if "
                "either run is not priceable. "
                "Mechanisms and targets are published here but were hidden from the agent."
            ),
            "micro": "Focused retrieval micro-benchmarks",
            "benchmark_map": "What each benchmark measures",
            "tokens": "Token usage",
            "token_note": (
                "Input is provider-visible input. Reasoning is a subset of output and must "
                "not be added again."
            ),
            "execution": "Execution and reliability",
            "eligibility": "End-to-end eligibility audit",
            "evidence": "Evidence-sufficiency audit",
            "evidence_note": (
                "The deterministic verifier is a separate audit and does not control "
                "headline eligibility."
            ),
            "audit": "Data and protocol audit",
            "cost": "Provider cost",
            "cost_note": (
                "Actual cost includes every executed run, including failures. Only complete "
                "estimates in the same currency are subtotaled. Currencies are not converted, "
                "and unavailable models are excluded from subtotals."
            ),
            "limits": "Limits",
        }
        navigation = (
            ("overview", "What this is"),
            ("summary", "Conclusion"),
            ("mechanisms", "Mechanisms"),
            ("models", "Models"),
            ("benchmarks", "Benchmarks"),
            ("cases", "Cases"),
            ("resources", "Cost and reliability"),
            ("method", "Methods and limits"),
        )
        limit_items = report["limitations"]
    limits = "".join(f"<li>{_escape(item)}</li>" for item in limit_items)
    glossary_html = "".join(
        f"<dt>{_escape(term)}</dt><dd>{_escape(definition)}</dd>" for term, definition in glossary
    )
    diagnosis_raw = 0
    diagnosis_graph = 0
    for model in report["model_order"]:
        diagnosis = _mapping(
            _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer"),
            "diagnosis_correct",
        )
        diagnosis_raw += int(diagnosis.get("raw", 0))
        diagnosis_graph += int(diagnosis.get("semantic_graph", 0))
    runs_per_treatment = int(execution["models"]) * transfer_runs_per_treatment
    diagnosis_total = (
        f"{model_count} 个模型合计：Raw {diagnosis_raw}/{runs_per_treatment}，"
        f"Graph {diagnosis_graph}/{runs_per_treatment}。"
        if language == "zh"
        else f"Across all {model_count} models: Raw {diagnosis_raw}/{runs_per_treatment}; "
        f"Graph {diagnosis_graph}/{runs_per_treatment}."
    )
    primary_result = (
        "聚焦检索更省，端到端因场景而异"
        if language == "zh"
        else "Focused retrieval improves; E2E varies"
    )
    capability_details = _details(
        (
            f"查看 {capability_rubric_label()} 评分细则和精确分数"
            if language == "zh"
            else f"View the {capability_rubric_label()} rubric and exact scores"
        ),
        f"{_capability_rubric_table(report, language)}{_capability_table(report, language)}",
    )
    benchmark_details = _details(
        "展开 benchmark 定义和 micro 结果"
        if language == "zh"
        else "Open benchmark definitions and micro results",
        f"<h3>{sections['benchmark_map']}</h3>{_benchmark_map(language)}"
        f"<h3>{sections['micro']}</h3>{_micro_table(report, language)}",
        element_id=f"{language}-benchmarks",
    )
    case_details = _details(
        f"展开 {transfer_cases} 个 case 和 {case_model_pairs} 个 case-model 组合的完整明细"
        if language == "zh"
        else f"Open all {transfer_cases} cases and {case_model_pairs} case-model combinations",
        f'<h3>{sections["catalog"]}</h3><p class="small">{sections["catalog_note"]}</p>'
        f"{_case_catalog_table(report, language)}"
        f"<h3>{sections['case_outcomes']}</h3>"
        f'<p class="small">{sections["case_outcome_note"]}</p>'
        f"{_case_outcome_table(report, language)}"
        f'<h3>{sections["cases"]}</h3><p class="small">{sections["case_note"]}</p>'
        f"{_transfer_table(report, language)}",
        element_id=f"{language}-cases",
    )
    resource_details = _details(
        "展开 token、可靠性和成本审计"
        if language == "zh"
        else "Open token, reliability, and cost audits",
        f'<h3>{sections["tokens"]}</h3><p class="small">{sections["token_note"]}</p>'
        f"{_usage_table(report, language)}"
        f"<h3>{sections['execution']}</h3>{_reliability_table(report, language)}"
        f'<h3>{sections["cost"]}</h3><p class="small">{sections["cost_note"]}</p>'
        f"{_treatment_cost_table(report, language)}{_cost_totals(report, language)}"
        f"{_pricing_table(report, language)}{_cost_table(report, language)}",
        element_id=f"{language}-resources",
    )
    edge_equality_definition = _definition(
        "Raw/Graph exact edge equality",
        audit.get("transfer_raw_graph_exact_edge_equality"),
    )
    method_details = _details(
        "展开评分、证据、数据协议和适用边界"
        if language == "zh"
        else "Open scoring, evidence, protocol, and limitation audits",
        f"<h3>{sections['eligibility']}</h3>{_eligibility_table(report, language)}"
        f'<h3>{sections["evidence"]}</h3><p class="small">{sections["evidence_note"]}</p>'
        f"{_evidence_quality_table(report, language)}"
        f"<h3>{sections['audit']}</h3><dl>"
        f"{_definition('Micro no-model gates', audit.get('micro_no_model_gates_passed'))}"
        f"{_definition('Transfer no-model gates', audit.get('transfer_no_model_gates_passed'))}"
        f"{edge_equality_definition}"
        f"{_definition('Budget exhaustions', execution.get('budget_exhaustions'))}</dl>"
        f"<h3>{sections['limits']}</h3><ul>{limits}</ul>",
        element_id=f"{language}-method",
    )
    mechanism_details = _details(
        (
            f"查看全部 {mechanism_cells} 个模型-机制组合"
            if language == "zh"
            else f"View all {mechanism_cells} model-mechanism combinations"
        ),
        f'<p class="small">{sections["mechanism_note"]}</p>{_mechanism_table(report, language)}',
    )
    attribution = (
        f'<aside class="attribution"><h3>{attribution_title}</h3><p>{attribution_text}</p>'
        f'<p class="small">{attribution_terms} {attribution_links}</p></aside>'
    )
    nav = "".join(f'<a href="#{language}-{target}">{label}</a>' for target, label in navigation)
    return f"""
<header><p class="eyebrow">{eyebrow}</p>
<h1>Semantic RCA Bench</h1><p class="lede">{lede}</p>
<div class="report-actions">{actions}</div><div class="stat-grid">
{_stat(execution["completed_cells"], labels["cells"])}{_stat(execution["models"], labels["models"])}
{_stat(execution["micro_cases"] + execution["transfer_cases"], labels["cases"])}
{_stat(primary_result, labels["result"])}</div></header>
<nav class="report-nav" aria-label="Report sections">{nav}</nav>
<main><section class="overview" id="{language}-overview"><h2>{overview}</h2>
<p class="overview-copy">{overview_text}</p><p class="trust-copy">{reproducibility_text}</p>
<dl class="glossary">{glossary_html}</dl>{attribution}</section>
<section id="{language}-summary"><h2>{conclusion}</h2>
<p class="section-lede">{conclusion_text}</p>
{_finding_grid(report, language)}</section>
<section id="{language}-mechanisms"><h2>{sections["mechanisms"]}</h2>
<p class="mechanism-lede">{_mechanism_summary(report, language)}</p>
{_mechanism_direction_grid(report, language)}
{mechanism_details}</section>
<section id="{language}-models"><h2>{sections["cards"]}</h2>
<h3>{sections["scores"]}</h3><p class="score-intro">{sections["score_note"]}</p>
{_capability_leaderboard(report, language)}{capability_details}
<h3 class="dimension-title">{sections["dimension_scores"]}</h3>
{_capability_dimension_charts(report, language)}
<h3 class="model-metrics-title">{sections["model_metrics"]}</h3>
<p class="diagnosis-total">{diagnosis_total}</p><div class="model-grid">{cards}</div></section>
{benchmark_details}{case_details}{resource_details}{method_details}</main>"""


def _join(items: list[str], language: str) -> str:
    if language == "zh":
        return "、".join(items)
    if len(items) < 3:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _dataset(adapter: object) -> dict[str, str]:
    attribution = DATASET_ATTRIBUTION.get(str(adapter))
    if attribution is None:
        raise ValueError(f"no publishable attribution for dataset: {adapter}")
    return attribution


def _attribution(report: Mapping[str, object], language: str) -> tuple[str, str, str]:
    provenance = _mapping(report, "cohort_provenance")
    micro = _mapping_list(provenance, "micro")
    transfer = _mapping_list(provenance, "transfer")
    micro_parts = []
    for entry in micro:
        benchmark = MICRO_BENCHMARK_LABELS.get(str(entry["benchmark"]))
        if benchmark is None:
            raise ValueError(f"no publishable label for benchmark: {entry['benchmark']}")
        label = _dataset(entry["dataset"])["label"]
        micro_parts.append(
            f"{entry['cases']} 个来自 {label} 的 {benchmark['zh']} case"
            if language == "zh"
            else f"{entry['cases']} {benchmark['en']} cases from {label}"
        )
    transfer_parts = [
        f"{entry['cases']} 个来自 {_dataset(entry['dataset'])['label']} 的 case"
        if language == "zh"
        else f"{entry['cases']} cases from {_dataset(entry['dataset'])['label']}"
        for entry in transfer
    ]
    datasets = list(dict.fromkeys(str(entry["dataset"]) for entry in (*micro, *transfer)))
    names = _join([_dataset(adapter)["label"] for adapter in datasets], language)
    if language == "zh":
        text = (
            f"Micro-benchmark 使用 {_join(micro_parts, language)}；"
            f"端到端 cohort 使用 {_join(transfer_parts, language)}。"
            f"感谢 {names} 的作者与维护者公开数据和研究材料，使本评测能够复现。"
        )
        terms = (
            "".join(_dataset(adapter)["zh"] for adapter in datasets)
            + "本项目不替上游解决 license 冲突，也不重新分发原始 telemetry。"
        )
    else:
        text = (
            f"The micro-benchmarks use {_join(micro_parts, language)}. The end-to-end cohort "
            f"uses {_join(transfer_parts, language)}. We thank the authors and maintainers of "
            f"{names} for publishing the datasets and research materials that make this "
            "evaluation reproducible."
        )
        terms = (
            " ".join(_dataset(adapter)["en"] for adapter in datasets)
            + " This project does not resolve upstream license conflicts and does not "
            "redistribute source telemetry."
        )
    links = " · ".join(
        f'<a href="{_dataset(adapter)["url"]}">{_escape(_dataset(adapter)["label"])}</a>'
        for adapter in datasets
    )
    return text, terms, links


def _causal_scope_phrase(report: Mapping[str, object], language: str) -> str:
    scopes = []
    for case in _mapping_list(report, "case_catalog"):
        label = CAUSAL_SCOPE_LABELS.get(str(case["causal_scope"]))
        if label is None:
            raise ValueError(f"no publishable label for causal scope: {case['causal_scope']}")
        if label[language] not in scopes:
            scopes.append(label[language])
    if language == "zh":
        return "、".join(scopes)
    return f"{', '.join(scopes[:-1])}, or {scopes[-1]}" if len(scopes) > 2 else " or ".join(scopes)


def _details(summary: str, body: str, *, element_id: str | None = None) -> str:
    identifier = f' id="{element_id}"' if element_id is not None else ""
    return (
        f'<details class="report-details"{identifier}><summary>{_escape(summary)}</summary>'
        f'<div class="details-body">{body}</div></details>'
    )


def _model_card(model, report, language):
    transfer = _mapping(report, "transfer")
    metrics = _mapping(transfer, "primary_metrics")
    rows = _mapping(metrics, "rows_returned")
    calls = _mapping(metrics, "correct_completion_tool_calls")
    descriptive = _mapping(transfer, "descriptive_metrics")
    input_tokens = _mapping(descriptive, "provider_visible_input_tokens")
    output_tokens = _mapping(descriptive, "output_tokens")
    cost = _mapping(descriptive, "estimated_cost")
    diagnosis = _mapping(transfer, "diagnosis_correct")
    # Derived from the treatments the run actually had. Dividing by two reported
    # 28 runs as 42 the moment a third arm existed.
    treatment_labels = {"raw": "Raw", "semantic_graph": "Graph", "split_pillars": "Split"}
    present = [key for key in treatment_labels if key in diagnosis]
    runs_per_treatment = int(transfer.get("runs", 0)) // max(len(present), 1)
    case_count = len(_mapping_list(transfer, "case_effects"))
    diagnosis_summary = " · ".join(
        f"{diagnosis.get(key, 0)}/{runs_per_treatment} {treatment_labels[key]}" for key in present
    )
    eligible_summary = f"{rows.get('eligible_cases')} / {case_count}"
    labels = (
        (
            "诊断正确",
            "合格 case",
            "Rows Graph − Raw",
            "Calls Graph − Raw",
            "Input Graph − Raw",
            "Output Graph − Raw",
            "合格 case 成本中位差",
            "Rows Holm p",
        )
        if language == "zh"
        else (
            "Correct diagnosis",
            "Eligible cases",
            "Rows Graph − Raw",
            "Calls Graph − Raw",
            "Input Graph − Raw",
            "Output Graph − Raw",
            "Eligible case-median cost",
            "Rows Holm p",
        )
    )
    return f"""<article class="model-card"><h3>{_escape(model)}</h3>
{_metric(labels[0], diagnosis_summary)}
{_metric(labels[1], eligible_summary)}
{_metric(labels[2], _delta(rows.get("case_median_delta")))}
{_metric(labels[3], _delta(calls.get("case_median_delta")))}
{_metric(labels[4], _delta(input_tokens.get("case_median_delta")))}
{_metric(labels[5], _delta(output_tokens.get("case_median_delta")))}
{_metric(labels[6], _cost_delta(cost))}
{_metric(labels[7], rows.get("holm_adjusted_p"))}</article>"""


def _capability_table(report, language):
    scores = _mapping(report, "capability_scores")
    models = _mapping(scores, "models")
    maxima = _capability_dimension_maxima()
    treatments = _capability_treatments(models)

    def by_treatment(model, key):
        return _mapping(_mapping(_mapping(models, model), "by_treatment"), key)

    rows = []
    for model in report["model_order"]:
        item = _mapping(models, model)
        overall = _mapping(item, "overall")
        dimensions = _mapping(overall, "average_dimension_points")
        rows.append(
            (
                model,
                overall.get("score"),
                *(by_treatment(model, key).get("score") for key in treatments),
                *(
                    f"{dimensions.get(key)} / {maxima[key]}"
                    for key in ("location", "root_cause", "evidence")
                ),
            )
        )
    treatment_names = {"raw": "Raw", "semantic_graph": "Graph", "split_pillars": "Split"}
    headers = (
        ("模型", "总分", *(treatment_names[key] for key in treatments), "定位", "根因", "证据")
        if language == "zh"
        else (
            "Model",
            "Overall",
            *(treatment_names[key] for key in treatments),
            "Location",
            "Root cause",
            "Evidence",
        )
    )
    return _html_table(headers, rows)


def _capability_leaderboard(report: Mapping[str, object], language: str) -> str:
    models = _mapping(_mapping(report, "capability_scores"), "models")
    ranked = sorted(
        report["model_order"],
        key=lambda model: float(_mapping(_mapping(models, model), "overall")["score"]),
        reverse=True,
    )
    labels = (
        {
            "location": "定位",
            "root_cause": "根因",
            "evidence": "严格证据",
            "raw": "Raw",
            "semantic_graph": "Graph",
            "split_pillars": "Split",
        }
        if language == "zh"
        else {
            "location": "Location",
            "root_cause": "Root cause",
            "evidence": "Strict evidence",
            "raw": "Raw",
            "semantic_graph": "Graph",
            "split_pillars": "Split",
        }
    )
    treatments = _capability_treatments(models)
    rows = []
    for rank, model in enumerate(ranked, 1):
        item = _mapping(models, model)
        scores = {
            "overall": _mapping(item, "overall").get("score"),
            **{
                key: _mapping(_mapping(item, "by_treatment"), key).get("score")
                for key in treatments
            },
        }
        dimensions = _mapping(_mapping(item, "overall"), "average_dimension_points")
        dimension_values = {
            "location": dimensions.get("location"),
            "root_cause": dimensions.get("root_cause"),
            "evidence": dimensions.get("evidence"),
        }
        if not all(isinstance(value, (int, float)) for value in dimension_values.values()):
            raise ValueError("capability dimension score is not numeric")
        maximum = sum(_capability_dimension_maxima().values())
        stack_label = ", ".join(
            f"{labels[key]} {_format_number(value)}" for key, value in dimension_values.items()
        )
        stack = "".join(
            f'<span class="score-segment score-{key}" '
            f'style="width: {100.0 * float(value) / maximum:g}%" '
            f'title="{labels[key]}: {_format_number(value)}"></span>'
            for key, value in dimension_values.items()
        )
        bars = []
        for key in treatments:
            value = scores[key]
            if not isinstance(value, (int, float)):
                raise ValueError("capability score is not numeric")
            width = min(max(100.0 * float(value) / maximum, 0.0), 100.0)
            display = _format_number(value)
            bars.append(
                f'<div class="score-bar-row"><span>{labels[key]}</span>'
                f'<div class="score-track" role="img" aria-label="{_escape(model)} '
                f'{labels[key]} {display} / {maximum}"><span class="score-fill score-{key}" '
                f'style="width: {width:g}%"></span></div><strong>{display}</strong></div>'
            )
        rows.append(
            f'<article class="score-entry"><div class="score-heading"><span class="rank">'
            f"#{rank}</span><h4>{_escape(model)}</h4><strong>{_format_number(scores['overall'])}"
            f'</strong></div><div class="score-stack" role="img" aria-label="{_escape(model)}: '
            f'{_escape(stack_label)}">{stack}</div>{"".join(bars)}</article>'
        )
    legend = "".join(
        f'<span><i class="score-{key}"></i>{labels[key]}</span>'
        for key in ("location", "root_cause", "evidence")
    )
    return (
        f'<div class="score-legend" aria-label="Score dimensions">{legend}</div>'
        f'<div class="score-leaderboard">{"".join(rows)}</div>'
    )


def capability_rubric_label() -> str:
    """The rubric's point split, e.g. `40/40/5`, derived rather than repeated."""
    maxima = _capability_dimension_maxima()
    return "/".join(str(maxima[key]) for key in ("location", "root_cause", "evidence"))


def _capability_treatments(models: Mapping[str, object]) -> tuple[str, ...]:
    """The treatments the run had, in report order."""
    order = ("split_pillars", "raw", "semantic_graph")
    present = {key for model in models for key in _mapping(_mapping(models, model), "by_treatment")}
    return tuple(key for key in order if key in present)


def _capability_dimension_maxima() -> dict[str, int]:
    maxima: dict[str, int] = {}
    for contract in CAPABILITY_SCORE_RUBRIC.values():
        maxima[str(contract["dimension"])] = maxima.get(str(contract["dimension"]), 0) + int(
            contract["points"]
        )
    return maxima


def _capability_dimension_charts(report: Mapping[str, object], language: str) -> str:
    models = _mapping(_mapping(report, "capability_scores"), "models")
    # Derived from the rubric and from the treatments the run actually had. A
    # repeated literal drifts the moment either changes.
    total = _capability_dimension_maxima()
    treatment_labels = {"raw": "Raw", "semantic_graph": "Graph", "split_pillars": "Split"}
    names = (
        {"overall": "总分", "location": "定位", "root_cause": "根因", "evidence": "严格证据"}
        if language == "zh"
        else {
            "overall": "Overall",
            "location": "Location",
            "root_cause": "Root cause",
            "evidence": "Strict evidence",
        }
    )
    treatments = tuple(
        key
        for key in treatment_labels
        if any(key in _mapping(_mapping(models, model), "by_treatment") for model in models)
    )
    dimensions = (
        ("overall", names["overall"], sum(total.values())),
        *((key, treatment_labels[key], sum(total.values())) for key in treatments),
        *((key, names[key], total[key]) for key in ("location", "root_cause", "evidence")),
    )
    charts = []
    for key, label, maximum in dimensions:
        if key == "overall":
            values = {
                str(model): _mapping(_mapping(models, model), "overall").get("score")
                for model in report["model_order"]
            }
        elif key in treatment_labels:
            values = {
                str(model): _mapping(_mapping(_mapping(models, model), "by_treatment"), key).get(
                    "score"
                )
                for model in report["model_order"]
            }
        else:
            values = {
                str(model): _mapping(
                    _mapping(_mapping(models, model), "overall"),
                    "average_dimension_points",
                ).get(key)
                for model in report["model_order"]
            }
        if not all(isinstance(value, (int, float)) for value in values.values()):
            raise ValueError("capability dimension score is not numeric")
        ranked = sorted(
            report["model_order"],
            key=lambda model: float(values[str(model)]),
            reverse=True,
        )
        rows = []
        for rank, model in enumerate(ranked, 1):
            value = float(values[str(model)])
            display = _format_number(value)
            width = min(max(value / maximum * 100, 0.0), 100.0)
            rows.append(
                f'<div class="dimension-row"><span class="dimension-rank">#{rank}</span>'
                f'<strong>{_escape(model)}</strong><div class="dimension-track" role="img" '
                f'aria-label="{_escape(model)} {label}: {display} / {maximum}"><span '
                f'class="dimension-fill score-{key}" style="width: {width:g}%"></span></div>'
                f"<b>{display}</b></div>"
            )
        charts.append(
            f'<article class="dimension-chart"><h4>{label} <span>/ {maximum}</span></h4>'
            f"{''.join(rows)}</article>"
        )
    return f'<div class="dimension-grid">{"".join(charts)}</div>'


def _capability_rubric_table(report, language):
    scopes = _causal_scope_phrase(report, language)
    # Derived from the rubric so the published points cannot state a split the
    # scorer does not use, and so a dimension that is scored nowhere is shown as
    # reported-only rather than as points a model can earn.
    text = {
        "causal_scope_match": (f"识别{scopes} scope", f"Identifies {scopes} scope"),
        "causal_locus_match": (f"命中正式声明的{scopes}", f"Matches the declared {scopes}"),
        "fault_category_match": ("命中故障大类", "Matches the fault class"),
        "mechanism_code_match": ("命中具体因果机制", "Matches the causal mechanism"),
        "has_execution_valid_citation": (
            "至少一条 citation 对应成功执行的查询",
            "At least one citation resolved to a successful query",
        ),
        "required_evidence_covered": (
            "引用结果通过冻结 evidence verifier；只对 SQL 证据可判定，故不计分",
            "Cited results pass the frozen evidence verifier; decidable only for "
            "SQL evidence, so reported rather than scored",
        ),
    }
    dimension_names = {
        "location": ("定位", "Location"),
        "root_cause": ("根因", "Root cause"),
        "evidence": ("证据", "Evidence"),
    }
    index = 0 if language == "zh" else 1
    not_scored = "不计分" if language == "zh" else "not scored"
    rows = [
        (
            dimension_names[str(contract["dimension"])][index],
            str(contract["label"]).capitalize(),
            int(contract["points"]),
            text[key][index],
        )
        for key, contract in CAPABILITY_SCORE_RUBRIC.items()
    ]
    rows.extend(
        (
            dimension_names["evidence"][index],
            key.replace("_", " ").capitalize(),
            not_scored,
            text[key][index],
        )
        for key in CAPABILITY_UNSCORED_DIMENSIONS
    )
    headers = (
        ("维度", "评分项", "分值", "判定")
        if language == "zh"
        else ("Dimension", "Item", "Points", "Rule")
    )
    return _html_table(headers, tuple(rows))


def _case_catalog_table(report, language):
    rows = []
    for case in _mapping_list(report, "case_catalog"):
        oracle = case.get("oracle")
        if isinstance(oracle, Mapping):
            operation = oracle.get("allowed_operations")
            operation_text = ", ".join(operation) if isinstance(operation, list) else ""
            signal = f"{oracle.get('source_table')}.{oracle.get('value_column')}"
            if operation_text:
                signal = f"{signal} · {operation_text}"
            threshold = _format_number(oracle.get("threshold"))
            samples = f"{oracle.get('normal_samples')} / {oracle.get('abnormal_samples')}"
        else:
            # No node metric expresses this mechanism as a threshold crossing, so
            # the secondary evidence audit reports not estimable for the case.
            signal = threshold = samples = "not estimable"
        rows.append(
            (
                case.get("case_id"),
                case.get("source_case"),
                case.get("system"),
                _mechanism_label(case.get("mechanism_code"), language),
                case.get("target"),
                signal,
                threshold,
                samples,
            )
        )
    headers = (
        (
            "Case",
            "Source case",
            "系统",
            "机制",
            "故障目标",
            "Oracle 信号",
            "阈值",
            "Normal / Abnormal samples",
        )
        if language == "zh"
        else (
            "Case",
            "Source case",
            "System",
            "Mechanism",
            "Fault target",
            "Oracle signal",
            "Threshold",
            "Normal / anomalous samples",
        )
    )
    return _html_table(headers, rows)


def _case_outcome_table(report, language):
    rows = []
    for case in _mapping_list(report, "case_outcomes"):
        diagnosis = _mapping(case, "diagnosis_correct")
        rows.append(
            (
                case.get("case_id"),
                _mechanism_label(case.get("mechanism_code"), language),
                f"{diagnosis.get('raw')} / {diagnosis.get('semantic_graph')}",
                case.get("eligible_models"),
                case.get("models_with_fewer_rows"),
                case.get("models_with_fewer_calls"),
                case.get("models_with_fewer_input_tokens"),
                case.get("models_with_fewer_output_tokens"),
                (
                    f"{case.get('models_with_lower_estimated_cost')} / "
                    f"{case.get('models_with_estimable_cost')}"
                ),
            )
        )
    headers = (
        (
            "Case",
            "机制",
            "诊断正确 Raw / Graph",
            "合格模型",
            "Rows 改善模型",
            "Calls 改善模型",
            "Input 改善模型",
            "Output 改善模型",
            "成本改善 / 可估算模型",
        )
        if language == "zh"
        else (
            "Case",
            "Mechanism",
            "Correct Raw / Graph",
            "Eligible models",
            "Models with fewer rows",
            "Models with fewer calls",
            "Models with less input",
            "Models with less output",
            "Lower cost / estimable models",
        )
    )
    return _html_table(headers, rows)


def _micro_row_reduction(report: Mapping[str, object], language: str) -> str:
    """Eligible micro cases where Graph returned fewer rows, per benchmark."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for model in report["model_order"]:
        micro = _mapping(_mapping(_mapping(report, "model_reports"), model), "micro")
        for benchmark, summary in _mapping(micro, "benchmarks").items():
            if not isinstance(summary, Mapping):
                raise ValueError("micro benchmark summary is not an object")
            effect = _mapping(
                _mapping(summary, "case_level_effect"), "rows_returned_through_evidence"
            )
            totals[benchmark][0] += int(effect["improvements"])
            totals[benchmark][1] += int(effect["eligible_cases"])
    parts = []
    for benchmark, (improved, eligible) in sorted(totals.items()):
        label = MICRO_BENCHMARK_LABELS.get(benchmark)
        if label is None:
            raise ValueError(f"no publishable label for benchmark: {benchmark}")
        parts.append(f"{label[language]} {improved}/{eligible}")
    return _join(parts, language)


def _mechanism_row_direction(report: Mapping[str, object]) -> tuple[int, int, int]:
    """Mechanisms that reduce rows, that increase rows, for every model, and the estimable total."""
    effects = _mechanism_row_effects(report)
    improved = 0
    regressed = 0
    estimable = 0
    for values in effects.values():
        known = [value for value in values.values() if value is not None]
        if not known:
            continue
        estimable += 1
        improved += all(value < 0 for value in known)
        regressed += all(value > 0 for value in known)
    return improved, regressed, estimable


def _finding_grid(report, language):
    reports = _mapping(report, "model_reports")
    significant = []
    comparable_cost_models = []
    lower_cost_models = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(reports, model), "transfer")
        for metric in _mapping(transfer, "primary_metrics").values():
            if (
                isinstance(metric, Mapping)
                and isinstance(metric.get("holm_adjusted_p"), (int, float))
                and float(metric["holm_adjusted_p"]) < 0.05
            ):
                significant.append((model, metric))
        actual_cost = _mapping(transfer, "actual_cost_by_treatment")
        raw_cost = actual_cost.get("raw")
        graph_cost = actual_cost.get("semantic_graph")
        if isinstance(raw_cost, (int, float)) and isinstance(graph_cost, (int, float)):
            comparable_cost_models.append(model)
            if graph_cost < raw_cost:
                lower_cost_models.append(model)
    micro_reduction = _micro_row_reduction(report, language)
    improved, regressed, estimable = _mechanism_row_direction(report)
    family_size = _mapping(_mapping(report, "scope"), "inference")["holm_family_size"]
    comparable = len(comparable_cost_models)
    if language == "zh":
        cost_text = (
            f"{comparable} 个可完整比较的模型中，没有模型降低端到端实际成本。"
            if not lower_cost_models
            else f"{comparable} 个可完整比较的模型中，"
            f"{_join(lower_cost_models, language)} 降低了端到端实际成本。"
        )
        findings = (
            (
                "聚焦检索",
                f"Graph 减少 rows 的合格 micro case：{micro_reduction}。",
            ),
            (
                "机制差异",
                f"{estimable} 个可估算机制中，{improved} 个在全部模型上减少 rows，"
                f"{regressed} 个在全部模型上增加 rows。",
            ),
            (
                "端到端结论",
                f"按预注册的 {family_size} 检验族做 Holm 多重比较校正后，"
                "没有主要指标达到统计显著。"
                "不能声称 Semantic Graph 普遍降低 RCA 的 rows 或 calls。",
            ),
            ("成本结果", f"{cost_text}Rows 压缩不能替代成本核算。"),
        )
    else:
        cost_text = (
            f"No model reduced actual end-to-end cost among {comparable} fully comparable models."
            if not lower_cost_models
            else f"{_join(lower_cost_models, language)} reduced actual end-to-end cost among "
            f"{comparable} fully comparable models."
        )
        findings = (
            (
                "Focused retrieval",
                f"Eligible micro cases where Graph returned fewer rows: {micro_reduction}.",
            ),
            (
                "Mechanism spread",
                f"Of {estimable} estimable mechanisms, {improved} reduce rows for every model "
                f"and {regressed} increase rows for every model.",
            ),
            (
                "End-to-end result",
                f"After Holm correction over the pre-registered family of {family_size} tests, "
                "no primary endpoint is statistically significant. The data does not support a "
                "general reduction in RCA rows or calls.",
            ),
            (
                "Cost result",
                f"{cost_text} Row compression is not a substitute for cost accounting.",
            ),
        )
    if significant:
        raise ValueError("formal report conclusion must be updated for a significant endpoint")
    return (
        '<div class="finding-grid">'
        + "".join(
            f'<article class="finding"><h3>{_escape(title)}</h3><p>{_escape(text)}</p></article>'
            for title, text in findings
        )
        + "</div>"
    )


def _benchmark_map(language):
    if language == "zh":
        rows = (
            (
                "Discovery micro-benchmark",
                "从未知 schema 中找到承载目标组件和异常信号的正确遥测表，并返回冻结窗口内的证据。",
                "隔离测量 schema discovery 与证据检索成本。",
                "Rows / calls through evidence",
            ),
            (
                "Graph micro-benchmark",
                "从已知异常信号出发，找到正确的服务依赖边，并用可执行证据确认。",
                "隔离测量依赖导航和关系检索成本。",
                "Rows / calls through evidence",
            ),
            (
                "End-to-end transfer",
                "从事故窗口和工具开始，完成组件定位、机制诊断和证据引用。",
                "测量完整 RCA；只有诊断正确且执行可靠的配对进入主要效率指标。",
                "Rows / complete-run calls",
            ),
        )
        headers = ("Benchmark", "Agent 任务", "测量目的", "核心指标")
    else:
        rows = (
            (
                "Discovery micro-benchmark",
                "Find the telemetry table that carries the target component and anomalous "
                "signal in an unfamiliar schema, then return evidence from the frozen window.",
                "Isolates schema discovery and evidence retrieval cost.",
                "Rows / calls through evidence",
            ),
            (
                "Graph micro-benchmark",
                "Start from a known anomalous signal, identify the correct service dependency, "
                "and confirm it with executable evidence.",
                "Isolates dependency navigation and relationship retrieval cost.",
                "Rows / calls through evidence",
            ),
            (
                "End-to-end transfer",
                "Start from the incident window and tools, then localize the component, diagnose "
                "the mechanism, and cite evidence.",
                "Measures complete RCA; headline efficiency requires a correct, reliable pair.",
                "Rows / complete-run calls",
            ),
        )
        headers = ("Benchmark", "Agent task", "Purpose", "Core metrics")
    return _html_table(headers, rows)


def _micro_table(report, language):
    rows = []
    for model in report["model_order"]:
        micro = _mapping(_mapping(_mapping(report, "model_reports"), model), "micro")
        for benchmark, summary in sorted(_mapping(micro, "benchmarks").items()):
            if not isinstance(summary, Mapping):
                raise ValueError("micro benchmark summary is not an object")
            success = _mapping(summary, "treatment_success")
            effects = _mapping(summary, "case_level_effect")
            row_effect = _mapping(effects, "rows_returned_through_evidence")
            call_effect = _mapping(effects, "tool_calls_through_evidence")
            resources = _mapping(summary, "resource_effects")
            input_effect = _mapping(_mapping(resources, "summary"), "provider_visible_input_tokens")
            output_effect = _mapping(_mapping(resources, "summary"), "output_tokens")
            cost_effect = _mapping(_mapping(resources, "summary"), "estimated_cost")
            rows.append(
                (
                    model,
                    benchmark,
                    success.get("raw"),
                    success.get("semantic_graph"),
                    _delta(row_effect.get("median_delta")),
                    _delta(call_effect.get("median_delta")),
                    _delta(input_effect.get("case_median_delta")),
                    _delta(output_effect.get("case_median_delta")),
                    _cost_delta(cost_effect),
                    row_effect.get("eligible_cases"),
                )
            )
    headers = (
        (
            "模型",
            "任务",
            "Raw 成功",
            "Graph 成功",
            "Rows Δ",
            "Calls Δ",
            "Input Δ",
            "Output Δ",
            "成本 Δ",
            "合格 case",
        )
        if language == "zh"
        else (
            "Model",
            "Task",
            "Raw success",
            "Graph success",
            "Rows Δ",
            "Calls Δ",
            "Input Δ",
            "Output Δ",
            "Cost Δ",
            "Eligible cases",
        )
    )
    return _html_table(headers, rows)


def _transfer_table(report, language):
    rows = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        for effect in _mapping_list(transfer, "case_effects"):
            rows.append(
                (
                    model,
                    effect.get("case_id"),
                    _mechanism_label(effect.get("mechanism_code"), language),
                    effect.get("target"),
                    effect.get("eligible_repetitions"),
                    _delta(effect.get("rows_returned")),
                    _delta(effect.get("correct_completion_tool_calls")),
                    _delta(effect.get("provider_visible_input_tokens")),
                    _delta(effect.get("output_tokens")),
                    _absolute_cost(
                        effect.get("actual_cost_raw"), effect.get("estimated_cost_currency")
                    ),
                    _absolute_cost(
                        effect.get("actual_cost_semantic_graph"),
                        effect.get("estimated_cost_currency"),
                    ),
                    _cost_delta_from_case(effect),
                )
            )
    headers = (
        (
            "模型",
            "Case",
            "机制",
            "故障目标",
            "合格重复",
            "Rows Δ",
            "Calls Δ",
            "Input Δ",
            "Output Δ",
            "Raw 实际成本",
            "Graph 实际成本",
            "成本 Δ",
        )
        if language == "zh"
        else (
            "Model",
            "Case",
            "Mechanism",
            "Fault target",
            "Eligible reps",
            "Rows Δ",
            "Calls Δ",
            "Input Δ",
            "Output Δ",
            "Raw actual cost",
            "Graph actual cost",
            "Cost Δ",
        )
    )
    return _html_table(headers, rows)


def _mechanism_row_effects(report: Mapping[str, object]) -> dict[str, dict[str, float | None]]:
    effects: dict[str, dict[str, float | None]] = defaultdict(dict)
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        for item in _mapping_list(transfer, "mechanism_effects"):
            rows = _mapping(_mapping(item, "metrics"), "rows_returned")
            value = rows.get("case_median_delta") if rows.get("eligible_cases", 0) else None
            effects[str(item["mechanism_code"])][str(model)] = (
                float(value) if isinstance(value, (int, float)) else None
            )
    return effects


def _mechanism_summary(report: Mapping[str, object], language: str) -> str:
    effects = _mechanism_row_effects(report)
    parts = []
    for mechanism, _ in _mechanism_cohort(_mapping_list(report, "case_catalog")):
        values = [value for value in effects.get(mechanism, {}).values() if value is not None]
        better = sum(value < 0 for value in values)
        parts.append(f"{_mechanism_label(mechanism, language)} {better}/{len(values)}")
    case_better = 0
    case_total = 0
    for case in _mapping_list(report, "case_outcomes"):
        case_better += int(case.get("models_with_fewer_rows", 0))
        case_total += int(case.get("eligible_models", 0))
    if language == "zh":
        return (
            f"按机制统计 rows 减少的模型数：{_join(parts, language)}。"
            f"逐 case 看，{case_better}/{case_total} 个可估算的模型-case 组合减少 rows。"
            "负数表示 Graph 返回更少数据。"
        )
    return (
        f"Models where Graph returned fewer rows, by mechanism: {_join(parts, language)}. "
        f"Rows fall in {case_better}/{case_total} estimable case-model combinations. "
        "Negative values mean Graph returned fewer rows."
    )


def _mechanism_direction_grid(report: Mapping[str, object], language: str) -> str:
    effects = _mechanism_row_effects(report)
    mechanisms = [
        mechanism for mechanism, _ in _mechanism_cohort(_mapping_list(report, "case_catalog"))
    ]
    headers = "".join(f"<th>{_escape(model)}</th>" for model in report["model_order"])
    rows = []
    for mechanism in mechanisms:
        cells = []
        for model in report["model_order"]:
            value = effects.get(mechanism, {}).get(str(model))
            if value is None:
                class_name = "effect-na"
                display = "n/a"
                meaning = "not estimable" if language == "en" else "不可估算"
            elif value < 0:
                class_name = "effect-better"
                display = f"↓ {_format_number(abs(value))}"
                meaning = "Graph returned fewer rows" if language == "en" else "Graph 返回更少 rows"
            elif value > 0:
                class_name = "effect-worse"
                display = f"↑ {_format_number(value)}"
                meaning = "Graph returned more rows" if language == "en" else "Graph 返回更多 rows"
            else:
                class_name = "effect-tied"
                display = "0"
                meaning = "no observed difference" if language == "en" else "未观察到差异"
            cells.append(
                f'<td class="effect-cell {class_name}" title="{_escape(meaning)}">'
                f"{_escape(display)}</td>"
            )
        rows.append(
            f"<tr><th>{_escape(_mechanism_label(mechanism, language))}</th>{''.join(cells)}</tr>"
        )
    mechanism_header = "机制" if language == "zh" else "Mechanism"
    return (
        '<div class="table-wrap mechanism-grid"><table><thead><tr>'
        f"<th>{mechanism_header}</th>{headers}</tr></thead><tbody>"
        f"{''.join(rows)}</tbody></table></div>"
    )


def _mechanism_table(report, language):
    rows = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        for effect in _mapping_list(transfer, "mechanism_effects"):
            metrics = _mapping(effect, "metrics")
            row_effect = _mapping(metrics, "rows_returned")
            call_effect = _mapping(metrics, "correct_completion_tool_calls")
            input_effect = _mapping(metrics, "provider_visible_input_tokens")
            output_effect = _mapping(metrics, "output_tokens")
            cost_effect = _mapping(metrics, "estimated_cost")
            rows.append(
                (
                    model,
                    _mechanism_label(effect.get("mechanism_code"), language),
                    effect.get("cohort_cases"),
                    effect.get("eligible_cases"),
                    _delta(row_effect.get("case_median_delta")),
                    _direction(row_effect),
                    _delta(call_effect.get("case_median_delta")),
                    _delta(input_effect.get("case_median_delta")),
                    _delta(output_effect.get("case_median_delta")),
                    _cost_delta(cost_effect),
                )
            )
    headers = (
        (
            "模型",
            "机制",
            "Cohort case",
            "合格 case",
            "Rows Δ",
            "改善/平/变差",
            "Calls Δ",
            "Input Δ",
            "Output Δ",
            "成本 Δ",
        )
        if language == "zh"
        else (
            "Model",
            "Mechanism",
            "Cohort cases",
            "Eligible cases",
            "Rows Δ",
            "Better/tied/worse",
            "Calls Δ",
            "Input Δ",
            "Output Δ",
            "Cost Δ",
        )
    )
    return _html_table(headers, rows)


def _eligibility_table(report, language):
    rows = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        eligibility = _mapping(transfer, "efficiency_eligibility")
        by_treatment = _mapping(eligibility, "by_treatment")
        disposition = _mapping(eligibility, "paired_disposition")
        rows.append(
            (
                model,
                by_treatment.get("raw"),
                by_treatment.get("semantic_graph"),
                disposition.get("both"),
                disposition.get("raw_only"),
                disposition.get("semantic_graph_only"),
                disposition.get("neither"),
            )
        )
    headers = (
        ("模型", "Raw 合格", "Graph 合格", "双臂", "仅 Raw", "仅 Graph", "均不合格")
        if language == "zh"
        else (
            "Model",
            "Raw eligible",
            "Graph eligible",
            "Both",
            "Raw only",
            "Graph only",
            "Neither",
        )
    )
    return _html_table(headers, rows)


def _evidence_quality_table(report, language):
    rows = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        quality = _mapping(transfer, "evidence_quality")
        coverage = _mapping(quality, "required_evidence_covered_by_treatment")
        disposition = _mapping(quality, "paired_disposition")
        rows.append(
            (
                model,
                coverage.get("raw"),
                coverage.get("semantic_graph"),
                disposition.get("both"),
                disposition.get("raw_only"),
                disposition.get("semantic_graph_only"),
                disposition.get("neither"),
            )
        )
    headers = (
        ("模型", "Raw 已证明", "Graph 已证明", "双臂", "仅 Raw", "仅 Graph", "均未证明")
        if language == "zh"
        else ("Model", "Raw proven", "Graph proven", "Both", "Raw only", "Graph only", "Neither")
    )
    return _html_table(headers, rows)


def _usage_table(report, language):
    rows = []
    for model in report["model_order"]:
        usage = _mapping(
            _mapping(_mapping(_mapping(report, "model_reports"), model), "usage"), "combined"
        )
        rows.append(
            (
                model,
                _format_tokens(usage.get("provider_visible_input_tokens")),
                _format_tokens(usage.get("uncached_input_tokens")),
                _format_tokens(usage.get("cache_read_input_tokens")),
                _format_tokens(usage.get("cache_creation_input_tokens")),
                _format_tokens(usage.get("output_tokens")),
                _format_tokens(usage.get("reasoning_output_tokens")),
                usage.get("input_breakdown_complete"),
            )
        )
    headers = (
        (
            "模型",
            "Input 总量",
            "Uncached input",
            "Cache read",
            "Cache write",
            "Output",
            "Reasoning（Output 子集）",
            "Input breakdown 完整",
        )
        if language == "zh"
        else (
            "Model",
            "Total input",
            "Uncached input",
            "Cache read",
            "Cache write",
            "Output",
            "Reasoning (output subset)",
            "Input breakdown complete",
        )
    )
    return _html_table(headers, rows)


def _reliability_table(report, language):
    rows = []
    for model in report["model_order"]:
        model_report = _mapping(_mapping(report, "model_reports"), model)
        reliability = _mapping(model_report, "reliability")
        transfer = _mapping(model_report, "transfer")
        diagnosis = _mapping(transfer, "diagnosis_correct")
        eligibility = _mapping(_mapping(transfer, "efficiency_eligibility"), "by_treatment")
        disposition = _mapping(_mapping(transfer, "efficiency_eligibility"), "paired_disposition")
        rows.append(
            (
                model,
                reliability.get("runs"),
                reliability.get("runner_errors"),
                reliability.get("failed_database_queries"),
                f"{diagnosis.get('raw')} / {diagnosis.get('semantic_graph')}",
                f"{eligibility.get('raw')} / {eligibility.get('semantic_graph')}",
                disposition.get("semantic_graph_only"),
            )
        )
    headers = (
        (
            "模型",
            "Runs",
            "Runner errors",
            "Transfer SQL 失败",
            "诊断正确 Raw / Graph",
            "合格 Raw / Graph",
            "仅 Graph 合格 pair",
        )
        if language == "zh"
        else (
            "Model",
            "Runs",
            "Runner errors",
            "Failed transfer SQL",
            "Correct Raw / Graph",
            "Eligible Raw / Graph",
            "Graph-only eligible pairs",
        )
    )
    return _html_table(headers, rows)


def _cost_table(report, language):
    costs = _mapping(report, "costs")
    rows = []
    for model in report["model_order"]:
        value = _mapping(_mapping(costs, "models"), model)
        estimate = value.get("estimated_cost")
        currency = value.get("currency")
        display = (
            f"{currency} {float(estimate):.4f}"
            if isinstance(estimate, (int, float)) and isinstance(currency, str)
            else "Not estimable"
            if language == "en"
            else "不可估算"
        )
        status = value.get("status")
        reason = value.get("unavailable_reason") or "—"
        if language == "zh":
            status = {"available": "可估算", "not_estimable": "不可估算"}.get(status, status)
            reason = {
                "model_price_not_frozen": "未冻结官方模型级价格。",
                "cache_breakdown_incomplete": (
                    "至少一个 run 缺少完整 cache breakdown，计价契约拒绝部分估算。"
                ),
                "usage_not_priceable": "Provider usage 无法按冻结计价契约估算。",
            }.get(value.get("unavailable_reason_code"), reason)
        rows.append((model, status, display, reason))
    headers = (
        ("模型", "状态", "估算", "不可估算原因")
        if language == "zh"
        else ("Model", "Status", "Estimate", "Reason when unavailable")
    )
    return _html_table(headers, rows)


def _treatment_cost_table(report, language):
    rows = []
    treatments: list[str] = []
    for model in report["model_order"]:
        model_report = _mapping(_mapping(report, "model_reports"), model)
        transfer = _mapping(model_report, "transfer")
        costs = _mapping(transfer, "actual_cost_by_treatment")
        currency = transfer.get("cost_currency")
        treatments = [key for key in ("split_pillars", "raw", "semantic_graph") if key in costs]
        per_treatment = [_absolute_cost(costs.get(key), currency) for key in treatments]
        delta = (
            _absolute_cost(
                float(costs["semantic_graph"]) - float(costs["raw"]), currency, signed=True
            )
            if isinstance(costs.get("raw"), (int, float))
            and isinstance(costs.get("semantic_graph"), (int, float))
            else "n/a"
        )
        combined = _mapping(
            _mapping(_mapping(model_report, "usage"), "combined"), "actual_cost_by_treatment"
        )
        rows.append(
            (
                model,
                *per_treatment,
                delta,
                _absolute_cost(combined.get("raw"), currency),
                _absolute_cost(combined.get("semantic_graph"), currency),
            )
        )
    names = {"raw": "Raw", "semantic_graph": "Graph", "split_pillars": "Split"}
    treatment_headers = tuple(names[key] for key in treatments)
    headers = (
        (
            "模型",
            *(f"端到端 {name}" for name in treatment_headers),
            "端到端 Graph − Raw",
            "全部 Raw",
            "全部 Graph",
        )
        if language == "zh"
        else (
            "Model",
            *(f"End-to-end {name}" for name in treatment_headers),
            "End-to-end Graph − Raw",
            "All Raw",
            "All Graph",
        )
    )
    return _html_table(headers, rows)


def _pricing_table(report, language):
    rows = []
    pricing = _mapping(_mapping(report, "costs"), "pricing_basis")
    for model in report["model_order"]:
        item = _mapping(pricing, model)
        currency = item.get("currency")
        rows.append(
            (
                model,
                _rate(item.get("uncached_input_per_million"), currency),
                _rate(item.get("cache_read_per_million"), currency),
                _rate(item.get("cache_write_per_million"), currency),
                _rate(item.get("output_per_million"), currency),
            )
        )
    headers = (
        (
            "模型",
            "Uncached input / 1M",
            "Cache read / 1M",
            "Cache write / 1M",
            "Output / 1M",
        )
        if language == "zh"
        else (
            "Model",
            "Uncached input / 1M",
            "Cache read / 1M",
            "Cache write / 1M",
            "Output / 1M",
        )
    )
    return _html_table(headers, rows)


def _cost_totals(report, language):
    totals = _mapping(_mapping(report, "costs"), "known_totals_by_currency")
    label = "完整估算小计" if language == "zh" else "complete subtotal"
    return "".join(
        '<p class="cost-total"><strong>'
        f"{_escape(currency)} {_escape(round(float(value), 4))}"
        f"</strong> {_escape(label)}</p>"
        for currency, value in sorted(totals.items())
    )


def _absolute_cost(value: object, currency: object, *, signed: bool = False) -> str:
    if not isinstance(value, (int, float)) or not isinstance(currency, str):
        return "n/a"
    formatted = f"{float(value):+.4f}" if signed else f"{float(value):.4f}"
    return f"{currency} {formatted}"


def _rate(value: object, currency: object) -> str:
    if not isinstance(value, (int, float)) or not isinstance(currency, str):
        return "n/a"
    return f"{currency} {float(value):g}"


def _html_table(headers, rows):
    head = "".join(f"<th>{_escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(item)}</td>" for item in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _definition(term, value):
    return f"<dt>{_escape(term)}</dt><dd>{_escape(value)}</dd>"


def _artifact_binding(artifact, path):
    return {
        "artifact_type": artifact.get("artifact_type"),
        "sha256": sha256_file(path),
        "semantic_payload_sha256": _mapping(artifact, "integrity").get("semantic_payload_sha256"),
    }


def _metric(label, value):
    return (
        f'<div class="metric"><span>{_escape(label)}</span><strong>{_escape(value)}</strong></div>'
    )


def _stat(value, label):
    return f'<div class="stat"><strong>{_escape(value)}</strong><span>{_escape(label)}</span></div>'


def _delta(value):
    return f"{float(value):+g}" if isinstance(value, (int, float)) else "n/a"


def _cost_delta(effect: Mapping[str, object]) -> str:
    value = effect.get("case_median_delta")
    currency = effect.get("currency")
    if not isinstance(value, (int, float)) or not isinstance(currency, str):
        return "n/a"
    return f"{currency} {float(value):+.4f}"


def _cost_delta_from_case(effect: Mapping[str, object]) -> str:
    value = effect.get("estimated_cost")
    currency = effect.get("estimated_cost_currency")
    if not isinstance(value, (int, float)) or not isinstance(currency, str):
        return "n/a"
    return f"{currency} {float(value):+.4f}"


def _direction(effect: Mapping[str, object]) -> str:
    return (
        f"{effect.get('negative_cases', 0)} / {effect.get('tied_cases', 0)} / "
        f"{effect.get('positive_cases', 0)}"
    )


def _mechanism_label(value: object, language: str) -> str:
    labels = {
        "workload_restart": ("Workload restart", "工作负载重启"),
        "call_path_delay": ("Call-path delay", "调用路径延迟"),
        "cpu_saturation": ("CPU saturation", "CPU 饱和"),
        "memory_pressure": ("Memory pressure", "内存压力"),
        "disk_io_degradation": ("Disk I/O degradation", "磁盘 I/O 退化"),
        "host_unavailable": ("Host unavailable", "主机不可用"),
    }
    english, chinese = labels.get(str(value), (str(value), str(value)))
    return chinese if language == "zh" else english


def _format_number(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value):g}"


def _format_tokens(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{int(value):,}"


def _escape(value):
    return html.escape(str(value), quote=True)


def _mapping(value: Mapping[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"formal report field is not an object: {key}")
    return dict(item)


def _mapping_list(value: Mapping[str, object], key: str) -> list[dict[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"formal report field is not an object list: {key}")
    return [dict(item) for item in items]


def _load_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value
