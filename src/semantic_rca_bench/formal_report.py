from __future__ import annotations

import html
import json
from collections import defaultdict
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

from semantic_rca_bench.formal_suite import canonical_sha256
from semantic_rca_bench.formal_suite_protocol import load_formal_suite_protocol, sha256_file
from semantic_rca_bench.formal_suite_release import validate_micro_measurement_artifact
from semantic_rca_bench.transfer_release import validate_measurement_artifact

FORMAL_MEASUREMENT_REPORT_SCHEMA_VERSION = 3


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
    model_reports = {
        name: _combined_model_report(
            next(model.model_dump(mode="json") for model in protocol.models if model.model == name),
            _mapping(micro_reports, name),
            _mapping(transfer_reports, name),
        )
        for name in names
    }
    execution = _execution(suite, protocol, micro, transfer, micro_runs, transfer_runs)
    if execution["completed_cells"] != execution["expected_cells"]:
        raise ValueError("formal measurement artifacts are incomplete")
    payload = {
        "report_schema_version": FORMAL_MEASUREMENT_REPORT_SCHEMA_VERSION,
        "report_type": "semantic-rca-measurement-report",
        "publication_status": "release-candidate measurement report",
        "research_question": (
            "Does the complete GreptimeDB Semantic Graph interface reduce RCA investigation "
            "work while preserving diagnosis and evidence validity?"
        ),
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
            "treatment_estimand": _mapping(transfer, "benchmark_protocol").get(
                "treatment_estimand"
            ),
            "treatment_components": _mapping(transfer, "benchmark_protocol").get(
                "treatment_components"
            ),
            "inference": transfer.get("inference"),
        },
        "execution": execution,
        "source_artifacts": source_artifacts,
        "model_order": names,
        "model_reports": model_reports,
        "semantic_layer_findings": _semantic_findings(model_reports),
        "costs": _cost_report(model_reports),
        "audit": _audit(micro, transfer),
        "limitations": [
            (
                "The eight-case micro cohort is fixed reference data; the ten-case end-to-end "
                "cohort is a source-ranked fresh measurement cohort."
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
                "Semantic adjudication is a sensitivity analysis. Its non-roster judges do not "
                "receive the explicit treatment label, but can infer treatment from evidence "
                "provenance."
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
        template.replace("__REPORT_TITLE__", "Semantic RCA Bench measurement")
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


def _combined_model_report(configuration, micro, transfer):
    micro_reliability = _mapping(micro, "reliability")
    transfer_reliability = _mapping(transfer, "reliability")
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
        "usage": {"micro": micro.get("usage"), "transfer": transfer.get("usage")},
    }


def _semantic_findings(reports):
    return {
        "end_to_end_transfer": {
            model: _mapping(_mapping(value, "transfer"), "primary_metrics")
            for model, value in reports.items()
        },
        "end_to_end_transfer_adjudicated_sensitivity": {
            model: _mapping(
                _mapping(_mapping(value, "transfer"), "adjudicated_sensitivity"),
                "primary_metrics",
            )
            for model, value in reports.items()
        },
        "interpretation_contract": (
            "The deterministic analysis is the headline result; adjudicated results are "
            "sensitivity analysis. Negative deltas favor Semantic Graph. Null is not estimable; "
            "zero is no observed reduction; non-significance is insufficient evidence, not "
            "equivalence."
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
        models[model] = {"estimated_cost": cost, "currency": currency}
        if cost is not None and isinstance(currency, str):
            totals[currency] += cost
        else:
            unavailable.append(model)
    return {
        "models": models,
        "known_totals_by_currency": dict(sorted(totals.items())),
        "models_with_unavailable_estimate": unavailable,
        "cross_currency_total": None,
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
    execution = _mapping(report, "execution")
    interpretation = _escape(_mapping(report, "semantic_layer_findings")["interpretation_contract"])
    cards = "".join(
        _model_card(model, _mapping(_mapping(report, "model_reports"), model))
        for model in report["model_order"]
    )
    audit = _mapping(report, "audit")
    limits = "".join(f"<li>{_escape(item)}</li>" for item in report["limitations"])
    return f"""
<header><p class="eyebrow">GreptimeDB Semantic Graph · paired agent benchmark</p>
<h1>Semantic RCA Bench</h1><p class="lede">Six model configurations, eighteen incidents,
Raw versus Semantic Graph.</p><div class="stat-grid">
{_stat(execution["completed_cells"], "completed cells")}{_stat(execution["models"], "models")}
{_stat(execution["micro_cases"] + execution["transfer_cases"], "cases")}
{_stat(execution["runner_errors"], "runner errors")}</div></header>
<main><section><h2>Interpretation</h2><p>{interpretation}</p></section>
<section><h2>Model report cards</h2><div class="model-grid">{cards}</div></section>
<section><h2>Retrieval micro-benchmarks</h2>{_micro_table(report)}</section>
<section><h2>End-to-end case effects</h2><p class="small">Graph − Raw after taking the
median across eligible repetitions within each model and case.</p>
{_transfer_table(report)}</section>
<section><h2>End-to-end eligibility audit</h2>{_eligibility_table(report)}</section>
<section><h2>Adjudicated sensitivity analysis</h2><p class="small">The deterministic analysis
above is the headline result. This table applies only the separately reported semantic
adjudications.</p>{_transfer_table(report, sensitivity=True)}
{_eligibility_table(report, sensitivity=True)}</section>
<section class="split"><div><h2>Execution audit</h2><dl>
{_definition("Micro no-model gates", audit.get("micro_no_model_gates_passed"))}
{_definition("Transfer no-model gates", audit.get("transfer_no_model_gates_passed"))}
{_definition("Raw/Graph exact edge equality", audit.get("transfer_raw_graph_exact_edge_equality"))}
{_definition("Budget exhaustions", execution.get("budget_exhaustions"))}</dl></div>
<div><h2>Estimated provider cost</h2>{_cost_table(report)}</div></section>
<section><h2>Limits</h2><ul>{limits}</ul></section></main>"""


def _model_card(model, report):
    transfer = _mapping(report, "transfer")
    metrics = _mapping(transfer, "primary_metrics")
    rows = _mapping(metrics, "rows_returned")
    calls = _mapping(metrics, "correct_completion_tool_calls")
    validity = _mapping(transfer, "valid_completion")
    validity_summary = f"{validity.get('raw', 0)} Raw · {validity.get('semantic_graph', 0)} Graph"
    return f"""<article class="model-card"><h3>{_escape(model)}</h3>
{_metric("Transfer validity", validity_summary)}
{_metric("Eligible cases", rows.get("eligible_cases"))}
{_metric("Graph − Raw rows", _delta(rows.get("case_median_delta")))}
{_metric("Graph − Raw calls", _delta(calls.get("case_median_delta")))}
{_metric("Holm p (rows)", rows.get("holm_adjusted_p"))}</article>"""


def _micro_table(report):
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
            rows.append(
                (
                    model,
                    benchmark,
                    success.get("raw"),
                    success.get("semantic_graph"),
                    _delta(row_effect.get("median_delta")),
                    _delta(call_effect.get("median_delta")),
                    row_effect.get("eligible_cases"),
                )
            )
    return _html_table(
        ("Model", "Task", "Raw success", "Graph success", "Rows Δ", "Calls Δ", "Cases"),
        rows,
    )


def _transfer_table(report, *, sensitivity: bool = False):
    rows = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        analysis = _mapping(transfer, "adjudicated_sensitivity") if sensitivity else transfer
        for effect in _mapping_list(analysis, "case_effects"):
            rows.append(
                (
                    model,
                    effect.get("case_id"),
                    effect.get("eligible_repetitions"),
                    _delta(effect.get("rows_returned")),
                    _delta(effect.get("correct_completion_tool_calls")),
                )
            )
    return _html_table(
        ("Model", "Case", "Eligible reps", "Rows Δ", "Calls Δ"),
        rows,
    )


def _eligibility_table(report, *, sensitivity: bool = False):
    rows = []
    for model in report["model_order"]:
        transfer = _mapping(_mapping(_mapping(report, "model_reports"), model), "transfer")
        analysis = _mapping(transfer, "adjudicated_sensitivity") if sensitivity else transfer
        eligibility = _mapping(analysis, "efficiency_eligibility")
        by_treatment = _mapping(eligibility, "by_treatment")
        disposition = _mapping(eligibility, "paired_disposition")
        rejections = _mapping(eligibility, "claim_rejection_codes")
        rows.append(
            (
                model,
                by_treatment.get("raw"),
                by_treatment.get("semantic_graph"),
                disposition.get("both"),
                disposition.get("raw_only"),
                disposition.get("semantic_graph_only"),
                disposition.get("neither"),
                _compact_counts(_mapping(rejections, "raw")),
                _compact_counts(_mapping(rejections, "semantic_graph")),
            )
        )
    return _html_table(
        (
            "Model",
            "Raw eligible",
            "Graph eligible",
            "Both",
            "Raw only",
            "Graph only",
            "Neither",
            "Raw claim rejection codes",
            "Graph claim rejection codes",
        ),
        rows,
    )


def _compact_counts(values: Mapping[str, object]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in sorted(values.items())) or "—"


def _cost_table(report):
    costs = _mapping(report, "costs")
    rows = []
    for model in report["model_order"]:
        value = _mapping(_mapping(costs, "models"), model)
        rows.append((model, value.get("estimated_cost"), value.get("currency")))
    return _html_table(("Model", "Estimated cost", "Currency"), rows)


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
