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


def _model_report() -> dict[str, object]:
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
        "case_effects": [],
        "primary_metrics": {
            "rows_returned": metric,
            "correct_completion_tool_calls": {**metric, "case_median_delta": 0},
        },
    }
    return {
        "runs": 40,
        **analysis,
        "adjudicated_sensitivity": copy.deepcopy(analysis),
        "reliability": {
            "runner_errors": 0,
            "budget_exhaustions": 0,
            "failed_database_queries": 1,
        },
        "usage": {
            "estimated_cost": 2.5,
            "cost_currency": "USD",
        },
    }


def _report() -> dict[str, object]:
    suite, protocol = load_formal_suite_protocol()
    names = [model.model for model in protocol.models]
    micro = {
        "license": {"source": "source terms"},
        "sources": [
            {"no_model_gates": {"all_passed": True}} for _ in range(len(suite.micro_cases))
        ],
        "runs": [
            {"runner_error": False, "tool_budget_exhausted": False}
            for _ in range(suite.expected_micro_cells)
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
                            },
                            "tool_calls_through_evidence": {
                                "eligible_cases": 4,
                                "median_delta": -1,
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
        "inference": {"independent_unit": "case"},
        "sources": [
            {
                "no_model_gates": {"all_passed": True},
                "edge_equality": {"exact_edge_set_equality": True},
            }
            for _ in range(10)
        ],
        "runs": [
            {
                "run": {
                    "execution": {
                        "runner_error": False,
                        "tool_budget_exhausted": False,
                    }
                }
            }
            for _ in range(suite.expected_transfer_cells)
        ],
        "model_reports": {name: _model_report() for name in names},
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
        "expected_cells": 360,
        "completed_cells": 360,
        "micro_cells": 160,
        "transfer_cells": 200,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "models": 5,
        "micro_cases": 8,
        "transfer_cases": 10,
        "treatments": ["raw", "semantic_graph"],
        "repetitions_per_model_case": 2,
    }
    assert report["costs"] == {
        "models": {
            model: {"estimated_cost": 4.0, "currency": "USD"} for model in report["model_order"]
        },
        "known_totals_by_currency": {"USD": 20.0},
        "models_with_unavailable_estimate": [],
        "cross_currency_total": None,
    }

    output = tmp_path / "report.html"
    render_formal_measurement_report(report, output)
    document = output.read_text()
    assert "__REPORT_DATA__" not in document
    assert "360" in document
    assert "gpt-5.6-sol" in document
    assert "Retrieval micro-benchmarks" in document
    assert "End-to-end case effects" in document
    assert "Raw/Graph exact edge equality" in document
    assert "/Users/" not in document
    assert "private/tmp" not in document


def test_formal_measurement_report_rejects_tampered_summary() -> None:
    report = _report()
    tampered = copy.deepcopy(report)
    tampered["execution"]["completed_cells"] = 431

    with pytest.raises(ValueError, match="execution is incomplete"):
        validate_formal_measurement_report(tampered)
