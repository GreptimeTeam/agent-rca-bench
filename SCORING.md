# RCA scoring contract

This document defines what the benchmark means by a correct and grounded root-cause analysis
(RCA). It applies to end-to-end RCA tasks. Retrieval micro-benchmarks keep their task-specific
scorers.

## Evaluation unit

The scorer evaluates one model run against one source-derived case rubric. It reports diagnosis,
grounding, citation integrity, execution reliability, and efficiency as separate dimensions. A
single `success` field may summarize auditable completion for machine control, but it is not a
substitute for the dimension results.

Execution validity, provenance, source identity, diagnosis structure, and directly decidable claim
grounding use deterministic checks. A run enters semantic adjudication only when its diagnosis is
correct, every citation is execution-valid, execution is reliable, and deterministic claim
grounding remains incomplete. Dataset labels and reference causal graphs remain hidden from the
investigating agent.

## Score the causal answer at its declared granularity

The structured diagnosis separates the causal locus from propagated impact:

- `causal_scope` is `component` or `dependency_edge`.
- A component-scoped diagnosis identifies one `causal_component` and no edge.
- An edge-scoped diagnosis identifies one directed `edge_source` and `edge_destination` and no
  causal component.
- `causal_operation` identifies the affected operation when the source telemetry supports one.
- `impacted_component` records a propagated symptom and never changes the causal locus.
- `mechanism_code` uses the case-independent mechanism ontology.
- `fault_type` is explanatory text and does not override the structured mechanism.

`diagnosis_correct` requires the scored structured fields to match the source-derived rubric. The
report also preserves each field match so that a correct component with an incorrect mechanism is
not reported as wholly wrong.

## Ground causal claims, not query recipes

Each final citation declares one or more claim types. These annotations describe the agent's
intent; they do not decide whether the cited result supports a claim. The scorer infers support
from the executed query and returned values. The current OpenRCA2 transfer cases require:

- `causal_locus`: evidence identifies the component or directed edge where the mechanism occurs.
- `fault_mechanism`: evidence discriminates the stated mechanism from plausible alternatives.

`onset`, `propagated_impact`, and `exclusion` are optional unless a case rubric explicitly makes
them required. Optional claims do not unlock the primary efficiency metrics.

A claim is grounded only when an execution-valid cited result supports it. The scorer may combine
several cited results. For example, one trace result can establish an operation-local error
transition while one log result establishes an exception signature.

Entity existence and an ordinary witnessed calls edge are navigation evidence, not proof that the
fault mechanism occurs at that entity or edge. They do not by themselves ground required
`causal_locus`. Incident-local evidence tied to the declared operation or mechanism must establish
the locus. A mechanism-bound result may ground `causal_locus` and `fault_mechanism` together.

The verifier separates three concerns: SQL establishes the measured scope and output lineage,
returned rows establish the observed values, and the case rubric defines the logical claim those
facts must satisfy. The rubric does not define one canonical agent query. Equivalent queries can
differ in aliases, aggregation, transparent common table expressions, filter placement, or whether
they return raw rows. Exact source counts remain part of the provider-free source audit, but an
agent result does not need to reproduce those counts unless the count itself is the causal
predicate.

## Preserve evidence provenance

An evidence result must satisfy all applicable conditions:

- The citation uniquely names one executed SQL or Semantic Graph result.
- The execution succeeded, the query ID matches, and the result is not truncated.
- The query reads the incident telemetry or the audited Semantic Graph surface.
- Source identity, span role, status, parent relation, operation, and time predicates retain their
  source meaning when the claim depends on them.
- Values used by the claim derive from telemetry columns or audited Graph fields, not projected
  constants.
- The result can be attributed to the claimed component, edge, operation, and period without mixing
  unrelated observations.

The scorer rejects neutralized predicates, identity cherry-picking, hard-coded evidence values,
row-multiplying joins used as counts, reference labels, and reference causal graphs. These checks
protect provenance. They must not require the agent to reproduce evaluator SQL.

An additional invalid or duplicated citation lowers `citation_integrity` and prevents
`auditable_completion`. It does not erase a correct diagnosis or required claims that other valid
citations already ground.

## Treat time as evidence, not an oracle literal

The scorer distinguishes three times:

- The intervention time comes from hidden source metadata.
- The observed onset is the first change supported by telemetry.
- The evaluation window bounds the telemetry available to the agent.

An agent may infer an observed onset after the intervention time. A baseline-to-anomaly query may
split at that observed onset when the baseline covers the source baseline window and the anomalous
result contains enough observations to establish the transition. The scorer does not require the
hidden intervention timestamp to appear in agent SQL.

Claim strength determines the required scope. A universal baseline claim such as "no restart at or
above the threshold" requires complete baseline coverage, telemetry-derived lineage, no predicate
on the asserted value, and a complete result. An existential anomalous claim needs only enough
in-window source observations to meet the frozen minimum; it may use a partial anomalous interval
or filter for threshold hits.

An aggregate threshold count is evaluated separately for each claim. Its predicate must include
every frozen-threshold violation to prove the universal baseline, and every counted row must meet
the frozen threshold to prove the existential anomaly.

Counted transitions must bind their measured interval so rows outside the incident cannot inflate
the result. A truncated tool result never proves a claim. A SQL `LIMIT` is complete only when the
returned cardinality is strictly below that limit; reaching the limit cannot prove a universal
claim. A case may accept an inclusive upper bound only after its provider-free source audit proves
that no source row lies on that boundary. `BETWEEN` and explicit comparisons are scored by their
actual temporal coverage. A time bucket that crosses the normal/anomalous boundary without an
unambiguous period projection cannot establish the transition.

## Separate the reported dimensions

The transfer scorer reports:

- `diagnosis_correct`: the structured causal answer matches the rubric.
- `claim_grounding`: required claim status and supporting citations.
- `required_evidence_covered`: every required claim is grounded.
- `citation_integrity`: every submitted citation is execution-valid and unique.
- `execution_reliability`: the runner contract, tool budget, and execution completed without an
  invalid call or runner failure.
- `auditable_completion`: diagnosis, required grounding, citation integrity, and execution
  reliability all pass.
- `efficiency_eligible`: diagnosis, required grounding, and execution reliability pass. An
  unrelated extra invalid citation does not discard the completed investigation trajectory.

Correctness rates use all runs. Efficiency deltas compare paired treatments only when both runs
reach the same required grounded endpoint. If one treatment reaches the endpoint and the other
does not, the result contributes to completion-rate differences rather than a conditional
efficiency delta. Rows, calls, tokens, cost, and latency from noneligible runs may be reported only
as descriptive trajectory data.

## Current OpenRCA2 evidence rubric

The v32 cohort covers four source-observable mechanisms: workload restart, container CPU
saturation, container memory pressure, and call-path start delay. Every rubric requires a nonempty
normal baseline with no value at or above its frozen threshold and at least two anomalous
observations at or above that threshold. The frozen threshold and exact source aggregate belong to
the provider-free oracle; the agent must prove the transition, not reproduce the evaluator's row
count or SQL text.

Metric rubrics require the exact source metric table, an unambiguous container population, and
telemetry-derived value lineage. Evidence may combine a complete baseline aggregate with a partial
anomalous query, or use one period-aware aggregate, separate period aggregates, unambiguous grouped
buckets, conditional aggregates, or raw rows. The population may be bound by the canonical
container identity, by a pod identity whose equivalence is frozen by the source audit, or by direct
identity columns in a complete result drawn from a wider population. Namespace and pod predicates
are accepted only when the frozen source audit proves they do not exclude rows from the selected
container population. `LIKE`, `IN`, and disjunction are judged by whether the frozen identity still
satisfies the predicate, not by their spelling. A wider result is bound only by the canonical
identity or a source-proven equivalent identity projected in the result; namespace alone cannot
identify the target container. Other identity narrowing, a wrong metric or workload, ambiguous
periods, truncation, and literal aggregates fail closed. A value predicate may prove anomalous
existence but cannot prove baseline absence. `HAVING` cannot prove baseline absence when it filters
the asserted value or can remove a subset of target-period buckets. It remains valid for a returned
aggregate whose grouping keys contain only the frozen identity and an unambiguous period. An
explicitly discriminated `UNION` may contribute an isolated source branch; undiscriminated rows or
overlapping source periods fail closed.

Metric values and call-path gaps are compared in their frozen source units. Exact positive linear
unit conversions in projections, aggregates, and threshold predicates are normalized before the
claim is checked, such as CPU ratio to percent, bytes to MiB, or nanoseconds to seconds. Affine,
negative, lossy, reversed, or otherwise unproven transforms fail closed. `AVG`, `MAX`, and `COUNT`
may establish anomalous existence only when their values mathematically imply the required number
of threshold observations; `MAX` at or above the threshold always invalidates baseline absence.

Call-path delay is edge-scoped. Its evidence pairs a source Client span with its Server child using
the same trace ID and exact parent-span relation, retains both source services and an allowed source
operation, and derives server-start minus client-start nanoseconds. Trace, span, or parent ID
filters cannot select a convenient example. Wrong roles or edge direction, arbitrary operation
patterns, incomplete windows, row multiplication, and hard-coded gaps fail closed.

Component-scoped metric cases do not publish a causal operation. That field is diagnostic rather
than scored for those cases: a model may leave it null or report an observed endpoint without
changing diagnosis correctness. Graph entities and ordinary calls edges may guide an investigation,
but they do not by themselves prove any of the four mechanisms.

## Regression standard

Before a protocol can call a model, its no-model scorer audit must cover:

- a canonical positive result;
- semantically equivalent result shapes;
- incorrect locus, direction, operation, and mechanism;
- missing required signals;
- incorrect span role, parent relation, source status, or source identity;
- hidden-oracle timestamp dependence;
- hard-coded, neutralized, multiplied, truncated, and incomplete evidence;
- additional invalid citations without loss of already grounded required claims.

The public artifact stores each cited query's derived scope, period facts, baseline and anomaly
verdicts, and per-claim rejection codes. It omits raw telemetry rows while retaining enough
information to deterministically recompute the scored claims and audit treatment-specific
eligibility failures.

Development trajectories can supply regression examples. They do not become measurement evidence,
and the runtime does not rescore old development protocols. A scoring-semantic change increments the
internal protocol before another model run.

Semantic adjudication is exhaustive over the frozen trigger, not selected after treatment results
are known. The judge input omits model identity, the explicit treatment label, run order, and
aggregate benchmark outcomes. Treatment remains inferable from tool names and query surfaces, which
must remain visible because SQL and Graph fields are evidence provenance; this is not a
treatment-blind review.
`claude-sonnet-5` and `deepseek-v4-flash`, neither of which is in the formal measurement roster,
judge each candidate independently. Both must find the cited
results sufficient for automatic acceptance; disagreement requires a human decision under the same
rubric. Deterministic hard-gate failures cannot be overruled. The deterministic result is the
headline analysis. Adjudication is a separately computed sensitivity analysis and does not rewrite
deterministic causal-locus, baseline, anomaly, or mechanism atoms. The public report publishes both
sets of case effects and inference statistics, the number of candidates and decisions by treatment,
and every decision basis.

## Design basis

The separation follows the useful boundaries in existing benchmarks without copying their weaker
parts. [OpenRCA](https://github.com/microsoft/OpenRCA/blob/main/main/evaluate.py) scores root-cause
fields independently. [RCAEval](https://github.com/phamquiluan/RCAEval) reports ranked localization
metrics. [OpenRCA 2.0](https://arxiv.org/abs/2606.27154) adds node, edge, and causal-path grounding.
The [trajectory-level RCA study](https://arxiv.org/abs/2608.21310) separates outcome correctness,
fault-path coverage, evidence use, and investigation behavior. This benchmark adds deterministic
citation and telemetry-provenance checks because its primary question concerns
correctness-preserving investigation efficiency.
