from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from agent_rca_bench.contracts import QueryResult

EDGE_COLUMNS = (
    "src_type",
    "src_id",
    "dst_type",
    "dst_id",
    "rel_type",
    "provenance",
    "request_count",
    "error_count",
)


def graph_audit_window(source_start: int, source_end: int) -> dict[str, object]:
    if source_start >= source_end:
        raise ValueError("graph source window must be non-empty")
    observed_start = source_start - source_start % 60
    observed_end = ((source_end + 59) // 60) * 60
    return {
        "source_window": [source_start, source_end],
        "source_window_semantics": "publisher half-open normal+abnormal envelope",
        "graph_observed_window": [observed_start, observed_end],
        "client_scan_window": [observed_start, observed_end],
        "server_scan_window": [observed_start - 5 * 60, observed_end + 60 * 60],
        "observed_at_bin": "date_bin(60s, client.timestamp)",
        "join_time_bounds": (
            "server.timestamp >= client.timestamp - 5m and <= client.timestamp + 1h"
        ),
        "transform": "minimal whole-minute envelope containing the complete source window",
    }


def canonical_raw_edge_query(window_start: int, window_end: int) -> str:
    start = _time_literal(window_start)
    end = _time_literal(window_end)
    server_start = _time_literal(window_start - 5 * 60)
    server_end = _time_literal(window_end + 60 * 60)
    return f"""SELECT 'service' AS src_type, c.service_name AS src_id,
       'service' AS dst_type, s.service_name AS dst_id,
       'calls' AS rel_type, 'trace' AS provenance,
       COUNT(*) AS request_count,
       SUM(CASE WHEN s.span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END) AS error_count
FROM traces c
JOIN traces s
  ON c.trace_id = s.trace_id
 AND s.parent_span_id = c.span_id
 AND s.timestamp >= c.timestamp - INTERVAL '5 minutes'
 AND s.timestamp <= c.timestamp + INTERVAL '1 hour'
WHERE c.span_kind = 'SPAN_KIND_CLIENT'
  AND s.span_kind = 'SPAN_KIND_SERVER'
  AND c.service_name <> s.service_name
  AND c.timestamp >= {start} AND c.timestamp < {end}
  AND s.timestamp >= {server_start} AND s.timestamp < {server_end}
GROUP BY c.service_name, s.service_name
ORDER BY src_id, dst_id"""


def canonical_graph_edge_query(
    window_start: int,
    window_end: int,
    *,
    paired_only: bool = False,
) -> str:
    """The graph's service call edges over a half-open observed_at range.

    paired_only keeps the edges a client/server span pairing produces. A virtual
    edge to an uninstrumented peer carries confidence below 1.0 and has no
    server span, so raw SQL cannot derive it and an equality audit that included
    it would be comparing two different claims.
    """
    start = _time_literal(window_start)
    end = _time_literal(window_end)
    paired = "\n  AND confidence = 1.0" if paired_only else ""
    return f"""SELECT src_type, src_id, dst_type, dst_id, rel_type, provenance,
       SUM(request_count) AS request_count,
       SUM(error_count) AS error_count
FROM greptime_private.semantic_relationships
WHERE observed_at >= {start} AND observed_at < {end}
  AND src_type = 'service' AND dst_type = 'service'
  AND rel_type = 'calls' AND provenance = 'trace'{paired}
GROUP BY src_type, src_id, dst_type, dst_id, rel_type, provenance
ORDER BY src_id, dst_id"""


def virtual_peer_edge_query(window_start: int, window_end: int) -> str:
    """The call edges the graph surfaces for peers that emit no spans."""
    start = _time_literal(window_start)
    end = _time_literal(window_end)
    return f"""SELECT src_id, dst_id, MAX(confidence) AS confidence,
       SUM(request_count) AS request_count,
       SUM(unmatched_count) AS unmatched_count
FROM greptime_private.semantic_relationships
WHERE observed_at >= {start} AND observed_at < {end}
  AND rel_type = 'calls' AND provenance = 'trace' AND confidence < 1.0
GROUP BY src_id, dst_id
ORDER BY src_id, dst_id"""


def normalize_edge_result(result: QueryResult) -> list[dict[str, object]] | None:
    if result.truncated:
        return None
    columns = [column.lower() for column in result.columns]
    if any(columns.count(column) != 1 for column in EDGE_COLUMNS):
        return None
    indexes = [columns.index(column) for column in EDGE_COLUMNS]
    edges = []
    identities = set()
    for row in result.rows:
        if len(row) <= max(indexes):
            return None
        values = [row[index] for index in indexes]
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values[6:]):
            return None
        identity = tuple(str(value) for value in values[:6])
        if identity in identities:
            return None
        identities.add(identity)
        edges.append(
            {
                **dict(zip(EDGE_COLUMNS[:6], identity, strict=True)),
                "request_count": values[6],
                "error_count": values[7],
            }
        )
    return sorted(edges, key=lambda edge: tuple(edge[column] for column in EDGE_COLUMNS))


def edge_set_sha256(edges: list[dict[str, object]] | None) -> str | None:
    if edges is None:
        return None
    canonical = json.dumps(edges, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _time_literal(epoch: int) -> str:
    value = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return f"TIMESTAMP '{value}'"
