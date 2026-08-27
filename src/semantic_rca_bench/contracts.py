from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Visibility(StrEnum):
    RAW = "raw"
    TABLE_SEMANTICS = "table_semantics"
    SEMANTIC_GRAPH = "semantic_graph"


class FaultCategory(StrEnum):
    CPU = "cpu"
    DELAY = "delay"
    DISK = "disk"
    LOSS = "loss"
    MEMORY = "memory"
    SOCKET = "socket"
    OTHER = "other"


class GroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True)

    component: str
    fault_type: str
    inject_time: int | None
    fault_category: FaultCategory | None = None


class CaseInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_token: str
    time_start: int
    time_end: int
    alert_time: int
    database: str = "rca_bench"
    alert_text: str | None = None
    fault_taxonomy: list[str] = Field(default_factory=list)


class RCAEvalCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_case: str
    dataset: str
    system: str
    root: Path
    input: CaseInput
    ground_truth: GroundTruth
    metrics_path: Path
    logs_path: Path | None
    traces_path: Path | None


class RCA100Case(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_case: str
    dataset: str
    system: str
    root: Path
    input: CaseInput
    ground_truth: GroundTruth
    metrics_path: Path
    logs_path: Path
    traces_path: Path
    events_path: Path
    alerts_path: Path
    topology_path: Path


class OpenRCACase(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_case: str
    dataset: str
    system: str
    root: Path
    input: CaseInput
    ground_truth: GroundTruth
    metric_app_path: Path
    metric_container_path: Path
    logs_path: Path
    traces_path: Path


class IngestCounts(BaseModel):
    metric_source_rows: int = 0
    metrics_samples: int = 0
    metric_unique_samples: int = 0
    metric_duplicate_samples: int = 0
    metric_conflicting_timestamps: int = 0
    log_records: int = 0
    trace_spans: int = 0
    event_records: int = 0
    alert_records: int = 0
    rejected_trace_spans: int = 0
    remapped_trace_ids: int = 0
    remapped_span_ids: int = 0


class QueryResult(BaseModel):
    query_id: str
    columns: list[str]
    rows: list[list[object]]
    elapsed_seconds: float
    truncated: bool = False


class Evidence(BaseModel):
    query_id: str
    claim: str


class Diagnosis(BaseModel):
    root_cause_component: str
    fault_category: FaultCategory
    fault_type: str
    onset_time: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    alternative_candidates: list[str] = Field(default_factory=list, max_length=3)
    explanation: str


class ToolTrace(BaseModel):
    tool_name: str
    input: dict[str, Any]
    query_id: str | None = None
    output: Any = None
    error: str | None = None


class AgentUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class DatabaseLoad(BaseModel):
    query_count: int = 0
    failed_query_count: int = 0
    rows_returned: int = 0
    query_elapsed_seconds: float = 0
    max_concurrency: int = 0


class AgentRun(BaseModel):
    run_id: str
    visibility: Visibility
    model: str
    diagnosis: Diagnosis
    tool_calls: list[ToolTrace]
    tool_calls_requested: int = 0
    tool_budget_exhausted: bool = False
    usage: AgentUsage
    elapsed_seconds: float
    responses: list[dict[str, object]]


class Evaluation(BaseModel):
    component_match: bool
    fault_type_match: bool
    fault_category_match: bool
    joint_match: bool
    predicted_fault_type: str
    expected_fault_type: str
    predicted_fault_category: str
    expected_fault_category: str
    onset_error_seconds: float | None
    cited_evidence_count: int
    valid_evidence_count: int
