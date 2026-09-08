import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from agent_rca_bench import split_rerun as rerun
from agent_rca_bench.greptimedb.server import write_json
from agent_rca_bench.split_rerun_release import compose
from agent_rca_bench.transfer_protocol import load_transfer_protocol
from agent_rca_bench.transfer_release import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "cohorts": {
        "four_model": {
            "protocol": "fixtures/reference/transfer-v34-protocol.json",
            "baseline": "artifacts/measurement/agent-rca-v34-transfer.json",
            "baseline_snapshot": "artifacts/measurement/agent-rca-v34-transfer.json",
        },
        "two_model_extension": {
            "protocol": "fixtures/reference/transfer-v34-two-model-extension-protocol.json",
            "baseline": "artifacts/measurement/agent-rca-v34-two-model-extension-transfer.json",
            "baseline_snapshot": (
                "artifacts/measurement/agent-rca-v34-two-model-extension-transfer.json"
            ),
        },
    },
    "cache_dir": ".data/openrca2",
    "source_manifest": ".data/openrca2/manifest.jsonl",
    "node_cache_dir": ".data/rca100",
    "greptimedb_repo": ".local/greptimedb-v34",
    "preflight_root": ".local/instances/v34-split-rerun-preflight",
    "run_root": ".local/instances/v34-split-rerun-paid",
}


@pytest.fixture(scope="module")
def manifest():
    return rerun.freeze(CONFIG, ROOT)


def test_dispatch_overlaps_models_and_respects_all_limits(manifest):
    contexts = rerun._contexts(manifest, ROOT)
    jobs = rerun._jobs(manifest, contexts, set())
    counters, maximum, seen = Counter(), Counter(), defaultdict(list)
    lock = Lock()
    first_wave = Barrier(8, timeout=5)
    started = 0

    def run(key, cell):
        nonlocal started
        resources = ["global", ("cohort", key[0]), ("provider", cell["provider"]), ("env", key)]
        with lock:
            started += 1
            first = started <= 8
            for resource in resources:
                counters[resource] += 1
                maximum[resource] = max(maximum[resource], counters[resource])
            seen[key, cell["model"]].append(cell["repetition"])
        if first:
            first_wave.wait()
        time.sleep(0.001)
        with lock:
            for resource in resources:
                counters[resource] -= 1

    for batch in rerun.environment_batches(jobs):
        rerun.dispatch({key: jobs[key] for key in batch}, run)
    assert started == 168
    assert maximum["global"] == 8
    for resource, value in maximum.items():
        if isinstance(resource, tuple):
            assert value <= {"cohort": 4, "provider": 2, "env": 1}[resource[0]]
    assert all(repetitions == [0, 1] for repetitions in seen.values())


def test_dispatch_drains_inflight_and_does_not_start_more_after_failure():
    barrier = Barrier(2, timeout=5)
    finished = []
    jobs = {
        ("a", str(i)): [{"model": "m", "provider": "p", "repetition": rep} for rep in (1, 2)]
        for i in range(2)
    }

    def run(key, cell):
        assert cell["repetition"] == 1
        barrier.wait()
        if key[1] == "0":
            raise ValueError("provider failed")
        time.sleep(0.02)
        finished.append(key)

    with pytest.raises(ValueError, match="provider failed"):
        rerun.dispatch(jobs, run)
    assert finished == [("a", "1")]


def test_paid_confirmation_precedes_all_io(tmp_path):
    with pytest.raises(ValueError, match="explicit confirmation"):
        rerun.execute({}, {}, tmp_path, {}, confirmed=False, max_new_runs=168)
    assert not list(tmp_path.iterdir())


def test_preparation_failure_closes_every_owned_environment(tmp_path):
    contexts = {"a": load_transfer_protocol(ROOT / CONFIG["cohorts"]["four_model"]["protocol"])}
    cases = [spec.opaque_case_id for spec in contexts["a"][1].selected_cases[:4]]
    closed = []

    @contextmanager
    def prepare(protocol, spec, config):
        if spec.opaque_case_id == cases[1]:
            raise ValueError("ingestion failed")
        try:
            yield SimpleNamespace(spec=spec)
        finally:
            closed.append(spec.opaque_case_id)

    from contextlib import ExitStack

    with pytest.raises(ValueError, match="ingestion failed"), ExitStack() as stack:
        rerun._prepare_batch(
            [("a", case) for case in cases], contexts, CONFIG, ROOT, tmp_path, stack, prepare
        )
    assert set(closed) == set(cases) - {cases[1]}


def test_manifest_rejects_code_or_configuration_drift(manifest):
    altered = deepcopy(CONFIG)
    altered["run_root"] += "-other"
    with pytest.raises(ValueError, match="configuration drifted"):
        rerun.validate_inputs(manifest, altered, ROOT)
    broken = deepcopy(manifest)
    broken["input_sha256"]["src/agent_rca_bench/agent.py"] = "0" * 64
    with pytest.raises(ValueError, match="frozen rerun input changed"):
        rerun.validate_inputs(rerun.seal(broken), CONFIG, ROOT)


@pytest.fixture(scope="module")
def public_replay(manifest):
    # Existing sanitized measurements stand in for new observations only inside this test.
    baselines = {
        name: rerun.read(ROOT / paths["baseline"]) for name, paths in CONFIG["cohorts"].items()
    }
    groups = {}
    for name, baseline in baselines.items():
        frozen = manifest["cohorts"][name]
        runs = deepcopy(
            [item for item in baseline["runs"] if item["visibility"] == "split_pillars"]
        )
        sources = {}
        for source in baseline["sources"]:
            digest = next(
                item["source_semantic_sha256"]
                for item in runs
                if item["case_id"] == source["opaque_case_id"]
            )
            sources[source["opaque_case_id"]] = {
                "source": source,
                "source_semantic_sha256": digest,
                "split_storage": {
                    "pass": True,
                    "gates": {"fixture": True},
                    "trace_sample_sha256": "sample",
                },
            }
        groups[name] = {
            "baseline_sha256": frozen["baseline_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "pricing_snapshot": frozen["pricing_snapshot"],
            "runs": runs,
            "sources": sources,
            "replacements": [
                {
                    "cell_index": cell["cell_index"],
                    "superseded_cell_sha256": canonical_sha256(cell),
                    "replacement_cell_sha256": canonical_sha256(cell),
                    "trace_gate": {
                        side: {
                            "sample_equal": True,
                            "source_window_traceql_search": True,
                            "causal_traces_reachable": True,
                            "stored_sample_sha256": "sample",
                        }
                        for side in ("before", "after")
                    },
                }
                for cell in runs
            ],
        }
    return rerun.seal(
        {
            "kind": "split-interface-rerun-measurement",
            "schema_version": 1,
            "manifest": manifest,
            "cohorts": groups,
        }
    ), baselines


def test_composition_preserves_all_retained_cells_and_statistics(public_replay):
    artifact, baselines = public_replay
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    outputs = compose(artifact, baselines, paths)
    retained = 0
    for name, output in outputs.items():
        baseline = baselines[name]
        for old, new in zip(baseline["runs"], output["runs"], strict=True):
            if old["visibility"] != "split_pillars":
                assert old == new
                retained += 1
        assert output["model_reports"] == baseline["model_reports"]
        assert "private_report_semantic_sha256" not in output["bindings"]
    assert retained == 336


def test_composition_cli_uses_public_inputs_without_local_config(
    public_replay, tmp_path, monkeypatch
):
    from agent_rca_bench.split_rerun_release import main

    artifact, baselines = public_replay
    source = tmp_path / "rerun.json"
    destination = tmp_path / "composed"
    write_json(source, artifact)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        "sys.argv",
        [
            "split-rerun-release",
            "compose",
            "--rerun",
            str(source),
            "--output-dir",
            str(destination),
        ],
    )
    main()
    for name, baseline in baselines.items():
        result = rerun.read(destination / f"{name}-transfer.json")
        assert result["model_reports"] == baseline["model_reports"]
        assert result["bindings"]["split_rerun_semantic_sha256"] == canonical_sha256(artifact)


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "raw", "source", "baseline"])
def test_composition_rejects_incomplete_or_misbound_replacements(public_replay, corruption):
    artifact, baselines = deepcopy(public_replay)
    group = artifact["cohorts"]["four_model"]
    if corruption == "missing":
        group["runs"].pop()
    elif corruption == "duplicate":
        group["runs"][1] = group["runs"][0]
    elif corruption == "raw":
        group["runs"][0]["visibility"] = "raw"
    elif corruption == "source":
        group["runs"][0]["source_semantic_sha256"] = "0" * 64
    else:
        baselines["four_model"]["publication_status"] += " altered"
        baseline = baselines["four_model"]
        baseline["integrity"]["semantic_payload_sha256"] = canonical_sha256(
            {key: value for key, value in baseline.items() if key != "integrity"}
        )
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    with pytest.raises(ValueError):
        compose(rerun.seal(artifact), baselines, paths)


def test_ambiguous_paid_cell_is_never_retried(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr(rerun, "validate_inputs", lambda *args: None)
    monkeypatch.setattr(rerun, "validate_preflight", lambda *args: None)
    config = {**CONFIG, "run_root": str(tmp_path / "paid")}
    cell = manifest["cohorts"]["four_model"]["schedule"][0]
    journal = tmp_path / "paid/four_model"
    journal.mkdir(parents=True)
    write_json(journal / f"{cell['cell_index']}.active.json", {"cell": cell})
    with pytest.raises(ValueError, match="ambiguous paid cell"):
        rerun.execute(manifest, config, ROOT, {}, confirmed=True, max_new_runs=168)


def test_paid_failure_is_durable_and_resume_skips_it(tmp_path, monkeypatch, manifest):
    from test_transfer_concurrent import _failed_item

    monkeypatch.setattr(rerun, "validate_inputs", lambda *args: None)
    config = {**CONFIG, "run_root": str(tmp_path / "paid")}
    audits = {}
    for name, group in manifest["cohorts"].items():
        audits[name] = {
            cell["case_id"]: {
                "source_audit": {
                    "case": {"opaque_case_id": cell["case_id"]},
                    "split_storage": {"traces": {"fidelity_sample_sha256": "sample"}},
                }
            }
            for cell in group["schedule"]
        }
    preflight = rerun.seal(
        {"manifest_sha256": canonical_sha256(manifest), "complete": True, "audits": audits}
    )
    called, closed, preparing, running = [], [], set(), set()
    lock = Lock()

    @contextmanager
    def prepare(protocol, spec, config):
        name = next(
            name for name, (p, _) in rerun._contexts(manifest, ROOT).items() if p == protocol
        )
        with lock:
            assert not running
            preparing.add((name, spec.opaque_case_id))
        config.run_dir.mkdir(parents=True)
        env = SimpleNamespace(
            spec=spec,
            source_audit=audits[name][spec.opaque_case_id]["source_audit"],
            split_stack=SimpleNamespace(run_dir=config.run_dir),
        )
        with lock:
            preparing.remove((name, spec.opaque_case_id))
        try:
            yield env
        finally:
            assert not running
            closed.append((name, spec.opaque_case_id))

    def execute(protocol, env, cell, **kwargs):
        with lock:
            assert not preparing
            running.add(cell["cell_index"])
            called.append(cell["cell_index"])
        item = _failed_item(protocol, env, cell, **kwargs)
        gate = {
            "sample_equal": True,
            "source_window_traceql_search": True,
            "causal_service_search": {"reachable": True},
            "stored_sample_sha256": "sample",
        }
        write_json(
            env.split_stack.run_dir / f"cell-{cell['cell_index']}-trace-health.json",
            {"before": gate, "after": gate, "item": item},
        )
        with lock:
            running.remove(cell["cell_index"])
        return item

    for _ in range(2):
        with pytest.raises(ValueError, match="recorded runner failure"):
            rerun.execute(
                manifest,
                config,
                ROOT,
                preflight,
                confirmed=True,
                max_new_runs=1,
                prepare_fn=prepare,
                execute_fn=execute,
            )
    assert len(called) == len(set(called)) == 2
    assert len(closed) == 2
    assert len(list((tmp_path / "paid").glob("*/*.complete.json"))) == 2
    assert not list((tmp_path / "paid").glob("*/*.active.json"))


def test_export_sanitizes_complete_private_journals(tmp_path, monkeypatch, manifest):
    from test_transfer_concurrent import _failed_item

    from agent_rca_bench import split_rerun_release as release

    monkeypatch.setattr(release, "validate_inputs", lambda *args: None)
    contexts = rerun._contexts(manifest, ROOT)
    public_sources = {}
    for name, group in manifest["cohorts"].items():
        public_sources[name] = {
            source["opaque_case_id"]: source
            for source in rerun.read(ROOT / group["baseline"])["sources"]
        }
    monkeypatch.setattr(
        release,
        "_public_source",
        lambda audit, spec: public_sources[audit["cohort"]][spec.opaque_case_id],
    )
    audits = {}
    records = []
    for name, group in manifest["cohorts"].items():
        protocol, selection = contexts[name]
        specs = {spec.opaque_case_id: spec for spec in selection.selected_cases}
        audits[name] = {}
        for cell in group["schedule"]:
            case_id = cell["case_id"]
            audit = {
                "cohort": name,
                "case": {"opaque_case_id": case_id},
                "split_storage": {
                    "traces": {"fidelity_sample_sha256": "sample"},
                    "pass": True,
                    "gates": {"synthetic": True},
                },
            }
            audits[name][case_id] = {"source_audit": audit}
            env = SimpleNamespace(spec=specs[case_id])
            item = _failed_item(
                protocol, env, cell, source_semantic_hash=rerun.source_semantic_sha256(audit)
            )
            gate = {
                "sample_equal": True,
                "source_window_traceql_search": True,
                "causal_service_search": {"reachable": True},
                "stored_sample_sha256": "sample",
            }
            record = rerun.seal(
                {
                    "manifest_sha256": canonical_sha256(manifest),
                    "source_audit": audit,
                    "item": item,
                    "trace_health": {"before": gate, "after": gate, "item": item},
                }
            )
            records.append((name, cell, record))
    preflight = rerun.seal(
        {"manifest_sha256": canonical_sha256(manifest), "complete": True, "audits": audits}
    )
    config = {**CONFIG, "run_root": str(tmp_path)}
    for name, cell, record in records:
        write_json(tmp_path / name / f"{cell['cell_index']}.complete.json", record)
    result = release.export(manifest, config, ROOT, preflight)
    assert sum(len(group["runs"]) for group in result["cohorts"].values()) == 168
    text = __import__("json").dumps(result)
    assert "synthetic provider failure" not in text
    assert '"responses"' not in text
    assert '"run_id"' not in text
    last = records[-1]
    (tmp_path / last[0] / f"{last[1]['cell_index']}.complete.json").unlink()
    with pytest.raises(FileNotFoundError):
        release.export(manifest, config, ROOT, preflight)


def test_preparation_reports_all_failures(tmp_path):
    from contextlib import ExitStack

    contexts = {"a": load_transfer_protocol(ROOT / CONFIG["cohorts"]["four_model"]["protocol"])}
    cases = [spec.opaque_case_id for spec in contexts["a"][1].selected_cases[:2]]

    @contextmanager
    def prepare(protocol, spec, config):
        raise ValueError(spec.opaque_case_id)
        yield

    with pytest.raises(ExceptionGroup) as caught, ExitStack() as stack:
        rerun._prepare_batch(
            [("a", case) for case in cases], contexts, CONFIG, ROOT, tmp_path, stack, prepare
        )
    assert [str(error) for error in caught.value.exceptions] == cases


def test_preflight_records_one_point_in_time_visibility_check(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr(rerun, "validate_inputs", lambda *args: None)
    checks = Counter()
    sources = {
        (name, source["opaque_case_id"]): source
        for name, paths in CONFIG["cohorts"].items()
        for source in rerun.read(ROOT / paths["baseline"])["sources"]
    }

    @contextmanager
    def prepare(protocol, spec, config):
        name = next(
            name for name, (p, _) in rerun._contexts(manifest, ROOT).items() if p == protocol
        )
        yield SimpleNamespace(
            spec=spec,
            source_audit=sources[name, spec.opaque_case_id],
            key=(name, spec.opaque_case_id),
        )

    def audit(env):
        checks[env.key] += 1
        return {"sample_equal": True}

    monkeypatch.setattr(rerun, "_public_source", lambda source, spec: source)
    monkeypatch.setattr(rerun, "audit_transfer_trace_visibility", audit)
    result = rerun.preflight(
        manifest, CONFIG, ROOT, tmp_path / "preflight.json", prepare_fn=prepare
    )
    assert len(checks) == 28 and set(checks.values()) == {1}
    assert all(
        set(item) == {"source_audit", "trace_visibility"}
        for group in result["audits"].values()
        for item in group.values()
    )


@pytest.mark.parametrize("corruption", ["missing", "count", "retained", "new_hash", "gate"])
def test_correction_audit_rejects_tampering(public_replay, corruption):
    from agent_rca_bench.transfer_release import validate_split_rerun

    artifact, baselines = public_replay
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    output = compose(artifact, baselines, paths)["four_model"]
    correction = output["split_rerun"]
    if corruption == "missing":
        del output["split_rerun"]
    elif corruption == "count":
        correction["replaced_cells"] -= 1
    elif corruption == "retained":
        correction["retained_cell_sha256"].clear()
    elif corruption == "new_hash":
        correction["replacements"][0]["replacement_cell_sha256"] = "0" * 64
    else:
        correction["replacements"][0]["trace_gate"]["after"]["sample_equal"] = False
    with pytest.raises(ValueError, match="Split"):
        validate_split_rerun(output)


def test_composition_rejects_wrong_superseded_hash(public_replay):
    artifact, baselines = deepcopy(public_replay)
    artifact["cohorts"]["four_model"]["replacements"][0]["superseded_cell_sha256"] = "0" * 64
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    with pytest.raises(ValueError, match="superseded"):
        compose(rerun.seal(artifact), baselines, paths)


@pytest.mark.parametrize("corruption", ["denominator", "fraction"])
def test_retained_encoding_estimate_is_bound_to_retained_usage(public_replay, corruption):
    artifact, baselines = deepcopy(public_replay)
    estimates = {}
    for arm in ("raw", "semantic_graph"):
        measured = sum(
            cell["run"]["usage"]["provider_visible_input_tokens"]
            for baseline in baselines.values()
            for cell in baseline["runs"]
            if cell["visibility"] == arm
        )
        estimates[arm] = {
            "measured_input": measured,
            "estimated_reencoding_saving": 1000,
            "estimated_share": 1000 / measured,
        }
    artifact["retained_encoding_estimate"] = {
        "cohorts": list(baselines),
        "by_treatment": estimates,
    }
    if corruption == "denominator":
        estimates["raw"]["measured_input"] += 1
    else:
        estimates["raw"]["estimated_share"] = 0.5
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    with pytest.raises(ValueError, match="denominator or fraction"):
        compose(rerun.seal(artifact), baselines, paths)


@pytest.fixture
def retry_replay(public_replay):
    artifact, baselines = deepcopy(public_replay)
    digest = canonical_sha256(artifact["manifest"])
    policy = rerun.seal(
        {"manifest_sha256": digest, "max_cell_reruns": 3, "max_total_attempts_per_cell": 4}
    )
    amendment = rerun.seal(
        {
            "manifest_sha256": digest,
            "retry_policy_sha256": canonical_sha256(policy),
            "measurement_code_changed": False,
        }
    )
    attempt = deepcopy(artifact["cohorts"]["four_model"]["runs"][0])
    final_cell_sha256 = canonical_sha256(attempt)
    attempt.update(
        cohort="four_model",
        attempt=1,
        private_record_sha256="a" * 64,
        retry_decision_sha256="b" * 64,
        final_cell_sha256=final_cell_sha256,
        failure_class="temporary_provider_or_transport_failure",
        failure_detail="sdk_connection_error",
    )
    attempt["run"]["execution"]["runner_error"] = True
    attempt["run"]["execution"]["tool_budget_exhausted"] = False
    attempt["run"]["evaluation"]["efficiency_eligible"] = False
    attempt["run"]["usage"]["cost_currency"] = "USD"
    attempt["run"]["usage"]["estimated_cost"] = 1.25
    artifact["retry_audit"] = rerun.seal(
        {
            "kind": "split-rerun-retry-audit",
            "manifest_sha256": digest,
            "policy": policy,
            "supervisor_amendments": [amendment],
            "failed_attempts": [attempt],
            "retained_failures": [],
        }
    )
    return rerun.seal(artifact), baselines


def test_retry_audit_survives_composition_without_double_counting_cost(retry_replay):
    from agent_rca_bench.formal_report_view import split_rerun_note

    artifact, baselines = retry_replay
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    outputs = compose(artifact, baselines, paths)
    corrections = [output["split_rerun"] for output in outputs.values()]
    note = split_rerun_note(corrections, {})
    assert "1 failed attempt was retried across 1 cell" in note["en"]
    assert "USD 1.2500" in note["en"] and "USD 2.5000" not in note["en"]
    assert "正常费用已计入" in note["zh"]
    assert "completed cells are not automatically retried" not in note["en"]
    for name, output in outputs.items():
        assert output["runs"] == baselines[name]["runs"]
        assert output["model_reports"] == baselines[name]["model_reports"]


@pytest.mark.parametrize(
    "corruption", ["cap", "gap", "duplicate", "cell", "configuration", "budget", "chain"]
)
def test_retry_audit_rejects_invalid_recovery(retry_replay, corruption):
    artifact, baselines = retry_replay
    audit = artifact["retry_audit"]
    attempt = audit["failed_attempts"][0]
    if corruption == "cap":
        audit["policy"]["max_cell_reruns"] = 4
        audit["policy"] = rerun.seal(audit["policy"])
    elif corruption == "gap":
        attempt["attempt"] = 2
    elif corruption == "duplicate":
        audit["failed_attempts"].append(deepcopy(attempt))
    elif corruption == "cell":
        attempt["case_id"] = "wrong-case"
    elif corruption == "configuration":
        attempt["run"]["max_output_tokens"] += 1
    elif corruption == "budget":
        attempt["run"]["execution"]["tool_budget_exhausted"] = True
    else:
        amendment = deepcopy(audit["supervisor_amendments"][0])
        amendment["supersedes_sha256"] = "0" * 64
        audit["supervisor_amendments"].append(rerun.seal(amendment))
    artifact["retry_audit"] = rerun.seal(audit)
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    with pytest.raises(ValueError, match="Split retry"):
        compose(rerun.seal(artifact), baselines, paths)


def test_retry_audit_requires_retained_output_failure_disclosure(retry_replay):
    from agent_rca_bench.formal_report_view import split_rerun_note
    from agent_rca_bench.transfer_release import validate_split_retry_audit

    artifact, _ = retry_replay
    runs = artifact["cohorts"]["four_model"]["runs"]
    failed = runs[-1]
    failed["run"]["execution"]["runner_error"] = True
    audit = artifact["retry_audit"]
    digest = canonical_sha256(artifact["manifest"])
    with pytest.raises(ValueError, match="omits or duplicates"):
        validate_split_retry_audit(audit, digest, "four_model", runs)
    audit["retained_failures"] = [
        {
            "cohort": "four_model",
            **{key: failed[key] for key in ("cell_index", "case_id", "model", "repetition")},
            "failure_class": "output_token_limit",
            "final_cell_sha256": canonical_sha256(failed),
        }
    ]
    audit = rerun.seal(audit)
    validate_split_retry_audit(audit, digest, "four_model", runs)
    note = split_rerun_note(
        [
            {
                "cohort": "four_model",
                "replaced_cells": len(runs),
                "retained_cells": 2 * len(runs),
                "retry_audit": audit,
            }
        ],
        {},
    )
    assert "Output-limited failures were retained without retry" in note["en"]
    assert "输出受限的失败原样保留，未重跑" in note["zh"]
    assert "runner error" in note["zh"]


def test_correction_is_bilingual_in_view_noscript_and_markdown(public_replay, tmp_path):
    from agent_rca_bench.formal_report_view import (
        build_report_view_model,
        render_formal_measurement_report,
    )
    from agent_rca_bench.split_rerun_release import sync_correction

    artifact, baselines = public_replay
    paths = {name: ROOT / group["protocol"] for name, group in CONFIG["cohorts"].items()}
    outputs = compose(artifact, baselines, paths)
    report = rerun.read(ROOT / "artifacts/measurement/agent-rca-v34-six-model.json")
    report["split_reruns"] = [output["split_rerun"] for output in outputs.values()]
    report["integrity"]["semantic_payload_sha256"] = canonical_sha256(
        {key: value for key, value in report.items() if key != "integrity"}
    )
    view = build_report_view_model(report)
    note = view["split_rerun_note"]
    assert "168 Split cells" in note["en"] and "336 Raw/Graph" in note["en"]
    assert "168 个 Split" in note["zh"] and "336 个 Raw/Graph" in note["zh"]
    assert "1,800,000-input-token/minute" in note["en"]
    assert "总耗时包含等待额度" in note["zh"]
    html = tmp_path / "report.html"
    render_formal_measurement_report(report, html)
    noscript = html.read_text().split('id="fallback"')[1].split("<script")[0]
    assert note["en"] in noscript and note["zh"] in noscript
    english, chinese = tmp_path / "REPORT.md", tmp_path / "REPORT.zh-CN.md"
    for path in (english, chinese):
        path.write_text(
            "prefix\n<!-- split-rerun-correction:start -->\npending\n"
            "<!-- split-rerun-correction:end -->\nsuffix"
        )
    sync_correction(report, english, chinese)
    assert note["en"] in english.read_text() and note["zh"] in chinese.read_text()
    before = english.read_text()
    chinese.write_text("no markers")
    with pytest.raises(ValueError, match="markers"):
        sync_correction(report, english, chinese)
    assert english.read_text() == before


@pytest.mark.parametrize("gap_multiplier, expected", [(1, "0.8%"), (2, "0.4%")])
def test_encoding_note_uses_aggregate_input_gap(gap_multiplier, expected):
    from agent_rca_bench.formal_report_view import split_rerun_note

    report = rerun.read(ROOT / "artifacts/measurement/agent-rca-v34-six-model.json")
    usage = report["usage_by_treatment"]
    arms = usage["by_treatment"]
    raw = arms["raw"]["provider_visible_input_tokens"]
    split = arms["split_pillars"]["provider_visible_input_tokens"]
    arms["split_pillars"]["provider_visible_input_tokens"] = raw + (split - raw) * gap_multiplier
    note = split_rerun_note(report["split_reruns"], usage)
    assert f"aggregate Split–Raw input gap by about {expected}" in note["en"]
    assert f"总输入差距扩大约 {expected}" in note["zh"]
    assert "对 GreptimeDB 不利" in note["zh"]


def test_baseline_binding_uses_snapshot_and_rejects_snapshot_mutation(tmp_path):
    config = deepcopy(CONFIG)
    for name, paths in config["cohorts"].items():
        snapshot = tmp_path / f"{name}.json"
        snapshot.write_bytes((ROOT / paths["baseline"]).read_bytes())
        paths["baseline_snapshot"] = str(snapshot)
        paths["baseline"] = "artifacts/not-used-at-execution.json"
    manifest = rerun.freeze(config, ROOT)
    rerun.validate_inputs(manifest, config, ROOT)
    snapshot.write_text("changed")
    with pytest.raises(ValueError, match="frozen baseline changed"):
        rerun.validate_inputs(manifest, config, ROOT)
