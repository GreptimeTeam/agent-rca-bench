import copy
import json
import re
from pathlib import Path

import pytest

from semantic_rca_bench.formal_report import (
    build_formal_measurement_report,
    validate_formal_measurement_report,
)
from semantic_rca_bench.formal_report_view import (
    build_report_view_model,
    render_formal_measurement_report,
)
from semantic_rca_bench.formal_suite_protocol import (
    DEFAULT_SUITE_PROTOCOL_FIXTURE,
    load_formal_suite_protocol,
)

# Causal scope and mechanism of each v33 measurement case, in cohort order.
_COHORT = (
    ("component", "workload_restart"),
    ("component", "workload_restart"),
    ("component", "workload_restart"),
    ("component", "workload_restart"),
    ("dependency_edge", "call_path_delay"),
    ("dependency_edge", "call_path_delay"),
    ("dependency_edge", "call_path_delay"),
    ("component", "cpu_saturation"),
    ("component", "cpu_saturation"),
    ("component", "memory_pressure"),
    ("infrastructure_node", "cpu_saturation"),
    ("infrastructure_node", "memory_pressure"),
    ("infrastructure_node", "disk_io_degradation"),
    ("infrastructure_node", "host_unavailable"),
)


def _model_report(
    case_ids: list[str], *, storage_holm_adjusted_p: float = 1.0
) -> dict[str, object]:
    metric = {
        "eligible_cases": 8,
        "case_median_delta": -10,
        "negative_cases": 6,
        "tied_cases": 1,
        "positive_cases": 1,
        "sign_test_two_sided_p": 0.125,
        "holm_adjusted_p": 1.0,
        "multiplicity_family_size": 10,
    }
    analysis = {
        "diagnosis_correct": {"split_pillars": 11, "raw": 18, "semantic_graph": 19},
        "valid_completion": {"split_pillars": 11, "raw": 18, "semantic_graph": 19},
        "efficiency_eligibility": {
            "by_treatment": {"split_pillars": 11, "raw": 18, "semantic_graph": 19},
            "paired_disposition": {
                "both": 17,
                "raw_only": 1,
                "semantic_graph_only": 2,
                "neither": 0,
            },
            "claim_rejection_codes": {
                "raw": {"baseline:time_coverage_incomplete": 1},
                "semantic_graph": {},
            },
        },
        "case_effects": [
            {
                "case_id": case_id,
                "eligible_repetitions": 2,
                "rows_returned": -10,
                "correct_completion_tool_calls": 0,
                "reported_total_tokens": -1000,
            }
            for case_id in case_ids
        ],
        "primary_metrics": {
            "rows_returned": metric,
            "correct_completion_tool_calls": {**metric, "case_median_delta": 0},
        },
        "descriptive_metrics": {"reported_total_tokens": {**metric, "case_median_delta": -1000}},
    }
    analysis["confirmatory_families"] = {
        "semantic_layer": {
            "comparison": "semantic_graph - raw",
            "primary_metrics": analysis["primary_metrics"],
            "case_effects": [
                {
                    "case_id": case_id,
                    "eligible_repetitions": 2,
                    "rows_returned": -10,
                    "correct_completion_tool_calls": 0,
                    "provider_visible_input_tokens": -100,
                }
                for case_id in case_ids
            ],
        },
        "storage_shape": {
            "comparison": "raw - split_pillars",
            "primary_metrics": {
                "correct_completion_tool_calls": {
                    **metric,
                    "holm_adjusted_p": storage_holm_adjusted_p,
                },
                "provider_visible_input_tokens": {
                    **metric,
                    "case_median_delta": -100,
                    "holm_adjusted_p": storage_holm_adjusted_p,
                },
            },
            # Rows are null against the split stack: not comparable, not zero.
            "case_effects": [
                {
                    "case_id": case_id,
                    "eligible_repetitions": 2,
                    "rows_returned": None,
                    "correct_completion_tool_calls": -2,
                    "provider_visible_input_tokens": -100,
                }
                for case_id in case_ids
            ],
        },
    }
    return {
        # 14 cases x 2 repetitions x 2 treatments
        "runs": 56,
        **analysis,
        "evidence_quality": {
            "role": "secondary deterministic evidence-sufficiency audit",
            "required_evidence_covered_by_treatment": {"raw": 8, "semantic_graph": 9},
            "paired_disposition": {
                "both": 7,
                "raw_only": 1,
                "semantic_graph_only": 2,
                "neither": 10,
            },
        },
        "reliability": {
            "runner_errors": 0,
            "budget_exhaustions": 0,
            "failed_database_queries": 1,
        },
        "usage": {
            "estimated_cost": 2.5,
            "cost_currency": "USD",
            "cache_breakdown_complete": True,
            "provider_visible_input_tokens": 200,
            "uncached_input_tokens": 100,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 20,
            "output_tokens": 50,
            "reasoning_output_tokens": 10,
        },
    }


def _tool_calls(visibility: str) -> list[dict[str, object]]:
    """One trajectory per run, shaped so every audit counter has a distinct value.

    Each arm issues a successful and a failed call, exactly one join, and one
    PromQL-evaluating operation, so a counter that silently counts failures or
    non-evaluating operations changes a number the test pins.
    """
    if visibility == "split_pillars":
        return [
            {"tool_name": "query_metrics", "error": False, "input": {"operation": "query_range"}},
            {"tool_name": "query_metrics", "error": True, "input": {"operation": "query"}},
            {"tool_name": "query_logs", "error": False, "input": {"query": '{app="x"}'}},
        ]
    calls: list[dict[str, object]] = [
        {
            "tool_name": "execute_sql",
            "error": False,
            "input": {"query": "SELECT * FROM a JOIN b ON a.id = b.id"},
        },
        # Failed, and it contains JOIN: a counter that ignores `error` overcounts.
        {"tool_name": "execute_sql", "error": True, "input": {"query": "SELECT * FROM c JOIN d"}},
        # `series` does not evaluate PromQL and must not be counted as such.
        {"tool_name": "query_metrics", "error": False, "input": {"operation": "series"}},
        {"tool_name": "query_metrics", "error": False, "input": {"operation": "query_range"}},
    ]
    if visibility == "semantic_graph":
        calls.append(
            {"tool_name": "query_semantic_graph", "error": False, "input": {"view": "entities"}}
        )
    return calls


def _report(
    *,
    transfer_graph_run_cost: float = 0.08,
    drop_last_transfer_run: bool = False,
    storage_holm_adjusted_p: float = 1.0,
    currency_by_model: dict[str, str] | None = None,
    unpriced_cells: set[tuple[str, str]] | None = None,
) -> dict[str, object]:
    suite, protocol = load_formal_suite_protocol()
    names = [model.model for model in protocol.models]
    case_ids = [f"semantic-rca-transfer-{index:03d}" for index in range(1, 15)]
    micro = {
        "license": {"source": "source terms"},
        "sources": [
            {"no_model_gates": {"all_passed": True}} for _ in range(len(suite.micro_cases))
        ],
        "runs": [
            {
                "model": name,
                "benchmark": "discovery" if case_index < 6 else "graph",
                "source_case": f"micro-case-{case_index}",
                "repetition": repetition,
                "visibility": visibility,
                "runner_error": False,
                "tool_budget_exhausted": False,
                "evaluation": {"success": True},
                "usage": {
                    "uncached_input_tokens": 10 if visibility == "raw" else 8,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 4 if visibility == "raw" else 3,
                    "reasoning_output_tokens": 2,
                    "estimated_cost": 0.1 if visibility == "raw" else 0.08,
                    "cost_currency": "USD",
                },
            }
            for name in names
            for case_index in range(8)
            for repetition in range(2)
            for visibility in ("raw", "semantic_graph")
        ],
        "model_reports": {
            name: {
                "benchmarks": {
                    benchmark: {
                        "treatment_success": {"raw": 15, "semantic_graph": 16},
                        "case_level_effect": {
                            "rows_returned_through_evidence": {
                                "eligible_cases": 4,
                                "median_delta": -12,
                                "improvements": 3,
                            },
                            "tool_calls_through_evidence": {
                                "eligible_cases": 4,
                                "median_delta": -1,
                                "improvements": 4,
                            },
                        },
                    }
                    for benchmark in ("discovery", "graph")
                },
                "reliability": {
                    "runs": 32,
                    "runner_errors": 0,
                    "budget_exhaustions": 0,
                },
                "usage": {
                    "estimated_cost": 1.5,
                    "cost_currency": "USD",
                    "cache_breakdown_complete": True,
                    "uncached_input_tokens": 40,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 5,
                },
            }
            for name in names
        },
    }
    transfer = {
        "license": {"source": "source terms"},
        "benchmark_protocol": {
            "treatment_estimand": "semantic_graph - raw",
            "treatment_components": {},
        },
        "bindings": {
            "protocol_fixture_sha256": suite.transfer_protocol_fixture_sha256,
        },
        "inference": {"independent_unit": "case", "holm_family_size": len(case_ids)},
        "sources": [
            {
                "opaque_case_id": case_id,
                "source_case": f"source-{index}",
                "system": "test-system",
                "mechanism_code": _COHORT[index - 1][1],
                "causal_scope": _COHORT[index - 1][0],
                "causal_component": (
                    None if _COHORT[index - 1][0] == "dependency_edge" else "target-service"
                ),
                "edge_source": ("caller" if _COHORT[index - 1][0] == "dependency_edge" else None),
                "edge_destination": (
                    "callee" if _COHORT[index - 1][0] == "dependency_edge" else None
                ),
                "mechanism_evidence": {
                    "predicate": "threshold_transition",
                    "source_table": "test_metric",
                    "value_column": "greptime_value",
                    "threshold": 1,
                    "allowed_operations": [],
                    "normal": {"count": 10},
                    "abnormal": {"count": 10},
                },
                "oracle": {
                    "predicate": "threshold_transition",
                    "source_table": "test_metric",
                    "value_column": "greptime_value",
                    "threshold": 1,
                    "allowed_operations": [],
                    "normal_samples": 10,
                    "abnormal_samples": 10,
                },
                "no_model_gates": {"all_passed": True},
                "edge_equality": {"exact_edge_set_equality": True},
            }
            for index, case_id in enumerate(case_ids, 1)
        ],
        "runs": [
            {
                "model": name,
                "case_id": case_id,
                "repetition": repetition,
                "visibility": visibility,
                "run": {
                    "execution": {
                        "runner_error": False,
                        "tool_budget_exhausted": False,
                    },
                    "evaluation": {
                        "causal_scope_match": True,
                        "causal_locus_match": True,
                        "fault_category_match": True,
                        "mechanism_code_match": True,
                        "required_evidence_covered": True,
                        "execution_reliability": True,
                        "valid_evidence_count": 1,
                        "diagnosis_correct": True,
                        "efficiency_eligible": True,
                        "correct_completion_tool_calls": 5,
                    },
                    # Null in the split arm: a returned row there is not a
                    # database row.
                    "database_load": {
                        "rows_returned": (
                            None
                            if visibility == "split_pillars"
                            else 100
                            if visibility == "raw"
                            else 90
                        )
                    },
                    "usage": {
                        "provider_visible_input_tokens": 100 if visibility == "raw" else 90,
                        "output_tokens": 20 if visibility == "raw" else 15,
                        "reasoning_output_tokens": 3 if visibility == "raw" else 2,
                        "estimated_cost": (
                            None
                            if (unpriced_cells or set()) & {(name, visibility)}
                            else 0.1
                            if visibility == "raw"
                            else transfer_graph_run_cost
                        ),
                        "cost_currency": (currency_by_model or {}).get(name, "USD"),
                    },
                    "tool_calls": _tool_calls(visibility),
                },
            }
            for name in names
            for case_id in case_ids
            for repetition in range(2)
            for visibility in ("split_pillars", "raw", "semantic_graph")
        ],
        "model_reports": {
            name: _model_report(
                case_ids,
                storage_holm_adjusted_p=storage_holm_adjusted_p,
            )
            for name in names
        },
    }
    if drop_last_transfer_run:
        transfer["runs"].pop()
    return build_formal_measurement_report(
        micro,
        transfer,
        DEFAULT_SUITE_PROTOCOL_FIXTURE,
        source_artifacts={
            "micro": {"sha256": "a" * 64},
            "transfer": {"sha256": "b" * 64},
        },
    )


def test_formal_measurement_report_combines_current_public_artifacts(tmp_path: Path) -> None:
    report = _report()
    validate_formal_measurement_report(report)

    assert report["execution"] == {
        "expected_cells": 464,
        "completed_cells": 464,
        "micro_cells": 128,
        "transfer_cells": 336,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "models": 4,
        "micro_cases": 8,
        "transfer_cases": 14,
        "treatments": ["split_pillars", "raw", "semantic_graph"],
        "repetitions_per_model_case": 2,
    }
    assert report["cohort_provenance"] == {
        "micro": [
            {"benchmark": "discovery", "dataset": "openrca", "cases": 6},
            {"benchmark": "graph", "dataset": "openrca2", "cases": 2},
        ],
        "transfer": [
            {"dataset": "openrca2", "cases": 10},
            {"dataset": "rca100", "cases": 4},
        ],
    }
    # "fresh" would misdescribe a cohort that carries ten cases over.
    assert report["limitations"][0] == (
        "The 8-case micro cohort is fixed reference data; the 14-case end-to-end cohort is "
        "source-ranked. Ten of its cases carry over from the previously published "
        "measurement rather than being reselected."
    )
    # Located by content, so inserting a limitation does not silently move which
    # sentence this test is checking.
    limitations = report["limitations"]
    assert next(item for item in limitations if item.startswith("Mechanism cohorts")) == (
        "Mechanism cohorts are unevenly sized: Workload restart 4, Call-path delay 3, "
        "CPU saturation 3, Memory pressure 2, Disk I/O degradation 1, Host unavailable 1. "
        "Mechanism-level summaries are descriptive and unevenly supported."
    )
    # The eligible-case span is the sample the tests ran on, not the cohort size,
    # and it is derived so it cannot keep quoting a range the data no longer has.
    assert next(item for item in limitations if "independent cases" in item) == (
        "The study has 14 independent cases, and endpoint eligibility leaves only 8-8 cases "
        "for an individual test. Exact sign tests on that many cases have low and discrete "
        "power, so a non-significant result indicates insufficient evidence and does not "
        "establish equivalence."
    )
    costs = report["costs"]
    assert costs["models"] == {
        model: {
            "status": "available",
            "estimated_cost": 4.0,
            "currency": "USD",
            "unavailable_reason_code": None,
            "unavailable_reason": None,
        }
        for model in report["model_order"]
    }
    assert costs["known_totals_by_currency"] == {"USD": 16.0}
    assert costs["models_with_unavailable_estimate"] == []
    # Every model priced in one currency, so the total needs no conversion.
    assert costs["cross_currency_total"] == {
        "currency": "USD",
        "amount": 16.0,
        "exchange_rates": {"USD": {"per_unit_usd": 1.0, "checked_at": None, "source": None}},
    }
    assert costs["pricing_basis"]["gpt-5.6-sol"]["output_per_million"] == 20.0
    assert report["case_catalog"][0]["mechanism_code"] == "workload_restart"
    assert report["case_outcomes"][0]["models_with_fewer_rows"] == 4
    assert report["case_outcomes"][0]["models_with_fewer_input_tokens"] == 4
    assert report["case_outcomes"][0]["models_with_fewer_output_tokens"] == 4
    assert report["case_outcomes"][0]["models_with_lower_estimated_cost"] == 4
    assert report["case_outcomes"][0]["models_with_estimable_cost"] == 4
    capability = report["capability_scores"]["models"]["gpt-5.6-sol"]["overall"]
    # 85 of 85: the deterministic proof is reported beside the score, not in it.
    assert capability["score"] == 85
    assert capability["normalized_score"] == 100.0
    assert capability["unscored_dimensions"]["required_evidence_covered"] == {
        "covered": capability["runs"]
    }
    transfer_metrics = report["model_reports"]["gpt-5.6-sol"]["transfer"]["descriptive_metrics"]
    assert transfer_metrics["provider_visible_input_tokens"]["case_median_delta"] == -10
    assert transfer_metrics["output_tokens"]["case_median_delta"] == -5
    assert transfer_metrics["estimated_cost"]["case_median_delta"] == pytest.approx(-0.02)
    transfer_report = report["model_reports"]["gpt-5.6-sol"]["transfer"]
    assert transfer_report["actual_cost_by_treatment"] == pytest.approx(
        {"raw": 2.8, "semantic_graph": 2.24}
    )
    assert transfer_report["case_effects"][0]["actual_cost_raw"] == pytest.approx(0.2)
    assert transfer_report["case_effects"][0]["actual_cost_semantic_graph"] == pytest.approx(0.16)
    assert report["model_reports"]["gpt-5.6-sol"]["usage"]["combined"][
        "actual_cost_by_treatment"
    ] == pytest.approx({"raw": 4.4, "semantic_graph": 3.52})
    discovery_resources = report["model_reports"]["gpt-5.6-sol"]["micro"]["benchmarks"][
        "discovery"
    ]["resource_effects"]["summary"]
    assert discovery_resources["provider_visible_input_tokens"]["case_median_delta"] == -2
    assert discovery_resources["output_tokens"]["case_median_delta"] == -1
    assert report["model_reports"]["gpt-5.6-sol"]["usage"]["combined"] == {
        "provider_visible_input_tokens": 300,
        "uncached_input_tokens": 140,
        "cache_read_input_tokens": 130,
        "cache_creation_input_tokens": 30,
        "output_tokens": 80,
        "reasoning_output_tokens": 15,
        "input_breakdown_complete": True,
        "reasoning_is_subset_of_output": True,
        "actual_cost_by_treatment": {"raw": 4.4, "semantic_graph": 3.5199999999999996},
        "cost_currency": "USD",
    }
    audit = report["tool_use_audit"]
    # 14 cases x 2 repetitions x 4 models. Only the successful join counts, and
    # `series` is not a PromQL evaluation.
    assert audit["by_treatment"]["raw"] == {
        "runs": 112,
        "tool_calls": {"execute_sql": 224, "query_metrics": 224},
        "successful_tool_calls": {"execute_sql": 112, "query_metrics": 224},
        "successful_sql_join_calls": 112,
        "runs_with_successful_sql_join": 112,
        # The fixture joins two metric tables, so no join spans signal kinds.
        "successful_cross_signal_join_calls": 0,
        "runs_with_successful_cross_signal_join": 0,
        "runs_with_successful_promql_evaluation": 112,
    }
    assert audit["by_treatment"]["split_pillars"]["successful_sql_join_calls"] == 0
    assert audit["by_treatment"]["split_pillars"]["runs_with_successful_promql_evaluation"] == 112
    assert audit["semantic_graph_tool"] == {
        "runs": 112,
        "runs_with_successful_call": 112,
        "successful_calls": 112,
    }

    # The cohort spans two sources; the split is what makes a reversal visible.
    assert report["diagnosis_by_dataset"] == {
        "openrca2": {
            "cases": 10,
            "runs": {"split_pillars": 80, "raw": 80, "semantic_graph": 80},
            "diagnosis_correct": {"split_pillars": 80, "raw": 80, "semantic_graph": 80},
        },
        "rca100": {
            "cases": 4,
            "runs": {"split_pillars": 32, "raw": 32, "semantic_graph": 32},
            "diagnosis_correct": {"split_pillars": 32, "raw": 32, "semantic_graph": 32},
        },
    }
    assert {case["dataset"] for case in report["case_catalog"]} == {"openrca2", "rca100"}


def test_view_model_states_the_verdict_and_the_strip_geometry() -> None:
    view = build_report_view_model(_report(storage_holm_adjusted_p=0.01))

    verdicts = {item["goal"]: item for item in view["verdicts"]}
    # Case medians agreeing is a direction, not a result: without correction the
    # family is simply unsupported.
    assert verdicts["semantic_layer"]["status"] == "not_supported"
    assert verdicts["semantic_layer"]["endpoints"] == {
        "total": 8,
        "significant": 0,
        "favouring_treatment": 8,
        "favouring_baseline": 0,
        "family_size": 10,
    }
    assert verdicts["semantic_layer"]["tally_text"]["en"] == (
        "No pre-specified endpoint of 8 survived Holm correction"
    )
    assert verdicts["semantic_layer"]["comparison"] == "Graph − Raw"
    # Every storage endpoint is significant in this fixture, so the whole family
    # is supported rather than partly supported.
    assert verdicts["storage_shape"]["status"] == "supported"
    assert verdicts["storage_shape"]["endpoints"]["significant"] == 8
    ranking = verdicts["model_ranking"]
    assert ranking["status"] == "descriptive"
    assert ranking["endpoints"] is None
    assert "no significance is claimed" in ranking["tally_text"]["en"]

    narrative = view["narrative"]["en"]
    assert narrative["families"]["semantic_layer"] == (
        "None of the 10 Graph − Raw tests is significant after Holm correction."
    )
    assert "Raw used fewer" in narrative["families"]["storage_shape"]
    # Effect size and case counts come before the p values.
    storage_finding = narrative["families"]["storage_shape"]
    assert "in 6 of 8 eligible cases" in storage_finding
    assert storage_finding.index("case median") < storage_finding.index("Exact sign p")
    assert "adjusted p 0.01" in storage_finding
    assert view["narrative"]["zh"]["families"]["semantic_layer"] == (
        "Graph − Raw 的 10 项检验，经 Holm 校正后没有一项显著。"
    )
    assert "Discovery 12/16" in narrative["micro_row_reduction"]

    strips = {
        (item["model"], item["family"], item["metric"]): item
        for item in view["charts"]["delta_strips"]
    }
    # Rows returned is not comparable against the split stack, so it is absent
    # from that family rather than plotted as a zero delta.
    assert ("gpt-5.6-sol", "storage_shape", "rows_returned") not in strips
    strip = strips[("gpt-5.6-sol", "semantic_layer", "rows_returned")]
    assert strip["favors"] == {"negative": "Graph", "positive": "Raw"}
    assert len(strip["points"]) == 14
    assert all(-1.0 <= point["position"] <= 0.0 for point in strip["points"])
    assert strip["counts"] == {"eligible": 8, "negative": 6, "positive": 1, "tied": 1}
    # Equal magnitudes must land on the same position for the axis to be readable.
    assert len({point["position"] for point in strip["points"]}) == 1
    assert strip["median"]["position"] == pytest.approx(-1.0)
    # knee is 1 here, so the decade ticks run from 1 to the widest magnitude.
    assert [tick["value"] for tick in strip["axis"]["ticks"]] == [-10.0, -1.0, 0.0, 1.0, 10.0]


def test_view_model_merges_actual_cost_from_both_families() -> None:
    # Each family reports only the arms it compares, so the split arm's spend is
    # absent from the semantic-layer family and would otherwise read as unpriced.
    series = {
        item["model"]: item
        for item in build_report_view_model(_report())["charts"]["cost_bars"]["series"]
    }
    entry = series["gpt-5.6-sol"]
    assert set(entry["values"]) == {"split_pillars", "raw", "semantic_graph"}
    assert all(value is not None for value in entry["values"].values())
    assert entry["estimable"] is True


def test_rendered_page_inlines_every_payload_and_leaks_nothing(tmp_path: Path) -> None:
    report = _report()
    output = tmp_path / "report.html"
    render_formal_measurement_report(report, output)
    document = output.read_text()

    assert "__REPORT_" not in document
    assert "/Users/" not in document
    assert "private/tmp" not in document
    assert 'href="semantic-rca-v34.json"' not in document  # emitted by the renderer, not the shell

    payloads = {
        name: json.loads(
            re.search(
                rf'<script type="application/json" id="{name}">(.*?)</script>',
                document,
                re.S,
            )
            .group(1)
            .replace("<\\/", "</")
        )
        for name in ("semantic-rca-report", "semantic-rca-view", "semantic-rca-i18n")
    }
    assert payloads["semantic-rca-report"]["report_schema_version"] == 7
    assert payloads["semantic-rca-view"]["report_json_filename"] == "semantic-rca-v34.json"
    assert set(payloads["semantic-rca-i18n"]["en"]) == set(payloads["semantic-rca-i18n"]["zh"])

    # Every key the renderer asks for must exist, or the page prints the key.
    referenced = set(re.findall(r'(?<![A-Za-z0-9_])t\("([a-z][a-z0-9_.]*)"', document))
    assert referenced
    assert referenced <= set(payloads["semantic-rca-i18n"]["en"])

    # The no-JavaScript summary carries the same findings the page leads with.
    summary = re.search(r'<div id="fallback">(.*?)</div>\s*<noscript>', document, re.S).group(1)
    view = build_report_view_model(report)
    for takeaway in view["takeaways"]:
        headline = view["narrative"]["en"]["takeaways"][takeaway["id"]]["headline"]
        assert headline in summary
    assert "REPORT.md" in summary
    assert str(report["execution"]["completed_cells"]) in summary


def test_rendered_page_reports_missing_i18n_keys(tmp_path: Path, monkeypatch) -> None:
    import semantic_rca_bench.formal_report_view as view_module

    monkeypatch.setattr(
        view_module, "_load_i18n", lambda assets: (_ for _ in ()).throw(ValueError("boom"))
    )
    with pytest.raises(ValueError, match="boom"):
        render_formal_measurement_report(_report(), tmp_path / "report.html")


def test_incomplete_artifacts_fail_before_pair_aggregation() -> None:
    with pytest.raises(ValueError, match="formal measurement artifacts are incomplete"):
        _report(drop_last_transfer_run=True)


def test_treatment_ranking_omits_a_model_without_a_score() -> None:
    from semantic_rca_bench.formal_report import _score_ranking

    reports = {
        "measured": {"by_treatment": {"split_pillars": {"normalized_score": 50.0}}},
        "missing": {"by_treatment": {"split_pillars": {"normalized_score": None}}},
    }

    assert _score_ranking(reports, "split_pillars") == [
        {"rank": 1, "model": "measured", "score": 50.0}
    ]


def test_cost_direction_counts_models_instead_of_claiming_a_reduction() -> None:
    # Graph costs more per run than Raw here, so nothing got cheaper on that
    # comparison and the sentence must say so.
    narrative = build_report_view_model(_report(transfer_graph_run_cost=0.12))["narrative"]
    assert narrative["en"]["cost_direction"]["semantic_layer"] == (
        "0 of 4 priced models spent less through Graph than through Raw."
    )
    assert narrative["en"]["cost_direction"]["storage_shape"] == (
        "4 of 4 priced models spent less through Raw than through Split."
    )
    assert narrative["zh"]["cost_direction"]["semantic_layer"] == (
        "4 个可计价模型中，0 个使用 Graph 的成本低于 Raw。"
    )

    cheaper = build_report_view_model(_report(transfer_graph_run_cost=0.05))
    assert cheaper["narrative"]["en"]["cost_direction"]["semantic_layer"] == (
        "4 of 4 priced models spent less through Graph than through Raw."
    )


def test_a_significant_interface_result_reaches_the_headline() -> None:
    view = build_report_view_model(_report(storage_holm_adjusted_p=0.01))

    assert view["narrative"]["en"]["headline"] == (
        "8 interface endpoints are significant; Graph E2E varies"
    )
    assert (
        "Raw used fewer provider-visible input tokens than Split"
        in view["narrative"]["en"]["conclusion"]
    )
    assert "接口组合检验族中通过校正的结果" in view["narrative"]["zh"]["conclusion"]


def test_formal_measurement_report_rejects_tampered_summary() -> None:
    report = _report()
    tampered = copy.deepcopy(report)
    tampered["execution"]["completed_cells"] = 431

    with pytest.raises(ValueError, match="execution is incomplete"):
        validate_formal_measurement_report(tampered)


def test_an_unmeasurable_evidence_dimension_does_not_cap_a_treatment() -> None:
    from semantic_rca_bench.formal_report import _score_runs

    def run(required_evidence_covered):
        return {
            "run": {
                "evaluation": {
                    "causal_scope_match": True,
                    "causal_locus_match": True,
                    "fault_category_match": True,
                    "mechanism_code_match": True,
                    "valid_evidence_count": 1,
                    "required_evidence_covered": required_evidence_covered,
                }
            }
        }

    audited = _score_runs([run(True)])
    not_estimable = _score_runs([run(None)])
    failed = _score_runs([run(False)])

    # The deterministic proof is decidable only for SQL evidence, so it is
    # reported beside the score rather than inside it. Scoring it would both
    # penalise an arm the verifier cannot read and reward a model for citing a
    # language the verifier cannot check.
    assert audited["normalized_score"] == 100.0
    assert not_estimable["normalized_score"] == 100.0
    assert failed["normalized_score"] == 100.0
    assert {r["maximum_points"] for r in (audited, not_estimable, failed)} == {85}
    assert audited["unscored_dimensions"]["required_evidence_covered"] == {"covered": 1}
    assert not_estimable["unscored_dimensions"]["required_evidence_covered"] == {"not_estimable": 1}
    assert failed["unscored_dimensions"]["required_evidence_covered"] == {"failed": 1}


def test_no_post_hoc_grade_survives_beside_the_registered_test() -> None:
    """Only Holm correction decides an endpoint; nothing softer may reappear.

    An earlier draft added a "directional" tier whose cutoffs were picked after
    seeing how the endpoints landed. This pins the vocabulary so that tier cannot
    come back through the view model.
    """
    import semantic_rca_bench.formal_report_view as view_module

    assert not hasattr(view_module, "_evidence_grade")
    assert not hasattr(view_module, "DIRECTIONAL_MINIMUM_CASES")
    assert not hasattr(view_module, "DIRECTIONAL_AGREEMENT")

    view = build_report_view_model(_report(storage_holm_adjusted_p=1.0))
    assert view["evidence_rule"] == {"alpha": 0.05}
    statuses = {item["goal"]: item["status"] for item in view["verdicts"]}
    assert statuses["semantic_layer"] == "not_supported"
    assert statuses["storage_shape"] == "not_supported"
    assert {t["grade"] for t in view["takeaways"]} <= {"confirmed", "not_confirmed", "descriptive"}

    # A family where every endpoint passes is supported; one that passes some is partial.
    assert (
        build_report_view_model(_report(storage_holm_adjusted_p=0.01))["verdicts"][1]["status"]
        == "supported"
    )


def test_rejection_codes_separate_claiming_citations_from_the_rest() -> None:
    """A code on a citation that never claimed the mechanism is verifier reach, not a failure.

    Counting both together made a run look like it failed far more checks than it
    attempted, which is how the published tally read before this split.
    """
    report = _report()
    audit = report["claim_rejection_audit"]
    assert audit["counting_unit"] == "citation"
    # The fixture cites nothing, so both sides are empty rather than absent.
    assert audit["by_code"] == {}

    submission = report["citation_submission"]["by_model"]
    assert set(submission) == set(report["model_order"])
    for counts in submission.values():
        assert counts["runs"] == 84
        assert counts["runs_without_citation"] == 84
        assert counts["correct_diagnoses_lost_to_missing_citation"] == 84


def test_a_model_that_always_cites_produces_no_citation_warning() -> None:
    from semantic_rca_bench.formal_report_view import _citation_submission_text

    report = copy.deepcopy(_report())
    for counts in report["citation_submission"]["by_model"].values():
        counts["runs_without_citation"] = 0
        counts["correct_diagnoses_lost_to_missing_citation"] = 0
    assert _citation_submission_text(report, "en") is None

    report["citation_submission"]["by_model"]["glm-5.3"].update(
        {"runs_without_citation": 24, "correct_diagnoses_lost_to_missing_citation": 13}
    )
    text = _citation_submission_text(report, "en")
    assert "24 of its 84 runs" in text
    assert "13 of which" in text
    assert _citation_submission_text(report, "zh").startswith("glm-5.3 的 84 次 run 里有 24 次")


def test_spend_in_two_currencies_converts_at_the_published_rate() -> None:
    """A single cost row requires conversion, and conversion requires a rate.

    Adding USD to CNY at 1:1 silently produced a single number before. The fix
    is not to refuse the total but to convert at a rate the page publishes, so a
    reader can check the arithmetic and see it is a market quote, not a
    measurement. The per-currency subtotals stay in the record either way.
    """
    from semantic_rca_bench.formal_report import EXCHANGE_RATES_TO_USD

    report = _report(currency_by_model={"glm-5.3": "CNY"})
    usage = report["usage_by_treatment"]
    raw = usage["by_treatment"]["raw"]["estimated_cost_by_currency"]
    assert set(raw) == {"USD", "CNY"}
    assert usage["comparable_currency"] is None
    assert usage["priced_models"]["glm-5.3"] == "CNY"

    # The billed currency is what the record keeps; USD is derived from it.
    rate = EXCHANGE_RATES_TO_USD["CNY"]["units_per_usd"]
    assert usage["estimated_cost_usd"]["raw"] == pytest.approx(raw["USD"] + raw["CNY"] / rate)
    assert usage["exchange_rates"]["CNY"]["checked_at"]
    assert usage["exchange_rates"]["CNY"]["source"]

    rows = {row["id"]: row for row in build_report_view_model(report)["charts"]["headline"]["rows"]}
    assert set(rows) == {"accuracy", "cost", "input_tokens"}
    cost = rows["cost"]
    assert cost["currency"] == "USD"
    assert cost["values"]["raw"] == pytest.approx(usage["estimated_cost_usd"]["raw"])
    # The page has to say which currency was converted and at what rate.
    assert cost["converted_from"] == ["CNY"]
    assert cost["exchange_rates"]["CNY"]["units_per_usd"] == rate
    assert "glm-5.3" in cost["covered_models"]


def test_a_currency_without_a_frozen_rate_fails_rather_than_converting() -> None:
    """An unknown currency has no rate, and guessing one would invent spend."""
    from semantic_rca_bench.formal_report import _usage_by_treatment

    runs = [
        {
            "model": "model-a",
            "visibility": "raw",
            "run": {
                "usage": {
                    "provider_visible_input_tokens": 10,
                    "output_tokens": 1,
                    "estimated_cost": 5.0,
                    "cost_currency": "JPY",
                }
            },
        }
    ]

    with pytest.raises(ValueError, match="no frozen exchange rate"):
        _usage_by_treatment(runs)


def test_a_model_priced_in_only_some_arms_is_excluded_entirely() -> None:
    """Partial pricing would make the unpriced arm look cheap.

    Keeping the model's other arms in the total is what creates that illusion,
    so the whole model leaves the comparison.
    """
    priced = _report()["usage_by_treatment"]
    partial = _report(unpriced_cells={("glm-5.3", "semantic_graph")})["usage_by_treatment"]

    assert "glm-5.3" in priced["priced_models"]
    assert "glm-5.3" in partial["unpriced_models"]
    assert "glm-5.3" not in partial["priced_models"]
    # Its raw spend must leave with it, not linger in the raw subtotal.
    assert (
        partial["by_treatment"]["raw"]["estimated_cost_by_currency"]["USD"]
        < priced["by_treatment"]["raw"]["estimated_cost_by_currency"]["USD"]
    )


def test_every_headline_ratio_is_the_arm_over_the_best_arm() -> None:
    """One convention, so the number and the bar cannot disagree.

    Accuracy previously used best/arm while cost used arm/best, which put 1.33
    beside a bar drawn at 75%.
    """
    chart = build_report_view_model(_report())["charts"]["headline"]
    for row in chart["rows"]:
        values = {t: v for t, v in row["values"].items() if v}
        if not values:
            continue
        reference = max(values.values()) if row["better"] == "higher" else min(values.values())
        for treatment, value in values.items():
            assert row["ratios"][treatment] == pytest.approx(value / reference)
        # The best arm sits at exactly 1, whichever direction is better.
        best = (
            max(values, key=values.get)
            if row["better"] == "higher"
            else min(values, key=values.get)
        )
        assert row["ratios"][best] == pytest.approx(1.0)
        # A longer bar always carries a larger number.
        ordered = sorted(values, key=lambda t: row["fractions"][t])
        assert [values[t] for t in ordered] == sorted(values.values())


def test_no_headline_row_claims_to_be_a_registered_endpoint() -> None:
    """The registered token endpoint is paired and filtered; these totals are not.

    Labelling the workload total as registered overstated what the page proves.
    """
    chart = build_report_view_model(_report())["charts"]["headline"]
    assert all("registered" not in row for row in chart["rows"])
    rendered = Path("src/semantic_rca_bench/assets/report/report.js").read_text()
    assert "row.registered" not in rendered


def test_an_all_zero_row_reports_no_ratio_instead_of_failing() -> None:
    """A cohort where nothing was correct is legal input, not a crash."""
    from semantic_rca_bench.formal_report_view import _headline_bars

    report = copy.deepcopy(_report())
    for model in report["model_reports"].values():
        for treatment in model["transfer"]["diagnosis_correct"]:
            model["transfer"]["diagnosis_correct"][treatment] = 0
    for treatment in report["usage_by_treatment"]["by_treatment"].values():
        treatment["estimated_cost_by_currency"] = {"USD": 0.0}
    report["usage_by_treatment"]["estimated_cost_usd"] = dict.fromkeys(
        report["usage_by_treatment"]["estimated_cost_usd"], 0.0
    )

    rows = {row["id"]: row for row in _headline_bars(report)["rows"]}
    # An all-zero row is measured, not missing: every arm scored nothing. The
    # row plots, names a best arm, and reports no ratio because dividing by
    # zero has no meaning.
    assert rows["accuracy"]["estimable"] is True
    assert set(rows["accuracy"]["values"].values()) == {0}
    assert set(rows["accuracy"]["ratios"].values()) == {None}
    assert rows["accuracy"]["best"] is not None
    assert rows["cost"]["estimable"] is True
    assert set(rows["cost"]["ratios"].values()) == {None}
    # A row that still has values keeps working alongside the empty ones.
    assert rows["input_tokens"]["estimable"] is True


def test_a_zero_arm_is_measured_not_missing() -> None:
    """Zero is an answer. Filtering it out printed N/A where the value was 0.

    An arm that got nothing right still sits on the axis, and it must not drop
    out of the reference either: on a lower-is-better row a zero arm is the
    cheapest one, and skipping it would crown the second-cheapest.
    """
    from semantic_rca_bench.formal_report_view import _headline_bars

    report = copy.deepcopy(_report())
    for model in report["model_reports"].values():
        model["transfer"]["diagnosis_correct"]["split_pillars"] = 0

    accuracy = {row["id"]: row for row in _headline_bars(report)["rows"]}["accuracy"]
    assert accuracy["estimable"] is True
    assert accuracy["values"]["split_pillars"] == 0
    # 0.0, not None: the page prints x0.00 rather than N/A.
    assert accuracy["ratios"]["split_pillars"] == 0.0
    assert accuracy["fractions"]["split_pillars"] == 0.0
    assert accuracy["best"] == "semantic_graph"
    assert accuracy["ratios"][accuracy["best"]] == 1.0


def test_a_zero_reference_names_the_best_arm_without_dividing() -> None:
    """A free arm is the cheapest arm, and no other arm has a multiple of it."""
    from semantic_rca_bench.formal_report_view import _headline_bars

    report = copy.deepcopy(_report())
    report["usage_by_treatment"]["estimated_cost_usd"]["raw"] = 0.0

    cost = {row["id"]: row for row in _headline_bars(report)["rows"]}["cost"]
    # The zero arm wins a lower-is-better row; it used to be skipped entirely.
    assert cost["best"] == "raw"
    assert cost["reference"] == 0.0
    # Every ratio is undefined against a zero reference, and the row says so
    # instead of dividing. The values themselves still plot.
    assert set(cost["ratios"].values()) == {None}
    assert cost["estimable"] is True
    assert cost["fractions"]["split_pillars"] == 1.0


def test_every_lookup_the_renderer_makes_resolves_in_the_view_model() -> None:
    """The renderer indexes into the view model; a missing key blanks the page.

    A cost chart entry once pointed at an exchange rate that was not in the
    table, and the resulting TypeError took down every section while the whole
    suite stayed green. These are the cross-references the renderer follows
    without checking, so the view model has to close them.
    """
    view = build_report_view_model(_report(currency_by_model={"glm-5.3": "CNY"}))
    charts = view["charts"]

    rates = charts["cost_bars"]["exchange_rates"]
    for entry in charts["cost_bars"]["converted"]:
        assert entry["currency"] in rates
        assert entry["units_per_usd"] and entry["checked_at"]

    for row in charts["headline"]["rows"]:
        for currency in row.get("converted_from", []):
            assert currency in row["exchange_rates"]
        # `best` indexes the same treatment map the bars iterate.
        if row["best"] is not None:
            assert row["best"] in row["values"]

    for key in ("delta_strips", "relative_change"):
        assert key in charts
    for group in charts["relative_change"]:
        for metric in group["metrics"]:
            for entry in metric["rows"]:
                assert entry["model"] in view["models"]

    # The retrieval table reads benchmark_labels[name] for every benchmark the
    # report carries; a name missing there would print a raw key.
    report = _report(currency_by_model={"glm-5.3": "CNY"})
    labels = view["benchmark_labels"]
    for model in view["models"]:
        for benchmark in report["model_reports"][model]["micro"]["benchmarks"]:
            assert benchmark in labels
