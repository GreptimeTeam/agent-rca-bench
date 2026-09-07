---
name: run-rca-bench
description: Reproduce Agent RCA Bench reports from published artifacts, run source and storage preflights, or execute and resume model measurements in this repository. Use for benchmark setup, report reproduction, and benchmark execution; distinguish provider-free reproduction from paid replication.
---

# Run and reproduce Agent RCA Bench

Work from the repository root. Read `AGENTS.md` and, if present,
`.local/AGENTS.md`. Use the checked-out revision's documentation and CLI rather
than commands or model settings remembered from another release.

## Choose the workflow

- **Reproduce a report:** regenerate JSON and HTML from sanitized artifacts.
  No model credentials, source telemetry, Docker, or GreptimeDB build are needed.
  Use this path when the request is to verify the published results.
- **Audit the measurement environment:** download and replay source telemetry
  and validate the stores without model calls. This requires source access,
  the pinned GreptimeDB build, and Docker where the protocol uses the split stack.
- **Run or resume measurements:** execute pending model cells after preflight
  and explicit approval for the paid invocation. New model responses are a
  replication, not a byte-for-byte reproduction of the published responses.

For an ambiguous request to “run the benchmark,” explain these choices before
incurring costs. Do not turn a report-reproduction request into a model run.

## Reproduce the published report

Read [the reproduction procedure](../../../README.md#reproduce-the-published-report)
before executing it. Python 3.11 and `uv` are required; dependency installation
may need network access, but report generation does not call providers.

1. Check `git status --short` and identify the requested release or checkout.
   Do not switch revisions over local edits. Use a separate checkout if needed.
2. Install the locked dependencies with `uv sync --extra dev --frozen`.
3. Verify all applicable `SHA256SUMS` manifests under `artifacts/measurement/`.
   A mismatch is a verification failure; do not rewrite the manifest to clear it.
4. Run the README's `formal-suite-report` commands into a fresh directory from
   `mktemp -d`. Generate both input reports before calling `formal-report-merge`
   with those regenerated JSON files and their bound transfer artifacts.
5. Compare every regenerated JSON and HTML with its published counterpart using
   `cmp`. For the six-model v34 report, all six comparisons must exit with 0.
   Preserve publication metadata, including both UTC timestamps, and output
   basenames: changing either can change the rendered bytes.
6. Report the comparison results and the generated six-model HTML path. Open
   the self-contained HTML directly, or serve only the output directory on
   loopback if a browser preview is requested. Do not serve the repository root,
   which can contain private execution records.

For a mismatch, identify the first differing artifact and distinguish data,
protocol, renderer, and publication-metadata differences. Do not overwrite the
reference artifacts, change frozen statistics, or invoke a provider to repair a
reproduction failure. Unit tests alone do not prove byte-for-byte reproduction.

## Prepare a source audit or paid replication

Read [RUNNING.md](../../../RUNNING.md) and
[DATASETS.md](../../../DATASETS.md). Before a paid run, also read
[SCORING.md](../../../SCORING.md) and the selected protocol fixtures.

1. Establish the requested models, benchmark layers, fresh-run or resume scope,
   and output locations. Keep private data in ignored directories such as
   `.data/`, `.reports/`, and `.instances/`. Preserve the published artifacts.
2. Select matching micro and transfer protocols. Pass `--protocol` explicitly
   to audit, run, and export commands; CLI defaults do not select all six models.
   The v34 fixture pairs under `fixtures/reference/` are:

   | Models | Micro suite | End-to-end protocol |
   | --- | --- | --- |
   | Four-model suite | `agent-rca-v34-four-model-suite.json` | `transfer-v34-protocol.json` |
   | Gemini and Qwen | `agent-rca-v34-two-model-extension-suite.json` | `transfer-v34-two-model-extension-protocol.json` |

   Inspect the fixtures for the actual model roster and bindings. The unified
   six-model report is a merge, not a separate execution protocol. Run only the
   requested missing measurements; reuse compatible completed records. A model
   substitution or change to a frozen configuration requires a new protocol,
   not an edit to the published fixture.
3. Locate the user's GreptimeDB checkout, verify the protocol revision and
   release build, and run `uv run agent-rca doctor --greptimedb-repo PATH` with
   the actual path. Never use a maintainer's machine-local default. Keep each
   case's database and split stack isolated; do not touch unrelated services.
4. Check the selected commands with `uv run agent-rca COMMAND --help`. Run the
   relevant provider-free preflights with the selected protocol and explicit
   GreptimeDB path:

   - Micro: `formal-suite-micro-preflight` writes the private report specified
     by `--output`; supply fresh `--run-root` and `--source-audits-dir` paths.
   - End-to-end: run `transfer-selection-audit`, then `transfer-preflight` with
     fresh `--run-root` and `--output` paths.

   The preflight output becomes the corresponding paid runner's `--report`.
   It is not a separate report that the runner discovers automatically. Check
   source and storage audit results before proceeding. Stop on environment or
   source-fidelity failures; do not classify them as model failures.

## Execute and resume paid measurements

Before each paid invocation, present the exact command, pending cell count,
model configurations, concurrency, and expected cost with its uncertainty.
Obtain explicit approval before adding `--confirm-paid-api` and executing it.
Do not treat a credential check or a smoke test as free if it calls a provider.

- Follow RUNNING.md for environment-variable or macOS Keychain credentials.
  Never print keys or place them in command arguments, public files, or logs.
  Keep tenant endpoints local. Verify the selected provider's transport and
  proxy behavior in `src/agent_rca_bench/agent.py`; do not apply one provider's
  proxy configuration to every provider.
- Check model availability, account quotas, and rates applicable to the planned
  run. Freeze the required pricing snapshot before execution. Keep native
  currencies and dated exchange rates. Correcting a rate the snapshot missed
  is a fix if the rate was in force during the run; retain the correction note
  and verification date wherever the cost appears. Do not apply a rate that
  only took effect after the run. Keep token counts unchanged in either case.
  Separate input, cache read/write, output, and reasoning according to the
  provider contract. Missing cache detail is not zero, and an ordinary-token
  estimate must remain separate from confirmed cache-aware cost.
- Micro execution uses `formal-suite-micro-run` with the preflight report,
  `--live-audits-dir`, and a fresh `--run-root`. On continuation, keep the same
  report but use fresh run-root and live-audit directories.
- End-to-end execution uses `transfer-run` with the preflight report and a
  dedicated execution `--run-root`. Resume with the same report and execution
  root so the runner can recover journaled results and skip completed cells.
  Do not launch a second runner against that root or remove its lock/journal.
- Both runners support `--max-new-runs` to bound an approved invocation. This
  limits cell count, not dollars. A concurrency change requires approval and
  must remain within the frozen per-provider limit. A higher limit requires a
  new protocol. Historical deviations can be exported but not resumed as
  protocol-conforming runs.
- Persist runner failures and budget exhaustion. Do not retry incorrect
  diagnoses to improve the score. If billing, authentication, quota exhaustion,
  or an unresolved active journal cell prevents safe progress, stop and report
  the affected cells. Do not loop paid invocations or delete records to force
  retries. Any recovery needs a defined scope and preserved prior records.

## Export and verify a measurement

Use `formal-suite-micro-export` and `transfer-export` with `--run-report`, the
matching `--protocol`, and fresh `--output` paths. Generate JSON and HTML with
`formal-suite-report`; use `formal-report-merge` only for compatible,
non-overlapping model rosters and their bound artifacts.

Check schedule completion separately from diagnosis success. Preserve cases as
the independent units, eligibility rules, and each frozen Holm family. Do not
pool the v34 four-model and two-model families into a new six-model test.
Keep split-stack row counts as N/A and unknown costs distinct from zero.

Verify sanitized exports contain no raw telemetry, provider payloads, reasoning
text, credentials, tenant endpoints, labels, or machine-local paths. For a new
measurement, record its actual measurement-update and report-generation times in UTC;
do not reuse historical publication timestamps. Regenerate from the exported
artifacts and compare the outputs before delivery.

Return completed and pending cell counts, failures requiring action, cost and
token accounting limits, artifact paths, and checks actually performed. Report
reproduction or benchmark execution does not authorize a commit, push, or site
deployment.
