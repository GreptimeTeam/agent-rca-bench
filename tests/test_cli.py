import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import semantic_rca_bench.cli as cli_module
from semantic_rca_bench.cli import (
    _assert_neutral_database_name,
    _batch_output,
    _discovery_output,
    _discovery_run_pair_descriptive,
    _graph_output,
    _graph_run_pair_descriptive,
    _ground_truth_from_report,
    _parser,
    _run_orders,
    _validate_microbenchmark_fixture_args,
    discovery_run,
)
from semantic_rca_bench.contracts import (
    AgentRunner,
    DatabaseLoad,
    GroundTruth,
    QueryResult,
    Visibility,
)
from semantic_rca_bench.discovery import DiscoveryAudit
from semantic_rca_bench.protocol import benchmark_protocol, discovery_protocol, graph_protocol


def test_run_orders_rotate_every_level_through_each_position() -> None:
    levels = list(Visibility)

    orders = _run_orders(levels, repetitions=3, seed=7)

    assert len(orders) == 3
    assert all(set(order) == set(levels) for order in orders)
    for position in range(3):
        assert {order[position] for order in orders} == set(levels)


def test_batch_output_uses_case_identity(tmp_path) -> None:
    source = tmp_path / "smoke.json"
    source.write_text(
        json.dumps(
            {
                "case": {"source_case": "re2ob_checkoutservice_cpu_1"},
                "ground_truth": {"component": "checkoutservice", "fault_type": "cpu"},
            }
        )
    )

    assert _batch_output(source, tmp_path) == (
        tmp_path / "v21-api-claude-sonnet-5-re2ob-checkoutservice-cpu-1.json"
    )


def test_run_accepts_subscription_runners() -> None:
    codex = _parser().parse_args(
        [
            "run",
            "--report",
            "source.json",
            "--runner",
            "codex-subscription",
            "--model",
            "gpt-5.6-luna",
        ]
    )
    claude = _parser().parse_args(
        [
            "run",
            "--report",
            "source.json",
            "--runner",
            "claude-subscription",
        ]
    )

    assert codex.runner == "codex-subscription"
    assert codex.model == "gpt-5.6-luna"
    assert claude.runner == "claude-subscription"


def test_case_role_defaults_to_development_and_accepts_measurement() -> None:
    default = _parser().parse_args(["run", "--report", "source.json"])
    measurement = _parser().parse_args(
        ["run", "--report", "source.json", "--case-role", "measurement"]
    )

    assert default.case_role == "development"
    assert measurement.case_role == "measurement"


def test_measurement_microbenchmark_requires_external_fixture(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"

    _validate_microbenchmark_fixture_args("measurement", fixture)
    _validate_microbenchmark_fixture_args("development", None)
    with pytest.raises(ValueError, match="require an external"):
        _validate_microbenchmark_fixture_args("measurement", None)
    with pytest.raises(ValueError, match="reserved for measurement"):
        _validate_microbenchmark_fixture_args("development", fixture)


def test_openrca2_smoke_defaults_to_the_frozen_measurement_case() -> None:
    args = _parser().parse_args(["smoke-openrca2"])

    assert args.case == "hs1-geo-pod-failure-drdmjj"
    assert str(args.cache_dir) == ".data/openrca2"


def test_measurement_database_name_cannot_leak_ground_truth() -> None:
    truth = GroundTruth(affected_component="emailservice", fault_type="socket", inject_time=None)

    _assert_neutral_database_name("case_01", truth)
    with pytest.raises(ValueError, match="ground-truth component"):
        _assert_neutral_database_name("benchmark_emailservice", truth)
    with pytest.raises(ValueError, match="ground-truth fault type"):
        _assert_neutral_database_name("benchmark_socket", truth)


def test_rca100_requires_a_post_audit_smoke_report() -> None:
    source = {
        "case": {"adapter": "rca100"},
        "ground_truth": {
            "component": "cart-pod",
            "fault_type": "redisUnavailable",
            "inject_time": None,
        },
    }

    with pytest.raises(ValueError, match="component-contract audit"):
        _ground_truth_from_report(source)

    source["ground_truth"]["component_scoreable"] = False
    truth = _ground_truth_from_report(source)
    assert not truth.component_scoreable


def test_discovery_cli_freezes_balanced_two_treatment_schedule() -> None:
    args = _parser().parse_args(
        ["discovery-run", "--report", "source.json", "--runner", "codex-subscription"]
    )

    assert args.repetitions == 2
    assert args.runner == "codex-subscription"
    assert discovery_protocol()["treatments"] == ["raw", "table_semantics"]
    assert discovery_protocol()["prompt"] == "case-preserving-double-quoted-identifiers-v2"
    assert "discovery_micro_benchmark" not in benchmark_protocol()


def test_graph_cli_freezes_balanced_two_treatment_schedule() -> None:
    args = _parser().parse_args(
        ["graph-run", "--report", "source.json", "--runner", "codex-subscription"]
    )

    assert args.repetitions == 2
    assert args.runner == "codex-subscription"
    assert graph_protocol()["treatments"] == ["table_semantics", "semantic_graph"]
    assert "graph_micro_benchmark" not in benchmark_protocol()


def test_discovery_runner_persists_failed_cells_and_continues(monkeypatch, tmp_path) -> None:
    source = tmp_path / "market-smoke.json"
    output = tmp_path / "discovery.json"
    source.write_text(
        json.dumps(
            {
                "case": {"source_case": "Market/cloudbed-1@2022-03-21T03:30"},
                "server": {"endpoint": "http://127.0.0.1:4100", "database": "case_market_01"},
            }
        )
    )
    canonical = QueryResult(
        query_id="canonical",
        columns=["phase", "sample_count", "mean_value", "max_value"],
        rows=[
            ["baseline", 8, 15.9375, 64.5],
            ["incident", 3, 224.5, 502.5],
        ],
        elapsed_seconds=0.01,
    )
    audit = DiscoveryAudit(
        fixture_id="openrca-market-node-write-io",
        canonical_query="SELECT 1",
        canonical_result=canonical,
        predicate_match=True,
        catalog_query="node disk write I/O",
        catalog_matched_table_count=29,
        catalog_target_rank=2,
        catalog_target_in_top_five=True,
        catalog_top_five=["system_io_w_await", "system_io_w_s"],
    )

    class FakeClient:
        def __init__(self, endpoint, database, timeout):
            self.endpoint = endpoint
            self.database = database

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def status(self):
            return {"version": "test"}

        @contextmanager
        def measure_query_load(self):
            yield DatabaseLoad()

    monkeypatch.setattr(cli_module, "GreptimeClient", FakeClient)
    monkeypatch.setattr(cli_module, "audit_discovery_fixture", lambda *_: audit)
    monkeypatch.setattr(
        cli_module,
        "run_discovery_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    args = _parser().parse_args(
        [
            "discovery-run",
            "--report",
            str(source),
            "--repetitions",
            "2",
            "--output",
            str(output),
        ]
    )

    assert discovery_run(args) == 0
    report = json.loads(output.read_text())
    assert len(report["runs"]) == 4
    assert all(item["run"]["answer"] is None for item in report["runs"])
    assert all(item["evaluation"]["success"] is False for item in report["runs"])
    assert report["discovery_report_schema_version"] == 2
    assert (
        report["token_accounting"]["cached_input"]
        == "separate raw response fields; omitted from legacy run.usage"
    )
    assert report["run_pair_descriptive"]["task_success"] == {
        "paired_observations": 2,
        "improvements": 0,
        "regressions": 0,
        "ties": 2,
    }
    assert report["run_pair_descriptive"]["inference_role"].startswith("descriptive only")


def test_discovery_summary_pairs_success_and_efficiency_by_repetition() -> None:
    def item(repetition, visibility, success, tools, rows, discovery):
        return {
            "repetition": repetition,
            "run": {"visibility": visibility},
            "evaluation": {
                "success": success,
                "tool_calls_through_evidence": tools,
                "rows_returned_through_evidence": rows,
                "discovery_calls_through_evidence": discovery,
            },
        }

    summary = _discovery_run_pair_descriptive(
        [
            item(0, "raw", False, None, None, None),
            item(0, "table_semantics", True, 4, 10, 2),
            item(1, "raw", True, 6, 40, 3),
            item(1, "table_semantics", True, 4, 10, 2),
        ]
    )

    assert summary["task_success"] == {
        "paired_observations": 2,
        "improvements": 1,
        "regressions": 0,
        "ties": 1,
    }
    efficiency = {item["metric"]: item for item in summary["successful_pair_efficiency"]}
    assert efficiency["tool_calls_through_evidence"] == {
        "metric": "tool_calls_through_evidence",
        "paired_observations": 1,
        "better": 1,
        "worse": 0,
        "ties": 0,
        "median_delta": -2.0,
    }
    assert summary["statistical_unit"] == "run pair within one case"
    assert "sign_test_p_value" not in json.dumps(summary)


def test_graph_summary_pairs_semantic_graph_against_table_semantics() -> None:
    def item(repetition, visibility, success, tools, rows):
        return {
            "repetition": repetition,
            "run": {"visibility": visibility},
            "evaluation": {
                "success": success,
                "tool_calls_through_evidence": tools,
                "rows_returned_through_evidence": rows,
            },
        }

    summary = _graph_run_pair_descriptive(
        [
            item(0, "table_semantics", False, None, None),
            item(0, "semantic_graph", True, 2, 10),
            item(1, "table_semantics", True, 5, 40),
            item(1, "semantic_graph", True, 2, 10),
        ]
    )

    assert summary["task_success"] == {
        "paired_observations": 2,
        "improvements": 1,
        "regressions": 0,
        "ties": 1,
    }
    efficiency = {item["metric"]: item for item in summary["successful_pair_efficiency"]}
    assert efficiency["rows_returned_through_evidence"]["median_delta"] == -30.0


def test_microbenchmark_default_outputs_use_protocol_versions() -> None:
    assert _discovery_output("case", AgentRunner.API, "test-model") == (
        Path(".reports") / f"discovery-v{discovery_protocol()['version']}-api-test-model-case.json"
    )
    assert _graph_output("case", AgentRunner.API, "test-model") == (
        Path(".reports") / f"graph-v{graph_protocol()['version']}-api-test-model-case.json"
    )
