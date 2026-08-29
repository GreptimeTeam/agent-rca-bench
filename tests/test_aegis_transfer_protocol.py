import hashlib
import json
from pathlib import Path

import pytest

from semantic_rca_bench.aegis_transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    audit_transfer_protocol,
    evaluate_transfer_protocol_run,
    load_transfer_protocol_fixture,
)
from semantic_rca_bench.aegis_transfer_scorer import (
    FORMAL_SCORER_FIXTURE,
    load_transfer_scorer_fixture,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.contracts import AgentRun, AgentUsage, Visibility


def _audits() -> tuple[dict[str, object], dict[str, object]]:
    source = {
        "selection_audit": {
            "frozen_selection_gate": {
                "manifest_name": "aegis-transfer-v26-selection.json",
                "pass": True,
            }
        },
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-003",
                "fault_taxonomy": [],
            }
        },
        "no_model_gates": {"all_passed": True},
    }
    scorer_audit = {
        "source_transfer_audit_sha256": source_transfer_audit_sha256(source),
        "fixture_sha256": hashlib.sha256(FORMAL_SCORER_FIXTURE.read_bytes()).hexdigest(),
        "no_model_gates": {"all_passed": True},
    }
    return source, scorer_audit


def test_three_model_protocol_freezes_cache_schedule_and_inference_boundary() -> None:
    fixture = load_transfer_protocol_fixture()
    scorer = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    source, scorer_audit = _audits()

    audit = audit_transfer_protocol(
        fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        scorer,
        FORMAL_SCORER_FIXTURE,
        source,
        scorer_audit,
    )

    assert audit["expected_paid_runs"] == 27
    assert audit["paid_execution_authorized"] is False
    assert audit["orders_per_model"] == [
        ["raw", "semantic_graph", "table_semantics"],
        ["semantic_graph", "table_semantics", "raw"],
        ["table_semantics", "raw", "semantic_graph"],
    ]
    assert audit["no_model_gates"]["prompt_cache_enabled_for_all_models"]
    assert audit["no_model_gates"]["all_passed"]
    assert audit["inference"]["correctness_pooled_across_models"] is False


def test_three_model_protocol_drift_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_PROTOCOL_FIXTURE.read_text())
    raw["models"][2]["prompt_cache"] = "disabled"
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="model roster drifted"):
        load_transfer_protocol_fixture(path)


def test_three_model_protocol_requires_source_and_scorer_gates() -> None:
    fixture = load_transfer_protocol_fixture()
    scorer = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    source, scorer_audit = _audits()
    source["no_model_gates"]["all_passed"] = False

    audit = audit_transfer_protocol(
        fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        scorer,
        FORMAL_SCORER_FIXTURE,
        source,
        scorer_audit,
    )

    assert not audit["no_model_gates"]["source_no_model_gates"]
    assert not audit["no_model_gates"]["source_scorer_binding"]
    assert not audit["no_model_gates"]["all_passed"]


def test_protocol_scorer_accepts_runner_contract_for_each_frozen_model() -> None:
    protocol = load_transfer_protocol_fixture()
    scorer = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)

    for model in (contract.model for contract in protocol.models):
        run = AgentRun(
            run_id="run",
            visibility=Visibility.RAW,
            model=model,
            diagnosis=None,
            tool_calls=[],
            usage=AgentUsage(),
            elapsed_seconds=0,
            responses=[],
        )

        assert evaluate_transfer_protocol_run(
            run, scorer, protocol, expected_model=model
        ).runner_contract_match

    outside = run.model_copy(update={"model": "not-frozen"})
    with pytest.raises(ValueError, match="outside the frozen"):
        evaluate_transfer_protocol_run(
            outside,
            scorer,
            protocol,
            expected_model="not-frozen",
        )
