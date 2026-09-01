# Public measurement artifacts

Files under `artifacts/measurement/` are sanitized inputs to the published
report. They exclude source telemetry, source archives, labels, provider
payloads, reasoning text, credentials, endpoints, and machine-local paths.

The active report uses:

- `semantic-rca-v32-micro.json`: 160 fixed-cohort Discovery and Graph cells.
- `semantic-rca-v32-transfer.json`: 200 fresh end-to-end RCA cells.
- `semantic-rca-v32.json`: deterministic combined report data.
- `semantic-rca-v32.html`: self-contained bilingual report.
- `semantic-rca-v32-SHA256SUMS`: hashes for the public artifacts and narrative reports.

Regenerate the combined JSON and HTML with:

```bash
output_dir=$(mktemp -d)

uv run semantic-rca formal-suite-report \
  --micro-artifact artifacts/measurement/semantic-rca-v32-micro.json \
  --transfer-artifact artifacts/measurement/semantic-rca-v32-transfer.json \
  --output-json "$output_dir/semantic-rca.json" \
  --output-html "$output_dir/semantic-rca.html"

cmp artifacts/measurement/semantic-rca-v32.json \
  "$output_dir/semantic-rca.json"
cmp artifacts/measurement/semantic-rca-v32.html \
  "$output_dir/semantic-rca.html"
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

Historical development artifacts belong under `.local/` or at their historical
Git revision. The active runtime does not include compatibility loaders for
internal protocol cycles.
