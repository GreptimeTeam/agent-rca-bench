from semantic_rca_bench.transfer_protocol import (
    formal_schedule,
    load_transfer_protocol,
)


def test_transfer_protocol_freezes_200_balanced_cells() -> None:
    protocol, selection = load_transfer_protocol()

    schedule = formal_schedule(protocol, selection)

    assert len(schedule) == 200
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
