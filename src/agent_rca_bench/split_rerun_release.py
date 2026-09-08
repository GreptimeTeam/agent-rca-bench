"""Sanitize the third measurement source and compose it with the two baselines."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

from agent_rca_bench.contracts import AgentRun, DatabaseLoad
from agent_rca_bench.formal_report import validate_formal_measurement_report
from agent_rca_bench.formal_report_view import split_rerun_note
from agent_rca_bench.greptimedb.server import write_json
from agent_rca_bench.split_rerun import (
    _contexts,
    check_seal,
    read,
    seal,
    validate_inputs,
    validate_preflight,
    validate_record,
)
from agent_rca_bench.transfer_protocol import load_transfer_protocol, sha256_file
from agent_rca_bench.transfer_release import (
    _model_reports,
    _public_source,
    _reject_private_fields,
    canonical_sha256,
    public_run_usage,
    sanitize_transfer_run,
    validate_measurement_artifact,
)
from agent_rca_bench.transfer_scorer import TransferEvaluation


def export(manifest: dict, config: dict, root: Path, preflight: dict) -> dict:
    validate_inputs(manifest, config, root)
    validate_preflight(manifest, preflight)
    contexts = _contexts(manifest, root)
    groups = {}
    for name, group in manifest["cohorts"].items():
        protocol, selection = contexts[name]
        specs = {spec.opaque_case_id: spec for spec in selection.selected_cases}
        sources, runs, replacements = {}, [], []
        for cell in group["schedule"]:
            path = root / config["run_root"] / name / f"{cell['cell_index']}.complete.json"
            record = read(path)
            validate_record(record, manifest, contexts, preflight, name, cell)
            item = record["item"]
            run = AgentRun.model_validate(item["run"])
            public = sanitize_transfer_run(
                run,
                TransferEvaluation.model_validate(item["evaluation"]),
                specs[cell["case_id"]],
                DatabaseLoad.model_validate(item["database_load"]),
            )
            public["usage"] = public_run_usage(run, group["pricing_snapshot"][cell["model"]])
            public["adjudication"] = {"status": "disabled"}
            digest = item["source_semantic_sha256"]
            sources[cell["case_id"]] = {
                "source_semantic_sha256": digest,
                "source": _public_source(record["source_audit"], specs[cell["case_id"]]),
                "split_storage": {
                    "pass": record["source_audit"]["split_storage"]["pass"],
                    "gates": record["source_audit"]["split_storage"]["gates"],
                    "trace_sample_sha256": record["trace_health"]["before"]["stored_sample_sha256"],
                },
            }
            public_cell = {**cell, "source_semantic_sha256": digest, "run": public}
            runs.append(public_cell)
            replacements.append(
                {
                    "cell_index": cell["cell_index"],
                    "superseded_cell_sha256": group["superseded_cells"][str(cell["cell_index"])],
                    "replacement_cell_sha256": canonical_sha256(public_cell),
                    "trace_gate": {
                        side: {
                            "sample_equal": record["trace_health"][side]["sample_equal"],
                            "source_window_traceql_search": record["trace_health"][side][
                                "source_window_traceql_search"
                            ],
                            "causal_traces_reachable": record["trace_health"][side][
                                "causal_service_search"
                            ]["reachable"],
                            "stored_sample_sha256": record["trace_health"][side][
                                "stored_sample_sha256"
                            ],
                        }
                        for side in ("before", "after")
                    },
                }
            )
        groups[name] = {
            "baseline_sha256": group["baseline_sha256"],
            "protocol_sha256": group["protocol_sha256"],
            "pricing_snapshot": group["pricing_snapshot"],
            "sources": sources,
            "runs": runs,
            "replacements": replacements,
        }
    result = seal(
        {
            "kind": "split-interface-rerun-measurement",
            "schema_version": 1,
            "manifest": manifest,
            "cohorts": groups,
            "trace_checks": (
                "Source visibility and sample fidelity passed before and after "
                "every replacement run."
            ),
            "provenance_scope": (
                "Source projections are compared at export and composition; hashes bind "
                "the private source audits and cell records. These are provenance, "
                "not proof of private telemetry."
            ),
        }
    )
    _reject_private_fields(result)
    compose(
        result,
        {
            name: read(root / paths["baseline_snapshot"])
            for name, paths in config["cohorts"].items()
        },
        {name: root / group["protocol"] for name, group in manifest["cohorts"].items()},
    )
    return result


def compose(rerun: dict, baselines: dict, protocol_paths: dict) -> dict:
    check_seal(rerun)
    manifest = rerun["manifest"]
    check_seal(manifest)
    if (
        rerun["kind"] != "split-interface-rerun-measurement"
        or set(rerun["cohorts"]) != set(manifest["cohorts"])
        or set(baselines) != set(manifest["cohorts"])
    ):
        raise ValueError("rerun cohort roster drifted")
    _reject_private_fields(rerun)
    retry_audit = rerun.get("retry_audit")
    if retry_audit is not None:
        check_seal(retry_audit)
        if any(
            item["cohort"] not in manifest["cohorts"]
            for item in (*retry_audit["failed_attempts"], *retry_audit["retained_failures"])
        ):
            raise ValueError("retry audit contains an unknown cohort")
    encoding_estimate = rerun.get("retained_encoding_estimate")
    if encoding_estimate is not None:
        if set(encoding_estimate["cohorts"]) != set(manifest["cohorts"]):
            raise ValueError("retained encoding estimate cohort roster drifted")
        for arm in ("raw", "semantic_graph"):
            measured = sum(
                cell["run"]["usage"]["provider_visible_input_tokens"]
                for baseline in baselines.values()
                for cell in baseline["runs"]
                if cell["visibility"] == arm
            )
            estimate = encoding_estimate["by_treatment"][arm]
            if (
                estimate["measured_input"] != measured
                or not 0 <= estimate["estimated_reencoding_saving"] <= measured
                or estimate["estimated_share"] != estimate["estimated_reencoding_saving"] / measured
            ):
                raise ValueError("retained encoding estimate denominator or fraction drifted")
    outputs = {}
    for name, group in rerun["cohorts"].items():
        baseline = baselines[name]
        path = protocol_paths[name]
        validate_measurement_artifact(baseline, path)
        protocol, selection = load_transfer_protocol(path)
        expected = [item for item in baseline["runs"] if item["visibility"] == "split_pillars"]
        frozen = manifest["cohorts"][name]
        if (
            group["protocol_sha256"] != sha256_file(path)
            or group["protocol_sha256"] != frozen["protocol_sha256"]
            or group["baseline_sha256"] != frozen["baseline_sha256"]
            or group["pricing_snapshot"] != frozen["pricing_snapshot"]
            or len(group["runs"]) != len(expected)
        ):
            raise ValueError("rerun source binding or cell count drifted")
        # The semantic baseline binding survives JSON formatting changes during reproduction.
        if canonical_sha256(baseline) != frozen["baseline_semantic_sha256"]:
            raise ValueError("rerun baseline contents changed")
        baseline_sources = {source["opaque_case_id"]: source for source in baseline["sources"]}
        if set(group["sources"]) != set(baseline_sources):
            raise ValueError("rerun source roster drifted")
        for case_id, source in group["sources"].items():
            if (
                source["source"] != baseline_sources[case_id]
                or source["split_storage"]["pass"] is not True
                or not all(source["split_storage"]["gates"].values())
            ):
                raise ValueError("rerun source equivalence failed")
        replacements = {}
        for old, new, cell, replacement in zip(
            expected, group["runs"], frozen["schedule"], group["replacements"], strict=True
        ):
            if (
                replacement["cell_index"] != cell["cell_index"]
                or replacement["superseded_cell_sha256"] != canonical_sha256(old)
                or replacement["superseded_cell_sha256"]
                != frozen["superseded_cells"][str(cell["cell_index"])]
                or replacement["replacement_cell_sha256"] != canonical_sha256(new)
            ):
                raise ValueError("rerun superseded or replacement cell binding drifted")
            if (
                any(new.get(key) != value for key, value in cell.items())
                or any(old.get(key) != value for key, value in cell.items())
                or new["source_semantic_sha256"]
                != group["sources"][cell["case_id"]]["source_semantic_sha256"]
            ):
                raise ValueError("rerun replacement schedule or source drifted")
            replacements[cell["cell_index"]] = new
        output = deepcopy(baseline)
        output["runs"] = [
            deepcopy(replacements.get(item["cell_index"], item)) for item in baseline["runs"]
        ]
        output["model_reports"] = _model_reports(
            output["runs"],
            [model.model for model in protocol.models],
            family_size=protocol.inference.holm_family_size,
            families=protocol.inference.confirmatory_families,
        )
        output["bindings"].pop("private_report_semantic_sha256", None)
        output["bindings"]["baseline_artifact_semantic_sha256"] = canonical_sha256(baseline)
        output["bindings"]["split_rerun_semantic_sha256"] = canonical_sha256(rerun)
        output["split_rerun_sources"] = deepcopy(group["sources"])
        output["split_rerun"] = {
            "schema_version": 1,
            "cohort": name,
            "changes": deepcopy(manifest["changes"]),
            "execution_amendment": manifest["execution_amendment"],
            "replaced_cells": len(replacements),
            "retained_cells": len(baseline["runs"]) - len(replacements),
            "source_artifact_sha256": canonical_sha256(rerun),
            "baseline_artifact_sha256": canonical_sha256(baseline),
            "replacements": deepcopy(group["replacements"]),
            "retained_cell_sha256": {
                str(item["cell_index"]): canonical_sha256(item)
                for item in baseline["runs"]
                if item["visibility"] != "split_pillars"
            },
        }
        if any(cell["provider"] == "google" for cell in frozen["schedule"]):
            output["split_rerun"]["gemini_input_pacing"] = deepcopy(manifest["gemini_input_pacing"])
        if retry_audit is not None:
            output["split_rerun"]["manifest_sha256"] = canonical_sha256(manifest)
            output["split_rerun"]["retry_audit"] = deepcopy(retry_audit)
        if encoding_estimate is not None:
            output["split_rerun"]["retained_encoding_estimate"] = deepcopy(encoding_estimate)
        output["pricing_snapshot"] = deepcopy(group["pricing_snapshot"])
        if output["pricing_snapshot"] != baseline["pricing_snapshot"]:
            raise ValueError("mixed pricing revisions require explicit per-run pricing support")
        output["integrity"]["semantic_payload_sha256"] = canonical_sha256(
            {key: value for key, value in output.items() if key != "integrity"}
        )
        validate_measurement_artifact(output, path)
        outputs[name] = output
    return outputs


def sync_correction(report: dict, english: Path, chinese: Path) -> None:
    validate_formal_measurement_report(report)
    if not report.get("split_reruns"):
        raise ValueError("report has no completed Split correction")
    note = split_rerun_note(report["split_reruns"], report["usage_by_treatment"])
    start, end = "<!-- split-rerun-correction:start -->", "<!-- split-rerun-correction:end -->"
    updates = {}
    for language, path in (("en", english), ("zh", chinese)):
        text = path.read_text()
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
            raise ValueError(f"correction section markers missing or ambiguous: {path}")
        prefix, suffix = text.split(start)[0], text.split(end)[1]
        updates[path] = f"{prefix}{start}\n{note[language]}\n{end}{suffix}"
    for path, text in updates.items():
        path.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "compose", "sync-correction"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--rerun", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--english", type=Path)
    parser.add_argument("--chinese", type=Path)
    args = parser.parse_args()
    if args.command == "sync-correction":
        if not args.report or not args.english or not args.chinese:
            parser.error("sync-correction requires --report, --english and --chinese")
        sync_correction(read(args.report), args.english, args.chinese)
        return
    if not args.rerun:
        parser.error("export and compose require --rerun")
    root = Path.cwd()
    if args.command == "export":
        if not args.config or not args.manifest or not args.preflight:
            parser.error("export requires --config, --manifest and --preflight")
        if args.rerun.exists():
            raise ValueError("rerun output already exists")
        write_json(
            args.rerun,
            export(read(args.manifest), read(args.config), root, read(args.preflight)),
        )
    else:
        if not args.output_dir:
            parser.error("compose requires --output-dir")
        rerun = read(args.rerun)
        paths = read(args.config)["cohorts"] if args.config else rerun["manifest"]["cohorts"]
        baseline_key = "baseline_snapshot" if args.config else "baseline"
        outputs = compose(
            rerun,
            {name: read(root / group[baseline_key]) for name, group in paths.items()},
            {name: root / group["protocol"] for name, group in paths.items()},
        )
        if args.output_dir.exists():
            raise ValueError("composition output directory already exists")
        args.output_dir.mkdir(parents=True)
        for name, output in outputs.items():
            write_json(args.output_dir / f"{name}-transfer.json", output)


if __name__ == "__main__":
    main()
