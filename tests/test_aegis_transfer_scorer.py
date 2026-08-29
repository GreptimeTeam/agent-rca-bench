import json
from pathlib import Path

import pytest

from semantic_rca_bench.aegis_transfer_scorer import (
    DELAY_SCORER_FIXTURE,
    audit_transfer_scorer,
    canonical_api_runner_contract,
    evaluate_aegis_transfer_run,
    load_transfer_scorer_fixture,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    FaultCategory,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)


def _mechanism_query(parent_operator: str = "=") -> str:
    return f"""WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-19 09:52:38'
            AND c.timestamp < '2025-07-19 09:56:38' THEN 'normal'
           WHEN c.timestamp >= '2025-07-19 09:56:38'
            AND c.timestamp < '2025-07-19 10:00:38' THEN 'abnormal'
         END AS period,
         c.\"span_attributes.http.request.method\" AS client_method,
         s.\"span_attributes.http.request.method\" AS server_method
  FROM traces c
  JOIN traces s
    ON c.trace_id = s.trace_id
   AND s.parent_span_id {parent_operator} c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = 'ts-security-service'
    AND s.service_name = 'ts-order-other-service'
)
SELECT period, side, method, COUNT(*) AS span_count
FROM (
  SELECT period, 'server' AS side, server_method AS method FROM paired WHERE period = 'normal'
  UNION ALL
  SELECT period, 'client' AS side, client_method AS method FROM paired WHERE period = 'abnormal'
  UNION ALL
  SELECT period, 'server' AS side, server_method AS method FROM paired WHERE period = 'abnormal'
) evidence
GROUP BY period, side, method"""


def _mechanism_result(*, remove: tuple[str, str] | None = None) -> QueryResult:
    rows = [
        ["normal", "server", "GET", 57],
        ["abnormal", "client", "GET", 758],
        ["abnormal", "server", "OPTIONS", 758],
    ]
    if remove is not None:
        rows = [row for row in rows if tuple(row[:2]) != remove]
    return QueryResult(
        query_id="q01",
        columns=["period", "side", "method", "span_count"],
        rows=rows,
        elapsed_seconds=0,
    )


def _run(
    *,
    affected_component: str = "ts-security-service",
    causal_dependency: str | None = "ts-order-other-service",
    fault_type: str = "HTTP request method replacement",
    query_id: str = "q01",
    query: str | None = None,
    result: QueryResult | None = None,
    runner: AgentRunner = AgentRunner.API,
) -> AgentRun:
    result = result or _mechanism_result()
    return AgentRun(
        run_id="run",
        visibility=Visibility.RAW,
        model="deepseek-v4-flash",
        runner=runner,
        diagnosis=Diagnosis(
            affected_component=affected_component,
            causal_dependency=causal_dependency,
            fault_category=FaultCategory.OTHER,
            fault_type=fault_type,
            confidence=1,
            evidence=[Evidence(query_id=query_id, claim="paired span methods changed")],
            explanation="The paired client/server spans show the replacement.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query or _mechanism_query()},
                query_id="q01",
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=len(result.rows)),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def _transfer_audit() -> dict[str, object]:
    result = _mechanism_result()
    expected = {
        "normal_server_methods": {"GET": 57},
        "abnormal_client_methods": {"GET": 758},
        "abnormal_server_methods": {"OPTIONS": 758},
    }
    return {
        "mode": "aegis-transfer-no-model-audit",
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-001",
                "time_start": 1752918758,
                "time_end": 1752919238,
                "alert_time": 1752918998,
                "fault_taxonomy": [],
            },
            "normal_window": [1752918758, 1752918998],
            "abnormal_window": [1752918998, 1752919238],
            "declared_edge": ["ts-security-service", "ts-order-other-service"],
            "fault_type": "HTTPRequestReplaceMethod",
        },
        "mechanism_evidence": {
            "predicate": "source_declared_http_method_replacement",
            "query": _mechanism_query(),
            "result": result.model_dump(mode="json"),
            "normalized_result": expected,
            "expected_result": expected,
            "pass": True,
        },
        "no_model_gates": {"all_passed": True},
    }


def _delay_transfer_audit() -> dict[str, object]:
    query = """WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-20 12:32:50'
            AND c.timestamp < '2025-07-20 12:36:50' THEN 'normal'
           WHEN c.timestamp >= '2025-07-20 12:36:50'
            AND c.timestamp < '2025-07-20 12:40:49' THEN 'abnormal'
         END AS period,
         s.duration_nano AS server_duration_ns
  FROM traces c
  JOIN traces s
    ON c.trace_id = s.trace_id
   AND s.parent_span_id = c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = 'ts-route-plan-service'
    AND s.service_name = 'ts-travel2-service'
    AND s.span_name = 'POST /api/v1/travel2service/trips/left'
    AND c.timestamp >= '2025-07-20 12:32:50'
    AND c.timestamp < '2025-07-20 12:40:49'
)
SELECT period, COUNT(*) AS span_count, MAX(server_duration_ns) AS max_duration_ns
FROM paired
GROUP BY period
ORDER BY period"""
    result = QueryResult(
        query_id="q01",
        columns=["period", "span_count", "max_duration_ns"],
        rows=[["abnormal", 25, 3_252_068_825], ["normal", 37, 846_092_899]],
        elapsed_seconds=0,
    )
    expected = {
        "normal": {"count": 37, "max_duration_ns": 846_092_899},
        "abnormal": {"count": 25, "max_duration_ns": 3_252_068_825},
    }
    return {
        "mode": "aegis-transfer-no-model-audit",
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-002",
                "time_start": 1753014770,
                "time_end": 1753015249,
                "alert_time": 1753015010,
                "fault_taxonomy": [],
            },
            "source_mapping": {
                "agent_case_id": "aegis-transfer-002",
                "source_case": "ts8-ts-route-plan-service-request-delay-5dmjfm",
            },
            "normal_window": [1753014770, 1753015010],
            "abnormal_window": [1753015010, 1753015249],
            "declared_edge": ["ts-route-plan-service", "ts-travel2-service"],
            "fault_type": "HTTPRequestDelay",
        },
        "mechanism_evidence": {
            "predicate": "source_declared_http_delay_threshold",
            "declared_delay_ns": 3_070_000_000,
            "span_name": "POST /api/v1/travel2service/trips/left",
            "query": query,
            "result": result.model_dump(mode="json"),
            "normalized_result": expected,
            "expected_result": expected,
            "pass": True,
        },
        "no_model_gates": {"all_passed": True},
    }


def _delay_run(query: str | None = None) -> AgentRun:
    audit = _delay_transfer_audit()
    mechanism = audit["mechanism_evidence"]
    assert isinstance(mechanism, dict)
    result = QueryResult.model_validate(mechanism["result"])
    return AgentRun(
        run_id="run",
        visibility=Visibility.RAW,
        model="deepseek-v4-flash",
        runner=AgentRunner.API,
        diagnosis=Diagnosis(
            affected_component="ts-route-plan-service",
            causal_dependency="ts-travel2-service",
            fault_category=FaultCategory.DELAY,
            fault_type="HTTP request delay",
            confidence=1,
            evidence=[Evidence(query_id="q01", claim="paired span duration transition")],
            explanation="The paired server duration crosses the declared threshold.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query or str(mechanism["query"])},
                query_id="q01",
                output=result.model_copy(update={"query_id": "q01"}).model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def test_frozen_scorer_accepts_only_complete_directed_mechanism_evidence() -> None:
    fixture = load_transfer_scorer_fixture()

    evaluation = evaluate_aegis_transfer_run(_run(), fixture)

    assert evaluation.success
    assert evaluation.declared_edge_match
    assert evaluation.mechanism_evidence_match
    assert evaluation.rows_returned_through_evidence == 3


def test_delay_scorer_requires_both_windows_and_threshold_transition() -> None:
    fixture = load_transfer_scorer_fixture(DELAY_SCORER_FIXTURE)
    audit = audit_transfer_scorer(_delay_transfer_audit(), fixture, DELAY_SCORER_FIXTURE)

    assert audit["case_role"] == "measurement"
    assert audit["no_model_gates"]["all_passed"]
    assert audit["synthetic_regressions"]["canonical_positive"]["observed_success"]
    assert not audit["synthetic_regressions"]["missing_normal"]["observed_success"]
    assert not audit["synthetic_regressions"]["missing_abnormal"]["observed_success"]


def test_delay_scorer_normalizes_equivalent_iso_timestamp_literals() -> None:
    query = str(_delay_transfer_audit()["mechanism_evidence"]["query"])
    for source, replacement in (
        ("2025-07-20 12:32:50", "2025-07-20T12:32:50+00:00"),
        ("2025-07-20 12:36:50", "2025-07-20T12:36:50Z"),
        ("2025-07-20 12:40:49", "2025-07-20T12:40:49+00:00"),
    ):
        query = query.replace(source, replacement)

    evaluation = evaluate_aegis_transfer_run(
        _delay_run(query), load_transfer_scorer_fixture(DELAY_SCORER_FIXTURE)
    )

    assert evaluation.success
    assert evaluation.mechanism_evidence_match


def test_requested_or_superseded_calls_do_not_violate_execution_budget() -> None:
    fixture = load_transfer_scorer_fixture(DELAY_SCORER_FIXTURE)
    run = _delay_run().model_copy(
        update={
            "tool_calls_requested": 49,
            "rejected_tool_calls": [
                RejectedToolCall(
                    tool_name="execute_sql",
                    input={"query": "SELECT 1"},
                    error="superseded by final output",
                    reason_code="superseded_by_final_output",
                )
            ],
        }
    )

    evaluation = evaluate_aegis_transfer_run(run, fixture)

    assert evaluation.success
    assert evaluation.tool_budget_contract_match
    assert evaluation.correct_completion_tool_calls == 1


def test_delay_scorer_fixture_rejects_nontransitioning_threshold(tmp_path: Path) -> None:
    fixture = json.loads(DELAY_SCORER_FIXTURE.read_text())
    fixture["mechanism_evidence"]["threshold_ns"] = 4_000_000_000
    path = tmp_path / "scorer.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="threshold transition"):
        load_transfer_scorer_fixture(path)


def test_frozen_scorer_accepts_complete_mechanism_across_multiple_citations() -> None:
    run = _run()
    normal = _mechanism_result()
    normal = normal.model_copy(update={"rows": [normal.rows[0]]})
    abnormal = _mechanism_result().model_copy(
        update={"query_id": "q02", "rows": _mechanism_result().rows[1:]}
    )
    traces = [
        run.tool_calls[0].model_copy(
            update={
                "output": normal.model_dump(mode="json"),
                "database_load": DatabaseLoad(query_count=1, rows_returned=1),
            }
        ),
        run.tool_calls[0].model_copy(
            update={
                "query_id": "q02",
                "output": abnormal.model_dump(mode="json"),
                "database_load": DatabaseLoad(query_count=1, rows_returned=2),
            }
        ),
    ]
    assert run.diagnosis is not None
    diagnosis = run.diagnosis.model_copy(
        update={
            "evidence": [
                Evidence(query_id="q01", claim="normal paired server method"),
                Evidence(query_id="q02", claim="abnormal paired client and server methods"),
            ]
        }
    )
    run = run.model_copy(
        update={"diagnosis": diagnosis, "tool_calls": traces, "tool_calls_requested": 2}
    )

    evaluation = evaluate_aegis_transfer_run(run, load_transfer_scorer_fixture())

    assert evaluation.success
    assert evaluation.supporting_evidence_query_ids == ["q01", "q02"]
    assert evaluation.rows_returned_through_evidence == 3


@pytest.mark.parametrize(
    "run",
    [
        _run(affected_component="ts-order-other-service"),
        _run(causal_dependency=None),
        _run(fault_type="HTTP response body replacement"),
        _run(query_id="missing"),
        _run(query=_mechanism_query("<>")),
        _run(runner=AgentRunner.CLAUDE_SUBSCRIPTION),
        _run(result=_mechanism_result(remove=("normal", "server"))),
        _run(result=_mechanism_result(remove=("abnormal", "client"))),
        _run(result=_mechanism_result(remove=("abnormal", "server"))),
    ],
)
def test_frozen_scorer_rejects_incomplete_or_noncanonical_runs(run: AgentRun) -> None:
    assert not evaluate_aegis_transfer_run(run, load_transfer_scorer_fixture()).success


def test_no_model_scorer_audit_binds_source_audit_runner_and_negative_regressions() -> None:
    fixture = load_transfer_scorer_fixture()

    audit = audit_transfer_scorer(
        _transfer_audit(), fixture, Path("fixtures/reference/aegis-transfer-scorer.json")
    )

    assert audit["no_model_gates"] == {
        "source_transfer_audit_match": True,
        "canonical_runner_contract_match": True,
        "opaque_agent_input": True,
        "scorer_regressions": True,
        "all_passed": True,
    }
    assert all(item["pass"] for item in audit["synthetic_regressions"].values())


def test_no_model_scorer_audit_rejects_agent_facing_source_label() -> None:
    transfer = _transfer_audit()
    transfer["case"]["agent_facing"]["fault_taxonomy"] = ["HTTPRequestReplaceMethod"]

    audit = audit_transfer_scorer(
        transfer,
        load_transfer_scorer_fixture(),
        Path("fixtures/reference/aegis-transfer-scorer.json"),
    )

    assert not audit["no_model_gates"]["opaque_agent_input"]
    assert not audit["no_model_gates"]["all_passed"]


def test_source_audit_binding_excludes_instance_lifecycle_metadata() -> None:
    transfer = _transfer_audit()
    transfer["exclusive_instance"] = {
        "ports": {"http": 41000},
        "process_stopped_by_command": False,
    }
    before = source_transfer_audit_sha256(transfer)
    transfer["exclusive_instance"] = {
        "ports": {"http": 42000},
        "process_stopped_by_command": True,
    }

    assert source_transfer_audit_sha256(transfer) == before
    transfer["mechanism_evidence"]["pass"] = False
    assert source_transfer_audit_sha256(transfer) != before


def test_scorer_fixture_runner_contract_is_bound_to_code() -> None:
    fixture = load_transfer_scorer_fixture()

    assert fixture.case_role == "development"
    assert fixture.canonical_api_runner.model_dump(mode="json") == canonical_api_runner_contract(25)


def test_scorer_fixture_drift_fails_closed(tmp_path: Path) -> None:
    fixture = json.loads(Path("fixtures/reference/aegis-transfer-scorer.json").read_text())
    fixture["canonical_api_runner"]["max_tool_calls"] = 49
    path = tmp_path / "scorer.json"
    path.write_text(json.dumps(fixture))

    with pytest.raises(ValueError, match="runner contract drifted"):
        load_transfer_scorer_fixture(path)
