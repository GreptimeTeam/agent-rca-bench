from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from semantic_rca_bench.contracts import AgentRunner, ApiTransport, Visibility
from semantic_rca_bench.datasets.openrca2_transfer import (
    TransferPilotFixture,
    TransferSelectionFixture,
    load_pilot_fixture,
    load_selection_fixture,
)
from semantic_rca_bench.protocol import benchmark_protocol, run_orders
from semantic_rca_bench.report import MODEL_PRICING

PROTOCOL_REVISION = "openrca2-transfer-six-model-v6"
DEFAULT_PROTOCOL_FIXTURE = Path("fixtures/reference/openrca2-transfer-v32-protocol.json")


class ModelContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    provider: str
    api_transport: ApiTransport
    prompt_cache: str
    max_output_tokens: int
    reasoning_effort: str | None


class PaidExecutionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    explicit_user_approval_required_per_batch: bool
    subscription_fallback_allowed: bool
    no_model_gates_required: bool
    pilot_required: bool
    pricing_snapshot_required_at_execution: bool


class PilotContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: int
    selection_fixture: str
    selection_fixture_sha256: str
    models: tuple[str, ...]
    repetitions: int
    expected_cells: int
    minimum_eligible_pairs: int
    minimum_eligible_pairs_per_case: int
    threshold_status: Literal["post-hoc-development-calibration"]
    threshold_basis: str


class InferenceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_effect_compared_within_model_only: bool
    correctness_pooled_across_models: bool
    case_is_independent_unit: bool
    repetitions_are_descriptive: bool
    primary_metrics: tuple[str, str]
    holm_family_size: int
    null_metric_meaning: str
    tied_calls_meaning: str
    direction_consistent_non_significant_meaning: str


class TransferProtocolFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    protocol_revision: str
    benchmark_protocol_version: int
    case_role: Literal["measurement"]
    selection_fixture: str
    selection_fixture_sha256: str
    greptimedb_revision: str
    greptimedb_build_profile: Literal["release"]
    runner: AgentRunner
    models: tuple[ModelContract, ...]
    visibility_levels: tuple[Visibility, ...]
    max_tool_calls: int
    max_turns: int
    repetitions_per_model: int
    treatment_order_seed: int
    parallel_runs: int
    sampling: str
    execution_order: str
    expected_cells: int
    paid_execution: PaidExecutionContract
    pilot: PilotContract
    inference: InferenceContract


def load_transfer_protocol(
    path: Path = DEFAULT_PROTOCOL_FIXTURE,
) -> tuple[TransferProtocolFixture, TransferSelectionFixture, TransferPilotFixture]:
    fixture = TransferProtocolFixture.model_validate_json(path.read_text())
    expected_models = (
        (
            "gpt-5.6-sol",
            "openai",
            ApiTransport.OPENAI_RESPONSES,
            "implicit-prefix-30m",
            16384,
            "medium",
        ),
        (
            "deepseek-v4-pro",
            "deepseek",
            ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
            "provider-automatic-prefix",
            16384,
            "high",
        ),
        (
            "claude-opus-5",
            "anthropic",
            ApiTransport.ANTHROPIC_MESSAGES,
            "ephemeral-request-cache-control",
            16384,
            "high",
        ),
        (
            "claude-fable-5",
            "anthropic",
            ApiTransport.ANTHROPIC_MESSAGES,
            "ephemeral-request-cache-control",
            16384,
            "high",
        ),
        (
            "glm-5.3",
            "zhipu-bigmodel",
            ApiTransport.BIGMODEL_CHAT_COMPLETIONS,
            "provider-automatic-prefix",
            16384,
            "max",
        ),
        (
            "qwen3.8-2.4t-a95b",
            "alibaba-cloud-model-studio",
            ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES,
            "session-cache-header",
            16384,
            "xhigh",
        ),
    )
    observed_models = tuple(
        (
            model.model,
            model.provider,
            model.api_transport,
            model.prompt_cache,
            model.max_output_tokens,
            model.reasoning_effort,
        )
        for model in fixture.models
    )
    if (
        fixture.version != 1
        or fixture.protocol_revision != PROTOCOL_REVISION
        or fixture.benchmark_protocol_version != benchmark_protocol()["version"]
        or fixture.case_role != "measurement"
        or fixture.greptimedb_build_profile != "release"
        or fixture.runner is not AgentRunner.API
        or observed_models != expected_models
        or fixture.visibility_levels != (Visibility.RAW, Visibility.SEMANTIC_GRAPH)
        or fixture.max_tool_calls != 48
        or fixture.max_turns != 58
        or fixture.repetitions_per_model != 2
        or fixture.treatment_order_seed != 0
        or fixture.parallel_runs != 1
        or fixture.sampling != "provider-default; no seed sent"
        or fixture.execution_order
        != "cases in selection order; models in roster order; seeded rotating treatments"
    ):
        raise ValueError("OpenRCA2 transfer protocol contract drifted")
    if any(model.model not in MODEL_PRICING for model in fixture.models):
        raise ValueError("OpenRCA2 transfer protocol has no pricing contract for a model")
    selection_path = _bound_path(
        path,
        fixture.selection_fixture,
        fixture.selection_fixture_sha256,
    )
    selection = load_selection_fixture(selection_path)
    schedule = formal_schedule(fixture, selection)
    if fixture.expected_cells != 240 or len(schedule) != fixture.expected_cells:
        raise ValueError("OpenRCA2 transfer protocol cell count drifted")
    paid = fixture.paid_execution
    if not (
        paid.explicit_user_approval_required_per_batch
        and not paid.subscription_fallback_allowed
        and paid.no_model_gates_required
        and paid.pilot_required
        and paid.pricing_snapshot_required_at_execution
    ):
        raise ValueError("OpenRCA2 transfer paid-execution contract drifted")
    pilot = fixture.pilot
    pilot_path = _bound_path(
        path,
        pilot.selection_fixture,
        pilot.selection_fixture_sha256,
    )
    pilot_selection = load_pilot_fixture(pilot_path)
    if (
        pilot.cases != 2
        or pilot.models != ("gpt-5.6-sol", "deepseek-v4-pro", "qwen3.8-2.4t-a95b")
        or pilot.repetitions != 2
        or pilot.expected_cells != 24
        or pilot.minimum_eligible_pairs != 6
        or pilot.minimum_eligible_pairs_per_case != 3
        or pilot.threshold_status != "post-hoc-development-calibration"
        or pilot.threshold_basis != "pilot-case-001-v5-shadow-score-4-of-6-jointly-eligible-pairs"
    ):
        raise ValueError("OpenRCA2 transfer pilot contract drifted")
    pilot_cases = {case.source_case for case in pilot_selection.selected_cases}
    if not pilot_cases <= set(selection.trajectory_exclusions):
        raise ValueError("OpenRCA2 transfer pilot cases are not excluded from measurement")
    inference = fixture.inference
    if (
        not inference.semantic_effect_compared_within_model_only
        or inference.correctness_pooled_across_models
        or not inference.case_is_independent_unit
        or not inference.repetitions_are_descriptive
        or inference.primary_metrics != ("rows_returned", "correct_completion_tool_calls")
        or inference.holm_family_size != 12
        or inference.null_metric_meaning != "not estimable; no eligible paired cases"
        or inference.tied_calls_meaning != "no observed tool-call reduction in the cohort"
        or inference.direction_consistent_non_significant_meaning
        != (
            "descriptive evidence only; report the directional case count, eligible case count, "
            "case median, unadjusted p, and Holm-adjusted p"
        )
    ):
        raise ValueError("OpenRCA2 transfer inference contract drifted")
    return fixture, selection, pilot_selection


def formal_schedule(
    fixture: TransferProtocolFixture,
    selection: TransferSelectionFixture,
) -> list[dict[str, object]]:
    orders = run_orders(
        list(fixture.visibility_levels),
        fixture.repetitions_per_model,
        fixture.treatment_order_seed,
    )
    schedule = []
    for case_index, case in enumerate(selection.selected_cases):
        for model_index, model in enumerate(fixture.models):
            for repetition, order in enumerate(orders):
                for position, visibility in enumerate(order):
                    schedule.append(
                        {
                            "cell_index": len(schedule),
                            "case_index": case_index,
                            "case_id": case.opaque_case_id,
                            "model_index": model_index,
                            "model": model.model,
                            "provider": model.provider,
                            "api_transport": model.api_transport.value,
                            "prompt_cache": model.prompt_cache,
                            "max_output_tokens": model.max_output_tokens,
                            "reasoning_effort": model.reasoning_effort,
                            "repetition": repetition,
                            "position": position,
                            "visibility": visibility.value,
                        }
                    )
    return schedule


def pilot_schedule(
    fixture: TransferProtocolFixture,
    selection: TransferPilotFixture,
) -> list[dict[str, object]]:
    models = [model for model in fixture.models if model.model in fixture.pilot.models]
    orders = run_orders(
        list(fixture.visibility_levels),
        fixture.pilot.repetitions,
        fixture.treatment_order_seed,
    )
    schedule = []
    for case_index, case in enumerate(selection.selected_cases):
        for model_index, model in enumerate(models):
            for repetition, order in enumerate(orders):
                for position, visibility in enumerate(order):
                    schedule.append(
                        {
                            "cell_index": len(schedule),
                            "case_index": case_index,
                            "case_id": case.opaque_case_id,
                            "model_index": model_index,
                            "model": model.model,
                            "provider": model.provider,
                            "api_transport": model.api_transport.value,
                            "prompt_cache": model.prompt_cache,
                            "max_output_tokens": model.max_output_tokens,
                            "reasoning_effort": model.reasoning_effort,
                            "repetition": repetition,
                            "position": position,
                            "visibility": visibility.value,
                        }
                    )
    if len(schedule) != fixture.pilot.expected_cells:
        raise ValueError("OpenRCA2 transfer pilot cell count drifted")
    return schedule


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bound_path(root_file: Path, relative: str, expected_sha256: str) -> Path:
    candidate = Path(relative)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.extend(parent / candidate for parent in root_file.resolve().parents)
    path = next((item for item in candidates if item.is_file()), None)
    if path is None or sha256_file(path) != expected_sha256:
        raise ValueError(f"OpenRCA2 transfer bound file drifted: {relative}")
    return path
