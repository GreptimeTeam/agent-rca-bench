# Release artifacts

Files under `artifacts/` contain sanitized benchmark results. They exclude provider payloads,
source telemetry rows, label files, source archives, credentials, and local machine paths.

The repository's Apache-2.0 license covers the exporter, artifact schema, and benchmark metadata.
It does not replace the source dataset license. Aegis aggregate facts cite the source dataset
record's CC-BY-4.0 declaration. The reviewer artifact's root Apache-2.0 file does not explicitly
cover `reproduction/data`, so reproduce the aggregates through the pinned downloader instead of
redistributing source telemetry.

Development artifacts preserve negative or prompt-informing pilot results. Measurement artifacts
require a complete frozen formal schedule and deterministic rescoring. Multi-model measurement
summaries keep treatment comparisons within each model; they do not pool correctness across
providers or models.
