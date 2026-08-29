from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from semantic_rca_bench.aegis_transfer_scorer import (
    AegisTransferEvaluation,
    AegisTransferScorerFixture,
    evaluate_aegis_transfer_run,
    sha256_file,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.contracts import AgentRun, AgentRunner, Visibility
from semantic_rca_bench.protocol import benchmark_protocol, run_orders
from semantic_rca_bench.report import MODEL_PRICING

LEGACY_PROTOCOL_REVISION = "aegis-transfer-three-model-v2"
PROTOCOL_REVISION = "aegis-transfer-three-model-v3"
DEFAULT_PROTOCOL_FIXTURE = Path("fixtures/reference/aegis-transfer-v26-three-model-protocol.json")


class TransferModelContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    provider: str
    api_transport: str
    prompt_cache: str


class PaidExecutionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    explicit_user_approval_required_per_batch: bool
    subscription_fallback_allowed: bool
    no_model_gates_required: bool
    pricing_snapshot_required_at_execution: bool


class InferenceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_effect_compared_within_model_only: bool
    correctness_pooled_across_models: bool
    case_is_independent_unit: bool
    repetitions_are_descriptive: bool
    single_case_claim: str


class AegisTransferProtocolFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    protocol_revision: str
    agent_case_id: str
    case_role: str
    selection_fixture: str
    selection_fixture_sha256: str
    scorer_fixture: str
    scorer_fixture_sha256: str
    benchmark_protocol_version: int
    runner: AgentRunner
    models: tuple[TransferModelContract, ...]
    visibility_levels: tuple[Visibility, ...]
    max_tool_calls: int
    max_turns: int
    max_tokens: int
    repetitions_per_model: int
    treatment_order_seed: int
    parallel_runs: int
    sampling: str
    execution_order: str
    paid_execution: PaidExecutionContract
    inference: InferenceContract


def load_transfer_protocol_fixture(
    path: Path = DEFAULT_PROTOCOL_FIXTURE,
) -> AegisTransferProtocolFixture:
    fixture = AegisTransferProtocolFixture.model_validate_json(path.read_text())
    specifications = {
        LEGACY_PROTOCOL_REVISION: {
            "version": 1,
            "agent_case_id": "aegis-transfer-002",
            "benchmark_protocol_version": 25,
            "selection_fixture": "fixtures/reference/aegis-transfer-v25-selection.json",
            "scorer_fixture": "fixtures/reference/aegis-transfer-v25-scorer.json",
            "models": (
                (
                    "deepseek-v4-flash",
                    "deepseek",
                    "anthropic-compatible-messages",
                    "provider-automatic-prefix",
                ),
                (
                    "deepseek-v4-pro",
                    "deepseek",
                    "anthropic-compatible-messages",
                    "provider-automatic-prefix",
                ),
                (
                    "claude-sonnet-5",
                    "anthropic",
                    "anthropic-messages",
                    "ephemeral-request-cache-control",
                ),
            ),
        },
        PROTOCOL_REVISION: {
            "version": 2,
            "agent_case_id": "aegis-transfer-003",
            "benchmark_protocol_version": 26,
            "selection_fixture": "fixtures/reference/aegis-transfer-v26-selection.json",
            "scorer_fixture": "fixtures/reference/aegis-transfer-v26-scorer.json",
            "models": (
                (
                    "deepseek-v4-pro",
                    "deepseek",
                    "anthropic-compatible-messages",
                    "provider-automatic-prefix",
                ),
                (
                    "claude-sonnet-5",
                    "anthropic",
                    "anthropic-messages",
                    "ephemeral-request-cache-control",
                ),
                (
                    "claude-opus-4-8",
                    "anthropic",
                    "anthropic-messages",
                    "ephemeral-request-cache-control",
                ),
            ),
        },
    }
    specification = specifications.get(fixture.protocol_revision)
    if specification is None:
        raise ValueError("unsupported Aegis transfer formal protocol revision")
    observed_models = tuple(
        (model.model, model.provider, model.api_transport, model.prompt_cache)
        for model in fixture.models
    )
    if fixture.version != specification["version"]:
        raise ValueError("unsupported Aegis transfer formal protocol revision")
    if (
        fixture.agent_case_id != specification["agent_case_id"]
        or fixture.case_role != "measurement"
    ):
        raise ValueError("formal protocol is not bound to the frozen measurement case")
    if fixture.benchmark_protocol_version != specification["benchmark_protocol_version"]:
        raise ValueError("formal protocol benchmark version drifted")
    if fixture.runner is not AgentRunner.API or observed_models != specification["models"]:
        raise ValueError("formal protocol model roster drifted")
    if (
        fixture.selection_fixture != specification["selection_fixture"]
        or fixture.scorer_fixture != specification["scorer_fixture"]
    ):
        raise ValueError("formal protocol fixture identity drifted")
    if any(model.model not in MODEL_PRICING for model in fixture.models):
        raise ValueError("formal protocol has no local pricing contract for a model")
    if fixture.visibility_levels != tuple(Visibility):
        raise ValueError("formal protocol treatment roster drifted")
    if (
        fixture.max_tool_calls != 48
        or fixture.max_turns != 58
        or fixture.max_tokens != 4096
        or fixture.repetitions_per_model != 3
        or fixture.treatment_order_seed != 0
        or fixture.parallel_runs != 1
        or fixture.sampling != "provider-default; no seed sent"
        or fixture.execution_order
        != "models in manifest order; seeded rotating treatments within each model"
    ):
        raise ValueError("formal protocol runner budget or schedule drifted")
    paid = fixture.paid_execution
    if not (
        paid.explicit_user_approval_required_per_batch
        and not paid.subscription_fallback_allowed
        and paid.no_model_gates_required
        and paid.pricing_snapshot_required_at_execution
    ):
        raise ValueError("formal protocol paid-execution guard drifted")
    inference = fixture.inference
    if not (
        inference.semantic_effect_compared_within_model_only
        and not inference.correctness_pooled_across_models
        and inference.case_is_independent_unit
        and inference.repetitions_are_descriptive
        and inference.single_case_claim
        == "fresh case-level transfer demonstration; not a general semantic-layer effect estimate"
    ):
        raise ValueError("formal protocol inference boundary drifted")
    _validate_bound_file(path, fixture.selection_fixture, fixture.selection_fixture_sha256)
    _validate_bound_file(path, fixture.scorer_fixture, fixture.scorer_fixture_sha256)
    return fixture


def audit_transfer_protocol(
    fixture: AegisTransferProtocolFixture,
    protocol_path: Path,
    scorer_fixture: AegisTransferScorerFixture,
    scorer_path: Path,
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
) -> dict[str, object]:
    if load_transfer_protocol_fixture(protocol_path) != fixture:
        raise ValueError("formal protocol object does not match its bound fixture file")
    if AegisTransferScorerFixture.model_validate_json(scorer_path.read_text()) != scorer_fixture:
        raise ValueError("formal scorer object does not match its bound fixture file")
    source_gates = source_audit.get("no_model_gates")
    scorer_gates = scorer_audit.get("no_model_gates")
    case = source_audit.get("case")
    agent_facing = case.get("agent_facing") if isinstance(case, dict) else None
    selection_audit = source_audit.get("selection_audit")
    frozen_selection = (
        selection_audit.get("frozen_selection_gate") if isinstance(selection_audit, dict) else None
    )
    gates = {
        "benchmark_protocol_match": (
            fixture.benchmark_protocol_version
            == benchmark_protocol(fixture.benchmark_protocol_version)["version"]
        ),
        "measurement_case_match": (
            scorer_fixture.agent_case_id == fixture.agent_case_id
            and scorer_fixture.case_role == fixture.case_role
            and isinstance(agent_facing, dict)
            and agent_facing.get("case_id") == fixture.agent_case_id
            and agent_facing.get("fault_taxonomy") == []
        ),
        "selection_fixture_binding": (
            isinstance(frozen_selection, dict)
            and frozen_selection.get("manifest_name") == Path(fixture.selection_fixture).name
        ),
        "scorer_fixture_binding": (
            sha256_file(scorer_path) == fixture.scorer_fixture_sha256
            and scorer_audit.get("fixture_sha256") == fixture.scorer_fixture_sha256
        ),
        "source_no_model_gates": (
            isinstance(source_gates, dict) and source_gates.get("all_passed") is True
        ),
        "scorer_no_model_gates": (
            isinstance(scorer_gates, dict) and scorer_gates.get("all_passed") is True
        ),
        "source_scorer_binding": (
            scorer_audit.get("source_transfer_audit_sha256")
            == source_transfer_audit_sha256(source_audit)
        ),
        "three_model_roster": len(fixture.models) == 3,
        "scorer_runner_common_contract": (
            scorer_fixture.canonical_api_runner.runner is fixture.runner
            and scorer_fixture.canonical_api_runner.visibility_levels == fixture.visibility_levels
            and scorer_fixture.canonical_api_runner.max_tool_calls == fixture.max_tool_calls
            and scorer_fixture.canonical_api_runner.max_turns == fixture.max_turns
            and scorer_fixture.canonical_api_runner.max_tokens == fixture.max_tokens
            and scorer_fixture.canonical_api_runner.repetitions == fixture.repetitions_per_model
            and scorer_fixture.canonical_api_runner.treatment_order_seed
            == fixture.treatment_order_seed
            and scorer_fixture.canonical_api_runner.parallel_runs == fixture.parallel_runs
            and scorer_fixture.canonical_api_runner.sampling == fixture.sampling
        ),
        "prompt_cache_enabled_for_all_models": all(
            model.prompt_cache in {"provider-automatic-prefix", "ephemeral-request-cache-control"}
            for model in fixture.models
        ),
        "paid_execution_requires_new_approval": (
            fixture.paid_execution.explicit_user_approval_required_per_batch
        ),
    }
    gates["all_passed"] = all(gates.values())
    orders = run_orders(
        list(fixture.visibility_levels),
        fixture.repetitions_per_model,
        fixture.treatment_order_seed,
    )
    return {
        "audit_schema_version": 1,
        "mode": "aegis-transfer-formal-protocol-no-model-audit",
        "protocol_revision": fixture.protocol_revision,
        "protocol_fixture_sha256": sha256_file(protocol_path),
        "agent_case_id": fixture.agent_case_id,
        "case_role": fixture.case_role,
        "models": [model.model_dump(mode="json") for model in fixture.models],
        "orders_per_model": [[level.value for level in order] for order in orders],
        "expected_paid_runs": (
            len(fixture.models) * fixture.repetitions_per_model * len(fixture.visibility_levels)
        ),
        "inference": fixture.inference.model_dump(mode="json"),
        "paid_execution_authorized": False,
        "no_model_gates": gates,
    }


def evaluate_transfer_protocol_run(
    run: AgentRun,
    scorer_fixture: AegisTransferScorerFixture,
    protocol_fixture: AegisTransferProtocolFixture,
    *,
    expected_model: str,
) -> AegisTransferEvaluation:
    models = {contract.model for contract in protocol_fixture.models}
    if run.model not in models:
        raise ValueError("run model is outside the frozen formal protocol roster")
    if expected_model not in models:
        raise ValueError("scheduled model is outside the frozen formal protocol roster")
    return evaluate_aegis_transfer_run(
        run,
        scorer_fixture,
        expected_model=expected_model,
    )


def _validate_bound_file(protocol_path: Path, name: str, expected_sha256: str) -> None:
    candidate = Path(name)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.extend(parent / candidate for parent in protocol_path.resolve().parents)
    existing = next((path for path in candidates if path.is_file()), None)
    if existing is None or hashlib.sha256(existing.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError(f"formal protocol bound fixture drifted: {name}")


def _sha256_json(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
