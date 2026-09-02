from pathlib import Path

import pytest

from semantic_rca_bench.contracts import CausalScope, MechanismCode
from semantic_rca_bench.datasets.openrca2 import OpenRCA2Error
from semantic_rca_bench.datasets.openrca2_transfer import canonical_mechanism_evidence_query
from semantic_rca_bench.datasets.rca100_transfer import (
    MECHANISM_QUOTAS,
    CandidateProfile,
    load_selection_fixture,
    typical_case_per_fault_type,
)

SELECTION = Path("fixtures/reference/rca100-transfer-node-selection.json")


def _profile(case: str, normal: float, abnormal: float, peer: float, **kwargs) -> CandidateProfile:
    return CandidateProfile(
        source_case=case,
        node=kwargs.pop("node", "node-a"),
        source_fault_type="nodeCpuHigh",
        normal_max=normal,
        abnormal_max=abnormal,
        peer_abnormal_max=peer,
        **kwargs,
    )


def test_typical_selection_takes_the_median_profile_not_an_extreme() -> None:
    # The quietest baseline and the loneliest anomaly are the easiest instances,
    # so a selection that reached for either would flatter the treatment.
    profiles = [
        _profile("t001", 1.0, 100.0, 1.0),
        _profile("t002", 5.0, 100.0, 14.0),
        _profile("t003", 40.0, 100.0, 99.0),
    ]

    assert typical_case_per_fault_type(profiles) == {"nodeCpuHigh": "t002"}


def test_typical_selection_ignores_candidates_the_source_cannot_support() -> None:
    profiles = [
        _profile("t001", 1.0, 100.0, 1.0),
        _profile("t002", 5.0, 100.0, 14.0, rejection="baseline_not_clear"),
        _profile("t003", 9.0, 100.0, 30.0),
    ]

    # t002 would have won on distance; rejected candidates never rank.
    assert typical_case_per_fault_type(profiles) == {"nodeCpuHigh": "t001"}


def test_frozen_node_selection_binds_one_typical_case_per_mechanism() -> None:
    fixture = load_selection_fixture(SELECTION)

    assert len(fixture.selected_cases) == sum(MECHANISM_QUOTAS.values())
    assert {case.mechanism_code for case in fixture.selected_cases} == set(MECHANISM_QUOTAS)
    for case in fixture.selected_cases:
        assert case.causal_scope is CausalScope.INFRASTRUCTURE_NODE
        assert case.causal_component
        assert case.edge_source is None and case.edge_destination is None
        assert case.normal_window[1] == case.abnormal_window[0]
        assert case.normal_window[1] - case.normal_window[0] == (
            case.abnormal_window[1] - case.abnormal_window[0]
        )


def test_mechanisms_without_a_node_signal_carry_no_deterministic_oracle() -> None:
    cases = {case.mechanism_code: case for case in load_selection_fixture(SELECTION).selected_cases}

    for code in (MechanismCode.CPU_SATURATION, MechanismCode.MEMORY_PRESSURE):
        evidence = cases[code].mechanism_evidence
        assert evidence is not None
        assert evidence.normal["high_count"] == 0
        assert evidence.abnormal["high_count"] >= evidence.minimum_anomalous_observations

    for code in (MechanismCode.DISK_IO_DEGRADATION, MechanismCode.HOST_UNAVAILABLE):
        assert cases[code].mechanism_evidence is None
        with pytest.raises(OpenRCA2Error, match="no deterministic mechanism oracle"):
            canonical_mechanism_evidence_query(cases[code])
