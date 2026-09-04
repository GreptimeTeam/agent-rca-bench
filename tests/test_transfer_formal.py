import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_rca_bench.transfer_formal as transfer_formal
from agent_rca_bench.contracts import DatabaseLoad, Visibility
from agent_rca_bench.transfer_formal import (
    PreparedTransferEnvironment,
    build_preflight_report,
    execute_case_runs,
    source_semantic_sha256,
    validate_private_report,
)
from agent_rca_bench.transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    load_transfer_protocol,
)


def _source_audit(case_id: str) -> dict[str, object]:
    return {
        "case": {"opaque_case_id": case_id},
        "no_model_gates": {"all_passed": True},
    }


def test_preflight_freezes_source_audits_without_provider_calls() -> None:
    protocol, selection = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in selection.selected_cases]

    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        selection,
        audits,
    )

    validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, selection)
    assert report["authorization"]["preflight_calls_provider"] is False
    assert report["phase"] == "measurement"
    assert report["execution"]["expected_runs"] == 336


def test_source_semantic_hash_ignores_runtime_ports_but_binds_images() -> None:
    audit = {
        "exclusive_split_stack": {
            "loopback_only": True,
            "images": {"tempo": "tempo@sha256:first"},
            "ports": {"tempo_http": 41001},
        }
    }
    different_ports = {
        **audit,
        "exclusive_split_stack": {
            **audit["exclusive_split_stack"],
            "ports": {"tempo_http": 42001},
        },
    }
    different_image = {
        **audit,
        "exclusive_split_stack": {
            **audit["exclusive_split_stack"],
            "images": {"tempo": "tempo@sha256:second"},
        },
    }

    assert source_semantic_sha256(audit) == source_semantic_sha256(different_ports)
    assert source_semantic_sha256(audit) != source_semantic_sha256(different_image)


def test_promql_preflight_results_must_match_the_source_metric_and_time() -> None:
    timestamp_ms = 1_500
    metric = "cpu"

    assert transfer_formal._promql_result_matches_source(
        "labels", [["__name__"]], metric=metric, expected_timestamp_ms=timestamp_ms
    )
    assert transfer_formal._promql_result_matches_source(
        "label_values", [[metric]], metric=metric, expected_timestamp_ms=timestamp_ms
    )
    assert transfer_formal._promql_result_matches_source(
        "series", [[{"__name__": metric}]], metric=metric, expected_timestamp_ms=timestamp_ms
    )
    assert transfer_formal._promql_result_matches_source(
        "query",
        [[{"__name__": metric}, 1.5, "1"]],
        metric=metric,
        expected_timestamp_ms=timestamp_ms,
    )
    assert transfer_formal._promql_result_matches_source(
        "query_range",
        [[{"__name__": metric}, 1.499, "1"]],
        metric=metric,
        expected_timestamp_ms=timestamp_ms,
    )
    assert not transfer_formal._promql_result_matches_source(
        "query_range",
        [[{"__name__": metric}, 2.5, "1"]],
        metric=metric,
        expected_timestamp_ms=timestamp_ms,
    )


def test_runner_failure_is_persisted_as_a_scoreable_cell() -> None:
    protocol, selection = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in selection.selected_cases]
    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        selection,
        audits,
    )

    class Client:
        @contextmanager
        def measure_query_load(self):
            yield DatabaseLoad()

    prepared = PreparedTransferEnvironment(
        client=Client(),
        case=SimpleNamespace(input=SimpleNamespace()),
        spec=selection.selected_cases[0],
        source_audit=audits[0],
        semantic_coverage={},
        graph_window=(0, 60),
        split_stack=SimpleNamespace(
            prometheus_endpoint="http://127.0.0.1:1",
            loki_endpoint="http://127.0.0.1:2",
            tempo_endpoint="http://127.0.0.1:3",
        ),
    )

    execute_case_runs(
        report,
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        selection,
        prepared,
        paid_api_confirmed=True,
        max_new_runs=1,
        run_agent_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    assert report["execution"]["completed_runs"] == 1
    assert report["execution"]["runner_errors"] == 1
    assert report["runs"][0]["evaluation"]["success"] is False
    assert report["runs"][0]["source_semantic_sha256"] == source_semantic_sha256(audits[0])
    validate_private_report(report, protocol, DEFAULT_PROTOCOL_FIXTURE, selection)


GATE_PRODUCERS = {
    "source": "source_telemetry_audit",
    "stored": "validate_transfer_ingest",
    "equality": "exact_edge_equality_audit",
}


def _module_ast(dotted: str) -> ast.Module:
    return ast.parse((Path("src/agent_rca_bench") / f"{dotted}.py").read_text())


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _returned(function: ast.FunctionDef) -> tuple[dict[str, ast.AST], set[str]]:
    """Literal keys the function returns, and the names it spreads with **."""
    values: dict[str, ast.AST] = {}
    spreads: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if key is None:
                if isinstance(value, ast.Name):
                    spreads.add(value.id)
            elif isinstance(key, ast.Constant):
                values[key.value] = value
    return values, spreads


def _gate_reads(gates: ast.FunctionDef) -> list[tuple[str, str]]:
    reads = []
    for node in ast.walk(gates):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in GATE_PRODUCERS
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            reads.append((node.func.value.id, node.args[0].value))
    return reads


@pytest.mark.parametrize(
    ("gate_module", "base_module"),
    [
        ("datasets.openrca2_transfer", "datasets.openrca2"),
        ("datasets.rca100_audit", "datasets.rca100"),
    ],
)
def test_no_model_gates_assert_measured_values_only(gate_module: str, base_module: str) -> None:
    """A gate must read a key its audit computes.

    A gate reading a key the audit never produces is always False; one reading a
    hard-coded literal is always True. Both have shipped: RCA100 asserted a
    missing window-boundary key, and both adapters asserted a constant revision
    string and a literal `reference_causal_graph_ingested: False`.
    """
    gate_tree = _module_ast(gate_module.replace(".", "/"))
    base_tree = _module_ast(base_module.replace(".", "/"))
    gates = _function(gate_tree, "no_model_gates")

    checked = 0
    for parameter, key in _gate_reads(gates):
        produced, spreads = _returned(_function(gate_tree, GATE_PRODUCERS[parameter]))
        if key not in produced and spreads:
            # validate_transfer_ingest re-exports the base adapter's audit.
            produced, _ = _returned(_function(base_tree, "validate_ingest"))
        assert key in produced, f"{gate_module}: {parameter}.{key} is never produced"
        assert not isinstance(produced[key], ast.Constant), (
            f"{gate_module}: {parameter}.{key} asserts a hard-coded literal"
        )
        checked += 1

    # Guards against the scan silently covering nothing if the gates stop
    # using .get() to reach their audits.
    assert checked >= 5


def test_a_split_cell_records_no_greptimedb_rows_and_survives_a_runner_failure() -> None:
    protocol, selection = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in selection.selected_cases]

    class Client:
        @contextmanager
        def measure_query_load(self):
            yield DatabaseLoad()

    prepared = PreparedTransferEnvironment(
        client=Client(),
        case=SimpleNamespace(input=SimpleNamespace()),
        spec=selection.selected_cases[0],
        source_audit=audits[0],
        semantic_coverage={},
        graph_window=(0, 60),
        split_stack=SimpleNamespace(
            prometheus_endpoint="http://127.0.0.1:1",
            loki_endpoint="http://127.0.0.1:2",
            tempo_endpoint="http://127.0.0.1:3",
        ),
    )

    run, load = transfer_formal._run_split_cell(
        prepared,
        protocol,
        protocol.models[0],
        run_split_agent_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    assert run.visibility is Visibility.SPLIT_PILLARS
    assert run.error == "runner failed: provider unavailable"
    # The split gateway keeps its own load, so a failed cell must not silently
    # borrow the GreptimeDB client's counters.
    assert load.query_count == 0


def test_every_arm_gets_the_same_agent_facing_query_timeout() -> None:
    source = Path("src/agent_rca_bench/transfer_formal.py").read_text()

    # A shorter budget on one store turns a slow query into a tool failure
    # there and a citable result elsewhere, which moves headline eligibility.
    assert "timeout=AGENT_QUERY_TIMEOUT_SECONDS," in source
    assert source.count("timeout=AGENT_QUERY_TIMEOUT_SECONDS,") == 2
    assert "timeout=120)" not in source
