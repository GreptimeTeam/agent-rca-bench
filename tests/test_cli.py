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
from semantic_rca_bench.protocol import (
    benchmark_protocol,
    discovery_protocol,
    graph_protocol,
    require_current_protocol,
)


def test_run_orders_rotate_every_level_through_each_position() -> None:
    levels = list(Visibility)

    orders = _run_orders(levels, repetitions=2, seed=7)

    assert len(orders) == 2
    assert all(set(order) == set(levels) for order in orders)
    for position in range(2):
        assert {order[position] for order in orders} == set(levels)


def test_batch_output_uses_case_identity(tmp_path) -> None:
    source = tmp_path / "smoke.json"
    source.write_text(
        json.dumps(
            {
                "case": {"source_case": "re2ob_checkoutservice_cpu_1"},
                "ground_truth": {"causal_component": "checkoutservice", "fault_type": "cpu"},
            }
        )
    )

    assert _batch_output(source, tmp_path) == (
        tmp_path / "v32-api-claude-sonnet-5-re2ob-checkoutservice-cpu-1.json"
    )


def test_noncurrent_protocol_cannot_start_new_agent_execution() -> None:
    require_current_protocol(32)

    with pytest.raises(ValueError, match="does not match current protocol v32"):
        require_current_protocol(30)


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


def test_aegis_source_commands_default_to_cohort_audit() -> None:
    audit = _parser().parse_args(
        [
            "aegis-audit",
            "--cases-dir",
            "cases",
            "--meta-dir",
            "meta",
            "--output",
            "audit.json",
        ]
    )
    fetch = _parser().parse_args(["aegis-fetch", "--output", "audit.json"])

    assert audit.selection is None
    assert fetch.selection is None


def test_formal_suite_separates_micro_preflight_from_paid_execution() -> None:
    preflight = _parser().parse_args(
        [
            "formal-suite-micro-preflight",
            "--run-root",
            "preflight-instances",
            "--source-audits-dir",
            "source-audits",
            "--output",
            "formal-micro.json",
        ]
    )
    execution = [
        "formal-suite-micro-run",
        "--run-root",
        "paid-instances",
        "--report",
        "formal-micro.json",
        "--live-audits-dir",
        "live-audits",
    ]

    assert not hasattr(preflight, "confirm_paid_api")
    assert str(preflight.protocol) == ("fixtures/reference/semantic-rca-v32-five-model-suite.json")
    with pytest.raises(SystemExit):
        _parser().parse_args(execution)
    paid = _parser().parse_args([*execution, "--max-new-runs", "1", "--confirm-paid-api"])
    assert paid.confirm_paid_api is True
    assert paid.max_new_runs == 1


def test_formal_suite_report_uses_current_public_fixtures() -> None:
    args = _parser().parse_args(
        [
            "formal-suite-report",
            "--micro-artifact",
            "micro.json",
            "--transfer-artifact",
            "transfer.json",
            "--output-json",
            "report.json",
            "--output-html",
            "report.html",
        ]
    )

    assert str(args.suite_protocol) == ("fixtures/reference/semantic-rca-v32-five-model-suite.json")
    assert str(args.transfer_protocol) == ("fixtures/reference/openrca2-transfer-v32-protocol.json")


def test_measurement_database_name_cannot_leak_ground_truth() -> None:
    truth = GroundTruth(causal_component="emailservice", fault_type="socket", inject_time=None)

    _assert_neutral_database_name("case_01", truth)
    with pytest.raises(ValueError, match="ground-truth component"):
        _assert_neutral_database_name("benchmark_emailservice", truth)
    with pytest.raises(ValueError, match="ground-truth fault type"):
        _assert_neutral_database_name("benchmark_socket", truth)


def test_rca100_requires_a_post_audit_smoke_report() -> None:
    source = {
        "case": {"adapter": "rca100"},
        "ground_truth": {
            "causal_component": "cart-pod",
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
    assert discovery_protocol()["treatments"] == ["raw", "semantic_graph"]
    assert discovery_protocol()["prompt"] == "case-preserving-double-quoted-identifiers-v2"
    assert "discovery_micro_benchmark" not in benchmark_protocol()


def test_graph_cli_freezes_balanced_two_treatment_schedule() -> None:
    args = _parser().parse_args(
        ["graph-run", "--report", "source.json", "--runner", "codex-subscription"]
    )

    assert args.repetitions == 2
    assert args.runner == "codex-subscription"
    assert graph_protocol()["treatments"] == ["raw", "semantic_graph"]
    assert "graph_micro_benchmark" not in benchmark_protocol()


def test_current_transfer_protocol_binds_the_extended_semantic_surface() -> None:
    assert benchmark_protocol()["semantic_graph"].endswith("v7")
    assert benchmark_protocol()["table_profile"].endswith("entity-roles-v3")
    assert benchmark_protocol()["treatment_estimand"] == "complete-agent-facing-interface-v1"
    assert benchmark_protocol()["treatment_components"] == {
        "raw": ["telemetry", "ordinary-schema-metadata", "read-only-sql"],
        "semantic_graph": [
            "telemetry",
            "ordinary-schema-metadata",
            "read-only-sql",
            "table-semantics",
            "semantic-entities",
            "semantic-relationships",
            "usage-guidance",
            "runtime-recovery-guidance",
            "coverage-snapshot",
        ],
    }


def test_openrca2_transfer_cli_separates_no_model_and_paid_commands() -> None:
    preflight = _parser().parse_args(
        [
            "transfer-preflight",
            "--phase",
            "pilot",
            "--run-root",
            "runs",
            "--output",
            "preflight.json",
        ]
    )
    execution = [
        "transfer-run",
        "--report",
        "preflight.json",
        "--run-dir",
        "live-run",
    ]

    assert not hasattr(preflight, "confirm_paid_api")
    assert str(preflight.protocol) == "fixtures/reference/openrca2-transfer-v32-protocol.json"
    with pytest.raises(SystemExit):
        _parser().parse_args(execution)
    paid = _parser().parse_args([*execution, "--confirm-paid-api"])
    assert paid.confirm_paid_api is True


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
        catalog_matched_table_count=30,
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
        == "included in input_tokens and retained separately in raw response usage"
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
            item(0, "semantic_graph", True, 4, 10, 2),
            item(1, "raw", True, 6, 40, 3),
            item(1, "semantic_graph", True, 4, 10, 2),
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


def test_graph_summary_pairs_semantic_graph_against_raw() -> None:
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
            item(0, "raw", False, None, None),
            item(0, "semantic_graph", True, 2, 10),
            item(1, "raw", True, 5, 40),
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
