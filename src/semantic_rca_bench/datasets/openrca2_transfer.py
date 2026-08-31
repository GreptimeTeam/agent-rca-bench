from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from semantic_rca_bench.contracts import (
    CausalScope,
    FaultCategory,
    MechanismCode,
    OpenRCA2Case,
    QueryResult,
)
from semantic_rca_bench.datasets.openrca2 import (
    DATASET_REVISION,
    SOURCE_REVISION,
    OpenRCA2Error,
    _iter_rows,
    _load_case,
    _metric_sample_audit,
    validate_ingest,
)
from semantic_rca_bench.edge_audit import (
    canonical_graph_edge_query,
    canonical_raw_edge_query,
    edge_set_sha256,
    graph_audit_window,
    normalize_edge_result,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient

SELECTION_REVISION = "openrca2-fresh-end-to-end-v1"
SELECTION_SEED = "semantic-rca-v1-openrca2-transfer-10"
PILOT_REVISION = "openrca2-development-pilot-v1"
MECHANISM_QUOTAS = {
    MechanismCode.WORKLOAD_RESTART: 4,
    MechanismCode.CALL_PATH_DELAY: 3,
    MechanismCode.CPU_SATURATION: 2,
    MechanismCode.MEMORY_PRESSURE: 1,
}
SYSTEM_QUOTAS = {
    "workload_restart/hs": 3,
    "workload_restart/otel-demo": 1,
    "call_path_delay/hs": 2,
    "call_path_delay/otel-demo": 1,
    "cpu_saturation/hs": 2,
    "memory_pressure/hs": 1,
}

_FAULT_MECHANISMS = {
    "PodFailure": (FaultCategory.OTHER, MechanismCode.WORKLOAD_RESTART),
    "NetworkDelay": (FaultCategory.DELAY, MechanismCode.CALL_PATH_DELAY),
    "CPUStress": (FaultCategory.CPU, MechanismCode.CPU_SATURATION),
    "MemoryStress": (FaultCategory.MEMORY, MechanismCode.MEMORY_PRESSURE),
}


class ScopePreservingPredicate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    column: str
    value: str


class SourceMechanismEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate: Literal[
        "workload_restart_transition",
        "call_path_start_gap_transition",
        "container_cpu_saturation",
        "container_memory_pressure",
    ]
    source_table: str
    value_column: str
    identity_column: str | None = None
    identity_value: str | None = None
    threshold: float
    minimum_anomalous_observations: int
    allowed_operations: tuple[str, ...] = ()
    scope_preserving_predicates: tuple[ScopePreservingPredicate, ...] = ()
    identity_equivalent_predicates: tuple[ScopePreservingPredicate, ...] = ()
    normal: dict[str, int | float]
    abnormal: dict[str, int | float]


class TransferCaseSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    opaque_case_id: str
    source_case: str
    system: str
    case_role: Literal["development", "measurement"]
    causal_scope: CausalScope
    causal_component: str | None = None
    edge_source: str | None = None
    edge_destination: str | None = None
    fault_category: FaultCategory
    mechanism_code: MechanismCode
    source_fault_type: str
    normal_window: tuple[int, int]
    abnormal_window: tuple[int, int]
    mechanism_evidence: SourceMechanismEvidence
    source_files_sha256: dict[str, str]


class TransferSelectionFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    selection_revision: str
    dataset_revision: str
    source_revision: str
    seed: str
    case_role: Literal["development", "measurement"]
    quotas: dict[MechanismCode, int]
    system_quotas: dict[str, int]
    trajectory_exclusions: tuple[str, ...]
    ranked_candidates: dict[MechanismCode, tuple[str, ...]]
    rejected_candidates: dict[str, tuple[str, ...]]
    selected_cases: tuple[TransferCaseSpec, ...]


class TransferPilotFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    pilot_revision: str
    dataset_revision: str
    source_revision: str
    case_role: Literal["development"]
    selected_cases: tuple[TransferCaseSpec, ...]


def load_selection_fixture(path: Path) -> TransferSelectionFixture:
    fixture = TransferSelectionFixture.model_validate_json(path.read_text())
    if (
        fixture.version != 1
        or fixture.selection_revision != SELECTION_REVISION
        or fixture.dataset_revision != DATASET_REVISION
        or fixture.source_revision != SOURCE_REVISION
        or fixture.seed != SELECTION_SEED
        or fixture.quotas != MECHANISM_QUOTAS
        or fixture.system_quotas != SYSTEM_QUOTAS
    ):
        raise ValueError("unsupported OpenRCA2 transfer selection fixture")
    if len(fixture.selected_cases) != sum(MECHANISM_QUOTAS.values()):
        raise ValueError("OpenRCA2 transfer selection has the wrong case count")
    expected_ids = tuple(
        f"semantic-rca-transfer-{index:03d}" for index in range(1, len(fixture.selected_cases) + 1)
    )
    if tuple(case.opaque_case_id for case in fixture.selected_cases) != expected_ids:
        raise ValueError("OpenRCA2 transfer opaque case IDs drifted")
    counts = Counter(case.mechanism_code for case in fixture.selected_cases)
    if counts != Counter(MECHANISM_QUOTAS):
        raise ValueError("OpenRCA2 transfer mechanism quotas drifted")
    system_counts = Counter(
        f"{case.mechanism_code.value}/{_system_key(case.system)}" for case in fixture.selected_cases
    )
    if system_counts != Counter(SYSTEM_QUOTAS):
        raise ValueError("OpenRCA2 transfer system quotas drifted")
    if len({case.source_case for case in fixture.selected_cases}) != len(fixture.selected_cases):
        raise ValueError("OpenRCA2 transfer selection contains duplicate source cases")
    if set(fixture.trajectory_exclusions) & {case.source_case for case in fixture.selected_cases}:
        raise ValueError("OpenRCA2 transfer selection reused an agent-visible case")
    return fixture


def load_pilot_fixture(path: Path) -> TransferPilotFixture:
    fixture = TransferPilotFixture.model_validate_json(path.read_text())
    if (
        fixture.version != 1
        or fixture.pilot_revision != PILOT_REVISION
        or fixture.dataset_revision != DATASET_REVISION
        or fixture.source_revision != SOURCE_REVISION
        or fixture.case_role != "development"
        or len(fixture.selected_cases) != 2
        or tuple(case.opaque_case_id for case in fixture.selected_cases)
        != ("semantic-rca-pilot-001", "semantic-rca-pilot-002")
        or tuple(case.mechanism_code for case in fixture.selected_cases)
        != (MechanismCode.WORKLOAD_RESTART, MechanismCode.CALL_PATH_DELAY)
        or any(case.case_role != "development" for case in fixture.selected_cases)
    ):
        raise ValueError("unsupported OpenRCA2 transfer pilot fixture")
    return fixture


def load_selected_case(
    cache_dir: Path,
    manifest_path: Path,
    spec: TransferCaseSpec,
    *,
    database: str,
) -> OpenRCA2Case:
    source = _load_case(
        cache_dir / "cases" / spec.source_case,
        manifest_path,
        require_observable_alert=False,
    )
    observed = audit_source_case(source, spec.opaque_case_id, spec.case_role)
    if observed != spec:
        raise ValueError(f"OpenRCA2 transfer source binding drifted: {spec.source_case}")
    return source.model_copy(
        update={
            "input": source.input.model_copy(
                update={
                    "case_token": spec.opaque_case_id,
                    "database": database,
                    "alert_text": source.input.alert_text or "Generic telemetry anomaly",
                    "fault_taxonomy": [],
                }
            )
        }
    )


def audit_source_case(
    case: OpenRCA2Case,
    opaque_case_id: str,
    case_role: Literal["development", "measurement"],
) -> TransferCaseSpec:
    injection = _read_object(case.injection_path)
    fault_type = str(injection.get("fault_type") or "")
    try:
        fault_category, mechanism_code = _FAULT_MECHANISMS[fault_type]
    except KeyError as error:
        raise OpenRCA2Error(f"unsupported transfer fault type: {fault_type}") from error
    config = _single_mapping(injection, "engine_config")
    truth = _single_mapping(injection, "ground_truth")
    services = _string_list(truth, "service")
    if len(services) not in (1, 2):
        raise OpenRCA2Error("transfer case must declare one component or one directed edge")
    _validate_trace_identity(case)
    normal_window = (case.input.time_start, case.input.alert_time)
    abnormal_window = (case.input.alert_time, case.input.time_end)
    if mechanism_code is MechanismCode.CALL_PATH_DELAY:
        if config.get("direction") != "to":
            raise OpenRCA2Error("call-path delay must declare direction=to")
        edge_source = str(config.get("app") or "")
        edge_destination = str(config.get("target_service") or "")
        if services != [edge_source, edge_destination] and set(services) != {
            edge_source,
            edge_destination,
        }:
            raise OpenRCA2Error("delay ground truth disagrees with directed injection")
        evidence = _delay_evidence(case, edge_source, edge_destination, truth)
        causal_scope = CausalScope.DEPENDENCY_EDGE
        causal_component = None
    else:
        if len(services) != 1:
            raise OpenRCA2Error("component mechanism declares multiple services")
        edge_source = None
        edge_destination = None
        causal_scope = CausalScope.COMPONENT
        causal_component = services[0]
        evidence = _metric_evidence(case, mechanism_code, config, truth)
    return TransferCaseSpec(
        opaque_case_id=opaque_case_id,
        source_case=case.source_case,
        system=case.system,
        case_role=case_role,
        causal_scope=causal_scope,
        causal_component=causal_component,
        edge_source=edge_source,
        edge_destination=edge_destination,
        fault_category=fault_category,
        mechanism_code=mechanism_code,
        source_fault_type=fault_type,
        normal_window=normal_window,
        abnormal_window=abnormal_window,
        mechanism_evidence=evidence,
        source_files_sha256=_source_file_hashes(case),
    )


def deterministic_rank(candidates: Iterable[str], mechanism: MechanismCode) -> tuple[str, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda case: (
                hashlib.sha256(f"{SELECTION_SEED}:{mechanism.value}:{case}".encode()).hexdigest(),
                case,
            ),
        )
    )


def rank_manifest_candidates(
    manifest_path: Path,
    trajectory_exclusions: Iterable[str],
) -> dict[MechanismCode, tuple[str, ...]]:
    excluded = set(trajectory_exclusions)
    manifest_names = {
        str(item["name"])
        for line in manifest_path.read_text().splitlines()
        if isinstance((item := json.loads(line)), Mapping) and item.get("name")
    }
    unknown = sorted(excluded - manifest_names)
    if unknown:
        raise ValueError(f"trajectory exclusions are absent from the source manifest: {unknown}")
    candidates: dict[MechanismCode, list[str]] = {mechanism: [] for mechanism in MECHANISM_QUOTAS}
    for line in manifest_path.read_text().splitlines():
        item = json.loads(line)
        if not isinstance(item, Mapping):
            continue
        fault_type = str(item.get("primary_kind") or "")
        contract = _FAULT_MECHANISMS.get(fault_type)
        name = str(item.get("name") or "")
        roots = item.get("root_services")
        if (
            contract is None
            or name in excluded
            or item.get("system") not in {"hs", "otel-demo"}
            or item.get("hybrid") is not False
            or not isinstance(roots, list)
            or len(roots) != 1
        ):
            continue
        candidates[contract[1]].append(name)
    return {
        mechanism: deterministic_rank(names, mechanism) for mechanism, names in candidates.items()
    }


def build_selection_fixture(
    cache_dir: Path,
    manifest_path: Path,
    *,
    trajectory_exclusions: Iterable[str],
    case_role: Literal["development", "measurement"] = "measurement",
) -> TransferSelectionFixture:
    exclusions = tuple(sorted(set(trajectory_exclusions)))
    ranked = rank_manifest_candidates(manifest_path, exclusions)
    manifest_systems = _manifest_systems(manifest_path)
    rejected: dict[str, tuple[str, ...]] = {}
    provisional: list[TransferCaseSpec] = []
    selected_counts: Counter[str] = Counter()
    for mechanism in MECHANISM_QUOTAS:
        for source_case in ranked[mechanism]:
            stratum = f"{mechanism.value}/{manifest_systems[source_case]}"
            quota = SYSTEM_QUOTAS.get(stratum, 0)
            if selected_counts[stratum] >= quota:
                continue
            root = cache_dir / "cases" / source_case
            if not root.is_dir():
                raise FileNotFoundError(
                    f"ranked OpenRCA2 transfer candidate is not downloaded: {source_case}"
                )
            try:
                case = _load_case(root, manifest_path, require_observable_alert=False)
                spec = audit_source_case(case, "pending", case_role)
            except (OpenRCA2Error, ValueError) as error:
                rejected[source_case] = (str(error),)
                continue
            provisional.append(spec)
            selected_counts[stratum] += 1
            if all(
                selected_counts[key] == value
                for key, value in SYSTEM_QUOTAS.items()
                if key.startswith(f"{mechanism.value}/")
            ):
                break
        mechanism_selected = sum(
            count for key, count in selected_counts.items() if key.startswith(f"{mechanism.value}/")
        )
        if mechanism_selected != MECHANISM_QUOTAS[mechanism]:
            raise ValueError(
                f"OpenRCA2 transfer cohort cannot satisfy {mechanism.value} quota: "
                f"{mechanism_selected}/{MECHANISM_QUOTAS[mechanism]}"
            )
    selected = tuple(
        spec.model_copy(update={"opaque_case_id": f"semantic-rca-transfer-{index:03d}"})
        for index, spec in enumerate(provisional, start=1)
    )
    return TransferSelectionFixture(
        version=1,
        selection_revision=SELECTION_REVISION,
        dataset_revision=DATASET_REVISION,
        source_revision=SOURCE_REVISION,
        seed=SELECTION_SEED,
        case_role=case_role,
        quotas=MECHANISM_QUOTAS,
        system_quotas=SYSTEM_QUOTAS,
        trajectory_exclusions=exclusions,
        ranked_candidates=ranked,
        rejected_candidates=rejected,
        selected_cases=selected,
    )


def source_telemetry_audit(case: OpenRCA2Case, spec: TransferCaseSpec) -> dict[str, object]:
    observed = audit_source_case(case, spec.opaque_case_id, spec.case_role)
    if observed != spec:
        raise OpenRCA2Error("live transfer source differs from the frozen selection")
    windows = {
        "normal": spec.normal_window,
        "abnormal": spec.abnormal_window,
    }
    trace_rows = list(_iter_rows(case.traces_paths))
    trace_identity = _source_trace_identity(trace_rows)
    trace_periods = {
        period: _source_edge_period(trace_rows, window) for period, window in windows.items()
    }
    signal_periods = {
        period: _source_signal_period(
            (
                case.gauge_paths[index],
                case.sum_paths[index],
                case.histogram_paths[index],
                case.logs_paths[index],
                case.traces_paths[index],
            ),
            window,
        )
        for index, (period, window) in enumerate(windows.items())
    }
    metric = _metric_sample_audit(case)
    return {
        "source_row_counts": {
            path.name: _parquet_rows(path)
            for path in (
                *case.gauge_paths,
                *case.sum_paths,
                *case.histogram_paths,
                *case.logs_paths,
                *case.traces_paths,
            )
        },
        "metric_representation": {
            "source_rows": metric["source_rows"],
            "unique_samples": metric["unique_samples"],
            "duplicate_samples": metric["duplicate_samples"],
            "conflicting_timestamps": metric["conflicting_timestamps"],
            "protocol_rows": metric["protocol_rows"],
            "type_name_collisions": metric["type_name_collisions"],
            "sum_temporality_available": False,
            "sum_monotonicity_available": False,
            "histogram_bucket_boundaries_available": False,
        },
        "scope_preserving_predicates": [
            predicate.model_dump(mode="json")
            for predicate in spec.mechanism_evidence.scope_preserving_predicates
        ],
        "identity_equivalent_predicates": [
            predicate.model_dump(mode="json")
            for predicate in spec.mechanism_evidence.identity_equivalent_predicates
        ],
        "trace_identity": trace_identity,
        "trace_periods": trace_periods,
        "signal_periods": signal_periods,
        "all_signal_timestamps_in_declared_windows": all(
            item["all_rows_in_declared_window"] is True for item in signal_periods.values()
        ),
        "source_window_end_boundaries_empty": all(
            item["rows_at_exact_end"] == 0 for item in signal_periods.values()
        ),
        "source_identity_valid": trace_identity["valid"] is True,
        "reference_causal_graph_read": False,
        "reference_causal_graph_ingested": False,
        "source_data_modified": False,
    }


def validate_transfer_ingest(
    client: GreptimeClient,
    case: OpenRCA2Case,
    counts: object,
    source: Mapping[str, object],
) -> dict[str, object]:
    base = validate_ingest(client, case, counts)
    source_identity = _mapping(source, "trace_identity")
    stored_services = _stored_group_counts(client, "service_name")
    stored_kinds = _stored_group_counts(client, "span_kind")
    stored_statuses = _stored_group_counts(client, "span_status_code")
    identity = {
        "source_service_counts": source_identity["service_counts"],
        "stored_service_counts": stored_services,
        "service_counts_match": source_identity["service_counts"] == stored_services,
        "source_span_kind_counts": source_identity["span_kind_counts"],
        "stored_span_kind_counts": stored_kinds,
        "span_kind_counts_match": source_identity["span_kind_counts"] == stored_kinds,
        "source_status_code_counts": source_identity["status_code_counts"],
        "stored_status_code_counts": stored_statuses,
        "status_code_counts_match": source_identity["status_code_counts"] == stored_statuses,
    }
    count_dump = counts.model_dump(mode="json")
    return {
        **base,
        "source_protocol_counts": count_dump,
        "protocol_rejections_zero": (
            count_dump["rejected_metric_points"] == 0 and count_dump["rejected_trace_spans"] == 0
        ),
        "id_remapping": {
            "trace_ids": count_dump["remapped_trace_ids"],
            "span_ids": count_dump["remapped_span_ids"],
            "pass": count_dump["remapped_trace_ids"] == 0 and count_dump["remapped_span_ids"] == 0,
        },
        "source_identity": identity,
    }


def exact_edge_equality_audit(
    client: GreptimeClient,
    spec: TransferCaseSpec,
    source: Mapping[str, object],
) -> dict[str, object]:
    periods = {"normal": spec.normal_window, "abnormal": spec.abnormal_window}
    source_periods = _mapping(source, "trace_periods")
    raw_periods: dict[str, dict[str, object]] = {}
    for period, window in periods.items():
        expected = _mapping(source_periods, period)["edge_set"]
        query = canonical_raw_edge_query(*window)
        result = client.query(query, max_rows=None)
        normalized = normalize_edge_result(result)
        raw_periods[period] = {
            "source_window": list(window),
            "query": query,
            "result": result.model_dump(mode="json"),
            "normalized_stored_edges": normalized,
            "normalized_source_edges": expected,
            "stored_sha256": edge_set_sha256(normalized),
            "source_sha256": edge_set_sha256(expected),
            "exact": normalized == expected,
        }
    raw_periods_exact = all(item["exact"] is True for item in raw_periods.values())
    window = graph_audit_window(spec.normal_window[0], spec.abnormal_window[1])
    observed_start, observed_end = window["graph_observed_window"]
    union_raw_query = canonical_raw_edge_query(observed_start, observed_end)
    union_graph_query = canonical_graph_edge_query(observed_start, observed_end)
    union_raw_result = client.query(union_raw_query, max_rows=None)
    union_graph_result = client.query(union_graph_query, max_rows=None)
    union_raw = normalize_edge_result(union_raw_result)
    union_graph = normalize_edge_result(union_graph_result)
    union_exact = union_raw is not None and union_raw == union_graph

    boundary = spec.normal_window[1]
    boundary_minute = boundary - boundary % 60
    minute_counts = {
        period: int(
            _mapping(source_periods, period)["client_minute_counts"].get(str(boundary_minute), 0)
        )
        for period in periods
    }
    split_supported = boundary == boundary_minute or not all(minute_counts.values())
    graph_periods = None
    graph_periods_exact = None
    graph_period_windows = None
    if split_supported:
        split = boundary
        if boundary != boundary_minute:
            split = boundary_minute + 60 if minute_counts["normal"] else boundary_minute
        graph_period_windows = {
            "normal": [observed_start, split],
            "abnormal": [split, observed_end],
        }
        graph_periods = {}
        for period in periods:
            start, end = graph_period_windows[period]
            query = canonical_graph_edge_query(start, end)
            result = client.query(query, max_rows=None)
            normalized = normalize_edge_result(result)
            expected = raw_periods[period]["normalized_stored_edges"]
            graph_periods[period] = {
                "observed_window": [start, end],
                "query": query,
                "result": result.model_dump(mode="json"),
                "normalized_graph_edges": normalized,
                "normalized_raw_edges": expected,
                "graph_sha256": edge_set_sha256(normalized),
                "raw_sha256": edge_set_sha256(expected),
                "exact": normalized == expected,
            }
        graph_periods_exact = all(item["exact"] is True for item in graph_periods.values())
    strategy = {
        "normal_abnormal_windows_contiguous": spec.normal_window[1] == spec.abnormal_window[0],
        "source_rows_within_half_open_windows": source.get(
            "all_signal_timestamps_in_declared_windows"
        )
        is True,
        "source_end_boundaries_empty": source.get("source_window_end_boundaries_empty") is True,
        "stored_period_raw_edges_match_source": raw_periods_exact,
        "graph_period_split_supported": split_supported,
        "shared_boundary_minute": boundary_minute,
        "shared_boundary_minute_client_counts": minute_counts,
        "comparison_strategy": (
            "separate_periods_and_contiguous_union" if split_supported else "contiguous_union_only"
        ),
        "graph_period_windows": graph_period_windows,
        "period_graph_exact": graph_periods_exact,
        "contiguous_union_exact": union_exact,
    }
    strategy["pass"] = all(
        (
            strategy["normal_abnormal_windows_contiguous"],
            strategy["source_rows_within_half_open_windows"],
            strategy["source_end_boundaries_empty"],
            raw_periods_exact,
            union_exact,
            not split_supported or graph_periods_exact is True,
        )
    )
    return {
        "window_contract": window,
        "period_raw_replay": raw_periods,
        "period_raw_replay_exact": raw_periods_exact,
        "period_graph_replay": graph_periods,
        "period_graph_replay_exact": graph_periods_exact,
        "graph_window_strategy_proof": strategy,
        "raw_edge_query": union_raw_query,
        "raw_edge_result": union_raw_result.model_dump(mode="json"),
        "graph_edge_query": union_graph_query,
        "graph_edge_result": union_graph_result.model_dump(mode="json"),
        "normalized_raw_edges": union_raw,
        "normalized_graph_edges": union_graph,
        "raw_edge_set_sha256": edge_set_sha256(union_raw),
        "graph_edge_set_sha256": edge_set_sha256(union_graph),
        "exact_edge_set_equality": union_exact,
    }


def mechanism_evidence_audit(
    client: GreptimeClient,
    spec: TransferCaseSpec,
) -> dict[str, object]:
    query = canonical_mechanism_evidence_query(spec)
    result = client.query(query, max_rows=None)
    normalized = normalize_mechanism_evidence(result)
    expected = {
        period: {
            "count": int(values["count"]),
            "min": float(values["min"]),
            "max": float(values["max"]),
            "high_count": int(values["high_count"]),
        }
        for period, values in (
            ("normal", spec.mechanism_evidence.normal),
            ("abnormal", spec.mechanism_evidence.abnormal),
        )
    }
    return {
        "predicate": spec.mechanism_evidence.predicate,
        "query": query,
        "result": result.model_dump(mode="json"),
        "normalized_result": normalized,
        "expected_result": expected,
        "evidence_match": normalized == expected,
        "pass": normalized == expected,
    }


def canonical_mechanism_evidence_query(spec: TransferCaseSpec) -> str:
    evidence = spec.mechanism_evidence
    normal_start = _time_literal(spec.normal_window[0])
    normal_end = _time_literal(spec.normal_window[1])
    abnormal_end = _time_literal(spec.abnormal_window[1])
    threshold = repr(float(evidence.threshold))
    if spec.mechanism_code is not MechanismCode.CALL_PATH_DELAY:
        if evidence.identity_column is None or evidence.identity_value is None:
            raise OpenRCA2Error("metric mechanism identity is incomplete")
        return f"""SELECT CASE
         WHEN greptime_timestamp < {normal_end} THEN 'normal'
         ELSE 'abnormal'
       END AS period,
       COUNT(*) AS sample_count,
       MIN({evidence.value_column}) AS min_value,
       MAX({evidence.value_column}) AS max_value,
       SUM(CASE WHEN {evidence.value_column} >= {threshold} THEN 1 ELSE 0 END) AS high_count
FROM {evidence.source_table}
WHERE {evidence.identity_column} = {_literal(evidence.identity_value)}
  AND greptime_timestamp >= {normal_start}
  AND greptime_timestamp < {abnormal_end}
GROUP BY period
ORDER BY period"""
    operations = ", ".join(_literal(value) for value in evidence.allowed_operations)
    return f"""SELECT CASE
         WHEN c.timestamp < {normal_end} THEN 'normal'
         ELSE 'abnormal'
       END AS period,
       COUNT(*) AS sample_count,
       MIN(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) AS min_value,
       MAX(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) AS max_value,
       SUM(CASE
             WHEN CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT) >= {threshold}
             THEN 1 ELSE 0
           END) AS high_count
FROM traces c
JOIN traces s
  ON c.trace_id = s.trace_id
 AND s.parent_span_id = c.span_id
WHERE c.span_kind = 'SPAN_KIND_CLIENT'
  AND s.span_kind = 'SPAN_KIND_SERVER'
  AND c.service_name = {_literal(str(spec.edge_source))}
  AND s.service_name = {_literal(str(spec.edge_destination))}
  AND c.span_name IN ({operations})
  AND c.timestamp >= {normal_start}
  AND c.timestamp < {abnormal_end}
GROUP BY period
ORDER BY period"""


def normalize_mechanism_evidence(
    result: QueryResult,
) -> dict[str, dict[str, int | float]] | None:
    required = ("period", "sample_count", "min_value", "max_value", "high_count")
    columns = [column.lower() for column in result.columns]
    if result.truncated or any(columns.count(column) != 1 for column in required):
        return None
    indexes = {column: columns.index(column) for column in required}
    normalized: dict[str, dict[str, int | float]] = {}
    for row in result.rows:
        if len(row) <= max(indexes.values()):
            return None
        period = row[indexes["period"]]
        count = row[indexes["sample_count"]]
        minimum = row[indexes["min_value"]]
        maximum = row[indexes["max_value"]]
        high = row[indexes["high_count"]]
        count_value = _strict_int(count)
        high_value = _strict_int(high)
        if (
            period not in {"normal", "abnormal"}
            or period in normalized
            or count_value is None
            or count_value <= 0
            or _strict_number(minimum) is None
            or _strict_number(maximum) is None
            or high_value is None
            or not 0 <= high_value <= count_value
        ):
            return None
        normalized[str(period)] = {
            "count": count_value,
            "min": float(minimum),
            "max": float(maximum),
            "high_count": high_value,
        }
    return normalized if set(normalized) == {"normal", "abnormal"} else None


def no_model_gates(
    case: OpenRCA2Case,
    spec: TransferCaseSpec,
    source: Mapping[str, object],
    stored: Mapping[str, object],
    equality: Mapping[str, object],
    mechanism: Mapping[str, object],
    *,
    isolated: bool,
    semantic_surface_contract: bool,
) -> dict[str, bool]:
    identity = _mapping(stored, "source_identity")
    input_json = case.input.model_dump_json()
    gates = {
        "pinned_source_revision": case.dataset == DATASET_REVISION,
        "frozen_selection_match": audit_source_case(case, spec.opaque_case_id, spec.case_role)
        == spec,
        "source_files_match": _source_file_hashes(case) == spec.source_files_sha256,
        "source_identity_valid": source.get("source_identity_valid") is True,
        "source_windows_exact": source.get("all_signal_timestamps_in_declared_windows") is True,
        "source_end_boundaries_empty": source.get("source_window_end_boundaries_empty") is True,
        "reference_causal_graph_excluded": source.get("reference_causal_graph_read") is False
        and source.get("reference_causal_graph_ingested") is False,
        "exclusive_graph_source": isolated,
        "current_semantic_surface_contract": semantic_surface_contract,
        "protocol_rejections_zero": stored.get("protocol_rejections_zero") is True,
        "stored_row_counts_match": stored.get("protocol_row_counts_match") is True,
        "id_remapping_zero": _mapping(stored, "id_remapping").get("pass") is True,
        "stored_source_identity_match": all(
            identity.get(key) is True
            for key in (
                "service_counts_match",
                "span_kind_counts_match",
                "status_code_counts_match",
            )
        ),
        "stored_period_raw_edges_match_source": equality.get("period_raw_replay_exact") is True,
        "raw_graph_exact_edge_set_equality": equality.get("exact_edge_set_equality") is True,
        "graph_window_strategy_proven": _mapping(equality, "graph_window_strategy_proof").get(
            "pass"
        )
        is True,
        "mechanism_evidence": mechanism.get("pass") is True,
        "opaque_agent_case_id": (
            case.input.case_token == spec.opaque_case_id
            and spec.source_case not in input_json
            and spec.source_fault_type not in input_json
            and not case.input.fault_taxonomy
        ),
    }
    gates["all_passed"] = all(gates.values())
    return gates


def _manifest_systems(path: Path) -> dict[str, str]:
    systems: dict[str, str] = {}
    for line in path.read_text().splitlines():
        item = json.loads(line)
        if isinstance(item, Mapping) and item.get("name") and item.get("system"):
            systems[str(item["name"])] = str(item["system"])
    return systems


def _system_key(system: str) -> str:
    return {
        "Hotel Reservation": "hs",
        "OpenTelemetry Demo": "otel-demo",
    }.get(system, system)


def _metric_evidence(
    case: OpenRCA2Case,
    mechanism: MechanismCode,
    config: Mapping[str, object],
    truth: Mapping[str, object],
) -> SourceMechanismEvidence:
    containers = _string_list(truth, "container")
    if len(containers) != 1:
        raise OpenRCA2Error("metric mechanism must declare one container identity")
    identity = containers[0]
    definitions = {
        MechanismCode.WORKLOAD_RESTART: (
            "workload_restart_transition",
            "k8s.container.restarts",
            1.0,
        ),
        MechanismCode.CPU_SATURATION: (
            "container_cpu_saturation",
            "container.cpu.usage",
            0.5,
        ),
        MechanismCode.MEMORY_PRESSURE: (
            "container_memory_pressure",
            "container.memory.working_set",
            float(512 * 1024 * 1024),
        ),
    }
    predicate, metric, threshold = definitions[mechanism]
    periods = [
        _metric_period(case.gauge_paths[index], metric, identity, threshold) for index in range(2)
    ]
    normal, abnormal = periods
    if normal["count"] < 1 or abnormal["count"] < 2:
        raise OpenRCA2Error("mechanism metric does not cover both source windows")
    if normal["high_count"] != 0 or abnormal["high_count"] < 2:
        raise OpenRCA2Error("source metric does not prove the frozen transition")
    return SourceMechanismEvidence(
        predicate=predicate,
        source_table=metric.replace(".", "_"),
        value_column="greptime_value",
        identity_column="k8s_container_name",
        identity_value=identity,
        threshold=threshold,
        minimum_anomalous_observations=2,
        scope_preserving_predicates=_metric_scope_preserving_predicates(case, metric, identity),
        identity_equivalent_predicates=_metric_identity_equivalent_predicates(
            case, metric, identity
        ),
        normal=normal,
        abnormal=abnormal,
    )


def _metric_scope_preserving_predicates(
    case: OpenRCA2Case,
    metric: str,
    identity: str,
) -> tuple[ScopePreservingPredicate, ...]:
    namespaces = {
        str(row["attr.k8s.namespace.name"])
        for row in _iter_rows(case.gauge_paths)
        if row.get("metric") == metric
        and row.get("attr.k8s.container.name") == identity
        and row.get("attr.k8s.namespace.name") not in (None, "")
    }
    if len(namespaces) != 1:
        raise OpenRCA2Error(
            "mechanism container identity is not bound to exactly one source namespace"
        )
    return (
        ScopePreservingPredicate(
            column="k8s_namespace_name",
            value=next(iter(namespaces)),
        ),
    )


def _metric_identity_equivalent_predicates(
    case: OpenRCA2Case,
    metric: str,
    identity: str,
) -> tuple[ScopePreservingPredicate, ...]:
    rows = [row for row in _iter_rows(case.gauge_paths) if row.get("metric") == metric]
    identity_rows = [row for row in rows if row.get("attr.k8s.container.name") == identity]
    pods = {
        str(row["attr.k8s.pod.name"])
        for row in identity_rows
        if row.get("attr.k8s.pod.name") not in (None, "")
    }
    if len(pods) != 1:
        return ()
    pod = next(iter(pods))
    pod_containers = {
        str(row["attr.k8s.container.name"])
        for row in rows
        if row.get("attr.k8s.pod.name") == pod
        and row.get("attr.k8s.container.name") not in (None, "")
    }
    if pod_containers != {identity} or len(identity_rows) != sum(
        row.get("attr.k8s.pod.name") == pod for row in identity_rows
    ):
        return ()
    return (ScopePreservingPredicate(column="k8s_pod_name", value=pod),)


def _metric_period(
    path: Path,
    metric: str,
    identity: str,
    threshold: float,
) -> dict[str, int | float]:
    values = [
        float(row["value"])
        for row in _iter_rows((path,))
        if row.get("metric") == metric and row.get("attr.k8s.container.name") == identity
    ]
    if not values:
        return {"count": 0, "min": 0.0, "max": 0.0, "high_count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "high_count": sum(value >= threshold for value in values),
    }


def _delay_evidence(
    case: OpenRCA2Case,
    edge_source: str,
    edge_destination: str,
    truth: Mapping[str, object],
) -> SourceMechanismEvidence:
    declared_operations = tuple(_string_list(truth, "span"))
    periods = [
        _delay_period(
            case.traces_paths[index],
            edge_source,
            edge_destination,
            declared_operations,
        )
        for index in range(2)
    ]
    normal, abnormal, operations = periods[0][0], periods[1][0], periods[0][1] | periods[1][1]
    if normal["count"] < 1 or abnormal["count"] < 2:
        raise OpenRCA2Error("delay mechanism does not cover both source windows")
    if normal["high_count"] != 0 or abnormal["high_count"] < 2:
        raise OpenRCA2Error("source spans do not prove the frozen start-gap transition")
    return SourceMechanismEvidence(
        predicate="call_path_start_gap_transition",
        source_table="traces",
        value_column="server_start_minus_client_start_ns",
        threshold=500_000_000.0,
        minimum_anomalous_observations=2,
        allowed_operations=tuple(sorted(operations)),
        normal=normal,
        abnormal=abnormal,
    )


def _delay_period(
    path: Path,
    edge_source: str,
    edge_destination: str,
    declared_operations: tuple[str, ...],
) -> tuple[dict[str, int | float], set[str]]:
    rows = list(_iter_rows((path,)))
    servers: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for row in rows:
        if row.get("attr.span_kind") != "Server":
            continue
        key = (str(row.get("trace_id")), str(row.get("parent_span_id")))
        servers.setdefault(key, []).append(row)
    gaps: list[int] = []
    operations: set[str] = set()
    for client in rows:
        if (
            client.get("attr.span_kind") != "Client"
            or client.get("service_name") != edge_source
            or (declared_operations and client.get("span_name") not in declared_operations)
        ):
            continue
        matches = [
            row
            for row in servers.get((str(client.get("trace_id")), str(client.get("span_id"))), [])
            if row.get("service_name") == edge_destination
        ]
        if len(matches) != 1:
            continue
        server = matches[0]
        gaps.append(int(server["time"]) - int(client["time"]))
        operations.update((str(client["span_name"]), str(server["span_name"])))
    if not gaps:
        return {"count": 0, "min": 0, "max": 0, "high_count": 0}, operations
    threshold = 500_000_000
    return {
        "count": len(gaps),
        "min": min(gaps),
        "max": max(gaps),
        "high_count": sum(value >= threshold for value in gaps),
    }, operations


def _validate_trace_identity(case: OpenRCA2Case) -> None:
    identities: set[tuple[str, str]] = set()
    for row in _iter_rows(case.traces_paths):
        trace_id = str(row.get("trace_id") or "")
        span_id = str(row.get("span_id") or "")
        if not _valid_hex_id(trace_id, 16) or not _valid_hex_id(span_id, 8):
            raise OpenRCA2Error("source trace identity requires OTLP remapping")
        identity = (trace_id, span_id)
        if identity in identities:
            raise OpenRCA2Error("source trace identity is not unique across windows")
        identities.add(identity)
        parent = str(row.get("parent_span_id") or "")
        if parent and not _valid_hex_id(parent, 8):
            raise OpenRCA2Error("source parent span identity requires OTLP remapping")


def _valid_hex_id(value: str, size: int) -> bool:
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return False
    return len(decoded) == size and any(decoded)


def _source_file_hashes(case: OpenRCA2Case) -> dict[str, str]:
    paths = (
        *case.gauge_paths,
        *case.sum_paths,
        *case.histogram_paths,
        *case.logs_paths,
        *case.traces_paths,
        case.injection_path,
        case.root / "env.json",
        case.root / "conclusion.parquet",
    )
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _single_mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    items = value.get(key)
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
        raise OpenRCA2Error(f"expected one {key} entry")
    return items[0]


def _string_list(value: Mapping[str, object], key: str) -> list[str]:
    items = value.get(key)
    if not isinstance(items, list):
        raise OpenRCA2Error(f"source ground truth has no {key} list")
    return [str(item) for item in items if item not in (None, "")]


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise OpenRCA2Error(f"expected JSON object: {path.name}")
    return value


def _source_trace_identity(rows: list[Mapping[str, object]]) -> dict[str, object]:
    identities = [(str(row.get("trace_id")), str(row.get("span_id"))) for row in rows]
    services = Counter(str(row.get("service_name")) for row in rows)
    kinds = Counter(f"SPAN_KIND_{str(row.get('attr.span_kind')).upper()}" for row in rows)
    statuses = Counter(f"STATUS_CODE_{str(row.get('attr.status_code')).upper()}" for row in rows)
    return {
        "rows": len(rows),
        "span_identity_unique": len(identities) == len(set(identities)),
        "service_counts": dict(sorted(services.items())),
        "span_kind_counts": dict(sorted(kinds.items())),
        "status_code_counts": dict(sorted(statuses.items())),
        "valid": len(identities) == len(set(identities))
        and all(
            _valid_hex_id(str(row.get("trace_id") or ""), 16)
            and _valid_hex_id(str(row.get("span_id") or ""), 8)
            and (not row.get("parent_span_id") or _valid_hex_id(str(row.get("parent_span_id")), 8))
            and row.get("service_name") not in (None, "")
            and row.get("attr.span_kind")
            in {"Unspecified", "Internal", "Server", "Client", "Producer", "Consumer"}
            and row.get("attr.status_code") in {"Unset", "Ok", "Error"}
            for row in rows
        ),
    }


def _source_edge_period(
    rows: list[Mapping[str, object]], window: tuple[int, int]
) -> dict[str, object]:
    start_ns, end_ns = window[0] * 1_000_000_000, window[1] * 1_000_000_000
    servers: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if row.get("attr.span_kind") == "Server" and row.get("parent_span_id"):
            servers[(str(row.get("trace_id")), str(row.get("parent_span_id")))].append(row)
    edges: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    client_minutes: Counter[int] = Counter()
    witness_count = 0
    for client in rows:
        if client.get("attr.span_kind") != "Client":
            continue
        client_time = _strict_int(client.get("time"))
        if client_time is None:
            raise OpenRCA2Error("client span has an invalid source timestamp")
        if not start_ns <= client_time < end_ns:
            continue
        client_minutes[(client_time // 1_000_000_000) // 60 * 60] += 1
        matches = servers.get((str(client.get("trace_id")), str(client.get("span_id"))), [])
        for server in matches:
            if client.get("service_name") == server.get("service_name"):
                continue
            pair = (str(client.get("service_name")), str(server.get("service_name")))
            edges[pair][0] += 1
            edges[pair][1] += server.get("attr.status_code") == "Error"
            witness_count += 1
    edge_set = [
        {
            "src_type": "service",
            "src_id": source,
            "dst_type": "service",
            "dst_id": destination,
            "rel_type": "calls",
            "provenance": "trace",
            "request_count": counts[0],
            "error_count": counts[1],
        }
        for (source, destination), counts in sorted(edges.items())
    ]
    return {
        "source_window": list(window),
        "client_minute_counts": {
            str(minute): count for minute, count in sorted(client_minutes.items())
        },
        "witness_count": witness_count,
        "edge_set": edge_set,
        "edge_set_sha256": edge_set_sha256(edge_set),
    }


def _source_signal_period(
    paths: tuple[Path, ...], window: tuple[int, int]
) -> dict[str, int | bool]:
    start_ns, end_ns = window[0] * 1_000_000_000, window[1] * 1_000_000_000
    rows = before = at_end = after_end = nulls = 0
    for path in paths:
        for row in _iter_rows((path,)):
            rows += 1
            value = row.get("time")
            if not isinstance(value, int):
                nulls += 1
                continue
            before += value < start_ns
            at_end += value == end_ns
            after_end += value > end_ns
    return {
        "source_rows": rows,
        "rows_before_start": before,
        "rows_at_exact_end": at_end,
        "rows_after_end": after_end,
        "null_timestamps": nulls,
        "all_rows_in_declared_window": not any((before, at_end, after_end, nulls)),
    }


def _stored_group_counts(client: GreptimeClient, column: str) -> dict[str, int]:
    result = client.query(
        f"SELECT {column}, COUNT(*) AS row_count FROM traces GROUP BY {column} ORDER BY {column}",
        max_rows=None,
    )
    if result.truncated or [name.lower() for name in result.columns] != [column, "row_count"]:
        raise OpenRCA2Error(f"stored trace {column} audit is malformed")
    counts: dict[str, int] = {}
    for row in result.rows:
        if len(row) != 2 or not isinstance(row[1], int) or isinstance(row[1], bool):
            raise OpenRCA2Error(f"stored trace {column} count is malformed")
        counts[str(row[0])] = row[1]
    return counts


def _parquet_rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise OpenRCA2Error(f"transfer audit field is not an object: {key}")
    return item


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strict_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _time_literal(epoch: int) -> str:
    from datetime import UTC, datetime

    value = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return f"TIMESTAMP '{value}'"
