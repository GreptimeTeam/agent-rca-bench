# Public measurement artifacts

Files under `artifacts/measurement/` are sanitized inputs to the published
report. They exclude source telemetry, source archives, labels, provider
payloads, reasoning text, credentials, endpoints, and machine-local paths.

The active report uses:

- `agent-rca-v34-micro.json`: 128 fixed-cohort Discovery and Graph cells.
- `agent-rca-v34-transfer.json`: 336 end-to-end RCA cells.
- `agent-rca-v34.json`: deterministic combined report data.
- `agent-rca-v34.html`: self-contained bilingual report.
- `agent-rca-v34-SHA256SUMS`: hashes for the public artifacts and narrative
  reports, listed as paths relative to the repository root.

Regenerate the combined JSON and HTML with:

```bash
output_dir=$(mktemp -d)

uv run agent-rca formal-suite-report \
  --micro-artifact artifacts/measurement/agent-rca-v34-micro.json \
  --transfer-artifact artifacts/measurement/agent-rca-v34-transfer.json \
  --suite-protocol fixtures/reference/agent-rca-v34-four-model-suite.json \
  --transfer-protocol fixtures/reference/transfer-v34-protocol.json \
  --output-json "$output_dir/agent-rca.json" \
  --output-html "$output_dir/agent-rca.html"

cmp artifacts/measurement/agent-rca-v34.json \
  "$output_dir/agent-rca.json"
cmp artifacts/measurement/agent-rca-v34.html \
  "$output_dir/agent-rca.html"
```

Verify the published hashes from the repository root:

```bash
shasum -a 256 -c artifacts/measurement/agent-rca-v34-SHA256SUMS
```

The exporter validates source artifact hashes before generating either output.
The tagged source deterministically revalidates scoring, aggregates, and report
integrity without invoking a model provider.

The repository's Apache-2.0 license covers the benchmark code, artifact schema,
and derived report. It does not relicense upstream telemetry. OpenRCA2's dataset
card declares Apache-2.0, while its paper declares CC-BY-SA-4.0 and does not link
the downloaded artifact as the promised archival release. Reproduction obtains
the pinned source under its upstream terms; this repository does not
redistribute it.

Artifacts from earlier protocol cycles are not published. Retrieve them from
their historical Git revision; the active runtime carries no compatibility
loaders for internal protocol cycles.
