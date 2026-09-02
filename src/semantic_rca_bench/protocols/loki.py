from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from semantic_rca_bench.greptimedb.client import GreptimeClient


@dataclass(frozen=True)
class LogRecord:
    timestamp: int
    message: str
    labels: Mapping[str, str] = field(default_factory=dict)


def to_nanoseconds(timestamp: int) -> int:
    magnitude = abs(timestamp)
    if magnitude < 100_000_000_000:
        return timestamp * 1_000_000_000
    if magnitude < 100_000_000_000_000:
        return timestamp * 1_000_000
    if magnitude < 100_000_000_000_000_000:
        return timestamp * 1_000
    return timestamp


def write_logs(
    client: GreptimeClient,
    database: str,
    records: Iterable[LogRecord | tuple[int, str, str]],
    *,
    table: str = "logs",
    batch_size: int = 5_000,
) -> int:
    batch: list[LogRecord] = []
    count = 0
    for record in records:
        batch.append(_log_record(record))
        if len(batch) >= batch_size:
            _write_batch(client, database, batch, table)
            count += len(batch)
            batch.clear()
    if batch:
        _write_batch(client, database, batch, table)
        count += len(batch)
    return count


def _write_batch(
    client: GreptimeClient,
    database: str,
    records: list[LogRecord],
    table: str,
) -> None:
    streams: dict[tuple[tuple[str, str], ...], list[list[str]]] = defaultdict(list)
    for record in records:
        labels = tuple(sorted((str(key), str(value)) for key, value in record.labels.items()))
        streams[labels].append([str(to_nanoseconds(record.timestamp)), record.message])
    payload = {
        "streams": [
            {
                "stream": dict(labels),
                "values": values,
            }
            for labels, values in streams.items()
        ]
    }
    client.post_bytes(
        "/v1/loki/api/v1/push",
        json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Content-Type": "application/json",
            "x-greptime-db-name": database,
            "x-greptime-log-table-name": table,
        },
    )


def _log_record(record: LogRecord | tuple[int, str, str]) -> LogRecord:
    if isinstance(record, LogRecord):
        return record
    timestamp, container_name, message = record
    return LogRecord(
        timestamp=timestamp,
        message=message,
        labels={"container_name": container_name or "unknown"},
    )
