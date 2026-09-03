"""Loads the split stack's Prometheus with the case's source metrics.

Logs and traces reach Loki and Tempo as the same protocol bytes GreptimeDB
receives, so they are teed by `FanoutIngestClient`. Metrics cannot be: the
OpenRCA2 archives declare no aggregation temporality, and Prometheus' OTLP
receiver rejects `AGGREGATION_TEMPORALITY_UNSPECIFIED` sums and histograms
outright (measured against the pinned image). Writing a temporality the source
does not state would stamp `metric.temporality` into GreptimeDB's semantic
options, so the replay keeps `UNSPECIFIED` and reaches Prometheus through
remote write instead, which carries no temporality at all.

The projection is derived from the source archive, never from GreptimeDB's
stored rows: reading the target back to build the other target's input would
let the parity audit clear itself.

It mirrors the naming GreptimeDB's OTLP metric path applies, so both stores end
up holding the same series:

- the metric name with characters outside `[a-zA-Z0-9_:]` replaced,
- data point attributes plus every resource attribute as labels,
- `job` from `service.namespace/service.name` (or `service.name` alone) and
  `instance` from `service.instance.id`,
- a histogram fanned out into `_count`, `_sum` and `_bucket{le="inf"}`; the
  source's `min` and `max` are dropped because GreptimeDB drops them too.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from semantic_rca_bench.contracts import OpenRCA2Case
from semantic_rca_bench.protocols.otlp import HistogramMetricPoint, NumberMetricPoint
from semantic_rca_bench.protocols.prometheus import (
    prometheus_label_name,
    prometheus_metric_name,
    write_series_batch_to_prometheus,
)

MetricSeries = tuple[str, list[tuple[int, float]], dict[str, str]]


class SplitIngestError(RuntimeError):
    pass


@dataclass
class _Observed:
    """Every source observation landing on one stored (series, millisecond) key.

    The OpenRCA2 archives lost the attribute that told two data points apart —
    `k8s.pod.network.io` carries no direction — so a key can legitimately see
    several values. GreptimeDB keeps the last one written (measured), and the
    split target has to keep the same one or the two stores would disagree on
    values the source itself cannot separate.
    """

    last_value: float
    count: int
    values: set[float]


def openrca2_prometheus_series(
    gauges: Iterable[NumberMetricPoint],
    sums: Iterable[NumberMetricPoint],
    histograms: Iterable[HistogramMetricPoint],
) -> tuple[list[MetricSeries], dict[str, int]]:
    observations: dict[tuple[str, tuple[tuple[str, str], ...]], dict[int, _Observed]] = {}
    source_observations = 0

    def add(
        name: str,
        timestamp_ns: int,
        value: float,
        labels: dict[str, str],
    ) -> None:
        nonlocal source_observations
        source_observations += 1
        # `encode_write_request` drops non-finite samples silently, which would
        # make `kept_samples` disagree with what Prometheus stored and break the
        # parity audit for a reason the audit could not name.
        if not math.isfinite(value):
            raise SplitIngestError(f"non-finite value for metric {name}")
        key = (prometheus_metric_name(name), tuple(sorted(labels.items())))
        timestamp_ms = timestamp_ns // 1_000_000
        group = observations.setdefault(key, {}).get(timestamp_ms)
        if group is None:
            observations[key][timestamp_ms] = _Observed(last_value=value, count=1, values={value})
            return
        group.last_value = value
        group.count += 1
        group.values.add(value)

    for point in (*gauges, *sums):
        add(point.name, point.time_unix_nano, point.value, _point_labels(point))
    for point in histograms:
        labels = _point_labels(point)
        add(f"{point.name}_count", point.time_unix_nano, float(point.count), labels)
        if point.sum is not None:
            add(f"{point.name}_sum", point.time_unix_nano, float(point.sum), labels)
        add(
            f"{point.name}_bucket",
            point.time_unix_nano,
            float(point.count),
            {**labels, "le": "inf"},
        )

    series: list[MetricSeries] = []
    kept_samples = 0
    repeated_observations = 0
    ambiguous_identities = 0
    ambiguous_observations = 0
    for (metric_name, labels), by_timestamp in sorted(observations.items()):
        samples: list[tuple[int, float]] = []
        for timestamp_ms, group in sorted(by_timestamp.items()):
            repeated_observations += group.count - 1
            if len(group.values) > 1:
                ambiguous_identities += 1
                ambiguous_observations += group.count
            samples.append((timestamp_ms, group.last_value))
        if samples:
            series.append((metric_name, samples, dict(labels)))
            kept_samples += len(samples)
    return series, {
        "source_observations": source_observations,
        "kept_samples": kept_samples,
        "repeated_observations_collapsed": repeated_observations,
        "source_ambiguous_identities": ambiguous_identities,
        "source_ambiguous_observations": ambiguous_observations,
        "ambiguity_resolution": "last observation in source order, as GreptimeDB stores it",
    }


def _point_labels(point: NumberMetricPoint | HistogramMetricPoint) -> dict[str, str]:
    resources: dict[str, object] = dict(point.resource_attributes)
    if point.service_name is not None:
        resources.setdefault("service.name", point.service_name)
    labels = _normalized_labels({**resources, **dict(point.attributes)})
    identity = _service_identity(resources)
    labels.update(identity)
    return labels


def _service_identity(resources: Mapping[str, object]) -> dict[str, str]:
    """`job` and `instance` as GreptimeDB's OTLP metric path derives them."""
    identity: dict[str, str] = {}
    name = resources.get("service.name")
    namespace = resources.get("service.namespace")
    if isinstance(name, str) and name:
        identity["job"] = (
            f"{namespace}/{name}" if isinstance(namespace, str) and namespace else name
        )
    instance = resources.get("service.instance.id")
    if isinstance(instance, str) and instance:
        identity["instance"] = instance
    return identity


def _normalized_labels(source: Mapping[str, object]) -> dict[str, str]:
    labels: dict[str, str] = {}
    origins: dict[str, str] = {}
    for raw_key, value in source.items():
        key = str(raw_key)
        normalized = prometheus_label_name(key)
        if normalized == "__name__":
            raise SplitIngestError("source metric attribute collides with __name__")
        previous = origins.get(normalized)
        if previous is not None and previous != key:
            raise SplitIngestError(
                f"metric label names collide after normalization: {previous}, {key}"
            )
        origins[normalized] = key
        labels[normalized] = _label_value(value)
    return labels


def _label_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        raise SplitIngestError("source metric attribute is not a finite number")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return str(value)


def collapse_series(series: Iterable[MetricSeries]) -> tuple[list[MetricSeries], dict[str, object]]:
    """Reduces already-built series to one sample per (series, timestamp).

    The RCA100 reader yields every source row, but both stores keep one sample
    per key: GreptimeDB is audited against `metric_unique_samples`, and
    Prometheus rejects a repeated timestamp within a series. Comparing the
    uncollapsed source against either would report the archive's own repetition
    as missing data.
    """
    collapsed: list[MetricSeries] = []
    source_observations = 0
    repeated = 0
    ambiguous_identities = 0
    ambiguous_observations = 0
    kept_samples = 0
    for metric_name, samples, labels in series:
        groups: dict[int, _Observed] = {}
        for timestamp_ms, value in samples:
            source_observations += 1
            group = groups.get(timestamp_ms)
            if group is None:
                groups[timestamp_ms] = _Observed(last_value=value, count=1, values={value})
                continue
            group.last_value = value
            group.count += 1
            group.values.add(value)
        kept = []
        for timestamp_ms, group in sorted(groups.items()):
            repeated += group.count - 1
            if len(group.values) > 1:
                ambiguous_identities += 1
                ambiguous_observations += group.count
            kept.append((timestamp_ms, group.last_value))
        if kept:
            collapsed.append((metric_name, kept, dict(labels)))
            kept_samples += len(kept)
    return collapsed, {
        "source_observations": source_observations,
        "kept_samples": kept_samples,
        "repeated_observations_collapsed": repeated,
        "source_ambiguous_identities": ambiguous_identities,
        "source_ambiguous_observations": ambiguous_observations,
        "ambiguity_resolution": "last observation in source order, as GreptimeDB stores it",
    }


def openrca2_case_series(case: OpenRCA2Case) -> tuple[list[MetricSeries], dict[str, int]]:
    from semantic_rca_bench.datasets.openrca2 import _iter_histograms, _iter_number_metrics

    return openrca2_prometheus_series(
        _iter_number_metrics(case.gauge_paths),
        _iter_number_metrics(case.sum_paths),
        _iter_histograms(case.histogram_paths),
    )


def load_prometheus_metrics(
    client: object,
    series: list[MetricSeries],
    projection: dict[str, int],
) -> dict[str, object]:
    write_series_batch_to_prometheus(client, series)
    return {
        "protocol": "prometheus-remote-write-0.1",
        "series": len(series),
        "samples": sum(len(samples) for _, samples, _ in series),
        "projection": projection,
    }
