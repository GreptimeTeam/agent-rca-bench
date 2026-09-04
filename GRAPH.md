# Semantic Graph retrieval protocol

The Graph micro-benchmark isolates service-dependency retrieval from complete
root cause analysis. Given a caller and incident window, the agent identifies
the direct callee with the strongest error evidence and cites the complete edge
set used for that comparison.

## Treatments

- `raw`: ordinary SQL over the stored trace table.
- `semantic_graph`: the same SQL surface plus semantic catalog, table profiles,
  entities, relationships, and `query_semantic_graph`.

The model, prompt, database, case window, tool budget, turn policy, and runner
configuration stay fixed within a pair. The Graph treatment remains assigned
when an agent chooses to verify its answer through raw SQL.

## Relationship contract

A trace-derived service `calls` edge pairs a `SPAN_KIND_CLIENT` span with a
`SPAN_KIND_SERVER` child when `trace_id` matches and
`child.parent_span_id = parent.span_id`. Both service identities come from
stored source fields.

`request_count` counts paired Server spans. `error_count` counts paired Server
spans whose source status is `STATUS_CODE_ERROR`. HTTP 5xx is not promoted to an
OpenTelemetry error.

GreptimeDB stores relationship observations in 60-second `observed_at` bins.
The audit derives the minimal whole-minute Graph envelope from the source
half-open window, applies the Graph implementation's widened Server scan, and
records the boundary strategy. It never changes a publisher boundary to make
the edge sets match.

Every audit compares the complete normalized Raw and Graph edge sets, not one
selected edge or only a hash. It compares endpoint types and IDs, relationship
type, provenance, request count, and error count.

## Agent output

```json
{
  "src_type": "service",
  "src_id": "frontend",
  "dst_type": "service",
  "dst_id": "search",
  "rel_type": "calls",
  "provenance": "trace",
  "request_count": 2304,
  "error_count": 1095,
  "evidence_query_id": "q04",
  "claim": "search is the unique direct callee with the greatest error count."
}
```

The citation must cover every direct callee of the supplied caller. A query
filtered to the submitted destination cannot prove that it is the maximum.

In `raw`, the citation must be a successful, non-truncated SQL result that
reconstructs the complete caller edge set from stored spans. In
`semantic_graph`, the citation may be the same trace reconstruction or an
unfiltered `query_semantic_graph` relationship result with sufficient limit.

## Deterministic scoring

A run succeeds when:

1. The submitted edge identity and counts match the frozen winner.
2. The citation resolves to one successful query allowed by the treatment.
3. The query covers the complete caller scope and frozen window.
4. The cited normalized edge set equals the independently audited set.
5. The canonical set has one greatest-error winner and the submitted edge is
   that winner.

Invalid output, a missing citation, truncation, runner failure, or a rejected
tool call fails the run. No LLM judge is used.

For jointly successful pairs, the benchmark reports rows and tool calls through
the cited evidence. Repetitions describe variability. Cross-case summaries
first reduce repeated pairs to one median per case.

## Provider-free audit

```bash
uv run agent-rca graph-audit \
  --report .reports/smoke-openrca2-<run-id>.json
```

The command exits nonzero unless Raw and Graph edge sets, the unique winner,
and the frozen fixture all agree.

The formal suite uses two OpenRCA2 Hotel Reservation cases selected before
their formal model trajectories. Both passed exact Raw/Graph edge equality.
Their four-model results appear in [REPORT.md](REPORT.md). Two cases support a
focused retrieval observation, not a general Semantic Graph effect claim.
