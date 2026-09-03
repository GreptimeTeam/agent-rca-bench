"""Proves the split stack holds what the case's source archive holds.

Only Prometheus, Loki and Tempo are audited here. GreptimeDB's side of the same
case is already audited by the OpenRCA2 and RCA100 adapters, and re-deriving one
target from the other would let the comparison clear itself.

Every expectation is built from the source, and the split arm's own projection
(`split_ingest`) is checked against it rather than trusted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from semantic_rca_bench.protocols.loki import LogRecord, to_nanoseconds
from semantic_rca_bench.protocols.otlp import TraceSpan
from semantic_rca_bench.protocols.prometheus import prometheus_label_name, read_series
from semantic_rca_bench.split_client import LOG_TABLE_LABEL
from semantic_rca_bench.split_ingest import MetricSeries

# Tempo needs a moment after ingestion before a block is flushed and polled, so
# a first empty search is not yet evidence that search is unavailable.
TEMPO_SEARCH_TIMEOUT = 60.0

# Exact trace readback is per-trace, so the fidelity check runs on a declared
# subset. Completeness comes from byte-identical ingestion with zero rejections.
TRACE_FIDELITY_SAMPLE = 25


class SplitStorageAuditError(RuntimeError):
    pass


def audit_split_storage(
    *,
    prometheus_endpoint: str,
    loki_endpoint: str,
    tempo_endpoint: str,
    window: tuple[int, int],
    expected_metrics: list[MetricSeries],
    expected_logs: Mapping[str, list[LogRecord]],
    expected_spans: list[TraceSpan],
    trace_scope_name: str,
    causal_services: frozenset[str] = frozenset(),
    causal_trace_ids: frozenset[str] = frozenset(),
    mechanism: Mapping[str, object] | None = None,
    ingestion_audit: Mapping[str, object] | None = None,
    timeout: float = 180.0,
    search_timeout: float = TEMPO_SEARCH_TIMEOUT,
) -> dict[str, object]:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        metrics = _audit_metrics(
            client,
            prometheus_endpoint,
            window=window,
            expected=expected_metrics,
            mechanism=mechanism,
        )
        logs = _audit_logs(client, loki_endpoint, window=window, expected=expected_logs)
        traces = _audit_traces(
            client,
            tempo_endpoint,
            window=window,
            expected=expected_spans,
            scope_name=trace_scope_name,
            causal_services=causal_services,
            causal_trace_ids=causal_trace_ids,
            search_timeout=search_timeout,
        )
    ingestion = ingestion_audit or {}
    # The computed parity, not the static list of which signals claim it.
    traces_byte_identical = ingestion.get("identical_protocol_payloads") is True
    trace_rejections = sum(
        int(
            _mapping(_mapping(ingestion.get("targets") or {}, target), "traces").get(
                "rejected_items", 0
            )
        )
        for target in ("greptimedb", "split")
    )
    gates = {
        "traces_ingested_as_identical_bytes": traces_byte_identical,
        "no_span_rejected_by_either_store": trace_rejections == 0,
        "prometheus_stores_the_projected_series": metrics["equal"],
        "prometheus_preserves_the_frozen_mechanism": metrics["mechanism_preserved"],
        "loki_stores_every_source_log": logs["equal"],
        "loki_keeps_the_log_tables_apart": logs["tables_distinguishable"],
        "tempo_sampled_spans_match_the_source": traces["sample_equal"],
        "tempo_search_answers_in_the_source_window": traces["source_window_traceql_search"],
        "tempo_service_names_are_source_declared": traces["service_names_declared"],
        # A delay case is scored on its traces and has no metric oracle, so the
        # fidelity sample has to actually contain the causal path.
        "tempo_sample_covers_the_causal_path": (
            traces["causal_traces_sampled"] > 0 or traces["causal_traces_in_source"] == 0
        ),
        "tempo_search_reaches_the_causal_traces": traces["causal_service_search"]["reachable"],
    }
    return {
        "metrics": metrics,
        "logs": logs,
        "traces": traces,
        "ingestion": dict(ingestion),
        "gates": gates,
        "pass": all(gates.values()),
    }


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    return item if isinstance(item, Mapping) else {}


def _audit_metrics(
    client: httpx.Client,
    endpoint: str,
    *,
    window: tuple[int, int],
    expected: list[MetricSeries],
    mechanism: Mapping[str, object] | None,
) -> dict[str, object]:
    stored = read_series(
        client,
        endpoint,
        start_timestamp_ms=window[0] * 1000,
        end_timestamp_ms=window[1] * 1000 - 1,
    )
    expected_rows = canonical_metric_rows(expected)
    stored_rows = canonical_metric_rows(stored)
    missing = expected_rows - stored_rows
    extra = stored_rows - expected_rows
    result: dict[str, object] = {
        "source_samples": sum(expected_rows.values()),
        "source_sha256": counter_hash(expected_rows),
        "stored_samples": sum(stored_rows.values()),
        "stored_sha256": counter_hash(stored_rows),
        "missing_samples": sum(missing.values()),
        "extra_samples": sum(extra.values()),
        "equal": not missing and not extra,
    }
    if mechanism is None:
        result["mechanism_preserved"] = True
        result["mechanism"] = "no metric oracle for this case"
        return result
    facts = _mechanism_facts(stored, mechanism)
    result["mechanism"] = facts
    result["mechanism_preserved"] = bool(facts["preserved"])
    return result


def _mechanism_facts(
    stored: list[tuple[str, list[tuple[int, float]], dict[str, str]]],
    mechanism: Mapping[str, object],
) -> dict[str, object]:
    """Replays the case's frozen metric oracle against what Prometheus stored."""
    metric = str(mechanism["source_table"])
    identity_column = mechanism.get("identity_column")
    identity_value = mechanism.get("identity_value")
    threshold = float(mechanism["threshold"])
    minimum = int(mechanism["minimum_anomalous_observations"])
    periods: dict[str, dict[str, object]] = {}
    for period in ("normal", "abnormal"):
        start, end = mechanism[f"{period}_window"]  # type: ignore[index]
        values = [
            value
            for name, samples, labels in stored
            if name == metric
            and (identity_column is None or labels.get(str(identity_column)) == identity_value)
            for timestamp_ms, value in samples
            if int(start) * 1000 <= timestamp_ms < int(end) * 1000
        ]
        periods[period] = {
            "count": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "high_count": sum(value >= threshold for value in values),
        }
    preserved = (
        periods["normal"]["count"] >= 1
        and periods["normal"]["high_count"] == 0
        and periods["abnormal"]["high_count"] >= minimum
    )
    return {
        "metric": metric,
        "identity": {"column": identity_column, "value": identity_value},
        "threshold": threshold,
        "minimum_anomalous_observations": minimum,
        "periods": periods,
        "preserved": preserved,
    }


def _audit_logs(
    client: httpx.Client,
    endpoint: str,
    *,
    window: tuple[int, int],
    expected: Mapping[str, list[LogRecord]],
) -> dict[str, object]:
    tables: dict[str, object] = {}
    equal = True
    mislabelled = 0
    generated: dict[str, int] = {}
    for table, records in sorted(expected.items()):
        # Loki holds the folded label names the fanout sent, so the source side
        # has to be folded the same way before the two can be compared at all.
        folded = [
            LogRecord(
                # The writer converts to nanoseconds on the way in, so the
                # source side has to be compared in the same unit.
                to_nanoseconds(record.timestamp),
                record.message,
                {
                    prometheus_label_name(str(key)): str(value)
                    for key, value in record.labels.items()
                    # Empty label values are omitted on the way in, because Loki
                    # treats them as absent; the source side has to match.
                    if str(value) != ""
                },
            )
            for record in records
        ]
        empty_label_values = sum(
            1 for record in records if any(str(value) == "" for value in record.labels.values())
        )
        expected_labels = {label for record in folded for label in record.labels}
        stored, table_generated, foreign = read_loki_logs(
            client,
            endpoint,
            start_ns=window[0] * 1_000_000_000,
            end_ns=window[1] * 1_000_000_000,
            log_table=table,
            expected_count=len(records),
            expected_labels=expected_labels,
        )
        expected_rows = canonical_log_rows(folded)
        stored_rows = canonical_log_rows(stored)
        # Loki collapses entries that are identical in labels, timestamp and
        # line; GreptimeDB keeps every copy. The RCA100 archive repeats each
        # alert three times, so the criterion is that every distinct source
        # record is present and nothing was invented. The multiplicity the two
        # stores disagree on is reported rather than folded away.
        distinct_expected = Counter(dict.fromkeys(expected_rows, 1))
        missing = distinct_expected - stored_rows
        extra = stored_rows - expected_rows
        equal = equal and not missing and not extra
        generated.update(table_generated)
        mislabelled += foreign
        tables[table] = {
            "source_records": sum(expected_rows.values()),
            "stored_records": sum(stored_rows.values()),
            "missing_records": sum(missing.values()),
            "extra_records": sum(extra.values()),
            # A count alone cannot be acted on, so a mismatch carries the rows
            # that differ.
            "missing_examples": sorted(missing)[:3],
            "extra_examples": sorted(extra)[:3],
            "distinct_source_records": len(expected_rows),
            "source_duplicate_records": sum(expected_rows.values()) - len(expected_rows),
            "source_records_with_an_empty_label_value": empty_label_values,
            "equal": not missing and not extra,
        }
    return {
        "log_table_label": LOG_TABLE_LABEL,
        "tables": tables,
        # Measured, not counted: a store that ignored the carrier label would
        # answer a per-table query with streams belonging to other tables.
        # Comparing the number of tables asked for with the number answered can
        # only ever be equal and proves nothing.
        "streams_from_another_table": mislabelled,
        "tables_distinguishable": mislabelled == 0,
        "loki_generated_labels": generated,
        "equal": equal,
    }


def _audit_traces(
    client: httpx.Client,
    endpoint: str,
    *,
    window: tuple[int, int],
    expected: list[TraceSpan],
    scope_name: str,
    causal_services: frozenset[str] = frozenset(),
    causal_trace_ids: frozenset[str] = frozenset(),
    search_timeout: float = TEMPO_SEARCH_TIMEOUT,
    fidelity_sample: int = TRACE_FIDELITY_SAMPLE,
) -> dict[str, object]:
    """Trace equivalence, argued from ingestion rather than from bulk readback.

    Traces are the one signal both stores receive as the same bytes, and the
    fanout already records that neither target rejected a span, so completeness
    follows from ingestion. Reading every trace back would issue one request per
    trace, which exhausts the store's file descriptors long before it proves
    anything the byte equality has not already settled.

    What readback still has to establish is that the stored spans are the source
    spans. That is checked exactly, on a declared and deterministic subset whose
    size is reported rather than assumed away.
    """
    declared = all(
        isinstance(span.service_name, str) and span.service_name not in ("", "unknown")
        for span in expected
    )
    # Poll search first: it is Tempo's only readiness signal, and an exact
    # lookup issued before the block is flushed would 404 on a trace that is
    # merely not indexed yet.
    visibility = tempo_search_visibility(
        client,
        endpoint,
        start_seconds=window[0],
        end_seconds=window[1],
        expected_spans=expected,
        timeout=search_timeout,
    )
    # Sample the traces the case is actually scored on first. An arbitrary slice
    # of trace ids would leave a delay case's causal path unchecked, and those
    # cases have no metric oracle to fall back on.
    causal_ids = sorted(
        causal_trace_ids
        or {str(span.trace_id) for span in expected if str(span.service_name) in causal_services}
    )
    other_ids = sorted({str(span.trace_id) for span in expected} - set(causal_ids))
    sampled_ids = (causal_ids + other_ids)[:fidelity_sample]
    # The agent reaches traces by searching for a service, not by guessing ids.
    # Proving id lookup works says nothing about whether that path returns the
    # case's evidence.
    causal_search = _causal_service_search(
        client,
        endpoint,
        window=window,
        services=causal_services,
        expected=expected,
        causal_trace_ids=set(causal_ids),
    )
    sampled_spans = [span for span in expected if str(span.trace_id) in set(sampled_ids)]
    expected_rows = canonical_source_spans(sampled_spans)
    stored_rows, absent = read_tempo_spans(
        client,
        endpoint,
        sampled_spans,
        expected_scope_name=scope_name,
        window=window,
    )
    missing = expected_rows - stored_rows
    extra = stored_rows - expected_rows
    return {
        "source_spans": len(expected),
        "source_traces": len({str(span.trace_id) for span in expected}),
        "fidelity_sample_traces": len(sampled_ids),
        "causal_traces_in_source": len(causal_ids),
        "causal_traces_sampled": len(set(sampled_ids) & set(causal_ids)),
        "fidelity_sample_spans": sum(expected_rows.values()),
        "fidelity_sample_sha256": counter_hash(expected_rows),
        "stored_sample_spans": sum(stored_rows.values()),
        "stored_sample_sha256": counter_hash(stored_rows),
        "missing_sample_spans": sum(missing.values()),
        "extra_sample_spans": sum(extra.values()),
        "absent_sample_traces": len(absent),
        "sample_equal": not missing and not extra and not absent,
        # RCA100's reader substitutes "unknown" when the archive has no service
        # name, so a non-empty check alone would pass on that fabricated value.
        "service_names_declared": declared,
        "causal_service_search": causal_search,
        **visibility,
    }


def _causal_service_search(
    client: httpx.Client,
    endpoint: str,
    *,
    window: tuple[int, int],
    services: frozenset[str],
    expected: list[TraceSpan],
    causal_trace_ids: set[str],
) -> dict[str, object]:
    """Does a TraceQL search on the causal service answer, and answer correctly?

    The agent reaches traces by searching for a service, so that path has to
    return at least one causal trace. Other traces touching the service are valid
    results, but traces outside that service are not.
    """
    if not services or not causal_trace_ids:
        return {"queried": False, "reachable": True}
    service = sorted(services)[0]
    service_trace_ids = {
        str(span.trace_id) for span in expected if str(span.service_name) == service
    }
    query = f'{{ resource.service.name = "{service}" }}'
    response = client.get(
        f"{endpoint.rstrip('/')}/api/search",
        params={
            "q": query,
            "start": str(window[0]),
            "end": str(window[1]),
            "limit": "20",
        },
    )
    response.raise_for_status()
    payload = response.json()
    traces = payload.get("traces") if isinstance(payload, Mapping) else None
    returned = {
        normalized
        for trace in (traces if isinstance(traces, list) else [])
        if isinstance(trace, Mapping)
        and (normalized := _normalize_tempo_trace_id(trace.get("traceID"))) is not None
    }
    returned_causal = returned & causal_trace_ids
    return {
        "queried": True,
        "query": query,
        "service": service,
        "source_traces_with_the_service": len(service_trace_ids),
        "returned_traces": len(returned),
        "returned_causal_traces": len(returned_causal),
        "returned_outside_the_service": len(returned - service_trace_ids),
        "reachable": bool(returned_causal) and returned <= service_trace_ids,
    }


def canonical_metric_rows(
    series: Iterable[tuple[str, Iterable[tuple[int, float]], Mapping[str, str] | None]],
) -> Counter[str]:
    rows: Counter[str] = Counter()
    for metric, samples, labels in series:
        canonical_labels = tuple(
            sorted((str(key), str(value)) for key, value in (labels or {}).items())
        )
        for timestamp_ms, value in samples:
            rows[
                canonical_json((metric, canonical_labels, int(timestamp_ms), float(value).hex()))
            ] += 1
    return rows


def counter_hash(rows: Counter[str]) -> str:
    digest = hashlib.sha256()
    for row, count in sorted(rows.items()):
        digest.update(f"{count}\0{row}\n".encode())
    return digest.hexdigest()


def read_loki_logs(
    client: httpx.Client,
    endpoint: str,
    *,
    start_ns: int,
    end_ns: int,
    log_table: str,
    expected_count: int,
    expected_labels: set[str],
) -> tuple[list[LogRecord], dict[str, int], int]:
    # Selects on the table label, not on `service_name`: RCA100's events and
    # alerts carry no service name, so a service-name matcher would silently
    # return nothing for two of the three tables.
    response = client.get(
        f"{endpoint.rstrip('/')}/loki/api/v1/query_range",
        params={
            "query": f'{{{LOG_TABLE_LABEL}="{log_table}"}}',
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(expected_count + 1),
            "direction": "forward",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise SplitStorageAuditError(f"Loki audit query failed: {payload}")
    records = []
    generated_labels: Counter[str] = Counter()
    foreign_streams = 0
    for stream in (payload.get("data") or {}).get("result") or []:
        labels = dict(stream.get("stream") or {})
        # The table label is the fanout's own carrier, not a source label. A
        # stream answering this query under a different table name would mean
        # the store did not keep the tables apart.
        if labels.pop(LOG_TABLE_LABEL, None) != log_table:
            foreign_streams += 1
        # Any label the source did not carry was invented by the store. Counting
        # them by name, rather than checking for the two Loki happens to add
        # today, keeps a future store-generated field from passing as source
        # data.
        for name in [name for name in labels if name not in expected_labels]:
            generated_labels[name] += len(stream.get("values") or [])
            del labels[name]
        for entry in stream.get("values") or []:
            timestamp, line = entry[0], entry[1]
            metadata = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
            records.append(
                LogRecord(
                    int(timestamp),
                    str(line),
                    {**labels, **{key: str(value) for key, value in metadata.items()}},
                )
            )
    if len(records) > expected_count:
        raise SplitStorageAuditError("Loki audit query exceeded the expected source count")
    return records, dict(sorted(generated_labels.items())), foreign_streams


def read_tempo_spans(
    client: httpx.Client,
    endpoint: str,
    expected_spans: Iterable[TraceSpan],
    *,
    expected_scope_name: str,
    window: tuple[int, int],
    timeout: float = TEMPO_SEARCH_TIMEOUT,
) -> tuple[Counter[str], list[str]]:
    """Reads back the given traces, reporting absences instead of raising.

    A trace the store does not hold is a measurement the gate has to see, not
    an exception that ends the case before the rest of the audit runs.

    An id lookup can miss a trace that search already answers for, because the
    two read different parts of the store while a block is being flushed. Absent
    ids are therefore retried until a deadline; a trace still missing when the
    deadline passes is reported, and the gate fails on it.
    """
    trace_ids = sorted({str(span.trace_id) for span in expected_spans})
    absent: list[str] = []

    def read(trace_id: str) -> dict[str, Any] | None:
        # Tempo narrows an id lookup to a time range when given one. The case
        # window is known, so passing it makes the lookup deterministic instead
        # of dependent on the store's default lookback.
        response = client.get(
            f"{endpoint.rstrip('/')}/api/traces/{trace_id}",
            params={"start": str(window[0]), "end": str(window[1])},
        )
        if response.status_code == 404:
            absent.append(trace_id)
            return None
        if response.is_error:
            raise SplitStorageAuditError(
                f"Tempo trace {trace_id} failed ({response.status_code}): {response.text[:400]}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise SplitStorageAuditError("Tempo trace response is not an object")
        return payload

    rows: Counter[str] = Counter()
    deadline = time.monotonic() + timeout
    pending = list(trace_ids)
    payloads: list[dict[str, Any]] = []
    while True:
        absent = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            payloads.extend(payload for payload in pool.map(read, pending) if payload is not None)
        if not absent or time.monotonic() >= deadline:
            break
        pending = list(absent)
        time.sleep(1.0)

    for payload in payloads:
        for batch in payload.get("batches") or []:
            resource = _otlp_attributes((batch.get("resource") or {}).get("attributes") or [])
            service_name = resource.get("service.name")
            if not isinstance(service_name, str) or not service_name:
                raise SplitStorageAuditError("Tempo span has no source service.name resource")
            for scope_spans in batch.get("scopeSpans") or []:
                scope = scope_spans.get("scope") or {}
                if scope.get("name") != expected_scope_name or not scope.get("version"):
                    raise SplitStorageAuditError("Tempo trace scope drifted")
                for span in scope_spans.get("spans") or []:
                    rows[
                        canonical_span(
                            trace_id=_base64_hex(span.get("traceId")),
                            span_id=_base64_hex(span.get("spanId")),
                            parent_span_id=_base64_hex(span.get("parentSpanId")),
                            name=str(span.get("name") or ""),
                            kind=str(span.get("kind") or "SPAN_KIND_UNSPECIFIED"),
                            start=int(span.get("startTimeUnixNano") or 0),
                            end=int(span.get("endTimeUnixNano") or 0),
                            status=str(
                                (span.get("status") or {}).get("code") or "STATUS_CODE_UNSET"
                            ),
                            service_name=service_name,
                            attributes=_otlp_attributes(span.get("attributes") or []),
                        )
                    ] += 1
    return rows, sorted(absent)


def tempo_search_visibility(
    client: httpx.Client,
    endpoint: str,
    *,
    start_seconds: int,
    end_seconds: int,
    expected_spans: Iterable[TraceSpan],
    timeout: float,
) -> dict[str, object]:
    expected_trace_ids = {str(span.trace_id) for span in expected_spans}
    start_ns = start_seconds * 1_000_000_000
    end_ns = end_seconds * 1_000_000_000
    deadline = time.monotonic() + timeout
    last_result: dict[str, object] = {}
    while True:
        response = client.get(
            f"{endpoint.rstrip('/')}/api/search",
            params={
                "q": "{ true }",
                "start": str(start_seconds),
                "end": str(end_seconds),
                "limit": "20",
            },
        )
        response.raise_for_status()
        payload = response.json()
        traces = payload.get("traces") if isinstance(payload, Mapping) else None
        returned = traces if isinstance(traces, list) else []
        returned_ids = {
            normalized
            for trace in returned
            if isinstance(trace, Mapping)
            and (normalized := _normalize_tempo_trace_id(trace.get("traceID"))) is not None
        }
        timestamps = [
            int(trace["startTimeUnixNano"])
            for trace in returned
            if isinstance(trace, Mapping) and trace.get("startTimeUnixNano") is not None
        ]
        all_known = len(returned_ids) == len(returned) and returned_ids <= expected_trace_ids
        all_in_window = len(timestamps) == len(returned) and all(
            start_ns <= timestamp < end_ns for timestamp in timestamps
        )
        passed = bool(returned) and all_known and all_in_window
        last_result = {
            "source_window_traceql_search": passed,
            "query": "{ true }",
            "window": {"start": start_seconds, "end": end_seconds},
            "returned_traces": len(returned),
            "all_trace_ids_from_source": all_known,
            "all_trace_starts_in_source_window": all_in_window,
        }
        # Tempo only answers TraceQL search once a block is flushed and polled,
        # so a first empty result is not yet evidence that search is unavailable.
        if passed or time.monotonic() >= deadline:
            return last_result
        time.sleep(0.5)


def canonical_log_rows(records: Iterable[LogRecord]) -> Counter[str]:
    rows: Counter[str] = Counter()
    for record in records:
        timestamp = int(record.timestamp)
        message = str(record.message)
        labels = tuple(sorted((str(key), str(value)) for key, value in record.labels.items()))
        rows[canonical_json((labels, timestamp, message))] += 1
    return rows


def canonical_source_spans(spans: Iterable[TraceSpan]) -> Counter[str]:
    rows: Counter[str] = Counter()
    for span in spans:
        rows[
            canonical_span(
                trace_id=str(span.trace_id),
                span_id=str(span.span_id),
                parent_span_id=str(span.parent_span_id),
                name=str(span.name),
                kind=_span_kind(int(span.kind)),
                start=int(span.start_time_unix_nano),
                end=int(span.end_time_unix_nano),
                status=_status_code(int(span.status_code)),
                service_name=str(span.service_name),
                attributes=span.attributes,
            )
        ] += 1
    return rows


def canonical_span(**span: object) -> str:
    attributes = span["attributes"]
    if not isinstance(attributes, Mapping):
        raise SplitStorageAuditError("span attributes are not a mapping")
    span["attributes"] = {
        str(key): _canonical_value(value) for key, value in sorted(attributes.items())
    }
    return canonical_json(span)


def canonical_json(value: object) -> str:
    return json.dumps(_canonical_value(value), sort_keys=True, separators=(",", ":"))


def _otlp_attributes(attributes: Iterable[Mapping[str, object]]) -> dict[str, object]:
    return {
        str(attribute.get("key")): _otlp_value(attribute.get("value")) for attribute in attributes
    }


def _otlp_value(value: object) -> object:
    if not isinstance(value, Mapping):
        return None
    fields = (
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "bytesValue",
        "arrayValue",
        "kvlistValue",
    )
    present = [(field, value[field]) for field in fields if field in value]
    if len(present) != 1:
        return None
    field, item = present[0]
    if field == "intValue":
        return int(item)
    if field == "doubleValue":
        return float(item)
    if field == "bytesValue":
        return base64.b64decode(str(item))
    if field == "arrayValue":
        return [_otlp_value(entry) for entry in (item or {}).get("values") or []]
    if field == "kvlistValue":
        return _otlp_attributes((item or {}).get("values") or [])
    return item


def _base64_hex(value: object) -> str:
    if value in (None, ""):
        return ""
    return base64.b64decode(str(value)).hex()


def _normalize_tempo_trace_id(value: object) -> str | None:
    trace_id = str(value or "").lower()
    if not trace_id or len(trace_id) > 32:
        return None
    try:
        int(trace_id, 16)
    except ValueError:
        return None
    return trace_id.zfill(32)


def _span_kind(value: int) -> str:
    return {
        0: "SPAN_KIND_UNSPECIFIED",
        1: "SPAN_KIND_INTERNAL",
        2: "SPAN_KIND_SERVER",
        3: "SPAN_KIND_CLIENT",
        4: "SPAN_KIND_PRODUCER",
        5: "SPAN_KIND_CONSUMER",
    }[value]


def _status_code(value: int) -> str:
    return {0: "STATUS_CODE_UNSET", 1: "STATUS_CODE_OK", 2: "STATUS_CODE_ERROR"}[value]


def _canonical_value(value: object) -> object:
    if isinstance(value, float):
        return {"float_hex": value.hex()}
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value
