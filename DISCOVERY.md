# Discovery micro-benchmark protocol

This protocol isolates telemetry-table discovery and temporal evidence retrieval
from end-to-end root-cause analysis (RCA). It tests whether GreptimeDB Semantic Graph
helps an agent find a relevant signal in a wide schema and compare that signal
across a declared incident boundary.

The micro-benchmark does not ask the agent to infer the affected component,
fault mechanism, dependency, or onset. The task prompt supplies the component,
signal concept, incident window, and boundary. The physical table and columns
remain hidden.

## Treatments

Run two paired treatments over the same ingested case:

1. `raw`: `execute_sql` and raw `describe_table` access.
2. `semantic_graph`: the complete Semantic Graph tool surface, including
   `search_table_semantics`, semantic table profiles, and graph queries.

The task remains table discovery. The benchmark does not treat table semantics as a separate
product treatment or attempt to attribute the result to one internal Graph capability.

Use the same model, prompt, database, 12-call visible safety cap, and execution
limits for both treatments. The API runner uses a 22-turn limit so a model that
issues one tool call per turn can use the full tool budget and submit an answer.
The supported Codex and Claude subscription CLIs do not expose a turn-limit
option. Their broker still enforces the 12-call database-tool cap, and the
report records that the runner turn limit is not enforceable. Do not compare
turn efficiency across runner implementations. Balance treatment position
across repetitions within one runner.

## Development fixtures

The protocol starts with two development-only regression fixtures. Their
telemetry and prior RCA trajectories have already influenced the design, so they
cannot support a measurement claim.

| Fixture | Supplied target | Hidden target table | Frozen evidence windows and predicate |
| --- | --- | --- | --- |
| OpenRCA Market `Market/cloudbed-1@2022-03-21T03:30` | `node-6`, node disk write I/O, official boundary `03:39:14` UTC+8 | `system_io_w_s` | Compare baseline `03:30:00–03:38:00` with incident `03:38:00–03:41:00`. The incident maximum must exceed four times the baseline maximum. |
| OpenRCA Telecom `Telecom@2020-05-27T05:00` | `docker_001`, container CPU usage, official boundary `05:09:00` UTC+8 | `container_cpu_used` | Compare baseline `05:00:00–05:09:00` with incident `05:13:00–05:20:00`. The incident mean must exceed three times the baseline mean. |

The Market source samples once per minute. Its largest boundary-adjacent value
is timestamped `03:39:00`, 14 seconds before the official record. Splitting the
complete half-hour exactly at `03:39:14` would put that sample in the baseline
and invert the intended assertion. The frozen incident window includes the
adjacent sample without changing the official boundary. The Telecom incident
window starts at `05:13:00` because the untouched direct CPU signal lags the
official record by about four minutes. These development windows and thresholds
come from no-model audits in `DATASETS.md`; they are regression assertions, not
effect estimates or onset corrections.

## Task output

The agent must execute an evidence query and return one structured object:

```json
{
  "table": "system_io_w_s",
  "evidence_query_id": "q07",
  "component": "node-6",
  "signal": "node disk write I/O",
  "claim": "The incident-window write-I/O maximum exceeds the baseline maximum."
}
```

The cited evidence query must return exactly one `baseline` row and one `incident`
row with these aliases:

- `phase`
- `sample_count`
- `mean_value`
- `max_value`

The query must use `UNION ALL` with one aggregate `SELECT` for each row. Each
branch must independently scope the supplied component and one exact frozen
evidence window. A whole-window maximum, ranking, shifted window, or aggregate
over multiple components is not valid temporal evidence.

## Deterministic scoring

A run succeeds only when all conditions hold:

1. `table` names the frozen target, either unqualified or qualified by the current database.
2. `evidence_query_id` identifies an executed, successful SQL query.
3. The cited query reads only the submitted target table. Both `SELECT` branches
   conjunctively filter the supplied component with the fixture's declared
   component column.
4. The two branches respectively use the exact half-open baseline and incident
   windows; bounds under `OR` or `NOT` do not count. Plain and typed timestamp
   literals are equivalent only when they resolve to the exact frozen epochs.
5. The result contains one non-empty `baseline` row and one non-empty `incident`
   row with the required columns.
6. The result matches an independent canonical query over the frozen table,
   component, columns, and windows.
7. The canonical result satisfies the fixture's temporal evidence predicate.

Do not use an LLM judge. Invalid JSON, a missing citation, a truncated evidence
result, a call rejected by the cap, or a runner failure counts as an
unsuccessful run.

The primary outcome is paired task success for `semantic_graph` versus `raw`.
Within one case, report run-pair improvements, regressions, ties, and efficiency
only as descriptive model variation; do not attach an inferential p-value to
repetitions. Cross-case inference first takes the median jointly successful
run-pair delta per case. For pairs in which both treatments succeed, report these
secondary efficiency metrics:

- tool calls through the cited evidence query;
- GreptimeDB rows returned through the cited evidence query;
- discovery calls through the cited evidence query.

Report catalog precision diagnostics separately: target reciprocal rank,
matched-table count, and whether the target appears in the first five results.
These diagnostics explain failures but do not replace task success.

## Execution

The source smoke report must point to a live database containing the unchanged
fixture ingestion. Run the deterministic gate first; it does not call a model:

```bash
uv run semantic-rca discovery-audit \
  --report .reports/smoke-openrca-<fixture-run>.json
```

`discovery-run` invokes the selected model. Use it only after the audit passes;
an even repetition count balances the two treatment positions:

```bash
uv run semantic-rca discovery-run \
  --report .reports/smoke-openrca-<fixture-run>.json \
  --runner codex-subscription \
  --model gpt-5.6-luna \
  --repetitions 2
```

Every completed cell is persisted. Reusing the same output path resumes only
when the source report, runner, model, seed, cap, repetition count, and discovery
protocol still match.

## Measurement gate

Before any measurement run:

1. Pass the Market punctuation regression and the Telecom delayed-signal
   regression without changing their source data.
2. Freeze the prompt, output schema, scorer, call cap, turn cap, catalog
   tokenizer, and runner-specific prompt representation.
3. Select fresh wide-schema tasks without inspecting agent trajectories. Record
   each task's target table, component filter, boundary, windows, and evidence
   predicate during a no-model fidelity gate.
4. Keep development and measurement reports separate. Do not promote the two
   development fixtures to measurement cases.

### Frozen measurement selection

Protocol v2 selects six new OpenRCA windows with seed
`semantic-rca-v21-discovery-measurement`. Eligibility is decided from `query.csv`
and `record.csv` only: the half-hour contains exactly one official record, its
reason names CPU, memory, or I/O, and the case has no earlier agent trajectory.
Candidates are ranked independently for Bank, Market cloudbed-1, Market
cloudbed-2, and Telecom. Bank keeps the first two candidates from distinct
CPU/memory/I/O reason classes; Telecom keeps its first two candidates; each
Market system keeps its first candidate. The prior-agent exclusion set, eligible
set counts and digests, consumed ranks, and source-gate rejections are frozen in
`fixtures/measurement/discovery-selection.json`.

The reproducible metadata-only ranking selects these initial candidates:

- `Bank/task_6@2021-03-25T09:00`
- `Bank/task_5@2021-03-12T15:00`
- `Market/cloudbed-1@2022-03-20T09:30`
- `Market/cloudbed-2@2022-03-21T20:30`
- `Telecom@2020-05-29T03:30`
- `Telecom@2020-05-23T04:30`

Before telemetry values are read, each task uses the official record timestamp
as its boundary, the case start through the preceding whole minute as baseline,
and the following whole minute through case end as incident. The canonical
signal must have non-empty samples in both windows and an incident mean at least
twice the baseline mean. A failing candidate is recorded and replaced only by
the next hash-ranked candidate in the same stratum. Target table and column
bindings are frozen in a source-case-matched external fixture before any model
run.

The source gate maps an official component to one exact source identifier. A
one-to-many mapping rejects the case. Among positive-direction KPIs that match
the official CPU, memory, read-I/O, or write-I/O concept, it selects the greatest
incident-to-baseline mean ratio, with the normalized table name as a deterministic
tie-break. CPU idle, memory free/available, and I/O wait/latency signals are not
eligible substitutes. A zero baseline is eligible only when the incident mean is
positive.

The first cloudbed-2 candidate had a canonical memory-usage ratio of `1.03695`
and failed the `2.0` gate. Its successor mapped one service label to multiple
physical identifiers and also failed. The third candidate,
`Market/cloudbed-2@2022-03-21T08:30`, passed both source and catalog gates, but
its four agent cells predated the mixed-case identifier prompt correction. That
report remains exploratory and is not pooled with the uniform formal cohort.
The next untouched ranked candidate,
`Market/cloudbed-2@2022-03-20T14:30`, passed and supplies the formal replacement.

The initial Bank CPU candidate passed its source ratio gate but its target ranked
14th in catalog search, outside the frozen top-five gate. No agent ran on that
case. The next untouched CPU candidate,
`Bank/task_5@2021-03-04T20:00`, passed with catalog rank 1 and replaced it.
The final six formal fixtures are:

- `Bank/task_6@2021-03-25T09:00`
- `Bank/task_5@2021-03-04T20:00`
- `Market/cloudbed-1@2022-03-20T09:30`
- `Market/cloudbed-2@2022-03-20T14:30`
- `Telecom@2020-05-29T03:30`
- `Telecom@2020-05-23T04:30`

Every replacement, rejection, eligible-set digest, and consumed rank is recorded
in `fixtures/measurement/discovery-selection.json`.

Selection manifests are frozen offline artifacts. `selection.deterministic_rank`
is the reproducible offline ranking primitive used to construct and audit them;
`discovery-run` consumes the frozen fixture and does not rerank cases.

Repeated run pairs describe model variation within the frozen cases. Cross-case
inference uses the median jointly successful delta per case. Table Semantics
reduced returned rows in all five eligible cases, with median case delta `-273`
and exact two-sided sign-test `p=0.0625`. Calls and reported model tokens do not
show a consistent case-level effect. The reproducible aggregate and exact formal
report hashes are in `fixtures/measurement/results-summary.json`.

Do not start another paid or subscription full RCA batch from a successful
micro-benchmark alone. The micro-benchmark establishes a discovery mechanism;
a later frozen RCA measurement must test whether that mechanism reduces
end-to-end investigation work while preserving valid diagnoses.
