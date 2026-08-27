import json

import snappy
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from semantic_rca_bench.protocols.loki import LogRecord, _to_nanoseconds, write_logs
from semantic_rca_bench.protocols.otlp import OtlpTraceWriter, TraceSpan
from semantic_rca_bench.protocols.prometheus import (
    encode_write_request,
    prometheus_metric_name,
    write_series_batch,
)


class _Response:
    content = b""


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post_bytes(
        self,
        path: str,
        body: bytes,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> _Response:
        self.calls.append({"path": path, "body": body, "headers": headers, "params": params})
        return _Response()


def test_prometheus_metric_name() -> None:
    assert prometheus_metric_name("checkoutservice_latency-90") == ("checkoutservice_latency_90")
    assert prometheus_metric_name("9bad") == "metric_9bad"


def test_remote_write_payload_is_non_empty() -> None:
    payload = encode_write_request(
        "requests_total",
        [(1_700_000_000_000, 2.0)],
        {"service_name": "checkout"},
    )
    assert payload.startswith(b"\x0a")
    assert b"requests_total" in payload
    assert b"checkout" in payload


def test_loki_timestamp_units() -> None:
    assert _to_nanoseconds(1_700_000_000) == 1_700_000_000_000_000_000
    assert _to_nanoseconds(1_700_000_000_000) == 1_700_000_000_000_000_000
    assert _to_nanoseconds(1_700_000_000_000_000) == 1_700_000_000_000_000_000
    assert _to_nanoseconds(1_700_000_000_000_000_000) == 1_700_000_000_000_000_000


def test_otlp_replay_preserves_native_graph_and_resource_fields() -> None:
    client = _Client()
    trace_id = "01" * 16
    span_id = "02" * 8
    parent_span_id = "03" * 8
    span = TraceSpan(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name="Payment/Charge",
        kind=3,
        start_time_unix_nano=1_700_000_000_123_456_789,
        end_time_unix_nano=1_700_000_000_223_456_789,
        service_name="payment",
        status_code=2,
        status_message="invalid token",
        resource_attributes={
            "service.name": "payment",
            "service.instance.id": "payment-7f8c",
            "k8s.pod.uid": "pod-uid",
        },
        attributes={"rpc.system": "grpc", "http.response.status_code": 500},
        events=(
            {
                "timestamp": 1_700_000_000_200_000_000,
                "name": "exception",
                "attributes": {"exception.message": "invalid token"},
            },
        ),
    )

    assert OtlpTraceWriter(client, "incident").write([span]) == 1

    assert len(client.calls) == 1
    request = ExportTraceServiceRequest.FromString(client.calls[0]["body"])
    resource_spans = request.resource_spans[0]
    resource = {
        attribute.key: attribute.value.string_value
        for attribute in resource_spans.resource.attributes
    }
    replayed = resource_spans.scope_spans[0].spans[0]
    attributes = {attribute.key: attribute.value for attribute in replayed.attributes}
    assert resource["service.name"] == "payment"
    assert resource["service.instance.id"] == "payment-7f8c"
    assert resource["k8s.pod.uid"] == "pod-uid"
    assert replayed.trace_id == bytes.fromhex(trace_id)
    assert replayed.span_id == bytes.fromhex(span_id)
    assert replayed.parent_span_id == bytes.fromhex(parent_span_id)
    assert replayed.kind == 3
    assert replayed.start_time_unix_nano == span.start_time_unix_nano
    assert replayed.end_time_unix_nano == span.end_time_unix_nano
    assert attributes["rpc.system"].string_value == "grpc"
    assert attributes["http.response.status_code"].int_value == 500
    assert replayed.status.code == 2
    assert replayed.events[0].name == "exception"


def test_otlp_replay_does_not_invent_service_name() -> None:
    client = _Client()
    span = TraceSpan(
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id="",
        name="",
        kind=0,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_000_000_000_000,
        service_name=None,
        resource_attributes={"openrca.cmdb_id": "Redis02"},
    )

    OtlpTraceWriter(client, "incident").write([span])

    request = ExportTraceServiceRequest.FromString(client.calls[0]["body"])
    resource = {
        attribute.key: attribute.value.string_value
        for attribute in request.resource_spans[0].resource.attributes
    }
    assert resource == {"openrca.cmdb_id": "Redis02"}


def test_otlp_replay_preserves_an_explicit_empty_service_name() -> None:
    client = _Client()
    span = TraceSpan(
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id="",
        name="",
        kind=0,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_000_000_000_000,
        service_name="",
    )

    OtlpTraceWriter(client, "incident").write([span])

    request = ExportTraceServiceRequest.FromString(client.calls[0]["body"])
    resource = {
        attribute.key: attribute.value.string_value
        for attribute in request.resource_spans[0].resource.attributes
    }
    assert resource == {"service.name": ""}


def test_remote_write_batches_multiple_time_series_in_one_request() -> None:
    client = _Client()

    write_series_batch(
        client,
        "incident",
        [
            ("requests_total", [(1_700_000_000_000, 1.0)], {"service": "checkout"}),
            ("errors_total", [(1_700_000_000_000, 2.0)], {"service": "payment"}),
        ],
    )

    assert len(client.calls) == 1
    assert _count_length_delimited_field(snappy.decompress(client.calls[0]["body"]), 1) == 2


def test_loki_replay_preserves_record_labels() -> None:
    client = _Client()

    write_logs(
        client,
        "incident",
        [
            LogRecord(
                timestamp=1_700_000_000,
                message="failed request",
                labels={"container_name": "payment", "pod_uid": "pod-uid"},
            )
        ],
    )

    payload = json.loads(client.calls[0]["body"])
    assert payload["streams"][0]["stream"] == {
        "container_name": "payment",
        "pod_uid": "pod-uid",
    }
    assert payload["streams"][0]["values"] == [
        ["1700000000000000000", "failed request"]
    ]


def _count_length_delimited_field(payload: bytes, field_number: int) -> int:
    offset = 0
    count = 0
    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        assert key & 0x07 == 2
        length, offset = _read_varint(payload, offset)
        if key >> 3 == field_number:
            count += 1
        offset += length
    return count


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
