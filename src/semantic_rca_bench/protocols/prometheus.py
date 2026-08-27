from __future__ import annotations

import math
import re
import struct
from collections.abc import Iterable

import snappy

from semantic_rca_bench.greptimedb.client import GreptimeClient

_INVALID_METRIC_CHARACTER = re.compile(r"[^a-zA-Z0-9_:]")


def prometheus_metric_name(source_name: str) -> str:
    name = _INVALID_METRIC_CHARACTER.sub("_", source_name)
    if not name or not re.match(r"[a-zA-Z_:]", name[0]):
        name = f"metric_{name}"
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
    series: Iterable[
        tuple[str, Iterable[tuple[int, float]], dict[str, str] | None]
    ],
    *,
    batch_size: int = 100,
) -> None:
    batch: list[bytes] = []
    for metric_name, samples, labels in series:
        batch.append(encode_write_request(metric_name, samples, labels))
        if len(batch) >= batch_size:
            _send_write_request(client, database, b"".join(batch))
            batch.clear()
    if batch:
        _send_write_request(client, database, b"".join(batch))


def _send_write_request(client: GreptimeClient, database: str, request: bytes) -> None:
    client.post_bytes(
        "/v1/prometheus/write",
        snappy.compress(request),
        params={"db": database},
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
        },
    )
