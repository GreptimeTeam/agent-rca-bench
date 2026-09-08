import httpx
import pytest

from agent_rca_bench.contracts import ToolTrace
from agent_rca_bench.evidence import is_valid_evidence_trace
from agent_rca_bench.split_query import (
    NativeQueryError,
    NativeQueryResult,
    SplitQueryGateway,
    loki_rows,
    native_result,
    prometheus_query_plan,
    prometheus_rows,
    split_investigation_tools,
)


def test_prometheus_matrix_is_normalized_to_samples() -> None:
    columns, rows = prometheus_rows(
        "query_range",
        {
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "cpu", "service_name": "search"},
                        "values": [["1", "0.1"], ["2", "0.9"]],
                    }
                ],
            }
        },
    )

    # One row per sample, so max_items still caps samples, but each label is a
    # column rather than a map repeated on every row.
    assert columns == ["__name__", "service_name", "timestamp", "value"]
    assert rows == [["cpu", "search", "1", "0.1"], ["cpu", "search", "2", "0.9"]]


def test_loki_streams_are_normalized_to_log_entries() -> None:
    columns, rows = loki_rows(
        "query_range",
        {
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service_name": "checkout"},
                        "values": [["10", "failed"], ["11", "recovered"]],
                    }
                ],
            }
        },
    )

    assert columns == ["service_name", "timestamp", "line"]
    assert rows[0] == ["checkout", "10", "failed"]


def test_native_result_marks_client_side_truncation() -> None:
    result = native_result(
        "prometheus",
        "query_range",
        ["value"],
        [[1], [2], [3]],
        0.1,
        2,
    )

    assert isinstance(result, NativeQueryResult)
    assert result.returned_items == 2
    assert result.rows == [[1], [2]]
    assert result.truncated is True
    assert result.result_bytes > 0


def test_split_tool_surface_contains_only_native_backends() -> None:
    tools = split_investigation_tools()

    assert [tool["name"] for tool in tools] == [
        "query_metrics",
        "query_logs",
        "query_traces",
    ]


def test_native_data_query_is_citable_but_metadata_discovery_is_not() -> None:
    output = native_result(
        "prometheus",
        "query_range",
        ["value"],
        [[1]],
        0.1,
        10,
    ).model_copy(update={"query_id": "q01"})
    trace = ToolTrace(
        tool_name="query_metrics",
        input={"operation": "query_range"},
        query_id="q01",
        output=output.model_dump(mode="json"),
    )

    assert is_valid_evidence_trace([trace]) is True
    trace.output["operation"] = "metadata"
    assert is_valid_evidence_trace([trace]) is False


def test_loki_and_tempo_can_report_truncation_at_all() -> None:
    """A backend that honours `limit` must still be able to set `truncated`."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        requested.append(str(request.url.path))
        if "loki" in str(request.url.path):
            values = [[str(index), f"line-{index}"] for index in range(limit)]
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "streams", "result": [{"stream": {}, "values": values}]},
                },
            )
        traces = [{"traceID": f"{index:032x}"} for index in range(limit)]
        return httpx.Response(200, json={"traces": traces})

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    logs = gateway.execute_tool(
        "query_logs",
        {"operation": "query_range", "query": '{a="b"}', "start": "1", "end": "2", "max_items": 3},
    )
    traces = gateway.execute_tool(
        "query_traces",
        {"operation": "search", "query": "{ true }", "start": "1", "end": "2", "max_items": 3},
    )

    assert logs["truncated"] is True
    assert logs["returned_items"] == 3
    assert traces["truncated"] is True
    assert traces["returned_items"] == 3


def test_tempo_search_converts_declared_rfc3339_times_to_epoch_seconds() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"traces": []})

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    gateway.execute_tool(
        "query_traces",
        {
            "operation": "search",
            "query": '{ resource.service.name = "search" }',
            "start": "2026-05-02T00:55:19Z",
            "end": "2026-05-02T01:05:19+00:00",
        },
    )

    assert requests[0].url.params["start"] == "1777683319"
    assert requests[0].url.params["end"] == "1777683919"


def test_tempo_get_trace_passes_the_declared_time_window() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"batches": []})

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    gateway.execute_tool(
        "query_traces",
        {
            "operation": "get_trace",
            "trace_id": "ab" * 16,
            "start": "2026-05-02T00:55:19Z",
            "end": "2026-05-02T01:05:19+00:00",
        },
    )

    assert requests[0].url.params["start"] == "1777683319"
    assert requests[0].url.params["end"] == "1777683919"


def test_prometheus_label_values_forwards_the_series_selector() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "data": ["cpu"]})

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    gateway.execute_tool(
        "query_metrics",
        {
            "operation": "label_values",
            "label": "__name__",
            "match": "cpu",
            "start": "1",
            "end": "2",
        },
    )

    assert requests[0].url.params["match[]"] == "cpu"


def test_prometheus_query_rejects_range_arguments_instead_of_ignoring_them() -> None:
    with pytest.raises(
        NativeQueryError,
        match=r"unsupported arguments for Prometheus query: start",
    ):
        prometheus_query_plan({"operation": "query", "query": "cpu", "start": "1"})


def test_split_gateway_returns_backend_error_detail_without_endpoint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid TraceQL: unexpected token")

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(
        NativeQueryError,
        match=r"tempo query failed \(400\): invalid TraceQL: unexpected token",
    ):
        gateway.execute_tool(
            "query_traces",
            {
                "operation": "search",
                "query": "{ true }",
                "start": "1",
                "end": "2",
            },
        )


def test_a_result_at_the_cap_is_not_reported_as_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The backend holds exactly `max_items`, one fewer than requested.
        values = [[str(index), f"line-{index}"] for index in range(3)]
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "streams", "result": [{"stream": {}, "values": values}]},
            },
        )

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    logs = gateway.execute_tool(
        "query_logs",
        {"operation": "query_range", "query": '{a="b"}', "start": "1", "end": "2", "max_items": 3},
    )

    assert logs["truncated"] is False
    assert logs["returned_items"] == 3


def test_the_split_gateway_reports_no_database_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": ["a", "b", "c"]})

    gateway = SplitQueryGateway(
        prometheus_endpoint="http://prometheus",
        loki_endpoint="http://loki",
        tempo_endpoint="http://tempo",
    )
    gateway.http = httpx.Client(transport=httpx.MockTransport(handler))

    with gateway.measure_query_load() as load:
        result = gateway.execute_tool("query_metrics", {"operation": "labels"})

    assert result["returned_items"] == 3
    # A Prometheus sample is not a GreptimeDB row. Reporting zero would read as
    # an investigation that read nothing; null says the endpoint does not apply.
    assert load.rows_returned is None
    assert load.query_count == 1
    assert load.model_dump(mode="json")["rows_returned"] is None


def test_a_log_line_carries_its_correlation_ids_to_the_model() -> None:
    columns, rows = loki_rows(
        "query_range",
        {
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"log_table": "logs", "service_name": "user"},
                        "values": [
                            ["1", "boom", {"trace_id": "abc", "span_id": "def"}],
                            ["2", "quiet"],
                        ],
                    }
                ],
            }
        },
    )

    # GreptimeDB holds trace_id and span_id as columns. Loki carries them as
    # structured metadata, and the split arm needs them to reach the trace.
    assert columns == ["log_table", "service_name", "trace_id", "span_id", "timestamp", "line"]
    assert rows[0] == ["logs", "user", "abc", "def", "1", "boom"]
    # An entry without structured metadata leaves those columns empty rather
    # than shifting the row.
    assert rows[1] == ["logs", "user", None, None, "2", "quiet"]


def test_a_label_named_like_a_result_column_is_escaped_not_refused() -> None:
    columns, rows = prometheus_rows(
        "query_range",
        {
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"value": "cpu", "timestamp": "noon", "value_": "taken"},
                        "values": [["1", "0.1"]],
                    }
                ],
            }
        },
    )

    assert columns == ["value", "timestamp", "value_", "timestamp_", "value__"]
    assert rows == [["cpu", "noon", "taken", "1", "0.1"]]


def test_prometheus_series_uses_the_same_columns_as_every_other_result() -> None:
    columns, rows = prometheus_rows(
        "series",
        {
            "data": [
                {"__name__": "cpu", "pod": "a"},
                {"__name__": "cpu", "pod": "b", "node": "n1"},
            ]
        },
    )

    assert columns == ["__name__", "pod", "node"]
    assert rows == [["cpu", "a", None], ["cpu", "b", "n1"]]
