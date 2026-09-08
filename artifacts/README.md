# Public measurement artifacts

Files under `artifacts/measurement/` are sanitized inputs to the published
report. They exclude source telemetry, source archives, labels, provider
payloads, reasoning text, credentials, endpoints, and machine-local paths.

The six-model report combines three end-to-end measurement sources:

| Source | Artifact | Cells |
| --- | --- | ---: |
| Original four-model cohort | `agent-rca-v34-transfer.json` | 336 |
| Gemini and Qwen extension | `agent-rca-v34-two-model-extension-transfer.json` | 168 |
| Complete Split rerun | `agent-rca-v34-split-rerun.json` | 168 replacements |

Composition replaces the 168 Split cells and retains 336 Raw/Graph cells. The
result contains 504 end-to-end cells, not the sum of the three source counts.
The two micro artifacts, `agent-rca-v34-micro.json` and
`agent-rca-v34-two-model-extension-micro.json`, supply another 128 and 64 cells.
The combined report contains 696 cells.

The derived outputs are:

- `agent-rca-v34-composed-transfer.json` and
  `agent-rca-v34-two-model-extension-composed-transfer.json`: composed end-to-end results.
- `agent-rca-v34.json` / `.html` and `agent-rca-v34-two-model-extension.json` / `.html`:
  the two cohort reports.
- `agent-rca-v34-six-model.json` / `.html`: the unified six-model report.
- Each report's `*-publication.json`: fixed publication timestamps for reproduction.
- Each report's `*-SHA256SUMS`: checksums, with paths relative to the repository root.

Follow the [report reproduction procedure](../README.md#reproduce-the-published-report)
to compose the transfer files and regenerate all three JSON/HTML reports. It
checks eight outputs byte for byte without invoking a model provider.

Verify all published checksums from the repository root:

```bash
shasum -a 256 -c artifacts/measurement/agent-rca-v34-SHA256SUMS
shasum -a 256 -c artifacts/measurement/agent-rca-v34-two-model-extension-SHA256SUMS
shasum -a 256 -c artifacts/measurement/agent-rca-v34-six-model-SHA256SUMS
```

The composition and report commands validate source bindings and artifact
integrity before generating their outputs.

The repository's Apache-2.0 license covers the benchmark code, artifact schema,
and derived report. It does not relicense upstream telemetry. OpenRCA2's dataset
card declares Apache-2.0, while its paper declares CC-BY-SA-4.0 and does not link
the downloaded artifact as the promised archival release. Reproduction obtains
the pinned source under its upstream terms; this repository does not
redistribute it.

Artifacts from earlier protocol cycles are not published. Retrieve them from
their historical Git revision; the active runtime carries no compatibility
loaders for internal protocol cycles.
