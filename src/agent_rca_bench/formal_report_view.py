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
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from agent_rca_bench.formal_report import (
    CAUSAL_SCOPE_LABELS,
    DATASET_ATTRIBUTION,
    MICRO_BENCHMARK_LABELS,
    capability_rubric_label,
    validate_formal_measurement_report,
)
from agent_rca_bench.formal_report import _capability_rubric_maximum as rubric_maximum
from agent_rca_bench.formal_report import _mapping as mapping
from agent_rca_bench.formal_report import _mapping_list as mapping_list
from agent_rca_bench.formal_report import _mechanism_cohort as mechanism_cohort
from agent_rca_bench.formal_report import _mechanism_label as mechanism_label
from agent_rca_bench.formal_report import _to_usd as to_usd

# Where the report is published, as recorded in CITATION.cff and the README
# badge. A page only claims a canonical URL when its caller knows one: a
# supplementary artifact served from somewhere else would otherwise tell a
# crawler that the primary report is a duplicate of it.
PUBLICATION_URL = "https://rca-bench.greptime.com"

# The site's own share-card geometry, so a card from this page crops the way a
# card from greptime.com does.
COVER_SIZE = (1800, 900)

REPORT_VIEW_SCHEMA_VERSION = 1

LANGUAGES = ("en", "zh")

TREATMENT_LABELS = {"raw": "Raw", "semantic_graph": "Graph", "split_pillars": "Split"}

# Raw, Graph and Split are the short axis labels a chart needs. A sentence naming
# an arm says what it is instead: a reader meeting "Raw used fewer tokens" has to
# look the code up, and the finding is where they are least willing to.
ARM_NAMES = {
    "raw": {"en": "GreptimeDB", "zh": "GreptimeDB"},
    "semantic_graph": {"en": "the Semantic Graph", "zh": "语义层"},
    "split_pillars": {"en": "three backends", "zh": "三个后端"},
}

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

# Decimal places kept on a plotted axis coordinate. The mapping runs through
# `log1p`, and libm implementations are free to differ in the last place, so an
# unrounded coordinate makes the same artifact render to different bytes on
# macOS and on Linux. Nine places sit far below what a pixel resolves and far
# above the error being absorbed.
POSITION_PRECISION = 9


def split_rerun_note(
    corrections: Sequence[Mapping[str, object]], usage_by_treatment: Mapping[str, object]
) -> dict[str, str]:
    replaced = sum(int(item["replaced_cells"]) for item in corrections)
    retained = sum(int(item["retained_cells"]) for item in corrections)
    pacing = next(
        (item["gemini_input_pacing"] for item in corrections if item.get("gemini_input_pacing")),
        None,
    )
    note = {
        "en": (
            f"Data correction: {replaced} Split cells were rerun and replaced; {retained} "
            "Raw/Graph cells and all micro results were retained. The rerun corrected Tempo "
            "retention and repeated label names in query results. Split max_items now uses "
            "the original SQL max_rows guidance, with items as the returned unit; no aggregation "
            "advice was added to the main tool descriptions. Raw/Graph query_metrics parameter "
            "descriptions remain unchanged. Trace visibility and sample fidelity "
            "passed before and after every replacement investigation. Per-cell gates and "
            "superseded-result hashes are in the JSON. Raw/Graph retain their original PromQL "
            "encoding and ran at different times; this comparison does not isolate provider "
            "time effects or the individual corrections."
        ),
        "zh": (
            f"数据修正：重新执行并替换 {replaced} 个 Split 单元，保留 {retained} 个 Raw/Graph "
            "单元及全部 micro 结果。重跑修正了 Tempo 保留时间和查询结果中重复的标签名称。"
            "Split max_items 沿用原 SQL max_rows 的参数建议，返回单位改为 items；"
            "工具主描述未追加聚合建议。Raw/Graph 的 query_metrics 参数描述保持原样。"
            "每次替换调查前后均通过 trace 可见性和样本完整性检查，"
            "逐单元检查记录和被取代结果的哈希保存在 JSON 中。Raw/Graph 保留原始 PromQL 编码，"
            "运行时间也不同；本次比较没有单独检验 provider 随时间变化或各项修正的影响。"
        ),
    }

    audits = [item for item in corrections if item.get("retry_audit") is not None]
    if audits:
        attempts = [
            attempt
            for correction in audits
            for attempt in correction["retry_audit"]["failed_attempts"]
            if attempt["cohort"] == correction["cohort"]
        ]
        affected = len({(item["cohort"], item["cell_index"]) for item in attempts})
        costs: dict[str, list[float]] = defaultdict(list)
        for attempt in attempts:
            usage = attempt["run"]["usage"]
            if usage.get("estimated_cost") is None:
                raise ValueError("Split retry cost is not estimable")
            costs[str(usage["cost_currency"])].append(float(usage["estimated_cost"]))
        extra_cost = " + ".join(
            f"{currency} {math.fsum(values):.4f}" for currency, values in sorted(costs.items())
        )
        note["en"] += (
            " A subsequent user authorization allowed temporary connection/provider failures "
            "to be retried at most three times per cell (four attempts total), with every failed "
            "attempt preserved. Incorrect diagnoses and exhausted budgets were not retried."
        )
        note["zh"] += (
            "后续用户授权允许对临时连接或 provider 错误每个单元最多重跑 3 次"
            "（总尝试最多 4 次），并保留每次失败记录；错误诊断和预算耗尽不重跑。"
        )
        if attempts:
            failures = (
                "SDK connection errors"
                if all(item["failure_detail"] == "sdk_connection_error" for item in attempts)
                else "temporary connection/provider errors"
            )
            failures_zh = (
                "SDK 连接错误"
                if all(item["failure_detail"] == "sdk_connection_error" for item in attempts)
                else "临时连接或 provider 错误"
            )
            note["en"] += (
                f" {len(attempts)} failed "
                f"{'attempt was' if len(attempts) == 1 else 'attempts were'} "
                f"retried across {affected} {'cell' if affected == 1 else 'cells'} after "
                f"{failures}. Their "
                f"additional observed cost was {extra_cost}; final replacement cells are already "
                "included in the investigation costs below. Requests without returned usage may "
                "have unobserved billed cost. The JSON retains the authorization, supervisor "
                "amendments, sanitized attempts and provenance hashes."
            )
            note["zh"] += (
                f"{affected} 个单元共发生 {len(attempts)} 次失败，已按授权重跑，"
                f"原因是 {failures_zh}，额外观测费用为 {extra_cost}；"
                "最终替换单元的正常费用已计入下方调查成本。"
                "未返回用量的请求可能另有未观测到账单费用。JSON 保留授权、执行修订、"
                "脱敏失败尝试及来源哈希。"
            )
        retained = [
            failure
            for correction in audits
            for failure in correction["retry_audit"]["retained_failures"]
            if failure["cohort"] == correction["cohort"]
        ]
        output_limited = [
            item for item in retained if item["failure_class"] == "output_token_limit"
        ]
        if output_limited:
            labels = "; ".join(
                f"{item['model']} / {item['case_id']} / rep{item['repetition']}"
                for item in output_limited
            )
            note["en"] += (
                f" Output-limited failures were retained without retry ({labels}); "
                "they remain scored failures and runner errors under the frozen runner contract."
            )
            note["zh"] += (
                f"输出受限的失败原样保留，未重跑（{labels}）；"
                "按冻结的运行器契约计为失败，并保留 runner error 标记。"
            )
    else:
        note["en"] += (
            " Runner failures pause new dispatch for review and remain scored failures; "
            "completed cells are not automatically retried."
        )
        note["zh"] += (
            "runner 失败会暂停新增调查以供复核，失败结果保留计分，已完成单元不会自动重试。"
        )

    encoding_estimate = next(
        (
            item["retained_encoding_estimate"]
            for item in corrections
            if item.get("retained_encoding_estimate")
        ),
        None,
    )
    if encoding_estimate and set(encoding_estimate["cohorts"]) == {
        item["cohort"] for item in corrections
    }:
        raw = float(encoding_estimate["by_treatment"]["raw"]["estimated_share"]) * 100
        graph = float(encoding_estimate["by_treatment"]["semantic_graph"]["estimated_share"]) * 100
        usage = mapping(usage_by_treatment, "by_treatment")
        gap = int(mapping(usage, "split_pillars")["provider_visible_input_tokens"]) - int(
            mapping(usage, "raw")["provider_visible_input_tokens"]
        )
        saving = float(encoding_estimate["by_treatment"]["raw"]["estimated_reencoding_saving"])
        note["en"] += (
            " With queries and trajectories held fixed, offline reencoding with o200k_base "
            f"and model-specific calibration estimates repeated-label overhead at {raw:.2f}% "
            f"of retained Raw input and {graph:.2f}% of retained Graph input. These are estimates, "
            "not new provider usage measurements or effects on diagnosis accuracy."
        )
        note["zh"] += (
            "固定查询和轨迹，以 o200k_base 分词并按模型系数校准，重复标签名称的输入开销"
            f"估算为保留 Raw 输入的 {raw:.2f}%、Graph 输入的 {graph:.2f}%。"
            "这些是离线估算，不是 provider 新用量测量，也不表示对诊断准确率的影响。"
        )
        if gap > 0 and saving > 0:
            increase = saving / gap * 100
            note["en"] += (
                " The retained overhead makes GreptimeDB appear more token-intensive; "
                f"removing it widens the aggregate Split–Raw input gap by about {increase:.1f}%."
            )
            note["zh"] += (
                "保留该冗余使 GreptimeDB 显得更费 token，对 GreptimeDB 不利；"
                f"扣除后，Split 与 Raw 的总输入差距扩大约 {increase:.1f}%。"
            )

    if pacing:
        budget = int(pacing["tokens_per_minute"])
        note["en"] += (
            f" Gemini requests shared a local {budget:,}-input-token/minute budget, including "
            "SDK retries. Input is estimated before sending and corrected from actual usage; "
            "elapsed time includes quota waits."
        )
        note["zh"] += (
            f"Gemini 请求（含 SDK 重试）共用每分钟 {budget:,} 输入 token 的本地额度，"
            "发送前估算、响应后按实际用量校正；总耗时包含等待额度的时间。"
        )
    return note


def build_report_view_model(
    report: Mapping[str, object], *, report_json_filename: str | None = None
) -> dict[str, object]:
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
        **(
            {
                "split_rerun_note": split_rerun_note(
                    mapping_list(report, "split_reruns"), mapping(report, "usage_by_treatment")
                )
            }
            if report.get("split_reruns")
            else {}
        ),
        "languages": list(LANGUAGES),
        "report_json_filename": report_json_filename
        or f"agent-rca-v{scope['benchmark_protocol_version']}.json",
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
            "publication": (
                dict(mapping(report, "publication")) if report.get("publication") else None
            ),
        },
        "verdicts": verdicts,
        "execution_deviation_note": {
            language: (
                f"{len(report['execution_deviations'])} 次运行的 provider 并发上限超过冻结值；"
                "逐次记录见 JSON。测量未单独检验并发调整的影响。"
                if language == "zh"
                else f"{len(report['execution_deviations'])} runs used a provider concurrency "
                "limit above the frozen value; per-run records are in the JSON. "
                "The measurement does not isolate the effect of this change."
            )
            for language in LANGUAGES
        }
        if report.get("execution_deviations")
        else None,
        "inference_groups": [
            {
                "protocol_revision": group["protocol_revision"],
                "models": group["models"],
                "family_size": mapping(group, "inference")["holm_family_size"],
            }
            for group in (
                mapping_list(report, "inference_cohorts")
                if report.get("inference_cohorts")
                else [
                    {
                        "protocol_revision": scope["protocol_revision"],
                        "models": models,
                        "inference": scope["inference"],
                    }
                ]
            )
        ],
        "takeaways": _takeaways(report),
        "interface_matrix": _interface_matrix(report),
        # "CPU saturation", not whatever title-casing the code point order gives.
        "mechanism_labels": {
            str(code): {language: mechanism_label(code, language) for language in LANGUAGES}
            for code, _ in mechanism_cohort(mapping_list(report, "case_catalog"))
        },
        # The table showed the raw keys "discovery" and "graph"; the reader sees
        # the task names the section uses.
        "benchmark_labels": {key: dict(value) for key, value in MICRO_BENCHMARK_LABELS.items()},
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
            "relative_change": _relative_change(report),
            "diagnosis_slope": _diagnosis_slope(report),
            "component_dependency_slope": _diagnosis_slope(
                report, ("component", "dependency_edge")
            ),
            "headline": _headline_bars(report),
            "hero_ledger": _hero_ledger(report),
            "accuracy_by_level": _accuracy_by_level(report),
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
    question_order = {"storage_shape": 0, "semantic_layer": 1, "model_ranking": 2}
    for question in sorted(
        mapping_list(report, "research_questions"), key=lambda item: question_order[item["goal"]]
    ):
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
        family_sizes = (
            [
                int(mapping(group, "inference")["holm_family_size"])
                for group in mapping_list(report, "inference_cohorts")
            ]
            if report.get("inference_cohorts")
            else [_family_size(results)]
        )
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
                    "family_sizes": family_sizes,
                },
                "tally_text": {
                    language: _tally_text(tally, language)
                    + (
                        (
                            "；按各模型组冻结的范围分别校正："
                            if language == "zh"
                            else "; corrected within separately frozen model cohorts: "
                        )
                        + ", ".join(f"m = {size}" for size in family_sizes)
                        + (
                            f"，未合并为 {tally['total']} 项统一校正"
                            if language == "zh"
                            else f"; not pooled across all {tally['total']} endpoints"
                        )
                        if len(family_sizes) > 1
                        else ""
                    )
                    for language in LANGUAGES
                },
            }
        )
    return verdicts


def _tally_text(tally: Mapping[str, int], language: str) -> str:
    """How many of a family's registered endpoints were significant."""
    total, confirmed = tally["total"], tally["confirmed"]
    if language == "zh":
        if not confirmed:
            return f"{total} 项预先指定端点均未通过 Holm 校正"
        return f"{total} 项预先指定端点中 {confirmed} 项通过 Holm 校正"
    if not confirmed:
        return f"No pre-specified endpoint of {total} survived Holm correction"
    return f"{confirmed} of {total} pre-specified endpoints survived Holm correction"


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

    One per research question, each carrying the counts it was derived from so
    the claim and its evidence cannot drift apart. The fault-level split is not
    among them: it is a post-measurement breakdown of the second question and is
    stated in that section, beside the chart that shows both sides of it.
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
    scoped = _scope_contrast(report)
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
            # The claim above the badge is a diagnosis-accuracy breakdown, which
            # no endpoint in this family tests. Grading it `not_confirmed` from
            # the efficiency tally would attach a verdict to a sentence the tally
            # never ran on. The tally itself is in the caveats and on the board.
            "goal": "semantic_layer",
            "grade": "descriptive",
            "evidence": {
                **semantic,
                # The three levels hold different numbers of runs, so a count
                # without its denominator is not comparable across them.
                "by_fault_level": {
                    scope: {
                        "cases": int(mapping(scopes, scope)["cases"]),
                        "runs": {
                            treatment: int(value)
                            for treatment, value in mapping(mapping(scopes, scope), "runs").items()
                        },
                        **{
                            treatment: int(value)
                            for treatment, value in mapping(
                                mapping(scopes, scope), "diagnosis_correct"
                            ).items()
                        },
                    }
                    for scope in scopes
                },
                **({"service_and_dependency": scoped} if scoped else {}),
            },
        },
        {
            "id": "cross_model",
            "goal": "model_ranking",
            "grade": "descriptive",
            "evidence": {
                "models": len(mapping_list(diagnosis, "series")),
                "input_tokens": _reduction_counts(
                    _relative_rows(report, "storage_shape", "provider_visible_input_tokens")
                ),
                "tool_calls": _reduction_counts(
                    _relative_rows(report, "storage_shape", "correct_completion_tool_calls")
                ),
                "diagnosis": totals,
                "diagnosis_runs": total_runs,
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
        for metric_name, effect in sorted(metrics.items()):
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
        "hero_title": _hero_title(report, language),
        "hero_lede": _hero_lede(report, language),
        "hero_result": [
            [{"kind": str(part["kind"]), "value": str(part[language])} for part in clause]
            for clause in _hero_result(report)
        ],
        "family_summaries": {
            family: _family_summary(report, family, language) for family in FAMILY_COMPARISONS
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


# Chinese typography puts a space between a Latin run and the Han characters
# around it, and none before Chinese punctuation. The arm and metric names that
# land in these sentences are sometimes Latin ("GreptimeDB") and sometimes not
# ("三个后端"), so the spacing follows the characters that actually meet rather
# than a per-name special case.
_ZH_PUNCTUATION = "。，、；：？！（）《》“”‘’…—％"


def _zh_join(*parts: str) -> str:
    text = ""
    for part in parts:
        if not part:
            continue
        if text:
            left, right = text[-1], part[0]
            latin_left = left.isascii() and left.isalnum()
            latin_right = right.isascii() and right.isalnum()
            han = right if latin_left else left
            if latin_left != latin_right and not left.isspace() and han not in _ZH_PUNCTUATION:
                text += " "
        text += part
    return text


def _significant_text(result: Mapping[str, object], language: str, *, detailed: bool = True) -> str:
    """The effect first, then the test that constrains what may be concluded from it.

    Leading with "the only endpoint that passed correction" makes the threshold
    the subject of the sentence. The reader needs the size of the effect and how
    consistently the cases pointed that way before the p values, which say how
    much of that pattern survives the multiplicity the protocol registered.
    """
    effect = mapping(result, "effect")
    parts = str(result["comparison"]).split(" - ")
    if len(parts) != 2:
        raise ValueError("confirmatory family comparison is malformed")
    delta = effect.get("case_median_delta")
    if not isinstance(delta, (int, float)):
        raise ValueError("significant primary result has no case median delta")
    left, right = (ARM_NAMES[part][language] if part in ARM_NAMES else part for part in parts)
    favored, other = (left, right) if delta < 0 else (right, left)
    # The cases that moved the way the median points, not the total.
    agreeing = int(effect["negative_cases"] if delta < 0 else effect["positive_cases"])
    eligible = int(effect["eligible_cases"])
    metric = _metric_label(result["metric"], language)
    delta_text = f"{float(delta):+,.12g}"
    if not detailed:
        metric = {
            "provider_visible_input_tokens": ("input tokens", "输入 token"),
            "correct_completion_tool_calls": ("tool calls", "工具调用"),
            "rows_returned": ("returned rows", "返回行数"),
        }[str(result["metric"])][language == "zh"]
        if language == "zh":
            return _zh_join(
                f"{result['model']}：",
                favored,
                "的",
                metric,
                f"更少（{agreeing}/{eligible} 个合格 case，中位差 {delta_text}）。",
            )
        return (
            f"{result['model']}: {favored} used fewer {metric} "
            f"({agreeing}/{eligible} eligible cases; median delta {delta_text})."
        )
    holm = f"{float(effect['holm_adjusted_p']):.8g}"
    exact = f"{float(effect['sign_test_two_sided_p']):.8g}"
    if language == "zh":
        return _zh_join(
            f"{result['model']}：{eligible} 个合格 case 中，{agreeing} 个的",
            favored,
            metric,
            "少于",
            other,
            f"，case 差值中位数为 {delta_text}。"
            f"精确符号检验 p={exact}；通过本检验族的 Holm 校正，校正后 p={holm}。",
        )
    return (
        f"{result['model']}: {favored} used fewer {metric} than {other} in "
        f"{agreeing} of {eligible} eligible cases, with a median case delta of {delta_text}. "
        f"Exact sign p {exact}; it passes Holm correction within its family at "
        f"adjusted p {holm}."
    )


def _family_summary(report: Mapping[str, object], family: str, language: str) -> list[str]:
    significant = [item for item in _family_results(report, family) if item["significant"]]
    if significant:
        return [_significant_text(item, language, detailed=False) for item in significant]
    return [
        "没有效率指标通过 Holm 校正；样本量有限，不代表两种接口等价。"
        if language == "zh"
        else "No efficiency endpoint passed Holm correction. "
        "The small sample does not establish equivalence."
    ]


def _conclusion(report: Mapping[str, object], language: str) -> str:
    """The result first, then the tests that constrain what may be concluded from it."""
    semantic = [item for item in _family_results(report, "semantic_layer") if item["significant"]]
    storage = [item for item in _family_results(report, "storage_shape") if item["significant"]]
    micro = _micro_row_reduction(report, language)
    scoped = _scope_contrast(report)
    facts = _contrast_facts(report)
    correct = mapping(facts, "correct")
    if language == "zh":
        effect = (
            f"同一批故障下，GreptimeDB 诊断正确 {correct['raw']}/{facts['runs']}，"
            f"三后端 {correct['split_pillars']}/{facts['runs']}；"
            f"读入的 token 为 {facts['tokens']['split_pillars']:,} 对 "
            f"{facts['tokens']['raw']:,}。"
        )
        if scoped:
            effect += (
                f"服务与依赖层故障上，语义层正确 {scoped['correct']['semantic_graph']}/"
                f"{scoped['runs']}，高于裸接口的 {scoped['correct']['raw']}/{scoped['runs']}；"
            )
        effect += f"聚焦检索中语义层减少返回行数的合格结果为 {micro}。"
        semantic_text = (
            "语义层检验族没有端到端主要指标通过 Holm 校正"
            if not semantic
            else f"语义层检验族有 {len(semantic)} 项端到端主要指标通过 Holm 校正"
        )
        storage_text = (
            "接口组合检验族没有主要指标通过 Holm 校正。"
            if not storage
            else "接口组合检验族中通过校正的结果——"
            + "".join(_significant_text(item, language) for item in storage)
        )
        return f"{effect}{semantic_text}。{storage_text}"
    effect = (
        f"On the same incidents, GreptimeDB reached a correct diagnosis in "
        f"{correct['raw']}/{facts['runs']} runs against "
        f"{correct['split_pillars']}/{facts['runs']} on three backends, "
        f"reading {facts['tokens']['split_pillars']:,} input tokens against "
        f"{facts['tokens']['raw']:,}. "
    )
    if scoped:
        effect += (
            f"On service and dependency faults the Semantic Graph was correct in "
            f"{scoped['correct']['semantic_graph']}/{scoped['runs']} runs against "
            f"{scoped['correct']['raw']}/{scoped['runs']} without it. "
        )
    effect += f"Eligible focused-retrieval results where the layer read back fewer rows: {micro}. "
    semantic_text = (
        "No end-to-end primary endpoint in the semantic-layer family passes Holm correction"
        if not semantic
        else f"{len(semantic)} end-to-end primary endpoints in the semantic-layer family "
        "pass Holm correction"
    )
    storage_text = (
        "No primary endpoint in the interface-bundle family passes Holm correction."
        if not storage
        else "Interface-bundle endpoints that pass Holm correction: "
        + " ".join(_significant_text(item, language) for item in storage)
    )
    return f"{effect}{semantic_text}. {storage_text}"


def _contrast_facts(report: Mapping[str, object]) -> dict[str, object]:
    """The arm-level contrast the page leads with, derived once.

    The hero line, the findings and the no-JavaScript summary all state this
    same comparison. Deriving it separately in each let one of them keep a
    number that a rerun had already moved.
    """
    rows = {str(row["id"]): row for row in mapping_list(_headline_bars(report), "rows")}
    accuracy = rows["accuracy"]
    runs = int(accuracy["denominator"])
    correct = {key: int(value) for key, value in mapping(accuracy, "values").items()}
    wrong = {key: runs - value for key, value in correct.items()}
    tokens = {key: int(value) for key, value in mapping(rows["input_tokens"], "values").items()}
    cost = rows["cost"]
    cost_label = (cost.get("bounds_ratio_labels") or {}).get("split_pillars")
    if cost_label is None:
        ratio = (cost.get("ratios") or {}).get("split_pillars")
        cost_label = f"×{float(ratio):.2f}" if isinstance(ratio, (int, float)) else None
    return {
        "runs": runs,
        "correct": correct,
        "wrong": wrong,
        "tokens": tokens,
        # A share of the baseline's misses, not of its runs: "40% fewer wrong"
        # is the quantity a reader checks against 63 and 38, not against 168.
        "wrong_reduction": (
            (wrong["split_pillars"] - wrong["raw"]) / wrong["split_pillars"]
            if wrong["split_pillars"] > 0
            else None
        ),
        "token_reduction": (
            (tokens["split_pillars"] - tokens["raw"]) / tokens["split_pillars"]
            if tokens["split_pillars"] > 0
            else None
        ),
        "cost_ratio_label": cost_label,
    }


def _percent(value: float) -> str:
    return f"{round(value * 100):g}%"


def _gain(facts: Mapping[str, object], key: str) -> float | None:
    """A reduction only when one was measured, so no headline can invent one."""
    value = facts.get(key)
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _hero_lead_metric(facts: Mapping[str, object]) -> str | None:
    """Which reduction the title states, so the stat band does not repeat it.

    Accuracy leads when there is one: it is the outcome a reader came for, and
    the resource figures mean less before they know whether the answer was right.
    """
    if _gain(facts, "wrong_reduction") is not None:
        return "accuracy"
    return "tokens" if _gain(facts, "token_reduction") is not None else None


def _hero_title(report: Mapping[str, object], language: str) -> str:
    """The claim the page makes, without the figures the result line states.

    The three stores are named rather than counted: "three backends" says
    nothing about what is being measured, while the three product names tell a
    reader the subject is observability before the lede gets a chance to.

    One clause, so it sets in two display lines. An earlier title carried both
    reductions and ran four lines while repeating the band below it.
    """
    if not _hero_result(report):
        return (
            "换掉 agent 背后的可观测性接口，根因分析的结果会变"
            if language == "zh"
            else "The observability interface behind the agent changes the root cause analysis"
        )
    if language == "zh":
        return "一个数据库取代 Prometheus、Loki 和 Tempo"
    return "One database instead of Prometheus, Loki and Tempo."


def _page_title(report: Mapping[str, object], language: str = "en") -> str:
    """The claim, then the benchmark, because a result is what a share card shows."""
    return f"{_hero_title(report, language).rstrip('.')} — Agent RCA Bench"


def _page_description(report: Mapping[str, object], language: str = "en") -> str:
    """The measured result and the scale it was measured at, in one sentence.

    Derived rather than written into the template: a description that outlives
    its numbers is what a stale share card is.
    """
    execution = mapping(report, "execution")
    cases = int(execution["transfer_cases"])
    models = len(_sequence(report, "model_order"))
    runs = (
        int(execution["repetitions_per_model_case"])
        * cases
        * len(_sequence(execution, "treatments"))
        * models
    )
    effect = _join(
        [" ".join(part[language] for part in clause) for clause in _hero_result(report)],
        language,
    )
    if language == "zh":
        opening = f"GreptimeDB 对比 Prometheus、Loki 和 Tempo：{effect}。" if effect else ""
        return (
            f"{opening}{models} 个模型、{cases} 个故障、{runs} 次端到端根因分析调查，"
            "同一套 prompt 与遥测数据，只改变 agent 背后的可观测性接口。"
        )
    opening = f"GreptimeDB against Prometheus, Loki and Tempo: {effect}. " if effect else ""
    return (
        f"{opening}{models} models, {cases} incidents and {runs} end-to-end root cause "
        "investigations on the same prompt and the same telemetry, changing only the "
        "observability interface behind the agent."
    )


def _hero_lede(report: Mapping[str, object], language: str) -> str:
    execution = mapping(report, "execution")
    treatments = len(_sequence(execution, "treatments"))
    cases = int(execution["transfer_cases"])
    models = len(_sequence(report, "model_order"))
    runs = int(execution["repetitions_per_model_case"]) * cases * treatments * models
    if language == "zh":
        return (
            f"{models} 个模型、{cases} 个故障、{runs} 次端到端调查。"
            "同一套 prompt、同一份遥测数据、同一个工具预算，"
            "唯一改变的是 agent 背后的可观测性接口。"
        )
    return (
        f"{models} models, {cases} incidents, {runs} end-to-end investigations. "
        "Same prompt, same telemetry, same tool budget. The observability interface "
        "behind the agent is the only variable."
    )


def _hero_ledger(report: Mapping[str, object]) -> dict[str, object] | None:
    """The first screen's comparison, as measured gaps rather than lone figures.

    Every row is the same two arms on the same runs, so a headline figure cannot
    appear without the value it is measured against. A row is included only while
    GreptimeDB is the lower side of it: the condition is on the direction the
    data took, never on which arm it favours, so a rerun that reverses one drops
    the row instead of restating it.
    """
    facts = _contrast_facts(report)
    left, right = "raw", "split_pillars"
    rows: list[dict[str, object]] = []

    misses = mapping(facts, "wrong")
    wrong = _gain(facts, "wrong_reduction")
    if wrong is not None:
        rows.append(
            {
                "id": "accuracy",
                "label": {
                    "en": f"wrong diagnoses, of {facts['runs']} runs",
                    "zh": f"错误诊断，共 {facts['runs']} 次运行",
                },
                "left_text": f"{misses[left]:,}",
                "right_text": f"{misses[right]:,}",
                "left_fill": round(misses[left] / misses[right], POSITION_PRECISION),
                "right_fill": 1.0,
                "delta": {"en": f"−{_percent(wrong)}", "zh": f"−{_percent(wrong)}"},
            }
        )

    read = mapping(facts, "tokens")
    tokens = _gain(facts, "token_reduction")
    if tokens is not None:
        rows.append(
            {
                "id": "input_tokens",
                "label": {"en": "input tokens read", "zh": "读入的 token"},
                "left_text": f"{read[left]:,}",
                "right_text": f"{read[right]:,}",
                "left_fill": round(read[left] / read[right], POSITION_PRECISION),
                "right_fill": 1.0,
                "delta": {"en": f"−{_percent(tokens)}", "zh": f"−{_percent(tokens)}"},
            }
        )

    cost = _cost_ledger_row(report, left, right)
    if cost:
        rows.append(cost)
    if not rows:
        return None
    return {
        "left": left,
        "right": right,
        "left_name": dict(ARM_NAMES[left]),
        "right_name": dict(ARM_NAMES[right]),
        "rows": rows,
    }


def _cost_arms(report: Mapping[str, object], left: str, right: str) -> dict[str, object] | None:
    """Both arms' estimated spend, as an interval when the estimate carries one.

    Returns None when either arm is unpriced, or when the two intervals overlap:
    a reduction cannot be stated from estimates that do not separate.
    """
    row = next(
        item for item in mapping_list(_headline_bars(report), "rows") if item["id"] == "cost"
    )
    bounds, values = row.get("bounds"), mapping(row, "values")
    if isinstance(bounds, Mapping):
        left_low, left_high = (float(value) for value in bounds[left])
        right_low, right_high = (float(value) for value in bounds[right])
        left_text = f"{left_low:,.2f}–{left_high:,.2f}"
        right_text = f"{right_low:,.2f}–{right_high:,.2f}"
    elif isinstance(values.get(left), (int, float)) and isinstance(values.get(right), (int, float)):
        left_low = left_high = float(values[left])
        right_low = right_high = float(values[right])
        left_text = f"{left_low:,.2f}"
        right_text = f"{right_low:,.2f}"
    else:
        return None
    if not right_low or left_high >= right_low:
        return None
    return {
        "currency": str(row.get("currency") or ""),
        "left_text": left_text,
        "right_text": right_text,
        "fill": round(left_high / right_high, POSITION_PRECISION),
        # The smallest and largest reductions the two intervals allow. The
        # smallest is the one a headline may state: it is true at either end.
        "least": 1.0 - left_high / right_low,
        "most": 1.0 - left_low / right_high,
    }


def _cost_ledger_row(
    report: Mapping[str, object], left: str, right: str
) -> dict[str, object] | None:
    """Spend as a pair of bars, carrying the estimate's interval into both ends."""
    arms = _cost_arms(report, left, right)
    if arms is None:
        return None
    least, most = float(arms["least"]), float(arms["most"])
    delta = (
        f"−{_percent(least)}"
        if round(least * 100) == round(most * 100)
        else f"−{round(least * 100):g}–{_percent(most)}"
    )
    currency = str(arms["currency"])
    return {
        "id": "cost",
        # The unit rides with the label, not with each end: repeating it in both
        # value cells pushed the bars out of a shared column and made three rows
        # in three different units look like one scale.
        "label": {
            "en": f"estimated cost to run, {currency}".rstrip(", "),
            "zh": f"估算运行成本（{currency}）" if currency else "估算运行成本",
        },
        "left_text": arms["left_text"],
        "right_text": arms["right_text"],
        "left_fill": arms["fill"],
        "right_fill": 1.0,
        "delta": {"en": delta, "zh": delta},
    }


def _hero_result(report: Mapping[str, object]) -> list[list[dict[str, str]]]:
    """The reductions the title line marks, as clauses the renderer only lays out.

    Two at most, and accuracy leads: the resource figures mean less before a
    reader knows whether the answer was right. A clause exists only while the
    measurement points that way, so a rerun that reverses one drops it rather
    than restating it. Cost states the smaller bound of its interval, which is
    the reduction that holds at either end of the estimate.
    """
    facts = _contrast_facts(report)
    clauses: list[list[dict[str, str]]] = []
    wrong = _gain(facts, "wrong_reduction")
    if wrong is not None:
        clauses.append(
            [
                {"kind": "mark", "en": _percent(wrong), "zh": _percent(wrong)},
                {"kind": "text", "en": "fewer wrong diagnoses", "zh": "更少的错误诊断"},
            ]
        )
    cost = _cost_arms(report, "raw", "split_pillars")
    if cost is not None:
        share = _percent(float(cost["least"]))
        clauses.append(
            [
                {"kind": "mark", "en": share, "zh": share},
                {"kind": "text", "en": "lower cost to run", "zh": "更低的运行成本"},
            ]
        )
    tokens = _gain(facts, "token_reduction")
    if tokens is not None and len(clauses) < 2:
        share = _percent(tokens)
        clauses.append(
            [
                {"kind": "mark", "en": share, "zh": share},
                {"kind": "text", "en": "fewer tokens read", "zh": "更少的读入 token"},
            ]
        )
    return clauses


def _headline(report: Mapping[str, object], language: str) -> str:
    """The one-line result, stated as the effect rather than as a test outcome."""
    facts = _contrast_facts(report)
    wrong, tokens = _gain(facts, "wrong_reduction"), _gain(facts, "token_reduction")
    if wrong is None and tokens is None:
        return (
            "聚焦检索用量下降，端到端结果随场景变化"
            if language == "zh"
            else "Focused retrieval improves; E2E varies"
        )
    correct = mapping(facts, "correct")
    parts = []
    if wrong is not None:
        parts.append(
            f"GreptimeDB 诊断正确 {correct['raw']}/{facts['runs']}，三后端 "
            f"{correct['split_pillars']}/{facts['runs']}"
            if language == "zh"
            else f"GreptimeDB got {correct['raw']}/{facts['runs']} right against "
            f"{correct['split_pillars']}/{facts['runs']} on three backends"
        )
    if tokens is not None:
        parts.append(
            f"读入 token 减少 {_percent(tokens)}"
            if language == "zh"
            else f"reading {_percent(tokens)} fewer tokens"
        )
    return "，".join(parts) if language == "zh" else ", ".join(parts)


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
    on its own it reads as a stray label. The sentence stops at the measured
    result: the section heading and the interpretation beside it already state
    that the reduction did not reach the end-to-end totals.
    """
    parts = _micro_row_reduction(report, language)
    if language == "zh":
        return f"聚焦检索中语义层读回更少行的合格结果：{parts}。"
    return f"Focused retrieval, eligible results where the layer read back fewer rows: {parts}."


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
            f"每种机制下，返回行数下降的模型数：{_join(parts, language)}。"
            f"按 case 统计为 {case_better}/{case_total} 个可估算的模型-case 组合。"
            "表中的负数表示启用 GreptimeDB 语义层后返回的行数更少。"
        )
    return (
        f"Models that read back fewer rows with the GreptimeDB Semantic Graph, by mechanism: "
        f"{_join(parts, language)}. Case by case that is "
        f"{case_better} of {case_total} estimable model-case combinations. "
        "A negative number in the table means fewer rows with the layer."
    )


def _takeaway_text(report: Mapping[str, object], language: str) -> dict[str, dict[str, object]]:
    """Each finding as an effect sentence, its counts, and the limits on reading it.

    `support` carries what was measured and `caveats` what constrains it. They are
    separate fields because the page gives them different weight, not different
    standing: every caveat is on the page, in the finding it belongs to.
    """
    scoped = _scope_contrast(report)
    node = _scope_contrast(report, ("infrastructure_node",))
    facts = _contrast_facts(report)
    correct = mapping(facts, "correct")
    runs = facts["runs"]
    tokens = _reduction_counts(
        _relative_rows(report, "storage_shape", "provider_visible_input_tokens")
    )
    steps = _reduction_counts(
        _relative_rows(report, "storage_shape", "correct_completion_tool_calls")
    )
    accuracy_gain = sum(
        int(mapping(item, "values")["raw"]) > int(mapping(item, "values")["split_pillars"])
        for item in mapping_list(_diagnosis_slope(report), "series")
    )
    models = len(_sequence(report, "model_order"))
    cost = _cost_direction_text(report, language)["storage_shape"]
    zh = language == "zh"

    one_store_support = [
        f"诊断正确 {correct['raw']}/{runs}，三后端 {correct['split_pillars']}/{runs}。"
        if zh
        else f"Correct diagnoses: {correct['raw']}/{runs} against "
        f"{correct['split_pillars']}/{runs} on three backends.",
        f"读入的 token 合计 {facts['tokens']['raw']:,}，三后端 "
        f"{facts['tokens']['split_pillars']:,}。"
        if zh
        else f"Input tokens read: {facts['tokens']['raw']:,} against "
        f"{facts['tokens']['split_pillars']:,}.",
    ]
    if tokens["reduced"]:
        one_store_support.append(
            f"{tokens['reduced']}/{tokens['models']} 个模型在 GreptimeDB 上读入更少 token，"
            f"case 中位降幅 {tokens['smallest']:.0f}%–{tokens['largest']:.0f}%。"
            if zh
            else f"{tokens['reduced']} of {tokens['models']} models read fewer tokens on "
            f"GreptimeDB; the case medians fall between {tokens['smallest']:.0f}% and "
            f"{tokens['largest']:.0f}%."
        )
    one_store_support.append(cost)
    # A surviving endpoint is the strongest evidence the family produced, so it
    # belongs beside the effect rather than among the limits on reading it. The
    # per-endpoint deltas stay in the caveats and the endpoint table: four
    # near-identical sentences buried the three lines above them.
    passed = [item for item in _family_results(report, "storage_shape") if item["significant"]]
    if passed:
        names = _join([str(item["model"]) for item in passed], language)
        one_store_support.append(
            f"{len(passed)} 项预先指定端点通过 Holm 校正，方向全部指向 GreptimeDB（{names}）。"
            if zh
            else f"{len(passed)} pre-specified endpoints passed Holm correction, all of them "
            f"favouring GreptimeDB ({names})."
        )

    semantic_support = []
    if scoped:
        semantic_support.append(
            f"服务与依赖层故障：语义层 {scoped['correct']['semantic_graph']}/{scoped['runs']}，"
            f"裸接口 {scoped['correct']['raw']}/{scoped['runs']}，"
            f"三后端 {scoped['correct']['split_pillars']}/{scoped['runs']}。"
            if zh
            else f"Service and dependency faults: "
            f"{scoped['correct']['semantic_graph']}/{scoped['runs']} with the layer, "
            f"{scoped['correct']['raw']}/{scoped['runs']} without it, "
            f"{scoped['correct']['split_pillars']}/{scoped['runs']} on three backends."
        )
        if not scoped["worse"]:
            semantic_support.append(
                f"这些故障上，启用语义层没有使任何模型的正确诊断数下降，"
                f"{scoped['better']}/{scoped['models']} 个模型上升。"
                if zh
                else "No model returned fewer correct diagnoses with the layer on these "
                f"faults; {scoped['better']} of {scoped['models']} returned more."
            )
    semantic_support.append(_micro_summary_text(report, language))

    semantic_caveats = list(_family_summary(report, "semantic_layer", language))
    if node:
        semantic_caveats.append(
            f"基础设施节点故障：语义层 {node['correct']['semantic_graph']}/{node['runs']}，"
            f"低于裸接口的 {node['correct']['raw']}/{node['runs']}。"
            if zh
            else f"Infrastructure-node faults: "
            f"{node['correct']['semantic_graph']}/{node['runs']} with the layer against "
            f"{node['correct']['raw']}/{node['runs']} without it."
        )
    semantic_caveats.extend(_dataset_reversal_text(report, language))

    cross_model_support = [
        f"{tokens['reduced']}/{tokens['models']} 个模型在 GreptimeDB 上读入更少 token。"
        if zh
        else f"{tokens['reduced']} of {tokens['models']} models read fewer tokens on GreptimeDB.",
        f"{accuracy_gain}/{models} 个模型的诊断正确数更高。"
        if zh
        else f"{accuracy_gain} of {models} models produced more correct diagnoses.",
        f"{steps['reduced']}/{steps['models']} 个模型达成正确诊断所用的工具调用更少。"
        if zh
        else f"{steps['reduced']} of {steps['models']} models reached a correct diagnosis in "
        "fewer tool calls.",
    ]

    return {
        "one_store": {
            "headline": _one_store_headline(report, language),
            "support": one_store_support,
            "caveats": _family_caveats(report, "storage_shape", language),
        },
        "semantic_layer": {
            "headline": _semantic_headline(scoped, language),
            "support": semantic_support,
            "caveats": semantic_caveats,
        },
        "cross_model": {
            "headline": _cross_model_headline(tokens, accuracy_gain, models, language),
            "support": cross_model_support,
            "caveats": [
                "模型排名是描述性结果，没有做显著性检验。"
                if zh
                else "Model ranking is descriptive; no significance is claimed for it."
            ],
        },
        "fault_dependent": {
            "headline": (
                _fault_level_headline(scoped, node, language)
                if scoped
                else (
                    "Graph 在两类故障上的诊断表现不同。"
                    if zh
                    else "Graph results differ by fault type."
                )
            ),
            "support": _dataset_reversal_text(report, language),
            "caveats": [],
        },
    }


def _cross_model_headline(
    tokens: Mapping[str, object], accuracy_gain: int, models: int, language: str
) -> str:
    """How consistently the arms separated across models, from whichever metric has a base.

    "Every model" needs every model in the cohort to have been measured and every
    one of them to have moved that way. Where no model had an eligible token pair
    the sentence falls back to diagnosis counts, which always have a denominator.
    """
    measured, reduced = int(tokens["models"]), int(tokens["reduced"])
    if measured == reduced == models:
        if language == "zh":
            return (
                f"{models} 个模型全部在 GreptimeDB 上读入更少 token，"
                f"{accuracy_gain}/{models} 个诊断更准。"
            )
        return (
            f"Every model read fewer tokens on GreptimeDB, and "
            f"{accuracy_gain} of {models} were more accurate."
        )
    if measured:
        if language == "zh":
            return f"{reduced}/{measured} 个模型在 GreptimeDB 上读入更少 token。"
        return f"{reduced} of {measured} models read fewer tokens on GreptimeDB."
    if language == "zh":
        return f"{accuracy_gain}/{models} 个模型在 GreptimeDB 上的诊断正确数更高。"
    return f"{accuracy_gain} of {models} models produced more correct diagnoses on GreptimeDB."


def _one_store_headline(report: Mapping[str, object], language: str) -> str:
    """Names the axes GreptimeDB actually led on, so a reversal cannot be claimed as a win."""
    facts = _contrast_facts(report)
    correct, tokens = mapping(facts, "correct"), mapping(facts, "tokens")
    won = []
    if correct["raw"] > correct["split_pillars"]:
        won.append("准确率" if language == "zh" else "accuracy")
    if tokens["raw"] < tokens["split_pillars"]:
        won.append("读入 token" if language == "zh" else "tokens read")
    cost = _headline_cost_direction(report)
    if cost is not None and cost > 1:
        won.append("成本" if language == "zh" else "cost")
    if not won:
        return (
            "一个库和三个后端的差距按指标而定。"
            if language == "zh"
            else "One store and three backends differ by metric."
        )
    if language == "zh":
        return f"在{'、'.join(won)}三项上，一个数据库均优于三个后端。"
    return f"One database beat three backends on {_join(won, language)}."


def _headline_cost_direction(report: Mapping[str, object]) -> float | None:
    """The three-backend spend as a multiple of GreptimeDB's, when both are priced."""
    cost = next(row for row in mapping_list(_headline_bars(report), "rows") if row["id"] == "cost")
    bounds = cost.get("bounds")
    if isinstance(bounds, Mapping):
        low = float(bounds["split_pillars"][0])
        high = float(bounds["raw"][1])
        return low / high if high else None
    ratio = (cost.get("ratios") or {}).get("split_pillars")
    return float(ratio) if isinstance(ratio, (int, float)) else None


def _semantic_headline(scoped: Mapping[str, object] | None, language: str) -> str:
    """The fault levels the layer was built for, named with the arm that led them."""
    if scoped is None:
        return (
            "语义层减少了聚焦检索读回的数据量。"
            if language == "zh"
            else "The Semantic Graph read back less data in focused retrieval."
        )
    correct = mapping(scoped, "correct")
    best = max(correct, key=lambda key: correct[key])
    if best != "semantic_graph":
        return (
            "服务与依赖层故障上，语义层没有取得最高准确率。"
            if language == "zh"
            else "The Semantic Graph did not lead on service and dependency faults."
        )
    if language == "zh":
        return "服务与依赖层故障上，语义层是三个接口里最准的。"
    return "On service and dependency faults, the Semantic Graph was the most accurate interface."


def _fault_level_headline(
    scoped: Mapping[str, object], node: Mapping[str, object] | None, language: str
) -> str:
    """States which side of the fault-level split each arm led, since the chart shows both."""
    graph_ahead = mapping(scoped, "correct")["semantic_graph"] >= mapping(scoped, "correct")["raw"]
    node_behind = node is not None and (
        mapping(node, "correct")["semantic_graph"] < mapping(node, "correct")["raw"]
    )
    if graph_ahead and node_behind:
        return (
            "语义层的收益集中在服务与依赖层故障；节点层故障上它低于不带语义层的 GreptimeDB。"
            if language == "zh"
            else "The layer's gain is on service and dependency faults; on node faults it fell "
            "behind GreptimeDB alone."
        )
    if graph_ahead:
        return (
            "语义层在服务与依赖层故障上领先。"
            if language == "zh"
            else "The layer leads on service and dependency faults."
        )
    return (
        "语义层在不同故障层级上的表现不同。"
        if language == "zh"
        else "The layer's results differ by fault level."
    )


def _family_caveats(report: Mapping[str, object], family: str, language: str) -> list[str]:
    """What the family's frozen test does and does not license, kept with the finding."""
    tally = _family_tally(report, family)
    caveats = [_tally_text(tally, language) + ("。" if language == "zh" else ".")]
    caveats.extend(
        _significant_text(item, language)
        for item in _family_results(report, family)
        if item["significant"]
    )
    caveats.append(
        "配对端点仅限于冻结的合格规则筛选出的 case，数量少于队列总数；"
        "不显著表示证据不足，不表示等价。"
        if language == "zh"
        else "Paired endpoints are restricted to the cases the frozen eligibility rule admits, "
        "which is fewer cases than the cohort holds. Non-significant means insufficient "
        "evidence, not equivalence."
    )
    return caveats


SCOPED_FAULT_LEVELS = ("component", "dependency_edge")


def _scope_contrast(
    report: Mapping[str, object], scopes: tuple[str, ...] = SCOPED_FAULT_LEVELS
) -> dict[str, object] | None:
    """Correct diagnoses per arm over one group of fault levels, with per-model direction.

    Returns None when the cohort holds none of those levels, so a finding about
    them is dropped rather than printed against an empty denominator.
    """
    buckets = mapping(report, "diagnosis_by_causal_scope")
    present = tuple(scope for scope in scopes if isinstance(buckets.get(scope), Mapping))
    if not present:
        return None
    slope = _diagnosis_slope(report, present)
    series = mapping_list(slope, "series")
    treatments = [str(item) for item in _sequence(slope, "treatments")]
    values = [mapping(item, "values") for item in series]
    return {
        "scopes": list(present),
        "models": len(series),
        "runs_per_model": int(slope["runs_per_treatment"]),
        "runs": int(slope["runs_per_treatment"]) * len(series),
        "correct": {
            treatment: sum(int(item[treatment]) for item in values) for treatment in treatments
        },
        "better": sum(int(item["semantic_graph"]) > int(item["raw"]) for item in values),
        "worse": sum(int(item["semantic_graph"]) < int(item["raw"]) for item in values),
    }


def _relative_rows(
    report: Mapping[str, object], family: str, metric: str
) -> list[Mapping[str, object]]:
    group = next((item for item in _relative_change(report) if item["family"] == family), None)
    if group is None:
        return []
    return next(
        (
            mapping_list(item, "rows")
            for item in mapping_list(group, "metrics")
            if item["metric"] == metric
        ),
        [],
    )


def _reduction_counts(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """How many models' case medians moved toward the first treatment, and by how much.

    A model with no eligible pair has no median and is left out of both the count
    and its denominator: carrying it in the denominator would report "5 of 6"
    where one of the six was never measured, and carrying it in the numerator
    would report a reduction that no pair produced.
    """
    percents = [
        float(row["percent"]) for row in rows if isinstance(row.get("percent"), (int, float))
    ]
    magnitudes = sorted(-value for value in percents if value < 0)
    return {
        "models": len(percents),
        "reduced": len(magnitudes),
        "smallest": magnitudes[0] if magnitudes else None,
        "largest": magnitudes[-1] if magnitudes else None,
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
        cheaper_label = ARM_NAMES[treatment][language]
        other_label = ARM_NAMES[baseline][language]
        if language == "zh":
            text = _zh_join(
                f"{len(comparable)} 个可计价模型中，{cheaper} 个在",
                cheaper_label,
                "上的支出低于",
                other_label,
                "。",
            )
            if unpriced:
                text += f"{_join(unpriced, language)} 的完整成本不可估算，不参与比较。"
        else:
            text = (
                f"{cheaper} of {len(comparable)} priced models spent less on "
                f"{cheaper_label} than on {other_label}."
            )
            if unpriced:
                text += (
                    f" {_join(unpriced, language)} "
                    f"{'has' if len(unpriced) == 1 else 'have'} no complete cost estimate and "
                    f"{'is' if len(unpriced) == 1 else 'are'} left out of the comparison."
                )
        texts[family] = text
    return texts


def _dataset_reversal_text(report: Mapping[str, object], language: str) -> list[str]:
    """States the direction of the Graph − Raw diagnosis difference per source.

    The cohort-wide total hides that the two sources disagree, so the sentence is
    derived from the split rather than asserted alongside it.
    """
    buckets = mapping(report, "diagnosis_by_dataset")
    parts = []
    for dataset, bucket in buckets.items():
        correct = mapping(bucket, "diagnosis_correct")
        label = _dataset_attribution(dataset)["label"]
        scopes = sorted(
            {
                str(case["causal_scope"])
                for case in mapping_list(report, "case_catalog")
                if case["dataset"] == dataset
            }
        )
        scope_names = {
            "component": ("service", "服务"),
            "dependency_edge": ("dependency", "依赖"),
            "infrastructure_node": ("node", "节点"),
        }
        fault_types = "/".join(scope_names[scope][language == "zh"] for scope in scopes)
        graph = int(correct["semantic_graph"])
        raw = int(correct["raw"])
        graph_name = ARM_NAMES["semantic_graph"][language]
        raw_name = ARM_NAMES["raw"][language]
        if language == "zh":
            parts.append(
                f"{fault_types}故障（{label}）：{graph_name} {graph} 次、"
                f"{raw_name} {raw} 次正确诊断"
            )
        else:
            parts.append(
                f"{fault_types.capitalize()} faults ({label}): "
                f"{graph} correct with {graph_name}, {raw} with {raw_name}"
            )
    if language == "zh":
        return [*parts, "这是测量后的分组分析；故障类型与数据来源重合，不能单独归因于故障类型。"]
    return [
        *parts,
        "Post-measurement comparison: fault type and dataset are confounded, "
        "so their effects cannot be separated.",
    ]


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
            f"{tool['runs']} 次 Graph 运行中，"
            f"{tool['runs_with_successful_call']} 次至少成功调用过一次 query_semantic_graph，"
            f"合计 {tool['successful_calls']} 次。两个 GreptimeDB 接口共执行 {join_calls} 次"
            f"成功的 SQL JOIN，分布在 {join_runs} 次运行中；其中跨信号的有 "
            f"{cross_calls} 次，出现在 {cross_runs} 次运行中。使用成功 PromQL 查询的运行，"
            f"GreptimeDB 侧为 {greptime_runs} 次中的 {greptime_promql} 次，"
            f"三后端侧为 {split_runs} 次中的 {promql.get('split_pillars', 0)} 次。"
        )
    return (
        f"Among Graph runs, "
        f"{tool['runs_with_successful_call']} of "
        f"{tool['runs']} runs that had it made at least one successful query_semantic_graph "
        f"call, {tool['successful_calls']} calls in all. The two GreptimeDB interfaces issued "
        f"{join_calls} successful SQL JOIN calls across {join_runs} runs, of which only "
        f"{cross_calls} joined across signal kinds, in {cross_runs} runs. PromQL evaluation "
        f"succeeded in {greptime_promql} of {greptime_runs} GreptimeDB runs and "
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
                f"{model} 的 {counts['runs']} 次运行中，"
                f"{counts['runs_without_citation']} 次没有提交任何引用"
                f"（其中 {counts['correct_diagnoses_lost_to_missing_citation']} 次诊断正确）"
            )
        else:
            parts.append(
                f"{model} submitted no citation in "
                f"{counts['runs_without_citation']} of its {counts['runs']} runs, "
                f"{counts['correct_diagnoses_lost_to_missing_citation']} of which "
                "had reached a correct diagnosis"
            )
    if language == "zh":
        return f"{_join(parts, language)}。这些运行缺少执行有效的引用，因此不纳入主要效率配对样本。"
    return (
        f"{_join(parts, language)}. Eligibility needs at least one execution-valid "
        "citation, so these runs are excluded from the paired efficiency sample."
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
            f"专项测试使用 {_join(micro_parts, language)}；"
            f"端到端样本使用 {_join(transfer_parts, language)}。"
            f"感谢 {names} 的作者与维护者公开数据和研究材料，使本评测能够复现。"
        )
        terms = (
            "".join(_dataset_attribution(adapter)["zh"] for adapter in datasets)
            + "本项目选择 case，将评测限制在冻结的时间窗内，并把源格式映射到"
            "评测使用的写入协议。RCA-100 的公开选择记录包含 15 个节点故障候选的"
            "聚合概况和 4 个入选 case。发布工件只包含脱敏标识符、派生事实、聚合测量"
            "和源文件哈希，不包含原始遥测行、源数据归档、拓扑、因果图或参考答案文件。"
            "本项目不解决上游许可冲突。Apache-2.0 只适用于本项目原创的代码、工件格式、"
            "报告文本和独立派生的聚合结果，不重新许可上游数据或上游数据集文档。"
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
        + " Agent RCA Bench selects cases, restricts evaluation to frozen case windows, "
        "and maps source formats into its ingestion protocols. The public RCA-100 "
        "selection fixture records 15 aggregate node-fault candidate profiles and four "
        "selected cases. Published artifacts contain sanitized identifiers, derived "
        "facts, aggregate measurements, and source hashes; they contain no source "
        "telemetry rows, source archives, topology, causal graphs, or ground-truth files. "
        "This project does not resolve upstream license conflicts. Apache-2.0 applies "
        "only to the benchmark's original code, artifact schemas, report text, and "
        "independently derived aggregates; it does not relicense upstream data or "
        "upstream dataset documentation."
    )
    return text, terms


def _attribution_links(report: Mapping[str, object]) -> list[dict[str, str]]:
    links = []
    for adapter in _cohort_datasets(report):
        attribution = _dataset_attribution(adapter)
        links.append({"label": f"{attribution['label']} source", "url": attribution["url"]})
        if citation_url := attribution.get("citation_url"):
            links.append({"label": f"{attribution['label']} citation", "url": citation_url})
        if license_url := attribution.get("license_url"):
            links.append({"label": f"{attribution['label']} license", "url": license_url})
        if terms_url := attribution.get("terms_url"):
            links.append({"label": f"{attribution['label']} terms", "url": terms_url})
    return links


def _metric_label(metric: object, language: str) -> str:
    labels = {
        "correct_completion_tool_calls": (
            "correct-completion tool calls",
            "正确完成所需的工具调用",
        ),
        "provider_visible_input_tokens": (
            "provider-visible input tokens",
            "输入 token 数",
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
                    "multiplicity_family_size": effect["multiplicity_family_size"],
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


# The work a correct answer cost, as a proportion of what the baseline arm spent
# on the same incident. Absolute deltas answer "how much" but not "how much of
# it", and a reader comparing models needs the second question answered.
RELATIVE_METRICS = ("correct_completion_tool_calls", "provider_visible_input_tokens")


def _relative_change(report: Mapping[str, object]) -> list[dict[str, object]]:
    """Median per-case relative change per model, for each family and metric.

    Each point is one case's paired change divided by that case's own baseline,
    reduced to a case median exactly as the registered endpoints are. The median
    of those proportions is reported, not the ratio of two medians, which is a
    quantity no pair produced.
    """
    families = []
    for family, (treatment, baseline) in FAMILY_COMPARISONS.items():
        metrics = []
        for metric in RELATIVE_METRICS:
            rows = []
            for model in _sequence(report, "model_order"):
                case_effects = _family_case_effects(report, str(model), family)
                values = [
                    float(item[f"{metric}_relative"])
                    for item in case_effects
                    if isinstance(item.get(f"{metric}_relative"), (int, float))
                ]
                rows.append(
                    {
                        "model": str(model),
                        "cases": len(values),
                        "percent": (statistics.median(values) * 100.0 if values else None),
                    }
                )
            widest = max(
                (abs(float(row["percent"])) for row in rows if row["percent"] is not None),
                default=0.0,
            )
            for row in rows:
                percent = row["percent"]
                row["fraction"] = (
                    0.0 if percent is None or not widest else abs(float(percent)) / widest
                )
                row["direction"] = "none" if percent is None else "down" if percent < 0 else "up"
            metrics.append(
                {
                    "metric": metric,
                    "label": {
                        language: _relative_metric_label(metric, language) for language in LANGUAGES
                    },
                    "rows": rows,
                }
            )
        # Bar length is magnitude, not confidence. The widest bar in a family can
        # be the model that qualified on the fewest incidents, so the spread in
        # case counts is stated rather than left to the small print beside it.
        counts = [int(row["cases"]) for metric in metrics for row in metric["rows"]]
        families.append(
            {
                "family": family,
                "comparison": _comparison_label(family),
                "favors": {
                    "down": TREATMENT_LABELS[treatment],
                    "up": TREATMENT_LABELS[baseline],
                },
                "metrics": metrics,
                "case_span": {
                    language: _case_span_text(min(counts), max(counts), language)
                    for language in LANGUAGES
                },
            }
        )
    return families


def _case_span_text(fewest: int, most: int, language: str) -> str:
    if fewest == most:
        return (
            f"Every model qualified on {most} incidents."
            if language == "en"
            else f"每个模型都在 {most} 个故障上入选。"
        )
    if language == "en":
        return (
            f"Eligible case counts range from {fewest} to {most} across models. "
            "Bar lengths show medians, not sampling uncertainty."
        )
    return f"各模型有 {fewest} 至 {most} 个合格 case；条长表示中位数，不表示抽样不确定性。"


def _relative_metric_label(metric: str, language: str) -> str:
    labels = {
        "correct_completion_tool_calls": ("Steps to a correct answer", "答对所用的步数"),
        "provider_visible_input_tokens": ("Tokens read", "读入的 token"),
    }
    english, chinese = labels[metric]
    return english if language == "en" else chinese


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
            return f"{eligible} 个故障中两侧没有一致方向"
        return f"Neither side needed consistently less across {eligible} incidents"
    leading = negative if negative > positive else positive
    side = TREATMENT_LABELS[treatment if negative > positive else baseline]
    if language == "zh":
        return f"{eligible} 个故障中 {leading} 个是 {side} 用量更低"
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
    exponent = _floor_log10(knee)
    while True:
        tick = 10.0**exponent
        if tick > widest:
            break
        if tick >= knee:
            values.append(tick)
        exponent += 1
    return values


def _floor_log10(value: float) -> int:
    """`floor(log10(value))`, corrected so a 1 ULP `log10` error cannot move it.

    `log10` is not required to be correctly rounded, so a power of ten can come
    back just under its exact exponent and shift every tick by a decade. The
    comparison that fixes it uses only exact powers and ordering.
    """
    exponent = math.floor(math.log10(value))
    if 10.0 ** (exponent + 1) <= value:
        return exponent + 1
    if 10.0**exponent > value:
        return exponent - 1
    return exponent


def _symlog_position(value: float, axis: Mapping[str, object]) -> float:
    """Map a delta to [-1, 1]; the browser turns that into pixels.

    The result is quantised because `log1p` is a libm function that platforms
    may round differently in the last place. Without that step the published
    HTML stops reproducing byte for byte across operating systems, which is the
    property the release tag exists to guarantee. `POSITION_PRECISION` keeps
    several orders of magnitude more resolution than any pixel needs.
    """
    max_abs = float(axis["max_abs"])
    if max_abs <= 0:
        return 0.0
    knee = float(axis["knee"])
    magnitude = abs(float(value))
    scaled = math.log1p(magnitude / knee) / math.log1p(max_abs / knee)
    return round(math.copysign(scaled, value), POSITION_PRECISION)


def _diagnosis_slope(
    report: Mapping[str, object], scopes: tuple[str, ...] = ()
) -> dict[str, object]:
    """Correct diagnoses per treatment for each model, on a shared run denominator."""
    execution = mapping(report, "execution")
    treatments = [str(item) for item in _sequence(execution, "treatments")]
    runs = int(execution["transfer_cases"]) * int(execution["repetitions_per_model_case"])
    buckets = [mapping(mapping(report, "diagnosis_by_causal_scope"), scope) for scope in scopes]
    if buckets:
        runs = sum(int(bucket["cases"]) for bucket in buckets) * int(
            execution["repetitions_per_model_case"]
        )
    series = []
    for model in _sequence(report, "model_order"):
        correct = (
            {
                treatment: sum(
                    int(
                        mapping(mapping(bucket, "diagnosis_correct_by_model"), str(model))[
                            treatment
                        ]
                    )
                    for bucket in buckets
                )
                for treatment in treatments
            }
            if buckets
            else mapping(_transfer(report, str(model)), "diagnosis_correct")
        )
        series.append(
            {
                "model": str(model),
                "values": {treatment: int(correct[treatment]) for treatment in treatments},
            }
        )
    return {
        "treatments": treatments,
        "runs_per_treatment": runs,
        "scopes": list(scopes),
        "label": (
            {
                language: " + ".join(CAUSAL_SCOPE_LABELS[scope][language] for scope in scopes)
                + ("：诊断正确数" if language == "zh" else ": correct diagnoses")
                for language in LANGUAGES
            }
            if scopes
            else None
        ),
        "series": series,
        "ranked_series": sorted(
            series, key=lambda item: (-sum(item["values"].values()), item["model"])
        ),
    }


def _headline_bars(report: Mapping[str, object]) -> dict[str, object]:
    """The arm-level contrasts, each as a bar with a ratio against the best arm.

    Every ratio is `arm / best`, whichever direction is better, so the number and
    the bar always agree: 2.40 means this arm spent 2.40 times the cheapest, 0.75
    means it got three quarters of the diagnoses the best arm did. Mixing the two
    conventions made a shorter bar carry a larger number.

    None of these rows is a registered endpoint. The registered token comparison
    is paired, eligibility-filtered and confined to one family; a workload total
    over every executed run is a different quantity and is labelled as one.
    """
    treatments = [str(item) for item in _sequence(mapping(report, "execution"), "treatments")]
    diagnosis = _diagnosis_slope(report)
    runs = int(diagnosis["runs_per_treatment"]) * len(mapping_list(diagnosis, "series"))
    correct = {
        treatment: sum(
            int(mapping(item, "values")[treatment]) for item in mapping_list(diagnosis, "series")
        )
        for treatment in treatments
    }
    usage = mapping(report, "usage_by_treatment")
    by_treatment = mapping(usage, "by_treatment")
    unpriced = [str(item) for item in _sequence(usage, "unpriced_models")]
    priced = mapping(usage, "priced_models")

    rows: list[dict[str, object]] = [
        {
            "id": "accuracy",
            "label_key": "accuracy",
            "better": "higher",
            "unit": "runs",
            "values": {treatment: correct[treatment] for treatment in treatments},
            "denominator": runs,
        }
    ]
    # One row in USD, converted at the frozen rate the report publishes. Spend
    # billed in another currency is still recorded in that currency; converting
    # only makes the arms addable, which is what a single cost row requires.
    usd = mapping(usage, "estimated_cost_usd")
    if unpriced:
        usd = dict.fromkeys(treatments)
    rows.append(
        {
            "id": "cost",
            "label_key": "cost",
            "better": "lower",
            "unit": "currency",
            "currency": "USD",
            "estimate_note": None,
            "values": {
                treatment: (None if usd[treatment] is None else float(usd[treatment]))
                for treatment in treatments
            },
            "unavailable_text": {
                language: (
                    f"{_join(unpriced, language)} 的端到端用量无法完整计价，因此不提供全模型合计。"
                    f"下方保留 {len(priced)} 个模型的端到端成本估算。"
                    if language == "zh"
                    else f"End-to-end usage for {_join(unpriced, language)} "
                    "cannot be fully priced, "
                    f"so no all-model total is reported. End-to-end cost estimates for "
                    f"{len(priced)} {'model remains' if len(priced) == 1 else 'models remain'} "
                    "available below."
                )
                for language in LANGUAGES
            }
            if unpriced
            else None,
        }
    )
    rows.append(
        {
            "id": "input_tokens",
            "label_key": "input_tokens",
            "better": "lower",
            "unit": "tokens",
            "values": {
                treatment: int(mapping(by_treatment, treatment)["provider_visible_input_tokens"])
                for treatment in treatments
            },
        }
    )

    if unpriced:
        cost_chart = _cost_bars(report)
        series = cost_chart["series"]
        if all(item["estimable"] or item["bounds"] for item in series):
            totals = {treatment: [[], []] for treatment in treatments}
            for item in series:
                bounded = report.get("bounded_transfer_cost_estimates", {}).get(item["model"])
                currency = bounded["currency"] if bounded else item["billed_currency"]
                rate = 1.0 if currency == "USD" else item["exchange_rate"]["per_unit_usd"]
                for treatment in treatments:
                    limits = (
                        bounded["by_treatment"][treatment]
                        if bounded
                        else [item["native"][treatment]] * 2
                    )
                    for index, value in enumerate(limits):
                        totals[treatment][index].append(float(value) * rate)
            bounds = {
                treatment: [math.fsum(values) for values in limits]
                for treatment, limits in totals.items()
            }
            widest = max(limits[1] for limits in bounds.values())
            reference = [min(limits[index] for limits in bounds.values()) for index in (0, 1)]
            ratio_bounds = {}
            for treatment, limits in bounds.items():
                if limits[1] <= min(other[0] for key, other in bounds.items() if key != treatment):
                    ratio_bounds[treatment] = [1.0, 1.0]
                elif reference[0] > 0:
                    ratio_bounds[treatment] = [
                        max(1.0, limits[0] / reference[1]),
                        limits[1] / reference[0],
                    ]
                else:
                    ratio_bounds[treatment] = None
            next(row for row in rows if row["id"] == "cost").update(
                bounds=bounds,
                bounds_ratio_labels={
                    treatment: (
                        None
                        if limits is None
                        else f"×{limits[0]:.2f}"
                        if limits[0] == limits[1]
                        else f"×{limits[0]:.2f}–{limits[1]:.2f}"
                    )
                    for treatment, limits in ratio_bounds.items()
                },
                bounds_fractions={
                    treatment: [value / widest if widest else 0.0 for value in limits]
                    for treatment, limits in bounds.items()
                },
                bounds_labels={
                    treatment: f"USD {limits[0]:.2f}–{limits[1]:.2f}"
                    for treatment, limits in bounds.items()
                },
                unavailable_text=None,
                estimate_note={
                    "en": (
                        f"Estimated end-to-end cost for all {len(series)} models. "
                        "The interval reflects missing cache detail."
                    ),
                    "zh": (
                        f"全部 {len(series)} 个模型的端到端成本估算；"
                        "费用区间反映缓存明细缺失的影响。"
                    ),
                },
            )

    for row in rows:
        # Only None is missing. Zero is a measured value: an arm that got nothing
        # right, or cost nothing, still belongs on the axis. Treating it as
        # missing dropped it out of the reference and printed N/A where the
        # answer was 0.
        values = [value for value in row["values"].values() if value is not None]
        # Nothing measured at all: no best arm and no ratio. Say so.
        if not values:
            row["estimable"] = False
            row["best"] = None
            row["reference"] = None
            row["ratios"] = dict.fromkeys(row["values"], None)
            row["fractions"] = dict.fromkeys(row["values"], 0.0)
            continue
        row["estimable"] = True
        reference = max(values) if row["better"] == "higher" else min(values)
        widest = max(values)
        row["reference"] = reference
        # The best arm is named here rather than recovered downstream by looking
        # for the ratio that equals 1: with a zero reference no ratio does.
        row["best"] = next(
            treatment
            for treatment, value in row["values"].items()
            if value is not None and value == reference
        )
        # A zero reference gives every other arm an undefined multiple, so the
        # row keeps its values and reports no ratio rather than dividing by it.
        row["ratios"] = {
            treatment: (None if value is None or not reference else value / reference)
            for treatment, value in row["values"].items()
        }
        row["fractions"] = {
            treatment: (value / widest if value is not None and widest else 0.0)
            for treatment, value in row["values"].items()
        }
    return {"treatments": treatments, "rows": rows}


def _accuracy_by_level(report: Mapping[str, object]) -> dict[str, object]:
    """Correct-diagnosis rate per arm, grouped by the level the fault sat at.

    The rate, not the count, because the three levels hold different numbers of
    runs and only the rate is comparable across them.
    """
    treatments = [str(item) for item in _sequence(mapping(report, "execution"), "treatments")]
    buckets = mapping(report, "diagnosis_by_causal_scope")
    groups = []
    for scope, bucket in buckets.items():
        correct = mapping(bucket, "diagnosis_correct")
        runs = mapping(bucket, "runs")
        groups.append(
            {
                "key": scope,
                "label": {
                    language: CAUSAL_SCOPE_LABELS.get(scope, {}).get(language, scope)
                    for language in LANGUAGES
                },
                "cases": int(bucket["cases"]),
                "rates": {
                    treatment: (
                        int(correct[treatment]) / int(runs[treatment])
                        if int(runs[treatment])
                        else 0.0
                    )
                    for treatment in treatments
                },
                "counts": {
                    treatment: {
                        "correct": int(correct[treatment]),
                        "runs": int(runs[treatment]),
                    }
                    for treatment in treatments
                },
            }
        )
    return {"treatments": treatments, "groups": groups}


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
        currency = entry.get("currency")
        bounded = report.get("bounded_transfer_cost_estimates", {}).get(str(model))
        model_rates = (
            mapping(mapping(report, "exchange_rates_by_model"), str(model))
            if report.get("exchange_rates_by_model")
            else mapping(mapping(report, "usage_by_treatment"), "exchange_rates")
        )
        estimable = all(merged.get(treatment) is not None for treatment in treatments)
        # One chart, one currency. Bars billed in CNY beside bars billed in USD
        # share an axis that means nothing, and the reader has no way to see it.
        # The billed figure stays in `native` so the conversion is checkable.
        series.append(
            {
                "model": str(model),
                "currency": "USD",
                "billed_currency": currency,
                "estimable": estimable,
                "bounds": bounded["by_treatment"] if bounded else None,
                "bounds_labels": {
                    treatment: f"{bounded['currency']} {limits[0]:.2f}–{limits[1]:.2f}"
                    for treatment, limits in bounded["by_treatment"].items()
                }
                if bounded
                else None,
                "estimate_note": {
                    "en": (
                        "Cost interval retains reported cache discounts. Only input with missing "
                        "cache detail ranges from cached to ordinary pricing. Not invoiced spend."
                    ),
                    "zh": (
                        "成本区间保留已报告的缓存折扣，仅对缺失缓存明细的输入分别按缓存和普通单价计算。"
                        "这不是账单金额。"
                    ),
                }
                if bounded
                else None,
                "exchange_rate": model_rates.get(str(currency)),
                "unavailable_reason_code": entry.get("unavailable_reason_code"),
                "values": {
                    treatment: (
                        None
                        if merged.get(treatment) is None or not isinstance(currency, str)
                        else round(
                            to_usd(float(merged[treatment]), currency, exchange_rates=model_rates),
                            6,
                        )
                    )
                    for treatment in treatments
                },
                "native": {treatment: merged.get(treatment) for treatment in treatments},
            }
        )
    # Ratios are within a model, against its own cheapest arm, on the same
    # `arm / best` convention the headline uses. Across models they would only
    # compare list prices, which is a fact about the providers rather than about
    # the interfaces this page is comparing.
    for item in series:
        priced = [value for value in item["values"].values() if isinstance(value, (int, float))]
        cheapest = min(priced) if priced else None
        widest = max(priced) if priced else None
        item["reference"] = cheapest
        item["ratios"] = {
            treatment: (
                None if not isinstance(value, (int, float)) or not cheapest else value / cheapest
            )
            for treatment, value in item["values"].items()
        }
        item["fractions"] = {
            treatment: (
                0.0 if not isinstance(value, (int, float)) or not widest else value / widest
            )
            for treatment, value in item["values"].items()
        }
    rates = {
        currency: dict(rate)
        for currency, rate in mapping(
            mapping(report, "usage_by_treatment"), "exchange_rates"
        ).items()
        if isinstance(rate, Mapping) and currency != "USD"
    }
    # Which models actually needed converting is decided here. A model billed in
    # another currency but never priced has no converted figure and no rate in
    # the table, and asking the renderer to work that out left it reading a rate
    # that was not there.
    converted = [
        {
            "model": str(item["model"]),
            "currency": str(item["billed_currency"]),
            "units_per_usd": item["exchange_rate"]["units_per_usd"],
            "checked_at": item["exchange_rate"]["checked_at"],
            "source": item["exchange_rate"]["source"],
        }
        for item in series
        if item["estimable"]
        and isinstance(item["billed_currency"], str)
        and item["billed_currency"] != "USD"
        and isinstance(item["exchange_rate"], Mapping)
    ]
    return {"series": series, "exchange_rates": rates, "converted": converted}


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


def render_formal_measurement_report(
    report: dict[str, object],
    output: Path,
    *,
    report_json_filename: str | None = None,
    canonical_url: str | None = None,
    cover_filename: str | None = None,
) -> None:
    """Write the self-contained page: skeleton, design, renderer, and both payloads."""
    validate_formal_measurement_report(report)
    view = build_report_view_model(report, report_json_filename=report_json_filename)
    assets = files("agent_rca_bench").joinpath("assets/report")
    document = assets.joinpath("index.html").read_text(encoding="utf-8")
    for placeholder, replacement in (
        ("__REPORT_STYLE__", assets.joinpath("report.css").read_text(encoding="utf-8")),
        ("__REPORT_SCRIPT__", assets.joinpath("report.js").read_text(encoding="utf-8")),
        ("__REPORT_LOGO__", assets.joinpath("logo.svg").read_text(encoding="utf-8").strip()),
        ("__REPORT_FAVICON__", _data_uri(assets.joinpath("favicon.svg").read_text("utf-8"))),
        ("__REPORT_TITLE__", _escape(_page_title(report))),
        ("__REPORT_DESCRIPTION__", _escape(_page_description(report))),
        ("__REPORT_LOCATION_META__", _location_meta(canonical_url, cover_filename)),
        ("__REPORT_JSONLD__", _structured_data(report, canonical_url)),
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


def _location_meta(canonical_url: str | None, cover_filename: str | None) -> str:
    """The tags that name a location, emitted only where the caller knows one.

    A guessed canonical tells a crawler the real page is a duplicate, and a
    guessed `og:image` shows a card with no picture; both are worse than the
    tag's absence.
    """
    if not canonical_url:
        return ""
    tags = [
        f'  <link rel="canonical" href="{_escape(canonical_url)}">',
        f'  <meta property="og:url" content="{_escape(canonical_url)}">',
    ]
    if cover_filename:
        cover = canonical_url.rstrip("/") + "/" + cover_filename
        width, height = COVER_SIZE
        tags += [
            f'  <meta property="og:image" content="{_escape(cover)}">',
            f'  <meta property="og:image:width" content="{width}">',
            f'  <meta property="og:image:height" content="{height}">',
        ]
    return "\n".join(tags)


def _structured_data(report: Mapping[str, object], canonical_url: str | None) -> str:
    """A Dataset record, which is what this page is: measurements and their terms."""
    publication = report.get("publication")
    record: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": _page_title(report),
        "description": _page_description(report),
        "creator": {"@type": "Organization", "name": "Greptime"},
        "license": "https://www.apache.org/licenses/LICENSE-2.0",
        "inLanguage": list(LANGUAGES),
        "isAccessibleForFree": True,
    }
    if isinstance(publication, Mapping):
        record["dateModified"] = str(publication["measurement_updated_at"])
        record["datePublished"] = str(publication["report_generated_at"])
    if canonical_url:
        record["url"] = canonical_url
    return _inline_json(record)


def render_social_cover(
    report: Mapping[str, object], output: Path, *, language: str = "en"
) -> None:
    """The share card as a standalone page, ready to be captured at COVER_SIZE.

    It carries the report's own stylesheet and the same class names as the first
    screen, so the card cannot drift into a second design of the same numbers.
    The capture step is separate: the page is deterministic, the raster is not.
    """
    if language not in LANGUAGES:
        raise ValueError(f"no cover language: {language}")
    assets = files("agent_rca_bench").joinpath("assets/report")
    ledger = _hero_ledger(report)
    rows = ""
    if ledger:
        arm = {side: ARM_NAMES[str(ledger[side])][language] for side in ("left", "right")}
        rows = "".join(
            '<div class="ledger-row">'
            f'<p class="ledger-label">{_escape(mapping(row, "label")[language])}</p>'
            + "".join(
                f'<div class="ledger-pair" data-side="{side}">'
                f'<span class="ledger-arm">{_escape(arm[side])}</span>'
                '<span class="ledger-track">'
                f'<span class="ledger-fill" style="width:{float(row[side + "_fill"]) * 100:.4f}%">'
                "</span></span>"
                f'<span class="ledger-value">{_escape(str(row[side + "_text"]))}</span>'
                f'<span class="ledger-delta">{_escape(delta)}</span>'
                "</div>"
                for side, delta in (
                    ("left", str(mapping(row, "delta")[language])),
                    ("right", ""),
                )
            )
            + "</div>"
            for row in mapping_list(ledger, "rows")
        )
    marks = "".join(
        '<span class="hero-claim">'
        + "".join(
            (
                f"<mark>{_escape(str(part[language]))}</mark>"
                if part["kind"] == "mark"
                else f"<span>{_escape(str(part[language]))}</span>"
            )
            for part in clause
        )
        + "</span>"
        for clause in _hero_result(report)
    )
    width, height = COVER_SIZE
    document = f"""<!doctype html>
<html lang="{"zh-CN" if language == "zh" else "en"}">
<head><meta charset="utf-8">
<style>{assets.joinpath("report.css").read_text(encoding="utf-8")}</style>
<style>
  body {{ width: {width}px; height: {height}px; overflow: hidden; }}
  .cover {{ height: 100%; padding: 72px 88px; display: flex; flex-direction: column; }}
  .cover .logo svg {{ height: 36px; }}
  .cover h1 {{ margin-top: 34px; font-size: 72px; line-height: 1.06; max-width: 24ch; }}
  .cover .hero-result {{ margin: 30px 0 0; font-size: 34px; gap: 14px 40px; }}
  .cover .ledger {{ margin-top: auto; }}
  .cover .ledger-row {{ padding: 14px 0 16px; }}
  .cover .ledger-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
  .cover .ledger-label {{ font-size: 18px; margin-bottom: 10px; }}
  .cover .ledger-pair {{ grid-template-columns: 230px minmax(0, 1fr) 280px 140px; gap: 0 24px; }}
  .cover .ledger-pair + .ledger-pair {{ margin-top: 6px; }}
  .cover .ledger-arm {{ font-size: 21px; }}
  .cover .ledger-track {{ height: 26px; }}
  .cover .ledger-value {{ font-size: 27px; }}
  .cover .ledger-delta {{ font-size: 26px; }}
</style></head>
<body><div class="cover">
<span class="logo">{assets.joinpath("logo.svg").read_text(encoding="utf-8").strip()}</span>
<h1>{_escape(_hero_title(report, language))}</h1>
<p class="hero-result">{marks}</p>
<div class="ledger">{rows}</div>
</div></body></html>
"""
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


def _data_uri(svg: str) -> str:
    """An SVG icon inlined into the document, so the page still fetches nothing.

    The double quotes inside the markup are percent-encoded: left literal they
    close the `href` attribute early and the rest of the icon lands in the body.
    """
    return "data:image/svg+xml," + quote(svg.strip(), safe="/:=<>() '")


def _inline_json(payload: object) -> str:
    # `</` would close the host script element early; the parser reads text, not JSON.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )


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
        "<ul>"
        + "".join(f"<li>{_escape(line)}</li>" for line in mapping(copy, str(item["id"]))["support"])
        + "</ul></li>"
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
        f"{_escape(str(item['status']).replace('_', ' '))}. "
        f"{_escape(mapping(item, 'tally_text')['en'])}</li>"
        for item in mapping_list(view, "verdicts")
    )
    publication = facts.get("publication")
    publication_summary = ""
    deviation_summary = (
        f"<p>{_escape(mapping(view, 'execution_deviation_note')['en'])}</p>"
        if view.get("execution_deviation_note")
        else ""
    )
    if isinstance(publication, Mapping):
        publication_summary = (
            f"<p>Measurement updated at {_escape(publication['measurement_updated_at'])}; "
            f"report generated at {_escape(publication['report_generated_at'])}.</p>"
        )
    cost = next(
        row
        for row in mapping_list(mapping(mapping(view, "charts"), "headline"), "rows")
        if row["id"] == "cost"
    )
    cost_summary = ""
    if cost.get("bounds_labels"):
        cost_summary = (
            "<h3>Estimated end-to-end cost / 端到端估算成本</h3><ul>"
            + "".join(
                f"<li>{_escape(TREATMENT_LABELS[treatment])}: {_escape(label)}</li>"
                for treatment, label in cost["bounds_labels"].items()
            )
            + "</ul>"
            + "".join(
                f'<p lang="{language}">{_escape(text)}</p>'
                for language, text in cost["estimate_note"].items()
            )
        )
    return (
        '<div class="static-summary">'
        "<h2>What we found</h2>"
        f"<ol>{findings}</ol>"
        "<h3>The three interfaces compared</h3>"
        f"<ul>{arms}</ul>"
        "<h3>Correct diagnoses by interface</h3>"
        f"<ul>{diagnosis_rows}</ul>"
        f"{cost_summary}"
        "<h3>Pre-specified questions</h3>"
        f"<ul>{verdicts}</ul>"
        f"<p>{_escape(english['tool_use'])}</p>"
        f"<p>{facts['completed_cells']} completed runs across {facts['models']} models, "
        f"{facts['transfer_cases']} end-to-end cases and {facts['micro_cases']} micro cases. "
        'The full narrative report is in <a href="https://github.com/GreptimeTeam/'
        'agent-rca-bench/blob/main/REPORT.md">REPORT.md</a>.</p>'
        f"{publication_summary}"
        f"{deviation_summary}"
        + (
            "".join(
                f'<p lang="{language}">{_escape(text)}</p>'
                for language, text in mapping(view, "split_rerun_note").items()
            )
            if view.get("split_rerun_note")
            else ""
        )
        + "</div>"
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
