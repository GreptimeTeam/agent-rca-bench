from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median

from semantic_rca_bench.agent import run_agent
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    ApiTransport,
    CaseInput,
    GroundTruth,
    QueryResult,
    Visibility,
)
from semantic_rca_bench.datasets.aegis import AegisRepository
from semantic_rca_bench.datasets.aegis import audit_cohort as audit_aegis_cohort
from semantic_rca_bench.datasets.openrca import (
    DATASET_REVISION as OPENRCA_DATASET_REVISION,
)
from semantic_rca_bench.datasets.openrca import (
    DEFAULT_BANK_CASE,
    OpenRCARepository,
    source_audit,
)
from semantic_rca_bench.datasets.openrca import (
    MIRROR_REVISION as OPENRCA_MIRROR_REVISION,
)
from semantic_rca_bench.datasets.openrca import (
    SOURCE_REVISION as OPENRCA_SOURCE_REVISION,
)
from semantic_rca_bench.datasets.openrca import ingest_case as ingest_openrca_case
from semantic_rca_bench.datasets.openrca import validate_ingest as validate_openrca_ingest
from semantic_rca_bench.datasets.openrca2 import (
    DATASET_REVISION as OPENRCA2_DATASET_REVISION,
)
from semantic_rca_bench.datasets.openrca2 import (
    DEFAULT_CASE as DEFAULT_OPENRCA2_CASE,
)
from semantic_rca_bench.datasets.openrca2 import (
    SOURCE_REVISION as OPENRCA2_SOURCE_REVISION,
)
from semantic_rca_bench.datasets.openrca2 import OpenRCA2Repository
from semantic_rca_bench.datasets.openrca2 import ingest_case as ingest_openrca2_case
from semantic_rca_bench.datasets.openrca2 import source_audit as source_audit_openrca2
from semantic_rca_bench.datasets.openrca2 import validate_ingest as validate_openrca2_ingest
from semantic_rca_bench.datasets.openrca2_transfer import (
    build_selection_fixture as build_openrca2_transfer_selection,
)
from semantic_rca_bench.datasets.rca100 import (
    DATASET_REVISION as RCA100_DATASET_REVISION,
)
from semantic_rca_bench.datasets.rca100 import (
    SOURCE_REVISION as RCA100_SOURCE_REVISION,
)
from semantic_rca_bench.datasets.rca100 import (
    RCA100Repository,
    reference_topology_summary,
    validate_ingest,
)
from semantic_rca_bench.datasets.rca100 import ingest_case as ingest_rca100_case
from semantic_rca_bench.datasets.rcaeval import (
    DATASET_REVISION as RCAEVAL_DATASET_REVISION,
)
from semantic_rca_bench.datasets.rcaeval import (
    SOURCE_REVISION as RCAEVAL_SOURCE_REVISION,
)
from semantic_rca_bench.datasets.rcaeval import RCAEvalRepository
from semantic_rca_bench.datasets.rcaeval import ingest_case as ingest_rcaeval_case
from semantic_rca_bench.datasets.rcaeval import source_audit as source_audit_rcaeval
from semantic_rca_bench.datasets.rcaeval import validate_ingest as validate_rcaeval_ingest
from semantic_rca_bench.discovery import (
    DISCOVERY_MAX_TOOL_CALLS,
    DiscoveryAgentRun,
    DiscoveryFixture,
    audit_discovery_fixture,
    evaluate_discovery_run,
    failed_discovery_run,
    fixture_for_source_case,
    run_discovery_agent,
)
from semantic_rca_bench.evaluation import evaluate
from semantic_rca_bench.formal_report import (
    build_formal_measurement_report_from_files,
    render_formal_measurement_report,
    validate_formal_measurement_report,
)
from semantic_rca_bench.formal_suite import (
    MicroEnvironmentConfig,
    build_micro_preflight_report,
    execute_micro_case_runs,
    prepare_micro_environment,
    validate_micro_report,
    write_micro_report,
)
from semantic_rca_bench.formal_suite_protocol import (
    DEFAULT_SUITE_PROTOCOL_FIXTURE,
    load_formal_suite_protocol,
)
from semantic_rca_bench.formal_suite_release import (
    build_micro_measurement_artifact_from_files,
    validate_micro_measurement_artifact,
)
from semantic_rca_bench.graph_benchmark import (
    GRAPH_MAX_TOOL_CALLS,
    GraphAgentRun,
    GraphAudit,
    GraphFixture,
    audit_graph_fixture,
    edge_results_match,
    evaluate_graph_run,
    failed_graph_run,
    run_graph_agent,
    validate_source_window,
)
from semantic_rca_bench.graph_benchmark import (
    fixture_for_source_case as graph_fixture_for_source_case,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.greptimedb.server import inspect_checkout, write_json
from semantic_rca_bench.greptimedb.visibility import QueryGateway
from semantic_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
    summarize_semantic_surfaces,
)
from semantic_rca_bench.protocol import (
    benchmark_protocol,
    discovery_protocol,
    graph_protocol,
)
from semantic_rca_bench.protocol import (
    run_orders as _run_orders,
)
from semantic_rca_bench.report import (
    CASE_REPORT_SCHEMA_VERSION,
    TOKEN_ACCOUNTING,
    case_context,
    render_reports,
)
from semantic_rca_bench.subscription import run_subscription_agent
from semantic_rca_bench.transfer_adjudication import build_semantic_adjudication_queue
from semantic_rca_bench.transfer_formal import (
    TransferEnvironmentConfig as OpenRCA2TransferEnvironmentConfig,
)
from semantic_rca_bench.transfer_formal import (
    bind_pilot_gate as bind_openrca2_pilot_gate,
)
from semantic_rca_bench.transfer_formal import (
    build_preflight_report as build_openrca2_transfer_preflight,
)
from semantic_rca_bench.transfer_formal import (
    execute_case_runs as execute_openrca2_transfer_case_runs,
)
from semantic_rca_bench.transfer_formal import (
    prepare_transfer_environment as prepare_openrca2_transfer_environment,
)
from semantic_rca_bench.transfer_formal import validate_private_report
from semantic_rca_bench.transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE as DEFAULT_OPENRCA2_TRANSFER_PROTOCOL,
)
from semantic_rca_bench.transfer_protocol import load_transfer_protocol
from semantic_rca_bench.transfer_release import (
    build_measurement_artifact as build_openrca2_transfer_artifact,
)

DEFAULT_GREPTIMEDB_REPO = Path("/Users/dennis/programming/rust/greptimedb")


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runner",
        choices=[runner.value for runner in AgentRunner],
        default=AgentRunner.API.value,
    )
    parser.add_argument("--model", default="claude-sonnet-5")
    _add_api_transport_arguments(parser)
    parser.add_argument(
        "--levels",
        nargs="+",
        choices=[level.value for level in Visibility],
        default=[level.value for level in Visibility],
    )
    parser.add_argument("--max-tool-calls", type=int, default=48)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--case-role",
        choices=["development", "measurement"],
        default="development",
    )


def _add_api_transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-transport",
        choices=[transport.value for transport in ApiTransport],
        default=ApiTransport.ANTHROPIC_MESSAGES.value,
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high", "xhigh", "max"],
    )
    parser.add_argument("--max-output-tokens", type=int, default=4096)


def _add_formal_suite_environment_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--openrca-cache-dir",
        type=Path,
        default=Path(".data/openrca"),
    )
    parser.add_argument(
        "--openrca2-cache-dir",
        type=Path,
        default=Path(".data/openrca2"),
    )
    parser.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument(
        "--protocol",
        type=Path,
        default=DEFAULT_SUITE_PROTOCOL_FIXTURE,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semantic-rca")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)

    aegis_audit = subparsers.add_parser("aegis-audit")
    aegis_audit.add_argument("--cases-dir", type=Path, required=True)
    aegis_audit.add_argument("--meta-dir", type=Path, required=True)
    aegis_audit.add_argument(
        "--selection",
        type=Path,
    )
    aegis_audit.add_argument("--output", type=Path, required=True)

    aegis_fetch = subparsers.add_parser("aegis-fetch")
    aegis_fetch.add_argument("--cache-dir", type=Path, default=Path(".data/aegis"))
    aegis_fetch.add_argument(
        "--selection",
        type=Path,
    )
    aegis_fetch.add_argument("--output", type=Path, required=True)

    suite_preflight = subparsers.add_parser("formal-suite-micro-preflight")
    _add_formal_suite_environment_arguments(suite_preflight)
    suite_preflight.add_argument("--source-audits-dir", type=Path, required=True)
    suite_preflight.add_argument("--output", type=Path, required=True)

    suite_run = subparsers.add_parser("formal-suite-micro-run")
    _add_formal_suite_environment_arguments(suite_run)
    suite_run.add_argument("--report", type=Path, required=True)
    suite_run.add_argument("--live-audits-dir", type=Path, required=True)
    suite_run.add_argument(
        "--confirm-paid-api",
        action="store_true",
        required=True,
        help="acknowledge that this invocation may execute pending paid API cells",
    )
    suite_run.add_argument(
        "--max-new-runs",
        type=int,
        help="execute at most this many pending micro-benchmark cells",
    )

    suite_export = subparsers.add_parser("formal-suite-micro-export")
    suite_export.add_argument("--run-report", type=Path, required=True)
    suite_export.add_argument(
        "--protocol",
        type=Path,
        default=DEFAULT_SUITE_PROTOCOL_FIXTURE,
    )
    suite_export.add_argument("--output", type=Path, required=True)

    suite_report = subparsers.add_parser("formal-suite-report")
    suite_report.add_argument("--micro-artifact", type=Path, required=True)
    suite_report.add_argument("--transfer-artifact", type=Path, required=True)
    suite_report.add_argument(
        "--suite-protocol",
        type=Path,
        default=DEFAULT_SUITE_PROTOCOL_FIXTURE,
    )
    suite_report.add_argument(
        "--transfer-protocol",
        type=Path,
        default=DEFAULT_OPENRCA2_TRANSFER_PROTOCOL,
    )
    suite_report.add_argument("--output-json", type=Path, required=True)
    suite_report.add_argument("--output-html", type=Path, required=True)

    transfer_selection = subparsers.add_parser("transfer-selection-audit")
    transfer_selection.add_argument("--cache-dir", type=Path, default=Path(".data/openrca2"))
    transfer_selection.add_argument(
        "--manifest", type=Path, default=Path(".data/openrca2/manifest.jsonl")
    )
    transfer_selection.add_argument(
        "--protocol", type=Path, default=DEFAULT_OPENRCA2_TRANSFER_PROTOCOL
    )
    transfer_selection.add_argument("--output", type=Path, required=True)

    transfer_preflight = subparsers.add_parser("transfer-preflight")
    transfer_preflight.add_argument("--phase", choices=["pilot", "measurement"], required=True)
    transfer_preflight.add_argument("--cache-dir", type=Path, default=Path(".data/openrca2"))
    transfer_preflight.add_argument(
        "--manifest", type=Path, default=Path(".data/openrca2/manifest.jsonl")
    )
    transfer_preflight.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    transfer_preflight.add_argument("--run-root", type=Path, required=True)
    transfer_preflight.add_argument(
        "--protocol", type=Path, default=DEFAULT_OPENRCA2_TRANSFER_PROTOCOL
    )
    transfer_preflight.add_argument("--output", type=Path, required=True)

    transfer_run = subparsers.add_parser("transfer-run")
    transfer_run.add_argument("--report", type=Path, required=True)
    transfer_run.add_argument("--pilot-report", type=Path)
    transfer_run.add_argument("--cache-dir", type=Path, default=Path(".data/openrca2"))
    transfer_run.add_argument(
        "--manifest", type=Path, default=Path(".data/openrca2/manifest.jsonl")
    )
    transfer_run.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    transfer_run.add_argument("--run-dir", type=Path, required=True)
    transfer_run.add_argument("--protocol", type=Path, default=DEFAULT_OPENRCA2_TRANSFER_PROTOCOL)
    transfer_run.add_argument("--confirm-paid-api", action="store_true", required=True)
    transfer_run.add_argument("--max-new-runs", type=int)

    transfer_export = subparsers.add_parser("transfer-export")
    transfer_export.add_argument("--run-report", type=Path, required=True)
    transfer_export.add_argument(
        "--protocol", type=Path, default=DEFAULT_OPENRCA2_TRANSFER_PROTOCOL
    )
    transfer_export.add_argument("--adjudication", type=Path)
    transfer_export.add_argument("--output", type=Path, required=True)

    transfer_adjudication = subparsers.add_parser("transfer-adjudication-queue")
    transfer_adjudication.add_argument("--run-report", type=Path, required=True)
    transfer_adjudication.add_argument(
        "--protocol", type=Path, default=DEFAULT_OPENRCA2_TRANSFER_PROTOCOL
    )
    transfer_adjudication.add_argument("--output", type=Path, required=True)

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    smoke.add_argument("--cache-dir", type=Path, default=Path(".cache/datasets/rcaeval"))
    smoke.add_argument("--reports-dir", type=Path, default=Path(".reports"))
    smoke.add_argument("--endpoint", default="http://127.0.0.1:4000")
    smoke.add_argument("--database")
    smoke.add_argument("--case")
    smoke.add_argument("--dataset", default="RE2-OB")
    smoke.add_argument("--fault", default="delay")

    rca100_smoke = subparsers.add_parser("smoke-rca100")
    rca100_smoke.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    rca100_smoke.add_argument("--cache-dir", type=Path, default=Path(".cache/datasets/rca100"))
    rca100_smoke.add_argument("--reports-dir", type=Path, default=Path(".reports"))
    rca100_smoke.add_argument("--endpoint", default="http://127.0.0.1:4000")
    rca100_smoke.add_argument("--database")
    rca100_smoke.add_argument("--task", default="t001")

    openrca_smoke = subparsers.add_parser("smoke-openrca")
    openrca_smoke.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    openrca_smoke.add_argument("--cache-dir", type=Path, default=Path(".data/openrca"))
    openrca_smoke.add_argument("--reports-dir", type=Path, default=Path(".reports"))
    openrca_smoke.add_argument("--endpoint", default="http://127.0.0.1:4000")
    openrca_smoke.add_argument("--database")
    openrca_smoke.add_argument("--case", default=DEFAULT_BANK_CASE)

    openrca2_smoke = subparsers.add_parser("smoke-openrca2")
    openrca2_smoke.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)
    openrca2_smoke.add_argument("--cache-dir", type=Path, default=Path(".data/openrca2"))
    openrca2_smoke.add_argument("--reports-dir", type=Path, default=Path(".reports"))
    openrca2_smoke.add_argument("--endpoint", default="http://127.0.0.1:4000")
    openrca2_smoke.add_argument("--database")
    openrca2_smoke.add_argument("--case", default=DEFAULT_OPENRCA2_CASE)

    run = subparsers.add_parser("run")
    run.add_argument("--report", type=Path, required=True)
    _add_run_arguments(run)
    run.add_argument("--output", type=Path)

    batch = subparsers.add_parser("batch")
    batch.add_argument("--report", type=Path, nargs="+", required=True)
    _add_run_arguments(batch)
    batch.add_argument("--jobs", type=int, default=1)
    batch.add_argument("--reports-dir", type=Path, default=Path(".reports"))

    discovery_audit = subparsers.add_parser("discovery-audit")
    discovery_audit.add_argument("--report", type=Path, required=True)
    discovery_audit.add_argument("--fixture", type=Path)
    discovery_audit.add_argument(
        "--case-role", choices=["development", "measurement"], default="development"
    )
    discovery_audit.add_argument("--output", type=Path)

    discovery_run = subparsers.add_parser("discovery-run")
    discovery_run.add_argument("--report", type=Path, required=True)
    discovery_run.add_argument("--fixture", type=Path)
    discovery_run.add_argument(
        "--case-role", choices=["development", "measurement"], default="development"
    )
    discovery_run.add_argument(
        "--runner",
        choices=[runner.value for runner in AgentRunner],
        default=AgentRunner.API.value,
    )
    discovery_run.add_argument("--model", default="claude-sonnet-5")
    _add_api_transport_arguments(discovery_run)
    discovery_run.add_argument("--repetitions", type=int, default=2)
    discovery_run.add_argument("--seed", type=int, default=0)
    discovery_run.add_argument("--output", type=Path)

    graph_audit = subparsers.add_parser("graph-audit")
    graph_audit.add_argument("--report", type=Path, required=True)
    graph_audit.add_argument("--fixture", type=Path)
    graph_audit.add_argument(
        "--case-role", choices=["development", "measurement"], default="development"
    )
    graph_audit.add_argument("--output", type=Path)

    graph_run = subparsers.add_parser("graph-run")
    graph_run.add_argument("--report", type=Path, required=True)
    graph_run.add_argument("--fixture", type=Path)
    graph_run.add_argument(
        "--case-role", choices=["development", "measurement"], default="development"
    )
    graph_run.add_argument(
        "--runner",
        choices=[runner.value for runner in AgentRunner],
        default=AgentRunner.API.value,
    )
    graph_run.add_argument("--model", default="claude-sonnet-5")
    _add_api_transport_arguments(graph_run)
    graph_run.add_argument("--repetitions", type=int, default=2)
    graph_run.add_argument("--seed", type=int, default=0)
    graph_run.add_argument("--output", type=Path)

    render = subparsers.add_parser("render")
    render.add_argument("--report", type=Path, nargs="+", required=True)
    render.add_argument("--output", type=Path)

    return parser


def doctor(repo: Path) -> int:
    metadata = inspect_checkout(
        repo,
        expected_branch="feat/semantic-graph-declaration-visibility",
    )
    print(json.dumps(metadata, indent=2))
    return 0


def aegis_audit(args: argparse.Namespace) -> int:
    output = audit_aegis_cohort(
        args.cases_dir,
        args.meta_dir,
        selection_path=args.selection,
    )
    write_json(args.output, output)
    print(args.output)
    return 0


def aegis_fetch(args: argparse.Namespace) -> int:
    cases_dir, meta_dir = AegisRepository(args.cache_dir).fetch()
    output = audit_aegis_cohort(
        cases_dir,
        meta_dir,
        archive_checksum_verified=True,
        selection_path=args.selection,
    )
    write_json(args.output, output)
    print(args.output)
    return 0


def formal_suite_micro_preflight(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"refusing to overwrite formal suite preflight: {args.output}")
    if args.run_root.exists():
        raise ValueError(f"formal suite run root already exists: {args.run_root}")
    if args.source_audits_dir.exists():
        raise ValueError(
            f"formal suite source audit directory already exists: {args.source_audits_dir}"
        )
    suite, transfer = load_formal_suite_protocol(args.protocol)
    source_audits: list[dict[str, object]] = []
    args.source_audits_dir.mkdir(parents=True)
    for case_index, case_contract in enumerate(suite.micro_cases):
        source_report = None
        with prepare_micro_environment(
            suite,
            args.protocol,
            case_contract,
            _micro_environment_config(args, case_index),
        ) as prepared:
            source_report = prepared.source_audit
        if source_report is None:
            raise ValueError("formal suite source preflight produced no audit")
        write_json(args.source_audits_dir / f"case-{case_index + 1:02d}.json", source_report)
        source_audits.append(source_report)
    report = build_micro_preflight_report(
        suite,
        transfer,
        args.protocol,
        source_audits,
    )
    write_micro_report(args.output, report)
    print(args.output)
    return 0


def formal_suite_micro_run(args: argparse.Namespace) -> int:
    if args.confirm_paid_api is not True:
        raise ValueError("formal suite micro run requires explicit paid API confirmation")
    if not args.report.is_file():
        raise ValueError("formal suite micro run requires an existing preflight report")
    if args.run_root.exists():
        raise ValueError(f"formal suite run root already exists: {args.run_root}")
    if args.live_audits_dir.exists():
        raise ValueError(
            f"formal suite live audit directory already exists: {args.live_audits_dir}"
        )
    if args.max_new_runs is not None and args.max_new_runs < 1:
        raise ValueError("max_new_runs must be positive")
    suite, transfer = load_formal_suite_protocol(args.protocol)
    report = _read_json_object(args.report)
    validate_micro_report(report, suite, transfer, args.protocol)
    args.live_audits_dir.mkdir(parents=True)
    remaining = args.max_new_runs
    try:
        while report["execution"]["complete"] is not True and (remaining is None or remaining > 0):
            schedule = report["schedule"]
            runs = report["runs"]
            if not isinstance(schedule, list) or not isinstance(runs, list):
                raise ValueError("formal suite micro report schedule or runs are malformed")
            next_cell = schedule[len(runs)]
            if not isinstance(next_cell, dict):
                raise ValueError("formal suite micro schedule cell is malformed")
            case_index = int(next_cell["case_index"])
            case_contract = suite.micro_cases[case_index]
            source_report = None
            before = len(runs)
            try:
                with prepare_micro_environment(
                    suite,
                    args.protocol,
                    case_contract,
                    _micro_environment_config(args, case_index),
                ) as prepared:
                    source_report = prepared.source_audit
                    execute_micro_case_runs(
                        report,
                        suite,
                        transfer,
                        args.protocol,
                        prepared,
                        paid_api_confirmed=True,
                        max_new_runs=remaining,
                        on_update=lambda value: write_micro_report(args.report, value),
                    )
            finally:
                if source_report is not None:
                    write_json(
                        args.live_audits_dir / f"case-{case_index + 1:02d}.json",
                        source_report,
                    )
                write_micro_report(args.report, report)
            completed = len(report["runs"]) - before
            if completed < 1:
                raise ValueError("formal suite micro execution made no progress")
            if remaining is not None:
                remaining -= completed
    finally:
        write_micro_report(args.report, report)
    execution = report["execution"]
    print(args.report)
    return 0 if execution["runner_errors"] == 0 and execution["budget_exhaustions"] == 0 else 1


def formal_suite_micro_export(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"refusing to overwrite formal micro artifact: {args.output}")
    artifact = build_micro_measurement_artifact_from_files(args.run_report, args.protocol)
    validate_micro_measurement_artifact(artifact, args.protocol)
    write_json(args.output, artifact)
    print(args.output)
    return 0


def formal_suite_report(args: argparse.Namespace) -> int:
    for output in (args.output_json, args.output_html):
        if output.exists():
            raise ValueError(f"refusing to overwrite formal measurement report: {output}")
    report = build_formal_measurement_report_from_files(
        args.micro_artifact,
        args.transfer_artifact,
        args.suite_protocol,
        args.transfer_protocol,
    )
    validate_formal_measurement_report(report)
    write_json(args.output_json, report)
    render_formal_measurement_report(report, args.output_html)
    print(args.output_json)
    print(args.output_html)
    return 0


def transfer_selection_audit(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"refusing to overwrite transfer selection audit: {args.output}")
    _, frozen, _ = load_transfer_protocol(args.protocol)
    rebuilt = build_openrca2_transfer_selection(
        args.cache_dir,
        args.manifest,
        trajectory_exclusions=frozen.trajectory_exclusions,
        case_role="measurement",
    )
    report = {
        "mode": "semantic-rca-openrca2-transfer-selection-audit",
        "frozen_selection": frozen.model_dump(mode="json"),
        "rebuilt_selection": rebuilt.model_dump(mode="json"),
        "exact_match": rebuilt == frozen,
        "no_model_gates": {"all_passed": rebuilt == frozen},
    }
    write_json(args.output, report)
    print(args.output)
    return 0 if rebuilt == frozen else 1


def transfer_preflight(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"refusing to overwrite transfer preflight: {args.output}")
    if args.run_root.exists():
        raise ValueError(f"transfer preflight run root already exists: {args.run_root}")
    protocol, measurement, pilot = load_transfer_protocol(args.protocol)
    selection = pilot if args.phase == "pilot" else measurement
    source_audits = []
    for spec in selection.selected_cases:
        source_report = None
        config = OpenRCA2TransferEnvironmentConfig(
            cache_dir=args.cache_dir,
            manifest_path=args.manifest,
            greptimedb_repo=args.greptimedb_repo,
            run_dir=args.run_root / spec.opaque_case_id,
            database=spec.opaque_case_id.replace("-", "_"),
        )
        with prepare_openrca2_transfer_environment(protocol, spec, config) as prepared:
            source_report = prepared.source_audit
        if source_report is None:
            raise ValueError("transfer no-model preflight produced no source audit")
        source_audits.append(source_report)
    report = build_openrca2_transfer_preflight(
        protocol,
        args.protocol,
        selection,
        source_audits,
        phase=args.phase,
    )
    write_json(args.output, report)
    print(args.output)
    return 0


def transfer_run(args: argparse.Namespace) -> int:
    if args.confirm_paid_api is not True:
        raise ValueError("transfer run requires explicit paid API confirmation")
    if not args.report.is_file():
        raise ValueError("transfer run requires an existing preflight report")
    protocol, measurement, pilot = load_transfer_protocol(args.protocol)
    report = _read_json_object(args.report)
    phase = report.get("phase")
    selection = pilot if phase == "pilot" else measurement if phase == "measurement" else None
    if selection is None:
        raise ValueError("transfer report has an unsupported phase")
    validate_private_report(report, protocol, args.protocol, selection)
    if phase == "measurement" and report.get("pilot_gate") is None:
        if args.pilot_report is None:
            raise ValueError("measurement execution requires --pilot-report")
        pilot_report = _read_json_object(args.pilot_report)
        validate_private_report(
            pilot_report,
            protocol,
            args.protocol,
            pilot,
            require_complete=True,
        )
        bind_openrca2_pilot_gate(report, pilot_report, protocol)
        write_json(args.report, report)
    execution = report.get("execution")
    if not isinstance(execution, dict) or execution.get("complete") is True:
        print(args.report)
        return 0
    schedule = report.get("schedule")
    runs = report.get("runs")
    if not isinstance(schedule, list) or not isinstance(runs, list):
        raise ValueError("transfer report schedule or runs are malformed")
    next_cell = schedule[len(runs)]
    if not isinstance(next_cell, dict):
        raise ValueError("transfer schedule cell is malformed")
    spec = selection.selected_cases[int(next_cell["case_index"])]
    config = OpenRCA2TransferEnvironmentConfig(
        cache_dir=args.cache_dir,
        manifest_path=args.manifest,
        greptimedb_repo=args.greptimedb_repo,
        run_dir=args.run_dir,
        database=spec.opaque_case_id.replace("-", "_"),
    )
    try:
        with prepare_openrca2_transfer_environment(protocol, spec, config) as prepared:
            execute_openrca2_transfer_case_runs(
                report,
                protocol,
                args.protocol,
                selection,
                prepared,
                paid_api_confirmed=True,
                max_new_runs=args.max_new_runs,
                on_update=lambda value: write_json(args.report, value),
            )
    finally:
        write_json(args.report, report)
    print(args.report)
    execution = report["execution"]
    return 0 if execution["runner_errors"] == 0 else 1


def transfer_export(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"refusing to overwrite transfer artifact: {args.output}")
    private = _read_json_object(args.run_report)
    adjudication = _read_json_object(args.adjudication) if args.adjudication is not None else None
    artifact = build_openrca2_transfer_artifact(private, args.protocol, adjudication)
    write_json(args.output, artifact)
    print(args.output)
    return 0


def transfer_adjudication_queue(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise ValueError(f"refusing to overwrite adjudication queue: {args.output}")
    report = _read_json_object(args.run_report)
    protocol, measurement, pilot = load_transfer_protocol(args.protocol)
    selection = (
        pilot
        if report.get("phase") == "pilot"
        else measurement
        if report.get("phase") == "measurement"
        else None
    )
    if selection is None:
        raise ValueError("adjudication report has an unsupported phase")
    queue = build_semantic_adjudication_queue(report, protocol, selection)
    write_json(args.output, queue)
    print(args.output)
    return 0


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _micro_environment_config(args: argparse.Namespace, case_index: int) -> MicroEnvironmentConfig:
    return MicroEnvironmentConfig(
        openrca_cache_dir=args.openrca_cache_dir,
        openrca2_cache_dir=args.openrca2_cache_dir,
        greptimedb_repo=args.greptimedb_repo,
        run_dir=args.run_root / f"case-{case_index + 1:02d}",
        database=f"suite_case_{case_index + 1:02d}",
    )


def smoke(args: argparse.Namespace) -> int:
    checkout = inspect_checkout(
        args.greptimedb_repo,
        expected_branch="feat/semantic-graph-declaration-visibility",
    )
    source_case = args.case
    repository = RCAEvalRepository(args.cache_dir)
    if source_case is None:
        source_case = repository.select_case(dataset=args.dataset, fault=args.fault)
    case = repository.fetch_case(source_case)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    database = args.database or f"semantic_rca_bench_{run_id.replace('-', '_')}"
    case = case.model_copy(update={"input": case.input.model_copy(update={"database": database})})
    report_path = args.reports_dir / f"smoke-{run_id}.json"
    audit = source_audit_rcaeval(case)
    with GreptimeClient(args.endpoint, database=case.input.database) as client:
        server_status = client.status()
        client.create_database(database)
        graph_isolation = assert_semantic_graph_window_empty(client, case.input)
        counts = ingest_rcaeval_case(client, case)
        surfaces = inspect_semantic_surfaces(client, case.input)
        validation = validate_rcaeval_ingest(client, case, counts)
    report = {
        "greptimedb": checkout,
        "server": {
            "endpoint": args.endpoint,
            "database": database,
            "status": server_status,
        },
        "dataset_revision": RCAEVAL_DATASET_REVISION,
        "adapter_source_revision": RCAEVAL_SOURCE_REVISION,
        "case": {
            "adapter": "rcaeval",
            "source_case": case.source_case,
            "dataset": case.dataset,
            "system": case.system,
            "time_start": case.input.time_start,
            "time_end": case.input.time_end,
            "alert_time": case.input.alert_time,
            "alert_text": case.input.alert_text,
            "fault_taxonomy": case.input.fault_taxonomy,
            "alert_source": "synthetic-generic",
        },
        "source_audit": audit,
        "graph_isolation": graph_isolation,
        "ingest": counts.model_dump(mode="json"),
        "semantic_surfaces": surfaces,
        "validation": validation,
        "ground_truth": case.ground_truth.model_dump(mode="json"),
    }
    write_json(report_path, report)
    print(report_path)
    return 0


def smoke_rca100(args: argparse.Namespace) -> int:
    checkout = inspect_checkout(
        args.greptimedb_repo,
        expected_branch="feat/semantic-graph-declaration-visibility",
    )
    case = RCA100Repository(args.cache_dir).fetch_case(args.task)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    database = args.database or f"semantic_rca100_{args.task}_{run_id.replace('-', '_')}"
    case = case.model_copy(update={"input": case.input.model_copy(update={"database": database})})
    report_path = args.reports_dir / f"smoke-rca100-{args.task}-{run_id}.json"
    with GreptimeClient(args.endpoint, database=database, timeout=120) as client:
        server_status = client.status()
        client.create_database(database)
        graph_isolation = assert_semantic_graph_window_empty(client, case.input)
        counts = ingest_rca100_case(client, case)
        surfaces = inspect_semantic_surfaces(client, case.input)
        validation = validate_ingest(client, case, counts)
    report = {
        "greptimedb": checkout,
        "server": {
            "endpoint": args.endpoint,
            "database": database,
            "status": server_status,
        },
        "dataset_revision": RCA100_DATASET_REVISION,
        "adapter_source_revision": RCA100_SOURCE_REVISION,
        "case": {
            "adapter": "rca100",
            "source_case": case.source_case,
            "dataset": case.dataset,
            "system": case.system,
            "time_start": case.input.time_start,
            "time_end": case.input.time_end,
            "alert_time": case.input.alert_time,
            "alert_text": case.input.alert_text,
            "fault_taxonomy": case.input.fault_taxonomy,
            "alert_source": "dataset-native",
        },
        "graph_isolation": graph_isolation,
        "ingest": counts.model_dump(mode="json"),
        "semantic_surfaces": surfaces,
        "validation": validation,
        "reference_topology": reference_topology_summary(case.topology_path),
        "ground_truth": case.ground_truth.model_dump(mode="json"),
    }
    write_json(report_path, report)
    print(report_path)
    return 0


def smoke_openrca(args: argparse.Namespace) -> int:
    checkout = inspect_checkout(
        args.greptimedb_repo,
        expected_branch="feat/semantic-graph-declaration-visibility",
    )
    case = OpenRCARepository(args.cache_dir).fetch_case(args.case)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    case_slug = re.sub(r"[^a-z0-9]+", "-", args.case.lower()).strip("-")
    database = args.database or f"semantic_openrca_{run_id.replace('-', '_')}"
    case = case.model_copy(update={"input": case.input.model_copy(update={"database": database})})
    report_path = args.reports_dir / f"smoke-openrca-{case_slug}-{run_id}.json"
    audit = source_audit(case)
    with GreptimeClient(args.endpoint, database=database, timeout=120) as client:
        server_status = client.status()
        client.create_database(database)
        graph_isolation = assert_semantic_graph_window_empty(client, case.input)
        counts = ingest_openrca_case(client, case)
        surfaces = inspect_semantic_surfaces(client, case.input)
        validation = validate_openrca_ingest(client, case, counts)
    report = {
        "greptimedb": checkout,
        "server": {
            "endpoint": args.endpoint,
            "database": database,
            "status": server_status,
        },
        "dataset_revision": OPENRCA_DATASET_REVISION,
        "adapter_source_revision": OPENRCA_SOURCE_REVISION,
        "mirror_revision": OPENRCA_MIRROR_REVISION,
        "case": {
            "adapter": f"openrca-{case.variant}",
            "source_case": case.source_case,
            "dataset": case.dataset,
            "system": case.system,
            "time_start": case.input.time_start,
            "time_end": case.input.time_end,
            "alert_time": case.input.alert_time,
            "alert_text": case.input.alert_text,
            "fault_taxonomy": case.input.fault_taxonomy,
            "alert_source": "benchmark-task-window",
        },
        "source_audit": audit,
        "graph_isolation": graph_isolation,
        "ingest": counts.model_dump(mode="json"),
        "semantic_surfaces": surfaces,
        "validation": validation,
        "ground_truth": case.ground_truth.model_dump(mode="json"),
    }
    write_json(report_path, report)
    print(report_path)
    return 0


def smoke_openrca2(args: argparse.Namespace) -> int:
    checkout = inspect_checkout(
        args.greptimedb_repo,
        expected_branch="feat/semantic-graph-declaration-visibility",
    )
    case = OpenRCA2Repository(args.cache_dir).fetch_case(args.case)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    case_slug = re.sub(r"[^a-z0-9]+", "-", args.case.lower()).strip("-")
    database = args.database or f"semantic_openrca2_{run_id.replace('-', '_')}"
    case = case.model_copy(update={"input": case.input.model_copy(update={"database": database})})
    report_path = args.reports_dir / f"smoke-openrca2-{case_slug}-{run_id}.json"
    audit = source_audit_openrca2(case)
    with GreptimeClient(args.endpoint, database=database, timeout=120) as client:
        server_status = client.status()
        client.create_database(database)
        graph_isolation = assert_semantic_graph_window_empty(client, case.input)
        counts = ingest_openrca2_case(client, case)
        surfaces = inspect_semantic_surfaces(client, case.input)
        validation = validate_openrca2_ingest(client, case, counts)
    report = {
        "greptimedb": checkout,
        "server": {
            "endpoint": args.endpoint,
            "database": database,
            "status": server_status,
        },
        "dataset_revision": OPENRCA2_DATASET_REVISION,
        "adapter_source_revision": OPENRCA2_SOURCE_REVISION,
        "case": {
            "adapter": "openrca2-ops-lite",
            "source_case": case.source_case,
            "dataset": case.dataset,
            "system": case.system,
            "time_start": case.input.time_start,
            "time_end": case.input.time_end,
            "alert_time": case.input.alert_time,
            "alert_text": case.input.alert_text,
            "fault_taxonomy": case.input.fault_taxonomy,
            "alert_source": "dataset-conclusion",
        },
        "source_audit": audit,
        "graph_isolation": graph_isolation,
        "ingest": counts.model_dump(mode="json"),
        "semantic_surfaces": surfaces,
        "validation": validation,
        "ground_truth": case.ground_truth.model_dump(mode="json"),
    }
    write_json(report_path, report)
    print(report_path)
    return 0


def discovery_audit(args: argparse.Namespace) -> int:
    _validate_microbenchmark_fixture_args(args.case_role, args.fixture)
    source, fixture, endpoint, database = _discovery_source(args.report, args.fixture)
    with GreptimeClient(endpoint, database=database, timeout=120) as client:
        server_status = client.status()
        audit = audit_discovery_fixture(client, fixture)
    if not audit.predicate_match:
        raise ValueError(f"discovery fixture predicate failed: {fixture.fixture_id}")
    if not audit.catalog_target_in_top_five:
        raise ValueError(
            "semantic catalog target is not in the first five results: "
            f"fixture={fixture.fixture_id}, rank={audit.catalog_target_rank}"
        )
    output = args.output or Path(".reports") / f"discovery-audit-{fixture.fixture_id}.json"
    write_json(
        output,
        {
            "discovery_report_schema_version": 1,
            "mode": "audit",
            "case_role": args.case_role,
            "source_report": str(args.report),
            "source_case": fixture.source_case,
            "fixture": fixture.model_dump(mode="json"),
            "protocol": discovery_protocol(),
            "server": {
                "endpoint": endpoint,
                "database": database,
                "status": server_status,
            },
            "audit": audit.model_dump(mode="json"),
            "source_ingest": source.get("ingest", {}),
        },
    )
    print(output)
    return 0


def discovery_run(args: argparse.Namespace) -> int:
    _validate_microbenchmark_fixture_args(args.case_role, args.fixture)
    if args.repetitions < 2 or args.repetitions % 2:
        raise ValueError("discovery repetitions must be a positive multiple of 2")
    source, fixture, endpoint, database = _discovery_source(args.report, args.fixture)
    runner = AgentRunner(args.runner)
    api_transport = ApiTransport(args.api_transport) if runner is AgentRunner.API else None
    levels = [Visibility.RAW, Visibility.SEMANTIC_GRAPH]
    orders = _run_orders(levels, args.repetitions, args.seed)
    output = args.output or _discovery_output(fixture.fixture_id, runner, args.model)
    expected = {
        "discovery_report_schema_version": 2,
        "source_report": str(args.report),
        "runner": runner.value,
        "model": args.model,
        "api_transport": api_transport.value if api_transport else None,
        "reasoning_effort": args.reasoning_effort if api_transport else None,
        "max_output_tokens": args.max_output_tokens if api_transport else None,
        "seed": args.seed,
        "max_tool_calls": DISCOVERY_MAX_TOOL_CALLS,
        "repetitions": args.repetitions,
        "protocol": discovery_protocol(),
        "token_accounting": TOKEN_ACCOUNTING[runner.value],
        "fixture": fixture.model_dump(mode="json"),
        "case_role": args.case_role,
    }
    if output.exists():
        result = json.loads(output.read_text())
        mismatches = {
            key: (result.get(key), value)
            for key, value in expected.items()
            if result.get(key) != value
        }
        if mismatches:
            raise ValueError(f"cannot resume incompatible discovery report: {mismatches}")
    else:
        result = {
            "mode": f"{args.case_role}-run",
            **expected,
            "source_case": fixture.source_case,
            "orders": [
                {
                    "repetition": repetition,
                    "levels": [level.value for level in order],
                }
                for repetition, order in enumerate(orders)
            ],
            "runs": [],
        }
    runs = result.get("runs")
    if not isinstance(runs, list):
        raise ValueError("discovery report runs must be a list")

    with GreptimeClient(endpoint, database=database, timeout=120) as client:
        result["server_status"] = client.status()
        audit = audit_discovery_fixture(client, fixture)
        if not audit.predicate_match or not audit.catalog_target_in_top_five:
            raise ValueError(
                "discovery no-model gate failed: "
                f"predicate={audit.predicate_match}, catalog_rank={audit.catalog_target_rank}"
            )
        previous_audit = result.get("audit")
        if isinstance(previous_audit, dict):
            previous_result = previous_audit.get("canonical_result")
            if isinstance(previous_result, dict) and (
                previous_result.get("columns") != audit.canonical_result.columns
                or previous_result.get("rows") != audit.canonical_result.rows
            ):
                raise ValueError("cannot resume after canonical discovery evidence changed")
        result["audit"] = audit.model_dump(mode="json")
        completed = {
            (int(item.get("repetition", 0)), str(item.get("run", {}).get("visibility")))
            for item in runs
            if isinstance(item, dict) and isinstance(item.get("run"), dict)
        }
        for item in runs:
            if not isinstance(item, dict) or not isinstance(item.get("run"), dict):
                continue
            recorded_run = DiscoveryAgentRun.model_validate(item["run"])
            item["evaluation"] = evaluate_discovery_run(
                recorded_run,
                fixture,
                audit.canonical_result,
                database=database,
            ).model_dump(mode="json")
        result["run_pair_descriptive"] = _discovery_run_pair_descriptive(runs)
        write_json(output, result)
        for repetition, order in enumerate(orders):
            for position, level in enumerate(order):
                if (repetition, level.value) in completed:
                    continue
                with client.measure_query_load() as database_load:
                    try:
                        agent_run = run_discovery_agent(
                            QueryGateway(client, level),
                            fixture,
                            level,
                            runner=runner,
                            model=args.model,
                            api_transport=api_transport,
                            reasoning_effort=args.reasoning_effort,
                            max_output_tokens=args.max_output_tokens,
                        )
                    except Exception as error:
                        agent_run = failed_discovery_run(
                            level,
                            runner,
                            args.model,
                            f"runner failed: {error}",
                            max_tool_calls=DISCOVERY_MAX_TOOL_CALLS,
                        )
                runs.append(
                    {
                        "repetition": repetition,
                        "position": position,
                        "run": agent_run.model_dump(mode="json"),
                        "evaluation": evaluate_discovery_run(
                            agent_run,
                            fixture,
                            audit.canonical_result,
                            database=database,
                        ).model_dump(mode="json"),
                        "database_load": database_load.model_dump(mode="json"),
                    }
                )
                result["run_pair_descriptive"] = _discovery_run_pair_descriptive(runs)
                write_json(output, result)
    print(output)
    return 0


def graph_audit(args: argparse.Namespace) -> int:
    _validate_microbenchmark_fixture_args(args.case_role, args.fixture)
    source, fixture, endpoint, database, _ = _graph_source(args.report, args.fixture)
    with GreptimeClient(endpoint, database=database, timeout=120) as client:
        server_status = client.status()
        assert_semantic_graph_isolated(client, database)
        audit = audit_graph_fixture(client, fixture)
    _require_graph_audit(audit)
    output = args.output or Path(".reports") / f"graph-audit-{fixture.fixture_id}.json"
    write_json(
        output,
        {
            "graph_report_schema_version": 1,
            "mode": "audit",
            "case_role": args.case_role,
            "source_report": str(args.report),
            "source_case": fixture.source_case,
            "fixture": fixture.model_dump(mode="json"),
            "protocol": graph_protocol(),
            "server": {
                "endpoint": endpoint,
                "database": database,
                "status": server_status,
            },
            "audit": audit.model_dump(mode="json"),
            "source_ingest": source.get("ingest", {}),
        },
    )
    print(output)
    return 0


def graph_run(args: argparse.Namespace) -> int:
    _validate_microbenchmark_fixture_args(args.case_role, args.fixture)
    if args.repetitions < 2 or args.repetitions % 2:
        raise ValueError("graph repetitions must be a positive multiple of 2")
    source, fixture, endpoint, database, semantic_coverage = _graph_source(
        args.report, args.fixture
    )
    runner = AgentRunner(args.runner)
    api_transport = ApiTransport(args.api_transport) if runner is AgentRunner.API else None
    levels = [Visibility.RAW, Visibility.SEMANTIC_GRAPH]
    orders = _run_orders(levels, args.repetitions, args.seed)
    output = args.output or _graph_output(fixture.fixture_id, runner, args.model)
    expected = {
        "graph_report_schema_version": 2,
        "source_report": str(args.report),
        "runner": runner.value,
        "model": args.model,
        "api_transport": api_transport.value if api_transport else None,
        "reasoning_effort": args.reasoning_effort if api_transport else None,
        "max_output_tokens": args.max_output_tokens if api_transport else None,
        "seed": args.seed,
        "max_tool_calls": GRAPH_MAX_TOOL_CALLS,
        "repetitions": args.repetitions,
        "protocol": graph_protocol(),
        "token_accounting": TOKEN_ACCOUNTING[runner.value],
        "fixture": fixture.model_dump(mode="json"),
        "case_role": args.case_role,
    }
    if output.exists():
        result = json.loads(output.read_text())
        mismatches = {
            key: (result.get(key), value)
            for key, value in expected.items()
            if result.get(key) != value
        }
        if mismatches:
            raise ValueError(f"cannot resume incompatible graph report: {mismatches}")
    else:
        result = {
            "mode": f"{args.case_role}-run",
            **expected,
            "source_case": fixture.source_case,
            "semantic_coverage": semantic_coverage,
            "orders": [
                {
                    "repetition": repetition,
                    "levels": [level.value for level in order],
                }
                for repetition, order in enumerate(orders)
            ],
            "runs": [],
        }
    runs = result.get("runs")
    if not isinstance(runs, list):
        raise ValueError("graph report runs must be a list")

    with GreptimeClient(endpoint, database=database, timeout=120) as client:
        result["server_status"] = client.status()
        assert_semantic_graph_isolated(client, database)
        audit = audit_graph_fixture(client, fixture)
        _require_graph_audit(audit)
        previous_audit = result.get("audit")
        if isinstance(previous_audit, dict):
            previous_result = previous_audit.get("graph_result")
            if isinstance(previous_result, dict):
                try:
                    same_edges = edge_results_match(
                        QueryResult.model_validate(previous_result),
                        audit.graph_result,
                    )
                except ValueError:
                    same_edges = False
                if not same_edges:
                    raise ValueError("cannot resume after canonical graph evidence changed")
        result["audit"] = audit.model_dump(mode="json")
        completed = {
            (int(item.get("repetition", 0)), str(item.get("run", {}).get("visibility")))
            for item in runs
            if isinstance(item, dict) and isinstance(item.get("run"), dict)
        }
        for item in runs:
            if not isinstance(item, dict) or not isinstance(item.get("run"), dict):
                continue
            recorded_run = GraphAgentRun.model_validate(item["run"])
            item["evaluation"] = evaluate_graph_run(
                recorded_run,
                fixture,
                audit.graph_result,
                database=database,
            ).model_dump(mode="json")
        result["run_pair_descriptive"] = _graph_run_pair_descriptive(runs)
        write_json(output, result)
        for repetition, order in enumerate(orders):
            for position, level in enumerate(order):
                if (repetition, level.value) in completed:
                    continue
                with client.measure_query_load() as database_load:
                    try:
                        agent_run = run_graph_agent(
                            QueryGateway(client, level),
                            fixture,
                            level,
                            semantic_coverage,
                            runner=runner,
                            model=args.model,
                            api_transport=api_transport,
                            reasoning_effort=args.reasoning_effort,
                            max_output_tokens=args.max_output_tokens,
                        )
                    except Exception as error:
                        agent_run = failed_graph_run(
                            level,
                            runner,
                            args.model,
                            f"runner failed: {error}",
                            max_tool_calls=GRAPH_MAX_TOOL_CALLS,
                        )
                runs.append(
                    {
                        "repetition": repetition,
                        "position": position,
                        "run": agent_run.model_dump(mode="json"),
                        "evaluation": evaluate_graph_run(
                            agent_run,
                            fixture,
                            audit.graph_result,
                            database=database,
                        ).model_dump(mode="json"),
                        "database_load": database_load.model_dump(mode="json"),
                    }
                )
                result["run_pair_descriptive"] = _graph_run_pair_descriptive(runs)
                write_json(output, result)
    print(output)
    return 0


def _discovery_run_pair_descriptive(runs: list[object]) -> dict[str, object]:
    by_key: dict[tuple[int, str], dict[str, object]] = {}
    for item in runs:
        if not isinstance(item, dict):
            continue
        run = item.get("run")
        if isinstance(run, dict):
            by_key[(int(item.get("repetition", 0)), str(run.get("visibility")))] = item

    paired = []
    for repetition in sorted({key[0] for key in by_key}):
        raw = by_key.get((repetition, Visibility.RAW.value))
        graph = by_key.get((repetition, Visibility.SEMANTIC_GRAPH.value))
        if raw is not None and graph is not None:
            paired.append((raw, graph))

    improvements = regressions = ties = 0
    for raw, graph in paired:
        raw_success = _discovery_success(raw)
        graph_success = _discovery_success(graph)
        if graph_success and not raw_success:
            improvements += 1
        elif raw_success and not graph_success:
            regressions += 1
        else:
            ties += 1

    efficiency = []
    for metric in (
        "tool_calls_through_evidence",
        "rows_returned_through_evidence",
        "discovery_calls_through_evidence",
    ):
        deltas: list[float] = []
        better = worse = metric_ties = 0
        for raw, graph in paired:
            if not _discovery_success(raw) or not _discovery_success(graph):
                continue
            raw_value = _discovery_metric(raw, metric)
            graph_value = _discovery_metric(graph, metric)
            if raw_value is None or graph_value is None:
                continue
            delta = graph_value - raw_value
            deltas.append(delta)
            if delta < 0:
                better += 1
            elif delta > 0:
                worse += 1
            else:
                metric_ties += 1
        efficiency.append(
            {
                "metric": metric,
                "paired_observations": len(deltas),
                "better": better,
                "worse": worse,
                "ties": metric_ties,
                "median_delta": median(deltas) if deltas else None,
            }
        )

    return {
        "statistical_unit": "run pair within one case",
        "inference_role": "descriptive only; repetitions are not independent cases",
        "task_success": {
            "paired_observations": len(paired),
            "improvements": improvements,
            "regressions": regressions,
            "ties": ties,
        },
        "successful_pair_efficiency": efficiency,
    }


def _discovery_success(item: dict[str, object]) -> bool:
    evaluation = item.get("evaluation")
    return isinstance(evaluation, dict) and evaluation.get("success") is True


def _discovery_metric(item: dict[str, object], metric: str) -> float | None:
    evaluation = item.get("evaluation")
    if not isinstance(evaluation, dict):
        return None
    value = evaluation.get(metric)
    return float(value) if isinstance(value, (int, float)) else None


def _graph_run_pair_descriptive(runs: list[object]) -> dict[str, object]:
    by_key: dict[tuple[int, str], dict[str, object]] = {}
    for item in runs:
        if not isinstance(item, dict):
            continue
        run = item.get("run")
        if isinstance(run, dict):
            by_key[(int(item.get("repetition", 0)), str(run.get("visibility")))] = item

    paired = []
    for repetition in sorted({key[0] for key in by_key}):
        raw = by_key.get((repetition, Visibility.RAW.value))
        graph = by_key.get((repetition, Visibility.SEMANTIC_GRAPH.value))
        if raw is not None and graph is not None:
            paired.append((raw, graph))

    improvements = regressions = ties = 0
    for raw, graph in paired:
        raw_success = _graph_success(raw)
        graph_success = _graph_success(graph)
        if graph_success and not raw_success:
            improvements += 1
        elif raw_success and not graph_success:
            regressions += 1
        else:
            ties += 1

    efficiency = []
    for metric in ("tool_calls_through_evidence", "rows_returned_through_evidence"):
        deltas: list[float] = []
        better = worse = metric_ties = 0
        for raw, graph in paired:
            if not _graph_success(raw) or not _graph_success(graph):
                continue
            raw_value = _graph_metric(raw, metric)
            graph_value = _graph_metric(graph, metric)
            if raw_value is None or graph_value is None:
                continue
            delta = graph_value - raw_value
            deltas.append(delta)
            if delta < 0:
                better += 1
            elif delta > 0:
                worse += 1
            else:
                metric_ties += 1
        efficiency.append(
            {
                "metric": metric,
                "paired_observations": len(deltas),
                "better": better,
                "worse": worse,
                "ties": metric_ties,
                "median_delta": median(deltas) if deltas else None,
            }
        )

    return {
        "statistical_unit": "run pair within one case",
        "inference_role": "descriptive only; repetitions are not independent cases",
        "task_success": {
            "paired_observations": len(paired),
            "improvements": improvements,
            "regressions": regressions,
            "ties": ties,
        },
        "successful_pair_efficiency": efficiency,
    }


def _graph_success(item: dict[str, object]) -> bool:
    evaluation = item.get("evaluation")
    return isinstance(evaluation, dict) and evaluation.get("success") is True


def _graph_metric(item: dict[str, object], metric: str) -> float | None:
    evaluation = item.get("evaluation")
    if not isinstance(evaluation, dict):
        return None
    value = evaluation.get(metric)
    return float(value) if isinstance(value, (int, float)) else None


def _discovery_source(
    report: Path,
    fixture_path: Path | None = None,
) -> tuple[dict[str, object], DiscoveryFixture, str, str]:
    source = json.loads(report.read_text())
    case = source.get("case")
    server = source.get("server")
    if not isinstance(case, dict) or not isinstance(server, dict):
        raise ValueError("discovery source must be a no-model smoke report")
    source_case = case.get("source_case")
    endpoint = server.get("endpoint")
    database = server.get("database")
    if not isinstance(source_case, str):
        raise ValueError("discovery source is missing case.source_case")
    if not isinstance(endpoint, str) or not isinstance(database, str):
        raise ValueError("discovery source is missing server endpoint or database")
    fixture = fixture_for_source_case(
        source_case,
        str(fixture_path) if fixture_path is not None else None,
    )
    return source, fixture, endpoint, database


def _validate_microbenchmark_fixture_args(
    case_role: str,
    fixture_path: Path | None,
) -> None:
    if case_role == "measurement" and fixture_path is None:
        raise ValueError("measurement micro-benchmarks require an external frozen fixture")
    if case_role == "development" and fixture_path is not None:
        raise ValueError("external fixtures are reserved for measurement micro-benchmarks")


def _discovery_output(fixture_id: str, runner: AgentRunner, model: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{runner.value}-{model}".lower()).strip("-")
    version = discovery_protocol()["version"]
    return Path(".reports") / f"discovery-v{version}-{slug}-{fixture_id}.json"


def _graph_source(
    report: Path,
    fixture_path: Path | None = None,
) -> tuple[dict[str, object], GraphFixture, str, str, dict[str, object]]:
    source = json.loads(report.read_text())
    case = source.get("case")
    server = source.get("server")
    surfaces = source.get("semantic_surfaces")
    if not isinstance(case, dict) or not isinstance(server, dict):
        raise ValueError("graph source must be a no-model smoke report")
    source_case = case.get("source_case")
    endpoint = server.get("endpoint")
    database = server.get("database")
    if not isinstance(source_case, str):
        raise ValueError("graph source is missing case.source_case")
    if not isinstance(endpoint, str) or not isinstance(database, str):
        raise ValueError("graph source is missing server endpoint or database")
    if not isinstance(surfaces, dict):
        raise ValueError("graph source is missing semantic_surfaces")
    coverage = surfaces.get("coverage")
    if not isinstance(coverage, dict):
        coverage = summarize_semantic_surfaces(surfaces)
    graph = coverage.get("graph")
    if not isinstance(graph, dict) or graph.get("status") != "relational":
        raise ValueError("graph source does not have relational semantic coverage")
    fixture = graph_fixture_for_source_case(
        source_case,
        str(fixture_path) if fixture_path is not None else None,
    )
    validate_source_window(fixture, int(case["time_start"]), int(case["time_end"]))
    return source, fixture, endpoint, database, coverage


def _require_graph_audit(audit: GraphAudit) -> None:
    if not (audit.edge_sets_match and audit.unique_winner and audit.expected_winner_match):
        raise ValueError(
            "graph no-model gate failed: "
            f"edge_sets_match={audit.edge_sets_match}, "
            f"unique_winner={audit.unique_winner}, "
            f"expected_winner_match={audit.expected_winner_match}"
        )


def _graph_output(fixture_id: str, runner: AgentRunner, model: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{runner.value}-{model}".lower()).strip("-")
    version = graph_protocol()["version"]
    return Path(".reports") / f"graph-v{version}-{slug}-{fixture_id}.json"


def run(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    source = json.loads(args.report.read_text())
    endpoint = str(source["server"]["endpoint"])
    database = str(source["server"]["database"])
    case = source["case"]
    case_input = CaseInput(
        case_token=uuid.uuid4().hex,
        database=database,
        time_start=int(case["time_start"]),
        time_end=int(case["time_end"]),
        alert_time=int(case.get("alert_time", case["time_end"])),
        alert_text=str(case["alert_text"]) if case.get("alert_text") else None,
        fault_taxonomy=[str(value) for value in case.get("fault_taxonomy", [])],
    )
    truth = _ground_truth_from_report(source)
    _assert_neutral_database_name(database, truth)
    levels = [Visibility(value) for value in args.levels]
    runner = AgentRunner(args.runner)
    api_transport = ApiTransport(args.api_transport) if runner is AgentRunner.API else None
    orders = _run_orders(levels, args.repetitions, args.seed)
    surfaces = source.get("semantic_surfaces", {})
    if not isinstance(surfaces, dict):
        surfaces = {}
    semantic_coverage = surfaces.get("coverage")
    if not isinstance(semantic_coverage, dict):
        semantic_coverage = summarize_semantic_surfaces(surfaces)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    output = args.output or Path(".reports") / f"eval-{timestamp}.json"
    runner_jobs = int(os.environ.get("SEMANTIC_RCA_RUNNER_JOBS", "1"))
    if runner_jobs < 1:
        raise ValueError("SEMANTIC_RCA_RUNNER_JOBS must be at least 1")
    if output.exists():
        result = json.loads(output.read_text())
        expected = {
            "report_schema_version": CASE_REPORT_SCHEMA_VERSION,
            "source_report": str(args.report),
            "runner": args.runner,
            "model": args.model,
            "api_transport": api_transport.value if api_transport else None,
            "reasoning_effort": args.reasoning_effort if api_transport else None,
            "max_output_tokens": args.max_output_tokens if api_transport else None,
            "seed": args.seed,
            "max_tool_calls": args.max_tool_calls,
            "repetitions": args.repetitions,
            "case_role": args.case_role,
            "protocol": benchmark_protocol(),
            "token_accounting": TOKEN_ACCOUNTING[args.runner],
        }
        expected["runner_jobs"] = runner_jobs
        mismatches = {
            key: (result.get(key), value)
            for key, value in expected.items()
            if result.get(key) != value
        }
        if mismatches:
            raise ValueError(f"cannot resume incompatible report: {mismatches}")
    else:
        result = {
            "report_schema_version": CASE_REPORT_SCHEMA_VERSION,
            "source_report": str(args.report),
            "case": source["case"],
            "ingest": source.get("ingest", {}),
            "semantic_coverage": semantic_coverage,
            "protocol": benchmark_protocol(),
            "token_accounting": TOKEN_ACCOUNTING[args.runner],
            "runner": args.runner,
            "model": args.model,
            "api_transport": api_transport.value if api_transport else None,
            "reasoning_effort": args.reasoning_effort if api_transport else None,
            "max_output_tokens": args.max_output_tokens if api_transport else None,
            "seed": args.seed,
            "max_tool_calls": args.max_tool_calls,
            "repetitions": args.repetitions,
            "case_role": args.case_role,
            "runner_jobs": runner_jobs,
            "orders": [
                {
                    "repetition": repetition,
                    "levels": [level.value for level in order],
                }
                for repetition, order in enumerate(orders)
            ],
            "ground_truth": truth.model_dump(mode="json"),
            "case_context": case_context(source["case"], source["ground_truth"]),
            "runs": [],
        }
    runs = result["runs"]
    if not isinstance(runs, list):
        raise ValueError("report runs must be a list")
    result["ground_truth"] = truth.model_dump(mode="json")
    for item in runs:
        recorded_run = AgentRun.model_validate(item["run"])
        item["evaluation"] = evaluate(recorded_run, truth).model_dump(mode="json")
    write_json(output, result)
    completed = {(int(item.get("repetition", 0)), item["run"]["visibility"]) for item in runs}
    with GreptimeClient(endpoint, database=database, timeout=120) as client:
        result["server_status"] = client.status()
        if Visibility.SEMANTIC_GRAPH in levels:
            assert_semantic_graph_isolated(client, database)
        for repetition, order in enumerate(orders):
            for position, level in enumerate(order):
                if (repetition, level.value) in completed:
                    continue
                gateway = QueryGateway(client, level)
                with client.measure_query_load() as database_load:
                    if runner is AgentRunner.API:
                        agent_run = run_agent(
                            gateway,
                            case_input,
                            level,
                            model=args.model,
                            api_transport=api_transport,
                            reasoning_effort=args.reasoning_effort,
                            max_output_tokens=args.max_output_tokens,
                            max_tool_calls=args.max_tool_calls,
                            max_turns=args.max_tool_calls + 10,
                            semantic_coverage=semantic_coverage,
                        )
                    else:
                        agent_run = run_subscription_agent(
                            gateway,
                            case_input,
                            level,
                            runner=runner,
                            model=args.model,
                            max_tool_calls=args.max_tool_calls,
                            semantic_coverage=semantic_coverage,
                        )
                runs.append(
                    {
                        "repetition": repetition,
                        "position": position,
                        "run": agent_run.model_dump(mode="json"),
                        "evaluation": evaluate(agent_run, truth).model_dump(mode="json"),
                        "database_load": database_load.model_dump(mode="json"),
                    }
                )
                write_json(output, result)
    print(output)
    return 0


def _assert_neutral_database_name(database: str, truth: GroundTruth) -> None:
    normalized_database = re.sub(r"[^a-z0-9]+", "", database.lower())
    for label, value in (
        ("component", truth.causal_component),
        ("fault type", truth.fault_type),
    ):
        if value is None:
            continue
        normalized_value = re.sub(r"[^a-z0-9]+", "", value.lower())
        if normalized_value and normalized_value in normalized_database:
            raise ValueError(f"database name leaks ground-truth {label}: use a neutral identifier")


def _ground_truth_from_report(source: dict[str, object]) -> GroundTruth:
    case = source.get("case")
    ground_truth = source.get("ground_truth")
    if not isinstance(ground_truth, dict):
        raise ValueError("source report is missing ground_truth")
    if (
        isinstance(case, dict)
        and case.get("adapter") == "rca100"
        and "component_scoreable" not in ground_truth
    ):
        raise ValueError(
            "RCA100 source report predates the component-contract audit; "
            "regenerate its no-model smoke report"
        )
    return GroundTruth.model_validate(ground_truth)


def _batch_output(
    source_report: Path,
    reports_dir: Path,
    *,
    runner: str = AgentRunner.API.value,
    model: str = "claude-sonnet-5",
) -> Path:
    source = json.loads(source_report.read_text())
    case = source.get("case", {})
    truth = source.get("ground_truth", {})
    identity = case.get("source_case")
    if not identity:
        identity = "-".join(
            str(value)
            for value in (
                case.get("dataset"),
                truth.get("causal_component"),
                truth.get("fault_type"),
            )
            if value
        )
    slug = re.sub(r"[^a-z0-9]+", "-", str(identity).lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive case name from {source_report}")
    version = benchmark_protocol()["version"]
    execution_slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        f"{runner}-{model}".lower(),
    ).strip("-")
    return reports_dir / f"v{version}-{execution_slug}-{slug}.json"


def batch(args: argparse.Namespace) -> int:
    if args.jobs < 1:
        raise ValueError("jobs must be at least 1")
    reports = list(dict.fromkeys(args.report))
    jobs = min(args.jobs, len(reports))

    def execute(source_report: Path) -> Path:
        output = _batch_output(
            source_report,
            args.reports_dir,
            runner=args.runner,
            model=args.model,
        )
        command = [
            sys.executable,
            "-m",
            "semantic_rca_bench.cli",
            "run",
            "--report",
            str(source_report),
            "--runner",
            args.runner,
            "--model",
            args.model,
            "--api-transport",
            args.api_transport,
            "--max-output-tokens",
            str(args.max_output_tokens),
            "--levels",
            *args.levels,
            "--max-tool-calls",
            str(args.max_tool_calls),
            "--repetitions",
            str(args.repetitions),
            "--seed",
            str(args.seed),
            "--case-role",
            args.case_role,
            "--output",
            str(output),
        ]
        if args.reasoning_effort is not None:
            command.extend(["--reasoning-effort", args.reasoning_effort])
        environment = os.environ.copy()
        environment["SEMANTIC_RCA_RUNNER_JOBS"] = str(jobs)
        subprocess.run(command, check=True, env=environment)
        return output

    failures: list[tuple[Path, Exception]] = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(execute, report): report for report in reports}
        for future in as_completed(futures):
            report = futures[future]
            try:
                print(future.result())
            except Exception as error:
                failures.append((report, error))
                print(f"batch case failed: {report}: {error}", file=sys.stderr)
    if failures:
        raise RuntimeError(f"{len(failures)} batch case(s) failed")
    return 0


def render(args: argparse.Namespace) -> int:
    if args.output:
        output = args.output
    elif len(args.report) == 1:
        output = args.report[0].with_suffix(".html")
    else:
        output = Path(".reports") / f"pilot-{time.strftime('%Y%m%d-%H%M%S')}.html"
    render_reports(args.report, output)
    print(output)
    return 0


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "doctor":
            code = doctor(args.greptimedb_repo)
        elif args.command == "aegis-audit":
            code = aegis_audit(args)
        elif args.command == "aegis-fetch":
            code = aegis_fetch(args)
        elif args.command == "formal-suite-micro-preflight":
            code = formal_suite_micro_preflight(args)
        elif args.command == "formal-suite-micro-run":
            code = formal_suite_micro_run(args)
        elif args.command == "formal-suite-micro-export":
            code = formal_suite_micro_export(args)
        elif args.command == "formal-suite-report":
            code = formal_suite_report(args)
        elif args.command == "transfer-selection-audit":
            code = transfer_selection_audit(args)
        elif args.command == "transfer-preflight":
            code = transfer_preflight(args)
        elif args.command == "transfer-run":
            code = transfer_run(args)
        elif args.command == "transfer-export":
            code = transfer_export(args)
        elif args.command == "transfer-adjudication-queue":
            code = transfer_adjudication_queue(args)
        elif args.command == "smoke":
            code = smoke(args)
        elif args.command == "smoke-rca100":
            code = smoke_rca100(args)
        elif args.command == "smoke-openrca":
            code = smoke_openrca(args)
        elif args.command == "smoke-openrca2":
            code = smoke_openrca2(args)
        elif args.command == "run":
            code = run(args)
        elif args.command == "batch":
            code = batch(args)
        elif args.command == "discovery-audit":
            code = discovery_audit(args)
        elif args.command == "discovery-run":
            code = discovery_run(args)
        elif args.command == "graph-audit":
            code = graph_audit(args)
        elif args.command == "graph-run":
            code = graph_run(args)
        elif args.command == "render":
            code = render(args)
        else:
            raise AssertionError(args.command)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    raise SystemExit(code)


if __name__ == "__main__":
    main()
