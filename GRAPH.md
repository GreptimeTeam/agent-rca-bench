# Semantic Graph micro-benchmark protocol

This protocol isolates witnessed service-call retrieval from end-to-end
root-cause analysis (RCA). It tests whether the Semantic Graph helps an agent
identify a direct callee with anomalous RED evidence after Table Semantics has
already been exposed.

The task does not ask for a root cause, affected component, causal dependency,
fault mechanism, or onset. This keeps the result independent of the disputed
RCA100 component labels and of any unavailable dependency ground truth.

## Treatments

Run two paired treatments over the same isolated case database:

1. `table_semantics`: ordinary telemetry queries, semantic catalog search, and
   semantic table profiles.
2. `semantic_graph`: Table Semantics plus `query_semantic_graph`.

Use the same model, prompt, database, 12-call visible safety cap, and execution
limits for both treatments. The API runner uses a 22-turn limit. The supported
subscription CLIs do not expose a turn-limit option; their broker still enforces
the database-tool cap. Balance treatment position with an even repetition count.

## Relationship contract

The benchmark uses trace-derived service `calls` relationships. A real edge
pairs a `SPAN_KIND_CLIENT` span with a `SPAN_KIND_SERVER` child when their
`trace_id` values match and the server's `parent_span_id` equals the client's
`span_id`. `request_count` counts paired server spans. `error_count` counts
paired server spans whose `span_status_code` is `STATUS_CODE_ERROR`.

GreptimeDB emits these observations in 60-second buckets. Its computed-table
contract selects bucket starts with a half-open `observed_at` range
`[start, end)`, then widens the source scan to complete buckets. Each fixture
therefore rounds both source-report bounds up to a whole minute and compares
only that minute-aligned half-open window. The benchmark SQL uses the same
client window and the graph implementation's five-minute early and one-hour
late server-span allowances.

## Development fixtures

These fixtures and earlier RCA trajectories influenced the protocol. They are
regression fixtures, not measurement cases.

| Fixture | Source window | Analysis window | Supplied caller | Frozen winner |
| --- | --- | --- | --- | --- |
| RCA100 `t002` | `2026-04-23 02:59:37–03:09:03Z` | `[03:00:00, 03:10:00)` | `service/frontend` | `frontend → cart`, 9,423 requests, 1 error |
| OpenRCA 2.0 Hotel `hs1-geo-pod-failure-drdmjj` | `2026-05-02 01:49:57–01:59:57Z` | `[01:50:00, 02:00:00)` | `service/frontend` | `frontend → search`, 2,304 requests, 1,095 errors |

The winner is the unique direct `calls` callee with the greatest
`error_count`. Request volume does not break an error-count tie; a tie fails the
fixture gate.

## Task output

The agent returns one structured object:

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
prefiltered to the submitted destination cannot prove that it is the maximum.

In `table_semantics`, the citation must be a successful, untruncated SQL result
with exactly these fields: `src_type`, `src_id`, `dst_type`, `dst_id`,
`rel_type`, `provenance`, `request_count`, and `error_count`. The query must read
only the current database's trace table, pair client and server spans, scope the
caller, use the frozen client window, and group by both service endpoints.

In `semantic_graph`, the citation must be a successful, untruncated
`query_semantic_graph` result for trace-provenance `calls` edges from the
supplied service caller. It must not set `dst_id`, and its limit must cover the
independently audited edge set. An explicit `dst_type` filter is optional: the
cited result must itself contain only the canonical `service` destinations.

## Deterministic scoring

A run succeeds only when all conditions hold:

1. The submitted edge identity and RED counts equal the frozen winner.
2. `evidence_query_id` names exactly one executed query of the treatment's
   required tool type.
3. The cited query has the complete caller scope described above.
4. The cited result's normalized edge set equals the independent canonical
   Graph result, including every endpoint and its request/error counts.
5. The canonical edge set has one error-count winner, and that winner equals the
   fixture label.

The no-model audit first executes an independent raw trace self-join and the
deduplicated Graph aggregation. Their normalized edge sets must be identical.
Invalid output, a missing citation, truncation, runner failure, or tool-cap
rejection counts as an unsuccessful run. No LLM judge is used.

The primary outcome is paired task success for `semantic_graph` versus
`table_semantics`. Within one case, run-pair directions and efficiency are
descriptive only and carry no inferential p-value. Cross-case inference first
takes the median jointly successful run-pair delta per case. For pairs in which
both treatments succeed, report tool calls and GreptimeDB rows returned through
the cited evidence query. These development fixtures can validate the mechanism
and accounting but cannot support an effect claim.

## Execution

Prerequisites:

- The source smoke report points to the unchanged fixture database.
- That database is the only user schema in its GreptimeDB instance.
- The report's Graph coverage status is `relational`.
- The fixture server is running; no model is needed for `graph-audit`.

Run the deterministic gate first:

```bash
uv run semantic-rca graph-audit \
  --report .reports/smoke-rca100-t002-<run-id>.json
```

A successful audit writes a report only after the raw and Graph edge sets,
unique winner, and frozen winner all match. Any mismatch exits nonzero without
calling a model.

Run the paired development pilot only after the audit passes:

```bash
uv run semantic-rca graph-run \
  --report .reports/smoke-rca100-t002-<run-id>.json \
  --runner codex-subscription \
  --model gpt-5.6-luna \
  --repetitions 2
```

Every completed cell is persisted. Reusing an output path resumes only when the
source report, runner, model, seed, cap, repetitions, and Graph protocol match.
Stop only the temporary fixture process after the audit or pilot; do not stop an
unrelated GreptimeDB instance. Reports and fixture databases are ignored local
artifacts and can be retained for audit and resume.

## Measurement gate

Before interpreting a Graph effect:

1. Keep the two development fixtures as regression gates.
2. Freeze the prompt, output schema, scorer, window policy, caps, and runner
   prompt representation.
3. Select new relational cases before inspecting their Graph edge values or
   model trajectories.
4. Require the same raw-versus-Graph edge-set audit and a unique task winner.
5. Keep development and measurement reports separate and increase the case
   count before making an effect claim.

### Frozen measurement selection

The first v2 selection record was invalid: its five stated initial candidates
cannot be reproduced by applying `deterministic_rank` to the documented eligible
manifest set. Sixteen completed v2 cells remain useful exploratory observations,
but they are not trajectory-blind measurement cells.

Protocol v3 keeps seed `semantic-rca-v21-graph-measurement`, excludes every case
with an earlier agent trajectory, and freezes the exclusion set, eligible-set
counts and digests, and every source-gate rejection in
`fixtures/measurement/graph-v3-selection.json`. Eligible cases remain
new-source, non-hybrid, single-root cases with a causal path of at least three.
Selection is stratified by system and consumes manifest-ranked candidates until
two Hotel Reservation cases, two OTel Demo cases, and one Train Ticket case pass
the no-model gate.

The selection JSON is a frozen offline artifact. `selection.deterministic_rank`
supports its reproducible construction and audit; `graph-run` consumes the
selected fixture and never reranks cases from live telemetry.

Six Hotel candidates failed because manifest and injection roots disagreed or
the conclusion had no observable alert. The next two passed:

- `hs4-geo-pod-failure-pdt289`
- `hs1-rate-pod-failure-vmvtxr`

All twelve remaining OTel Demo candidates failed the same pre-model source gate:
eight lacked an observable alert and four had conflicting manifest/injection
roots. Train Ticket had one eligible candidate, and it had conflicting roots.
Those strata are exhausted rather than filled with known cases. Protocol v3
therefore has two Hotel Reservation fixtures and no valid fresh OTel Demo or
Train Ticket fixture; this is an external-validity result, not missing data to
impute.

For each selected case, caller selection reads only the distinct witnessed
client-to-server service endpoints over the minute-aligned source window. It
keeps callers with at least two direct callees and hash-ranks their IDs with the
seed `semantic-rca-v21-graph-measurement + "\\0" + source_case`; request counts,
status codes, durations, and Graph output are not
read during caller selection. The selected caller must then pass the existing
raw-versus-Graph exact edge-set gate and have one greatest-error winner. A
failing case is recorded and replaced only by the next manifest-ranked case in
the same system. The caller and audited winner are frozen in a
source-case-matched external fixture before any model run.

Passing this micro-benchmark does not establish that Graph improves end-to-end
RCA efficiency and does not authorize another paid full RCA batch.

### Measurement result

Both formal fixtures passed exact raw-versus-Graph edge equality and ran two
position-balanced repetitions with Codex `gpt-5.6-luna`. Graph succeeded in all
four cells; Table Semantics succeeded in three. Among the three jointly
successful pairs, Graph returned fewer rows and used fewer calls in all three,
with median deltas `-72` rows, `-2` calls, and `-40,705` reported model tokens.
These run-pair results describe model variation within the frozen cases. After
taking the median delta within each case, both cases favor Graph, with median
case deltas `-61.25` rows, `-2.5` calls, and `-40,831` reported model tokens.
The case-level exact two-sided sign-test is `p=0.5` for each metric. The
direction is consistent, but two Hotel cases cannot establish a broad Graph
effect. Model tokens are post-hoc and exploratory. Case-level evidence and
exclusions are in `RESULTS.md`, `DATASETS.md`, and
`fixtures/measurement/results-summary.json`.
