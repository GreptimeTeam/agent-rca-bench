import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from semantic_rca_bench.contracts import QueryResult
from semantic_rca_bench.datasets.aegis import AegisAuditError
from semantic_rca_bench.datasets.aegis_transfer import (
    RELEASE_AGENT_CASE_ID,
    RELEASE_SOURCE_CASE,
    _iter_traces,
    archive_checksum_status,
    canonical_mechanism_evidence_query,
    canonical_raw_edge_query,
    edge_results_equal,
    exact_edge_equality_audit,
    graph_audit_window,
    load_selected_case,
    no_model_gates,
    normalize_edge_result,
    normalize_workload_restart_evidence,
)


def _write_table(path: Path, values: dict[str, list[object]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pydict(values, schema=schema), path)


def _selected_case_fixture(
    tmp_path: Path,
    selection_path: Path = Path("fixtures/reference/aegis-transfer-v31-selection.json"),
    *,
    injection_start: str = "2025-07-19T14:03:52Z",
):
    cases_dir = tmp_path / "cases"
    meta_dir = tmp_path / "meta"
    root = cases_dir / RELEASE_SOURCE_CASE
    root.mkdir(parents=True)
    meta_dir.mkdir()
    (root / ".finished").touch()
    (root / "env.json").write_text(
        json.dumps(
            {
                "NORMAL_START": "1752933592",
                "NORMAL_END": "1752933832",
                "ABNORMAL_START": "1752933832",
                "ABNORMAL_END": "1752934072",
            }
        )
    )
    (root / "injection.json").write_text(
        json.dumps(
            {
                "injection_name": RELEASE_SOURCE_CASE,
                "status": 2,
                "start_time": injection_start,
                "display_config": json.dumps(
                    {
                        "injection_point": {
                            "app_name": "ts-auth-service",
                        },
                        "duration": 4,
                        "namespace": "ts",
                    }
                ),
                "ground_truth": {
                    "container": ["ts-auth-service"],
                    "pod": ["ts-auth-service-6966cbcd89-qvlgp"],
                    "service": ["ts-auth-service"],
                },
            }
        )
    )
    (root / "causal_graph.json").write_text("not valid JSON and must not be read")
    for period in ("normal", "abnormal"):
        for suffix in (
            "metrics.parquet",
            "metrics_sum.parquet",
            "metrics_histogram.parquet",
            "logs.parquet",
            "traces.parquet",
        ):
            (root / f"{period}_{suffix}").touch()

    _write_table(
        meta_dir / "index.parquet",
        {"dataset": ["rcabench"], "datapack": [RELEASE_SOURCE_CASE]},
        pa.schema([("dataset", pa.large_string()), ("datapack", pa.large_string())]),
    )
    _write_table(
        meta_dir / "attributes.parquet",
        {
            "datapack": [RELEASE_SOURCE_CASE],
            "injection.fault_type": ["PodFailure"],
            "ground_truth.service_count": [1],
        },
        pa.schema(
            [
                ("datapack", pa.large_string()),
                ("injection.fault_type", pa.large_string()),
                ("ground_truth.service_count", pa.int64()),
            ]
        ),
    )
    _write_table(
        meta_dir / "labels.parquet",
        {
            "datapack": [RELEASE_SOURCE_CASE],
            "gt.level": ["service"],
            "gt.name": ["ts-auth-service"],
        },
        pa.schema(
            [
                ("datapack", pa.large_string()),
                ("gt.level", pa.large_string()),
                ("gt.name", pa.large_string()),
            ]
        ),
    )
    return load_selected_case(
        cases_dir,
        meta_dir,
        selection_path,
        database="case_04",
    )


def _edge_result(*rows: list[object]) -> QueryResult:
    return QueryResult(
        query_id="q",
        columns=[
            "src_type",
            "src_id",
            "dst_type",
            "dst_id",
            "rel_type",
            "provenance",
            "request_count",
            "error_count",
        ],
        rows=list(rows),
        elapsed_seconds=0,
    )


def test_release_loader_keeps_component_truth_opaque_and_never_reads_causal_graph(
    tmp_path: Path,
) -> None:
    case = _selected_case_fixture(tmp_path)

    assert case.input.case_token == RELEASE_AGENT_CASE_ID
    assert case.source_case not in case.input.model_dump_json()
    assert case.ground_truth.fault_type not in case.input.model_dump_json()
    assert case.input.fault_taxonomy == []
    assert case.ground_truth.services == ("ts-auth-service",)
    assert case.ground_truth.declared_edge is None
    query = canonical_mechanism_evidence_query(case)
    assert "FROM k8s_container_restarts" in query
    assert "k8s_container_name = 'ts-auth-service'" in query
    assert "MIN(greptime_value) AS min_restarts" in query
    assert "MAX(greptime_value) AS max_restarts" in query


def test_workload_restart_evidence_requires_both_nonempty_periods() -> None:
    result = QueryResult(
        query_id="q",
        columns=["period", "sample_count", "min_restarts", "max_restarts"],
        rows=[["normal", 24, 0.0, 0.0], ["abnormal", 24, 1.0, 1.0]],
        elapsed_seconds=0,
    )

    assert normalize_workload_restart_evidence(result) == {
        "normal": {"count": 24, "min_restarts": 0.0, "max_restarts": 0.0},
        "abnormal": {"count": 24, "min_restarts": 1.0, "max_restarts": 1.0},
    }
    assert (
        normalize_workload_restart_evidence(
            result.model_copy(update={"rows": [["abnormal", 24, 1.0, 1.0]]})
        )
        is None
    )


def test_selected_loader_rejects_unfrozen_opaque_case_mapping(tmp_path: Path) -> None:
    selection = json.loads(Path("fixtures/reference/aegis-transfer-v31-selection.json").read_text())
    selection["agent_case_id"] = "aegis-transfer-003"
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selection))

    with pytest.raises(AegisAuditError, match="invalid agent case ID"):
        _selected_case_fixture(tmp_path, selection_path)


def test_selected_loader_rejects_naive_injection_timestamp(tmp_path: Path) -> None:
    with pytest.raises(AegisAuditError, match="explicit timezone"):
        _selected_case_fixture(tmp_path, injection_start="2025-07-19T14:03:52")


def test_archive_status_preserves_zero_byte_observation(tmp_path: Path) -> None:
    archive = tmp_path / "segments"
    archive.mkdir()
    for index in range(32):
        (archive / f"{index:02d}").touch()

    status = archive_checksum_status(archive)

    assert status["observed_size"] == 0
    assert status["observed_md5"] == "d41d8cd98f00b204e9800998ecf8427e"


def test_trace_replay_keeps_http_500_separate_from_source_span_status(tmp_path: Path) -> None:
    schema = pa.schema(
        [
            ("time", pa.timestamp("ns", tz="UTC")),
            ("trace_id", pa.large_string()),
            ("span_id", pa.large_string()),
            ("parent_span_id", pa.large_string()),
            ("span_name", pa.large_string()),
            ("attr.span_kind", pa.large_string()),
            ("service_name", pa.large_string()),
            ("duration", pa.uint64()),
            ("attr.status_code", pa.large_string()),
            ("attr.http.response.status_code", pa.uint16()),
        ]
    )
    normal = tmp_path / "normal.parquet"
    abnormal = tmp_path / "abnormal.parquet"
    _write_table(
        normal,
        {
            "time": [1_700_000_000_000_000_000],
            "trace_id": ["01" * 16],
            "span_id": ["02" * 8],
            "parent_span_id": [""],
            "span_name": ["GET /fault"],
            "attr.span_kind": ["Server"],
            "service_name": ["service-a"],
            "duration": [1],
            "attr.status_code": ["Unset"],
            "attr.http.response.status_code": [500],
        },
        schema,
    )
    _write_table(
        abnormal,
        {field.name: [] for field in schema},
        schema,
    )

    span = next(_iter_traces((normal, abnormal)))

    assert span.status_code == 0
    assert span.attributes["http.response.status_code"] == 500


def test_raw_edge_query_uses_native_roles_parent_relation_and_source_status() -> None:
    query = canonical_raw_edge_query(1_700_000_000, 1_700_000_600)

    assert "c.span_kind = 'SPAN_KIND_CLIENT'" in query
    assert "s.span_kind = 'SPAN_KIND_SERVER'" in query
    assert "s.parent_span_id = c.span_id" in query
    assert "s.span_status_code = 'STATUS_CODE_ERROR'" in query
    assert "http.response.status" not in query


def test_mechanism_query_uses_exact_source_metric_identity(tmp_path: Path) -> None:
    query = canonical_mechanism_evidence_query(_selected_case_fixture(tmp_path))

    assert "FROM k8s_container_restarts" in query
    assert "k8s_container_name = 'ts-auth-service'" in query
    assert "MIN(greptime_value) AS min_restarts" in query
    assert "MAX(greptime_value) AS max_restarts" in query


def test_wrong_role_or_parent_relation_cannot_match_graph_edge_set() -> None:
    graph = _edge_result(["service", "security", "service", "order", "calls", "trace", 1, 0])
    raw_after_wrong_role_or_parent = _edge_result()

    assert not edge_results_equal(raw_after_wrong_role_or_parent, graph)


@pytest.mark.parametrize(
    "changed",
    [
        [],
        [["service", "security", "service", "order", "calls", "trace", 2, 0]],
        [
            ["service", "security", "service", "order", "calls", "trace", 1, 0],
            ["service", "security", "service", "route", "calls", "trace", 1, 0],
        ],
    ],
)
def test_graph_missing_count_changed_or_extra_edge_fails_exact_equality(
    changed: list[list[object]],
) -> None:
    expected = _edge_result(["service", "security", "service", "order", "calls", "trace", 1, 0])

    assert not edge_results_equal(expected, _edge_result(*changed))


def test_non_minute_source_window_uses_minimal_complete_envelope() -> None:
    window = graph_audit_window(1_752_918_758, 1_752_919_238)

    assert window["source_window"] == [1_752_918_758, 1_752_919_238]
    assert window["graph_observed_window"] == [1_752_918_720, 1_752_919_260]
    assert window["client_scan_window"] == [1_752_918_720, 1_752_919_260]
    assert window["server_scan_window"] == [1_752_918_420, 1_752_922_860]


def test_non_minute_period_replay_proves_why_graph_equality_uses_union(
    tmp_path: Path,
) -> None:
    case = _selected_case_fixture(tmp_path)
    normal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 3, 0])
    abnormal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 2, 1])
    combined = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 5, 1])

    class Client:
        def __init__(self) -> None:
            self.results = [normal, abnormal, combined, combined]

        def query(self, query: str, *, max_rows: int | None = None) -> QueryResult:
            assert max_rows is None
            return self.results.pop(0)

    source = {
        "trace_windows_exact": True,
        "trace_windows": {
            "normal": {
                "edge_set": normalize_edge_result(normal),
                "client_observed_minute_counts": {"1752933780": 7},
            },
            "abnormal": {
                "edge_set": normalize_edge_result(abnormal),
                "client_observed_minute_counts": {"1752933780": 2},
            },
        },
    }

    audit = exact_edge_equality_audit(Client(), case, source)  # type: ignore[arg-type]

    assert audit["period_raw_replay_exact"] is True
    assert audit["period_graph_replay"] is None
    assert audit["period_graph_replay_exact"] is None
    assert audit["graph_window_strategy_proof"] == {
        "normal_abnormal_windows_contiguous": True,
        "source_trace_windows_exact": True,
        "stored_period_raw_edges_match_source": True,
        "graph_period_split_supported": False,
        "shared_boundary_minute": 1752933780,
        "shared_boundary_minute_client_counts": {"normal": 7, "abnormal": 2},
        "comparison_strategy": "contiguous_union_only",
        "graph_period_windows": None,
        "period_graph_replay_exact": None,
        "contiguous_union_raw_graph_exact": True,
        "graph_window_valid": True,
        "unified_graph_window_required": True,
        "reason": (
            "observed_at is minute-binned and the non-minute normal/abnormal boundary has "
            "client spans from both periods; only the contiguous union is representable exactly"
        ),
        "pass": True,
    }
    assert audit["exact_edge_set_equality"] is True


def test_minute_aligned_period_boundary_does_not_invalidate_union_equality(
    tmp_path: Path,
) -> None:
    case = _selected_case_fixture(tmp_path).model_copy(
        update={
            "normal_window": (1752841320, 1752841560),
            "abnormal_window": (1752841560, 1752841800),
        }
    )
    normal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 3, 0])
    abnormal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 2, 1])
    combined = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 5, 1])

    class Client:
        def __init__(self) -> None:
            self.results = [normal, abnormal, normal, abnormal, combined, combined]

        def query(self, query: str, *, max_rows: int | None = None) -> QueryResult:
            assert max_rows is None
            return self.results.pop(0)

    source = {
        "trace_windows_exact": True,
        "trace_windows": {
            "normal": {
                "edge_set": normalize_edge_result(normal),
                "client_observed_minute_counts": {"1752841560": 0},
            },
            "abnormal": {
                "edge_set": normalize_edge_result(abnormal),
                "client_observed_minute_counts": {"1752841560": 2},
            },
        },
    }

    audit = exact_edge_equality_audit(Client(), case, source)  # type: ignore[arg-type]

    proof = audit["graph_window_strategy_proof"]
    assert proof["graph_period_split_supported"] is True
    assert proof["unified_graph_window_required"] is False
    assert proof["graph_window_valid"] is True
    assert proof["period_graph_replay_exact"] is True
    assert proof["comparison_strategy"] == "separate_periods_and_contiguous_union"
    assert proof["graph_period_windows"] == {
        "normal": [1752841320, 1752841560],
        "abnormal": [1752841560, 1752841800],
    }
    assert proof["pass"] is True
    assert audit["period_graph_replay_exact"] is True
    assert audit["exact_edge_set_equality"] is True


def test_non_minute_boundary_splits_at_next_minute_when_only_normal_uses_shared_bin(
    tmp_path: Path,
) -> None:
    case = _selected_case_fixture(tmp_path)
    normal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 3, 0])
    abnormal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 2, 1])
    combined = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 5, 1])

    class Client:
        def __init__(self) -> None:
            self.results = [normal, abnormal, normal, abnormal, combined, combined]

        def query(self, query: str, *, max_rows: int | None = None) -> QueryResult:
            assert max_rows is None
            return self.results.pop(0)

    source = {
        "trace_windows_exact": True,
        "trace_windows": {
            "normal": {
                "edge_set": normalize_edge_result(normal),
                "client_observed_minute_counts": {"1752933780": 7},
            },
            "abnormal": {
                "edge_set": normalize_edge_result(abnormal),
                "client_observed_minute_counts": {"1752933780": 0},
            },
        },
    }

    audit = exact_edge_equality_audit(Client(), case, source)  # type: ignore[arg-type]

    proof = audit["graph_window_strategy_proof"]
    assert proof["graph_period_windows"] == {
        "normal": [1752933540, 1752933840],
        "abnormal": [1752933840, 1752934080],
    }
    assert proof["period_graph_replay_exact"] is True
    assert proof["pass"] is True


def test_split_period_graph_count_mismatch_fails_window_strategy(tmp_path: Path) -> None:
    case = _selected_case_fixture(tmp_path).model_copy(
        update={
            "normal_window": (1752841320, 1752841560),
            "abnormal_window": (1752841560, 1752841800),
        }
    )
    normal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 3, 0])
    abnormal = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 2, 1])
    wrong_abnormal = _edge_result(
        ["service", "caller", "service", "callee", "calls", "trace", 1, 1]
    )
    combined = _edge_result(["service", "caller", "service", "callee", "calls", "trace", 5, 1])

    class Client:
        def __init__(self) -> None:
            self.results = [normal, abnormal, normal, wrong_abnormal, combined, combined]

        def query(self, query: str, *, max_rows: int | None = None) -> QueryResult:
            assert max_rows is None
            return self.results.pop(0)

    source = {
        "trace_windows_exact": True,
        "trace_windows": {
            "normal": {
                "edge_set": normalize_edge_result(normal),
                "client_observed_minute_counts": {"1752841560": 0},
            },
            "abnormal": {
                "edge_set": normalize_edge_result(abnormal),
                "client_observed_minute_counts": {"1752841560": 2},
            },
        },
    }

    audit = exact_edge_equality_audit(Client(), case, source)  # type: ignore[arg-type]

    assert audit["exact_edge_set_equality"] is True
    assert audit["period_graph_replay_exact"] is False
    assert audit["graph_window_strategy_proof"]["pass"] is False


def test_id_remap_and_source_identity_mismatch_fail_no_model_gate(tmp_path: Path) -> None:
    case = _selected_case_fixture(tmp_path)
    archive = {"verified": True}
    source = {
        "frozen_edge_sets_match": True,
        "trace_windows_exact": True,
        "source_window_boundaries_accounted": True,
        "source_identity_valid": True,
        "reference_causal_graph_read": False,
        "reference_causal_graph_ingested": False,
    }
    stored = {
        "protocol_rejections_zero": True,
        "stored_row_counts_match": True,
        "id_remapping": {"pass": False},
        "source_identity": {
            "service_counts_match": False,
            "log_service_counts_match": True,
            "span_kind_counts_match": True,
            "status_code_counts_match": True,
            "case_normalization_unambiguous": {
                "service_name": True,
                "span_kind": True,
                "span_status_code": True,
            },
        },
    }

    gates = no_model_gates(
        case,
        archive,
        source,
        stored,
        {"exact_edge_set_equality": True},
        {"pass": True},
        isolated=True,
        frozen_selection=True,
        semantic_surface_contract=True,
    )

    assert not gates["id_remapping_zero"]
    assert not gates["stored_source_identity_match"]
    assert not gates["all_passed"]


def test_case_normalization_collision_fails_no_model_gate(tmp_path: Path) -> None:
    case = _selected_case_fixture(tmp_path)
    source = {
        "frozen_edge_sets_match": True,
        "trace_windows_exact": True,
        "source_window_boundaries_accounted": True,
        "source_identity_valid": True,
        "reference_causal_graph_read": False,
        "reference_causal_graph_ingested": False,
    }
    stored = {
        "protocol_rejections_zero": True,
        "stored_row_counts_match": True,
        "id_remapping": {"pass": True},
        "source_identity": {
            "service_counts_match": True,
            "log_service_counts_match": True,
            "span_kind_counts_match": True,
            "status_code_counts_match": True,
            "case_normalization_unambiguous": {
                "service_name": False,
                "span_kind": True,
                "span_status_code": True,
            },
        },
    }

    gates = no_model_gates(
        case,
        {"verified": True},
        source,
        stored,
        {
            "exact_edge_set_equality": True,
            "period_raw_replay_exact": True,
            "graph_window_strategy_proof": {"pass": True},
        },
        {"pass": True},
        isolated=True,
        frozen_selection=True,
        semantic_surface_contract=True,
    )

    assert gates["stored_source_identity_match"] is True
    assert gates["case_normalized_predicates_source_equivalent"] is False
    assert gates["all_passed"] is False
