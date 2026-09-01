from types import SimpleNamespace

import pytest

import semantic_rca_bench.graph_benchmark as graph_module
from semantic_rca_bench.agent import StructuredAgentResult
from semantic_rca_bench.contracts import (
    AgentRunner,
    AgentUsage,
    ApiTransport,
    DatabaseLoad,
    QueryResult,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.graph_benchmark import (
    DEVELOPMENT_GRAPH_FIXTURES,
    GraphAgentRun,
    GraphAnswer,
    canonical_graph_query,
    canonical_trace_query,
    edge_results_match,
    evaluate_graph_run,
    failed_graph_run,
    fixture_for_source_case,
    graph_task_prompt,
    run_graph_agent,
    validate_source_window,
)

RCA100 = DEVELOPMENT_GRAPH_FIXTURES["rca100-t002-frontend-callee-errors"]


def test_failed_graph_run_records_configured_api_turn_limit() -> None:
    run = failed_graph_run(
        Visibility.SEMANTIC_GRAPH,
        AgentRunner.API,
        "test-model",
        "provider unavailable",
        max_tool_calls=3,
    )

    assert run.turn_limit == 13
    assert run.turn_limit_enforced


def _canonical_result() -> QueryResult:
    return QueryResult(
        query_id="canonical",
        columns=[
            "src_type",
            "src_id",
            "dst_type",
            "dst_id",
            "rel_type",
            "provenance",
            "confidence",
            "request_count",
            "error_count",
        ],
        rows=[
            ["service", "frontend", "service", "ad", "calls", "trace", 1.0, 608, 0],
            ["service", "frontend", "service", "cart", "calls", "trace", 1.0, 9423, 1],
            [
                "service",
                "frontend",
                "service",
                "checkout",
                "calls",
                "trace",
                1.0,
                3881,
                0,
            ],
            [
                "service",
                "frontend",
                "service",
                "product-catalog",
                "calls",
                "trace",
                1.0,
                13634,
                0,
            ],
        ],
        elapsed_seconds=0.1,
    )


def _answer() -> GraphAnswer:
    return GraphAnswer(
        src_type="service",
        src_id="frontend",
        dst_type="service",
        dst_id="cart",
        rel_type="calls",
        provenance="trace",
        request_count=9423,
        error_count=1,
        evidence_query_id="q01",
        claim="cart is the only direct callee with an error.",
    )


def _run(visibility: Visibility, trace: ToolTrace) -> GraphAgentRun:
    return GraphAgentRun(
        run_id="run",
        visibility=visibility,
        model="test-model",
        runner=AgentRunner.API,
        answer=_answer(),
        tool_calls=[trace],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0.1,
        responses=[],
        turn_limit=22,
        turn_limit_enforced=True,
    )


def _graph_trace(**argument_updates: object) -> ToolTrace:
    arguments = {
        "view": "relationships",
        "src_type": "service",
        "src_id": "frontend",
        "dst_type": "service",
        "rel_type": "calls",
        "provenance": "trace",
        "limit": 200,
        **argument_updates,
    }
    result = _canonical_result().model_copy(update={"query_id": "q01"})
    return ToolTrace(
        tool_name="query_semantic_graph",
        input=arguments,
        query_id="q01",
        output=result.model_dump(mode="json"),
        database_load=DatabaseLoad(query_count=1, rows_returned=4, max_concurrency=1),
    )


def _sql_trace(query: str | None = None) -> ToolTrace:
    canonical = _canonical_result()
    result = QueryResult(
        query_id="q01",
        columns=[column for column in canonical.columns if column != "confidence"],
        rows=[row[:6] + row[7:] for row in canonical.rows],
        elapsed_seconds=canonical.elapsed_seconds,
    )
    return ToolTrace(
        tool_name="execute_sql",
        input={"query": query or canonical_trace_query(RCA100)},
        query_id="q01",
        output=result.model_dump(mode="json"),
        database_load=DatabaseLoad(query_count=1, rows_returned=4, max_concurrency=1),
    )


def test_frozen_graph_fixture_is_selected_and_minute_aligned() -> None:
    assert fixture_for_source_case("t002") == RCA100
    validate_source_window(RCA100, 1_776_913_177, 1_776_913_743)

    with pytest.raises(ValueError, match="start"):
        validate_source_window(RCA100, 1_776_913_201, 1_776_913_743)


def test_external_graph_fixture_must_match_source_case(tmp_path) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(RCA100.model_dump_json())

    assert fixture_for_source_case("t002", str(fixture_path)) == RCA100
    with pytest.raises(ValueError, match="does not match"):
        fixture_for_source_case("t003", str(fixture_path))


def test_canonical_queries_use_half_open_window_and_complete_caller_scope() -> None:
    trace_query = canonical_trace_query(RCA100)
    graph_query = canonical_graph_query("case_04", RCA100)

    assert "c.service_name = 'frontend'" in trace_query
    assert "c.trace_id = s.trace_id" in trace_query
    assert "s.parent_span_id = c.span_id" in trace_query
    assert "c.timestamp >= '2026-04-23 03:00:00'" in trace_query
    assert "c.timestamp < '2026-04-23 03:10:00'" in trace_query
    assert "observed_at >= '2026-04-23 03:00:00'" in graph_query
    assert "observed_at < '2026-04-23 03:10:00'" in graph_query
    assert "dst_id =" not in graph_query
    assert "SUM(unmatched_count) AS unmatched_count" in graph_query
    assert "MAX(duration_max) AS duration_max" in graph_query


def test_edge_result_equality_ignores_unscored_duration_roundoff() -> None:
    left = _canonical_result()
    right = left.model_copy(
        update={
            "rows": [[*row[:6], row[6], row[7], row[8], 4.831521360000001] for row in left.rows],
            "columns": [*left.columns, "duration_sum"],
        }
    )

    assert edge_results_match(left, right)


def test_edge_result_equality_rejects_truncation() -> None:
    canonical = _canonical_result()

    assert not edge_results_match(canonical, canonical.model_copy(update={"truncated": True}))


def test_graph_prompt_hides_winner_and_physical_trace_table() -> None:
    prompt = graph_task_prompt("case_04", RCA100, 12)

    assert RCA100.caller in prompt
    assert RCA100.expected_callee not in prompt
    assert RCA100.trace_table not in prompt
    assert "2026-04-23T03:00:00+00:00" in prompt


def test_valid_graph_evidence_matches_complete_canonical_edge_set() -> None:
    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, _graph_trace()),
        RCA100,
        _canonical_result(),
    )

    assert evaluation.success
    assert evaluation.answer_match
    assert evaluation.evidence_scope_valid
    assert evaluation.evidence_result_match
    assert evaluation.rows_returned_through_evidence == 4


def test_graph_evidence_prefiltered_to_winner_is_rejected() -> None:
    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, _graph_trace(dst_id="cart")),
        RCA100,
        _canonical_result(),
    )

    assert not evaluation.success
    assert not evaluation.evidence_scope_valid


def test_graph_destination_type_can_be_proven_by_the_exact_result_set() -> None:
    trace = _graph_trace()
    trace = trace.model_copy(
        update={"input": {key: value for key, value in trace.input.items() if key != "dst_type"}}
    )

    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, trace),
        RCA100,
        _canonical_result(),
    )

    assert evaluation.success


def test_graph_source_type_can_be_proven_by_the_exact_result_set() -> None:
    trace = _graph_trace()
    trace = trace.model_copy(
        update={"input": {key: value for key, value in trace.input.items() if key != "src_type"}}
    )

    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, trace),
        RCA100,
        _canonical_result(),
    )

    assert evaluation.success


def test_graph_evidence_without_caller_scope_is_rejected() -> None:
    trace = _graph_trace()
    trace = trace.model_copy(
        update={"input": {key: value for key, value in trace.input.items() if key != "src_id"}}
    )

    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, trace),
        RCA100,
        _canonical_result(),
    )

    assert not evaluation.success
    assert not evaluation.evidence_scope_valid


def test_valid_trace_self_join_matches_complete_canonical_edge_set() -> None:
    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, _sql_trace()),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert evaluation.success
    assert evaluation.evidence_scope_valid


def test_valid_trace_scope_accepts_destination_grouped_by_projection_ordinal() -> None:
    query = canonical_trace_query(RCA100).replace(
        "GROUP BY s.service_name", "GROUP BY 1, 2, 3, 4, 5, 6"
    )

    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, _sql_trace(query)),
        RCA100,
        _canonical_result(),
    )

    assert evaluation.success
    assert evaluation.evidence_scope_valid


@pytest.mark.parametrize(
    "query",
    [
        """
        SELECT 'service' AS src_type, c.service_name AS src_id,
               'service' AS dst_type, s.service_name AS dst_id,
               'calls' AS rel_type, 'trace' AS provenance,
               COUNT(*) AS request_count,
               SUM(CASE WHEN s.span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END)
                   AS error_count
        FROM (
          SELECT trace_id, span_id, service_name
          FROM case_04.traces
          WHERE span_kind = 'SPAN_KIND_CLIENT'
            AND service_name = 'frontend'
            AND timestamp >= '2026-04-23 03:00:00'
            AND timestamp < '2026-04-23 03:10:00'
        ) c
        JOIN (
          SELECT trace_id, parent_span_id, service_name, span_status_code
          FROM case_04.traces
          WHERE span_kind = 'SPAN_KIND_SERVER'
        ) s ON s.trace_id = c.trace_id AND s.parent_span_id = c.span_id
        GROUP BY 1, 2, 3, 4, 5, 6
        """,
        """
        WITH client_spans AS (
          SELECT trace_id, span_id, timestamp
          FROM case_04.traces
          WHERE service_name = 'frontend'
            AND span_kind = 'SPAN_KIND_CLIENT'
            AND timestamp >= '2026-04-23T03:00:00+00:00'::timestamp
            AND timestamp < '2026-04-23T03:10:00+00:00'::timestamp
        ), paired AS (
          SELECT c.trace_id, s.service_name AS dst_service, s.span_status_code
          FROM client_spans c
          JOIN case_04.traces s
            ON s.trace_id = c.trace_id
           AND s.parent_span_id = c.span_id
           AND s.span_kind = 'SPAN_KIND_SERVER'
        )
        SELECT 'service' AS src_type, 'frontend' AS src_id,
               'service' AS dst_type, dst_service AS dst_id,
               'calls' AS rel_type, 'trace' AS provenance,
               COUNT(*) AS request_count,
               COUNT(*) FILTER (WHERE span_status_code = 'STATUS_CODE_ERROR') AS error_count
        FROM paired
        GROUP BY dst_service
        """,
    ],
)
def test_valid_trace_scope_accepts_derived_tables(query: str) -> None:
    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, _sql_trace(query)),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert evaluation.success
    assert evaluation.evidence_scope_valid

    hardcoded = query.replace("s.service_name AS dst_id", "'cart' AS dst_id").replace(
        "dst_service AS dst_id", "'cart' AS dst_id"
    )
    hardcoded = hardcoded.replace("COUNT(*) AS request_count", "9423 AS request_count")
    self_trace_join = query.replace("s.trace_id = c.trace_id", "c.trace_id = c.trace_id")
    self_parent_join = query.replace("s.parent_span_id = c.span_id", "s.parent_span_id = s.span_id")
    constant_error_count = query.replace(
        "SUM(CASE WHEN s.span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END)",
        "0",
    ).replace(
        "COUNT(*) FILTER (WHERE span_status_code = 'STATUS_CODE_ERROR')",
        "0",
    )
    for invalid in (
        hardcoded,
        self_trace_join,
        self_parent_join,
        constant_error_count,
    ):
        invalid_evaluation = evaluate_graph_run(
            _run(Visibility.RAW, _sql_trace(invalid)),
            RCA100,
            _canonical_result(),
            database="case_04",
        )
        assert not invalid_evaluation.success
        assert not invalid_evaluation.evidence_scope_valid


def test_graph_assignment_keeps_valid_raw_verification_in_intention_to_treat() -> None:
    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, _sql_trace()),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert evaluation.success
    assert evaluation.citation_valid
    assert evaluation.evidence_scope_valid
    assert evaluation.evidence_result_match


def test_trace_query_without_parent_child_join_is_rejected() -> None:
    query = canonical_trace_query(RCA100).replace(
        " AND s.parent_span_id = c.span_id",
        "",
    )
    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, _sql_trace(query)),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert not evaluation.success
    assert not evaluation.evidence_scope_valid


def test_trace_time_bounds_under_or_are_rejected() -> None:
    query = canonical_trace_query(RCA100).replace(
        "c.timestamp >= '2026-04-23 03:00:00'",
        "(c.timestamp >= '2026-04-23 03:00:00' OR 1 = 0)",
    )
    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, _sql_trace(query)),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert not evaluation.success
    assert not evaluation.evidence_scope_valid


def test_trace_window_on_server_alias_does_not_scope_the_client() -> None:
    query = (
        canonical_trace_query(RCA100)
        .replace("c.timestamp >=", "s.timestamp >=")
        .replace(
            "c.timestamp <",
            "s.timestamp <",
        )
    )
    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, _sql_trace(query)),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert not evaluation.success
    assert not evaluation.evidence_scope_valid


def test_citation_with_incomplete_edge_set_is_rejected() -> None:
    trace = _graph_trace()
    output = QueryResult.model_validate(trace.output).model_copy(
        update={"rows": QueryResult.model_validate(trace.output).rows[:2]}
    )
    trace = trace.model_copy(update={"output": output.model_dump(mode="json")})

    evaluation = evaluate_graph_run(
        _run(Visibility.SEMANTIC_GRAPH, trace),
        RCA100,
        _canonical_result(),
    )

    assert not evaluation.success
    assert not evaluation.evidence_result_match


def test_trace_citation_with_extra_columns_is_rejected() -> None:
    trace = _sql_trace()
    output = QueryResult.model_validate(trace.output)
    output = output.model_copy(
        update={
            "columns": [*output.columns, "debug"],
            "rows": [[*row, "extra"] for row in output.rows],
        }
    )
    trace = trace.model_copy(update={"output": output.model_dump(mode="json")})

    evaluation = evaluate_graph_run(
        _run(Visibility.RAW, trace),
        RCA100,
        _canonical_result(),
        database="case_04",
    )

    assert not evaluation.success
    assert not evaluation.evidence_result_match


def test_graph_api_runner_uses_graph_tool_and_fixed_turn_limit(monkeypatch) -> None:
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return StructuredAgentResult(
            output=_answer().model_dump(mode="json"),
            error=None,
            tool_calls=[],
            rejected_tool_calls=[],
            tool_calls_requested=0,
            tool_budget_exhausted=False,
            usage=AgentUsage(),
            elapsed_seconds=0.1,
            responses=[],
        )

    monkeypatch.setattr(graph_module, "run_structured_api_agent", fake_run)
    coverage = {"graph": {"status": "relational", "distinct_relationship_count": 14}}

    run = run_graph_agent(
        SimpleNamespace(client=SimpleNamespace(database="case_04")),
        RCA100,
        Visibility.SEMANTIC_GRAPH,
        coverage,
        runner=AgentRunner.API,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
    )

    assert captured["max_tool_calls"] == 12
    assert captured["max_turns"] == 22
    assert captured["max_output_tokens"] == 4096
    assert "query_semantic_graph" in {tool["name"] for tool in captured["investigation_tools"]}
    assert RCA100.expected_callee not in captured["user_prompt"]
    assert run.turn_limit == 22
