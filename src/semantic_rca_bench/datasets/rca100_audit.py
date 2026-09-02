"""Provider-free source and ingestion audits for the RCA100 node cases.

The OpenRCA2 audits read that dataset's parquet columns directly. These work
from the normalized spans the RCA100 adapter already produces, so the two
adapters prove the same properties without sharing a source schema.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping

from opentelemetry.proto.trace.v1.trace_pb2 import Span as ProtoSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as ProtoStatus

from semantic_rca_bench.contracts import IngestCounts, RCA100Case
from semantic_rca_bench.datasets.openrca2_transfer import TransferCaseSpec
from semantic_rca_bench.datasets.rca100 import (
    DATASET_REVISION,
    RCA100Error,
    _iter_traces,
    validate_ingest,
)
from semantic_rca_bench.datasets.rca100_transfer import (
    _source_files_sha256,
    audit_source_case,
)
from semantic_rca_bench.edge_audit import (
    canonical_graph_edge_query,
    canonical_raw_edge_query,
    normalize_edge_result,
    virtual_peer_edge_query,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient

KIND_NAMES = {value: name for name, value in ProtoSpan.SpanKind.items()}
STATUS_NAMES = {value: name for name, value in ProtoStatus.StatusCode.items()}
# The tables the replay writes. The answer key and the vendor topology snapshot
# are labels: the audits read them, but nothing may put them in the database the
# agent queries, so the gate compares the table set instead of asserting intent.
INGESTED_TABLES = frozenset({"logs", "events", "alerts", "traces"})


def source_telemetry_audit(case: RCA100Case, spec: TransferCaseSpec) -> dict[str, object]:
    """Derive the source-side facts the no-model gates compare against."""
    observed = audit_source_case(case, spec.opaque_case_id, spec.case_role)
    if observed != spec:
        raise RCA100Error("live RCA100 source differs from the frozen selection")
    window = (spec.normal_window[0], spec.abnormal_window[1])
    start_ns, end_ns = window[0] * 1_000_000_000, window[1] * 1_000_000_000
    # Only the declared window is kept: the archive holds about an hour around
    # each alert and these cases run to 600k spans.
    windowed = [
        span
        for span in _iter_traces(case.traces_path)
        if start_ns <= span.start_time_unix_nano < end_ns
    ]
    identity = _trace_identity(windowed)
    return {
        "dataset_revision": case.dataset,
        "declared_window": list(window),
        "trace_identity": identity,
        "trace_periods": {
            "normal": _edge_period(windowed, spec.normal_window),
            "abnormal": _edge_period(windowed, spec.abnormal_window),
        },
        "source_identity_valid": identity["valid"],
        "spans_in_declared_window": len(windowed),
    }


def validate_transfer_ingest(
    client: GreptimeClient,
    case: RCA100Case,
    counts: IngestCounts,
    source: Mapping[str, object],
) -> dict[str, object]:
    """Compare what the database holds against what the source offered."""
    base = validate_ingest(client, case, counts)
    identity = source["trace_identity"]
    stored = {
        "service_name": _stored_group_counts(client, "service_name"),
        "span_kind": _stored_group_counts(client, "span_kind"),
        "span_status_code": _stored_group_counts(client, "span_status_code"),
    }
    unexpected = _unexpected_tables(client, case.input.database)
    window = source["declared_window"]
    stored_spans = _stored_span_window(client)
    return {
        **base,
        # Compared against the source restricted to the declared window, not
        # against what the writer reported writing: a filter that dropped one
        # span and admitted another outside the window would otherwise agree
        # with itself on every count.
        "stored_span_window": stored_spans,
        "stored_spans_match_declared_window": (
            stored_spans["count"] == source["spans_in_declared_window"]
            and stored_spans["min_epoch"] >= window[0]
            and stored_spans["max_epoch"] < window[1]
        ),
        # Measured, not declared: a label that reached the database would show up
        # as a table the replay never writes and no metric declared.
        "unexpected_tables": sorted(unexpected),
        "reference_labels_not_ingested": not unexpected,
        "protocol_rejections_zero": counts.rejected_metric_points == 0
        and counts.rejected_trace_spans == 0,
        "id_remapping": {
            "trace_ids": counts.remapped_trace_ids,
            "span_ids": counts.remapped_span_ids,
            "pass": counts.remapped_trace_ids == 0 and counts.remapped_span_ids == 0,
        },
        "source_identity": {
            "service_counts_match": stored["service_name"] == identity["service_counts"],
            "span_kind_counts_match": stored["span_kind"] == identity["span_kind_counts"],
            "status_code_counts_match": stored["span_status_code"]
            == identity["status_code_counts"],
            "stored_service_counts": stored["service_name"],
            "stored_span_kind_counts": stored["span_kind"],
            "stored_status_code_counts": stored["span_status_code"],
        },
    }


def exact_edge_equality_audit(
    client: GreptimeClient,
    spec: TransferCaseSpec,
    source: Mapping[str, object],
) -> dict[str, object]:
    """Prove the graph's service call edges equal what raw SQL derives.

    Only the trace-derived calls edges take part: the attribute-derived runs_on
    and part_of edges the node cases rely on have no client/server span pairing
    to compare against, so claiming equality for them here would overstate it.
    """
    periods = {"normal": spec.normal_window, "abnormal": spec.abnormal_window}
    source_periods = source["trace_periods"]
    raw_replay: dict[str, object] = {}
    replay_exact = True
    for period, window in periods.items():
        expected = source_periods[period]["edge_set"]
        result = client.query(canonical_raw_edge_query(*window), max_rows=None)
        observed = normalize_edge_result(result)
        raw_replay[period] = {"expected": expected, "observed": observed}
        replay_exact = replay_exact and observed == expected
    window = (spec.normal_window[0], spec.abnormal_window[1])
    raw = normalize_edge_result(client.query(canonical_raw_edge_query(*window), max_rows=None))
    graph = normalize_edge_result(
        client.query(canonical_graph_edge_query(*window, paired_only=True), max_rows=None)
    )
    virtual = client.query(virtual_peer_edge_query(*window), max_rows=None)
    return {
        "window_contract": {"graph_observed_window": list(window)},
        "period_raw_replay": raw_replay,
        "period_raw_replay_exact": replay_exact,
        "normalized_raw_edges": raw,
        "normalized_graph_edges": graph,
        "exact_edge_set_equality": raw is not None and raw == graph,
        # Recorded, not asserted: these are edges to peers that emit no spans,
        # so the client/server pairing raw SQL performs cannot produce them.
        "graph_only_virtual_peer_edges": [
            {
                "src_id": str(row[0]),
                "dst_id": str(row[1]),
                "confidence": float(row[2]),
                "request_count": int(row[3]) if row[3] is not None else None,
                "unmatched_count": int(row[4]) if row[4] is not None else None,
            }
            for row in virtual.rows
        ],
    }


def no_model_gates(
    case: RCA100Case,
    spec: TransferCaseSpec,
    source: Mapping[str, object],
    stored: Mapping[str, object],
    equality: Mapping[str, object],
    mechanism: Mapping[str, object],
    *,
    isolated: bool,
    semantic_surface_contract: bool,
) -> dict[str, bool]:
    identity = stored["source_identity"]
    input_json = case.input.model_dump_json()
    gates = {
        "pinned_source_revision": case.dataset == f"RCA100-{DATASET_REVISION}",
        "frozen_selection_match": audit_source_case(case, spec.opaque_case_id, spec.case_role)
        == spec,
        "source_files_match": _source_files_sha256(case.root) == spec.source_files_sha256,
        "source_identity_valid": source.get("source_identity_valid") is True,
        "source_window_end_boundaries_empty": source.get("source_window_end_boundaries_empty")
        is True,
        "reference_labels_not_ingested": stored.get("reference_labels_not_ingested") is True,
        "stored_spans_match_declared_window": stored.get("stored_spans_match_declared_window")
        is True,
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
        # A case with no deterministic oracle reports None, which is not a
        # failure: the secondary audit is not estimable, not violated.
        "mechanism_evidence": mechanism.get("pass") is not False,
        "opaque_agent_case_id": (
            case.input.case_token == spec.opaque_case_id
            and spec.source_case not in input_json
            and spec.source_fault_type not in input_json
            and not case.input.fault_taxonomy
        ),
    }
    gates["all_passed"] = all(gates.values())
    return gates


def _trace_identity(spans: list[object]) -> dict[str, object]:
    identities = [(span.trace_id, span.span_id) for span in spans]
    return {
        "spans": len(spans),
        "span_identity_unique": len(identities) == len(set(identities)),
        "service_counts": dict(sorted(Counter(span.service_name or "" for span in spans).items())),
        "span_kind_counts": dict(sorted(Counter(KIND_NAMES[span.kind] for span in spans).items())),
        "status_code_counts": dict(
            sorted(Counter(STATUS_NAMES[span.status_code] for span in spans).items())
        ),
        "valid": len(identities) == len(set(identities))
        and all(
            _valid_hex_id(span.trace_id, 16)
            and _valid_hex_id(span.span_id, 8)
            and (not span.parent_span_id or _valid_hex_id(span.parent_span_id, 8))
            and span.service_name
            and span.kind in KIND_NAMES
            and span.status_code in STATUS_NAMES
            for span in spans
        ),
    }


def _edge_period(spans: list[object], window: tuple[int, int]) -> dict[str, object]:
    start_ns, end_ns = window[0] * 1_000_000_000, window[1] * 1_000_000_000
    servers: dict[tuple[str, str], list[object]] = defaultdict(list)
    for span in spans:
        if span.kind == ProtoSpan.SPAN_KIND_SERVER and span.parent_span_id:
            servers[(span.trace_id, span.parent_span_id)].append(span)
    edges: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for client in spans:
        if client.kind != ProtoSpan.SPAN_KIND_CLIENT:
            continue
        if not start_ns <= client.start_time_unix_nano < end_ns:
            continue
        for server in servers.get((client.trace_id, client.span_id), []):
            if client.service_name == server.service_name:
                continue
            pair = (str(client.service_name), str(server.service_name))
            edges[pair][0] += 1
            if server.status_code == ProtoStatus.STATUS_CODE_ERROR:
                edges[pair][1] += 1
    return {
        "edge_set": [
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
    }


def _stored_span_window(client: GreptimeClient) -> dict[str, int]:
    result = client.query(
        "SELECT COUNT(*) AS total, MIN(timestamp) AS first, MAX(timestamp) AS last FROM traces",
        max_rows=None,
    )
    total, first, last = result.rows[0]
    return {
        "count": int(total),
        "min_epoch": int(first) // 1_000_000_000,
        "max_epoch": int(last) // 1_000_000_000,
    }


def _unexpected_tables(client: GreptimeClient, database: str) -> set[str]:
    """Tables the replay did not write and no metric declared."""
    result = client.query(
        "SELECT table_name, signal_type FROM information_schema.table_semantics "
        f"WHERE table_schema = '{database}'",
        max_rows=None,
    )
    return {
        str(row[0])
        for row in result.rows
        if str(row[0]) not in INGESTED_TABLES and str(row[1]) != "metric"
    }


def _stored_group_counts(client: GreptimeClient, column: str) -> dict[str, int]:
    result = client.query(
        f"SELECT {column} AS value, COUNT(*) AS total FROM traces GROUP BY {column}",
        max_rows=None,
    )
    if result.truncated:
        raise RCA100Error(f"stored {column} counts were truncated")
    return dict(sorted((str(row[0]), int(row[1])) for row in result.rows))


def _valid_hex_id(value: str, length: int) -> bool:
    return len(value) == length * 2 and all(char in "0123456789abcdef" for char in value.lower())


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    return item if isinstance(item, Mapping) else {}
