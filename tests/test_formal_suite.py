import copy
import json
from pathlib import Path

import pytest

from agent_rca_bench.contracts import (
    AgentRunner,
    AgentUsage,
    ApiTransport,
    DatabaseLoad,
    QueryResult,
    ToolTrace,
    Visibility,
)
from agent_rca_bench.discovery import (
    DiscoveryAgentRun,
    DiscoveryAnswer,
    DiscoveryAudit,
    DiscoveryFixture,
    canonical_evidence_query,
    evaluate_discovery_run,
)
from agent_rca_bench.formal_suite import (
    build_micro_preflight_report,
    canonical_sha256,
    micro_source_semantic_sha256,
    validate_micro_report,
)
from agent_rca_bench.formal_suite_protocol import (
    DEFAULT_SUITE_PROTOCOL_FIXTURE,
    EXTENSION_SUITE_PROTOCOL_FIXTURE,
    load_formal_suite_protocol,
    micro_schedule,
)
from agent_rca_bench.formal_suite_release import (
    build_micro_measurement_artifact,
    score_public_micro_run,
    validate_micro_measurement_artifact,
)
from agent_rca_bench.graph_benchmark import (
    GraphAgentRun,
    GraphAnswer,
    GraphAudit,
    GraphFixture,
    canonical_graph_query,
    canonical_trace_query,
    evaluate_graph_run,
)


def _source_audits() -> list[dict[str, object]]:
    suite, _ = load_formal_suite_protocol()
    audits = []
    for index, case in enumerate(suite.micro_cases):
        fixture_path = Path(case.fixture)
        if case.benchmark == "discovery":
            fixture = DiscoveryFixture.model_validate_json(fixture_path.read_text())
            canonical_result = QueryResult(
                query_id=f"runtime-{index}",
                columns=["phase", "sample_count", "mean_value", "max_value"],
                rows=[
                    ["baseline", 2, 1.0, 1.0],
                    ["incident", 2, fixture.minimum_ratio + 1, fixture.minimum_ratio + 1],
                ],
                elapsed_seconds=0.1,
            )
            task_audit = DiscoveryAudit(
                fixture_id=fixture.fixture_id,
                canonical_query=canonical_evidence_query(fixture),
                canonical_result=canonical_result,
                predicate_match=True,
                catalog_query=fixture.signal,
                catalog_matched_table_count=1,
                catalog_target_rank=1,
                catalog_target_in_top_five=True,
                catalog_top_five=[fixture.target_table],
            ).model_dump(mode="json")
        else:
            fixture = GraphFixture.model_validate_json(fixture_path.read_text())
            trace_columns = [
                "src_type",
                "src_id",
                "dst_type",
                "dst_id",
                "rel_type",
                "provenance",
                "request_count",
                "error_count",
            ]
            trace_row = [
                "service",
                fixture.caller,
                "service",
                fixture.expected_callee,
                "calls",
                "trace",
                fixture.expected_request_count,
                fixture.expected_error_count,
            ]
            trace_result = QueryResult(
                query_id=f"runtime-trace-{index}",
                columns=trace_columns,
                rows=[trace_row],
                elapsed_seconds=0.1,
            )
            graph_result = QueryResult(
                query_id=f"runtime-graph-{index}",
                columns=[*trace_columns[:6], "confidence", *trace_columns[6:]],
                rows=[[*trace_row[:6], 1.0, *trace_row[6:]]],
                elapsed_seconds=0.1,
            )
            task_audit = GraphAudit(
                fixture_id=fixture.fixture_id,
                trace_query=canonical_trace_query(fixture),
                trace_result=trace_result,
                graph_query=canonical_graph_query(f"suite_case_{index + 1:02d}", fixture),
                graph_result=graph_result,
                edge_sets_match=True,
                unique_winner=True,
                expected_winner_match=True,
            ).model_dump(mode="json")
        audits.append(
            {
                "source_audit_schema_version": 1,
                "mode": "semantic-rca-micro-no-model-audit",
                "benchmark": case.benchmark,
                "source_case": case.source_case,
                "fixture": case.fixture,
                "fixture_sha256": case.fixture_sha256,
                "dataset_revision": "dataset-revision",
                "adapter_revision": "adapter-revision",
                "mirror_revision": None,
                "greptimedb": {
                    "head": suite.greptimedb_revision,
                    "branch": "branch",
                    "build_profile": suite.greptimedb_build_profile,
                    "binary_version": "version",
                    "binary_sha256": "binary-sha256",
                },
                "exclusive_instance": {
                    "database": f"suite_case_{index + 1:02d}",
                    "process_stopped_by_command": True,
                },
                "source": {},
                "ingestion": {},
                "validation": {},
                "semantic_coverage": {},
                "task_audit": task_audit,
                "no_model_gates": {"all_passed": True},
            }
        )
    return audits


def test_formal_suite_freezes_every_micro_and_transfer_cell() -> None:
    suite, transfer = load_formal_suite_protocol()
    schedule = micro_schedule(suite, transfer)

    assert len(schedule) == 128
    assert suite.expected_transfer_cells == 336
    assert suite.expected_total_cells == 464
    assert {cell["model"] for cell in schedule} == {model.model for model in transfer.models}
    for source_case in {str(cell["source_case"]) for cell in schedule}:
        cells = [cell for cell in schedule if cell["source_case"] == source_case]
        assert len(cells) == 4 * len(transfer.models)
        for model in transfer.models:
            model_cells = [cell for cell in cells if cell["model"] == model.model]
            assert [cell["visibility"] for cell in model_cells] == [
                "raw",
                "semantic_graph",
                "semantic_graph",
                "raw",
            ]


def test_extension_suite_schedules_only_new_model_micro_cells() -> None:
    suite, transfer = load_formal_suite_protocol(EXTENSION_SUITE_PROTOCOL_FIXTURE)
    schedule = micro_schedule(suite, transfer)

    assert len(schedule) == suite.expected_micro_cells == 64
    assert suite.expected_transfer_cells == 168
    assert suite.expected_total_cells == 232
    assert {cell["model"] for cell in schedule} == {
        "gemini-3.8-flash",
        "qwen3.8-max-0902",
    }


def test_micro_preflight_has_no_provider_calls_and_validates() -> None:
    suite, transfer = load_formal_suite_protocol()
    report = build_micro_preflight_report(
        suite,
        transfer,
        DEFAULT_SUITE_PROTOCOL_FIXTURE,
        _source_audits(),
    )

    assert report["authorization"]["preflight_calls_provider"] is False
    assert report["execution"] == {
        "completed_runs": 0,
        "expected_runs": 128,
        "remaining_runs": 128,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "complete": False,
    }
    validate_micro_report(report, suite, transfer, DEFAULT_SUITE_PROTOCOL_FIXTURE)


def test_micro_source_hash_ignores_runtime_query_metadata() -> None:
    left = _source_audits()[0]
    right = copy.deepcopy(left)
    result = right["task_audit"]["canonical_result"]
    result["query_id"] = "another-runtime-id"
    result["elapsed_seconds"] = 99.0

    assert micro_source_semantic_sha256(left) == micro_source_semantic_sha256(right)


def test_graph_source_hash_uses_scored_edge_projection() -> None:
    left = _source_audits()[6]
    right = copy.deepcopy(left)
    right["task_audit"]["graph_result"]["rows"][0].extend([0.07874431600000001])
    left["task_audit"]["graph_result"]["columns"].append("duration_sum")
    left["task_audit"]["graph_result"]["rows"][0].extend([0.078744316])
    right["task_audit"]["graph_result"]["columns"].append("duration_sum")

    assert micro_source_semantic_sha256(left) == micro_source_semantic_sha256(right)

    right["task_audit"]["graph_result"]["rows"][0][7] += 1
    assert micro_source_semantic_sha256(left) != micro_source_semantic_sha256(right)


def test_micro_report_rejects_nonprefix_completed_cell() -> None:
    suite, transfer = load_formal_suite_protocol()
    report = build_micro_preflight_report(
        suite,
        transfer,
        DEFAULT_SUITE_PROTOCOL_FIXTURE,
        _source_audits(),
    )
    report["runs"].append({**report["schedule"][1]})
    report["execution"]["completed_runs"] = 1
    report["execution"]["remaining_runs"] = 191

    with pytest.raises(ValueError, match="exact schedule prefix"):
        validate_micro_report(report, suite, transfer, DEFAULT_SUITE_PROTOCOL_FIXTURE)


def test_micro_preflight_rejects_source_case_reordering() -> None:
    suite, transfer = load_formal_suite_protocol()
    sources = _source_audits()
    sources[0], sources[1] = sources[1], sources[0]

    with pytest.raises(ValueError, match="suite case order"):
        build_micro_preflight_report(
            suite,
            transfer,
            DEFAULT_SUITE_PROTOCOL_FIXTURE,
            sources,
        )


def test_micro_preflight_rejects_greptimedb_build_profile_drift() -> None:
    suite, transfer = load_formal_suite_protocol()
    sources = _source_audits()
    sources[0]["greptimedb"]["build_profile"] = "debug"

    with pytest.raises(ValueError, match="frozen gates"):
        build_micro_preflight_report(
            suite,
            transfer,
            DEFAULT_SUITE_PROTOCOL_FIXTURE,
            sources,
        )


def _digest(columns: list[str], rows: list[list[object]]) -> str:
    return canonical_sha256({"columns": columns, "rows": rows, "truncated": False})


def test_public_discovery_score_recomputes_from_canonical_aggregate() -> None:
    suite, _ = load_formal_suite_protocol()
    case = suite.micro_cases[0]
    fixture = DiscoveryFixture.model_validate_json(Path(case.fixture).read_text())
    columns = ["phase", "sample_count", "mean_value", "max_value"]
    rows = [["baseline", 2, 1.0, 1.0], ["incident", 2, 100.0, 100.0]]
    source = {
        "source_case": case.source_case,
        "database": "suite_case_01",
        "task_audit": {
            "fixture_id": fixture.fixture_id,
            "canonical_query": canonical_evidence_query(fixture),
            "canonical_result": {"columns": columns, "rows": rows, "truncated": False},
            "predicate_match": True,
            "catalog_query": fixture.signal,
            "catalog_matched_table_count": 1,
            "catalog_target_rank": 1,
            "catalog_target_in_top_five": True,
            "catalog_top_five": [fixture.target_table],
        },
    }
    item = {
        "benchmark": "discovery",
        "case_index": 0,
        "source_case": case.source_case,
        "model": "gpt-5.6-sol",
        "api_transport": "openai-responses",
        "reasoning_effort": "medium",
        "max_output_tokens": 16384,
        "visibility": "raw",
        "answer": {
            "table": fixture.target_table,
            "component": fixture.component,
            "signal": fixture.signal,
        },
        "evidence_call_ordinal": 1,
        "calls": [
            {
                "ordinal": 1,
                "tool_name": "execute_sql",
                "input": {"query": canonical_evidence_query(fixture)},
                "query_succeeded": True,
                "result": {
                    "columns": columns,
                    "row_count": 2,
                    "truncated": False,
                    "sha256": _digest(columns, rows),
                    "canonical_evidence": {
                        "columns": columns,
                        "rows": rows,
                        "truncated": False,
                        "sha256": _digest(columns, rows),
                    },
                },
                "database_load": {
                    "query_count": 1,
                    "failed_query_count": 0,
                    "rows_returned": 2,
                    "max_concurrency": 1,
                },
            }
        ],
        "tool_calls_requested": 1,
        "runner_error": False,
        "tool_budget_exhausted": False,
        "turn_limit": 22,
        "turn_limit_enforced": True,
    }

    evaluation = score_public_micro_run(item, source, suite, DEFAULT_SUITE_PROTOCOL_FIXTURE)
    assert evaluation["success"] is True
    assert evaluation["rows_returned_through_evidence"] == 2

    item["calls"][0]["result"]["canonical_evidence"]["sha256"] = "wrong"
    assert not score_public_micro_run(item, source, suite, DEFAULT_SUITE_PROTOCOL_FIXTURE)[
        "success"
    ]


def test_public_graph_score_uses_treatment_specific_canonical_projection() -> None:
    suite, _ = load_formal_suite_protocol()
    case = suite.micro_cases[6]
    fixture = GraphFixture.model_validate_json(Path(case.fixture).read_text())
    trace_columns = [
        "src_type",
        "src_id",
        "dst_type",
        "dst_id",
        "rel_type",
        "provenance",
        "request_count",
        "error_count",
    ]
    graph_columns = [*trace_columns[:6], "confidence", *trace_columns[6:]]
    trace_rows = [
        [
            "service",
            fixture.caller,
            "service",
            fixture.expected_callee,
            "calls",
            "trace",
            fixture.expected_request_count,
            fixture.expected_error_count,
        ]
    ]
    graph_rows = [[*trace_rows[0][:6], 1.0, *trace_rows[0][6:]]]
    source = {
        "source_case": case.source_case,
        "database": "suite_case_07",
        "task_audit": {
            "fixture_id": fixture.fixture_id,
            "trace_query": canonical_trace_query(fixture),
            "trace_result": {
                "columns": trace_columns,
                "rows": trace_rows,
                "truncated": False,
            },
            "graph_query": "SELECT 1",
            "graph_result": {
                "columns": graph_columns,
                "rows": graph_rows,
                "truncated": False,
            },
            "edge_sets_match": True,
            "unique_winner": True,
            "expected_winner_match": True,
        },
    }
    answer = {
        "src_type": "service",
        "src_id": fixture.caller,
        "dst_type": "service",
        "dst_id": fixture.expected_callee,
        "rel_type": "calls",
        "provenance": "trace",
        "request_count": fixture.expected_request_count,
        "error_count": fixture.expected_error_count,
    }
    base = {
        "benchmark": "graph",
        "case_index": 6,
        "source_case": case.source_case,
        "model": "gpt-5.6-sol",
        "api_transport": "openai-responses",
        "reasoning_effort": "medium",
        "max_output_tokens": 16384,
        "answer": answer,
        "evidence_call_ordinal": 1,
        "tool_calls_requested": 1,
        "runner_error": False,
        "tool_budget_exhausted": False,
        "turn_limit": 22,
        "turn_limit_enforced": True,
    }
    raw = {
        **base,
        "visibility": "raw",
        "calls": [
            {
                "ordinal": 1,
                "tool_name": "execute_sql",
                "input": {"query": canonical_trace_query(fixture)},
                "query_succeeded": True,
                "result": {
                    "columns": trace_columns,
                    "row_count": 1,
                    "truncated": False,
                    "sha256": _digest(trace_columns, trace_rows),
                    "canonical_evidence": {
                        "columns": trace_columns,
                        "rows": trace_rows,
                        "truncated": False,
                        "sha256": _digest(trace_columns, trace_rows),
                    },
                },
                "database_load": {
                    "query_count": 1,
                    "failed_query_count": 0,
                    "rows_returned": 1,
                    "max_concurrency": 1,
                },
            }
        ],
    }
    graph = copy.deepcopy(raw)
    graph["visibility"] = "semantic_graph"
    graph["calls"][0].update(
        {
            "tool_name": "query_semantic_graph",
            "input": {
                "view": "relationships",
                "src_type": "service",
                "src_id": fixture.caller,
                "dst_type": "service",
                "rel_type": "calls",
                "provenance": "trace",
                "limit": 200,
            },
            "result": {
                "columns": graph_columns,
                "row_count": 1,
                "truncated": False,
                "sha256": _digest(graph_columns, graph_rows),
                "canonical_evidence": {
                    "columns": trace_columns,
                    "rows": trace_rows,
                    "truncated": False,
                    "sha256": _digest(trace_columns, trace_rows),
                },
            },
        }
    )

    assert score_public_micro_run(raw, source, suite, DEFAULT_SUITE_PROTOCOL_FIXTURE)["success"]
    assert score_public_micro_run(graph, source, suite, DEFAULT_SUITE_PROTOCOL_FIXTURE)["success"]


def _successful_private_run(
    cell: dict[str, object],
    source: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    visibility = Visibility(str(cell["visibility"]))
    transport = ApiTransport(str(cell["api_transport"]))
    task_audit = source["task_audit"]
    assert isinstance(task_audit, dict)
    query_id = "private-evidence-query-id"
    load = DatabaseLoad(query_count=1, rows_returned=2, max_concurrency=1)
    if cell["benchmark"] == "discovery":
        fixture = DiscoveryFixture.model_validate_json(
            Path(
                str(load_formal_suite_protocol()[0].micro_cases[int(cell["case_index"])].fixture)
            ).read_text()
        )
        audit = DiscoveryAudit.model_validate(task_audit)
        evidence = audit.canonical_result.model_copy(
            update={
                "query_id": query_id,
                "rows": list(reversed(audit.canonical_result.rows)),
                "elapsed_seconds": 0.25,
            }
        )
        run = DiscoveryAgentRun(
            run_id="private-run-id-must-not-leak",
            visibility=visibility,
            model=str(cell["model"]),
            runner=AgentRunner.API,
            api_transport=transport,
            reasoning_effort=str(cell["reasoning_effort"]),
            max_output_tokens=int(cell["max_output_tokens"]),
            answer=DiscoveryAnswer(
                table=fixture.target_table,
                evidence_query_id=query_id,
                component=fixture.component,
                signal=fixture.signal,
                claim="private free-form claim must not leak",
            ),
            tool_calls=[
                ToolTrace(
                    tool_name="execute_sql",
                    input={"query": canonical_evidence_query(fixture)},
                    query_id=query_id,
                    output=evidence.model_dump(mode="json"),
                    database_load=load,
                )
            ],
            tool_calls_requested=1,
            usage=AgentUsage(input_tokens=100, output_tokens=20, reasoning_tokens=10),
            elapsed_seconds=1.0,
            responses=[
                {
                    "private_provider_payload": "must-not-leak",
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 20},
                    },
                }
            ],
            turn_limit=22,
            turn_limit_enforced=True,
        )
        evaluation = evaluate_discovery_run(
            run,
            fixture,
            audit.canonical_result,
            database=str(source["exclusive_instance"]["database"]),
        )
    else:
        fixture = GraphFixture.model_validate_json(
            Path(
                str(load_formal_suite_protocol()[0].micro_cases[int(cell["case_index"])].fixture)
            ).read_text()
        )
        audit = GraphAudit.model_validate(task_audit)
        if visibility is Visibility.RAW:
            tool_name = "execute_sql"
            tool_input = {"query": canonical_trace_query(fixture)}
            evidence = audit.trace_result
        else:
            tool_name = "query_semantic_graph"
            tool_input = {
                "view": "relationships",
                "src_type": "service",
                "src_id": fixture.caller,
                "dst_type": "service",
                "rel_type": "calls",
                "provenance": "trace",
                "limit": 200,
            }
            evidence = audit.graph_result
        evidence = evidence.model_copy(update={"query_id": query_id, "elapsed_seconds": 0.25})
        run = GraphAgentRun(
            run_id="private-run-id-must-not-leak",
            visibility=visibility,
            model=str(cell["model"]),
            runner=AgentRunner.API,
            api_transport=transport,
            reasoning_effort=str(cell["reasoning_effort"]),
            max_output_tokens=int(cell["max_output_tokens"]),
            answer=GraphAnswer(
                src_type="service",
                src_id=fixture.caller,
                dst_type="service",
                dst_id=fixture.expected_callee,
                rel_type="calls",
                provenance="trace",
                request_count=fixture.expected_request_count,
                error_count=fixture.expected_error_count,
                evidence_query_id=query_id,
                claim="private free-form claim must not leak",
            ),
            tool_calls=[
                ToolTrace(
                    tool_name=tool_name,
                    input=tool_input,
                    query_id=query_id,
                    output=evidence.model_dump(mode="json"),
                    database_load=load,
                )
            ],
            tool_calls_requested=1,
            usage=AgentUsage(input_tokens=100, output_tokens=20, reasoning_tokens=10),
            elapsed_seconds=1.0,
            responses=[
                {
                    "private_provider_payload": "must-not-leak",
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 20},
                    },
                }
            ],
            turn_limit=22,
            turn_limit_enforced=True,
        )
        evaluation = evaluate_graph_run(
            run,
            fixture,
            audit.graph_result,
            database=str(source["exclusive_instance"]["database"]),
        )
    return run.model_dump(mode="json"), evaluation.model_dump(mode="json")


@pytest.mark.parametrize("missing_cache_breakdown", [None, "input", "creation"])
def test_complete_micro_artifact_sanitizes_and_deterministically_rescores(
    missing_cache_breakdown: str | None,
) -> None:
    suite, transfer = load_formal_suite_protocol()
    sources = _source_audits()
    report = build_micro_preflight_report(
        suite,
        transfer,
        DEFAULT_SUITE_PROTOCOL_FIXTURE,
        sources,
    )
    source_by_case = {str(source["source_case"]): source for source in sources}
    runs = []
    for cell in report["schedule"]:
        assert isinstance(cell, dict)
        source = source_by_case[str(cell["source_case"])]
        run, evaluation = _successful_private_run(cell, source)
        runs.append(
            {
                **cell,
                "run": run,
                "evaluation": evaluation,
                "database_load": DatabaseLoad(
                    query_count=1,
                    rows_returned=2,
                    max_concurrency=1,
                ).model_dump(mode="json"),
                "source_semantic_sha256": micro_source_semantic_sha256(source),
            }
        )
    report["runs"] = runs
    if missing_cache_breakdown == "input":
        runs[0]["run"]["responses"][0]["usage"].pop("input_tokens_details")
    elif missing_cache_breakdown == "creation":
        report["pricing_snapshot"][runs[0]["model"]]["cache_creation_breakdown_required"] = True
        report["bindings"]["pricing_snapshot_sha256"] = canonical_sha256(report["pricing_snapshot"])
    report["execution"] = {
        "completed_runs": 128,
        "expected_runs": 128,
        "remaining_runs": 0,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "complete": True,
    }

    artifact = build_micro_measurement_artifact(
        report,
        suite,
        DEFAULT_SUITE_PROTOCOL_FIXTURE,
    )
    validate_micro_measurement_artifact(artifact, DEFAULT_SUITE_PROTOCOL_FIXTURE)

    encoded = json.dumps(artifact, sort_keys=True)
    assert len(artifact["runs"]) == 128
    assert "private_provider_payload" not in encoded
    assert "private-run-id-must-not-leak" not in encoded
    assert "private free-form claim must not leak" not in encoded
    assert "private-evidence-query-id" not in encoded
    if missing_cache_breakdown:
        usage = artifact["runs"][0]["usage"]
        assert usage["provider_visible_input_tokens"] == 100
        assert usage["uncached_input_tokens"] is None
        summary = artifact["model_reports"][runs[0]["model"]]["usage"]
        assert summary["provider_visible_input_tokens"] == 3200
        assert summary["cache_read_input_tokens"] is None
        assert summary["cache_breakdown_complete"] is False
