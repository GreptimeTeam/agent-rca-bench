from __future__ import annotations

import math
import re
import struct
from collections.abc import Iterable

import httpx
import snappy

from agent_rca_bench.greptimedb.client import GreptimeClient

_INVALID_METRIC_CHARACTER = re.compile(r"[^a-zA-Z0-9_:]")
_INVALID_LABEL_CHARACTER = re.compile(r"[^a-zA-Z0-9_]")


def prometheus_metric_name(source_name: str) -> str:
    name = _INVALID_METRIC_CHARACTER.sub("_", source_name)
    if not name or not re.match(r"[a-zA-Z_:]", name[0]):
        name = f"metric_{name}"
    return name


def prometheus_label_name(source_name: str) -> str:
    """The Prometheus/Loki label name for a source attribute.

    Both stores accept only `[a-zA-Z_][a-zA-Z0-9_]*`, so the dotted OpenRCA2
    attribute names have to be folded before they can be sent to either.
    """
    name = _INVALID_LABEL_CHARACTER.sub("_", source_name)
    if not name or not re.match(r"[a-zA-Z_]", name[0]):
        name = f"label_{name}"
    return name


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _key(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _bytes_field(field: int, value: bytes) -> bytes:
    return _key(field, 2) + _varint(len(value)) + value


def _string_field(field: int, value: str) -> bytes:
    return _bytes_field(field, value.encode())


def _label(name: str, value: str) -> bytes:
    return _string_field(1, name) + _string_field(2, value)


def _sample(value: float, timestamp_ms: int) -> bytes:
    return _key(1, 1) + struct.pack("<d", value) + _key(2, 0) + _varint(timestamp_ms)


def _int64_field(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def encode_write_request(
    metric_name: str,
    samples: Iterable[tuple[int, float]],
    labels: dict[str, str] | None = None,
) -> bytes:
    series = bytearray()
    all_labels = {"__name__": metric_name, **(labels or {})}
    for name, value in sorted(all_labels.items()):
        series.extend(_bytes_field(1, _label(name, value)))
    for timestamp_ms, value in samples:
        if not math.isfinite(value):
            continue
        series.extend(_bytes_field(2, _sample(value, timestamp_ms)))
    return _bytes_field(1, bytes(series))


def write_series(
    client: GreptimeClient,
    database: str,
    metric_name: str,
    samples: Iterable[tuple[int, float]],
    labels: dict[str, str] | None = None,
) -> None:
    write_series_batch(client, database, [(metric_name, samples, labels)])


def write_series_batch(
    client: GreptimeClient,
    database: str,
    series: Iterable[tuple[str, Iterable[tuple[int, float]], dict[str, str] | None]],
    *,
    batch_size: int = 100,
) -> None:
    _write_series_batches(
        client,
        series,
        path="/v1/prometheus/write",
        params={"db": database},
        batch_size=batch_size,
    )


def write_series_batch_to_prometheus(
    client: object,
    series: Iterable[tuple[str, Iterable[tuple[int, float]], dict[str, str] | None]],
    *,
    batch_size: int = 100,
) -> None:
    """Remote-write the same series to a stock Prometheus receiver.

    Prometheus' own endpoint takes no database, so the only difference from the
    GreptimeDB call is the path; the encoder is shared.
    """
    _write_series_batches(
        client,
        series,
        path="/api/v1/write",
        params=None,
        batch_size=batch_size,
    )


def _write_series_batches(
    client: object,
    series: Iterable[tuple[str, Iterable[tuple[int, float]], dict[str, str] | None]],
    *,
    path: str,
    params: dict[str, str] | None,
    batch_size: int,
) -> None:
    batch: list[bytes] = []
    for metric_name, samples, labels in series:
        batch.append(encode_write_request(metric_name, samples, labels))
        if len(batch) >= batch_size:
            _send_write_request(client, path, params, b"".join(batch))
            batch.clear()
    if batch:
        _send_write_request(client, path, params, b"".join(batch))


def read_series(
    client: httpx.Client,
    endpoint: str,
    *,
    start_timestamp_ms: int,
    end_timestamp_ms: int,
) -> list[tuple[str, list[tuple[int, float]], dict[str, str]]]:
    matcher = _int64_field(1, 2) + _string_field(2, "__name__") + _string_field(3, ".+")
    query = (
        _int64_field(1, start_timestamp_ms)
        + _int64_field(2, end_timestamp_ms)
        + _bytes_field(3, matcher)
    )
    response = client.post(
        f"{endpoint.rstrip('/')}/api/v1/read",
        content=snappy.compress(_bytes_field(1, query)),
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
            "X-Prometheus-Remote-Read-Version": "0.1.0",
        },
    )
    response.raise_for_status()
    return _decode_read_response(snappy.decompress(response.content))


def _send_write_request(
    client: object,
    path: str,
    params: dict[str, str] | None,
    request: bytes,
) -> None:
    client.post_bytes(
        path,
        snappy.compress(request),
        params=params,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
        },
    )


def _decode_read_response(
    payload: bytes,
) -> list[tuple[str, list[tuple[int, float]], dict[str, str]]]:
    series: list[tuple[str, list[tuple[int, float]], dict[str, str]]] = []
    for field, wire_type, value in _protobuf_fields(payload):
        if field != 1 or wire_type != 2:
            continue
        for result_field, result_wire_type, result_value in _protobuf_fields(value):
            if result_field != 1 or result_wire_type != 2:
                continue
            labels: dict[str, str] = {}
            samples: list[tuple[int, float]] = []
            for series_field, series_wire_type, series_value in _protobuf_fields(result_value):
                if series_field == 1 and series_wire_type == 2:
                    name = ""
                    label_value = ""
                    for label_field, label_wire_type, item in _protobuf_fields(series_value):
                        if label_wire_type != 2:
                            continue
                        if label_field == 1:
                            name = item.decode()
                        elif label_field == 2:
                            label_value = item.decode()
                    if not name:
                        raise ValueError("remote-read label has no name")
                    labels[name] = label_value
                elif series_field == 2 and series_wire_type == 2:
                    sample_value = 0.0
                    timestamp_ms: int | None = None
                    for sample_field, sample_wire_type, item in _protobuf_fields(series_value):
                        if sample_field == 1 and sample_wire_type == 1:
                            sample_value = struct.unpack("<d", item)[0]
                        elif sample_field == 2 and sample_wire_type == 0:
                            timestamp_ms = item
                    if timestamp_ms is None:
                        raise ValueError("remote-read sample is incomplete")
                    samples.append((timestamp_ms, sample_value))
            metric_name = labels.pop("__name__", None)
            if metric_name is None:
                raise ValueError("remote-read series has no __name__ label")
            series.append((metric_name, samples, labels))
    return series


def _protobuf_fields(payload: bytes) -> Iterable[tuple[int, int, int | bytes]]:
    offset = 0
    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        field = key >> 3
        wire_type = key & 7
        if field == 0:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(payload):
                raise ValueError("truncated protobuf fixed64 field")
            value = payload[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(payload, offset)
            end = offset + size
            if end > len(payload):
                raise ValueError("truncated protobuf bytes field")
            value = payload[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(payload):
                raise ValueError("truncated protobuf fixed32 field")
            value = payload[offset:end]
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        yield field, wire_type, value


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(payload) and shift < 70:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")
