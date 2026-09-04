from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path

import sqlglot
from sqlglot import exp as sqlglot_exp

from semantic_rca_bench.formal_suite import canonical_sha256
from semantic_rca_bench.formal_suite_protocol import (
    load_formal_suite_protocol,
    load_transfer_cohort,
    sha256_file,
)
from semantic_rca_bench.formal_suite_release import validate_micro_measurement_artifact
from semantic_rca_bench.report import MODEL_PRICING
from semantic_rca_bench.transfer_release import validate_measurement_artifact

FORMAL_MEASUREMENT_REPORT_SCHEMA_VERSION = 7

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
    execution = _execution(suite, protocol, micro, transfer, micro_runs, transfer_runs)
    if execution["completed_cells"] != execution["expected_cells"]:
        raise ValueError("formal measurement artifacts are incomplete")
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
    cohort = load_transfer_cohort(suite, suite_protocol_path)
    case_context = {
        str(source["opaque_case_id"]): _case_context(
            source, cohort.adapter_for(str(source["opaque_case_id"]))
        )
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
    cohort_provenance = _cohort_provenance(suite, cohort)
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
        "diagnosis_by_dataset": _diagnosis_by(transfer_runs, case_context, "dataset"),
        "diagnosis_by_causal_scope": _diagnosis_by(transfer_runs, case_context, "causal_scope"),
        "tool_use_audit": _tool_use_audit(transfer_runs),
        "claim_rejection_audit": _claim_rejection_audit(transfer_runs),
        "citation_submission": _citation_submission(transfer_runs, names),
        "usage_by_treatment": _usage_by_treatment(transfer_runs),
        "capability_scores": _capability_scores(transfer_runs, names),
        "confirmatory_family_resource_effects": transfer_families,
        "semantic_layer_findings": _semantic_findings(model_reports),
        "resource_effect_contract": {
            "role": "descriptive; not a pre-specified endpoint",
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
            _power_limitation(execution, transfer_reports),
            "Provider reasoning settings are frozen configurations, not a common compute scale.",
            (
                "Deterministic evidence sufficiency is reported as a secondary audit and does "
                "not control headline efficiency eligibility."
            ),
            (
                f"The {capability_rubric_label()} capability score is a post-measurement "
                "descriptive index. It is "
                "not a pre-specified endpoint and its evidence component inherits verifier "
                "coverage limits."
            ),
            (
                "Mechanism cohorts are unevenly sized: "
                f"{_mechanism_cohort_phrase(_mechanism_cohort(case_context.values()), 'en')}. "
                "Mechanism-level summaries are descriptive and unevenly supported."
            ),
            (
                "Costs are estimates from frozen provider rates, not invoices, and are kept in "
                "the currency each provider billed in. A USD total is a derived figure, "
                "converted at the frozen rate published beside it; an exchange rate is a market "
                "quote, not a measurement."
            ),
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


def _case_context(source: Mapping[str, object], dataset: str) -> dict[str, object]:
    # None when the source offers no signal the deterministic oracle can express.
    raw_oracle = source.get("oracle")
    oracle = _mapping(source, "oracle") if raw_oracle is not None else None
    return {
        "case_id": source["opaque_case_id"],
        "source_case": source["source_case"],
        "dataset": dataset,
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
                for repetition, pair in repetitions.items():
                    missing = {baseline, treatment} - set(pair)
                    if missing:
                        raise ValueError(
                            f"resource pair for model {model}, cohort {cohort}, case {case}, "
                            f"repetition {repetition} is missing {sorted(missing)}"
                        )
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
            "post-measurement descriptive capability index; not a pre-specified endpoint "
            "and not used for hypothesis testing"
        ),
        "overall_formula": (
            f"sum of the {_capability_rubric_maximum()}-point rubric over every end-to-end "
            f"run for the model, divided by {_capability_rubric_maximum()} points per run "
            "and normalized to 100; every treatment contributes equally and failed runs "
            "remain in the denominator; deterministic evidence sufficiency is outside the "
            "rubric because the verifier cannot decide it for every query language"
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
    maximum = len(runs) * _capability_rubric_maximum()
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
        if not isinstance(score, (int, float)):
            continue
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
            for repetition, pair in pair_values.items():
                missing = {baseline, treatment} - set(pair)
                if missing:
                    raise ValueError(
                        f"case pair for model {model}, case {case_id}, repetition {repetition} "
                        f"is missing {sorted(missing)}"
                    )
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


def _diagnosis_by(
    runs: list[Mapping[str, object]],
    case_context: Mapping[str, Mapping[str, object]],
    field: str,
) -> dict[str, object]:
    """Correct diagnoses per treatment, grouped by one case attribute.

    Reported for the source dataset and for the causal scope. The cohort totals
    hide a direction reversal that both splits expose, and the two splits are
    published together because they are collinear here: every infrastructure-node
    case comes from one source, so neither split can attribute the reversal on its
    own.
    """
    treatments = _treatments_present(runs)
    group_of = {case_id: str(context[field]) for case_id, context in case_context.items()}
    cases: Counter[str] = Counter(group_of.values())
    correct: dict[str, Counter[str]] = {group: Counter() for group in cases}
    totals: dict[str, Counter[str]] = {group: Counter() for group in cases}
    for item in runs:
        group = group_of[str(item["case_id"])]
        treatment = str(item["visibility"])
        totals[group][treatment] += 1
        if _mapping(_mapping(item, "run"), "evaluation").get("diagnosis_correct") is True:
            correct[group][treatment] += 1
    return {
        group: {
            "cases": cases[group],
            "runs": {treatment: totals[group][treatment] for treatment in treatments},
            "diagnosis_correct": {treatment: correct[group][treatment] for treatment in treatments},
        }
        for group in sorted(cases)
    }


# A tool call is one model decision, so the audit counts calls and the runs that
# issued them. `\bJOIN\b` covers INNER, LEFT and comma-free ANSI joins alike.
_SQL_JOIN = re.compile(r"\bJOIN\b", re.IGNORECASE)
_PROMQL_EVALUATING_OPERATIONS = ("query", "query_range")

# The protocol fixes these two table names: the direct-log oracle reads `logs`
# and call-path delay evidence reads `traces`. Every other table in the replayed
# schema carries metrics. A join is cross-signal when its base tables span more
# than one of those kinds, which is the thing a single-store interface makes
# possible and the three-backend bundle cannot express at all.
_LOG_TABLE = "logs"
_TRACE_TABLE = "traces"


def _signal_kinds(query: str) -> set[str]:
    """Signal kinds among a statement's base tables, ignoring CTEs and subqueries.

    Counting alias names as tables inflates this badly: a subquery alias is not a
    second signal. Only names sqlglot resolves to real tables are considered.
    """
    try:
        statement = sqlglot.parse_one(query)
    except Exception:  # noqa: BLE001 - an unparseable agent query proves nothing here
        return set()
    if statement is None:
        return set()
    defined = {cte.alias_or_name.lower() for cte in statement.find_all(sqlglot_exp.CTE)}
    tables = {table.name.lower() for table in statement.find_all(sqlglot_exp.Table)} - defined
    return {
        "log" if name == _LOG_TABLE else "trace" if name == _TRACE_TABLE else "metric"
        for name in tables
    }


def _tool_use_audit(runs: list[Mapping[str, object]]) -> dict[str, object]:
    """Which of the offered interfaces the frozen agents actually exercised.

    Descriptive only. It records what the trajectories did, not whether a run was
    correct, and it never reclassifies a query by guessing what its tables hold.
    """
    treatments = _treatments_present(runs)
    calls: dict[str, Counter[str]] = {treatment: Counter() for treatment in treatments}
    successful_calls: dict[str, Counter[str]] = {treatment: Counter() for treatment in treatments}
    join_calls: Counter[str] = Counter()
    join_runs: dict[str, set[tuple[str, str, int]]] = defaultdict(set)
    cross_signal_calls: Counter[str] = Counter()
    cross_signal_runs: dict[str, set[tuple[str, str, int]]] = defaultdict(set)
    promql_runs: dict[str, set[tuple[str, str, int]]] = defaultdict(set)
    semantic_graph_runs: set[tuple[str, str, int]] = set()
    semantic_graph_calls = 0
    run_counts: Counter[str] = Counter()

    for item in runs:
        treatment = str(item["visibility"])
        key = (str(item["model"]), str(item["case_id"]), int(item["repetition"]))
        run_counts[treatment] += 1
        for call in _mapping_list(_mapping(item, "run"), "tool_calls"):
            name = str(call.get("tool_name"))
            failed = call.get("error") is True
            calls[treatment][name] += 1
            if failed:
                continue
            successful_calls[treatment][name] += 1
            arguments = call.get("input")
            arguments = arguments if isinstance(arguments, Mapping) else {}
            query = str(arguments.get("query", ""))
            if name == "execute_sql" and _SQL_JOIN.search(query):
                join_calls[treatment] += 1
                join_runs[treatment].add(key)
                if len(_signal_kinds(query)) > 1:
                    cross_signal_calls[treatment] += 1
                    cross_signal_runs[treatment].add(key)
            if (
                name == "query_metrics"
                and arguments.get("operation") in _PROMQL_EVALUATING_OPERATIONS
            ):
                promql_runs[treatment].add(key)
            if name == "query_semantic_graph":
                semantic_graph_runs.add(key)
                semantic_graph_calls += 1

    return {
        "role": (
            "descriptive audit of which offered interfaces the frozen agents used; "
            "not an endpoint and not part of eligibility"
        ),
        "by_treatment": {
            treatment: {
                "runs": run_counts[treatment],
                "tool_calls": dict(sorted(calls[treatment].items())),
                "successful_tool_calls": dict(sorted(successful_calls[treatment].items())),
                "successful_sql_join_calls": join_calls[treatment],
                "runs_with_successful_sql_join": len(join_runs[treatment]),
                "successful_cross_signal_join_calls": cross_signal_calls[treatment],
                "runs_with_successful_cross_signal_join": len(cross_signal_runs[treatment]),
                "runs_with_successful_promql_evaluation": len(promql_runs[treatment]),
            }
            for treatment in treatments
        },
        "semantic_graph_tool": {
            "runs": run_counts.get("semantic_graph", 0),
            "runs_with_successful_call": len(semantic_graph_runs),
            "successful_calls": semantic_graph_calls,
        },
        "promql_evaluation_contract": (
            "query_metrics operations "
            f"{' or '.join(_PROMQL_EVALUATING_OPERATIONS)} evaluate PromQL; metadata, "
            "series, labels and label_values operations do not and are excluded"
        ),
    }


def _power_limitation(
    execution: Mapping[str, object],
    transfer_reports: Mapping[str, object],
) -> str:
    """State the sample the tests actually ran on, not the cohort size.

    Eligibility keeps only pairs where both arms answered correctly, so an
    endpoint tests far fewer cases than the cohort holds. Reporting the cohort
    size alone would make the tests look better powered than they are, and a
    reader would then over-read a non-significant result.
    """
    sizes = [
        int(metric["eligible_cases"])
        for report in transfer_reports.values()
        for family in _mapping(report, "confirmatory_families").values()
        for metric in _mapping(family, "primary_metrics").values()
    ]
    if not sizes:
        raise ValueError("transfer artifact declares no endpoint eligible-case counts")
    return (
        f"The study has {execution['transfer_cases']} independent cases, and endpoint "
        f"eligibility leaves only {min(sizes)}-{max(sizes)} cases for an individual test. "
        "Exact sign tests on that many cases have low and discrete power, so a "
        "non-significant result indicates insufficient evidence and does not establish "
        "equivalence."
    )


def _usage_by_treatment(runs: list[Mapping[str, object]]) -> dict[str, object]:
    """Tokens per arm, and spend per arm kept in the currency it was billed in.

    Token totals cover every executed run, unsuccessful ones included, because
    that is the workload the arm actually generated. They are not the registered
    token endpoint, which is paired, eligibility-filtered and reduced to a case
    median inside one family.

    Spend is stricter. A model counts only when every one of its runs is priced
    in one currency: a model priced in one arm and not another would make the
    unpriced arm look cheap. Currencies are never added together, so a cohort
    spanning two of them yields two subtotals and no single figure.
    """
    treatments = _treatments_present(runs)
    tokens: Counter[str] = Counter()
    output: Counter[str] = Counter()
    per_model: dict[str, dict[str, object]] = defaultdict(
        lambda: {"spend": Counter(), "currencies": set(), "missing": False, "runs": 0}
    )
    for item in runs:
        treatment = str(item["visibility"])
        model = str(item["model"])
        usage = _mapping(_mapping(item, "run"), "usage")
        tokens[treatment] += int(usage.get("provider_visible_input_tokens") or 0)
        output[treatment] += int(usage.get("output_tokens") or 0)
        entry = per_model[model]
        entry["runs"] = int(entry["runs"]) + 1
        cost = usage.get("estimated_cost")
        currency = usage.get("cost_currency")
        if isinstance(cost, (int, float)) and isinstance(currency, str) and currency:
            spend = entry["spend"]
            assert isinstance(spend, Counter)
            spend[treatment] += float(cost)
            currencies = entry["currencies"]
            assert isinstance(currencies, set)
            currencies.add(currency)
        else:
            entry["missing"] = True

    priced: dict[str, str] = {}
    unpriced: list[str] = []
    for model, entry in per_model.items():
        currencies = entry["currencies"]
        assert isinstance(currencies, set)
        if entry["missing"] or len(currencies) != 1:
            unpriced.append(model)
        else:
            priced[model] = next(iter(currencies))

    spend_by_currency: dict[str, dict[str, float]] = defaultdict(
        lambda: dict.fromkeys(treatments, 0.0)
    )
    for model, currency in priced.items():
        spend = per_model[model]["spend"]
        assert isinstance(spend, Counter)
        for treatment in treatments:
            spend_by_currency[currency][treatment] += spend[treatment]
    currencies_present = sorted(spend_by_currency)
    return {
        "role": (
            "descriptive; token totals cover every executed run and are not the "
            "pre-specified token endpoint. Spend counts only models priced in one "
            "currency across every arm, and currencies are never combined"
        ),
        "by_treatment": {
            treatment: {
                "provider_visible_input_tokens": tokens[treatment],
                "output_tokens": output[treatment],
                "estimated_cost_by_currency": {
                    currency: round(spend_by_currency[currency][treatment], 6)
                    for currency in currencies_present
                },
            }
            for treatment in treatments
        },
        "priced_models": {model: priced[model] for model in sorted(priced)},
        "unpriced_models": sorted(unpriced),
        # Present only when the priced cohort shares one currency; a comparable
        # cross-model total does not exist otherwise.
        "comparable_currency": currencies_present[0] if len(currencies_present) == 1 else None,
        # Null when no model could be priced, because summing an empty cohort
        # gives 0.0 and that reads as "the runs were free" rather than "spend is
        # not estimable here".
        "estimated_cost_usd": {
            treatment: (
                None
                if not currencies_present
                else round(
                    sum(
                        _to_usd(spend_by_currency[currency][treatment], currency)
                        for currency in currencies_present
                    ),
                    6,
                )
            )
            for treatment in treatments
        },
        "exchange_rates": {
            currency: dict(EXCHANGE_RATES_TO_USD[currency])
            for currency in currencies_present
            if currency in EXCHANGE_RATES_TO_USD
        },
    }


# Frozen so the same artifact converts to the same figure on any day. A rate is
# a market quote, not a measurement: spend stays recorded in the currency it was
# billed in, and this only exists so one arm's total can be compared with
# another's when the cohort spans two currencies.
EXCHANGE_RATES_TO_USD = {
    "USD": {"per_unit_usd": 1.0, "checked_at": None, "source": None},
    "CNY": {
        "per_unit_usd": 1.0 / 6.7179,
        "units_per_usd": 6.7179,
        "checked_at": "2026-09-03",
        "source": "https://tradingeconomics.com/china/currency",
    },
}


def _to_usd(amount: float, currency: str) -> float:
    rate = EXCHANGE_RATES_TO_USD.get(currency)
    if rate is None:
        raise ValueError(f"no frozen exchange rate for currency: {currency}")
    return amount * float(rate["per_unit_usd"])


def _citation_submission(
    runs: list[Mapping[str, object]],
    models: list[str],
) -> dict[str, object]:
    """How often each model submitted no citation at all.

    Eligibility needs at least one execution-valid citation, so a model that
    ends a run without citing anything drops out of the paired sample even when
    its diagnosis was right. Published per model because a small eligible sample
    then reflects that habit rather than chance.
    """
    submitted: Counter[str] = Counter()
    empty: Counter[str] = Counter()
    empty_but_correct: Counter[str] = Counter()
    for item in runs:
        model = str(item["model"])
        evaluation = _mapping(_mapping(item, "run"), "evaluation")
        submitted[model] += 1
        if int(evaluation.get("cited_evidence_count") or 0) == 0:
            empty[model] += 1
            if evaluation.get("diagnosis_correct") is True:
                empty_but_correct[model] += 1
    return {
        "role": "descriptive; explains eligible-sample size, not model accuracy",
        "by_model": {
            model: {
                "runs": submitted[model],
                "runs_without_citation": empty[model],
                "correct_diagnoses_lost_to_missing_citation": empty_but_correct[model],
            }
            for model in models
        },
    }


def _claim_rejection_audit(runs: list[Mapping[str, object]]) -> dict[str, object]:
    """Rejection codes split by whether the citation claimed the mechanism.

    The verifier runs its mechanism check over every citation, including ones
    submitted as exclusion or propagated-impact evidence that never claimed to
    prove the mechanism. Those produce codes too. Counting both together makes a
    run look like it failed far more checks than it attempted, so the two are
    reported apart: `claiming` is the count that describes a model's attempt,
    `not_claiming` is the count that only describes verifier coverage.
    """
    claiming: Counter[str] = Counter()
    not_claiming: Counter[str] = Counter()
    by_treatment: dict[str, Counter[str]] = defaultdict(Counter)
    for item in runs:
        treatment = str(item["visibility"])
        # A run may carry no citations at all; that is an absence of codes.
        citations = _mapping(item, "run").get("citations")
        for citation in citations if isinstance(citations, list) else ():
            if not isinstance(citation, Mapping):
                continue
            claim_types = citation.get("claim_types")
            claims_mechanism = isinstance(claim_types, list) and "fault_mechanism" in claim_types
            codes: set[str] = set()
            verdicts = citation.get("mechanism_verdicts")
            for verdict in verdicts if isinstance(verdicts, list) else ():
                if not isinstance(verdict, Mapping):
                    continue
                for key in ("anomaly_rejection_codes", "baseline_rejection_codes"):
                    value = verdict.get(key)
                    if isinstance(value, list):
                        codes.update(str(code) for code in value)
            for code in codes:
                if claims_mechanism:
                    claiming[code] += 1
                    by_treatment[treatment][code] += 1
                else:
                    not_claiming[code] += 1
    return {
        "role": (
            "descriptive; counts each citation once per distinct code. Only "
            "`claiming` describes an attempt to prove the mechanism"
        ),
        "counting_unit": "citation",
        "by_code": {
            code: {
                "claiming": claiming[code],
                "not_claiming": not_claiming[code],
            }
            for code in sorted(set(claiming) | set(not_claiming))
        },
        "claiming_by_treatment": {
            treatment: dict(sorted(codes.items()))
            for treatment, codes in sorted(by_treatment.items())
        },
    }


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
        # Only when every model could be priced. A total that silently omits a
        # model reads as the cost of the whole measurement and is not.
        "cross_currency_total": None
        if unavailable
        else {
            "currency": "USD",
            "amount": round(
                sum(_to_usd(amount, currency) for currency, amount in totals.items()), 6
            ),
            "exchange_rates": {
                currency: dict(EXCHANGE_RATES_TO_USD[currency]) for currency in sorted(totals)
            },
        },
        "aggregation_policy": (
            "Only complete estimates are counted, and a partially priced model is rejected "
            "whole. Spend is kept in the currency it was billed in; a USD total is derived "
            "from those subtotals at the frozen rates published beside it, and only when "
            "every model is priced."
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


def capability_rubric_label() -> str:
    """The rubric's point split, e.g. `40/40/5`, derived rather than repeated."""
    maxima = _capability_dimension_maxima()
    return "/".join(str(maxima[key]) for key in ("location", "root_cause", "evidence"))


def _capability_rubric_maximum() -> int:
    return sum(_capability_dimension_maxima().values())


def _capability_dimension_maxima() -> dict[str, int]:
    maxima: dict[str, int] = {}
    for contract in CAPABILITY_SCORE_RUBRIC.values():
        maxima[str(contract["dimension"])] = maxima.get(str(contract["dimension"]), 0) + int(
            contract["points"]
        )
    return maxima


def _artifact_binding(artifact, path):
    return {
        "artifact_type": artifact.get("artifact_type"),
        "sha256": sha256_file(path),
        "semantic_payload_sha256": _mapping(artifact, "integrity").get("semantic_payload_sha256"),
    }


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
