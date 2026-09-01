import json
from pathlib import Path

import pytest

from semantic_rca_bench.contracts import MechanismCode
from semantic_rca_bench.datasets.openrca2_transfer import (
    MECHANISM_QUOTAS,
    SYSTEM_QUOTAS,
    deterministic_rank,
    load_selection_fixture,
    rank_manifest_candidates,
)

SELECTION = Path("fixtures/reference/openrca2-transfer-v32-selection.json")


def test_transfer_selection_is_fresh_opaque_and_source_bound() -> None:
    fixture = load_selection_fixture(SELECTION)

    assert len(fixture.selected_cases) == 10
    assert fixture.quotas == MECHANISM_QUOTAS
    assert fixture.system_quotas == SYSTEM_QUOTAS
    assert not set(fixture.trajectory_exclusions) & {
        case.source_case for case in fixture.selected_cases
    }
    for index, case in enumerate(fixture.selected_cases, start=1):
        assert case.opaque_case_id == f"semantic-rca-transfer-{index:03d}"
        assert case.source_case not in case.opaque_case_id
        assert "causal_graph.json" not in case.source_files_sha256
        assert set(case.source_files_sha256) == {
            "normal_metrics.parquet",
            "abnormal_metrics.parquet",
            "normal_metrics_sum.parquet",
            "abnormal_metrics_sum.parquet",
            "normal_metrics_histogram.parquet",
            "abnormal_metrics_histogram.parquet",
            "normal_logs.parquet",
            "abnormal_logs.parquet",
            "normal_traces.parquet",
            "abnormal_traces.parquet",
            "injection.json",
            "env.json",
            "conclusion.parquet",
        }
        if case.mechanism_evidence.identity_column is not None:
            assert len(case.mechanism_evidence.scope_preserving_predicates) == 1
            predicate = case.mechanism_evidence.scope_preserving_predicates[0]
            assert predicate.column == "k8s_namespace_name"
            assert predicate.value
            equivalents = {
                predicate.column: predicate.value
                for predicate in case.mechanism_evidence.identity_equivalent_predicates
            }
            assert equivalents["k8s_pod_name"]
            if case.mechanism_code in {
                MechanismCode.CPU_SATURATION,
                MechanismCode.MEMORY_PRESSURE,
            }:
                assert equivalents["service_name"] == case.causal_component
                assert equivalents["k8s_deployment_name"] == case.causal_component
            for signal in case.mechanism_evidence.alternative_metric_signals:
                assert signal.normal["high_count"] == 0
                assert signal.abnormal["high_count"] >= 2
        if index <= 3:
            assert len(case.direct_log_mechanism_evidence) == 1
            direct = case.direct_log_mechanism_evidence[0]
            assert direct.mechanism_code is MechanismCode.CONFIGURATION_ERROR
            assert direct.normal_count == 0
            assert direct.abnormal_count >= 1
        else:
            assert not case.direct_log_mechanism_evidence


def test_development_trajectories_are_explicit_measurement_exclusions() -> None:
    selection = load_selection_fixture(SELECTION)

    assert {
        "hs4-geo-pod-failure-pdt289",
        "otel-demo3-shipping-delay-m6fhpx",
    } <= set(selection.trajectory_exclusions)


def test_transfer_ranking_rejects_unknown_trajectory_exclusion(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "name": "known-case",
                "primary_kind": "PodFailure",
                "system": "hs",
                "hybrid": False,
                "root_services": ["service"],
            }
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="absent from the source manifest"):
        rank_manifest_candidates(manifest, ["misspelled-case"])


def test_transfer_frozen_ranking_uses_the_declared_seed() -> None:
    fixture = load_selection_fixture(SELECTION)

    for mechanism, candidates in fixture.ranked_candidates.items():
        assert deterministic_rank(candidates, mechanism) == candidates


def test_transfer_ranking_is_order_independent() -> None:
    candidates = ["case-c", "case-a", "case-b"]

    assert deterministic_rank(candidates, MechanismCode.CPU_SATURATION) == deterministic_rank(
        reversed(candidates), MechanismCode.CPU_SATURATION
    )
