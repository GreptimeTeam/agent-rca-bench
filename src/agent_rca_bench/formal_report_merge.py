"""Combine disjoint model cohorts without recomputing their frozen tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from agent_rca_bench import formal_report as report_core
from agent_rca_bench.formal_suite import canonical_sha256
from agent_rca_bench.formal_suite_protocol import sha256_file


def merge_formal_reports(
    report_paths: list[Path],
    transfer_paths: list[Path],
    publication_path: Path,
) -> dict[str, object]:
    if len(report_paths) < 2 or len(report_paths) != len(transfer_paths):
        raise ValueError("provide at least two reports and one bound transfer artifact per report")
    reports = [report_core._load_object(path) for path in report_paths]
    reference_inference = {
        key: value
        for key, value in reports[0]["scope"]["inference"].items()
        if key != "holm_family_size"
    }
    runs = []
    models = []
    for report, transfer_path in zip(reports, transfer_paths, strict=True):
        report_core.validate_formal_measurement_report(report)
        if report.get("inference_cohorts"):
            raise ValueError("merge source reports, not previously merged reports")
        if set(models) & set(report["model_order"]):
            raise ValueError("model cohorts overlap")
        models.extend(report["model_order"])
        if sha256_file(transfer_path) != report["source_artifacts"]["transfer"]["sha256"]:
            raise ValueError("transfer artifact does not match its report binding")
        transfer = report_core._load_object(transfer_path)
        runs.extend(transfer["runs"])
        for key in ("case_catalog", "cohort_provenance", "research_questions", "audit"):
            if report[key] != reports[0][key]:
                raise ValueError(f"cohorts disagree on {key}")
        for key in ("micro_cases", "transfer_cases", "treatments", "repetitions_per_model_case"):
            if report["execution"][key] != reports[0]["execution"][key]:
                raise ValueError(f"cohorts disagree on execution.{key}")
        for key in (
            "benchmark_protocol_version",
            "greptimedb_revision",
            "greptimedb_build_profile",
            "treatment_estimand",
            "treatment_components",
        ):
            if report["scope"][key] != reports[0]["scope"][key]:
                raise ValueError(f"cohorts disagree on scope.{key}")
        inference = {
            key: value
            for key, value in report["scope"]["inference"].items()
            if key != "holm_family_size"
        }
        if inference != reference_inference:
            raise ValueError("cohorts disagree on the inference contract")

    merged = deepcopy(reports[0])
    merged.pop("integrity")
    merged["publication"] = report_core._load_publication_metadata(publication_path)
    for report in reports:
        if report.get("publication") and (
            report["publication"]["measurement_updated_at"]
            > merged["publication"]["measurement_updated_at"]
        ):
            raise ValueError("merged measurement timestamp predates a source cohort")
    merged["model_order"] = models
    merged["model_reports"] = {
        model: deepcopy(report["model_reports"][model])
        for report in reports
        for model in report["model_order"]
    }
    for key in (
        "expected_cells",
        "completed_cells",
        "micro_cells",
        "transfer_cells",
        "runner_errors",
        "budget_exhaustions",
        "models",
    ):
        merged["execution"][key] = sum(report["execution"][key] for report in reports)
    merged["inference_cohorts"] = [
        {
            "protocol_revision": report["scope"]["protocol_revision"],
            "models": report["model_order"],
            "inference": report["scope"]["inference"],
        }
        for report in reports
    ]
    merged["scope"]["protocol_revision"] = " + ".join(
        report["scope"]["protocol_revision"] for report in reports
    )
    merged["scope"]["inference"] = {
        **reference_inference,
        "policy": "Separate frozen model cohorts; Holm correction is retained within each cohort.",
    }
    merged["source_artifacts"] = {
        f"cohort_{index + 1}_{key}": binding
        for index, (report, path) in enumerate(zip(reports, report_paths, strict=True))
        for key, binding in {
            **report["source_artifacts"],
            "report": report_core._artifact_binding(report, path),
        }.items()
    }
    context = {case["case_id"]: case for case in merged["case_catalog"]}
    merged["case_outcomes"] = report_core._case_outcomes(runs, context)
    for field in ("dataset", "causal_scope"):
        merged[f"diagnosis_by_{field}"] = report_core._diagnosis_by(runs, context, field)
    merged["tool_use_audit"] = report_core._tool_use_audit(runs)
    merged["claim_rejection_audit"] = report_core._claim_rejection_audit(runs)
    merged["citation_submission"] = report_core._citation_submission(runs, models)
    merged["capability_scores"] = report_core._capability_scores(runs, models)
    merged["semantic_layer_findings"] = report_core._semantic_findings(merged["model_reports"])
    merged["confirmatory_family_resource_effects"] = {
        family: {
            model: values
            for report in reports
            for model, values in report["confirmatory_family_resource_effects"][family].items()
        }
        for family in reports[0]["confirmatory_family_resource_effects"]
    }
    # Different cohorts can freeze different rates for the same billed currency.
    # Keep those bindings per model; there is no cohort-wide CNY conversion rate.
    merged["exchange_rates_by_model"] = {
        model: {
            currency: (
                report_core.EXCHANGE_RATES_TO_USD["USD"]
                if currency == "USD"
                else report["usage_by_treatment"]["exchange_rates"][currency]
            )
            for currency in (report["costs"]["models"][model]["currency"],)
        }
        for report in reports
        for model in report["model_order"]
    }
    usage = report_core._usage_by_treatment(runs)
    usage["exchange_rates"] = {}
    usage["estimated_cost_usd"] = {
        treatment: (
            None
            if usage["unpriced_models"]
            else round(
                sum(
                    report["usage_by_treatment"]["estimated_cost_usd"][treatment]
                    for report in reports
                ),
                6,
            )
        )
        for treatment in merged["execution"]["treatments"]
    }
    merged["usage_by_treatment"] = usage
    undiscounted = {}
    for model in usage["unpriced_models"]:
        basis = next(
            report["costs"]["pricing_basis"][model]
            for report in reports
            if model in report["model_order"]
        )
        amounts = dict.fromkeys(merged["execution"]["treatments"], 0.0)
        for item in runs:
            if item["model"] != model:
                continue
            run_usage = item["run"]["usage"]
            amounts[item["visibility"]] += (
                run_usage["provider_visible_input_tokens"] * basis["uncached_input_per_million"]
                + run_usage["output_tokens"] * basis["output_per_million"]
            ) / 1_000_000
        undiscounted[model] = {
            "currency": basis["currency"],
            "by_treatment": amounts,
            "basis": (
                "all input at the ordinary input rate; output includes reasoning; no cache discount"
            ),
        }
    merged["undiscounted_transfer_cost_estimates"] = undiscounted
    if undiscounted:
        usage["conservative_estimated_cost_usd"] = {
            treatment: round(
                sum(
                    report["usage_by_treatment"]["estimated_cost_usd"][treatment] or 0.0
                    for report in reports
                )
                + sum(
                    report_core._to_usd(
                        entry["by_treatment"][treatment],
                        entry["currency"],
                        exchange_rates=merged["exchange_rates_by_model"][model],
                    )
                    for model, entry in undiscounted.items()
                ),
                6,
            )
            for treatment in merged["execution"]["treatments"]
        }
    costs = report_core._cost_report(merged["model_reports"])
    costs["models"] = {
        model: values for report in reports for model, values in report["costs"]["models"].items()
    }
    costs["pricing_basis"] = {
        model: values
        for report in reports
        for model, values in report["costs"]["pricing_basis"].items()
    }
    costs["cross_currency_total"] = (
        None
        if costs["models_with_unavailable_estimate"]
        else {
            "currency": "USD",
            "amount": round(
                sum(report["costs"]["cross_currency_total"]["amount"] for report in reports), 6
            ),
            "exchange_rates_by_model": merged["exchange_rates_by_model"],
        }
    )
    merged["costs"] = costs
    deviations = [item for report in reports for item in report.get("execution_deviations", [])]
    if deviations:
        merged["execution_deviations"] = deviations
    merged["limitations"] = list(
        dict.fromkeys(
            report_core._power_limitation(
                merged["execution"],
                {model: values["transfer"] for model, values in merged["model_reports"].items()},
            )
            if text
            == report_core._power_limitation(
                report["execution"],
                {model: values["transfer"] for model, values in report["model_reports"].items()},
            )
            else text
            for report in reports
            for text in report["limitations"]
        )
    )
    merged["integrity"] = {"semantic_payload_sha256": canonical_sha256(merged)}
    report_core.validate_formal_measurement_report(merged)
    return merged
