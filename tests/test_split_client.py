from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from semantic_rca_bench.split_client import FanoutIngestClient, SplitBackendError


@dataclass
class _Response:
    content: bytes = b""


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.databases: list[str] = []

    def create_database(self, database: str) -> None:
        self.databases.append(database)

    def post_bytes(
        self,
        path: str,
        body: bytes,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> _Response:
        self.calls.append(
            {
                "path": path,
                "body": body,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
        )
        return _Response()


def test_fanout_replays_identical_payloads_to_native_endpoints() -> None:
    greptime = _Client()
    prometheus = _Client()
    loki = _Client()
    tempo = _Client()
    client = FanoutIngestClient(
        greptime,  # type: ignore[arg-type]
        prometheus=prometheus,  # type: ignore[arg-type]
        loki=loki,  # type: ignore[arg-type]
        tempo=tempo,  # type: ignore[arg-type]
    )

    client.create_database("case_001")

    client.post_bytes(
        "/v1/otlp/v1/metrics",
        b"metric-payload",
        headers={
            "Content-Type": "application/x-protobuf",
            "x-greptime-db-name": "case_001",
        },
    )
    client.post_bytes(
        "/v1/loki/api/v1/push",
        b'{"streams":[{"stream":{"attr.k8s.pod.name":"user-1"},"values":[["1","boom"]]}]}',
        headers={
            "Content-Type": "application/json",
            "x-greptime-db-name": "case_001",
            "x-greptime-log-table-name": "events",
        },
    )
    client.post_bytes(
        "/v1/otlp/v1/traces",
        b"trace-payload",
        headers={
            "Content-Type": "application/x-protobuf",
            "x-greptime-db-name": "case_001",
        },
    )

    # Prometheus rejects the source's unspecified-temporality sums and
    # histograms, so metrics never reach it through the tee.
    assert prometheus.calls == []
    assert loki.calls[0]["path"] == "/loki/api/v1/push"
    assert tempo.calls[0]["path"] == "/v1/traces"
    # Loki cannot take dotted label names and does not read the table header,
    # so its payload carries folded names plus the table as a stream label.
    assert json.loads(loki.calls[0]["body"])["streams"][0]["stream"] == {
        "attr_k8s_pod_name": "user-1",
        "log_table": "events",
    }
    assert tempo.calls[0]["body"] == b"trace-payload"
    assert [call["path"] for call in greptime.calls] == [
        "/v1/otlp/v1/metrics",
        "/v1/loki/api/v1/push",
        "/v1/otlp/v1/traces",
    ]
    assert all(set(call["headers"]) == {"Content-Type"} for call in (*loki.calls, *tempo.calls))
    audit = client.audit()
    assert audit["identical_protocol_payloads"] is True
    assert audit["byte_identical_signals"] == ["traces"]
    assert audit["protocol_mapping"]["metrics"]["split"] == "prometheus-remote-write-0.1"
    targets = audit["targets"]
    assert targets["split"]["metrics"]["batches"] == 0
    assert targets["greptimedb"]["metrics"]["batches"] == 1
    assert greptime.databases == ["case_001"]
    assert prometheus.databases == []


def test_fanout_keeps_the_three_greptimedb_log_tables_apart_in_loki() -> None:
    greptime = _Client()
    loki = _Client()
    client = FanoutIngestClient(
        greptime,  # type: ignore[arg-type]
        prometheus=_Client(),  # type: ignore[arg-type]
        loki=loki,  # type: ignore[arg-type]
        tempo=_Client(),  # type: ignore[arg-type]
    )

    for table in ("logs", "events", "alerts"):
        client.post_bytes(
            "/v1/loki/api/v1/push",
            b'{"streams":[{"stream":{"pod_name":"user-1"},"values":[["1","boom"]]}]}',
            headers={
                "Content-Type": "application/json",
                "x-greptime-log-table-name": table,
            },
        )

    labels = [json.loads(call["body"])["streams"][0]["stream"] for call in loki.calls]
    assert [item["log_table"] for item in labels] == ["logs", "events", "alerts"]


def test_fanout_rejects_a_log_label_that_would_shadow_the_table_label() -> None:
    client = FanoutIngestClient(
        _Client(),  # type: ignore[arg-type]
        prometheus=_Client(),  # type: ignore[arg-type]
        loki=_Client(),  # type: ignore[arg-type]
        tempo=_Client(),  # type: ignore[arg-type]
    )

    with pytest.raises(SplitBackendError, match="collides with log_table"):
        client.post_bytes(
            "/v1/loki/api/v1/push",
            b'{"streams":[{"stream":{"log.table":"spoofed"},"values":[["1","boom"]]}]}',
            headers={"Content-Type": "application/json"},
        )


def test_fanout_rejects_unknown_ingestion_path_before_writing() -> None:
    greptime = _Client()
    split = _Client()
    client = FanoutIngestClient(
        greptime,  # type: ignore[arg-type]
        prometheus=split,  # type: ignore[arg-type]
        loki=split,  # type: ignore[arg-type]
        tempo=split,  # type: ignore[arg-type]
    )

    try:
        client.post_bytes("/unknown", b"payload", headers={"Content-Type": "text/plain"})
    except SplitBackendError as error:
        assert str(error) == "unsupported fanout ingestion path: /unknown"
    else:
        raise AssertionError("unknown ingestion path was accepted")
    assert greptime.calls == []
