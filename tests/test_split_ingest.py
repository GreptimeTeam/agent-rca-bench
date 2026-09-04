from __future__ import annotations

import pytest

from agent_rca_bench.protocols.otlp import HistogramMetricPoint, NumberMetricPoint
from agent_rca_bench.split_ingest import SplitIngestError, openrca2_prometheus_series


def _gauge(**overrides: object) -> NumberMetricPoint:
    base = {
        "name": "k8s.container.restarts",
        "time_unix_nano": 1_700_000_000_000_000_000,
        "value": 1.0,
        "service_name": "user",
        "resource_attributes": {"k8s.node.name": "node-a"},
        "attributes": {"k8s.container.name": "hotel-reserv-user"},
    }
    return NumberMetricPoint(**{**base, **overrides})  # type: ignore[arg-type]


def _series_map(series: list[tuple[str, list[tuple[int, float]], dict[str, str]]]) -> dict:
    return {name: (samples, labels) for name, samples, labels in series}


def test_projection_normalizes_names_and_derives_the_job_identity() -> None:
    series, audit = openrca2_prometheus_series([_gauge()], [], [])

    (name, samples, labels) = series[0]
    assert name == "k8s_container_restarts"
    assert samples == [(1_700_000_000_000, 1.0)]
    assert labels == {
        "k8s_node_name": "node-a",
        "k8s_container_name": "hotel-reserv-user",
        "service_name": "user",
        "job": "user",
    }
    assert audit["kept_samples"] == 1


def test_projection_joins_the_service_namespace_into_job() -> None:
    point = _gauge(
        resource_attributes={"service.namespace": "hs8", "service.instance.id": "pod-1"},
    )

    series, _ = openrca2_prometheus_series([point], [], [])

    labels = series[0][2]
    assert labels["job"] == "hs8/user"
    assert labels["instance"] == "pod-1"


def test_projection_fans_a_histogram_out_the_way_greptimedb_does() -> None:
    point = HistogramMetricPoint(
        name="request.duration",
        time_unix_nano=1_700_000_000_000_000_000,
        count=3,
        sum=1.5,
        min=0.1,
        max=1.0,
        service_name="search",
        resource_attributes={},
        attributes={},
    )

    series, _ = openrca2_prometheus_series([], [], [point])

    projected = _series_map(series)
    # GreptimeDB's OTLP path emits bucket/count/sum and drops the source's
    # min and max, so the split target must not carry them either.
    assert set(projected) == {
        "request_duration_bucket",
        "request_duration_count",
        "request_duration_sum",
    }
    assert projected["request_duration_bucket"][1]["le"] == "inf"
    assert projected["request_duration_count"][0] == [(1_700_000_000_000, 3.0)]
    assert projected["request_duration_sum"][0] == [(1_700_000_000_000, 1.5)]


def test_projection_resolves_an_ambiguous_identity_the_way_greptimedb_stores_it() -> None:
    # The source lost the attribute that told these two points apart, so both
    # land on one stored key. GreptimeDB keeps the last write; dropping the
    # group instead would leave the split arm holding strictly less.
    first = _gauge(time_unix_nano=1_700_000_000_000_100_000, value=1.0)
    second = _gauge(time_unix_nano=1_700_000_000_000_900_000, value=5.0)

    series, audit = openrca2_prometheus_series([first, second], [], [])

    assert series[0][1] == [(1_700_000_000_000, 5.0)]
    assert audit["source_ambiguous_identities"] == 1
    assert audit["source_ambiguous_observations"] == 2
    assert audit["repeated_observations_collapsed"] == 1


def test_projection_reports_no_ambiguity_when_a_repeated_key_agrees() -> None:
    first = _gauge(time_unix_nano=1_700_000_000_000_100_000, value=2.0)
    second = _gauge(time_unix_nano=1_700_000_000_000_900_000, value=2.0)

    series, audit = openrca2_prometheus_series([first, second], [], [])

    assert series[0][1] == [(1_700_000_000_000, 2.0)]
    assert audit["repeated_observations_collapsed"] == 1
    assert audit["source_ambiguous_identities"] == 0


def test_projection_rejects_attributes_that_collide_after_normalization() -> None:
    point = _gauge(attributes={"k8s.container.name": "a", "k8s/container/name": "b"})

    with pytest.raises(SplitIngestError, match="collide after normalization"):
        openrca2_prometheus_series([point], [], [])
