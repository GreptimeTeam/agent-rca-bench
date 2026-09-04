from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)

from agent_rca_bench.greptimedb.client import GreptimeClient
from agent_rca_bench.protocols.prometheus import prometheus_label_name


class SplitBackendError(RuntimeError):
    pass


class ProtocolHttpClient:
    def __init__(self, endpoint: str, *, timeout: float = 120.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.http = httpx.Client(timeout=timeout, trust_env=False)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> ProtocolHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def post_bytes(
        self,
        path: str,
        body: bytes,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> httpx.Response:
        response = self.http.post(
            f"{self.endpoint}{path}",
            params=params,
            headers=headers,
            content=body,
            timeout=timeout,
        )
        if response.is_error:
            raise SplitBackendError(
                f"ingestion failed for {path} ({response.status_code}): {response.text[:2000]}"
            )
        return response


@dataclass
class _TargetStats:
    batches: int = 0
    bytes_sent: int = 0
    rejected_items: int = 0
    payload_sha256: list[str] = field(default_factory=list)

    def record(self, body: bytes, rejected_items: int) -> None:
        self.batches += 1
        self.bytes_sent += len(body)
        self.rejected_items += rejected_items
        self.payload_sha256.append(hashlib.sha256(body).hexdigest())


class FanoutIngestClient:
    def __init__(
        self,
        greptime: GreptimeClient,
        *,
        prometheus: ProtocolHttpClient,
        loki: ProtocolHttpClient,
        tempo: ProtocolHttpClient,
    ) -> None:
        self.greptime = greptime
        self.prometheus = prometheus
        self.loki = loki
        self.tempo = tempo
        self._stats = {
            "greptimedb": {signal: _TargetStats() for signal in ("metrics", "logs", "traces")},
            "split": {signal: _TargetStats() for signal in ("metrics", "logs", "traces")},
        }

    def create_database(self, database: str) -> None:
        self.greptime.create_database(database)

    def post_bytes(
        self,
        path: str,
        body: bytes,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> httpx.Response:
        # Resolve the target before writing anything: an unsupported path must
        # not leave a half-written case behind.
        target = self._split_target(path)
        greptime_response = self.greptime.post_bytes(
            path,
            body,
            headers=headers,
            params=params,
            timeout=timeout,
        )
        if target is None:
            # Metrics reach Prometheus through `split_ingest`, not this tee:
            # Prometheus rejects the source's unspecified-temporality sums and
            # histograms, so it cannot take the same OTLP bytes.
            self._stats["greptimedb"]["metrics"].record(
                body,
                _rejected_items("metrics", greptime_response.content),
            )
            return greptime_response
        signal, split_client, split_path = target
        split_body = _loki_body(body, headers) if signal == "logs" else body
        split_response = split_client.post_bytes(
            split_path,
            split_body,
            headers={
                key: value
                for key, value in headers.items()
                if not key.lower().startswith("x-greptime-")
            },
            timeout=timeout,
        )
        self._stats["greptimedb"][signal].record(
            body,
            _rejected_items(signal, greptime_response.content),
        )
        self._stats["split"][signal].record(
            split_body,
            _rejected_items(signal, split_response.content),
        )
        return greptime_response

    def audit(self) -> dict[str, object]:
        targets: dict[str, object] = {}
        for target, signals in self._stats.items():
            targets[target] = {
                signal: {
                    "batches": stats.batches,
                    "bytes_sent": stats.bytes_sent,
                    "rejected_items": stats.rejected_items,
                    "payload_sha256": list(stats.payload_sha256),
                }
                for signal, stats in signals.items()
            }
        # Only traces use the same wire protocol on both sides. Stored-content
        # checks live in `split_audit`.
        byte_identical = ("traces",)
        return {
            "targets": targets,
            "byte_identical_signals": list(byte_identical),
            "protocol_mapping": {
                "metrics": {
                    "greptimedb": "otlp",
                    "split": "prometheus-remote-write-0.1",
                    "reason": (
                        "Prometheus rejects the source's unspecified-temporality sums "
                        "and histograms"
                    ),
                },
                "logs": {
                    "greptimedb": "loki-push with x-greptime-log-table-name",
                    "split": (
                        f"loki-push with folded label names, a {LOG_TABLE_LABEL} label, "
                        f"{'/'.join(LOKI_STRUCTURED_METADATA_FIELDS)} as structured metadata, "
                        "and empty label values omitted"
                    ),
                    "reason": (
                        "Loki accepts only [a-zA-Z_][a-zA-Z0-9_]* label names, does not read "
                        "the table header, indexes every distinct label set as a stream, "
                        "treats an empty label value as absent, and collapses entries that "
                        "are identical in labels, timestamp and line"
                    ),
                },
            },
        }

    def _split_target(self, path: str) -> tuple[str, ProtocolHttpClient, str] | None:
        if path in {"/v1/prometheus/write", "/v1/otlp/v1/metrics"}:
            return None
        if path == "/v1/loki/api/v1/push":
            return "logs", self.loki, "/loki/api/v1/push"
        if path == "/v1/otlp/v1/traces":
            return "traces", self.tempo, "/v1/traces"
        raise SplitBackendError(f"unsupported fanout ingestion path: {path}")


LOG_TABLE_LABEL = "log_table"

# Per-entry correlation identifiers, which Loki holds as structured metadata
# rather than as stream labels. GreptimeDB keeps them as ordinary columns; making
# them stream labels instead would give one stream per log line and a query would
# then return only the first few hundred streams, so the split arm would appear
# to have lost data it in fact holds.
LOKI_STRUCTURED_METADATA_FIELDS = ("trace_id", "span_id")


def _loki_body(body: bytes, headers: Mapping[str, str]) -> bytes:
    """Rewrites a Loki push so a stock Loki can hold what GreptimeDB holds.

    Two source properties do not survive a byte-identical replay. Loki accepts
    only `[a-zA-Z_][a-zA-Z0-9_]*` label names, so the dotted OpenRCA2 attributes
    have to be folded; and GreptimeDB routes `logs`, `events` and `alerts` into
    separate tables through `x-greptime-log-table-name`, a header Loki does not
    read, so without a carrier label the three streams would collapse into one
    undifferentiated set and the split arm would hold strictly less than the
    GreptimeDB arm. The table name therefore travels as a stream label, which is
    Loki's own equivalent of a table.
    """
    table = next(
        (
            value
            for key, value in headers.items()
            if key.lower() == "x-greptime-log-table-name" and value
        ),
        "logs",
    )
    payload = json.loads(body)
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise SplitBackendError("Loki push payload has no streams")
    rewritten = []
    for stream in streams:
        labels = stream.get("stream") if isinstance(stream, dict) else None
        if not isinstance(labels, dict):
            raise SplitBackendError("Loki push stream has no labels")
        normalized = _normalized_stream(labels)
        metadata = {
            field: normalized.pop(field)
            for field in LOKI_STRUCTURED_METADATA_FIELDS
            if field in normalized
        }
        values = stream.get("values")
        if not isinstance(values, list):
            raise SplitBackendError("Loki push stream has no values")
        rewritten.append(
            {
                "stream": {**normalized, LOG_TABLE_LABEL: table},
                "values": [
                    [*entry[:2], dict(metadata)] if metadata else list(entry[:2])
                    for entry in values
                ],
            }
        )
    return json.dumps({**payload, "streams": rewritten}, separators=(",", ":")).encode()


def _normalized_stream(labels: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    origins: dict[str, str] = {}
    for key, value in labels.items():
        name = prometheus_label_name(str(key))
        if name == LOG_TABLE_LABEL:
            raise SplitBackendError(f"source log label collides with {LOG_TABLE_LABEL}")
        previous = origins.get(name)
        if previous is not None and previous != key:
            raise SplitBackendError(
                f"log label names collide after normalization: {previous}, {key}"
            )
        origins[name] = str(key)
        # Loki, like Prometheus, treats an empty label value as the label not
        # being there. Sending it and letting the store drop it would leave the
        # difference undeclared; dropping it here states the mapping.
        if str(value) == "":
            continue
        normalized[name] = str(value)
    return normalized


def _rejected_items(signal: str, content: bytes) -> int:
    if not content or signal == "logs":
        return 0
    try:
        if signal == "metrics":
            response = ExportMetricsServiceResponse.FromString(content)
            return int(response.partial_success.rejected_data_points)
        response = ExportTraceServiceResponse.FromString(content)
        return int(response.partial_success.rejected_spans)
    except Exception as error:
        raise SplitBackendError(f"invalid {signal} ingestion response") from error


def response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise SplitBackendError("backend response is not JSON") from error
    if not isinstance(payload, dict):
        raise SplitBackendError("backend response must be a JSON object")
    return payload
