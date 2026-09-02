import json
from pathlib import Path

import pytest

from semantic_rca_bench.contracts import CausalScope, MechanismCode
from semantic_rca_bench.datasets.openrca2 import OpenRCA2Error
from semantic_rca_bench.datasets.openrca2_transfer import canonical_mechanism_evidence_query
from semantic_rca_bench.datasets.rca100_transfer import (
    MECHANISM_QUOTAS,
    CandidateProfile,
    load_selection_fixture,
    mark_duplicate_incidents,
    typical_case_per_fault_type,
)
from semantic_rca_bench.transfer_release import _valid_public_locus_shape

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


def test_typical_selection_ranks_instances_whose_oracle_cannot_run() -> None:
    # Whether the deterministic transition audit can run is an oracle property.
    # Letting it drop a case would make the evidence audit choose the cohort.
    profiles = [
        _profile("t001", 1.0, 100.0, 1.0),
        _profile("t002", 5.0, 100.0, 14.0, oracle_available=False),
        _profile("t003", 9.0, 100.0, 30.0),
    ]

    assert typical_case_per_fault_type(profiles) == {"nodeCpuHigh": "t002"}


def test_typical_selection_drops_only_source_fidelity_rejections() -> None:
    profiles = [
        _profile("t001", 1.0, 100.0, 1.0),
        _profile("t002", 5.0, 100.0, 14.0, rejection="duplicate_incident"),
        _profile("t003", 9.0, 100.0, 30.0),
    ]

    assert typical_case_per_fault_type(profiles) == {"nodeCpuHigh": "t001"}


def test_duplicate_incidents_keep_one_alert_per_overlapping_window() -> None:
    profiles = [
        _profile("t001", 1.0, 100.0, 1.0, node="node-a"),
        _profile("t002", 2.0, 100.0, 1.0, node="node-a"),
        _profile("t003", 3.0, 100.0, 1.0, node="node-a"),
        _profile("t004", 4.0, 100.0, 1.0, node="node-b"),
    ]
    windows = {
        "t001": (100, 200),
        "t002": (150, 250),  # same node, overlaps t001
        "t003": (300, 400),  # same node, disjoint
        "t004": (150, 250),  # overlaps in time but a different node
    }

    marked = {
        item.source_case: item.rejection for item in mark_duplicate_incidents(profiles, windows)
    }

    assert marked == {
        "t001": None,
        "t002": "duplicate_incident",
        "t003": None,
        "t004": None,
    }


def test_frozen_selection_must_match_its_own_recorded_ranking(tmp_path) -> None:
    # A swap to another source-eligible instance keeps every other invariant
    # intact, so the ranking replay is what protects the selection contract.
    payload = json.loads(SELECTION.read_text())
    ranked = {case["source_case"] for case in payload["selected_cases"]}
    replacement = next(
        profile["source_case"]
        for profile in payload["candidate_profiles"]
        if profile["source_fault_type"] == "nodeCpuHigh"
        and profile["rejection"] is None
        and profile["source_case"] not in ranked
    )
    for case in payload["selected_cases"]:
        if case["mechanism_code"] == MechanismCode.CPU_SATURATION.value:
            case["source_case"] = replacement
    swapped = tmp_path / "swapped.json"
    swapped.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="does not match its recorded ranking"):
        load_selection_fixture(swapped)


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


def test_public_artifacts_accept_a_node_locus_projection() -> None:
    # The release rescorer validates the shape of every published locus
    # projection. A node case emits scope=infrastructure_node, so a validator
    # that only knew the two original scopes would reject its own artifacts.
    assert _valid_public_locus_shape({"scope": "infrastructure_node", "component": "node-a"})
    assert _valid_public_locus_shape({"scope": "component", "component": "cart"})
    assert _valid_public_locus_shape(
        {"scope": "dependency_edge", "edge_source": "a", "edge_destination": "b"}
    )
    assert not _valid_public_locus_shape(
        {"scope": "infrastructure_node", "edge_source": "a", "edge_destination": "b"}
    )
    assert not _valid_public_locus_shape({"scope": "rack", "component": "r1"})
