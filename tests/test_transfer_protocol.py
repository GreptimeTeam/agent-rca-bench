from collections import Counter

import pytest

from semantic_rca_bench.transfer_protocol import (
    formal_schedule,
    load_transfer_protocol,
)


def test_transfer_protocol_freezes_a_balanced_cell_per_case_model_and_treatment() -> None:
    protocol, selection = load_transfer_protocol()

    schedule = formal_schedule(protocol, selection)

    assert len(schedule) == protocol.expected_cells == 336
    assert len(selection.selected_cases) == 14
    assert len(protocol.models) == 4
    assert {cell["case_id"] for cell in schedule} == {
        case.opaque_case_id for case in selection.selected_cases
    }
    levels = [level.value for level in protocol.visibility_levels]
    for case in selection.selected_cases:
        for model in protocol.models:
            cells = [
                cell
                for cell in schedule
                if cell["case_id"] == case.opaque_case_id and cell["model"] == model.model
            ]
            assert len(cells) == len(levels) * protocol.repetitions_per_model
            # Every treatment runs the same number of times for a case and model,
            # whatever order the counterbalancing picked.
            assert sorted(cell["visibility"] for cell in cells) == sorted(
                levels * protocol.repetitions_per_model
            )
            for repetition in range(protocol.repetitions_per_model):
                order = [cell for cell in cells if cell["repetition"] == repetition]
                assert [cell["position"] for cell in order] == list(range(len(levels)))
                assert len({cell["visibility"] for cell in order}) == len(levels)


def test_the_schedule_spreads_every_treatment_across_every_position_per_model() -> None:
    protocol, selection = load_transfer_protocol()

    schedule = formal_schedule(protocol, selection)

    # Checking the cohort as a whole would hide a per-model skew, and each model
    # is analysed on its own, so the balance has to hold inside each model.
    for model in protocol.models:
        counts = Counter(
            (cell["visibility"], cell["position"])
            for cell in schedule
            if cell["model"] == model.model
        )
        assert len(counts) == len(protocol.visibility_levels) ** 2
        assert max(counts.values()) - min(counts.values()) <= 1


def test_transfer_protocol_disables_semantic_adjudication_for_headline_measurement() -> None:
    protocol, _ = load_transfer_protocol()

    assert protocol.semantic_adjudication.enabled is False
    assert protocol.semantic_adjudication.judge_models == (
        "claude-sonnet-5",
        "deepseek-v4-flash",
    )
    assert not set(protocol.semantic_adjudication.judge_models).intersection(
        model.model for model in protocol.models
    )


def test_transfer_cohort_merges_both_selection_sources() -> None:
    _, cohort = load_transfer_protocol()

    assert [source.adapter for source in cohort.sources] == ["openrca2", "rca100"]
    assert sum(len(source.case_ids) for source in cohort.sources) == len(cohort.selected_cases)
    # Every case resolves to exactly one adapter, so the environment cannot
    # replay a node case through the service-and-edge loader.
    for case in cohort.selected_cases:
        assert cohort.adapter_for(case.opaque_case_id) in {"openrca2", "rca100"}
    assert cohort.adapter_for("semantic-rca-transfer-001") == "openrca2"
    assert cohort.adapter_for("semantic-rca-transfer-011") == "rca100"
    with pytest.raises(ValueError, match="outside the bound cohort"):
        cohort.adapter_for("semantic-rca-transfer-999")
