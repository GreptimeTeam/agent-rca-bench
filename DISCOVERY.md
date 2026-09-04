# Semantic discovery protocol

The Discovery micro-benchmark isolates telemetry-table discovery and temporal
evidence retrieval from complete root cause analysis. The task supplies a
component, signal concept, incident window, and boundary. The physical table and
columns remain hidden.

## Treatments

- `raw`: SQL and raw table descriptions.
- `semantic_graph`: the same SQL surface plus semantic catalog search, table
  profiles, entities, relationships, and Graph tools.

Table semantics are part of the complete Semantic Graph treatment, not a third
treatment. The model, prompt, database, window, tool budget, turn policy, and
runner configuration stay fixed within a pair.

## Agent output

```json
{
  "table": "system_io_w_s",
  "evidence_query_id": "q07",
  "component": "node-6",
  "signal": "node disk write I/O",
  "claim": "The incident-window write-I/O maximum exceeds the baseline maximum."
}
```

The citation returns one non-empty baseline aggregate and one non-empty
incident aggregate with the frozen component, signal, and half-open windows.
The scorer validates result values against an independent canonical query. It
does not infer success from a table name or free-form claim.

## Deterministic scoring

A run succeeds when:

1. The submitted table matches the frozen target.
2. The citation resolves to one successful, non-truncated SQL query.
3. The query reads only the target table and binds the supplied component.
4. Baseline and incident branches cover the exact frozen windows.
5. Both result rows are non-empty and contain the required aggregates.
6. The normalized result equals the independent canonical result.
7. The canonical result satisfies the frozen temporal predicate.

Invalid structured output, a missing citation, truncation, runner failure, or a
rejected tool call fails the run. No LLM judge is used.

For jointly successful pairs, the benchmark reports rows, tool calls, and
catalog-discovery calls through the cited evidence. Repetitions describe model
variability. Cross-case summaries first reduce repeated pairs to one median per
case.

## Reference cohort

The fixed cohort contains six OpenRCA 1.0 cases: two Bank cases, one Market
cloudbed-1 case, one Market cloudbed-2 case, and two Telecom cases. The frozen
selection is recorded in `fixtures/measurement/discovery-selection.json`.
Formal model trajectories do not participate in selection.

Each case must pass exact source identity, non-empty baseline and incident
samples, the frozen signal transition, unambiguous component identity, a
top-five catalog result, and isolated ingestion into an empty GreptimeDB
instance.

```bash
uv run agent-rca discovery-audit \
  --report .reports/smoke-openrca-<run-id>.json
```

The complete four-model results appear in [REPORT.md](REPORT.md). Discovery
tests schema and signal retrieval; it does not establish end-to-end RCA
correctness or efficiency.
