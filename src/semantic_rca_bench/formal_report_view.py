"""View model for the published measurement report.

The combined report JSON stays the authoritative measurement record. This module
derives everything the page needs on top of it: the sentences whose wording
depends on the numbers, and the geometry of each chart. The browser renders those
values; it never decides what the measurement says.

Static interface copy - section titles, table headers, legends, the glossary -
lives in `assets/report/i18n.json` instead, because it does not depend on any
measured value and must not be regenerated to change a label.
"""

from __future__ import annotations

import html
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path

from semantic_rca_bench.formal_report import (
    CAUSAL_SCOPE_LABELS,
    DATASET_ATTRIBUTION,
    MICRO_BENCHMARK_LABELS,
    capability_rubric_label,
    validate_formal_measurement_report,
)
from semantic_rca_bench.formal_report import _capability_rubric_maximum as rubric_maximum
from semantic_rca_bench.formal_report import _mapping as mapping
from semantic_rca_bench.formal_report import _mapping_list as mapping_list
from semantic_rca_bench.formal_report import _mechanism_cohort as mechanism_cohort
from semantic_rca_bench.formal_report import _mechanism_label as mechanism_label

REPORT_VIEW_SCHEMA_VERSION = 1

LANGUAGES = ("en", "zh")

TREATMENT_LABELS = {"raw": "Raw", "semantic_graph": "Graph", "split_pillars": "Split"}

# Registered endpoints only. A metric that is not comparable in a family - rows
# returned across the split stack - is absent here rather than plotted as zero.
FAMILY_COMPARISONS = {
    "semantic_layer": ("semantic_graph", "raw"),
    "storage_shape": ("raw", "split_pillars"),
}

# The conventional two-sided level. It is not tuned to this cohort and no other
# threshold is applied anywhere in the page: an endpoint is either significant
# after the registered Holm correction or it is not. Earlier drafts added a
# post-hoc "directional" tier on top of this; it was removed because its cutoffs
# were chosen after seeing how the endpoints landed, which is exactly the kind of
# reading the registration is meant to prevent.
SIGNIFICANCE_ALPHA = 0.05

# A strip's axis is symmetric log so that a token delta near a million and a
# tool-call delta near ten stay legible on the same mark spec. The knee is a
# fixed fraction of the widest observed magnitude, so the scale is a function of
# the data alone and two runs of the same artifact place every point identically.
SYMLOG_KNEE_FRACTION = 0.01


def build_report_view_model(report: Mapping[str, object]) -> dict[str, object]:
    """Everything the page renders that the report JSON does not already state."""
    execution = mapping(report, "execution")
    scope = mapping(report, "scope")
    treatments = [str(item) for item in _sequence(execution, "treatments")]
    models = [str(item) for item in _sequence(report, "model_order")]
    transfer_cases = int(execution["transfer_cases"])
    repetitions = int(execution["repetitions_per_model_case"])
    runs_per_treatment = transfer_cases * repetitions
    verdicts = _verdicts(report)
    return {
        "view_schema_version": REPORT_VIEW_SCHEMA_VERSION,
        "languages": list(LANGUAGES),
        "report_json_filename": f"semantic-rca-v{scope['benchmark_protocol_version']}.json",
        "treatment_labels": dict(TREATMENT_LABELS),
        "treatments": treatments,
        "models": models,
        "facts": {
            "completed_cells": int(execution["completed_cells"]),
            "models": len(models),
            "transfer_cases": transfer_cases,
            "micro_cases": int(execution["micro_cases"]),
            "repetitions": repetitions,
            "treatment_count": len(treatments),
            "runs_per_treatment": runs_per_treatment,
            "runs_per_model": runs_per_treatment * len(treatments),
            "case_model_pairs": transfer_cases * len(models),
            "mechanism_cells": sum(
                len(mapping_list(_transfer(report, model), "mechanism_effects")) for model in models
            ),
            "capability_rubric_label": capability_rubric_label(),
            "capability_rubric_maximum": rubric_maximum(),
        },
        "verdicts": verdicts,
        "takeaways": _takeaways(report),
        "interface_matrix": _interface_matrix(report),
        # "CPU saturation", not whatever title-casing the code point order gives.
        "mechanism_labels": {
            str(code): {language: mechanism_label(code, language) for language in LANGUAGES}
            for code, _ in mechanism_cohort(mapping_list(report, "case_catalog"))
        },
        "scope_labels": {
            key: dict(value)
            for key, value in CAUSAL_SCOPE_LABELS.items()
            if key in {str(case["causal_scope"]) for case in mapping_list(report, "case_catalog")}
        },
        # Published so the page states the one rule it applies.
        "evidence_rule": {"alpha": SIGNIFICANCE_ALPHA},
        "narrative": {language: _narrative(report, language) for language in LANGUAGES},
        "attribution_links": _attribution_links(report),
        "charts": {
            "delta_strips": _delta_strips(report),
            "diagnosis_slope": _diagnosis_slope(report),
            "diagnosis_by_scope": _diagnosis_split(report, "causal_scope"),
            "diagnosis_by_dataset": _diagnosis_split(report, "dataset"),
            "cost_bars": _cost_bars(report),
            "pricing_basis": _pricing_basis(report),
            "capability_bars": _capability_bars(report),
        },
    }


# --- verdicts -------------------------------------------------------------


def _verdicts(report: Mapping[str, object]) -> list[dict[str, object]]:
    """One row per research question, with the endpoint tally behind its status."""
    verdicts = []
    for question in mapping_list(report, "research_questions"):
        goal = str(question["goal"])
        if goal not in FAMILY_COMPARISONS:
            verdicts.append(
                {
                    "goal": goal,
                    "role": str(question["status"]),
                    "status": "descriptive",
                    "endpoints": None,
                    "tally_text": {
                        "en": "Reported as a descriptive ranking; no significance is claimed",
                        "zh": "仅作描述性排名，不声称显著性",
                    },
                }
            )
            continue
        results = _family_results(report, goal)
        if not results:
            raise ValueError(f"formal report has no primary results for family: {goal}")
        tally = _family_tally(report, goal)
        status = _family_status(tally)
        verdicts.append(
            {
                "goal": goal,
                "role": str(question["status"]),
                "status": status,
                "comparison": _comparison_label(goal),
                "endpoints": {
                    "total": tally["total"],
                    "significant": tally["confirmed"],
                    "favouring_treatment": tally["favouring_treatment"],
                    "favouring_baseline": tally["favouring_baseline"],
                    "family_size": _family_size(results),
                },
                "tally_text": {language: _tally_text(tally, language) for language in LANGUAGES},
            }
        )
    return verdicts


def _tally_text(tally: Mapping[str, int], language: str) -> str:
    """How many of a family's registered endpoints were significant."""
    total, confirmed = tally["total"], tally["confirmed"]
    if language == "zh":
        if not confirmed:
            return f"{total} 项注册端点均未通过 Holm 校正"
        return f"{total} 项注册端点中 {confirmed} 项通过 Holm 校正"
    if not confirmed:
        return f"No registered endpoint of {total} survived Holm correction"
    return f"{confirmed} of {total} registered endpoints survived Holm correction"


def _interface_matrix(report: Mapping[str, object]) -> dict[str, object]:
    """What each arm actually gave the agent, from the protocol's own component list.

    A reader who has never seen this stack needs the three arms defined before
    any delta means anything, and defining them from the bound protocol keeps the
    definition from drifting away from what the runs were given.
    """
    components = mapping(mapping(report, "scope"), "treatment_components")
    treatments = [str(item) for item in _sequence(mapping(report, "execution"), "treatments")]
    rows: list[str] = []
    for treatment in treatments:
        for component in components.get(treatment) or ():
            if str(component) not in rows:
                rows.append(str(component))
    return {
        "treatments": treatments,
        "components": rows,
        "present": {
            treatment: [str(item) for item in (components.get(treatment) or ())]
            for treatment in treatments
        },
    }


def _takeaways(report: Mapping[str, object]) -> list[dict[str, object]]:
    """The three findings a reader who stops after the first screen should have.

    Ordered by how much the measurement supports them, not by which arm they
    favour, and each carries the counts it was derived from so the claim and its
    evidence cannot drift apart.
    """
    storage = _family_tally(report, "storage_shape")
    semantic = _family_tally(report, "semantic_layer")
    diagnosis = _diagnosis_slope(report)
    totals = {
        treatment: sum(int(item["values"][treatment]) for item in diagnosis["series"])
        for treatment in diagnosis["treatments"]
    }
    total_runs = diagnosis["runs_per_treatment"] * len(diagnosis["series"])
    scopes = mapping(report, "diagnosis_by_causal_scope")
    return [
        {
            "id": "one_store",
            "goal": "storage_shape",
            "grade": _tally_grade(storage),
            "evidence": {
                **storage,
                "diagnosis": totals,
                "diagnosis_runs": total_runs,
            },
        },
        {
            "id": "semantic_layer",
            "goal": "semantic_layer",
            "grade": _tally_grade(semantic),
            "evidence": dict(semantic),
        },
        {
            "id": "fault_dependent",
            "goal": "semantic_layer",
            "grade": "descriptive",
            "evidence": {
                scope: {
                    "cases": int(mapping(scopes, scope)["cases"]),
                    **{
                        treatment: int(value)
                        for treatment, value in mapping(
                            mapping(scopes, scope), "diagnosis_correct"
                        ).items()
                    },
                }
                for scope in scopes
            },
        },
    ]


def _tally_grade(tally: Mapping[str, int]) -> str:
    return "confirmed" if tally["confirmed"] else "not_confirmed"


def _family_results(report: Mapping[str, object], family: str) -> list[dict[str, object]]:
    """Every registered endpoint in one confirmatory family, across models."""
    results = []
    for model in _sequence(report, "model_order"):
        families = mapping(_transfer(report, str(model)), "confirmatory_families")
        raw_family = families.get(family)
        if raw_family is None:
            continue
        if not isinstance(raw_family, Mapping):
            raise ValueError("confirmatory family result is not an object")
        metrics = mapping(raw_family, "primary_metrics")
        comparison = raw_family.get("comparison")
        if not isinstance(comparison, str):
            raise ValueError("confirmatory family comparison is malformed")
        for metric_name, effect in metrics.items():
            if not isinstance(effect, Mapping):
                raise ValueError("confirmatory primary metric is not an object")
            results.append(
                {
                    "family": family,
                    "model": str(model),
                    "metric": str(metric_name),
                    "comparison": comparison,
                    "effect": dict(effect),
                    "significant": _endpoint_significant(effect),
                }
            )
    return results


def _endpoint_significant(effect: Mapping[str, object]) -> bool:
    """The registered test outcome, and the only verdict the page draws."""
    adjusted = effect.get("holm_adjusted_p")
    return isinstance(adjusted, (int, float)) and float(adjusted) < SIGNIFICANCE_ALPHA


def _endpoint_direction(effect: Mapping[str, object]) -> int:
    """-1 when most cases favour the first treatment named, +1 the second, 0 neither."""
    negative = int(effect.get("negative_cases") or 0)
    positive = int(effect.get("positive_cases") or 0)
    if negative > positive:
        return -1
    return 1 if positive > negative else 0


def _family_tally(report: Mapping[str, object], family: str) -> dict[str, int]:
    """The registered quantities across a family's endpoints, counted once.

    Median sign counts are descriptive: they say which way each endpoint's case
    median pointed, not that the endpoint showed an effect. Only `confirmed`
    carries an inferential claim.
    """
    results = _family_results(report, family)
    directions = [_endpoint_direction(mapping(item, "effect")) for item in results]
    return {
        "total": len(results),
        "confirmed": sum(bool(item["significant"]) for item in results),
        "favouring_treatment": directions.count(-1),
        "favouring_baseline": directions.count(1),
        "no_direction": directions.count(0),
    }


def _family_status(tally: Mapping[str, int]) -> str:
    """Whether the registered tests in this family were significant."""
    if tally["confirmed"] == tally["total"]:
        return "supported"
    if tally["confirmed"]:
        return "partially_supported"
    return "not_supported"


def _family_size(results: Sequence[Mapping[str, object]]) -> int:
    sizes = {
        int(mapping(item, "effect")["multiplicity_family_size"])
        for item in results
        if isinstance(mapping(item, "effect").get("multiplicity_family_size"), int)
    }
    if len(sizes) != 1:
        raise ValueError("confirmatory family multiplicity size is inconsistent")
    return sizes.pop()


def _comparison_label(family: str) -> str:
    treatment, baseline = FAMILY_COMPARISONS[family]
    return f"{TREATMENT_LABELS[treatment]} − {TREATMENT_LABELS[baseline]}"


# --- narrative ------------------------------------------------------------


def _narrative(report: Mapping[str, object], language: str) -> dict[str, object]:
    attribution_text, attribution_terms = _attribution(report, language)
    return {
        "conclusion": _conclusion(report, language),
        "headline": _headline(report, language),
        "families": {
            family: _family_finding(report, family, language) for family in FAMILY_COMPARISONS
        },
        "takeaways": _takeaway_text(report, language),
        "micro_row_reduction": _micro_row_reduction(report, language),
        "micro_summary": _micro_summary_text(report, language),
        "mechanism_summary": _mechanism_summary(report, language),
        "cost_direction": _cost_direction_text(report, language),
        "dataset_reversal": _dataset_reversal_text(report, language),
        "tool_use": _tool_use_text(report, language),
        "citation_submission": _citation_submission_text(report, language),
        "cohort_mechanisms": _mechanism_cohort_phrase(report, language),
        "causal_scopes": _causal_scope_phrase(report, language),
        "attribution_text": attribution_text,
        "attribution_terms": attribution_terms,
    }


def _significant_text(result: Mapping[str, object], language: str) -> str:
    effect = mapping(result, "effect")
    parts = str(result["comparison"]).split(" - ")
    if len(parts) != 2:
        raise ValueError("confirmatory family comparison is malformed")
    delta = effect.get("case_median_delta")
    if not isinstance(delta, (int, float)):
        raise ValueError("significant primary result has no case median delta")
    left, right = (TREATMENT_LABELS.get(part, part) for part in parts)
    favored, other = (left, right) if delta < 0 else (right, left)
    metric = _metric_label(result["metric"], language)
    delta_text = f"{float(delta):+,.12g}"
    p_text = f"{float(effect['holm_adjusted_p']):.8g}"
    if language == "zh":
        return (
            f"{result['model']}：{favored} 使用的 {metric} 少于 {other}"
            f"（case-median Δ {delta_text}；Holm p {p_text}）"
        )
    return (
        f"{result['model']}: {favored} used fewer {metric} than {other} "
        f"(case-median Δ {delta_text}; Holm p {p_text})"
    )


def _family_finding(report: Mapping[str, object], family: str, language: str) -> str:
    results = _family_results(report, family)
    if not results:
        raise ValueError(f"formal report has no primary results for confirmatory family: {family}")
    significant = [item for item in results if item["significant"]]
    comparison = _comparison_label(family)
    if not significant:
        size = _family_size(results)
        if language == "zh":
            return f"{comparison} 的 {size} 项检验，经 Holm 校正后没有一项显著。"
        return f"None of the {size} {comparison} tests is significant after Holm correction."
    separator = "；" if language == "zh" else "; "
    terminator = "。" if language == "zh" else "."
    return separator.join(_significant_text(item, language) for item in significant) + terminator


def _conclusion(report: Mapping[str, object], language: str) -> str:
    semantic = [item for item in _family_results(report, "semantic_layer") if item["significant"]]
    storage = [item for item in _family_results(report, "storage_shape") if item["significant"]]
    micro = _micro_row_reduction(report, language)
    if language == "zh":
        semantic_text = (
            "语义层检验族没有端到端主要指标通过 Holm 校正"
            if not semantic
            else f"语义层检验族有 {len(semantic)} 项端到端主要指标通过 Holm 校正"
        )
        storage_text = (
            "接口组合检验族没有主要指标通过 Holm 校正"
            if not storage
            else "接口组合检验族的显著结果是"
            + "；".join(_significant_text(item, language) for item in storage)
        )
        return (
            f"聚焦检索中 Graph 减少 rows 的合格结果为 {micro}；{semantic_text}。"
            f"{storage_text}。其余端到端效果随模型和故障机制变化。"
        )
    semantic_text = (
        "No end-to-end primary endpoint in the semantic-layer family passes Holm correction"
        if not semantic
        else f"{len(semantic)} end-to-end primary endpoints in the semantic-layer family "
        "pass Holm correction"
    )
    storage_text = (
        "No primary endpoint in the interface-bundle family passes Holm correction"
        if not storage
        else (
            "The significant interface-bundle result is "
            if len(storage) == 1
            else "The significant interface-bundle results are "
        )
        + "; ".join(_significant_text(item, language) for item in storage)
    )
    return (
        f"Eligible focused-retrieval results where Graph reduced rows: {micro}. "
        f"{semantic_text}. {storage_text}. Other end-to-end effects vary by model and fault "
        "mechanism."
    )


def _headline(report: Mapping[str, object], language: str) -> str:
    storage = [item for item in _family_results(report, "storage_shape") if item["significant"]]
    semantic = [item for item in _family_results(report, "semantic_layer") if item["significant"]]
    if storage and not semantic:
        count = len(storage)
        if language == "zh":
            return f"接口组合 {count} 项显著；Graph 端到端不稳定"
        return (
            f"{'One' if count == 1 else count} interface "
            f"endpoint{' is' if count == 1 else 's are'} significant; Graph E2E varies"
        )
    if language == "zh":
        return "聚焦检索更省，端到端因场景而异"
    return "Focused retrieval improves; E2E varies"


def _micro_row_reduction(report: Mapping[str, object], language: str) -> str:
    """Eligible micro cases where Graph returned fewer rows, per benchmark."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for model in _sequence(report, "model_order"):
        micro = mapping(mapping(mapping(report, "model_reports"), str(model)), "micro")
        for benchmark, summary in mapping(micro, "benchmarks").items():
            if not isinstance(summary, Mapping):
                raise ValueError("micro benchmark summary is not an object")
            effect = mapping(
                mapping(summary, "case_level_effect"), "rows_returned_through_evidence"
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


def _micro_summary_text(report: Mapping[str, object], language: str) -> str:
    """The focused-retrieval result as a sentence, not a bare ratio.

    `_micro_row_reduction` is a fragment built for embedding in the conclusion;
    on its own it reads as a stray label.
    """
    parts = _micro_row_reduction(report, language)
    if language == "zh":
        return (
            f"在两端都合格的 case 上，加了语义层之后读回的行数都变少了：{parts}。"
            "这一层收益没有传导到端到端调查。"
        )
    return (
        f"On every eligible case, the semantic layer read back fewer rows: {parts}. "
        "That saving did not carry through to the end-to-end investigations."
    )


def _mechanism_row_effects(report: Mapping[str, object]) -> dict[str, dict[str, float | None]]:
    effects: dict[str, dict[str, float | None]] = defaultdict(dict)
    for model in _sequence(report, "model_order"):
        for item in mapping_list(_transfer(report, str(model)), "mechanism_effects"):
            rows = mapping(mapping(item, "metrics"), "rows_returned")
            value = rows.get("case_median_delta") if rows.get("eligible_cases", 0) else None
            effects[str(item["mechanism_code"])][str(model)] = (
                float(value) if isinstance(value, (int, float)) else None
            )
    return effects


def _mechanism_summary(report: Mapping[str, object], language: str) -> str:
    effects = _mechanism_row_effects(report)
    parts = []
    for mechanism, _ in mechanism_cohort(mapping_list(report, "case_catalog")):
        values = [value for value in effects.get(mechanism, {}).values() if value is not None]
        better = sum(value < 0 for value in values)
        parts.append(f"{mechanism_label(mechanism, language)} {better}/{len(values)}")
    case_better = 0
    case_total = 0
    for case in mapping_list(report, "case_outcomes"):
        case_better += int(case.get("models_with_fewer_rows", 0))
        case_total += int(case.get("eligible_models", 0))
    if language == "zh":
        return (
            f"每种机制下，返回行数变少的模型数：{_join(parts, language)}。"
            f"逐 case 看是 {case_better}/{case_total} 个可估算的模型-case 组合。"
            "表里的负数表示加了语义层之后读回的行更少。"
        )
    return (
        f"Models that read back fewer rows with the semantic layer, by mechanism: "
        f"{_join(parts, language)}. Case by case that is "
        f"{case_better} of {case_total} estimable model-case combinations. "
        "A negative number in the table means fewer rows with the layer."
    )


def _takeaway_text(report: Mapping[str, object], language: str) -> dict[str, dict[str, str]]:
    """A headline and a supporting sentence per takeaway, built from its counts.

    Each headline states only what its own evidence supports. The first names the
    one endpoint that survived correction rather than generalising from it; the
    third reports a reversal without attributing it, because fault level and
    source dataset are fully confounded in this cohort.
    """
    takeaways = {item["id"]: mapping(item, "evidence") for item in _takeaways(report)}
    storage = takeaways["one_store"]
    semantic = takeaways["semantic_layer"]
    diagnosis = mapping(storage, "diagnosis")
    runs = int(storage["diagnosis_runs"])
    split, raw, graph = (
        int(diagnosis["split_pillars"]),
        int(diagnosis["raw"]),
        int(diagnosis["semantic_graph"]),
    )
    significant = [item for item in _family_results(report, "storage_shape") if item["significant"]]
    confirmed = _significant_text(significant[0], language) if len(significant) == 1 else None
    reversal = takeaways["fault_dependent"]
    ahead, behind = [], []
    for scope in reversal:
        bucket = mapping(reversal, scope)
        label = CAUSAL_SCOPE_LABELS.get(scope, {}).get(language, scope)
        gap = int(bucket["semantic_graph"]) - int(bucket["raw"])
        if language == "zh":
            part = (
                f"{label}（{bucket['cases']} 个 case）{bucket['semantic_graph']} 比 {bucket['raw']}"
            )
        else:
            part = (
                f"{label} faults ({bucket['cases']} cases), "
                f"{bucket['semantic_graph']} against {bucket['raw']}"
            )
        (ahead if gap >= 0 else behind).append(part)

    if language == "zh":
        return {
            "one_store": {
                "headline": (
                    "唯一通过校正的结果：Fable 在一体化接口下读的 token 每个 case 都更少。"
                ),
                "support": (
                    f"{storage['total']} 项注册端点里只有这一项通过 Holm 校正"
                    f"（{confirmed}）。另有 "
                    f"{storage['favouring_treatment'] - storage['confirmed']} "
                    f"项的 case median 也指向一体化接口，但都没有通过校正，只能作为方向记录。"
                    f"同一批故障的诊断正确数：一体化接口 {raw}，三后端组合 {split}，"
                    f"各 {runs} 次 run；"
                    "诊断正确率不是注册端点，只作描述。"
                ),
            },
            "semantic_layer": {
                "headline": "没有证据显示语义层减少了调查工作量。",
                "support": (
                    f"{semantic['total']} 项注册端点中 {semantic['confirmed']} 项通过 Holm 校正。"
                    f"case median 的方向也不一致：{semantic['favouring_treatment']} 项指向语义层，"
                    f"{semantic['favouring_baseline']} 项指向不加语义层。"
                    f"诊断正确数：加语义层 {graph} 次，不加 {raw} 次。"
                ),
            },
            "fault_dependent": {
                "headline": "Graph 与 Raw 的诊断差在两类故障上方向相反。",
                "support": (
                    f"按故障所在层级拆开——{_join(ahead, language)} 上 Graph 更高；"
                    f"{_join(behind, language)} 上 Raw 更高。"
                    "本队列里节点故障全部来自同一个数据源，层级和数据源完全混杂，"
                    "因此无法判断是哪一个造成了这个反转。这是描述，不是解释。"
                ),
            },
        }
    return {
        "one_store": {
            "headline": (
                "One endpoint survived correction: Fable read fewer tokens through "
                "the single interface in every eligible case."
            ),
            "support": (
                f"It is the only one of {storage['total']} registered endpoints to pass Holm "
                f"correction ({confirmed}). Another "
                f"{storage['favouring_treatment'] - storage['confirmed']} endpoints have a case "
                "median pointing the same way but did not pass correction, so they are recorded "
                f"as a direction and nothing more. On the same incidents the models diagnosed "
                f"{raw} correctly through one database and {split} through the three-backend "
                f"bundle, out of {runs} runs each; diagnosis accuracy is descriptive, not a "
                "registered endpoint."
            ),
        },
        "semantic_layer": {
            "headline": "No evidence that the semantic layer reduced investigation work.",
            "support": (
                f"{semantic['confirmed']} of {semantic['total']} registered endpoints pass Holm "
                f"correction. The case medians do not agree either: "
                f"{semantic['favouring_treatment']} point to the semantic layer and "
                f"{semantic['favouring_baseline']} point the other way. Correct diagnoses came "
                f"out at {graph} with the layer and {raw} without."
            ),
        },
        "fault_dependent": {
            "headline": "The Graph-to-Raw diagnosis gap runs opposite ways on the two cohorts.",
            "support": (
                f"Split by the level the fault sat at, Graph is ahead on "
                f"{_join(ahead, language)}, and behind on {_join(behind, language)}. "
                "Every node-level case in this cohort comes from a single source, so level and "
                "source are fully confounded and neither can be credited with the reversal. "
                "This is a description, not an explanation."
            ),
        },
    }


def _cost_direction_text(report: Mapping[str, object], language: str) -> dict[str, str]:
    """How many priced models spent less, stated per comparison.

    Keyed by family so a section can quote its own comparison without dragging
    the other question's result in beside it.

    Counted from the actual per-treatment spend rather than asserted, so a run
    where nothing got cheaper cannot be described as one where something did.
    Models without a frozen rate are excluded from both the count and the base.
    """
    series = _cost_bars(report)["series"]
    priced = [item for item in series if item["estimable"]]
    unpriced = [str(item["model"]) for item in series if not item["estimable"]]
    texts = {}
    for family, (treatment, baseline) in FAMILY_COMPARISONS.items():
        comparable = [
            item
            for item in priced
            if isinstance(item["values"].get(treatment), (int, float))
            and isinstance(item["values"].get(baseline), (int, float))
        ]
        cheaper = sum(item["values"][treatment] < item["values"][baseline] for item in comparable)
        cheaper_label = TREATMENT_LABELS[treatment]
        other_label = TREATMENT_LABELS[baseline]
        if language == "zh":
            text = (
                f"{len(comparable)} 个可计价模型里，{cheaper} 个走 {cheaper_label} 比走 "
                f"{other_label} 便宜。"
            )
            if unpriced:
                text += f"{_join(unpriced, language)} 没有冻结的官方价格，不参与比较。"
        else:
            text = (
                f"{cheaper} of {len(comparable)} priced models spent less through "
                f"{cheaper_label} than through {other_label}."
            )
            if unpriced:
                text += (
                    f" {_join(unpriced, language)} "
                    f"{'has' if len(unpriced) == 1 else 'have'} no frozen rate and "
                    f"{'is' if len(unpriced) == 1 else 'are'} left out of the comparison."
                )
        texts[family] = text
    return texts


def _dataset_reversal_text(report: Mapping[str, object], language: str) -> str:
    """States the direction of the Graph − Raw diagnosis difference per source.

    The cohort-wide total hides that the two sources disagree, so the sentence is
    derived from the split rather than asserted alongside it.
    """
    buckets = mapping(report, "diagnosis_by_dataset")
    parts = []
    for dataset, bucket in buckets.items():
        correct = mapping(bucket, "diagnosis_correct")
        label = _dataset_attribution(dataset)["label"]
        graph = int(correct["semantic_graph"])
        raw = int(correct["raw"])
        cases = int(bucket["cases"])
        if language == "zh":
            parts.append(f"{label} 的 {cases} 个 case 上 Graph {graph}、Raw {raw}")
        else:
            parts.append(f"{graph} against {raw} over the {cases} {label} cases")
    if language == "zh":
        return (
            f"同一批数据按来源拆分是：{_join(parts, language)}。"
            "节点故障全部来自后一个来源，因此层级和来源在本队列里无法区分，"
            "两种拆法只是同一个分界的两种说法。"
        )
    return (
        f"The same runs split by source instead: {_join(parts, language)}. "
        "Every node fault comes from the second source, so level and source cannot be "
        "separated in this cohort; the two splits are two names for one boundary."
    )


def _tool_use_text(report: Mapping[str, object], language: str) -> str:
    audit = mapping(report, "tool_use_audit")
    tool = mapping(audit, "semantic_graph_tool")
    by_treatment = mapping(audit, "by_treatment")
    join_calls = join_runs = cross_calls = cross_runs = 0
    for treatment in ("raw", "semantic_graph"):
        bucket = by_treatment.get(treatment)
        if not isinstance(bucket, Mapping):
            continue
        join_calls += int(bucket["successful_sql_join_calls"])
        join_runs += int(bucket["runs_with_successful_sql_join"])
        cross_calls += int(bucket["successful_cross_signal_join_calls"])
        cross_runs += int(bucket["runs_with_successful_cross_signal_join"])
    promql = {
        treatment: int(mapping(by_treatment, treatment)["runs_with_successful_promql_evaluation"])
        for treatment in by_treatment
    }
    greptime_promql = promql.get("raw", 0) + promql.get("semantic_graph", 0)
    greptime_runs = sum(
        int(mapping(by_treatment, treatment)["runs"])
        for treatment in ("raw", "semantic_graph")
        if treatment in by_treatment
    )
    split_runs = (
        int(mapping(by_treatment, "split_pillars")["runs"])
        if "split_pillars" in by_treatment
        else 0
    )
    if language == "zh":
        return (
            f"语义层确实被用上了：{tool['runs']} 次 Graph run 里有 "
            f"{tool['runs_with_successful_call']} 次至少成功调用过一次 query_semantic_graph，"
            f"合计 {tool['successful_calls']} 次。两个 GreptimeDB 接口一共发出 {join_calls} 次"
            f"成功的 SQL JOIN，分布在 {join_runs} 次 run 里；其中真正跨信号的只有 "
            f"{cross_calls} 次，出现在 {cross_runs} 次 run 里。用到 PromQL 求值的，"
            f"GreptimeDB 侧是 {greptime_runs} 次里的 {greptime_promql} 次，"
            f"三后端侧是 {split_runs} 次里的 {promql.get('split_pillars', 0)} 次。"
        )
    return (
        f"The semantic layer was actually used: {tool['runs_with_successful_call']} of "
        f"{tool['runs']} runs that had it made at least one successful query_semantic_graph "
        f"call, {tool['successful_calls']} calls in all. The two GreptimeDB interfaces issued "
        f"{join_calls} successful SQL JOIN calls across {join_runs} runs, of which only "
        f"{cross_calls} joined across signal kinds, in {cross_runs} runs. PromQL evaluation "
        f"shows up in {greptime_promql} of {greptime_runs} GreptimeDB runs and "
        f"{promql.get('split_pillars', 0)} of {split_runs} three-backend runs."
    )


def _citation_submission_text(report: Mapping[str, object], language: str) -> str | None:
    """Names the models that ended runs without citing anything, if any did.

    A model with few eligible cases looks like a small sample until you know it
    withheld citations, so the reason is stated next to the counts.
    """
    by_model = mapping(mapping(report, "citation_submission"), "by_model")
    offenders = [
        (model, mapping(by_model, model))
        for model in _sequence(report, "model_order")
        if int(mapping(by_model, str(model))["runs_without_citation"]) > 0
    ]
    if not offenders:
        return None
    parts = []
    for model, counts in offenders:
        if language == "zh":
            parts.append(
                f"{model} 的 {counts['runs']} 次 run 里有 "
                f"{counts['runs_without_citation']} 次没有提交任何引用"
                f"（其中 {counts['correct_diagnoses_lost_to_missing_citation']} 次诊断本来是对的）"
            )
        else:
            parts.append(
                f"{model} submitted no citation in "
                f"{counts['runs_without_citation']} of its {counts['runs']} runs, "
                f"{counts['correct_diagnoses_lost_to_missing_citation']} of which "
                "had reached a correct diagnosis"
            )
    if language == "zh":
        return (
            f"{_join(parts, language)}。"
            "入选需要至少一条可执行的引用，所以这些 run 不进配对样本；"
            "该模型合格 case 偏少是这个原因，不是随机波动。"
        )
    return (
        f"{_join(parts, language)}. Eligibility needs at least one execution-valid "
        "citation, so those runs leave the paired sample. A small eligible count for "
        "that model reflects this habit rather than chance."
    )


def _mechanism_cohort_phrase(report: Mapping[str, object], language: str) -> str:
    counts = mechanism_cohort(mapping_list(report, "case_catalog"))
    separator = "、" if language == "zh" else ", "
    return separator.join(f"{mechanism_label(code, language)} {count}" for code, count in counts)


def _causal_scope_phrase(report: Mapping[str, object], language: str) -> str:
    scopes: list[str] = []
    for case in mapping_list(report, "case_catalog"):
        label = CAUSAL_SCOPE_LABELS.get(str(case["causal_scope"]))
        if label is None:
            raise ValueError(f"no publishable label for causal scope: {case['causal_scope']}")
        if label[language] not in scopes:
            scopes.append(label[language])
    if language == "zh":
        return "、".join(scopes)
    return f"{', '.join(scopes[:-1])}, or {scopes[-1]}" if len(scopes) > 2 else " or ".join(scopes)


def _dataset_attribution(adapter: object) -> dict[str, str]:
    attribution = DATASET_ATTRIBUTION.get(str(adapter))
    if attribution is None:
        raise ValueError(f"no publishable attribution for dataset: {adapter}")
    return attribution


def _cohort_datasets(report: Mapping[str, object]) -> list[str]:
    provenance = mapping(report, "cohort_provenance")
    entries = (*mapping_list(provenance, "micro"), *mapping_list(provenance, "transfer"))
    return list(dict.fromkeys(str(entry["dataset"]) for entry in entries))


def _attribution(report: Mapping[str, object], language: str) -> tuple[str, str]:
    provenance = mapping(report, "cohort_provenance")
    micro = mapping_list(provenance, "micro")
    transfer = mapping_list(provenance, "transfer")
    micro_parts = []
    for entry in micro:
        benchmark = MICRO_BENCHMARK_LABELS.get(str(entry["benchmark"]))
        if benchmark is None:
            raise ValueError(f"no publishable label for benchmark: {entry['benchmark']}")
        label = _dataset_attribution(entry["dataset"])["label"]
        micro_parts.append(
            f"{entry['cases']} 个来自 {label} 的 {benchmark['zh']} case"
            if language == "zh"
            else f"{entry['cases']} {benchmark['en']} cases from {label}"
        )
    transfer_parts = [
        f"{entry['cases']} 个来自 {_dataset_attribution(entry['dataset'])['label']} 的 case"
        if language == "zh"
        else f"{entry['cases']} cases from {_dataset_attribution(entry['dataset'])['label']}"
        for entry in transfer
    ]
    datasets = _cohort_datasets(report)
    names = _join([_dataset_attribution(adapter)["label"] for adapter in datasets], language)
    if language == "zh":
        text = (
            f"Micro-benchmark 使用 {_join(micro_parts, language)}；"
            f"端到端 cohort 使用 {_join(transfer_parts, language)}。"
            f"感谢 {names} 的作者与维护者公开数据和研究材料，使本评测能够复现。"
        )
        terms = (
            "".join(_dataset_attribution(adapter)["zh"] for adapter in datasets)
            + "本项目不替上游解决 license 冲突，也不重新分发原始 telemetry。"
        )
        return text, terms
    text = (
        f"The micro-benchmarks use {_join(micro_parts, language)}. The end-to-end cohort "
        f"uses {_join(transfer_parts, language)}. We thank the authors and maintainers of "
        f"{names} for publishing the datasets and research materials that make this "
        "evaluation reproducible."
    )
    terms = (
        " ".join(_dataset_attribution(adapter)["en"] for adapter in datasets)
        + " This project does not resolve upstream license conflicts and does not "
        "redistribute source telemetry."
    )
    return text, terms


def _attribution_links(report: Mapping[str, object]) -> list[dict[str, str]]:
    return [
        {
            "label": _dataset_attribution(adapter)["label"],
            "url": _dataset_attribution(adapter)["url"],
        }
        for adapter in _cohort_datasets(report)
    ]


def _metric_label(metric: object, language: str) -> str:
    labels = {
        "correct_completion_tool_calls": (
            "correct-completion tool calls",
            "正确完成所需的工具调用",
        ),
        "provider_visible_input_tokens": (
            "provider-visible input tokens",
            "provider-visible input",
        ),
        "rows_returned": ("rows returned", "返回行数"),
    }
    english, chinese = labels.get(str(metric), (str(metric), str(metric)))
    return chinese if language == "zh" else english


def _join(items: Sequence[str], language: str) -> str:
    if language == "zh":
        return "、".join(items)
    if len(items) < 3:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


# --- charts ---------------------------------------------------------------


def _delta_strips(report: Mapping[str, object]) -> list[dict[str, object]]:
    """One strip per model and registered endpoint: the sign test's own input.

    Each eligible case contributes one point, so a reader can count the signs the
    test counted instead of taking the p value on trust.
    """
    strips = []
    for family, (treatment, baseline) in FAMILY_COMPARISONS.items():
        for result in _family_results(report, family):
            metric = str(result["metric"])
            effect = mapping(result, "effect")
            case_effects = _family_case_effects(report, str(result["model"]), family)
            points = [
                {"case_id": str(item["case_id"]), "value": float(item[metric])}
                for item in case_effects
                if int(item.get("eligible_repetitions", 0)) > 0
                and isinstance(item.get(metric), (int, float))
            ]
            median = effect.get("case_median_delta")
            magnitudes = [abs(point["value"]) for point in points]
            if isinstance(median, (int, float)):
                magnitudes.append(abs(float(median)))
            axis = _symlog_axis(magnitudes)
            strips.append(
                {
                    "family": family,
                    "model": str(result["model"]),
                    "metric": metric,
                    "metric_label": {
                        language: _metric_label(metric, language) for language in LANGUAGES
                    },
                    "comparison": _comparison_label(family),
                    "favors": {
                        "negative": TREATMENT_LABELS[treatment],
                        "positive": TREATMENT_LABELS[baseline],
                    },
                    "points": [
                        {**point, "position": _symlog_position(point["value"], axis)}
                        for point in points
                    ],
                    "median": None
                    if not isinstance(median, (int, float))
                    else {
                        "value": float(median),
                        "position": _symlog_position(float(median), axis),
                    },
                    "counts": {
                        "eligible": int(effect["eligible_cases"]),
                        "negative": int(effect["negative_cases"]),
                        "positive": int(effect["positive_cases"]),
                        "tied": int(effect["tied_cases"]),
                    },
                    "unadjusted_p": effect.get("sign_test_two_sided_p"),
                    "holm_adjusted_p": effect.get("holm_adjusted_p"),
                    "significant": bool(result["significant"]),
                    # Which side the medians fell on is a reading of the data, so
                    # it is decided here and the renderer only prints it.
                    "reading": {
                        language: _strip_reading(effect, treatment, baseline, language)
                        for language in LANGUAGES
                    },
                    "axis": axis,
                }
            )
    return strips


def _strip_reading(
    effect: Mapping[str, object],
    treatment: str,
    baseline: str,
    language: str,
) -> str:
    """One sentence for how the eligible cases split, in the reader's language."""
    negative = int(effect.get("negative_cases") or 0)
    positive = int(effect.get("positive_cases") or 0)
    eligible = int(effect.get("eligible_cases") or 0)
    if negative == positive:
        if language == "zh":
            return f"{eligible} 个故障里两侧没有一致的高下"
        return f"Neither side needed consistently less across {eligible} incidents"
    leading = negative if negative > positive else positive
    side = TREATMENT_LABELS[treatment if negative > positive else baseline]
    if language == "zh":
        return f"{eligible} 个故障里 {leading} 个是 {side} 更省"
    return f"{side} needed less in {leading} of {eligible} incidents"


def _family_case_effects(
    report: Mapping[str, object], model: str, family: str
) -> list[dict[str, object]]:
    families = mapping(_transfer(report, model), "confirmatory_families")
    raw_family = families.get(family)
    if not isinstance(raw_family, Mapping):
        raise ValueError("confirmatory family result is not an object")
    return mapping_list(raw_family, "case_effects")


def _symlog_axis(magnitudes: Sequence[float]) -> dict[str, object]:
    """A symmetric-log axis wide enough for the widest magnitude on the strip.

    `knee` is where the scale turns from linear to logarithmic; below it small
    deltas keep their proportions instead of being pushed onto the zero line.
    """
    widest = max((value for value in magnitudes if value > 0), default=0.0)
    if widest <= 0:
        return {"max_abs": 0.0, "knee": 1.0, "ticks": []}
    knee = max(widest * SYMLOG_KNEE_FRACTION, 1.0)
    axis = {"max_abs": widest, "knee": knee}
    ticks = []
    for value in _tick_values(widest, knee):
        ticks.append({"value": value, "position": _symlog_position(value, axis)})
        if value:
            ticks.insert(0, {"value": -value, "position": _symlog_position(-value, axis)})
    axis["ticks"] = ticks
    return axis


def _tick_values(widest: float, knee: float) -> list[float]:
    """Zero plus every power of ten between the knee and the widest magnitude."""
    values = [0.0]
    exponent = math.floor(math.log10(knee))
    while True:
        tick = 10.0**exponent
        if tick > widest:
            break
        if tick >= knee:
            values.append(tick)
        exponent += 1
    return values


def _symlog_position(value: float, axis: Mapping[str, object]) -> float:
    """Map a delta to [-1, 1]; the browser turns that into pixels."""
    max_abs = float(axis["max_abs"])
    if max_abs <= 0:
        return 0.0
    knee = float(axis["knee"])
    magnitude = abs(float(value))
    scaled = math.log1p(magnitude / knee) / math.log1p(max_abs / knee)
    return math.copysign(scaled, value)


def _diagnosis_slope(report: Mapping[str, object]) -> dict[str, object]:
    """Correct diagnoses per treatment for each model, on a shared run denominator."""
    execution = mapping(report, "execution")
    treatments = [str(item) for item in _sequence(execution, "treatments")]
    runs = int(execution["transfer_cases"]) * int(execution["repetitions_per_model_case"])
    series = []
    for model in _sequence(report, "model_order"):
        correct = mapping(_transfer(report, str(model)), "diagnosis_correct")
        series.append(
            {
                "model": str(model),
                "values": {treatment: int(correct[treatment]) for treatment in treatments},
            }
        )
    return {"treatments": treatments, "runs_per_treatment": runs, "series": series}


def _diagnosis_split(report: Mapping[str, object], field: str) -> dict[str, object]:
    """Diagnosis counts grouped one way, with a label per group.

    Published for both the causal scope and the source dataset. The scope split
    is what the finding is about; the dataset split is published beside it
    because the two are collinear in this cohort and the reader needs to see
    that rather than be told the scope explains it.
    """
    buckets = mapping(report, f"diagnosis_by_{field}")
    groups = []
    for key, bucket in buckets.items():
        correct = mapping(bucket, "diagnosis_correct")
        runs = mapping(bucket, "runs")
        labels = (
            CAUSAL_SCOPE_LABELS.get(key, {})
            if field == "causal_scope"
            else {language: _dataset_attribution(key)["label"] for language in LANGUAGES}
        )
        groups.append(
            {
                "key": key,
                "label": {language: labels.get(language, key) for language in LANGUAGES},
                "cases": int(bucket["cases"]),
                "values": {treatment: int(count) for treatment, count in correct.items()},
                "runs": {treatment: int(count) for treatment, count in runs.items()},
            }
        )
    return {"field": field, "groups": groups}


def _cost_bars(report: Mapping[str, object]) -> dict[str, object]:
    """Actual spend per treatment. A model without a frozen rate stays unpriced.

    Each family reports only the two arms it compares, so the third arm's spend
    has to come from the other family. Reading one family alone would leave the
    split arm looking unpriced when it is merely absent from that comparison.
    """
    costs = mapping(mapping(report, "costs"), "models")
    families = mapping(report, "confirmatory_family_resource_effects")
    treatments = [str(item) for item in _sequence(mapping(report, "execution"), "treatments")]
    series = []
    for model in _sequence(report, "model_order"):
        merged: dict[str, float | None] = {}
        for family in families:
            per_model = mapping(families, family).get(str(model))
            if not isinstance(per_model, Mapping):
                continue
            by_treatment = per_model.get("actual_cost_by_treatment")
            if not isinstance(by_treatment, Mapping):
                continue
            for treatment, value in by_treatment.items():
                cost = float(value) if isinstance(value, (int, float)) else None
                previous = merged.get(treatment)
                if treatment in merged and previous != cost:
                    raise ValueError(
                        f"families disagree on actual cost for {model} {treatment}: "
                        f"{previous} and {cost}"
                    )
                merged[treatment] = cost
        entry = mapping(costs, str(model))
        series.append(
            {
                "model": str(model),
                "currency": entry.get("currency"),
                "estimable": entry.get("status") == "available",
                "unavailable_reason_code": entry.get("unavailable_reason_code"),
                "values": {treatment: merged.get(treatment) for treatment in treatments},
            }
        )
    return {"series": series}


def _pricing_basis(report: Mapping[str, object]) -> list[dict[str, object]]:
    """The frozen rate each cost estimate was computed from, per million tokens."""
    basis = mapping(mapping(report, "costs"), "pricing_basis")
    rows = []
    for model in _sequence(report, "model_order"):
        entry = mapping(basis, str(model))
        rows.append(
            {
                "model": str(model),
                "currency": entry.get("currency"),
                "checked_at": entry.get("checked_at"),
                "source": entry.get("source"),
                "rates": {
                    key: entry.get(key)
                    for key in (
                        "uncached_input_per_million",
                        "cache_read_per_million",
                        "cache_write_per_million",
                        "output_per_million",
                    )
                },
            }
        )
    return rows


def _capability_bars(report: Mapping[str, object]) -> dict[str, object]:
    scores = mapping(report, "capability_scores")
    return {
        "maximum": 100,
        "rubric": mapping(scores, "rubric"),
        "rankings": {
            key: [dict(entry) for entry in value]
            for key, value in mapping(scores, "rankings").items()
            if isinstance(value, list)
        },
    }


# --- rendering ------------------------------------------------------------


def render_formal_measurement_report(report: dict[str, object], output: Path) -> None:
    """Write the self-contained page: skeleton, design, renderer, and both payloads."""
    validate_formal_measurement_report(report)
    view = build_report_view_model(report)
    assets = files("semantic_rca_bench").joinpath("assets/report")
    document = assets.joinpath("index.html").read_text(encoding="utf-8")
    for placeholder, replacement in (
        ("__REPORT_STYLE__", assets.joinpath("report.css").read_text(encoding="utf-8")),
        ("__REPORT_SCRIPT__", assets.joinpath("report.js").read_text(encoding="utf-8")),
        ("__REPORT_I18N__", _inline_json(_load_i18n(assets))),
        ("__REPORT_DATA__", _inline_json(report)),
        ("__REPORT_VIEW__", _inline_json(view)),
        ("__REPORT_SUMMARY__", _static_summary(report, view)),
    ):
        if placeholder not in document:
            raise ValueError(f"report template is missing a placeholder: {placeholder}")
        document = document.replace(placeholder, replacement)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def _load_i18n(assets) -> dict[str, object]:
    strings = json.loads(assets.joinpath("i18n.json").read_text(encoding="utf-8"))
    missing = {
        language: sorted(set(strings["en"]) - set(strings.get(language, {})))
        for language in LANGUAGES
    }
    incomplete = {language: keys for language, keys in missing.items() if keys}
    if incomplete:
        raise ValueError(f"report i18n is missing keys: {incomplete}")
    return strings


def _inline_json(payload: object) -> str:
    # `</` would close the host script element early; the parser reads text, not JSON.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _static_summary(report: Mapping[str, object], view: Mapping[str, object]) -> str:
    """The conclusions a reader gets without JavaScript, from the same view model.

    Leads with the same three findings the page leads with, so a search engine
    or a text-mode reader gets the result rather than a request to enable
    scripts, and cannot come away with a different one.
    """
    facts = mapping(view, "facts")
    english = mapping(mapping(view, "narrative"), "en")
    copy = mapping(english, "takeaways")
    findings = "".join(
        f"<li><strong>{_escape(mapping(copy, str(item['id']))['headline'])}</strong> "
        f"[{_escape(str(item['grade']))}] "
        f"{_escape(mapping(copy, str(item['id']))['support'])}</li>"
        for item in mapping_list(view, "takeaways")
    )
    arms = "".join(
        f"<li><strong>{_escape(TREATMENT_LABELS.get(treatment, treatment))}</strong>: "
        f"{_escape(', '.join(mapping(mapping(view, 'interface_matrix'), 'present')[treatment]))}"
        "</li>"
        for treatment in _sequence(mapping(view, "interface_matrix"), "treatments")
    )
    diagnosis = _diagnosis_slope(report)
    diagnosis_rows = "".join(
        "<li>"
        + _escape(str(item["model"]))
        + ": "
        + _escape(
            ", ".join(
                f"{TREATMENT_LABELS.get(treatment, treatment)} "
                f"{mapping(item, 'values')[treatment]}/{diagnosis['runs_per_treatment']}"
                for treatment in _sequence(diagnosis, "treatments")
            )
        )
        + "</li>"
        for item in mapping_list(diagnosis, "series")
    )
    verdicts = "".join(
        f"<li>{_escape(str(item['goal']).replace('_', ' '))}: "
        f"{_escape(str(item['status']).replace('_', ' '))}</li>"
        for item in mapping_list(view, "verdicts")
    )
    return (
        '<div class="static-summary">'
        "<h2>What we found</h2>"
        f"<ol>{findings}</ol>"
        "<h3>The three interfaces compared</h3>"
        f"<ul>{arms}</ul>"
        "<h3>Correct diagnoses by interface</h3>"
        f"<ul>{diagnosis_rows}</ul>"
        "<h3>Registered questions</h3>"
        f"<ul>{verdicts}</ul>"
        f"<p>{_escape(english['tool_use'])}</p>"
        f"<p>{facts['completed_cells']} completed runs across {facts['models']} models, "
        f"{facts['transfer_cases']} end-to-end cases and {facts['micro_cases']} micro cases. "
        'The full narrative report is in <a href="https://github.com/GreptimeTeam/'
        'semantic-rca-bench/blob/main/REPORT.md">REPORT.md</a>.</p>'
        "</div>"
    )


def _transfer(report: Mapping[str, object], model: str) -> dict[str, object]:
    return mapping(mapping(mapping(report, "model_reports"), model), "transfer")


def _sequence(value: Mapping[str, object], key: str) -> list[object]:
    items = value.get(key)
    if not isinstance(items, list):
        raise ValueError(f"formal report field is not a list: {key}")
    return items


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)
