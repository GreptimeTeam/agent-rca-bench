import json
from pathlib import Path

import pytest

from semantic_rca_bench.contracts import MechanismCode
from semantic_rca_bench.datasets.openrca2_transfer import (
    MECHANISM_QUOTAS,
    SYSTEM_QUOTAS,
    deterministic_rank,
    load_pilot_fixture,
    load_selection_fixture,
    rank_manifest_candidates,
)

SELECTION = Path("fixtures/reference/openrca2-transfer-v32-selection.json")
PILOT = Path("fixtures/reference/openrca2-transfer-v32-pilot.json")


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
            assert len(case.mechanism_evidence.identity_equivalent_predicates) == 1
            equivalent = case.mechanism_evidence.identity_equivalent_predicates[0]
            assert equivalent.column == "k8s_pod_name"
            assert equivalent.value


def test_transfer_pilot_cases_are_explicit_measurement_exclusions() -> None:
    selection = load_selection_fixture(SELECTION)
    pilot = load_pilot_fixture(PILOT)

    assert {case.source_case for case in pilot.selected_cases} <= set(
        selection.trajectory_exclusions
    )


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
