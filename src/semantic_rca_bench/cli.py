from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from semantic_rca_bench.agent import run_agent
from semantic_rca_bench.contracts import AgentRun, CaseInput, GroundTruth, Visibility
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
from semantic_rca_bench.evaluation import evaluate
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.greptimedb.server import inspect_checkout, write_json
from semantic_rca_bench.greptimedb.visibility import QueryGateway
from semantic_rca_bench.inspect import (
    assert_semantic_graph_isolated,
    inspect_semantic_surfaces,
    summarize_semantic_surfaces,
)
from semantic_rca_bench.protocol import benchmark_protocol
from semantic_rca_bench.report import case_context, render_reports

DEFAULT_GREPTIMEDB_REPO = Path("/Users/dennis/programming/rust/greptimedb")


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument(
        "--levels",
        nargs="+",
        choices=[level.value for level in Visibility],
        default=[level.value for level in Visibility],
    )
    parser.add_argument("--max-tool-calls", type=int, default=24)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--case-role",
        choices=["development", "measurement"],
        default="development",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semantic-rca")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO)

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
    openrca_smoke.add_argument(
        "--greptimedb-repo", type=Path, default=DEFAULT_GREPTIMEDB_REPO
    )
    openrca_smoke.add_argument("--cache-dir", type=Path, default=Path(".data/openrca"))
    openrca_smoke.add_argument("--reports-dir", type=Path, default=Path(".reports"))
    openrca_smoke.add_argument("--endpoint", default="http://127.0.0.1:4000")
    openrca_smoke.add_argument("--database")
    openrca_smoke.add_argument("--case", default=DEFAULT_BANK_CASE)

    run = subparsers.add_parser("run")
    run.add_argument("--report", type=Path, required=True)
    _add_run_arguments(run)
    run.add_argument("--output", type=Path)

    batch = subparsers.add_parser("batch")
    batch.add_argument("--report", type=Path, nargs="+", required=True)
    _add_run_arguments(batch)
    batch.add_argument("--jobs", type=int, default=1)
    batch.add_argument("--reports-dir", type=Path, default=Path(".reports"))

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
        assert_semantic_graph_isolated(client, database)
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
        assert_semantic_graph_isolated(client, database)
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
    case = OpenRCARepository(args.cache_dir).fetch_bank_case(args.case)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    case_slug = re.sub(r"[^a-z0-9]+", "-", args.case.lower()).strip("-")
    database = args.database or f"semantic_openrca_{run_id.replace('-', '_')}"
    case = case.model_copy(update={"input": case.input.model_copy(update={"database": database})})
    report_path = args.reports_dir / f"smoke-openrca-{case_slug}-{run_id}.json"
    audit = source_audit(case)
    with GreptimeClient(args.endpoint, database=database, timeout=120) as client:
        server_status = client.status()
        client.create_database(database)
        assert_semantic_graph_isolated(client, database)
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
            "adapter": "openrca-bank",
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
        "ingest": counts.model_dump(mode="json"),
        "semantic_surfaces": surfaces,
        "validation": validation,
        "ground_truth": case.ground_truth.model_dump(mode="json"),
    }
    write_json(report_path, report)
    print(report_path)
    return 0


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
    truth = GroundTruth.model_validate(source["ground_truth"])
    levels = [Visibility(value) for value in args.levels]
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
            "source_report": str(args.report),
            "model": args.model,
            "seed": args.seed,
            "max_tool_calls": args.max_tool_calls,
            "repetitions": args.repetitions,
            "case_role": args.case_role,
            "protocol": benchmark_protocol(),
        }
        if "runner_jobs" in result:
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
            "report_schema_version": 2,
            "source_report": str(args.report),
            "case": source["case"],
            "ingest": source.get("ingest", {}),
            "semantic_coverage": semantic_coverage,
            "protocol": benchmark_protocol(),
            "model": args.model,
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
    assert isinstance(runs, list)
    result["ground_truth"] = truth.model_dump(mode="json")
    for item in runs:
        item["evaluation"] = evaluate(
            AgentRun.model_validate(item["run"]),
            truth,
        ).model_dump(mode="json")
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
                    agent_run = run_agent(
                        gateway,
                        case_input,
                        level,
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


def _batch_output(source_report: Path, reports_dir: Path) -> Path:
    source = json.loads(source_report.read_text())
    case = source.get("case", {})
    truth = source.get("ground_truth", {})
    identity = case.get("source_case")
    if not identity:
        identity = "-".join(
            str(value)
            for value in (case.get("dataset"), truth.get("component"), truth.get("fault_type"))
            if value
        )
    slug = re.sub(r"[^a-z0-9]+", "-", str(identity).lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive case name from {source_report}")
    version = benchmark_protocol()["version"]
    return reports_dir / f"v{version}-{slug}.json"


def batch(args: argparse.Namespace) -> int:
    if args.jobs < 1:
        raise ValueError("jobs must be at least 1")
    reports = list(dict.fromkeys(args.report))
    jobs = min(args.jobs, len(reports))

    def execute(source_report: Path) -> Path:
        output = _batch_output(source_report, args.reports_dir)
        command = [
            sys.executable,
            "-m",
            "semantic_rca_bench.cli",
            "run",
            "--report",
            str(source_report),
            "--model",
            args.model,
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


def _run_orders(levels: list[Visibility], repetitions: int, seed: int) -> list[list[Visibility]]:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if not levels:
        raise ValueError("at least one visibility level is required")
    shuffled = levels.copy()
    random.Random(seed).shuffle(shuffled)
    return [
        shuffled[offset:] + shuffled[:offset]
        for repetition in range(repetitions)
        for offset in [repetition % len(shuffled)]
    ]


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
        elif args.command == "smoke":
            code = smoke(args)
        elif args.command == "smoke-rca100":
            code = smoke_rca100(args)
        elif args.command == "smoke-openrca":
            code = smoke_openrca(args)
        elif args.command == "run":
            code = run(args)
        elif args.command == "batch":
            code = batch(args)
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
