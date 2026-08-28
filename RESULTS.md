# Fresh micro-benchmark measurement conclusion

On 2026-08-28, Codex `gpt-5.6-luna` completed the frozen Discovery v2 and
Graph v3 measurement cells through the subscription runner. These were
constrained retrieval tasks, not full RCA runs. No paid full RCA batch was run.

The supported conclusion is that semantic surfaces can improve RCA investigation
efficiency when their metadata contract fits the telemetry. Task success is a
validity guardrail, not the target effect: completion-efficiency comparisons use
only pairs in which both treatments produced valid evidence. Table Semantics
reduced database output but did not establish a call or model-token benefit.
Semantic Graph reduced output, calls, and reported model tokens, but its formal
sample contains only two eligible cases.

The primary unit for inference across incidents is the case. Repeated run pairs
describe model variation within the frozen cases but are not independent cases.
The tables below report both levels and use the case-level result for the broad
conclusion.

## Discovery v2 formal measurement

The formal cohort contains six trajectory-blind OpenRCA windows selected from
Bank, Market cloudbed-1, Market cloudbed-2, and Telecom. Each case ran two
position-balanced repetitions of `raw` and `table_semantics`, for 24 cells and
12 paired observations. All cells completed without runner errors, rejected
calls, or 12-call cap hits.

| Case and hidden signal | Catalog rank | Raw | Table Semantics | Row deltas when both succeeded | Call deltas |
| --- | ---: | ---: | ---: | ---: | ---: |
| Market cloudbed-1, email container read throughput | 4/97 | 2/2 | 2/2 | -191, -733 | +4, -3 |
| Market cloudbed-2, email container write throughput | 5/64 | 2/2 | 2/2 | -520, -502 | 0, +2 |
| Bank, Redis02 CPU user utilization | 1/41 | 0/2 | 0/2 | not eligible | not eligible |
| Bank, Tomcat01 local-disk read I/O | 1/58 | 1/2 | 2/2 | -273 | -2 |
| Telecom, docker_001 CPU | 1/18 | 2/2 | 2/2 | -117, -117 | 0, -1 |
| Telecom, docker_006 CPU | 1/18 | 2/2 | 2/2 | -116, -116 | -1, -1 |

Task success was `9/12` for Raw and `10/12` for Table Semantics: one paired
improvement, no regression, and 11 ties. The exact two-sided sign-test result is
`p=1.0`. The only improvement occurred on the Bank read-I/O case. One Raw run
selected `DSKRTps`; both Table Semantics runs selected the frozen `DSKRead`
table. This is evidence of one localization correction, not a general accuracy
effect.

Both treatments failed both Redis CPU repetitions even though the target ranked
first in catalog search. All four runs chose Redis's cumulative
`used_cpu_user` counter instead of the frozen OS CPU-utilization gauge. The
submitted means changed only from about `15838.26` to `15840.30`, while the
independent target query changed from `0.19297` to `16.91008`. Catalog rank alone
was therefore insufficient to resolve two semantically similar CPU signals.

The nine run pairs in which both treatments succeeded give this descriptive
result:

| Metric, Table Semantics minus Raw | Better / worse / ties | Median delta | Exact two-sided sign-test |
| --- | ---: | ---: | ---: |
| Rows returned through cited evidence | 9 / 0 / 0 | -191 | `p=0.00390625` |
| Tool calls through cited evidence | 5 / 2 / 2 | -1 | `p=0.453125` |
| Discovery calls through cited evidence | 5 / 2 / 2 | -1 | `p=0.453125` |
| Reported model tokens through completion | 5 / 4 / 0 | -506 | `p=1.0` |

For cross-case inference, each case contributes the median of its jointly
successful run-pair deltas:

| Metric, Table Semantics minus Raw | Cases better / worse / tied | Median case delta | Exact two-sided sign-test |
| --- | ---: | ---: | ---: |
| Rows returned through cited evidence | 5 / 0 / 0 | -273 | `p=0.0625` |
| Tool calls through cited evidence | 3 / 2 / 0 | -0.5 | `p=1.0` |
| Discovery calls through cited evidence | 3 / 2 / 0 | -0.5 | `p=1.0` |
| Reported model tokens through completion | 3 / 2 / 0 | -2,725 | `p=1.0` |

Table Semantics reduced returned rows in every case eligible for completion
efficiency, covering all three source systems and both Market cloudbeds. The
case-level direction is consistent but does not cross the conventional `0.05`
threshold. Calls and tokens do not show a consistent effect. Reported model
tokens were recorded by the runner but were not a pre-registered primary metric,
so their analysis is exploratory.

All six formal fixtures passed independent canonical-query and catalog top-five
gates. Their catalog mean reciprocal rank is `0.7417`. The gate itself limits
external validity: these results measure agent behavior when the frozen target
is already in the first five semantic results. A Bank CPU candidate was rejected
at rank 14 before any agent run, so the measurement does not estimate catalog
recall over all eligible incidents.

The first cloudbed-2 report,
`.reports/discovery-v2-measurement-codex-luna-market-c2-node-write.json`, has
four valid exploratory cells but predates the mixed-case identifier prompt
correction. It is not pooled with the formal cohort. The next untouched
hash-ranked cloudbed-2 candidate passed the same source and catalog gates and
supplies the formal replacement. This preserves one prompt representation
across the 24 pooled cells.

## Graph v3 formal measurement

Graph v3 contains the only two fresh candidates that survived the frozen
manifest, source-fidelity, and raw-versus-Graph gates:
`hs4-geo-pod-failure-pdt289` and `hs1-rate-pod-failure-vmvtxr`. Both are Hotel
Reservation cases. Every OTel Demo and Train Ticket candidate was exhausted by
pre-model observable-alert or manifest/injection-root failures; no known case
was substituted.

For each selected caller, the raw client/server span self-join and the
deduplicated Graph query returned exactly the same six destination edges and
request/error counts. `search` was the unique greatest-error callee: 504 errors
out of 1,043 requests in the `hs4` window and 1,504 out of 2,772 in the `hs1`
window.

The two cases ran two position-balanced repetitions of `table_semantics` and
`semantic_graph`, for eight cells and four pairs. Graph succeeded `4/4`; Table
Semantics succeeded `3/4`. The single improvement was a Table answer that named
the correct `search` destination and counts but submitted `dst_type=search`
instead of the independently proven `service`. There were no regressions;
task-success sign-test `p=1.0`.

Among the three jointly successful run pairs, Graph used fewer calls and
returned fewer rows every time:

| Metric, Graph minus Table Semantics | Better / worse / ties | Median delta | Exact two-sided sign-test |
| --- | ---: | ---: | ---: |
| Rows returned through cited evidence | 3 / 0 / 0 | -72 | `p=0.25` |
| Tool calls through cited evidence | 3 / 0 / 0 | -2 | `p=0.25` |
| Reported model tokens through completion | 3 / 0 / 0 | -40,705 | `p=0.25` |

After taking the median delta within each case, both cases favor Graph:

| Metric, Graph minus Table Semantics | Cases better / worse / tied | Median case delta | Exact two-sided sign-test |
| --- | ---: | ---: | ---: |
| Rows returned through cited evidence | 2 / 0 / 0 | -61.25 | `p=0.5` |
| Tool calls through cited evidence | 2 / 0 / 0 | -2.5 | `p=0.5` |
| Reported model tokens through completion | 2 / 0 / 0 | -40,831 | `p=0.5` |

The direction is consistent with direct Graph retrieval replacing schema
inspection and a raw span self-join. The reported total-token deltas for the
three valid run pairs were `-40,705`, `-679`, and `-81,235`. Reported model
tokens are exploratory because they were not a pre-registered primary metric.
Two cases are not enough for a broad or statistically conclusive effect. The
earlier 16-cell Graph v2 run showed the same efficiency direction, but its stated
candidate list was not reproducible from the frozen manifest ranking. It remains
exploratory and is not pooled with v3.

## Decision

The semantic layer demonstrated a consistent investigation-efficiency mechanism
under constrained, fidelity-gated tasks. Table Semantics reduced rows in all
five cases eligible for completion efficiency, without a reliable call or token
effect. Semantic Graph reduced rows, calls, and reported model tokens in both
eligible relational cases. Neither case-level result is statistically
conclusive, and Graph remains underpowered and system-specific.

The main claim is efficiency, not higher diagnosis accuracy. Correct task output
is the guardrail that prevents fewer rows or tokens from counting as an
improvement when the evidence is wrong. Well-defined entity identities,
relationship semantics, and trace roles are applicability requirements for the
Graph result; the benchmark does not infer them when the source lacks them.

These measurements do not yet establish an end-to-end RCA efficiency effect and
do not justify another paid full RCA batch. The next valid step is to obtain more
independent, source-faithful relational cases and freeze an RCA transfer test
with returned rows, calls, model tokens, and elapsed time as efficiency outcomes
under a correctness-preserving guardrail. Do not tune or rerun the v17
development RCA cases as a holdout.

The tracked aggregate, exact report hashes, and metric-registration status are
in `fixtures/measurement/results-summary.json`. Regenerate it from the retained
local reports with:

```bash
uv run python -m semantic_rca_bench.measurement_summary
```

# Graph v1 development pilot

On 2026-08-27, Codex `gpt-5.6-luna` ran two position-balanced repetitions of
`table_semantics` and `semantic_graph` over each Graph development fixture. This
was an 8-cell service-call retrieval micro-benchmark, not a full RCA batch. All
cells completed without runner errors, rejected calls, or 12-call cap hits.

Both treatments succeeded in all four runs, so Graph did not change task
success. It did reduce work through the cited evidence query in every pair:

| Fixture | Table Semantics rows | Graph rows | Row deltas | Tool-call deltas |
| --- | --- | --- | --- | --- |
| RCA100 `t002` | 317, 253 | 4, 4 | -313, -249 | -2, -2 |
| OpenRCA 2.0 Hotel | 71, 72 | 6, 6 | -65, -66 | -2, -2 |

Across the four pairs, Graph was better / worse / tied on returned rows in
`4 / 0 / 0`, with median delta `-157.5` and exact two-sided sign-test
`p=0.125`. Tool calls had the same `4 / 0 / 0` direction, median delta `-2`, and
`p=0.125`. These are development results from two fixtures selected after their
telemetry and earlier RCA trajectories were known. They establish that direct
Graph retrieval can replace schema inspection plus a raw span self-join for
this constrained task; they do not establish an end-to-end RCA benefit.

The first scoring pass incorrectly required the Graph call to include an
explicit `dst_type=service` argument. All four Graph results already contained
only canonical service destinations and exactly matched the independent edge
set, so that argument check added no evidence. Scorer v2 accepts an omitted
destination-type filter while still rejecting a wrong filter, a destination ID
filter, an incomplete result, or any edge-set mismatch. The saved trajectories
were rescored; no model cell was rerun. Resume equality also ignores unscored
`duration_sum` floating-point roundoff and continues to compare every scored
edge identity and request/error count.

The run artifacts are `.reports/graph-v1-codex-luna-rca100.json` and
`.reports/graph-v1-codex-luna-hotel.json`.

# Graph v1 no-model implementation gate

The RCA100 `t002` and OpenRCA 2.0 Hotel development fixtures passed the
deterministic Graph gate. The audit called no model and performed two independent
queries per fixture: a raw client/server span self-join and a deduplicated
`semantic_relationships` aggregation over the same minute-aligned half-open
window.

| Fixture | Direct caller edges | Raw/Graph edge-set equality | Unique winner |
| --- | ---: | --- | --- |
| RCA100 `t002`, caller `frontend` | 4 | exact | `cart`: 9,423 requests, 1 error |
| OpenRCA 2.0 Hotel, caller `frontend` | 6 | exact | `search`: 2,304 requests, 1,095 errors |

The equality check includes every direct destination plus its request and error
counts. Duration fields are not part of this task. These results validate the
fixture, direction, RED derivation, deduplication, and scorer input; they are not
agent outcomes or evidence of an RCA effect.

The source audit also exposed a benchmark contract mismatch. GreptimeDB's
computed Graph tables implement half-open `[start, end)` `observed_at` windows,
but protocol v19 used `<= end` and described the tool as inclusive. Protocol v20
now uses `< end` consistently in the Graph tool, surface inspection, isolation
gate, and adapter validation queries. No RCA cells were rerun.

The audit artifacts are
`.reports/graph-audit-rca100-t002-frontend-callee-errors.json` and
`.reports/graph-audit-openrca2-hotel-frontend-callee-errors.json`.

# Discovery v1 development pilot

On 2026-08-27, Codex `gpt-5.6-luna` ran two position-balanced repetitions of
`raw` and `table_semantics` over each frozen discovery fixture. This was an
8-cell discovery micro-benchmark, not a full RCA batch. All cells completed
without runner errors, rejected calls, or 12-call cap hits.

| Fixture | Raw success | Table Semantics success | Paired outcome | Catalog target |
| --- | ---: | ---: | --- | --- |
| OpenRCA Market node write I/O | 2/2 | 2/2 | 0 improvements / 0 regressions / 2 ties | rank 2 of 29 matches |
| OpenRCA Telecom container CPU | 2/2 | 2/2 | 0 improvements / 0 regressions / 2 ties | rank 1 of 18 matches |

Table Semantics did not improve task success because Raw also succeeded in all
four runs. It did reduce GreptimeDB rows returned through the cited evidence
query in every pair:

| Fixture | Raw rows | Table Semantics rows | Paired row delta | Tool-call delta | Discovery-call delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| Market, each repetition | 600 | 227 | -373 | +1 | +1 |
| Telecom, each repetition | 145 | 31 | -114 | 0 | 0 |

Across the four pairs, Table Semantics was better / worse / tied on returned
rows in `4 / 0 / 0`, with a median delta of `-243.5` rows and exact two-sided
sign-test `p=0.125`. For tool calls and discovery calls, the direction was
`0 / 2 / 2`, median delta `+0.5`, and `p=0.5`. These are development results
from two fixtures and cannot support a measurement claim.

The first scoring pass exposed two deterministic-scorer false negatives. A
model may legally name the target as `current_database.table`, and GreptimeDB
accepts typed timestamp literals such as `TIMESTAMP '...+00:00'`. Scorer v2 now
accepts those equivalent forms while rejecting another database and still
requires exact epoch bounds plus equality with the independent canonical
result. The eight saved traces were rescored; no model cell was rerun.

The run artifacts are `.reports/discovery-v1-codex-luna-market.json` and
`.reports/discovery-v1-codex-luna-telecom.json`.

# Discovery v1 no-model implementation gate

The two frozen development fixtures passed the deterministic discovery gate on
their existing isolated GreptimeDB databases. No model or RCA runner was called.

| Fixture | Canonical baseline | Canonical incident | Predicate | Catalog result |
| --- | --- | --- | --- | --- |
| OpenRCA Market `node-6` write I/O | 8 samples; mean 15.9375; max 64.5 | 3 samples; mean 224.5; max 502.5 | incident max > 4× baseline max | `system_io_w_s` rank 2 of 29 matches |
| OpenRCA Telecom `docker_001` CPU | 9 samples; mean 8.8889; max 64 | 7 samples; mean 77.2857; max 83 | incident mean > 3× baseline mean | `container_cpu_used` rank 1 of 18 matches |

The Market target had ranked 29th before the catalog direction-token fix. The
new `I/O` normalization and adjacent `io_w` alias move it into the frozen top-five
gate without changing candidate recall or source telemetry. Telecom remains the
rank-1 control. These checks validate the fixtures and catalog regression only;
they are not agent outcomes or evidence of an RCA effect.

The audit artifacts are
`.reports/discovery-audit-openrca-market-node-write-io.json` and
`.reports/discovery-audit-openrca-telecom-container-cpu.json`.

# Protocol v17 subscription development evaluation

Protocol v17 ran two subscription-backed agents over six development cases.
Each model completed three position-balanced repetitions of `raw`,
`table_semantics`, and `semantic_graph`, for 54 runs per model and 108 runs in
total. Codex used `gpt-5.6-terra`; Claude Code used `sonnet`. Every treatment
used the same 48-call safety cap and the same case-local GreptimeDB instance.
The reports omit API dollar estimates because both runners used interactive
subscriptions.

The results do not establish an end-to-end RCA accuracy or efficiency benefit
for either semantic layer. Effects changed by dataset and model, and no positive
effect replicated across both models. The v14 finding that Graph reduced rows
returned also did not replicate.

## Post-v17 protocol audit

The v17 Codex subscription runner did not receive the system prompt used by the
API and Claude subscription runners. Within-Codex treatment pairs still share
that omission, but Codex-versus-Claude replication is not a same-protocol
comparison. Treat the cross-model agreement statements in this section as
development observations, not replicated effects.

The API runner also had a fixed 30-turn limit below the visible 48-call cap.
No v17 subscription run used that path, and no recorded v14 API run crossed the
limit, but the v14 API protocol did not make its full tool budget reachable for
a one-call-per-turn model. Protocol v19 gives each runner the same system
contract, aligns the enforceable database-tool cap, records runner failures,
and gives taxonomy violations the same scored-answer semantics. The supported
subscription CLIs do not expose a turn-limit option, so their process timeout is
recorded instead of claiming turn-limit equivalence.

RCA100 `t002` is not eligible for strict component or joint scoring. Its answer
file names `cart-64944cd445-8pbgx` in `root_cause_entities`, names the `cart`
service in `outcome.target_entities`, and contains another stale cart pod name
in the reasoning structure. Redis/Valkey appears in prose and in the
`redisUnavailable` mechanism, but the source does not publish a canonical
dependency entity. Protocol v19 therefore keeps mechanism scoring, disables
component and joint scoring for this case, and records a predicted causal
dependency without scoring it. The v17 zero joint matches remain historical
outputs, not valid component failures.

## Correctness by case

Correctness remains separate by dataset because the answer contracts and fault
taxonomies differ. Each cell reports strict joint matches out of three runs as
`Codex / Claude`.

| Dataset and ground truth | Graph coverage | Raw | Table Semantics | Table Semantics + Graph |
| --- | --- | ---: | ---: | ---: |
| RCAEval `currencyservice` / `disk` | Entity-only | 0/3 / 1/3 | 1/3 / 0/3 | 0/3 / 0/3 |
| RCA100 `cart-64944cd445-8pbgx` / `redisUnavailable` | Relational | 0/3 / 0/3 | 0/3 / 0/3 | 0/3 / 0/3 |
| OpenRCA Bank `Tomcat04` / `high CPU usage` | Empty | 3/3 / 1/3 | 0/3 / 3/3 | 2/3 / 2/3 |
| OpenRCA Hotel `geo` / `PodFailure` | Relational | 3/3 / 3/3 | 1/3 / 3/3 | 3/3 / 3/3 |
| OpenRCA Market `node-6` / node disk write I/O | Empty | 0/3 / 0/3 | 0/3 / 0/3 | 0/3 / 0/3 |
| OpenRCA Telecom `docker_001` / CPU fault | Empty | 0/3 / 0/3 | 1/3 / 3/3 | 0/3 / 0/3 |

Telecom provides the strongest positive Table Semantics observation. Table
Semantics produced four joint matches across the six model-and-repetition runs,
while Raw and the empty-Graph treatment produced none. The paired direction is
four improvements, no regressions, and two ties (`p=0.125`, exact two-sided sign
test). The effect is promising but not conclusive. The empty-Graph treatment
has the same stored semantic metadata and no Graph query tool, yet it failed all
six runs. That divergence shows that model variance or the coverage wording can
still dominate the data surface.

Bank does not replicate the Telecom direction. Codex favored Raw (3/3) over
Table Semantics (0/3), while Claude favored Table Semantics (3/3) over Raw
(1/3). Both Graph arms scored 2/3 even though the graph was empty. The reversal
rules out a model-independent Table Semantics claim on this case.

Market is a useful hard failure. All 18 runs missed the official node write-I/O
fault. Some Codex runs queried `system_io_w_s` and saw `node-6` reach 502.5, but
they ranked whole-window extrema or averages instead of comparing the 03:39
change point. Semantic catalog searches also matched up to 568 of 587 tables
because the query tokenizer treated `I/O` as the generic one-letter terms `i`
and `o`. The semantic surface did not turn a wide legacy schema into an
effective temporal investigation.

The two relational cases do not establish a Graph accuracy effect. Hotel is a
ceiling case: Claude scored every arm 3/3, while Codex scored Raw and Graph 3/3.
Two Codex Table Semantics answers named the correct `geo` pod but failed the
pre-registered strict component normalizer, so the 1/3 score includes an answer
granularity issue rather than two different causal diagnoses. RCA100 exposes a
ground-truth contract mismatch: both agents usually named the unavailable
Redis/Valkey dependency, while the official component is the affected cart pod.
For the mechanism alone, Codex improved from Raw 1/3 to Table Semantics 2/3 and
Graph 3/3, but Claude moved in the opposite direction from Raw 3/3 to 1/3 in
both semantic arms.

RCAEval remains label-ambiguous. The injected disk mechanism appears after the
container reaches its memory ceiling, so most runs chose `mem`. Codex produced
one Table Semantics disk match; Claude produced one Raw disk match. This case
does not isolate a semantic-layer effect.

## Pre-registered efficiency metrics

Lower returned-row counts are better. Each comparison pairs treatments by case
and repetition. Ties do not enter the exact two-sided sign test.

| Model | Comparison | Better / worse / ties | Median row delta | Sign-test p |
| --- | --- | ---: | ---: | ---: |
| Codex | Table Semantics vs Raw | 8 / 10 / 0 | +601 | 0.8145 |
| Codex | Graph vs Table Semantics | 9 / 9 / 0 | +24.5 | 1.0000 |
| Codex | Graph vs Raw | 8 / 10 / 0 | +179.5 | 0.8145 |
| Claude | Table Semantics vs Raw | 4 / 14 / 0 | +209 | 0.0309 |
| Claude | Graph vs Table Semantics | 10 / 8 / 0 | -71.5 | 0.8145 |
| Claude | Graph vs Raw | 6 / 12 / 0 | +152 | 0.2379 |

Claude Table Semantics returned more rows than Raw in 14 of 18 pairs. This is
the only v17 row comparison below `p<0.05`, and its direction is adverse. Codex
did not replicate the effect. Neither model replicated v14's Graph row
reduction: Graph was better than Raw in 8 of 18 Codex pairs and 6 of 18 Claude
pairs.

Calls to a correct diagnosis had too few eligible pairs for an effect estimate.
The metric requires both treatments in a pair to be jointly correct and not hit
the cap. Codex had at most five eligible pairs in any comparison; Claude had at
most four. Every exact sign test returned `p>=0.25`.

## Resource use

Totals are valid load measurements within a model because every treatment has
18 runs. They are not pooled correctness scores.

| Model | Treatment | Tools | DB queries | Rows returned | Failed calls | Cap hits | Agent time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Codex | Raw | 510 | 611 | 20,826 | 28 | 0 | 3,520 s |
| Codex | Table Semantics | 631 | 994 | 84,531 | 27 | 1 | 3,758 s |
| Codex | Table Semantics + Graph | 567 | 845 | 23,047 | 25 | 1 | 3,540 s |
| Claude | Raw | 594 | 685 | 17,950 | 35 | 0 | 3,030 s |
| Claude | Table Semantics | 561 | 705 | 24,013 | 31 | 0 | 3,031 s |
| Claude | Table Semantics + Graph | 599 | 740 | 21,559 | 33 | 0 | 3,209 s |

One Codex Market Graph run and one Codex Telecom Table Semantics run reached the
48-call cap. Claude had no cap hits. The controlled broker rejected and recorded
two unconfigured Codex tool calls and four unconfigured Claude tool calls; no
rejected call escaped the treatment boundary.

## Decision

Keep the three-layer design, but do not claim that either semantic layer improves
RCA yet.

1. Treat Telecom as a positive development lead for Table Semantics, not as an
   effect estimate. Reproduce the mechanism in a discovery micro-benchmark
   before another full RCA batch.
2. Fix semantic catalog precision for punctuation and one-letter terms, then
   test whether the agent can find and compare `system_io_w_s` around a declared
   incident boundary. Market supplies a concrete regression target.
3. Do not use Hotel to measure Graph accuracy; its RCA task has a ceiling effect.
   Use it only for graph-interface and efficiency regressions.
4. Resolve the RCA100 component contract before using strict joint accuracy.
   Report affected-component and causal-dependency answers separately if the
   dataset contract supports both.
5. Do not run another paid or subscription full batch until the discovery
   mechanism, scorer contracts, and a fresh measurement set are frozen.

The full trajectories and paired summaries are in
`.reports/v17-codex-terra-development.html` and
`.reports/v17-claude-sonnet-development.html`.

---

# Protocol v14 DeepSeek development evaluation

The complete batch used DeepSeek V4 Flash, six position-balanced repetitions,
and a visible 48-call safety cap for each of three treatments over four isolated
cases. All servers used GreptimeDB commit
`75cd53243e2c645621ae62202ec9413797bc98aa` from PR #8936. The four case runners
were concurrent; treatments within one case were serial. Database names were
neutral and rejected when they contained the ground-truth component or fault.

The result does not support a claim that either semantic layer improves
end-to-end RCA correctness. At the run-pair level, Semantic Graph returned fewer
GreptimeDB rows in 19 of 24 Graph-versus-Raw pairs (exact two-sided sign-test
`p=0.0066`), with a 36% aggregate reduction. Those repetitions are clustered in
four development cases, so the run-pair p-value is descriptive rather than a
cross-case significance result. Model cost and tool-call count did not differ
consistently.

## Per-case outcome

Correctness remains per dataset; the answer taxonomies and root-component
contracts are not comparable across corpora.

| Dataset and ground truth | Raw | Table Semantics | Table Semantics + Graph |
| --- | ---: | ---: | ---: |
| RCAEval `currencyservice` / `disk` | joint 4/6; component 6/6 | joint 3/6; component 6/6 | joint 2/6; component 6/6 |
| RCA100 `cart-64944cd445-8pbgx` / `redisUnavailable` | joint 0/6; mechanism 5/6 | joint 0/6; mechanism 5/6 | joint 0/6; mechanism 5/6 |
| OpenRCA Bank `Tomcat04` / `high CPU usage` | 0/6 | 1/6 | 0/6 |
| OpenRCA 2.0 Hotel `geo` / `PodFailure` | 6/6 | 6/6 | 6/6 |

RCAEval's component result is reliable, but its mechanism score is not a clean
RCA gold label. In the selected `disk` injection, memory reaches and remains at
the 128 MiB ceiling at 06:31:21; disk I/O first appears 12 seconds later. The
model alternates between the injected mechanism and the earlier observable
mechanism. This is a dataset contract issue, not evidence that semantic metadata
caused a wrong diagnosis.

The observed correctness direction on RCAEval is nevertheless adverse: joint
accuracy falls monotonically from Raw 4/6 to Table Semantics 3/6 to Graph 2/6.
The difference is not statistically significant, but it must not be summarized
as a neutral or positive result.

RCA100 has the inverse ambiguity. The model usually identifies
`redisUnavailable` but assigns causality to the unavailable Redis/Valkey endpoint,
while the official component label is the affected cart pod. The strict score
therefore records zero component matches even when the mechanism and dependency
are correctly identified.

## Efficiency and database load

Totals below are valid resource measurements because every treatment has the
same number of paired runs. They are not pooled correctness scores.

| Treatment | Runs | Tools | Discovery calls | DB queries | Rows returned | Failed queries | Cost | Agent time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Raw | 24 | 979 | 250 | 1,066 | 48,758 | 60 | $0.262 | 3,851 s |
| Table Semantics | 24 | 956 | 293 | 1,162 | 67,252 | 57 | $0.275 | 4,065 s |
| Table Semantics + Graph | 24 | 942 | 273 | 1,077 | 31,200 | 47 | $0.262 | 3,845 s |
| Total | 72 | 2,877 | 816 | 3,305 | 147,210 | 164 | $0.799 | 11,761 s |

Table Semantics did not reduce paired discovery calls or database load. Its
RCA100 aggregate of 43,742 returned rows is dominated by one 36,120-row run;
the other five runs returned 1,039–2,287 rows, so the total cannot support a
treatment conclusion. Graph reduced aggregate returned rows by 36% versus Raw
and 54% versus Table Semantics. The paired Graph-versus-Raw row result is the
only tested efficiency comparison below `p<0.05`.

Two previously reported trajectory metrics are invalid as efficiency evidence.
The old “first correct component turn” counted any substring mention, including
service-list dumps and false matches such as `geo` inside `range of`. The old
“discovery calls before first evidence” stopped at the first tool not classified
as discovery, so an opening Graph query improved the metric by definition. The
Hotel tool-call medians were also unpaired noise: Graph was lower in three
repetitions and Raw in three (`p=1.0`). None of these values supports “faster
entity localization.” Protocol v17 keeps component mention exploratory,
uses token-safe matching, and anchors discovery ordering to cited query IDs.

The 48-call cap was reached in six runs: four OpenRCA Bank runs, one RCA100
Table Semantics run, and one RCAEval Graph run. A run that used exactly 48 calls
counts as cap-reached even if the next action was submission. Completion-cost
statistics for any affected case-treatment cell are excluded rather than
conditioning on the runs that happened to finish early. The Bank Table
Semantics cell itself hit the cap in two of six runs, further weakening its
single correct diagnosis.

Across all runs, the model used 2,885,952 input tokens and 1,410,685 output
tokens. Cost uses the registered DeepSeek cache-miss rates of $0.14/MTok input
and $0.28/MTok output. Four case runners were concurrent, while maximum database
query concurrency within each agent remained one. Position was balanced, but
wall-clock latency remains secondary because provider load varied as runners
finished.

## Decision

Keep the three-layer experiment. The data rejects a universal semantic-layer
claim and gives a more useful applicability boundary:

1. Table Semantics has no established effect yet. The only Bank joint match is
   one run in a partly cap-bound cell and cannot support a capability claim.
2. Semantic Graph consistently reduced returned rows without reducing model
   cost or tool calls. The current evidence shows no accuracy gain and no valid
   faster-localization result. It is overhead on entity-only corpora and becomes
   a no-op when coverage is empty.
3. End-to-end RCA is too noisy to isolate the discovery mechanism by itself.
   Add a lower-cost discovery micro-benchmark over wide schemas, then use more
   sealed Bank-like and relational cases to test whether that mechanism transfers
   to RCA outcomes.
4. Do not tune the protocol on these four cases again and call the result a
   holdout. They are now development and regression cases for protocol v14.

The complete sessions, per-case matrices, cost, query load, and interaction
timelines are in `.reports/v14-deepseek-development.html`.

---

# Protocol v8 development diagnostics

The run used Claude Sonnet 5, one repetition, and an 18-tool-call cap for
one case from each accepted corpus. Every case ran against an exclusive
GreptimeDB instance at commit `75cd53243e2c645621ae62202ec9413797bc98aa`.
The adapters did not repair source timestamps, labels, topology, or ground
truth.

These cases are a development set. Protocol changes in versions v6 through v10
used their failures, so the results cannot measure generalization.

## Per-case outcome

| Dataset | Graph coverage | Raw | Table Semantics | Table Semantics + Graph |
| --- | --- | --- | --- | --- |
| RCA100 `t001` | Relational | Component only | Component only | Component only |
| RCAEval `re2ob_checkoutservice_cpu_1` | Entity-only | Joint correct | Joint correct | Joint correct |
| OpenRCA Bank `task_6@2021-03-04T18:00` | None | Incorrect | Incorrect | Incorrect |

Do not aggregate these rows into one accuracy score. RCAEval uses a small
resource-fault taxonomy, RCA100 uses fine-grained mechanisms, and OpenRCA uses
natural-language labels. A sum would assign equal weight to different answer
spaces and difficulty levels.

RCA100 is still a positive usability result for the graph. The Graph agent's
first graph query returned all 13 witnessed `calls` edges. It cited the
checkout-to-payment edge, including 752 errors in 1,541 requests, to separate
the payment failure from its upstream symptoms. Raw and Table Semantics reached
the same component from traces. All three selected the plausible but
non-canonical `codeDefect` label instead of the dataset's exact
`httpError5xx` label, so the official joint score remains false.

RCAEval exposes only seven service identities and no relationships. All three
treatments found the checkout-service CPU fault. The Graph agent used one
unfiltered entity query, but identity did not change the conclusion.

OpenRCA exposes 345 metric tables, one log table, and one trace table, with an
graph identity or relationship semantics. All three agents exhausted their
investigation on transaction and trace latency and missed the direct Redis02
memory signal. The current Table Semantics interface enriches a table only
after its name is known; it does not provide semantic catalog search across a
wide schema. That discovery limitation dominates this case.

## Resource use

| Treatment | Cases | Tools executed / requested | Cap-hit cases | Estimated model cost | Agent time | Queries | Rows | Failed queries |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Raw | 3 | 49 / 53 | 2 / 3 | $1.324 | 254.55 s | 58 | 1,643 | 1 |
| Table Semantics | 3 | 49 / 53 | 2 / 3 | $1.641 | 233.84 s | 75 | 2,292 | 1 |
| Table Semantics + Graph | 3 | 49 / 53 | 2 / 3 | $2.093 | 331.38 s | 63 | 2,267 | 3 |
| Total | 9 | 147 / 159 | 6 / 9 | $5.057 | 819.77 s | 196 | 6,202 | 5 |

Cost uses the report's 2026-08-10 standard API pricing snapshot: $2 per million
input tokens and $10 per million output tokens. Maximum observed database query
concurrency was one in every run. Every case ran in the order Graph, Raw, then
Table Semantics. The elapsed-time values are descriptive; cache and execution
position are fully confounded with treatment.

The call cap was binding on RCA100 and OpenRCA in every treatment. Semantic
metadata and graph calls consumed the same cap as SQL. These runs measure how
far each interface progressed under 18 calls, not whether an unconstrained
agent had enough evidence to stop.

## Decision

Keep these three cases as development and regression cases. Protocol v11 fixes
the confirmed implementation defects and records the design limitations in the
report. Add and freeze representative measurement cases before another paid
batch. Use one model for all treatments, balance execution position, and choose
a cap high enough to separate voluntary completion from forced submission.
