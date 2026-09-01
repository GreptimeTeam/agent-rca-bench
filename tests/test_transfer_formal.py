from contextlib import contextmanager
from types import SimpleNamespace

from semantic_rca_bench.contracts import DatabaseLoad
from semantic_rca_bench.transfer_formal import (
    PreparedTransferEnvironment,
    build_preflight_report,
    execute_case_runs,
    source_semantic_sha256,
    validate_private_report,
)
from semantic_rca_bench.transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    load_transfer_protocol,
)


def _source_audit(case_id: str) -> dict[str, object]:
    return {
        "case": {"opaque_case_id": case_id},
        "no_model_gates": {"all_passed": True},
    }


def test_preflight_freezes_source_audits_without_provider_calls() -> None:
    protocol, selection = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in selection.selected_cases]

    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        selection,
        audits,
    )

    validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, selection)
    assert report["authorization"]["preflight_calls_provider"] is False
    assert report["phase"] == "measurement"
    assert report["execution"]["expected_runs"] == 200


def test_runner_failure_is_persisted_as_a_scoreable_cell() -> None:
    protocol, selection = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in selection.selected_cases]
    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        selection,
        audits,
    )

    class Client:
        @contextmanager
        def measure_query_load(self):
            yield DatabaseLoad()

    prepared = PreparedTransferEnvironment(
        client=Client(),
        case=SimpleNamespace(input=SimpleNamespace()),
        spec=selection.selected_cases[0],
        source_audit=audits[0],
        semantic_coverage={},
        graph_window=(0, 60),
    )

    execute_case_runs(
        report,
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        selection,
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
    validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, selection)
