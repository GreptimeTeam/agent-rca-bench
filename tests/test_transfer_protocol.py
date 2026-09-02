import pytest

from semantic_rca_bench.transfer_protocol import (
    formal_schedule,
    load_transfer_protocol,
)


def test_transfer_protocol_freezes_a_balanced_cell_per_case_model_and_treatment() -> None:
    protocol, selection = load_transfer_protocol()

    schedule = formal_schedule(protocol, selection)

    assert len(schedule) == 224
    assert len(selection.selected_cases) == 14
    assert len(protocol.models) == 4
    assert {cell["case_id"] for cell in schedule} == {
        case.opaque_case_id for case in selection.selected_cases
    }
    for case in selection.selected_cases:
        for model in protocol.models:
            cells = [
                cell
                for cell in schedule
                if cell["case_id"] == case.opaque_case_id and cell["model"] == model.model
            ]
            assert len(cells) == 4
            assert [cell["visibility"] for cell in cells] == [
                "raw",
                "semantic_graph",
                "semantic_graph",
                "raw",
            ]


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
