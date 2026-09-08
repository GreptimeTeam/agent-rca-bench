"""Split-only replication with frozen inputs and independent, durable cell journals."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from agent_rca_bench.gemini_rate_limit import INPUT_TOKENS_PER_MINUTE
from agent_rca_bench.greptimedb.server import write_json
from agent_rca_bench.report import MODEL_PRICING
from agent_rca_bench.transfer_concurrent import _case_execution_order, _exclusive_invocation
from agent_rca_bench.transfer_formal import (
    TransferEnvironmentConfig,
    audit_transfer_trace_visibility,
    execute_transfer_cell,
    prepare_transfer_environment,
    source_semantic_sha256,
    validate_transfer_run_item,
)
from agent_rca_bench.transfer_protocol import formal_schedule, load_transfer_protocol, sha256_file
from agent_rca_bench.transfer_release import (
    _public_source,
    canonical_sha256,
    validate_measurement_artifact,
)

LIMITS = {"global": 8, "cohort": 4, "provider": 2, "environment": 1, "preparation": 2}


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def seal(value: dict) -> dict:
    payload = {key: item for key, item in value.items() if key != "integrity"}
    return {**payload, "integrity": canonical_sha256(payload)}


def check_seal(value: dict) -> None:
    if seal(value) != value:
        raise ValueError("rerun artifact integrity mismatch")


def freeze(config: dict, root: Path) -> dict:
    cohorts = {}
    for name, paths in config["cohorts"].items():
        protocol_path = root / paths["protocol"]
        protocol, selection = load_transfer_protocol(protocol_path)
        baseline_path = root / paths["baseline_snapshot"]
        baseline = read(baseline_path)
        validate_measurement_artifact(baseline, protocol_path)
        for model in protocol.models:
            previous = baseline["pricing_snapshot"][model.model]
            current = MODEL_PRICING[model.model]
            rate_keys = {
                key
                for key in previous | current
                if key.endswith("_per_million") or key == "currency"
            }
            if any(previous.get(key) != current.get(key) for key in rate_keys):
                raise ValueError("pricing rates changed; review before freezing this rerun")
        if protocol.max_parallel_runs_per_provider != LIMITS["provider"]:
            raise ValueError("rerun provider cap differs from the frozen protocol")
        cohorts[name] = {
            "protocol": paths["protocol"],
            "protocol_sha256": sha256_file(protocol_path),
            "baseline": paths["baseline"],
            "baseline_sha256": sha256_file(baseline_path),
            "baseline_semantic_sha256": canonical_sha256(baseline),
            "superseded_cells": {
                str(item["cell_index"]): canonical_sha256(item)
                for item in baseline["runs"]
                if item["visibility"] == "split_pillars"
            },
            "schedule": [
                cell
                for cell in formal_schedule(protocol, selection)
                if cell["visibility"] == "split_pillars"
            ],
            "pricing_snapshot": baseline["pricing_snapshot"],
        }
    cells = [cell for group in cohorts.values() for cell in group["schedule"]]
    if len(cells) != 168 or sorted(Counter(cell["model"] for cell in cells).values()) != [28] * 6:
        raise ValueError("expected exactly 168 Split cells across six models")
    files = sorted((root / "src/agent_rca_bench").rglob("*.py"))
    files += sorted((root / "fixtures/reference").glob("*.json"))
    files += [root / "uv.lock", root / "pyproject.toml"]
    return seal(
        {
            "schema_version": 1,
            "kind": "split-interface-rerun",
            "created_at": datetime.now(UTC).isoformat(),
            "limits": LIMITS,
            "gemini_input_pacing": {
                "tokens_per_minute": INPUT_TOKENS_PER_MINUTE,
                "window_seconds": 60,
                "scope": "shared by both Gemini workers in this process; external clients excluded",
                "estimate": "UTF-8 input bytes / 2 + 1024; ratio raised by actual prompt usage",
                "accounting": "full prompt including cache; reservation corrected at response time",
                "waiting": (
                    "before every HTTP attempt, including SDK retries; included in elapsed time"
                ),
            },
            "execution_amendment": (
                "Split-only replay; four ready case environments per cohort, eight globally; "
                "two preparations globally and no preparation during investigations; dispatch "
                "available providers without blocking worker slots, preserving repetition order "
                "within each model and case. Original statistical families remain unchanged. "
                "A runner error is persisted as a scoreable failed cell and pauses new dispatch "
                "for review; in-flight cells finish and are saved. Explicitly authorized resume "
                "skips completed failures; no automatic cell retries. This deliberately pauses "
                "the schedule instead of continuing after a runner failure."
                " Gemini HTTP attempts share a local input-token window; waiting counts toward "
                "elapsed time but does not consume tool or turn budgets."
            ),
            "changes": [
                "Tempo retention: 10 years",
                "columnar label names",
                "max_items guidance matches SQL max_rows, with items as the returned unit",
            ],
            "retained": (
                "Original Raw/Graph responses, including their historical PromQL encoding, "
                "and all micro cells."
            ),
            "cohorts": cohorts,
            "input_sha256": {str(path.relative_to(root)): sha256_file(path) for path in files},
            "runtime_config_sha256": canonical_sha256(config),
        }
    )


def validate_inputs(manifest: dict, config: dict, root: Path) -> None:
    check_seal(manifest)
    if manifest["limits"] != LIMITS or manifest["runtime_config_sha256"] != canonical_sha256(
        config
    ):
        raise ValueError("rerun configuration drifted")
    for path, digest in manifest["input_sha256"].items():
        if sha256_file(root / path) != digest:
            raise ValueError(f"frozen rerun input changed: {path}")
    for name, group in manifest["cohorts"].items():
        for key in ("protocol", "baseline"):
            path = config["cohorts"][name]["baseline_snapshot"] if key == "baseline" else group[key]
            if sha256_file(root / path) != group[f"{key}_sha256"]:
                raise ValueError(f"frozen {key} changed")
        protocol, selection = load_transfer_protocol(root / group["protocol"])
        expected = [
            c for c in formal_schedule(protocol, selection) if c["visibility"] == "split_pillars"
        ]
        if group["schedule"] != expected:
            raise ValueError("rerun selection drifted")


def environment_batches(jobs: dict) -> list[list[tuple[str, str]]]:
    by_cohort = defaultdict(list)
    for key in jobs:
        by_cohort[key[0]].append(key)
    batches = []
    while any(by_cohort.values()):
        batch = []
        for keys in by_cohort.values():
            count = min(LIMITS["cohort"], LIMITS["global"] - len(batch), len(keys))
            batch.extend(keys[:count])
            del keys[:count]
        batches.append(batch)
    return batches


def dispatch(jobs: dict, execute) -> None:
    """Only submit runnable cells; no worker waits for a provider or environment slot."""
    pending = {key: list(cells) for key, cells in jobs.items()}
    providers, cohorts = Counter(), Counter()
    busy = set()
    active = {}
    with ThreadPoolExecutor(max_workers=LIMITS["global"]) as pool:
        while any(pending.values()) or active:
            for key, cells in pending.items():
                if key in busy or cohorts[key[0]] >= LIMITS["cohort"]:
                    continue
                for index, cell in enumerate(cells):
                    provider = cell["provider"]
                    if providers[provider] >= LIMITS["provider"]:
                        continue
                    if len(active) >= LIMITS["global"]:
                        break
                    cells.pop(index)
                    providers[provider] += 1
                    cohorts[key[0]] += 1
                    busy.add(key)
                    active[pool.submit(execute, key, cell)] = (key, provider)
                    break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            # Inspect every finished future before scheduling more. On failure the executor
            # drains in-flight work; each worker persists its own completion before returning.
            errors = []
            for future in done:
                key, provider = active.pop(future)
                providers[provider] -= 1
                cohorts[key[0]] -= 1
                busy.remove(key)
                try:
                    future.result()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise errors[0]


def _contexts(manifest: dict, root: Path) -> dict:
    return {
        name: load_transfer_protocol(root / group["protocol"])
        for name, group in manifest["cohorts"].items()
    }


def _jobs(manifest: dict, contexts: dict, completed: set) -> dict:
    jobs = defaultdict(list)
    for name, group in manifest["cohorts"].items():
        for cell in group["schedule"]:
            if (name, cell["cell_index"]) not in completed:
                jobs[name, cell["case_id"]].append(cell)
    return {
        key: _case_execution_order(cells, len(contexts[key[0]][0].models))
        for key, cells in jobs.items()
    }


def _prepare_batch(batch, contexts, config, root, instance_root, stack, prepare_fn):
    ownership = Lock()

    def prepare(key):
        name, case_id = key
        protocol, selection = contexts[name]
        spec = next(case for case in selection.selected_cases if case.opaque_case_id == case_id)
        manager = ExitStack()
        try:
            prepared = manager.enter_context(
                prepare_fn(
                    protocol,
                    spec,
                    TransferEnvironmentConfig(
                        cache_dir=root / config["cache_dir"],
                        manifest_path=root / config["source_manifest"],
                        node_cache_dir=root / config["node_cache_dir"],
                        greptimedb_repo=root / config["greptimedb_repo"],
                        run_dir=instance_root / name / case_id,
                        database=case_id.replace("-", "_"),
                    ),
                )
            )
            with ownership:
                stack.callback(manager.close)
            return key, prepared
        except BaseException:
            manager.close()
            raise

    prepared = {}
    errors = []
    with ThreadPoolExecutor(max_workers=LIMITS["preparation"]) as pool:
        futures = [pool.submit(prepare, key) for key in batch]
        for future in futures:
            try:
                key, env = future.result()
                prepared[key] = env
            except Exception as caught:
                errors.append(caught)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("transfer environment preparations failed", errors)
    return prepared


def preflight(manifest, config, root, output, *, prepare_fn=prepare_transfer_environment):
    validate_inputs(manifest, config, root)
    if output.exists():
        raise ValueError("preflight output already exists; use a fresh path")
    contexts = _contexts(manifest, root)
    jobs = _jobs(manifest, contexts, set())
    baselines = {
        name: read(root / paths["baseline_snapshot"]) for name, paths in config["cohorts"].items()
    }
    audits = defaultdict(dict)
    result = {"manifest_sha256": canonical_sha256(manifest), "audits": audits, "complete": False}
    instance_root = root / config["preflight_root"] / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for batch in environment_batches(jobs):
        with ExitStack() as stack:
            prepared = _prepare_batch(
                batch, contexts, config, root, instance_root, stack, prepare_fn
            )

            def probe(key, environments=prepared):
                env = environments[key]
                baseline_source = next(
                    source
                    for source in baselines[key[0]]["sources"]
                    if source["opaque_case_id"] == key[1]
                )
                if _public_source(env.source_audit, env.spec) != baseline_source:
                    raise ValueError(f"source equivalence failed: {key}")
                visibility = audit_transfer_trace_visibility(env)
                return key, {"source_audit": env.source_audit, "trace_visibility": visibility}

            # Eight simultaneous source-based probes exercise the live isolated stacks.
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                for key, audit in pool.map(probe, batch):
                    audits[key[0]][key[1]] = audit
            write_json(output, seal(result))
            print(
                f"preflight: {sum(map(len, audits.values()))}/{len(jobs)} environments passed",
                flush=True,
            )
    result["complete"] = True
    write_json(output, seal(result))
    return result


def validate_preflight(manifest, preflight_report):
    check_seal(preflight_report)
    if (
        preflight_report["manifest_sha256"] != canonical_sha256(manifest)
        or preflight_report["complete"] is not True
    ):
        raise ValueError("preflight incomplete or bound to another manifest")
    for name, group in manifest["cohorts"].items():
        if set(preflight_report["audits"][name]) != {c["case_id"] for c in group["schedule"]}:
            raise ValueError("preflight case roster mismatch")


def validate_record(record, manifest, contexts, preflight_report, name, cell):
    check_seal(record)
    if record["manifest_sha256"] != canonical_sha256(manifest):
        raise ValueError("completed cell manifest drifted")
    expected_audit = preflight_report["audits"][name][cell["case_id"]]["source_audit"]
    digest = source_semantic_sha256(expected_audit)
    if source_semantic_sha256(record["source_audit"]) != digest:
        raise ValueError("completed cell source drifted")
    protocol, selection = contexts[name]
    spec = next(s for s in selection.selected_cases if s.opaque_case_id == cell["case_id"])
    validate_transfer_run_item(record["item"], cell, protocol, spec, digest)
    health = record["trace_health"]
    for side in ("before", "after"):
        gate = health[side]
        if (
            gate["sample_equal"] is not True
            or gate["source_window_traceql_search"] is not True
            or gate["causal_service_search"]["reachable"] is not True
            or gate["stored_sample_sha256"]
            != expected_audit["split_storage"]["traces"]["fidelity_sample_sha256"]
        ):
            raise ValueError("completed cell trace gate failed")
    if health["item"] != record["item"]:
        raise ValueError("trace gate is bound to another run")


def execute(
    manifest,
    config,
    root,
    preflight_report,
    *,
    confirmed,
    max_new_runs,
    prepare_fn=prepare_transfer_environment,
    execute_fn=execute_transfer_cell,
):
    if confirmed is not True or type(max_new_runs) is not int or not 1 <= max_new_runs <= 168:
        raise ValueError("paid invocation requires explicit confirmation and a 1..168 cell ceiling")
    validate_inputs(manifest, config, root)
    validate_preflight(manifest, preflight_report)
    contexts = _contexts(manifest, root)
    run_root = root / config["run_root"]
    with _exclusive_invocation(run_root):
        completed = set()
        for name, group in manifest["cohorts"].items():
            journal = run_root / name
            journal.mkdir(exist_ok=True)
            for cell in group["schedule"]:
                stem = journal / str(cell["cell_index"])
                if stem.with_suffix(".complete.json").exists():
                    record = read(stem.with_suffix(".complete.json"))
                    validate_record(record, manifest, contexts, preflight_report, name, cell)
                    completed.add((name, cell["cell_index"]))
                elif stem.with_suffix(".active.json").exists():
                    raise ValueError(f"ambiguous paid cell; refusing automatic retry: {stem}")
        jobs = _jobs(manifest, contexts, completed)
        remaining = max_new_runs
        selected = {}
        for key, cells in jobs.items():
            if remaining:
                selected[key] = cells[:remaining]
                remaining -= len(selected[key])
        invocation = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        for batch in environment_batches(selected):
            with ExitStack() as stack:
                prepared = _prepare_batch(
                    batch,
                    contexts,
                    config,
                    root,
                    run_root / "instances" / invocation,
                    stack,
                    prepare_fn,
                )
                for (name, case_id), env in prepared.items():
                    expected = preflight_report["audits"][name][case_id]["source_audit"]
                    if source_semantic_sha256(env.source_audit) != source_semantic_sha256(expected):
                        raise ValueError(f"live source drifted: {name}/{case_id}")

                def run(key, cell, environments=prepared):
                    name, _ = key
                    env = environments[key]
                    stem = run_root / name / str(cell["cell_index"])
                    write_json(
                        stem.with_suffix(".active.json"),
                        seal(
                            {
                                "manifest_sha256": canonical_sha256(manifest),
                                "cell": cell,
                                "started_at": datetime.now(UTC).isoformat(),
                            }
                        ),
                    )
                    item = execute_fn(
                        contexts[name][0],
                        env,
                        cell,
                        source_semantic_hash=source_semantic_sha256(env.source_audit),
                    )
                    health = read(
                        env.split_stack.run_dir / f"cell-{cell['cell_index']}-trace-health.json"
                    )
                    record = seal(
                        {
                            "manifest_sha256": canonical_sha256(manifest),
                            "source_audit": env.source_audit,
                            "item": item,
                            "trace_health": health,
                            "finished_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    validate_record(record, manifest, contexts, preflight_report, name, cell)
                    write_json(stem.with_suffix(".complete.json"), record)
                    stem.with_suffix(".active.json").unlink()
                    print(
                        f"completed: {name}/{cell['cell_index']} {cell['model']} "
                        f"{cell['case_id']} rep{cell['repetition']}",
                        flush=True,
                    )
                    # Keep scoreable failures, but stop new spending until their cause is reviewed.
                    if item["run"].get("error"):
                        raise ValueError(
                            f"recorded runner failure; review before continuing: {stem}"
                        )

                dispatch({key: selected[key] for key in batch}, run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "preflight", "run", "check"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--confirm-paid-api", action="store_true")
    parser.add_argument("--max-new-runs", type=int)
    args = parser.parse_args()
    root = Path.cwd()
    config = read(args.config)
    if args.command == "freeze":
        if args.manifest.exists():
            raise ValueError("manifest exists; freeze to a fresh path")
        write_json(args.manifest, freeze(config, root))
        return
    manifest = read(args.manifest)
    validate_inputs(manifest, config, root)
    if args.command == "preflight":
        if args.preflight is None:
            parser.error("--preflight is required")
        preflight(manifest, config, root, args.preflight)
    elif args.command == "run":
        if args.preflight is None:
            parser.error("--preflight is required")
        execute(
            manifest,
            config,
            root,
            read(args.preflight),
            confirmed=args.confirm_paid_api,
            max_new_runs=args.max_new_runs,
        )
    elif args.preflight:
        validate_preflight(manifest, read(args.preflight))
        print("Frozen inputs and all 28 preflight environments verified; no provider called.")


if __name__ == "__main__":
    main()
