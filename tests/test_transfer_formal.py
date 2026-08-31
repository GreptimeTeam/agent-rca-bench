from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from semantic_rca_bench.contracts import DatabaseLoad
from semantic_rca_bench.transfer_formal import (
    PreparedTransferEnvironment,
    bind_pilot_gate,
    build_preflight_report,
    execute_case_runs,
    pilot_gate,
    source_semantic_sha256,
    validate_private_report,
)
from semantic_rca_bench.transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    load_transfer_protocol,
    pilot_schedule,
)


def _source_audit(case_id: str) -> dict[str, object]:
    return {
        "case": {"opaque_case_id": case_id},
        "no_model_gates": {"all_passed": True},
    }


def test_preflight_freezes_source_audits_without_provider_calls() -> None:
    protocol, _, pilot = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in pilot.selected_cases]

    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        pilot,
        audits,
        phase="pilot",
    )

    validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, pilot)
    assert report["authorization"]["preflight_calls_provider"] is False
    assert report["execution"]["expected_runs"] == 24


def test_pilot_gate_requires_jointly_eligible_pairs() -> None:
    protocol, _, pilot = load_transfer_protocol()
    schedule = pilot_schedule(protocol, pilot)
    runs = [
        {
            **cell,
            "evaluation": {"efficiency_eligible": cell["repetition"] == 0},
            "run": {"error": None, "tool_budget_exhausted": False},
        }
        for cell in schedule
    ]
    report = {
        "phase": "pilot",
        "runs": runs,
        "execution": {"complete": True},
    }

    gate = pilot_gate(report, protocol)

    assert gate["eligible_pairs"] == 6
    assert len(gate["pair_results"]) == 12
    assert gate["asymmetric_pairs"] == []
    assert gate["gates"]["minimum_eligible_pairs"] is True
    assert gate["gates"]["minimum_eligible_pairs_per_case"] is True
    assert gate["gates"]["all_passed"] is True


def test_pilot_gate_reports_but_does_not_reject_treatment_asymmetry() -> None:
    protocol, _, pilot = load_transfer_protocol()
    schedule = pilot_schedule(protocol, pilot)
    runs = []
    for index, cell in enumerate(schedule):
        eligible = index != 0
        runs.append(
            {
                **cell,
                "evaluation": {
                    "efficiency_eligible": eligible,
                    "failure_reasons": [] if eligible else ["fault mechanism lacks evidence"],
                },
                "run": {"error": None, "tool_budget_exhausted": False},
            }
        )
    report = {"phase": "pilot", "runs": runs, "execution": {"complete": True}}

    gate = pilot_gate(report, protocol)

    assert gate["eligible_pairs"] == 11
    assert len(gate["asymmetric_pairs"]) == 1
    assert gate["eligibility_failures_by_treatment"]["raw"] == {"fault mechanism lacks evidence": 1}
    assert gate["gates"]["all_passed"] is True


def test_failed_pilot_threshold_is_recorded_without_blocking_measurement() -> None:
    protocol, _, pilot = load_transfer_protocol()
    report = {
        "phase": "pilot",
        "runs": [
            {
                **cell,
                "evaluation": {"efficiency_eligible": False, "failure_reasons": ["incorrect"]},
                "run": {"error": None, "tool_budget_exhausted": False},
            }
            for cell in pilot_schedule(protocol, pilot)
        ],
        "execution": {"complete": True},
    }
    measurement: dict[str, object] = {}

    bind_pilot_gate(measurement, report, protocol)

    assert measurement["pilot_gate"]["gates"]["all_passed"] is False


def test_measurement_report_rejects_tampered_pilot_gate() -> None:
    protocol, measurement, pilot = load_transfer_protocol()
    schedule = pilot_schedule(protocol, pilot)
    pilot_report = {
        "phase": "pilot",
        "runs": [
            {
                **cell,
                "evaluation": {"efficiency_eligible": True},
                "run": {"error": None, "tool_budget_exhausted": False},
            }
            for cell in schedule
        ],
        "execution": {"complete": True},
    }
    gate = pilot_gate(pilot_report, protocol)
    gate["pair_results"][0]["pair_eligible"] = False
    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        measurement,
        [_source_audit(case.opaque_case_id) for case in measurement.selected_cases],
        phase="measurement",
    )
    report["pilot_gate"] = gate

    with pytest.raises(ValueError, match="pair eligibility drifted"):
        validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, measurement)


def test_runner_failure_is_persisted_as_a_scoreable_cell() -> None:
    protocol, _, pilot = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in pilot.selected_cases]
    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        pilot,
        audits,
        phase="pilot",
    )

    class Client:
        @contextmanager
        def measure_query_load(self):
            yield DatabaseLoad()

    prepared = PreparedTransferEnvironment(
        client=Client(),
        case=SimpleNamespace(input=SimpleNamespace()),
        spec=pilot.selected_cases[0],
        source_audit=audits[0],
        semantic_coverage={},
        graph_window=(0, 60),
    )

    execute_case_runs(
        report,
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        pilot,
        prepared,
        paid_api_confirmed=True,
        max_new_runs=1,
        run_agent_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    assert report["execution"]["completed_runs"] == 1
    assert report["execution"]["runner_errors"] == 1
    assert report["runs"][0]["evaluation"]["success"] is False
    assert report["runs"][0]["source_semantic_sha256"] == source_semantic_sha256(audits[0])
    validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, pilot)
