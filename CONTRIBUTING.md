# Contributing

Thanks for looking at Agent RCA Bench. This file covers what a change has to
satisfy before it can land.

Read [AGENTS.md](AGENTS.md) first. It is the working contract for this
repository: what the measurement compares, which fields are frozen, and which
edits would invalidate a published result. It applies to human and agent
contributors alike.

## Development setup

Python 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/GreptimeTeam/agent-rca-bench.git
cd agent-rca-bench
uv sync --extra dev --frozen
uv run pytest -q
```

Nothing in the provider-free path needs Docker, a GreptimeDB build, or an API
key. Rescoring the published artifacts and regenerating the report run offline.

## Before opening a pull request

Run the local checks:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
uv build
shasum -a 256 -c artifacts/measurement/agent-rca-v34-SHA256SUMS
shasum -a 256 -c artifacts/measurement/agent-rca-v34-two-model-extension-SHA256SUMS
shasum -a 256 -c artifacts/measurement/agent-rca-v34-six-model-SHA256SUMS
```

If your change touches report generation, confirm the published artifacts still
reproduce byte for byte using the composition and rendering commands in
[README.md](README.md#reproduce-the-published-report). A diff there means either
the change is wrong or the artifacts need regenerating in the same commit; the
two must never disagree on `main`. CI also validates `CITATION.cff` and rejects
machine-local paths in published HTML.

## What a change has to hold to

The benchmark's value is that its results can be checked. These constraints
protect that, and a change that breaks one will be sent back even if it is
otherwise an improvement.

- **Do not edit measurement data.** Artifacts under `artifacts/measurement/` are
  regenerated from the exporters, never hand-edited. Source telemetry,
  identifiers, timestamps, and source defects are preserved as the upstream
  publishes them.
- **Do not relax a scorer to change a result.** If a scorer is wrong, fix the
  rule and say which measured outcomes move. A fix that happens to improve the
  sponsor's arm needs the same evidence as one that worsens it.
- **Do not select cases after seeing trajectories.** Case selection is frozen
  before any model runs, and the eligible population, exclusions, and ranking
  are recorded.
- **A protocol change needs a new protocol identifier.** Changing the prompt,
  the treatment surface, the scorer, selection, the runner contract, or a
  frozen metric produces a new version rather than mutating the published one.
- **Published prose tracks the numbers.** HTML text that depends on measured values
  is built in `formal_report_view.py` and tested there, not written in the renderer.
  Both Markdown reports must agree with the artifacts; generate their correction
  notes with `uv run python -m agent_rca_bench.split_rerun_release sync-correction`
  and its `--report`, `--english`, and `--chinese` paths.

## Tests

Add a test when a change protects a non-trivial behaviour: a business rule, a
boundary, an error path, a compatibility contract, or a regression you can
describe. Say in the test's docstring what would break without it.

Do not add tests for getters, direct field mapping, or anything the type system
already guarantees, and do not assert that a value the test just wrote is the
value it reads back.

## Commits and pull requests

- Signed-off Conventional Commit subjects: `git commit -s`.
- The body says what was wrong, what changed, and what was traded off. The
  repository history is the design record.
- No generated-tool attribution or co-author trailers.
- Keep the diff reviewable. Unrelated cleanups belong in their own commit.

## Reporting a problem with a published result

Open an issue with the artifact and the field. If you can, include the command
you ran and its output. A reproducible disagreement with a published number is
the most useful issue this project can receive, and it will be investigated
before feature work.

Security-relevant reports go to [SECURITY.md](SECURITY.md) instead.
