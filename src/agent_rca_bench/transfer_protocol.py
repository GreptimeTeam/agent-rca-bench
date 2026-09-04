from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent_rca_bench.contracts import AgentRunner, ApiTransport, Visibility
from agent_rca_bench.datasets.openrca2_transfer import (
    TransferCaseSpec,
    load_selection_fixture,
)
from agent_rca_bench.datasets.rca100_transfer import (
    load_selection_fixture as load_node_selection_fixture,
)
from agent_rca_bench.protocol import (
    benchmark_protocol,
    counterbalanced_orders,
    rotate_levels,
)
from agent_rca_bench.report import MODEL_PRICING

PROTOCOL_REVISION = "transfer-four-model-three-arm-service-edge-node-v20"
DEFAULT_PROTOCOL_FIXTURE = Path("fixtures/reference/transfer-v34-protocol.json")


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
    no_model_gates_required: bool
    pricing_snapshot_required_at_execution: bool


class ConfirmatoryFamily(BaseModel):
    """One pre-declared paired comparison and the endpoints it is tested on.

    Families are corrected separately: pooling them would make one goal's
    significance depend on how many tests the other goal ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: Literal["storage_shape", "semantic_layer"]
    status: Literal["primary", "secondary"]
    baseline: Visibility
    treatment: Visibility
    metrics: tuple[str, str]


class InferenceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_effect_compared_within_model_only: bool
    correctness_pooled_across_models: bool
    case_is_independent_unit: bool
    repetitions_are_descriptive: bool
    primary_metrics: tuple[str, str]
    holm_family_size: int
    confirmatory_families: tuple[ConfirmatoryFamily, ...]
    descriptive_comparisons: tuple[tuple[Visibility, Visibility], ...]
    rows_returned_applicable_treatments: tuple[Visibility, ...]
    null_metric_meaning: str
    tied_calls_meaning: str
    direction_consistent_non_significant_meaning: str
    headline_eligibility: str
    evidence_sufficiency_role: str


class SemanticAdjudicationContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    trigger: str
    judge_models: tuple[str, str]
    decision_rule: str
    deterministic_hard_gates_remain_authoritative: bool
    publish_deterministic_and_adjudicated_results: bool


class SplitStackImages(BaseModel):
    """The container images that define the split-pillars arm.

    Bound here for the same reason `greptimedb_revision` is: the arm's results
    are only attributable to a store if the protocol says which one ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prometheus: str
    loki: str
    tempo: str

    def require_digests(self) -> None:
        for name, image in self.model_dump().items():
            if "@sha256:" not in image:
                raise ValueError(f"split-stack {name} image is not pinned by digest")


class CohortSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: Literal["openrca2", "rca100"]
    fixture: str
    fixture_sha256: str
    case_ids: tuple[str, ...]


class TransferCohort(BaseModel):
    """The measurement cases, in schedule order, and where each one came from.

    The cohort spans two adapters because no single source carries both the
    service and edge faults and the node faults, so the case specs are merged
    here while each source keeps its own selection contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_cases: tuple[TransferCaseSpec, ...]
    sources: tuple[CohortSource, ...]

    def adapter_for(self, opaque_case_id: str) -> Literal["openrca2", "rca100"]:
        for source in self.sources:
            if opaque_case_id in source.case_ids:
                return source.adapter
        raise ValueError(f"case is outside the bound cohort: {opaque_case_id}")


class TransferProtocolFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    protocol_revision: str
    benchmark_protocol_version: int
    case_role: Literal["measurement"]
    selection_fixture: str
    selection_fixture_sha256: str
    node_selection_fixture: str
    node_selection_fixture_sha256: str
    greptimedb_revision: str
    greptimedb_build_profile: Literal["release"]
    split_stack_images: SplitStackImages
    runner: AgentRunner
    models: tuple[ModelContract, ...]
    visibility_levels: tuple[Visibility, ...]
    max_tool_calls: int
    max_turns: int
    repetitions_per_model: int
    parallel_runs: int
    max_parallel_environment_preparations: int
    max_parallel_runs_per_provider: int
    sampling: str
    execution_order: str
    expected_cells: int
    paid_execution: PaidExecutionContract
    inference: InferenceContract
    semantic_adjudication: SemanticAdjudicationContract


def load_transfer_protocol(
    path: Path = DEFAULT_PROTOCOL_FIXTURE,
) -> tuple[TransferProtocolFixture, TransferCohort]:
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
            "claude-fable-5-1",
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
            "high",
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
        or fixture.visibility_levels
        != (Visibility.SPLIT_PILLARS, Visibility.RAW, Visibility.SEMANTIC_GRAPH)
        or fixture.max_tool_calls != 48
        or fixture.max_turns != 58
        or fixture.repetitions_per_model != 2
        or fixture.parallel_runs != 4
        or fixture.max_parallel_environment_preparations != 2
        or fixture.max_parallel_runs_per_provider != 2
        or fixture.sampling != "provider-default; no seed sent"
        or fixture.execution_order
        != "case batches of up to four; at most two environment preparations at once; "
        "all batch environments ready before model calls; model queues round-robin with case "
        "rotation; counterbalanced treatment order preserved within each model"
    ):
        raise ValueError("transfer protocol contract drifted")
    fixture.split_stack_images.require_digests()
    if any(model.model not in MODEL_PRICING for model in fixture.models):
        raise ValueError("transfer protocol has no pricing contract for a model")
    selection_path = _bound_path(
        path,
        fixture.selection_fixture,
        fixture.selection_fixture_sha256,
    )
    selection = load_selection_fixture(selection_path)
    node_selection_path = _bound_path(
        path,
        fixture.node_selection_fixture,
        fixture.node_selection_fixture_sha256,
    )
    node_selection = load_node_selection_fixture(node_selection_path)
    cohort = TransferCohort(
        selected_cases=(*selection.selected_cases, *node_selection.selected_cases),
        sources=(
            CohortSource(
                adapter="openrca2",
                fixture=fixture.selection_fixture,
                fixture_sha256=fixture.selection_fixture_sha256,
                case_ids=tuple(case.opaque_case_id for case in selection.selected_cases),
            ),
            CohortSource(
                adapter="rca100",
                fixture=fixture.node_selection_fixture,
                fixture_sha256=fixture.node_selection_fixture_sha256,
                case_ids=tuple(case.opaque_case_id for case in node_selection.selected_cases),
            ),
        ),
    )
    case_ids = [case.opaque_case_id for case in cohort.selected_cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("transfer cohort contains duplicate opaque case IDs")
    if case_ids != sorted(case_ids):
        raise ValueError("transfer cohort case IDs are out of schedule order")
    schedule = formal_schedule(fixture, cohort)
    expected_cells = (
        len(cohort.selected_cases)
        * len(fixture.models)
        * len(fixture.visibility_levels)
        * fixture.repetitions_per_model
    )
    if fixture.expected_cells != expected_cells or len(schedule) != fixture.expected_cells:
        raise ValueError("transfer protocol cell count drifted")
    paid = fixture.paid_execution
    if not (
        paid.explicit_user_approval_required_per_batch
        and paid.no_model_gates_required
        and paid.pricing_snapshot_required_at_execution
    ):
        raise ValueError("transfer paid-execution contract drifted")
    inference = fixture.inference
    expected_families = (
        (
            "storage_shape",
            "secondary",
            Visibility.SPLIT_PILLARS,
            Visibility.RAW,
            ("provider_visible_input_tokens", "correct_completion_tool_calls"),
        ),
        (
            "semantic_layer",
            "primary",
            Visibility.RAW,
            Visibility.SEMANTIC_GRAPH,
            ("rows_returned", "correct_completion_tool_calls"),
        ),
    )
    observed_families = tuple(
        (family.goal, family.status, family.baseline, family.treatment, family.metrics)
        for family in inference.confirmatory_families
    )
    if (
        not inference.semantic_effect_compared_within_model_only
        or inference.correctness_pooled_across_models
        or not inference.case_is_independent_unit
        or not inference.repetitions_are_descriptive
        or inference.primary_metrics != ("rows_returned", "correct_completion_tool_calls")
        # `m` is the number of hypotheses in one family: every model tested on
        # every endpoint of that family. It is not the case count, which is the
        # unit the deltas are reduced over.
        or inference.holm_family_size != len(fixture.models) * 2
        or observed_families != expected_families
        or inference.rows_returned_applicable_treatments
        != (Visibility.RAW, Visibility.SEMANTIC_GRAPH)
        or inference.null_metric_meaning != "not estimable; no eligible paired cases"
        or inference.tied_calls_meaning != "no observed tool-call reduction in the cohort"
        or inference.direction_consistent_non_significant_meaning
        != (
            "descriptive evidence only; report the directional case count, eligible case count, "
            "case median, unadjusted p, and Holm-adjusted p"
        )
        or inference.headline_eligibility
        != "diagnosis-correct-with-execution-valid-citation-and-reliable-run"
        or inference.evidence_sufficiency_role
        != "secondary-deterministic-audit-not-headline-eligibility"
    ):
        raise ValueError("transfer inference contract drifted")
    adjudication = fixture.semantic_adjudication
    if (
        adjudication.enabled
        or adjudication.trigger
        != "diagnosis-correct-execution-valid-deterministic-grounding-incomplete"
        or adjudication.judge_models != ("claude-sonnet-5", "deepseek-v4-flash")
        or adjudication.decision_rule != "unanimous-sufficient; disagreement-human-tiebreak"
        or not adjudication.deterministic_hard_gates_remain_authoritative
        or not adjudication.publish_deterministic_and_adjudicated_results
    ):
        raise ValueError("transfer semantic adjudication contract drifted")
    formal_models = {model.model for model in fixture.models}
    if formal_models.intersection(adjudication.judge_models):
        raise ValueError("transfer adjudication judges must be outside the formal roster")
    if any(model not in MODEL_PRICING for model in adjudication.judge_models):
        raise ValueError("transfer adjudication judge has no pricing contract")
    return fixture, cohort


def formal_schedule(
    fixture: TransferProtocolFixture,
    cohort: TransferCohort,
) -> list[dict[str, object]]:
    levels = list(fixture.visibility_levels)
    repetitions = fixture.repetitions_per_model
    # One balanced sequence covering every (case, repetition) slot, then rotated
    # per model so a case-specific effect cannot line up with one treatment
    # across the whole roster.
    balanced = counterbalanced_orders(
        levels,
        orderings=len(cohort.selected_cases) * repetitions,
    )
    schedule = []
    for case_index, case in enumerate(cohort.selected_cases):
        for model_index, model in enumerate(fixture.models):
            for repetition in range(repetitions):
                order = rotate_levels(
                    balanced[case_index * repetitions + repetition],
                    levels,
                    model_index % len(levels),
                )
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bound_path(root_file: Path, relative: str, expected_sha256: str) -> Path:
    candidate = Path(relative)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.extend(parent / candidate for parent in root_file.resolve().parents)
    path = next((item for item in candidates if item.is_file()), None)
    if path is None or sha256_file(path) != expected_sha256:
        raise ValueError(f"transfer bound file drifted: {relative}")
    return path
