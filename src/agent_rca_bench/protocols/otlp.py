from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    InstrumentationScope,
    KeyValue,
    KeyValueList,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AGGREGATION_TEMPORALITY_UNSPECIFIED,
    Gauge,
    Histogram,
    HistogramDataPoint,
    Metric,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
    Sum,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import (
    ResourceSpans,
    ScopeSpans,
    Status,
)
from opentelemetry.proto.trace.v1.trace_pb2 import (
    Span as ProtoSpan,
)

from agent_rca_bench.greptimedb.client import GreptimeClient


@dataclass(frozen=True)
class TraceSpan:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: int
    start_time_unix_nano: int
    end_time_unix_nano: int
    service_name: str | None
    status_code: int = Status.STATUS_CODE_UNSET
    status_message: str = ""
    resource_attributes: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)
    events: tuple[Mapping[str, object], ...] = ()
    scope_name: str = "telemetry-replay"
    scope_version: str = "0.1.0"


@dataclass(frozen=True)
class NumberMetricPoint:
    name: str
    time_unix_nano: int
    value: float
    service_name: str | None
    resource_attributes: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HistogramMetricPoint:
    name: str
    time_unix_nano: int
    count: int
    sum: float | None
    min: float | None
    max: float | None
    service_name: str | None
    resource_attributes: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)


@dataclass
class IdStats:
    remapped_trace_ids: int = 0
    remapped_span_ids: int = 0


class OtlpMetricWriter:
    def __init__(
        self,
        client: GreptimeClient,
        database: str,
        *,
        scope_name: str = "telemetry-replay",
        scope_version: str = "0.1.0",
    ) -> None:
        self.client = client
        self.database = database
        self.scope_name = scope_name
        self.scope_version = scope_version
        self.rejected_data_points = 0

    def write_gauges(
        self,
        points: Iterable[NumberMetricPoint],
        *,
        batch_size: int = 5_000,
    ) -> int:
        return self._write(points, kind="gauge", batch_size=batch_size)

    def write_sums(
        self,
        points: Iterable[NumberMetricPoint],
        *,
        batch_size: int = 5_000,
    ) -> int:
        return self._write(points, kind="sum", batch_size=batch_size)

    def write_histograms(
        self,
        points: Iterable[HistogramMetricPoint],
        *,
        batch_size: int = 5_000,
    ) -> int:
        return self._write(points, kind="histogram", batch_size=batch_size)

    def _write(
        self,
        points: Iterable[NumberMetricPoint | HistogramMetricPoint],
        *,
        kind: str,
        batch_size: int,
    ) -> int:
        batch: list[NumberMetricPoint | HistogramMetricPoint] = []
        written = 0
        for point in points:
            batch.append(point)
            if len(batch) >= batch_size:
                self._write_batch(batch, kind)
                written += len(batch)
                batch.clear()
        if batch:
            self._write_batch(batch, kind)
            written += len(batch)
        return written

    def _write_batch(
        self,
        points: list[NumberMetricPoint | HistogramMetricPoint],
        kind: str,
    ) -> None:
        grouped: dict[
            str,
            tuple[Mapping[str, object], dict[str, list[NumberMetricPoint | HistogramMetricPoint]]],
        ] = {}
        for point in points:
            resources = dict(point.resource_attributes)
            if point.service_name is not None:
                resources = {"service.name": point.service_name, **resources}
            resource_key = json.dumps(
                resources,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if resource_key not in grouped:
                grouped[resource_key] = (resources, {})
            grouped[resource_key][1].setdefault(point.name, []).append(point)

        resource_metrics = []
        for resources, points_by_name in grouped.values():
            metrics = [
                self._metric(name, metric_points, kind)
                for name, metric_points in points_by_name.items()
            ]
            resource_metrics.append(
                ResourceMetrics(
                    resource=Resource(attributes=_key_values(resources)),
                    scope_metrics=[
                        ScopeMetrics(
                            scope=InstrumentationScope(
                                name=self.scope_name,
                                version=self.scope_version,
                            ),
                            metrics=metrics,
                        )
                    ],
                )
            )

        request = ExportMetricsServiceRequest(resource_metrics=resource_metrics)
        response = self.client.post_bytes(
            "/v1/otlp/v1/metrics",
            request.SerializeToString(),
            headers={
                "Content-Type": "application/x-protobuf",
                "x-greptime-db-name": self.database,
                "x-greptime-otlp-metric-promote-all-resource-attrs": "true",
            },
        )
        if response.content:
            decoded = ExportMetricsServiceResponse.FromString(response.content)
            self.rejected_data_points += decoded.partial_success.rejected_data_points

    @staticmethod
    def _metric(
        name: str,
        points: list[NumberMetricPoint | HistogramMetricPoint],
        kind: str,
    ) -> Metric:
        if kind == "histogram":
            histograms = []
            for point in points:
                if not isinstance(point, HistogramMetricPoint):
                    raise TypeError("histogram batch contains a number data point")
                histograms.append(
                    HistogramDataPoint(
                        attributes=_key_values(point.attributes),
                        time_unix_nano=point.time_unix_nano,
                        count=point.count,
                        sum=point.sum,
                        min=point.min,
                        max=point.max,
                        bucket_counts=[point.count],
                    )
                )
            return Metric(
                name=name,
                histogram=Histogram(
                    data_points=histograms,
                    aggregation_temporality=AGGREGATION_TEMPORALITY_UNSPECIFIED,
                ),
            )

        numbers = []
        for point in points:
            if not isinstance(point, NumberMetricPoint):
                raise TypeError("number batch contains a histogram data point")
            numbers.append(
                NumberDataPoint(
                    attributes=_key_values(point.attributes),
                    time_unix_nano=point.time_unix_nano,
                    as_double=point.value,
                )
            )
        if kind == "gauge":
            return Metric(name=name, gauge=Gauge(data_points=numbers))
        if kind == "sum":
            return Metric(
                name=name,
                sum=Sum(
                    data_points=numbers,
                    aggregation_temporality=AGGREGATION_TEMPORALITY_UNSPECIFIED,
                    is_monotonic=False,
                ),
            )
        raise ValueError(f"unsupported OTLP metric kind: {kind}")


class OtlpTraceWriter:
    def __init__(self, client: GreptimeClient, database: str, table: str = "traces") -> None:
        self.client = client
        self.database = database
        self.table = table
        self.stats = IdStats()
        self.rejected_spans = 0
        self._trace_ids: dict[str, bytes] = {}
        self._span_ids: dict[str, bytes] = {}

    def write(self, spans: Iterable[TraceSpan], *, batch_size: int = 5_000) -> int:
        batch: list[TraceSpan] = []
        written = 0
        for span in spans:
            batch.append(span)
            if len(batch) >= batch_size:
                self._write_batch(batch)
                written += len(batch)
                batch.clear()
        if batch:
            self._write_batch(batch)
            written += len(batch)
        return written

    def _write_batch(self, records: list[TraceSpan]) -> None:
        grouped: dict[tuple[str, str, str], tuple[Mapping[str, object], list[ProtoSpan]]] = {}
        for record in records:
            resources = dict(record.resource_attributes)
            if record.service_name is not None:
                resources = {"service.name": record.service_name, **resources}
            key = (
                json.dumps(resources, sort_keys=True, separators=(",", ":"), default=str),
                record.scope_name,
                record.scope_version,
            )
            if key not in grouped:
                grouped[key] = (resources, [])
            grouped[key][1].append(self._span(record))

        resource_spans = []
        for (_, scope_name, scope_version), (attributes, spans) in grouped.items():
            resource = Resource(attributes=_key_values(attributes))
            scope_spans = ScopeSpans(
                scope=InstrumentationScope(name=scope_name, version=scope_version),
                spans=spans,
            )
            resource_spans.append(ResourceSpans(resource=resource, scope_spans=[scope_spans]))

        request = ExportTraceServiceRequest(resource_spans=resource_spans)
        response = self.client.post_bytes(
            "/v1/otlp/v1/traces",
            request.SerializeToString(),
            headers={
                "Content-Type": "application/x-protobuf",
                "x-greptime-db-name": self.database,
                "x-greptime-pipeline-name": "greptime_trace_v1",
                "x-greptime-trace-table-name": self.table,
            },
        )
        if response.content:
            decoded = ExportTraceServiceResponse.FromString(response.content)
            self.rejected_spans += decoded.partial_success.rejected_spans

    def _span(self, record: TraceSpan) -> ProtoSpan:
        status_code = record.status_code
        if status_code not in (0, 1, 2):
            status_code = Status.STATUS_CODE_ERROR if status_code else Status.STATUS_CODE_UNSET
        events = []
        for event in record.events:
            attributes = event.get("attributes")
            events.append(
                ProtoSpan.Event(
                    time_unix_nano=int(event.get("timestamp") or 0),
                    name=str(event.get("name") or "event"),
                    attributes=_key_values(attributes if isinstance(attributes, Mapping) else {}),
                )
            )
        return ProtoSpan(
            trace_id=self._id(record.trace_id, 16, True),
            span_id=self._id(record.span_id, 8, False),
            parent_span_id=(
                self._id(record.parent_span_id, 8, False) if record.parent_span_id else b""
            ),
            name=record.name,
            kind=record.kind,
            start_time_unix_nano=record.start_time_unix_nano,
            end_time_unix_nano=max(record.end_time_unix_nano, record.start_time_unix_nano),
            attributes=_key_values(record.attributes),
            events=events,
            status=Status(code=status_code, message=record.status_message),
        )

    def _id(self, value: str, size: int, trace: bool) -> bytes:
        cache = self._trace_ids if trace else self._span_ids
        if value in cache:
            return cache[value]
        try:
            decoded = bytes.fromhex(value)
        except ValueError:
            decoded = b""
        if len(decoded) != size or not any(decoded):
            decoded = hashlib.blake2b(
                f"{'trace' if trace else 'span'}:{value}".encode(),
                digest_size=size,
            ).digest()
            if trace:
                self.stats.remapped_trace_ids += 1
            else:
                self.stats.remapped_span_ids += 1
        cache[value] = decoded
        return decoded


def _key_values(values: Mapping[str, object]) -> list[KeyValue]:
    return [KeyValue(key=str(key), value=_any_value(value)) for key, value in values.items()]


def _any_value(value: object) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, bytes):
        return AnyValue(bytes_value=value)
    if isinstance(value, Mapping):
        return AnyValue(kvlist_value=KeyValueList(values=_key_values(value)))
    if isinstance(value, (list, tuple)):
        return AnyValue(array_value=ArrayValue(values=[_any_value(item) for item in value]))
    if value is None:
        return AnyValue(string_value="")
    return AnyValue(string_value=str(value))
