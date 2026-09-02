"""RCA100 node-layer cases for the end-to-end transfer benchmark.

The OpenRCA2 cohort localises faults to a service or a directed edge between two
services. RCA100 adds the layer below: the alert is a service symptom and the
labelled root cause is the Kubernetes node the affected workloads run on, so a
correct answer has to cross service -> instance -> pod -> node.

Selection is typicality-first: one case per labelled node fault type, and within
a fault type the instance closest to that population's median signal profile.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from semantic_rca_bench.contracts import (
    CausalScope,
    FaultCategory,
    MechanismCode,
    RCA100Case,
)
from semantic_rca_bench.datasets.openrca2_transfer import (
    ScopePreservingPredicate,
    SourceMechanismEvidence,
    TransferCaseSpec,
)
from semantic_rca_bench.datasets.rca100 import (
    DATASET_REVISION,
    SOURCE_REVISION,
    RCA100Error,
    RCA100Repository,
)

SELECTION_REVISION = "rca100-node-layer-typicality-v1"
SELECTION_SEED = "semantic-rca-v1-rca100-node-4"
NODE_ENTITY_TYPE = "k8s.node"
NODE_ENTITY_SET = "k8s.node"
CASE_ID_OFFSET = 10
MINIMUM_ANOMALOUS_OBSERVATIONS = 2

MECHANISM_QUOTAS: dict[MechanismCode, int] = {
    MechanismCode.CPU_SATURATION: 1,
    MechanismCode.MEMORY_PRESSURE: 1,
    MechanismCode.DISK_IO_DEGRADATION: 1,
    MechanismCode.HOST_UNAVAILABLE: 1,
}


class NodeOracle(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    threshold: float
    predicate: Literal["node_cpu_saturation", "node_memory_pressure"]


class FaultProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    mechanism_code: MechanismCode
    fault_category: FaultCategory
    # None when no node metric expresses the mechanism as a threshold crossing.
    # The case still scores diagnosis correctness; its secondary evidence audit
    # reports as not estimable.
    oracle: NodeOracle | None = None


FAULT_PROFILES: dict[str, FaultProfile] = {
    "nodeCpuHigh": FaultProfile(
        mechanism_code=MechanismCode.CPU_SATURATION,
        fault_category=FaultCategory.CPU,
        oracle=NodeOracle(
            metric="node_cpu_usage_rate",
            threshold=50.0,
            predicate="node_cpu_saturation",
        ),
    ),
    "nodeMemoryOOM": FaultProfile(
        mechanism_code=MechanismCode.MEMORY_PRESSURE,
        fault_category=FaultCategory.MEMORY,
        oracle=NodeOracle(
            metric="node_memory_usage_rate",
            threshold=50.0,
            predicate="node_memory_pressure",
        ),
    ),
    # The node metric set carries disk capacity but no IO rate, so the fault has
    # no direct node-layer signal.
    "diskIOHigh": FaultProfile(
        mechanism_code=MechanismCode.DISK_IO_DEGRADATION,
        fault_category=FaultCategory.DISK,
    ),
    # Readiness is only emitted while the node is unready, so there is no
    # baseline period for the deterministic transition audit to compare against.
    "nodeDown": FaultProfile(
        mechanism_code=MechanismCode.HOST_UNAVAILABLE,
        fault_category=FaultCategory.OTHER,
    ),
}

SOURCE_FILES = (
    "task.json",
    "metrics.parquet",
    "logs.parquet",
    "traces.parquet",
    "events.parquet",
    "alerts.parquet",
    "topology.json",
)


class NodeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_case: str
    node: str
    source_fault_type: str


class CandidateProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_case: str
    node: str
    source_fault_type: str
    normal_max: float | None = None
    abnormal_max: float | None = None
    peer_abnormal_max: float | None = None
    oracle_available: bool = False
    # Set only by source-fidelity exclusions, never by whether the deterministic
    # transition audit can run.
    rejection: str | None = None


class RCA100SelectionFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    selection_revision: str
    dataset_revision: str
    source_revision: str
    seed: str
    case_role: Literal["development", "measurement"]
    quotas: dict[MechanismCode, int]
    candidate_profiles: tuple[CandidateProfile, ...]
    selected_cases: tuple[TransferCaseSpec, ...]


def node_candidates(repository: RCA100Repository, task_ids: tuple[str, ...]) -> list[NodeCandidate]:
    """Every case whose labelled root cause is one Kubernetes node."""
    candidates = []
    for task_id in task_ids:
        truth = repository.fetch_answer_key(task_id)
        entities = truth.get("root_cause_entities")
        types = truth.get("root_cause_types")
        if not isinstance(entities, list) or len(entities) != 1:
            continue
        if not isinstance(types, list) or len(types) != 1:
            continue
        if _target_entity_type(truth) != NODE_ENTITY_TYPE:
            continue
        candidates.append(
            NodeCandidate(
                source_case=task_id,
                node=str(entities[0]),
                source_fault_type=str(types[0]),
            )
        )
    return sorted(candidates, key=lambda item: item.source_case)


def profile_candidate(case_root: Path, candidate: NodeCandidate) -> CandidateProfile:
    """Measure a candidate's signal shape from the source metrics alone.

    The baseline is the equally long window immediately before the alert window,
    matching how the OpenRCA2 cases split their declared periods.
    """
    profile = FAULT_PROFILES.get(candidate.source_fault_type)
    oracle = profile.oracle if profile is not None else None
    if oracle is None:
        return CandidateProfile(
            source_case=candidate.source_case,
            node=candidate.node,
            source_fault_type=candidate.source_fault_type,
            oracle_available=False,
        )
    alert_start, alert_end = _alert_window(case_root)
    metrics_path = case_root / "metrics.parquet"
    normal = _metric_period(
        metrics_path, oracle, candidate.node, (alert_start - (alert_end - alert_start), alert_start)
    )
    abnormal = _metric_period(metrics_path, oracle, candidate.node, (alert_start, alert_end))
    peer_max = _peer_maximum(metrics_path, oracle, candidate.node, (alert_start, alert_end))
    return CandidateProfile(
        source_case=candidate.source_case,
        node=candidate.node,
        source_fault_type=candidate.source_fault_type,
        normal_max=float(normal["max"]),
        abnormal_max=float(abnormal["max"]),
        peer_abnormal_max=peer_max,
        # Whether the transition audit can run decides only whether this case
        # carries an oracle, never whether it is in the cohort: two fault types
        # here have no node metric at all and are still measured.
        oracle_available=transition_proven(normal, abnormal),
    )


def mark_duplicate_incidents(
    profiles: list[CandidateProfile],
    windows: Mapping[str, tuple[int, int]],
) -> list[CandidateProfile]:
    """Reject the later alert of a pair that observes one underlying incident.

    RCA100 raises several alerts per injection, so two cases can name the same
    root-cause node over overlapping windows. Treating both as measurement cases
    would break the contract that cases are the independent unit.
    """
    kept: list[CandidateProfile] = []
    for profile in sorted(profiles, key=lambda item: item.source_case):
        start, end = windows[profile.source_case]
        duplicate = any(
            other.node == profile.node
            and other.rejection is None
            and start < windows[other.source_case][1]
            and windows[other.source_case][0] < end
            for other in kept
        )
        kept.append(
            profile.model_copy(update={"rejection": "duplicate_incident"}) if duplicate else profile
        )
    return sorted(kept, key=lambda item: item.source_case)


def typical_case_per_fault_type(profiles: list[CandidateProfile]) -> dict[str, str]:
    """Pick, per fault type, the instance closest to that type's median profile.

    Typicality is measured on the signal the fault produces: how quiet the
    baseline is, how far the anomaly goes, and how much the rest of the cluster
    moves at the same time. Every source-eligible instance ranks, including ones
    whose transition audit cannot run; fault types the node metrics cannot
    express have one instance each and are typical by construction.
    """
    by_fault: dict[str, list[CandidateProfile]] = {}
    for profile in profiles:
        if profile.rejection is None:
            by_fault.setdefault(profile.source_fault_type, []).append(profile)
    selected = {}
    for fault_type, items in by_fault.items():
        measured = [item for item in items if item.normal_max is not None]
        if not measured:
            selected[fault_type] = min(item.source_case for item in items)
            continue
        medians = [
            _median([getattr(item, field) for item in measured])
            for field in ("normal_max", "abnormal_max", "peer_abnormal_max")
        ]
        ranked = sorted(
            measured,
            key=lambda item: (
                sum(
                    abs(getattr(item, field) - median)
                    for field, median in zip(
                        ("normal_max", "abnormal_max", "peer_abnormal_max"), medians, strict=True
                    )
                ),
                item.source_case,
            ),
        )
        selected[fault_type] = ranked[0].source_case
    return selected


def _alert_window(case_root: Path) -> tuple[int, int]:
    task = json.loads((case_root / "task.json").read_text())
    window = task.get("alert_window")
    if not isinstance(window, Mapping):
        raise RCA100Error(f"missing alert_window in {case_root.name}")
    start = int(datetime.fromisoformat(str(window["start"])).timestamp())
    end = int(datetime.fromisoformat(str(window["end"])).timestamp())
    return start, end


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _peer_maximum(
    path: Path,
    oracle: NodeOracle,
    node: str,
    window: tuple[int, int],
) -> float:
    table = pq.read_table(
        path, columns=["time", "metric", "entity_set", "entity_name", "value"]
    ).to_pydict()
    values = [
        float(table["value"][index])
        for index in range(len(table["time"]))
        if table["metric"][index] == oracle.metric
        and table["entity_set"][index] == NODE_ENTITY_SET
        and table["entity_name"][index] != node
        and table["value"][index] is not None
        and window[0] <= int(table["time"][index]) // 1_000_000 < window[1]
    ]
    return max(values) if values else 0.0


def build_selection(
    repository: RCA100Repository,
    task_ids: tuple[str, ...],
    case_role: Literal["development", "measurement"],
) -> RCA100SelectionFixture:
    """Rank the whole node-fault population and freeze one case per fault type."""
    candidates = node_candidates(repository, task_ids)
    profiles = [
        profile_candidate(repository.cache_dir / candidate.source_case, candidate)
        for candidate in candidates
    ]
    windows = {
        candidate.source_case: _alert_window(repository.cache_dir / candidate.source_case)
        for candidate in candidates
    }
    profiles = mark_duplicate_incidents(profiles, windows)
    typical = typical_case_per_fault_type(profiles)
    by_source = {candidate.source_case: candidate for candidate in candidates}
    ordered = [
        typical[fault_type]
        for mechanism in MECHANISM_QUOTAS
        for fault_type, profile in FAULT_PROFILES.items()
        if profile.mechanism_code is mechanism and fault_type in typical
    ]
    if len(ordered) != sum(MECHANISM_QUOTAS.values()):
        raise RCA100Error("RCA100 node selection did not fill every mechanism quota")
    selected = tuple(
        audit_source_case(
            repository.fetch_case(by_source[source_case].source_case),
            opaque_case_id(index),
            case_role,
        )
        for index, source_case in enumerate(ordered, start=1)
    )
    return RCA100SelectionFixture(
        version=1,
        selection_revision=SELECTION_REVISION,
        dataset_revision=DATASET_REVISION,
        source_revision=SOURCE_REVISION,
        seed=SELECTION_SEED,
        case_role=case_role,
        quotas=MECHANISM_QUOTAS,
        candidate_profiles=tuple(profiles),
        selected_cases=selected,
    )


def audit_source_case(
    case: RCA100Case,
    opaque_case_id: str,
    case_role: Literal["development", "measurement"],
) -> TransferCaseSpec:
    """Derive the frozen case contract from the source archive alone."""
    fault_type = case.ground_truth.fault_type
    profile = FAULT_PROFILES.get(fault_type)
    if profile is None:
        raise RCA100Error(f"{case.source_case} declares an unmodelled node fault: {fault_type}")
    node = case.ground_truth.causal_component
    alert_start, alert_end = case.input.time_start, case.input.time_end
    span = alert_end - alert_start
    if span <= 0:
        raise RCA100Error(f"{case.source_case} has an empty alert window")
    normal_window = (alert_start - span, alert_start)
    abnormal_window = (alert_start, alert_end)
    evidence = (
        _node_metric_evidence(case, profile.oracle, node, normal_window, abnormal_window)
        if profile.oracle is not None
        else None
    )
    return TransferCaseSpec(
        opaque_case_id=opaque_case_id,
        source_case=case.source_case,
        system=case.system,
        case_role=case_role,
        causal_scope=CausalScope.INFRASTRUCTURE_NODE,
        causal_component=node,
        edge_source=None,
        edge_destination=None,
        fault_category=profile.fault_category,
        mechanism_code=profile.mechanism_code,
        source_fault_type=fault_type,
        normal_window=normal_window,
        abnormal_window=abnormal_window,
        mechanism_evidence=evidence,
        source_files_sha256=_source_files_sha256(case.root),
    )


def transition_proven(
    normal: Mapping[str, int | float],
    abnormal: Mapping[str, int | float],
) -> bool:
    """Whether both declared periods support the deterministic transition audit."""
    return (
        normal["count"] >= 1
        and abnormal["count"] >= MINIMUM_ANOMALOUS_OBSERVATIONS
        and normal["high_count"] == 0
        and abnormal["high_count"] >= MINIMUM_ANOMALOUS_OBSERVATIONS
    )


def _node_metric_evidence(
    case: RCA100Case,
    oracle: NodeOracle,
    node: str,
    normal_window: tuple[int, int],
    abnormal_window: tuple[int, int],
) -> SourceMechanismEvidence | None:
    normal = _metric_period(case.metrics_path, oracle, node, normal_window)
    abnormal = _metric_period(case.metrics_path, oracle, node, abnormal_window)
    if not transition_proven(normal, abnormal):
        return None
    return SourceMechanismEvidence(
        predicate=oracle.predicate,
        source_table=oracle.metric,
        value_column="greptime_value",
        identity_column="entity_name",
        identity_value=node,
        threshold=oracle.threshold,
        minimum_anomalous_observations=MINIMUM_ANOMALOUS_OBSERVATIONS,
        scope_preserving_predicates=(
            ScopePreservingPredicate(column="entity_set", value=NODE_ENTITY_SET),
        ),
        normal=normal,
        abnormal=abnormal,
    )


def _metric_period(
    path: Path,
    oracle: NodeOracle,
    node: str,
    window: tuple[int, int],
) -> dict[str, int | float]:
    values = []
    table = pq.read_table(
        path, columns=["time", "metric", "entity_set", "entity_name", "value"]
    ).to_pydict()
    for index in range(len(table["time"])):
        if table["metric"][index] != oracle.metric or table["entity_name"][index] != node:
            continue
        if table["entity_set"][index] != NODE_ENTITY_SET:
            continue
        value = table["value"][index]
        if value is None:
            continue
        seconds = int(table["time"][index]) // 1_000_000
        if not window[0] <= seconds < window[1]:
            continue
        values.append(float(value))
    if not values:
        return {"count": 0, "min": 0.0, "max": 0.0, "high_count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "high_count": sum(value >= oracle.threshold for value in values),
    }


def _source_files_sha256(root: Path) -> dict[str, str]:
    digests = {}
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file():
            raise RCA100Error(f"missing source file for {root.name}: {name}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digests[name] = digest.hexdigest()
    return digests


def _target_entity_type(truth: Mapping[str, object]) -> str | None:
    raw = truth.get("raw_ground_truth")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    outcome = parsed.get("outcome") if isinstance(parsed, Mapping) else None
    entities = outcome.get("target_entities") if isinstance(outcome, Mapping) else None
    if not isinstance(entities, list) or len(entities) != 1:
        return None
    entity = entities[0]
    return str(entity.get("entity_type")) if isinstance(entity, Mapping) else None


def opaque_case_id(index: int) -> str:
    """Continue the cohort's opaque numbering so the dataset stays unnamed."""
    return f"semantic-rca-transfer-{CASE_ID_OFFSET + index:03d}"


def load_selected_case(
    repository: RCA100Repository,
    spec: TransferCaseSpec,
    *,
    database: str,
) -> RCA100Case:
    """Re-derive a frozen case from source and reject any drift.

    fault_taxonomy is cleared so every case in the merged cohort offers the agent
    the same answer surface; RCA100 would otherwise supply its own 28-label
    vocabulary while the OpenRCA2 cases supply none.
    """
    source = repository.fetch_case(spec.source_case)
    observed = audit_source_case(source, spec.opaque_case_id, spec.case_role)
    if observed != spec:
        raise RCA100Error(f"RCA100 transfer source binding drifted: {spec.source_case}")
    return source.model_copy(
        update={
            "input": source.input.model_copy(
                update={
                    "case_token": spec.opaque_case_id,
                    "database": database,
                    "time_start": spec.normal_window[0],
                    "time_end": spec.abnormal_window[1],
                    "alert_text": _alert_title(source.root),
                    "fault_taxonomy": [],
                }
            )
        }
    )


def _alert_title(case_root: Path) -> str:
    task = json.loads((case_root / "task.json").read_text())
    return str(task.get("alert_title") or "Generic telemetry anomaly")


def load_selection_fixture(path: Path) -> RCA100SelectionFixture:
    fixture = RCA100SelectionFixture.model_validate_json(path.read_text())
    if (
        fixture.version != 1
        or fixture.selection_revision != SELECTION_REVISION
        or fixture.dataset_revision != DATASET_REVISION
        or fixture.source_revision != SOURCE_REVISION
        or fixture.seed != SELECTION_SEED
        or fixture.quotas != MECHANISM_QUOTAS
    ):
        raise ValueError("unsupported RCA100 transfer selection fixture")
    if len(fixture.selected_cases) != sum(MECHANISM_QUOTAS.values()):
        raise ValueError("RCA100 transfer selection has the wrong case count")
    expected_ids = tuple(
        opaque_case_id(index) for index in range(1, len(fixture.selected_cases) + 1)
    )
    if tuple(case.opaque_case_id for case in fixture.selected_cases) != expected_ids:
        raise ValueError("RCA100 transfer opaque case IDs drifted")
    if Counter(case.mechanism_code for case in fixture.selected_cases) != Counter(MECHANISM_QUOTAS):
        raise ValueError("RCA100 transfer mechanism quotas drifted")
    if any(
        case.causal_scope is not CausalScope.INFRASTRUCTURE_NODE for case in fixture.selected_cases
    ):
        raise ValueError("RCA100 transfer cases must localise to a node")
    if len({case.source_case for case in fixture.selected_cases}) != len(fixture.selected_cases):
        raise ValueError("RCA100 transfer selection contains duplicate source cases")
    # The recorded ranking is what makes the selection auditable, so replay it:
    # a fixture whose selected cases are not the ones its own profiles rank first
    # is a selection contract violation, not merely a stale file.
    ranked = typical_case_per_fault_type(list(fixture.candidate_profiles))
    if sorted(ranked.values()) != sorted(case.source_case for case in fixture.selected_cases):
        raise ValueError("RCA100 transfer selection does not match its recorded ranking")
    for case in fixture.selected_cases:
        expected = FAULT_PROFILES[case.source_fault_type]
        if case.mechanism_code is not expected.mechanism_code:
            raise ValueError(f"RCA100 mechanism mapping drifted for {case.source_case}")
        if (case.mechanism_evidence is not None) != _profile_of(
            fixture.candidate_profiles, case.source_case
        ).oracle_available:
            raise ValueError(f"RCA100 oracle availability drifted for {case.source_case}")
    return fixture


def _profile_of(
    profiles: tuple[CandidateProfile, ...],
    source_case: str,
) -> CandidateProfile:
    for profile in profiles:
        if profile.source_case == source_case:
            return profile
    raise ValueError(f"RCA100 selected case is absent from the ranking: {source_case}")
