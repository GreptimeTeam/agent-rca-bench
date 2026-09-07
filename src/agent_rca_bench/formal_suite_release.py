from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from statistics import median

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
    canonical_evidence_rows,
    evaluate_discovery_run,
)
from agent_rca_bench.formal_suite import canonical_sha256, validate_micro_report
from agent_rca_bench.formal_suite_protocol import (
    FormalSuiteProtocolFixture,
    load_formal_suite_protocol,
    micro_schedule,
    repository_path,
    sha256_file,
)
from agent_rca_bench.graph_benchmark import (
    GraphAgentRun,
    GraphAnswer,
    GraphAudit,
    GraphFixture,
    canonical_edge_set,
    evaluate_graph_run,
)
from agent_rca_bench.report import (
    _cache_creation_breakdown_available,
    _estimated_api_cost,
    _raw_input_breakdown,
)

MICRO_ARTIFACT_SCHEMA_VERSION = 1


def build_micro_measurement_artifact(
    report: dict[str, object],
    suite: FormalSuiteProtocolFixture,
    suite_path: Path,
) -> dict[str, object]:
    loaded_suite, transfer = load_formal_suite_protocol(suite_path)
    if loaded_suite != suite:
        raise ValueError("formal suite object differs from its bound fixture")
    validate_micro_report(report, suite, transfer, suite_path, require_complete=True)
    source_audits = _list_of_mappings(report, "source_audits")
    public_sources = [_public_source(source, suite, suite_path) for source in source_audits]
    source_by_case = {str(source["source_case"]): source for source in public_sources}
    pricing = _mapping(report, "pricing_snapshot")
    runs = [
        _public_run(item, source_by_case[str(item["source_case"])], suite, suite_path, pricing)
        for item in _list_of_mappings(report, "runs")
    ]
    summaries = _model_summaries(runs, [model.model for model in transfer.models])
    payload = {
        "artifact_schema_version": MICRO_ARTIFACT_SCHEMA_VERSION,
        "artifact_type": "semantic-rca-micro-measurement-result",
        "publication_status": (
            "sanitized measurement result; contains no provider payloads or raw telemetry rows"
        ),
        "analysis_role": "fixed reference cohort; not a fresh population holdout",
        "license": {
            "benchmark_code": "Apache-2.0",
            "openrca_1_telemetry": "CC BY-NC 4.0; telemetry rows are not redistributed",
            "openrca_2_terms": (
                "dataset card says Apache-2.0; paper says CC-BY-SA 4.0; telemetry rows are "
                "not redistributed"
            ),
            "artifact_schema_and_derived_aggregates": "Apache-2.0 benchmark artifact",
        },
        "suite_protocol": _mapping(report, "suite_protocol"),
        "pricing_snapshot": pricing,
        "sources": public_sources,
        "runs": runs,
        "model_reports": summaries,
        "inference": {
            "semantic_effect": "paired within model and case descriptive effect sizes only",
            "case_unit": True,
            "repetitions_descriptive": True,
            "cross_model_pooling": False,
            "cross_task_overall_score": False,
            "hypothesis_tests": False,
            "multiplicity_policy": None,
            "reason": (
                "the fixed reference micro cohorts were not enrolled or powered as a "
                "confirmatory hypothesis family"
            ),
        },
        "sanitization": {
            "excluded": [
                "provider responses, thinking, signatures, and tool-call IDs",
                "run IDs, elapsed timings, ports, process metadata, and local paths",
                "raw telemetry rows and noncanonical query result values",
                "free-form answer claims and provider error text",
            ],
            "included": [
                "task answers without free-form claim text",
                "SQL or Graph query inputs and per-call row counts",
                "canonical aggregate evidence and result digests",
                "deterministically recomputed task validity and primary metrics",
                "aggregate token, cache, reliability, and cost fields",
            ],
        },
    }
    return {
        **payload,
        "integrity": {
            "semantic_payload_sha256": canonical_sha256(payload),
            "private_report_sha256": None,
        },
    }


def build_micro_measurement_artifact_from_files(
    report_path: Path,
    suite_path: Path,
) -> dict[str, object]:
    report = json.loads(report_path.read_text())
    if not isinstance(report, dict):
        raise ValueError("formal micro report must be a JSON object")
    suite, _ = load_formal_suite_protocol(suite_path)
    artifact = build_micro_measurement_artifact(report, suite, suite_path)
    _mapping(artifact, "integrity")["private_report_sha256"] = sha256_file(report_path)
    return artifact


def validate_micro_measurement_artifact(
    artifact: dict[str, object],
    suite_path: Path,
) -> None:
    suite, transfer = load_formal_suite_protocol(suite_path)
    if (
        artifact.get("artifact_schema_version") != MICRO_ARTIFACT_SCHEMA_VERSION
        or artifact.get("artifact_type") != "semantic-rca-micro-measurement-result"
    ):
        raise ValueError("unsupported formal micro measurement artifact")
    if artifact.get("suite_protocol") != json.loads(suite_path.read_text()):
        raise ValueError("formal micro artifact protocol drifted")
    sources = _list_of_mappings(artifact, "sources")
    if [source.get("source_case") for source in sources] != [
        case.source_case for case in suite.micro_cases
    ]:
        raise ValueError("formal micro artifact source roster drifted")
    source_by_case = {str(source["source_case"]): source for source in sources}
    runs = _list_of_mappings(artifact, "runs")
    if len(runs) != suite.expected_micro_cells:
        raise ValueError("formal micro artifact cell count drifted")
    schedule = micro_schedule(suite, transfer)
    for expected, item in zip(schedule, runs, strict=True):
        if any(item.get(key) != value for key, value in expected.items()):
            raise ValueError("formal micro artifact schedule drifted")
        recomputed = score_public_micro_run(
            item,
            source_by_case[str(item["source_case"])],
            suite,
            suite_path,
        )
        if item.get("evaluation") != recomputed:
            raise ValueError("formal micro artifact evaluation does not deterministically rescore")
    expected_summaries = _model_summaries(runs, [model.model for model in transfer.models])
    if artifact.get("model_reports") != expected_summaries:
        raise ValueError("formal micro artifact model summaries drifted")
    integrity = _mapping(artifact, "integrity")
    payload = {key: value for key, value in artifact.items() if key != "integrity"}
    if integrity.get("semantic_payload_sha256") != canonical_sha256(payload):
        raise ValueError("formal micro artifact semantic payload hash drifted")


def score_public_micro_run(
    item: Mapping[str, object],
    source: Mapping[str, object],
    suite: FormalSuiteProtocolFixture,
    suite_path: Path,
) -> dict[str, object]:
    case = suite.micro_cases[int(item["case_index"])]
    fixture_path = repository_path(suite_path, case.fixture)
    audit_payload = _mapping(source, "task_audit")
    run = _reconstructed_run(item, case.benchmark)
    database = str(source["database"])
    if case.benchmark == "discovery":
        fixture = DiscoveryFixture.model_validate_json(fixture_path.read_text())
        audit = DiscoveryAudit.model_validate(_restore_query_result_metadata(audit_payload))
        evaluation = evaluate_discovery_run(
            DiscoveryAgentRun.model_validate(run),
            fixture,
            audit.canonical_result,
            database=database,
        )
    else:
        fixture = GraphFixture.model_validate_json(fixture_path.read_text())
        audit = GraphAudit.model_validate(_restore_query_result_metadata(audit_payload))
        evaluation = evaluate_graph_run(
            GraphAgentRun.model_validate(run),
            fixture,
            audit.graph_result,
            database=database,
        )
    payload = evaluation.model_dump(mode="json")
    payload.pop("failure_reasons")
    payload["runner_error"] = item.get("runner_error") is True
    payload["tool_budget_exhausted"] = item.get("tool_budget_exhausted") is True
    return payload


def _public_source(
    source: Mapping[str, object],
    suite: FormalSuiteProtocolFixture,
    suite_path: Path,
) -> dict[str, object]:
    case = next(item for item in suite.micro_cases if item.source_case == source["source_case"])
    fixture_path = repository_path(suite_path, case.fixture)
    fixture = json.loads(fixture_path.read_text())
    exclusive = _mapping(source, "exclusive_instance")
    return {
        "benchmark": source.get("benchmark"),
        "source_case": source.get("source_case"),
        "database": exclusive.get("database"),
        "fixture": fixture,
        "fixture_sha256": case.fixture_sha256,
        "dataset_revision": source.get("dataset_revision"),
        "adapter_revision": source.get("adapter_revision"),
        "mirror_revision": source.get("mirror_revision"),
        "greptimedb": _mapping(source, "greptimedb"),
        "ingestion": _mapping(source, "ingestion"),
        "semantic_coverage": _mapping(source, "semantic_coverage"),
        "task_audit": _without_runtime_query_metadata(source.get("task_audit")),
        "no_model_gates": _mapping(source, "no_model_gates"),
    }


def _public_run(
    item: Mapping[str, object],
    source: Mapping[str, object],
    suite: FormalSuiteProtocolFixture,
    suite_path: Path,
    pricing: Mapping[str, object],
) -> dict[str, object]:
    run = _mapping(item, "run")
    answer = run.get("answer")
    answer = dict(answer) if isinstance(answer, dict) else None
    evidence_query_id = answer.pop("evidence_query_id", None) if answer is not None else None
    if answer is not None:
        answer.pop("claim", None)
    calls = []
    evidence_ordinals = []
    for ordinal, trace in enumerate(_list_of_mappings(run, "tool_calls"), start=1):
        if trace.get("query_id") == evidence_query_id:
            evidence_ordinals.append(ordinal)
        output = trace.get("output")
        result = _query_result_summary(output)
        if trace.get("query_id") == evidence_query_id and result is not None:
            result["canonical_evidence"] = _canonical_evidence_summary(
                output,
                str(item["benchmark"]),
            )
        load = trace.get("database_load")
        calls.append(
            {
                "ordinal": ordinal,
                "tool_name": trace.get("tool_name"),
                "input": trace.get("input"),
                "query_succeeded": trace.get("error") is None,
                "result": result,
                "database_load": {
                    key: load.get(key)
                    for key in (
                        "query_count",
                        "failed_query_count",
                        "rows_returned",
                        "max_concurrency",
                    )
                }
                if isinstance(load, dict)
                else None,
            }
        )
    uncached, cache_read, cache_creation, cache_complete = _raw_input_breakdown(run)
    model_pricing = _mapping(pricing, str(item["model"]))
    if model_pricing.get("cache_creation_breakdown_required") is True:
        cache_complete = cache_complete and _cache_creation_breakdown_available(run)
    public = {
        key: item[key]
        for key in (
            "cell_index",
            "benchmark",
            "case_index",
            "source_case",
            "model_index",
            "model",
            "provider",
            "api_transport",
            "prompt_cache",
            "reasoning_effort",
            "max_output_tokens",
            "repetition",
            "position",
            "visibility",
        )
    }
    public.update(
        {
            "answer": answer,
            "evidence_call_ordinal": evidence_ordinals[0] if len(evidence_ordinals) == 1 else None,
            "calls": calls,
            "tool_calls_requested": run.get("tool_calls_requested"),
            "rejected_tool_calls": dict(
                sorted(
                    Counter(
                        str(rejected.get("reason_code"))
                        for rejected in run.get("rejected_tool_calls", [])
                        if isinstance(rejected, dict)
                    ).items()
                )
            ),
            "runner_error": run.get("error") is not None,
            "tool_budget_exhausted": run.get("tool_budget_exhausted") is True,
            "turn_limit": run.get("turn_limit"),
            "turn_limit_enforced": run.get("turn_limit_enforced"),
            "database_load": {
                key: _mapping(item, "database_load").get(key)
                for key in (
                    "query_count",
                    "failed_query_count",
                    "rows_returned",
                    "max_concurrency",
                )
            },
            "usage": {
                **(
                    {"provider_visible_input_tokens": _mapping(run, "usage")["input_tokens"]}
                    if not cache_complete
                    else {}
                ),
                "uncached_input_tokens": uncached if cache_complete else None,
                "cache_read_input_tokens": cache_read if cache_complete else None,
                "cache_creation_input_tokens": cache_creation if cache_complete else None,
                "cache_breakdown_complete": cache_complete,
                "output_tokens": _mapping(run, "usage").get("output_tokens"),
                "reasoning_output_tokens": _mapping(run, "usage").get("reasoning_tokens"),
                "estimated_cost": _estimated_api_cost(run, model_pricing),
                "cost_currency": model_pricing.get("currency"),
            },
        }
    )
    public["evaluation"] = score_public_micro_run(public, source, suite, suite_path)
    private_evaluation = _mapping(item, "evaluation")
    for field in (
        "success",
        "tool_calls_through_evidence",
        "rows_returned_through_evidence",
    ):
        if public["evaluation"].get(field) != private_evaluation.get(field):
            raise ValueError(f"public micro score disagrees with private evaluation: {field}")
    return public


def _reconstructed_run(
    item: Mapping[str, object],
    benchmark: str,
) -> dict[str, object]:
    calls = _list_of_mappings(item, "calls")
    evidence_ordinal = item.get("evidence_call_ordinal")
    traces = []
    for call in calls:
        ordinal = int(call["ordinal"])
        result = call.get("result")
        output = None
        if isinstance(result, dict):
            canonical_evidence = result.get("canonical_evidence")
            if ordinal == evidence_ordinal and isinstance(canonical_evidence, dict):
                evidence_payload = {
                    key: canonical_evidence.get(key) for key in ("columns", "rows", "truncated")
                }
                if canonical_evidence.get("sha256") == _result_digest(evidence_payload):
                    output = evidence_payload
            else:
                output = {
                    "query_id": f"call-{ordinal}",
                    "columns": result.get("columns", []),
                    "rows": [],
                    "elapsed_seconds": 0.0,
                    "truncated": result.get("truncated", False),
                }
            if isinstance(output, dict):
                output["query_id"] = f"call-{ordinal}"
                output["elapsed_seconds"] = 0.0
        load = call.get("database_load")
        traces.append(
            ToolTrace(
                tool_name=str(call["tool_name"]),
                input=dict(call.get("input") or {}),
                query_id=f"call-{ordinal}" if output is not None else None,
                output=output,
                error=None if call.get("query_succeeded") is True else "query failed",
                database_load=DatabaseLoad.model_validate(load) if isinstance(load, dict) else None,
            ).model_dump(mode="json")
        )
    answer = item.get("answer")
    if isinstance(answer, dict) and isinstance(evidence_ordinal, int):
        answer = {**answer, "evidence_query_id": f"call-{evidence_ordinal}", "claim": "evidence"}
    else:
        answer = None
    run_type = DiscoveryAgentRun if benchmark == "discovery" else GraphAgentRun
    answer_type = DiscoveryAnswer if benchmark == "discovery" else GraphAnswer
    run = run_type(
        run_id="sanitized",
        visibility=Visibility(str(item["visibility"])),
        model=str(item["model"]),
        runner=AgentRunner.API,
        api_transport=ApiTransport(str(item["api_transport"])),
        reasoning_effort=(
            str(item["reasoning_effort"]) if item.get("reasoning_effort") is not None else None
        ),
        max_output_tokens=int(item["max_output_tokens"]),
        answer=answer_type.model_validate(answer) if answer is not None else None,
        error="runner error" if item.get("runner_error") is True else None,
        tool_calls=[ToolTrace.model_validate(trace) for trace in traces],
        rejected_tool_calls=[],
        tool_calls_requested=int(item.get("tool_calls_requested", 0) or 0),
        tool_budget_exhausted=item.get("tool_budget_exhausted") is True,
        usage=AgentUsage(),
        elapsed_seconds=0.0,
        responses=[],
        turn_limit=int(item["turn_limit"]) if item.get("turn_limit") is not None else None,
        turn_limit_enforced=item.get("turn_limit_enforced") is True,
    )
    return run.model_dump(mode="json")


def _query_result_summary(value: object) -> dict[str, object] | None:
    try:
        result = QueryResult.model_validate(value)
    except ValueError:
        return None
    return {
        "columns": result.columns,
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "sha256": _result_digest(result.model_dump(mode="json")),
    }


def _canonical_evidence_summary(
    value: object,
    benchmark: str,
) -> dict[str, object] | None:
    try:
        result = QueryResult.model_validate(value)
    except ValueError:
        return None
    if result.truncated:
        return None
    if benchmark == "discovery":
        evidence = canonical_evidence_rows(result)
        if evidence is None:
            return None
        columns = ["phase", "sample_count", "mean_value", "max_value"]
        rows = [
            [phase, *(evidence[phase][field] for field in columns[1:])]
            for phase in ("baseline", "incident")
        ]
    elif benchmark == "graph":
        edges = canonical_edge_set(result)
        if edges is None:
            return None
        columns = [
            "src_type",
            "src_id",
            "dst_type",
            "dst_id",
            "rel_type",
            "provenance",
            "request_count",
            "error_count",
        ]
        rows = [list(edge) for edge in sorted(edges)]
    else:
        raise ValueError(f"unsupported micro benchmark: {benchmark}")
    payload = {"columns": columns, "rows": rows, "truncated": False}
    return {**payload, "sha256": _result_digest(payload)}


def _result_digest(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    columns = value.get("columns")
    rows = value.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    return canonical_sha256(
        {
            "columns": columns,
            "rows": rows,
            "truncated": value.get("truncated", False),
        }
    )


def _model_summaries(
    runs: list[dict[str, object]],
    models: list[str],
) -> dict[str, object]:
    return {
        model: _model_summary([run for run in runs if run["model"] == model]) for model in models
    }


def _model_summary(runs: list[dict[str, object]]) -> dict[str, object]:
    by_benchmark: dict[str, list[dict[str, object]]] = defaultdict(list)
    for run in runs:
        by_benchmark[str(run["benchmark"])].append(run)
    benchmark_reports = {
        benchmark: _benchmark_model_summary(items)
        for benchmark, items in sorted(by_benchmark.items())
    }
    usage_fields = (
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    usage = {
        field: (
            sum(int(_mapping(run, "usage").get(field, 0) or 0) for run in runs)
            if all(_mapping(run, "usage").get(field) is not None for run in runs)
            else None
        )
        for field in usage_fields
    }
    if any("provider_visible_input_tokens" in _mapping(run, "usage") for run in runs):
        usage["provider_visible_input_tokens"] = sum(
            _reported_tokens(run) - int(_mapping(run, "usage").get("output_tokens", 0) or 0)
            for run in runs
        )
        usage["cache_breakdown_complete"] = all(
            _mapping(run, "usage").get("cache_breakdown_complete") is True for run in runs
        )
    costs = [_mapping(run, "usage").get("estimated_cost") for run in runs]
    currencies = {_mapping(run, "usage").get("cost_currency") for run in runs}
    return {
        "inference_role": "within-model case-level description",
        "benchmarks": benchmark_reports,
        "reliability": {
            "runs": len(runs),
            "runner_errors": sum(run.get("runner_error") is True for run in runs),
            "budget_exhaustions": sum(run.get("tool_budget_exhausted") is True for run in runs),
            "rejected_tool_calls": sum(
                sum(int(value) for value in _mapping(run, "rejected_tool_calls").values())
                for run in runs
            ),
        },
        "usage": {
            **usage,
            "estimated_cost": (
                sum(float(cost) for cost in costs)
                if costs and all(isinstance(cost, (int, float)) for cost in costs)
                else None
            ),
            "cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
        },
    }


def _benchmark_model_summary(runs: list[dict[str, object]]) -> dict[str, object]:
    cases = sorted({str(run["source_case"]) for run in runs})
    treatment_success = {
        visibility: sum(
            run["visibility"] == visibility and _mapping(run, "evaluation").get("success") is True
            for run in runs
        )
        for visibility in ("raw", "semantic_graph")
    }
    case_deltas: dict[str, list[float]] = {
        "tool_calls_through_evidence": [],
        "rows_returned_through_evidence": [],
        "reported_total_tokens": [],
    }
    case_records = []
    for source_case in cases:
        case_runs = [run for run in runs if run["source_case"] == source_case]
        by_pair = {(int(run["repetition"]), str(run["visibility"])): run for run in case_runs}
        pair_deltas = {metric: [] for metric in case_deltas}
        eligible_pairs = 0
        for repetition in sorted({int(run["repetition"]) for run in case_runs}):
            raw = by_pair[(repetition, "raw")]
            graph = by_pair[(repetition, "semantic_graph")]
            if not (
                _mapping(raw, "evaluation").get("success") is True
                and _mapping(graph, "evaluation").get("success") is True
            ):
                continue
            eligible_pairs += 1
            for metric in pair_deltas:
                if metric == "reported_total_tokens":
                    delta = _reported_tokens(graph) - _reported_tokens(raw)
                else:
                    delta = float(_mapping(graph, "evaluation")[metric]) - float(
                        _mapping(raw, "evaluation")[metric]
                    )
                pair_deltas[metric].append(delta)
        medians = {
            metric: median(values) if values else None for metric, values in pair_deltas.items()
        }
        for metric, value in medians.items():
            if value is not None:
                case_deltas[metric].append(value)
        case_records.append(
            {
                "source_case": source_case,
                "eligible_pairs": eligible_pairs,
                "pair_deltas": pair_deltas,
                "case_median_deltas": medians,
            }
        )
    return {
        "runs": len(runs),
        "cases": case_records,
        "treatment_success": treatment_success,
        "metric_roles": {
            "tool_calls_through_evidence": "pre-specified primary",
            "rows_returned_through_evidence": "pre-specified primary",
            "reported_total_tokens": "exploratory provider-accounted metric",
        },
        "case_level_effect": {
            metric: _direction_summary(values) for metric, values in case_deltas.items()
        },
    }


def _reported_tokens(run: Mapping[str, object]) -> int:
    usage = _mapping(run, "usage")
    if isinstance(usage.get("provider_visible_input_tokens"), int):
        return int(usage["provider_visible_input_tokens"]) + int(usage.get("output_tokens", 0) or 0)
    return sum(
        int(usage.get(field, 0) or 0)
        for field in (
            "uncached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )


def _direction_summary(values: list[float]) -> dict[str, object]:
    improvements = sum(value < 0 for value in values)
    regressions = sum(value > 0 for value in values)
    return {
        "eligible_cases": len(values),
        "median_delta": median(values) if values else None,
        "improvements": improvements,
        "regressions": regressions,
        "ties": sum(value == 0 for value in values),
        "inference_status": "descriptive_only",
    }


def _restore_query_result_metadata(value: object) -> object:
    if isinstance(value, dict):
        restored = {key: _restore_query_result_metadata(item) for key, item in value.items()}
        if "columns" in restored and "rows" in restored:
            restored.setdefault("query_id", "canonical")
            restored.setdefault("elapsed_seconds", 0.0)
        return restored
    if isinstance(value, list):
        return [_restore_query_result_metadata(item) for item in value]
    return value


def _without_runtime_query_metadata(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_runtime_query_metadata(item)
            for key, item in value.items()
            if key not in {"query_id", "elapsed_seconds"}
        }
    if isinstance(value, list):
        return [_without_runtime_query_metadata(item) for item in value]
    return value


def _mapping(source: Mapping[str, object], key: str) -> dict[str, object]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"expected object at {key}")
    return value


def _list_of_mappings(source: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = source.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"expected object list at {key}")
    return value
