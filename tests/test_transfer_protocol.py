import pytest

from semantic_rca_bench import transfer_protocol
from semantic_rca_bench.transfer_protocol import (
    formal_schedule,
    load_transfer_protocol,
    pilot_schedule,
)


def test_transfer_protocol_freezes_200_balanced_cells() -> None:
    protocol, selection, _ = load_transfer_protocol()

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
    assert [model.model for model in protocol.pilot.models] == [
        "gpt-5.6-sol",
        "deepseek-v4-pro",
        "qwen3.8-max",
    ]
    assert "qwen3.8-max" not in {model.model for model in protocol.models}
    assert protocol.semantic_adjudication.judge_models == (
        "claude-sonnet-5",
        "deepseek-v4-flash",
    )
    assert not set(protocol.semantic_adjudication.judge_models).intersection(
        model.model for model in protocol.models
    )
    assert len(pilot_schedule(protocol, pilot)) == 24
    assert {cell["case_id"] for cell in pilot_schedule(protocol, pilot)} == {
        "semantic-rca-pilot-001",
        "semantic-rca-pilot-002",
    }


def test_transfer_protocol_requires_pricing_for_pilot_only_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(transfer_protocol.MODEL_PRICING, "qwen3.8-max")

    with pytest.raises(ValueError, match="no pricing contract"):
        load_transfer_protocol()
