import copy
from pathlib import Path

import pytest

from semantic_rca_bench.formal_report import (
    build_formal_measurement_report,
    render_formal_measurement_report,
    validate_formal_measurement_report,
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


def _model_report(case_ids: list[str]) -> dict[str, object]:
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
        "diagnosis_correct": {"raw": 18, "semantic_graph": 19},
        "valid_completion": {"raw": 18, "semantic_graph": 19},
        "efficiency_eligibility": {
            "by_treatment": {"raw": 18, "semantic_graph": 19},
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


def _report(*, transfer_graph_run_cost: float = 0.08) -> dict[str, object]:
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
                        "estimated_cost": (0.1 if visibility == "raw" else transfer_graph_run_cost),
                        "cost_currency": "USD",
                    },
                },
            }
            for name in names
            for case_id in case_ids
            for repetition in range(2)
            for visibility in ("split_pillars", "raw", "semantic_graph")
        ],
        "model_reports": {name: _model_report(case_ids) for name in names},
    }
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
    assert report["limitations"][6] == (
        "Mechanism cohorts are unevenly sized: Workload restart 4, Call-path delay 3, "
        "CPU saturation 3, Memory pressure 2, Disk I/O degradation 1, Host unavailable 1. "
        "Mechanism-level summaries are descriptive and unevenly supported."
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
    assert costs["cross_currency_total"] is None
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

    output = tmp_path / "report.html"
    render_formal_measurement_report(report, output)
    document = output.read_text()
    assert "__REPORT_DATA__" not in document
    assert 'href="semantic-rca-v34.json"' in document
    assert "sanitized records for all 464 runs" in document
    assert "仓库公开全部 464 次运行" in document
    assert "gpt-5.6-sol" in document
    assert "Focused retrieval micro-benchmarks" in document
    assert "End-to-end case effects" in document
    assert "Fault mechanism changes the effect" in document
    assert "What this is" in document
    assert "这是什么" in document
    assert "Focused retrieval improves; E2E varies" in document
    assert "聚焦检索更省，端到端因场景而异" in document
    assert (
        "Models where Graph returned fewer rows, by mechanism: Workload restart 4/4, "
        "Call-path delay 4/4, CPU saturation 4/4, Memory pressure 4/4, "
        "Disk I/O degradation 4/4, and Host unavailable 4/4." in document
    )
    assert "Of 6 estimable mechanisms, 6 reduce rows for every model" in document
    assert (
        "Eligible micro cases where Graph returned fewer rows: Discovery 12/16 and "
        "Graph-retrieval 12/16." in document
    )
    assert "the pre-registered family of 14 tests" in document
    assert "18/28 Raw · 19/28 Graph" in document
    assert "Across all 4 models: Raw 72/112; Graph 76/112." in document
    assert "Open all 14 cases and 56 case-model combinations" in document
    assert "View all 24 model-mechanism combinations" in document
    assert (
        "The micro-benchmarks use 6 Discovery cases from OpenRCA 1.0 and 2 Graph-retrieval "
        "cases from OpenRCA2 ops-lite. The end-to-end cohort uses 10 cases from OpenRCA2 "
        "ops-lite and 4 cases from RCA100." in document
    )
    assert "RCA100 v1.1 declares CC BY-NC-SA 4.0" in document
    assert "Identifies component, dependency edge, or infrastructure node scope" in document
    assert "识别组件、依赖边、基础设施节点 scope" in document
    assert '<details class="report-details"' in document
    assert '<div class="score-leaderboard">' in document
    assert '<span class="rank">#1</span>' in document
    assert "gpt-5.6-sol: Location 40, Root cause 40, Strict evidence 5" in document
    assert '<div class="score-stack" role="img"' in document
    # A full 85-point score fills the stack instead of leaving a 15% remainder.
    assert 'style="width: 47.0588%" title="Location: 40"' in document
    assert 'style="width: 5.88235%" title="Strict evidence: 5"' in document
    # Overall, three treatments, and three rubric dimensions, in both languages.
    assert document.count('<article class="dimension-chart">') == 14
    assert "Independent rankings by dimension" in document
    assert "各维度独立排行" in document
    assert "gpt-5.6-sol Strict evidence: 5 / 5" in document
    # The rubric split is derived, so the copy cannot drift from it.
    assert "查看 40/40/5 评分细则和精确分数" in document
    assert "Datasets and acknowledgements" in document
    assert "数据来源与致谢" in document
    assert "https://github.com/microsoft/OpenRCA" in document
    assert 'href="#en" data-language-button="en"' in document
    assert 'href="#zh" data-language-button="zh"' in document
    assert "/^#(en|zh)(?:-(.+))?$/" in document
    assert 'window.addEventListener("hashchange", applyHash)' in document
    assert "navigator.language" not in document
    for language in ("en", "zh"):
        assert document.count(f'id="{language}" data-report-language="{language}"') == 1
        for section in (
            "overview",
            "summary",
            "mechanisms",
            "models",
            "benchmarks",
            "cases",
            "resources",
            "method",
        ):
            assert document.count(f'id="{language}-{section}"') == 1
            assert f'href="#{language}-{section}"' in document
    assert "Raw/Graph exact edge equality" in document
    assert "/Users/" not in document
    assert "private/tmp" not in document


def test_formal_measurement_report_states_a_cost_result_without_improved_models(
    tmp_path: Path,
) -> None:
    report = _report(transfer_graph_run_cost=0.12)

    output = tmp_path / "report.html"
    render_formal_measurement_report(report, output)
    document = output.read_text()
    assert "No model reduced actual end-to-end cost among 4 fully comparable models." in document
    assert "4 个可完整比较的模型中，没有模型降低端到端实际成本。" in document


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
