# Semantic RCA Bench v31 measurement analysis

This release-candidate run completed all 216 scheduled cells across six model configurations, nine
cases, two treatments, and two repetitions. It recorded no runner error or budget exhaustion. The
machine-readable [combined report](semantic-rca-v31-report.json), the
[self-contained HTML report](semantic-rca-v31-report.html), and the two sanitized source artifacts
contain the reproducible results.

## Conclusion

The result supports a narrow claim: Semantic Graph materially reduces investigation work when the
task is to retrieve an already-supported service relationship. It does not support a general
end-to-end RCA efficiency claim.

On the two relationship micro-benchmark cases, five of six models had correctness-preserving paired
results. Every one of those five models returned fewer rows and used fewer tool calls with Semantic
Graph. Their median Graph-minus-Raw deltas ranged from `-1.5` to `-4.75` calls and from `-64` to
`-183` rows. Reported token deltas were also negative for all five models. Claude Opus 5 found the
correct edge in all four Graph runs but cited an `execute_sql` result instead of the Graph result;
those runs failed the treatment-compatible evidence gate and did not enter the efficiency
comparison.

The six discovery cases produced mixed results. Semantic metadata often reduced returned rows, but
catalog exploration added calls or tokens for several models. This surface is not a uniform
efficiency win for table localization.

The fresh end-to-end Aegis case is a negative transfer result. All 24 runs identified
`ts-auth-service` and the `workload_restart` mechanism. All 12 Graph runs called
`query_semantic_graph`. Only one Graph run, from `gpt-5.6-sol`, cited evidence that covered both the
causal locus and the required normal-to-abnormal restart transition. No Raw/Graph repetition pair
was jointly evidence-eligible, so rows, calls, and tokens cannot be compared as valid end-to-end
efficiency outcomes.

The transfer result shows the boundary of the product claim. Graph navigation can identify the
right component and relationship, but it does not replace metric-level mechanism evidence. A model
must still query and cite the restart counter over both windows.

## Model report cards

Counts are `Raw / Semantic Graph`. Discovery has 12 runs per treatment and relationship retrieval
has four. Transfer has two runs per treatment. Model rows are separate report cards, not a pooled
ranking.

| Model | Micro validity | Discovery validity | Discovery median calls / rows / tokens delta | Relationship validity | Relationship median calls / rows / tokens delta | Transfer diagnosis | Transfer evidence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 16 / 16 | 12 / 12 | 0 / -197.25 / -377.5 | 4 / 4 | -1.5 / -64 / -2,159.25 | 2 / 2 | 0 / 1 |
| `deepseek-v4-pro` | 16 / 15 | 12 / 11 | +0.5 / -180 / +774 | 4 / 4 | -4.75 / -98 / -15,141.25 | 2 / 2 | 0 / 0 |
| `claude-opus-5` | 14 / 10 | 10 / 10 | 0 / -228 / +2,662.5 | 4 / 0 | n/a | 2 / 2 | 0 / 0 |
| `claude-fable-5` | 16 / 16 | 12 / 12 | 0 / +12 / +12,363 | 4 / 4 | -2.75 / -67 / -8,610.75 | 2 / 2 | 0 / 0 |
| `glm-5.3` | 12 / 15 | 8 / 11 | -1.5 / -461.75 / -4,096.25 | 4 / 4 | -4.75 / -183 / -11,052 | 2 / 2 | 0 / 0 |
| `qwen3.8-2.4t-a95b` | 15 / 13 | 11 / 9 | 0 / -288.5 / +4,134 | 4 / 4 | -2.25 / -152.5 / -7,211.75 | 2 / 2 | 0 / 0 |

`gpt-5.6-sol` and `claude-fable-5` completed all 32 micro tasks successfully.
`deepseek-v4-pro` completed 31, `qwen3.8-2.4t-a95b` completed 28, `glm-5.3` completed 27, and
`claude-opus-5` completed 24. These counts mix two different micro tasks and therefore do not form
an overall RCA score. The end-to-end diagnosis count was identical across models; the evidence
coverage result separates only one run.

## Reliability and accounting

- The formal reports contain 216 completed cells, zero runner errors, and zero budget exhaustions.
- Agents issued 2,038 database queries. Forty-five failed (`2.21%`): 24 of 957 Raw queries and 21
  of 1,081 Graph queries. Failed SQL was returned to the agent and did not become valid evidence.
- Combined cache hit rates ranged from `59.3%` to `91.1%`. The report does not estimate a
  no-cache counterfactual.
- Prompt caching was enabled for both Claude models. Their micro-benchmark responses nevertheless
  reported zero cache creation and zero cache reads, while their longer transfer responses did
  report both. Cost estimates use the provider-reported usage without imputing cache hits.
- Known estimated cost is `USD 29.223846104` plus `CNY 12.0569415`. The GLM 5.3 price was not
  listed in the frozen BigModel pricing source, so its cost remains unavailable. The report does
  not convert or add currencies.
- Two execution recoveries preserved exact-prefix semantics. A missing DashScope endpoint caused
  four pre-provider failures with zero token or query usage, and a Graph source hash exposed
  floating-point diagnostic instability before any Graph model cell ran. No completed paid cell
  was rerun.

## Audit boundary

The two public artifacts exclude provider responses, reasoning payloads, credentials, local paths,
and source telemetry rows. The micro artifact retains only cited canonical aggregate evidence and
can deterministically rescore all 192 cells. The transfer artifact retains structured diagnoses,
citation-grounding certificates, execution counts, usage, and deterministic evaluations; its
validator recomputes the public scoring layers and model summaries.

The run binds GreptimeDB commit `e67af3fad8f698d964398843d78df782f20847bb`, the release binary
profile, task fixtures, selection manifests, scorer, protocols, and source checksums. It does not
bind the benchmark worktree to a Git commit. Under the repository's release policy, this is a
release-candidate measurement rather than the first public 1.0 run. A public tag requires a clean,
commit-bound rerun of the included cells.

## Limits

- The six discovery and two relationship cases are a fixed reference cohort whose earlier runs
  influenced the mature task interfaces. They are not a fresh population holdout.
- The end-to-end result contributes one independent measurement case. Two repetitions describe
  model variability and do not increase the case count.
- Relationship micro-benchmark evidence comes from one system family.
- Reasoning settings are frozen provider configurations, not a common compute budget.
- The experiment measures the complete agent-facing Semantic Graph interface, including semantic
  metadata, usage guidance, recovery guidance, and coverage information. It does not isolate a
  pure data-representation effect.
