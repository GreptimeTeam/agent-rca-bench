from types import SimpleNamespace

import pytest

import semantic_rca_bench.discovery as discovery_module
from semantic_rca_bench.agent import StructuredAgentResult
from semantic_rca_bench.contracts import (
    AgentRunner,
    AgentUsage,
    DatabaseLoad,
    QueryResult,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.discovery import (
    DEVELOPMENT_FIXTURES,
    DiscoveryAgentRun,
    DiscoveryAnswer,
    canonical_evidence_query,
    discovery_task_prompt,
    evaluate_discovery_run,
    evidence_predicate_matches,
    fixture_for_source_case,
    run_discovery_agent,
)

MARKET = DEVELOPMENT_FIXTURES["openrca-market-node-write-io"]


def _canonical_result() -> QueryResult:
    return QueryResult(
        query_id="canonical",
        columns=["phase", "sample_count", "mean_value", "max_value"],
        rows=[
            ["baseline", 8, 15.9375, 64.5],
            ["incident", 3, 224.5, 502.5],
        ],
        elapsed_seconds=0.01,
    )


def _run(*, query: str | None = None, result: QueryResult | None = None) -> DiscoveryAgentRun:
    query = query or canonical_evidence_query(MARKET)
    result = result or _canonical_result()
    result = result.model_copy(update={"query_id": "q01"})
    return DiscoveryAgentRun(
        run_id="run",
        visibility=Visibility.TABLE_SEMANTICS,
        model="test-model",
        runner=AgentRunner.API,
        answer=DiscoveryAnswer(
            table=MARKET.target_table,
            evidence_query_id="q01",
            component=MARKET.component,
            signal=MARKET.signal,
            claim="The incident-window maximum exceeds the baseline maximum.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query},
                query_id="q01",
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2, max_concurrency=1),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0.1,
        responses=[],
        turn_limit=22,
        turn_limit_enforced=True,
    )


def test_frozen_fixture_is_selected_by_source_case() -> None:
    assert fixture_for_source_case(MARKET.source_case) == MARKET


def test_external_fixture_must_match_source_case(tmp_path) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(MARKET.model_dump_json())

    assert fixture_for_source_case(MARKET.source_case, str(fixture_path)) == MARKET
    with pytest.raises(ValueError, match="does not match"):
        fixture_for_source_case("another-case", str(fixture_path))


def test_canonical_query_uses_component_and_exact_frozen_windows() -> None:
    query = canonical_evidence_query(MARKET)

    assert 'FROM "system_io_w_s"' in query
    assert "\"cmdb_id\" = 'node-6'" in query
    assert "'2022-03-20 19:30:00'" in query
    assert "'2022-03-20 19:38:00'" in query
    assert "'2022-03-20 19:41:00'" in query


def test_canonical_query_quotes_mixed_case_identifiers() -> None:
    fixture = MARKET.model_copy(update={"target_table": "container_fs_reads_MB__dev_vda"})

    query = canonical_evidence_query(fixture)

    assert 'FROM "container_fs_reads_MB__dev_vda"' in query


def test_current_database_qualification_and_typed_timestamps_are_valid() -> None:
    run = _run()
    qualified = f"case_market_01.{MARKET.target_table}"
    answer = run.answer.model_copy(update={"table": qualified})
    query = str(run.tool_calls[0].input["query"])
    for timestamp in (
        "2022-03-20 19:30:00",
        "2022-03-20 19:38:00",
        "2022-03-20 19:41:00",
    ):
        query = query.replace(f"'{timestamp}'", f"TIMESTAMP '{timestamp}+00:00'")
    trace = run.tool_calls[0].model_copy(
        update={
            "input": {
                "query": query.replace(
                    f'FROM "{MARKET.target_table}"',
                    f'FROM "case_market_01"."{MARKET.target_table}"',
                )
            }
        }
    )
    run = run.model_copy(update={"answer": answer, "tool_calls": [trace]})

    evaluation = evaluate_discovery_run(
        run,
        MARKET,
        _canonical_result(),
        database="case_market_01",
    )

    assert evaluation.success


def test_other_database_qualification_is_rejected() -> None:
    run = _run()
    answer = run.answer.model_copy(update={"table": f"other.{MARKET.target_table}"})
    trace = run.tool_calls[0].model_copy(
        update={
            "input": {
                "query": str(run.tool_calls[0].input["query"]).replace(
                    f'FROM "{MARKET.target_table}"',
                    f'FROM "other"."{MARKET.target_table}"',
                )
            }
        }
    )
    run = run.model_copy(update={"answer": answer, "tool_calls": [trace]})

    evaluation = evaluate_discovery_run(
        run,
        MARKET,
        _canonical_result(),
        database="case_market_01",
    )

    assert not evaluation.table_match
    assert not evaluation.query_reads_only_target


def test_valid_discovery_run_matches_independent_canonical_result() -> None:
    evaluation = evaluate_discovery_run(_run(), MARKET, _canonical_result())

    assert evaluation.success
    assert evaluation.canonical_result_match
    assert evaluation.predicate_match
    assert evaluation.tool_calls_through_evidence == 1
    assert evaluation.rows_returned_through_evidence == 2
    assert evaluation.discovery_calls_through_evidence == 0


def test_shifted_evidence_window_is_rejected_even_when_values_match() -> None:
    query = canonical_evidence_query(MARKET).replace(
        "2022-03-20 19:38:00",
        "2022-03-20 19:37:00",
        1,
    )

    evaluation = evaluate_discovery_run(_run(query=query), MARKET, _canonical_result())

    assert not evaluation.success
    assert not evaluation.window_scope_valid


def test_query_without_component_scope_is_rejected() -> None:
    query = canonical_evidence_query(MARKET).replace("\"cmdb_id\" = 'node-6' AND ", "")

    evaluation = evaluate_discovery_run(_run(query=query), MARKET, _canonical_result())

    assert not evaluation.success
    assert not evaluation.component_scope_valid


def test_each_evidence_branch_requires_component_scope() -> None:
    query = canonical_evidence_query(MARKET).replace("\"cmdb_id\" = 'node-6' AND ", "", 1)

    evaluation = evaluate_discovery_run(_run(query=query), MARKET, _canonical_result())

    assert not evaluation.success
    assert not evaluation.component_scope_valid


def test_time_bounds_under_or_are_rejected() -> None:
    query = canonical_evidence_query(MARKET).replace(
        "\"greptime_timestamp\" >= '2022-03-20 19:30:00' AND ",
        "(\"greptime_timestamp\" >= '2022-03-20 19:30:00' OR 1 = 0) AND ",
        1,
    )

    evaluation = evaluate_discovery_run(_run(query=query), MARKET, _canonical_result())

    assert not evaluation.success
    assert not evaluation.window_scope_valid


def test_database_qualified_target_is_accepted() -> None:
    query = canonical_evidence_query(MARKET).replace(
        'FROM "system_io_w_s"',
        'FROM "case_market_01"."system_io_w_s"',
    )

    evaluation = evaluate_discovery_run(
        _run(query=query),
        MARKET,
        _canonical_result(),
        database="case_market_01",
    )

    assert evaluation.query_reads_only_target


def test_quoted_database_qualified_answer_is_accepted() -> None:
    run = _run()
    answer = run.answer.model_copy(update={"table": f'"case_market_01"."{MARKET.target_table}"'})

    evaluation = evaluate_discovery_run(
        run.model_copy(update={"answer": answer}),
        MARKET,
        _canonical_result(),
        database="case_market_01",
    )

    assert evaluation.table_match


def test_fabricated_citation_is_rejected() -> None:
    run = _run().model_copy(
        update={"answer": _run().answer.model_copy(update={"evidence_query_id": "q99"})}
    )

    evaluation = evaluate_discovery_run(run, MARKET, _canonical_result())

    assert not evaluation.success
    assert not evaluation.citation_valid


def test_cited_values_must_match_the_canonical_query() -> None:
    fabricated = _canonical_result().model_copy(
        update={
            "rows": [
                ["baseline", 8, 1.0, 1.0],
                ["incident", 3, 999.0, 999.0],
            ]
        }
    )

    evaluation = evaluate_discovery_run(
        _run(result=fabricated),
        MARKET,
        _canonical_result(),
    )

    assert not evaluation.success
    assert not evaluation.canonical_result_match


def test_truncated_evidence_is_rejected() -> None:
    truncated = _canonical_result().model_copy(update={"truncated": True})

    evaluation = evaluate_discovery_run(
        _run(result=truncated),
        MARKET,
        _canonical_result(),
    )

    assert not evaluation.success
    assert not evaluation.citation_valid


def test_market_predicate_uses_incident_maximum() -> None:
    assert evidence_predicate_matches(_canonical_result(), MARKET)


def test_predicate_accepts_the_frozen_minimum_ratio() -> None:
    result = _canonical_result().model_copy(
        update={
            "rows": [
                ["baseline", 2, 10.0, 10.0],
                ["incident", 2, 40.0, 40.0],
            ]
        }
    )

    assert evidence_predicate_matches(result, MARKET)


def test_discovery_prompt_exposes_task_contract_without_target_schema() -> None:
    prompt = discovery_task_prompt("case_market_01", MARKET, 12)

    assert MARKET.component in prompt
    assert MARKET.signal in prompt
    assert "2022-03-20T19:38:00+00:00" in prompt
    assert MARKET.target_table not in prompt
    assert MARKET.component_column not in prompt
    assert MARKET.value_column not in prompt


def test_discovery_api_runner_uses_fixed_budget_and_turn_limit(monkeypatch) -> None:
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return StructuredAgentResult(
            output={
                "table": MARKET.target_table,
                "evidence_query_id": "q01",
                "component": MARKET.component,
                "signal": MARKET.signal,
                "claim": "supported",
            },
            error=None,
            tool_calls=[],
            rejected_tool_calls=[],
            tool_calls_requested=0,
            tool_budget_exhausted=False,
            usage=AgentUsage(),
            elapsed_seconds=0.1,
            responses=[],
        )

    monkeypatch.setattr(discovery_module, "run_structured_api_agent", fake_run)

    run = run_discovery_agent(
        SimpleNamespace(client=SimpleNamespace(database="case_market_01")),
        MARKET,
        Visibility.TABLE_SEMANTICS,
        runner=AgentRunner.API,
        model="test-model",
    )

    assert captured["max_tool_calls"] == 12
    assert captured["max_turns"] == 22
    assert {tool["name"] for tool in captured["investigation_tools"]} == {
        "execute_sql",
        "describe_table",
        "search_table_semantics",
    }
    assert MARKET.target_table not in captured["user_prompt"]
    assert run.turn_limit == 22
    assert run.turn_limit_enforced


def test_discovery_subscription_records_unenforceable_turn_limit(monkeypatch) -> None:
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return StructuredAgentResult(
            output=None,
            error="runner unavailable",
            tool_calls=[],
            rejected_tool_calls=[],
            tool_calls_requested=0,
            tool_budget_exhausted=False,
            usage=AgentUsage(),
            elapsed_seconds=0.1,
            responses=[],
        )

    monkeypatch.setattr(discovery_module, "run_structured_subscription_agent", fake_run)

    run = run_discovery_agent(
        SimpleNamespace(client=SimpleNamespace(database="case_market_01")),
        MARKET,
        Visibility.RAW,
        runner=AgentRunner.CODEX_SUBSCRIPTION,
        model="test-model",
    )

    assert captured["max_tool_calls"] == 12
    assert "The only MCP server is named semantic_rca" in captured["user_prompt"]
    assert run.turn_limit is None
    assert not run.turn_limit_enforced
    assert run.error == "runner unavailable"
