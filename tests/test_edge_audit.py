import pytest

from semantic_rca_bench.contracts import QueryResult
from semantic_rca_bench.datasets.openrca2 import OpenRCA2Error
from semantic_rca_bench.datasets.openrca2_transfer import _source_edge_period
from semantic_rca_bench.edge_audit import (
    graph_audit_window,
    normalize_edge_result,
)

_COLUMNS = [
    "src_type",
    "src_id",
    "dst_type",
    "dst_id",
    "rel_type",
    "provenance",
    "request_count",
    "error_count",
]


def _result(rows: list[list[object]]) -> QueryResult:
    return QueryResult(
        query_id="q",
        columns=_COLUMNS,
        rows=rows,
        elapsed_seconds=0,
    )


def test_edge_equality_compares_the_complete_normalized_set_and_counts() -> None:
    expected = _result([["service", "a", "service", "b", "calls", "trace", 2, 1]])

    assert normalize_edge_result(expected) == [
        {
            "src_type": "service",
            "src_id": "a",
            "dst_type": "service",
            "dst_id": "b",
            "rel_type": "calls",
            "provenance": "trace",
            "request_count": 2,
            "error_count": 1,
        }
    ]
    assert (
        normalize_edge_result(
            _result(
                [
                    ["service", "a", "service", "b", "calls", "trace", 2, 1],
                    ["service", "a", "service", "b", "calls", "trace", 2, 1],
                ]
            )
        )
        is None
    )


def test_source_edge_requires_client_server_parent_relation_and_server_status() -> None:
    start = 1_700_000_000
    client = {
        "trace_id": "01" * 16,
        "span_id": "02" * 8,
        "parent_span_id": "",
        "service_name": "caller",
        "attr.span_kind": "Client",
        "attr.status_code": "Unset",
        "time": (start + 1) * 1_000_000_000,
    }
    server = {
        "trace_id": client["trace_id"],
        "span_id": "03" * 8,
        "parent_span_id": client["span_id"],
        "service_name": "callee",
        "attr.span_kind": "Server",
        "attr.status_code": "Error",
        "time": (start + 1) * 1_000_000_000 + 10,
    }

    valid = _source_edge_period([client, server], (start, start + 60))
    wrong_parent = _source_edge_period(
        [client, {**server, "parent_span_id": "04" * 8}],
        (start, start + 60),
    )
    wrong_role = _source_edge_period(
        [client, {**server, "attr.span_kind": "Internal"}],
        (start, start + 60),
    )

    assert valid["edge_set"][0]["request_count"] == 1
    assert valid["edge_set"][0]["error_count"] == 1
    assert wrong_parent["edge_set"] == []
    assert wrong_role["edge_set"] == []


def test_source_edge_rejects_invalid_client_timestamp_after_role_filter() -> None:
    non_client = {"attr.span_kind": "Internal", "time": None}
    assert _source_edge_period([non_client], (1_700_000_000, 1_700_000_060))["edge_set"] == []

    with pytest.raises(OpenRCA2Error, match="invalid source timestamp"):
        _source_edge_period(
            [{"attr.span_kind": "Client", "time": None}],
            (1_700_000_000, 1_700_000_060),
        )


def test_graph_window_is_the_minimal_whole_minute_envelope() -> None:
    audit = graph_audit_window(121, 239)

    assert audit["source_window"] == [121, 239]
    assert audit["graph_observed_window"] == [120, 240]
    assert audit["client_scan_window"] == [120, 240]
