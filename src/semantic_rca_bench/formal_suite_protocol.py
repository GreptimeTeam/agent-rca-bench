from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from semantic_rca_bench.contracts import AgentRunner, Visibility
from semantic_rca_bench.discovery import DiscoveryFixture
from semantic_rca_bench.graph_benchmark import GraphFixture
from semantic_rca_bench.protocol import (
    benchmark_protocol,
    discovery_protocol,
    graph_protocol,
    run_orders,
)
from semantic_rca_bench.transfer_protocol import (
    TransferProtocolFixture,
    formal_schedule,
    load_transfer_protocol,
)

SUITE_PROTOCOL_REVISION = "semantic-rca-five-model-suite-v15"
DEFAULT_SUITE_PROTOCOL_FIXTURE = Path("fixtures/reference/semantic-rca-v32-five-model-suite.json")


class MicroCaseContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark: Literal["discovery", "graph"]
    adapter: Literal["openrca", "openrca2"]
    source_case: str
    fixture: str
    fixture_sha256: str


class MicroExecutionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    visibility_levels: tuple[Visibility, ...]
    repetitions_per_model: int
    treatment_order_seed: int
    parallel_runs: int
    sampling: str
    execution_order: str


class FormalSuiteProtocolFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    protocol_revision: str
    benchmark_protocol_version: int
    greptimedb_revision: str
    greptimedb_build_profile: Literal["release"]
    runner: AgentRunner
    transfer_protocol_fixture: str
    transfer_protocol_fixture_sha256: str
    discovery_selection_fixture: str
    discovery_selection_fixture_sha256: str
    graph_selection_fixture: str
    graph_selection_fixture_sha256: str
    discovery_protocol: dict[str, object]
    graph_protocol: dict[str, object]
    micro_execution: MicroExecutionContract
    micro_cases: tuple[MicroCaseContract, ...]
    expected_micro_cells: int = Field(gt=0)
    expected_transfer_cells: int = Field(gt=0)
    expected_total_cells: int = Field(gt=0)


def load_formal_suite_protocol(
    path: Path = DEFAULT_SUITE_PROTOCOL_FIXTURE,
) -> tuple[FormalSuiteProtocolFixture, TransferProtocolFixture]:
    fixture = FormalSuiteProtocolFixture.model_validate_json(path.read_text())
    if fixture.version != 1 or fixture.protocol_revision != SUITE_PROTOCOL_REVISION:
        raise ValueError("unsupported formal suite protocol revision")
    if fixture.benchmark_protocol_version != benchmark_protocol()["version"]:
        raise ValueError("formal suite benchmark protocol drifted")
    if fixture.greptimedb_build_profile != "release":
        raise ValueError("formal suite GreptimeDB build profile drifted")
    if fixture.runner is not AgentRunner.API:
        raise ValueError("formal suite must use the API runner")
    if fixture.discovery_protocol != discovery_protocol():
        raise ValueError("formal suite Discovery protocol drifted")
    if fixture.graph_protocol != graph_protocol():
        raise ValueError("formal suite Graph protocol drifted")

    execution = fixture.micro_execution
    if (
        execution.visibility_levels != (Visibility.RAW, Visibility.SEMANTIC_GRAPH)
        or execution.repetitions_per_model != 2
        or execution.treatment_order_seed != 0
        or execution.parallel_runs != 1
        or execution.sampling != "provider-default; no seed sent"
        or execution.execution_order
        != "benchmark cases in manifest order; models in transfer roster order; "
        "seeded rotating treatments within each model"
    ):
        raise ValueError("formal suite micro execution contract drifted")

    root = _repository_root(path)
    transfer_path = _bound_path(
        root,
        fixture.transfer_protocol_fixture,
        fixture.transfer_protocol_fixture_sha256,
    )
    transfer, transfer_selection = load_transfer_protocol(transfer_path)
    if transfer.benchmark_protocol_version != fixture.benchmark_protocol_version:
        raise ValueError("formal suite and transfer benchmark protocols disagree")
    _bound_path(
        root,
        fixture.discovery_selection_fixture,
        fixture.discovery_selection_fixture_sha256,
    )
    _bound_path(
        root,
        fixture.graph_selection_fixture,
        fixture.graph_selection_fixture_sha256,
    )

    expected_cases = (
        ("discovery", "openrca", "Bank/task_5@2021-03-04T20:00"),
        ("discovery", "openrca", "Bank/task_6@2021-03-25T09:00"),
        ("discovery", "openrca", "Market/cloudbed-1@2022-03-20T09:30"),
        ("discovery", "openrca", "Market/cloudbed-2@2022-03-20T14:30"),
        ("discovery", "openrca", "Telecom@2020-05-29T03:30"),
        ("discovery", "openrca", "Telecom@2020-05-23T04:30"),
        ("graph", "openrca2", "hs4-geo-pod-failure-pdt289"),
        ("graph", "openrca2", "hs1-rate-pod-failure-vmvtxr"),
    )
    observed_cases = tuple(
        (case.benchmark, case.adapter, case.source_case) for case in fixture.micro_cases
    )
    if observed_cases != expected_cases:
        raise ValueError("formal suite micro case roster drifted")
    for case in fixture.micro_cases:
        fixture_path = _bound_path(root, case.fixture, case.fixture_sha256)
        if case.benchmark == "discovery":
            bound = DiscoveryFixture.model_validate_json(fixture_path.read_text())
        else:
            bound = GraphFixture.model_validate_json(fixture_path.read_text())
        if bound.source_case != case.source_case:
            raise ValueError("formal suite case and task fixture disagree")

    micro_cells = micro_schedule(fixture, transfer)
    transfer_cells = formal_schedule(transfer, transfer_selection)
    if (
        len(micro_cells) != fixture.expected_micro_cells
        or len(transfer_cells) != fixture.expected_transfer_cells
        or len(micro_cells) + len(transfer_cells) != fixture.expected_total_cells
    ):
        raise ValueError("formal suite cell count drifted")
    return fixture, transfer


def micro_schedule(
    fixture: FormalSuiteProtocolFixture,
    transfer: TransferProtocolFixture,
) -> list[dict[str, object]]:
    schedule: list[dict[str, object]] = []
    levels = list(fixture.micro_execution.visibility_levels)
    orders = run_orders(
        levels,
        fixture.micro_execution.repetitions_per_model,
        seed=fixture.micro_execution.treatment_order_seed,
    )
    for case_index, case in enumerate(fixture.micro_cases):
        for model_index, model in enumerate(transfer.models):
            for repetition, order in enumerate(orders):
                for position, visibility in enumerate(order):
                    schedule.append(
                        {
                            "cell_index": len(schedule),
                            "benchmark": case.benchmark,
                            "case_index": case_index,
                            "source_case": case.source_case,
                            "model_index": model_index,
                            "model": model.model,
                            "provider": model.provider,
                            "api_transport": model.api_transport.value,
                            "prompt_cache": model.prompt_cache,
                            "reasoning_effort": model.reasoning_effort,
                            "max_output_tokens": model.max_output_tokens,
                            "repetition": repetition,
                            "position": position,
                            "visibility": visibility.value,
                        }
                    )
    return schedule


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_path(protocol_path: Path, relative: str) -> Path:
    return _repository_root(protocol_path) / relative


def _bound_path(root: Path, relative: str, expected_sha256: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise ValueError(f"formal suite bound file is missing: {relative}")
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"formal suite bound file drifted: {relative}")
    return path


def _repository_root(path: Path) -> Path:
    resolved = path.resolve()
    for parent in (resolved.parent, *resolved.parents):
        if (parent / "pyproject.toml").is_file() and (parent / "fixtures").is_dir():
            return parent
    raise ValueError(f"cannot locate repository root for formal suite fixture: {path}")
