import httpx

import semantic_rca_bench.split_audit as split_audit
from semantic_rca_bench.protocols.otlp import TraceSpan
from semantic_rca_bench.split_audit import tempo_search_visibility


def _span() -> TraceSpan:
    return TraceSpan(
        trace_id="01" * 16,
        span_id="02" * 8,
        parent_span_id="",
        name="request",
        kind=1,
        start_time_unix_nano=1_500_000_000,
        end_time_unix_nano=1_600_000_000,
        service_name="frontend",
    )


def test_tempo_visibility_requires_source_trace_in_source_window() -> None:
    def response(start_ns: int) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "traces": [
                        {
                            "traceID": ("01" * 16).lstrip("0"),
                            "startTimeUnixNano": str(start_ns),
                        }
                    ]
                },
            )
        )

    def visibility(start_ns: int) -> dict[str, object]:
        with httpx.Client(transport=response(start_ns)) as client:
            return tempo_search_visibility(
                client,
                "http://tempo",
                start_seconds=1,
                end_seconds=2,
                expected_spans=[_span()],
                timeout=0,
            )

    assert visibility(1_500_000_000)["source_window_traceql_search"] is True
    assert visibility(2_000_000_000)["source_window_traceql_search"] is False


def test_loki_readback_selects_by_table_label_not_service_name() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"log_table": "events", "pod_name": "user-1"},
                            "values": [["1", "boom"]],
                        }
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records, generated, foreign = split_audit.read_loki_logs(
            client,
            "http://loki",
            start_ns=0,
            end_ns=10,
            log_table="events",
            expected_count=1,
            expected_labels={"pod_name"},
        )

    # RCA100 events and alerts carry no service name, so a service-name matcher
    # would silently return nothing for two of the three tables.
    assert seen == ['{log_table="events"}']
    assert generated == {}
    assert foreign == 0
    # The carrier label is the fanout's, not the source's, so it must not show
    # up as a stored label.
    assert records == [split_audit.LogRecord(1, "boom", {"pod_name": "user-1"})]


def test_loki_extra_records_are_reported_as_a_gate_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"log_table": "events", "pod_name": "user-1"},
                            "values": [["1000000000", "source"], ["2000000000", "extra"]],
                        }
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        audit = split_audit._audit_logs(
            client,
            "http://loki",
            window=(0, 3),
            expected={"events": [split_audit.LogRecord(1, "source", {"pod_name": "user-1"})]},
        )

    assert audit["equal"] is False
    assert audit["tables"]["events"]["extra_records"] == 1


def test_loki_generated_labels_fail_the_storage_audit(monkeypatch) -> None:
    monkeypatch.setattr(
        split_audit,
        "_audit_metrics",
        lambda *_args, **_kwargs: {"equal": True, "mechanism_preserved": True},
    )
    monkeypatch.setattr(
        split_audit,
        "_audit_logs",
        lambda *_args, **_kwargs: {
            "equal": True,
            "tables_distinguishable": True,
            "loki_generated_labels": {"__stream_shard__": 1},
        },
    )
    monkeypatch.setattr(
        split_audit,
        "_audit_traces",
        lambda *_args, **_kwargs: {
            "sample_equal": True,
            "source_window_traceql_search": True,
            "service_names_declared": True,
            "causal_traces_sampled": 0,
            "causal_traces_in_source": 0,
            "causal_service_search": {"reachable": True},
        },
    )

    audit = split_audit.audit_split_storage(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
        window=(0, 1),
        expected_metrics=[],
        expected_logs={},
        expected_spans=[],
        trace_scope_name="scope",
        ingestion_audit={
            "targets": {
                "greptimedb": {"traces": {"rejected_items": 0}},
                "split": {"traces": {"rejected_items": 0}},
            }
        },
    )

    assert audit["gates"]["loki_adds_no_generated_labels"] is False
    assert audit["pass"] is False


def test_mechanism_facts_replay_the_frozen_oracle_against_stored_samples() -> None:
    mechanism = {
        "source_table": "k8s_container_restarts",
        "identity_column": "k8s_container_name",
        "identity_value": "user",
        "threshold": 1.0,
        "minimum_anomalous_observations": 2,
        "normal_window": (100, 200),
        "abnormal_window": (200, 300),
    }
    stored = [
        (
            "k8s_container_restarts",
            [(100_000, 0.0), (150_000, 0.0), (200_000, 3.0), (250_000, 4.0)],
            {"k8s_container_name": "user"},
        ),
        # A different container must not leak into the identity-bound facts.
        ("k8s_container_restarts", [(150_000, 9.0)], {"k8s_container_name": "other"}),
    ]

    facts = split_audit._mechanism_facts(stored, mechanism)

    assert facts["periods"]["normal"] == {"count": 2, "min": 0.0, "max": 0.0, "high_count": 0}
    assert facts["periods"]["abnormal"]["high_count"] == 2
    assert facts["preserved"] is True


def test_mechanism_facts_fail_when_the_baseline_is_not_clear() -> None:
    mechanism = {
        "source_table": "cpu",
        "identity_column": None,
        "identity_value": None,
        "threshold": 1.0,
        "minimum_anomalous_observations": 1,
        "normal_window": (100, 200),
        "abnormal_window": (200, 300),
    }
    stored = [("cpu", [(150_000, 5.0), (250_000, 5.0)], {})]

    assert split_audit._mechanism_facts(stored, mechanism)["preserved"] is False


def test_trace_audit_rejects_the_unknown_service_name_fallback() -> None:
    spans = [_span(), _span()]
    object.__setattr__(spans[1], "service_name", "unknown")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"traces": [], "batches": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        audit = split_audit._audit_traces(
            client,
            "http://tempo",
            window=(1, 2),
            expected=spans,
            scope_name="openrca2-replay",
            search_timeout=0,
        )

    # RCA100's reader substitutes "unknown" when the archive has no service
    # name; a non-empty check alone would pass on that fabricated value.
    assert audit["service_names_declared"] is False


def test_the_trace_sample_prefers_the_traces_the_case_is_scored_on() -> None:
    def span(trace_id: str, service: str) -> TraceSpan:
        return TraceSpan(
            trace_id=trace_id,
            span_id="02" * 8,
            parent_span_id="",
            name="request",
            kind=2,
            start_time_unix_nano=1_500_000_000,
            end_time_unix_nano=1_600_000_000,
            service_name=service,
        )

    # The causal trace sorts last, so an arbitrary slice would miss it.
    spans = [span(f"{index:032x}", "noise") for index in range(30)]
    spans.append(span("f" * 32, "rate"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"traces": [], "batches": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        audit = split_audit._audit_traces(
            client,
            "http://tempo",
            window=(1, 2),
            expected=spans,
            scope_name="openrca2-replay",
            causal_services=frozenset({"rate"}),
            search_timeout=0,
        )

    assert audit["causal_traces_in_source"] == 1
    assert audit["causal_traces_sampled"] == 1
    assert audit["fidelity_sample_traces"] == split_audit.TRACE_FIDELITY_SAMPLE


def test_service_search_requires_a_causal_trace() -> None:
    causal = "1" * 32
    noncausal = "2" * 32
    spans = [_span(), _span()]
    object.__setattr__(spans[0], "trace_id", causal)
    object.__setattr__(spans[1], "trace_id", noncausal)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"traces": [{"traceID": noncausal}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = split_audit._causal_service_search(
            client,
            "http://tempo",
            window=(1, 2),
            services=frozenset({"frontend"}),
            expected=spans,
            causal_trace_ids={causal},
        )

    assert result["returned_outside_the_service"] == 0
    assert result["returned_causal_traces"] == 0
    assert result["reachable"] is False


def test_a_store_that_ignores_the_table_label_fails_the_separation_gate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A merged store answers every per-table query with every stream.
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {"stream": {"log_table": "events", "a": "1"}, "values": [["1", "x"]]},
                        {"stream": {"log_table": "alerts", "a": "2"}, "values": [["2", "y"]]},
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, _, foreign = split_audit.read_loki_logs(
            client,
            "http://loki",
            start_ns=0,
            end_ns=10,
            log_table="events",
            expected_count=2,
            expected_labels={"a"},
        )

    # Comparing how many tables were asked for with how many answered can only
    # ever be equal; this counts streams that came back under another table.
    assert foreign == 1
