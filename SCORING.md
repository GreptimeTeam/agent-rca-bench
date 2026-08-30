# RCA scoring contract

This document defines what the benchmark means by a correct and grounded root-cause analysis
(RCA). It applies to end-to-end RCA tasks. Retrieval micro-benchmarks keep their task-specific
scorers.

## Evaluation unit

The scorer evaluates one model run against one source-derived case rubric. It reports diagnosis,
grounding, citation integrity, execution reliability, and efficiency as separate dimensions. A
single `success` field may summarize auditable completion for machine control, but it is not a
substitute for the dimension results.

The scorer is deterministic. It does not use an LLM judge. Dataset labels and reference causal
graphs remain hidden from the agent.

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

Each final citation declares one or more claim types. A transfer case specifies which claim types
are required. The Aegis transfer cases require:

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

The case rubric defines observable facts and their relationships. It does not define one canonical
agent query. Equivalent queries can differ in aliases, aggregation, common table expressions,
filter placement, or whether they return complete raw rows. Exact source counts remain part of the
provider-free source audit, but an agent result does not need to reproduce those counts unless the
count itself is the causal predicate.

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

Absence claims need complete temporal coverage under the same source predicate used for the
anomalous period. This does not assert absence across every possible attribute value: an additional
source filter defines a narrower observation. The current JVM rubric permits that because its
frozen baseline oracle is zero and it requires repeated anomalous observations; a case with a
nonzero baseline needs a different rubric. Counted transitions must explicitly bind both outer
window bounds so rows outside the incident cannot inflate the anomalous count. A limited or
truncated result cannot prove absence. `BETWEEN` and explicit lower/upper comparisons are accepted
when they express the same inclusive bounds.

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

## Current Aegis evidence rubrics

The request-delay rubric requires source Client and paired Server spans on the declared edge. The
baseline start gap must stay below the frozen injected-delay threshold, and at least the frozen
minimum number of anomalous pairs must reach the threshold. Server duration alone cannot prove a
delay before server start.

The JVM-exception rubric requires both of these observations at the causal service and operation:

- source `STATUS_CODE_ERROR` spans change from a clean baseline to repeated anomalous errors;
- error-level logs contain an exception signature after onset, with no matching baseline
  exception.

The same observations may be represented as period rows, status-grouped counts, conditional
period aggregates, or complete grouped log lines. When a query covers both windows without
`LIMIT` or `HAVING`, an absent period row is a zero count. If a query includes multiple services or
operations, the result must project source identity columns so the scorer can isolate the declared
locus. Counts must retain aggregate lineage to source status or log rows; literal and multiplied
counts remain invalid.

HTTP 5xx values do not substitute for source OTel Error status. A Graph edge or propagated caller
failure can ground path or impact claims, but it cannot by itself establish a JVM exception.

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

Development trajectories can supply regression examples. They do not become measurement evidence,
and the runtime does not rescore old development protocols. A scoring-semantic change increments the
internal protocol before another model run.

## Design basis

The separation follows the useful boundaries in existing benchmarks without copying their weaker
parts. [OpenRCA](https://github.com/microsoft/OpenRCA/blob/main/main/evaluate.py) scores root-cause
fields independently. [RCAEval](https://github.com/phamquiluan/RCAEval) reports ranked localization
metrics. [OpenRCA 2.0](https://arxiv.org/abs/2606.27154) adds node, edge, and causal-path grounding.
The [trajectory-level RCA study](https://arxiv.org/abs/2608.21310) separates outcome correctness,
fault-path coverage, evidence use, and investigation behavior. This benchmark adds deterministic
citation and telemetry-provenance checks because its primary question concerns
correctness-preserving investigation efficiency.
