from semantic_rca_bench.transfer_protocol import (
    formal_schedule,
    load_transfer_protocol,
    pilot_schedule,
)


def test_transfer_protocol_freezes_240_balanced_cells() -> None:
    protocol, selection, _ = load_transfer_protocol()

    schedule = formal_schedule(protocol, selection)

    assert len(schedule) == 240
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


def test_transfer_protocol_freezes_paid_pilot_gate() -> None:
    protocol, _, pilot = load_transfer_protocol()

    assert protocol.paid_execution.pilot_required is True
    assert protocol.pilot.expected_cells == 24
    assert protocol.pilot.minimum_eligible_pairs == 6
    assert protocol.pilot.minimum_eligible_pairs_per_case == 3
    assert protocol.pilot.threshold_status == "post-hoc-development-calibration"
    assert protocol.pilot.threshold_basis == (
        "pilot-case-001-v5-shadow-score-4-of-6-jointly-eligible-pairs"
    )
    assert len(pilot_schedule(protocol, pilot)) == 24
    assert {cell["case_id"] for cell in pilot_schedule(protocol, pilot)} == {
        "semantic-rca-pilot-001",
        "semantic-rca-pilot-002",
    }
