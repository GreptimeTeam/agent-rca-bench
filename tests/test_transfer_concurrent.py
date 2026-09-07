import time
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Lock, Timer
from types import SimpleNamespace

import pytest

from agent_rca_bench.contracts import DatabaseLoad, Visibility
from agent_rca_bench.transfer_concurrent import (
    TransferRunState,
    _case_execution_order,
    _exclusive_invocation,
    collect_source_audits_concurrently,
    execute_pending_runs_concurrently,
)
from agent_rca_bench.transfer_formal import (
    PreparedTransferEnvironment,
    _failed_run,
    build_preflight_report,
    validate_transfer_run_item,
)
from agent_rca_bench.transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    formal_schedule,
    load_transfer_protocol,
    sha256_file,
)
from agent_rca_bench.transfer_scorer import evaluate_transfer_run


def test_interrupt_drains_active_cells_before_exiting(tmp_path: Path, monkeypatch) -> None:
    import agent_rca_bench.transfer_concurrent as concurrent

    protocol, cohort, report, audits = _preflight()
    started = Event()
    release = Event()
    lock = Lock()
    executing = []
    closed = []

    @contextmanager
    def prepare(_protocol, spec, _config):
        try:
            yield SimpleNamespace(spec=spec, source_audit=_source_audit(spec.opaque_case_id))
        finally:
            closed.append(spec.opaque_case_id)

    def execute(*args, **kwargs):
        with lock:
            executing.append(args[2]["cell_index"])
            if len(executing) == protocol.parallel_runs:
                started.set()
        assert release.wait(5)
        return _failed_item(*args, **kwargs)

    def interrupt(*_args, **_kwargs):
        assert started.wait(5)
        Timer(0.05, release.set).start()
        raise KeyboardInterrupt

    monkeypatch.setattr(concurrent, "wait", interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute_pending_runs_concurrently(
            report,
            protocol,
            DEFAULT_PROTOCOL_FIXTURE,
            cohort,
            cache_dir=tmp_path / "cache",
            manifest_path=tmp_path / "manifest.jsonl",
            greptimedb_repo=tmp_path / "greptimedb",
            run_root=tmp_path / "runs",
            paid_api_confirmed=True,
            prepare_environment_fn=prepare,
            execute_cell_fn=execute,
        )
    state = TransferRunState(
        tmp_path / "runs" / ".transfer-run-state",
        protocol_fixture_sha256=sha256_file(DEFAULT_PROTOCOL_FIXTURE),
        schedule=formal_schedule(protocol, cohort),
    )
    complete = {item["cell_index"] for item in report["runs"]}
    complete.update(state.completed_indexes(report, protocol, cohort))
    assert complete == set(executing)
    assert len(complete) == protocol.parallel_runs
    assert len(closed) == protocol.parallel_runs
    assert not list(state.root.glob("*.active.json"))


def _source_audit(case_id: str) -> dict[str, object]:
    return {
        "case": {"opaque_case_id": case_id},
        "no_model_gates": {"all_passed": True},
    }


def _preflight():
    protocol, cohort = load_transfer_protocol()
    audits = [_source_audit(case.opaque_case_id) for case in cohort.selected_cases]
    report = build_preflight_report(
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        cohort,
        audits,
    )
    return protocol, cohort, report, audits


def _failed_item(protocol, prepared, cell, source_semantic_hash, **_kwargs):
    model = protocol.models[int(cell["model_index"])]
    visibility = Visibility(str(cell["visibility"]))
    run = _failed_run(
        visibility,
        model.model,
        model.api_transport,
        model.reasoning_effort,
        model.max_output_tokens,
        "synthetic provider failure",
    )
    evaluation = evaluate_transfer_run(
        run,
        prepared.spec,
        expected_model=model.model,
        expected_transport=model.api_transport,
        expected_reasoning_effort=model.reasoning_effort,
        expected_max_output_tokens=model.max_output_tokens,
        max_tool_calls=protocol.max_tool_calls,
    )
    return {
        **cell,
        "run": run.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
        "database_load": DatabaseLoad().model_dump(mode="json"),
        "source_semantic_sha256": source_semantic_hash,
    }


def test_case_execution_rotates_models_without_reordering_each_model() -> None:
    protocol, cohort = load_transfer_protocol()
    schedule = formal_schedule(protocol, cohort)
    case_cells = [cell for cell in schedule if cell["case_index"] == 1]

    ordered = _case_execution_order(case_cells, len(protocol.models))

    assert [cell["model_index"] for cell in ordered[:4]] == [1, 2, 3, 0]
    for model_index in range(len(protocol.models)):
        expected = [cell for cell in case_cells if cell["model_index"] == model_index]
        assert [cell for cell in ordered if cell["model_index"] == model_index] == expected


def test_provider_limit_runs_pending_cells_and_preserves_reused_results(
    tmp_path: Path,
) -> None:
    protocol, cohort, report, _audits = _preflight()
    schedule = formal_schedule(protocol, cohort)
    pending = {0, 24, 48, 72}
    specs = {spec.opaque_case_id: spec for spec in cohort.selected_cases}
    run_root = tmp_path / "runs"
    state = TransferRunState(
        run_root / ".transfer-run-state",
        protocol_fixture_sha256=sha256_file(DEFAULT_PROTOCOL_FIXTURE),
        schedule=schedule,
    )
    reused = {}
    for cell in schedule:
        if cell["cell_index"] in pending:
            continue
        item = _failed_item(
            protocol,
            SimpleNamespace(spec=specs[cell["case_id"]]),
            cell,
            source_semantic_hash=report["bindings"]["source_semantic_sha256"][cell["case_id"]],
        )
        state.mark_active(cell)
        state.mark_complete(item)
        reused[cell["cell_index"]] = item
    active = Barrier(2)

    @contextmanager
    def prepare(_protocol, spec, _config):
        yield SimpleNamespace(spec=spec, source_audit=_source_audit(spec.opaque_case_id))

    def execute(*args, **kwargs):
        assert args[2]["cell_index"] in pending
        active.wait(timeout=5)
        return _failed_item(*args, **kwargs)

    execute_pending_runs_concurrently(
        report,
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        cohort,
        cache_dir=tmp_path / "cache",
        manifest_path=tmp_path / "manifest.jsonl",
        greptimedb_repo=tmp_path / "greptimedb",
        run_root=run_root,
        paid_api_confirmed=True,
        provider_concurrency_limit=2,
        prepare_environment_fn=prepare,
        execute_cell_fn=execute,
    )
    assert len(report["runs"]) == len(schedule)
    for item in report["runs"]:
        if item["cell_index"] in pending:
            assert item.get("provider_concurrency_limit", 2) == 2
        else:
            assert item == reused[item["cell_index"]]


@pytest.mark.parametrize("limit", [3, 4, True])
def test_provider_limit_cannot_exceed_frozen_contract(tmp_path, limit):
    protocol, cohort, report, _ = _preflight()
    with pytest.raises(ValueError, match="frozen provider limit"):
        execute_pending_runs_concurrently(
            report,
            protocol,
            DEFAULT_PROTOCOL_FIXTURE,
            cohort,
            cache_dir=tmp_path / "cache",
            manifest_path=tmp_path / "manifest.jsonl",
            greptimedb_repo=tmp_path / "db",
            run_root=tmp_path / "runs",
            paid_api_confirmed=True,
            provider_concurrency_limit=limit,
        )
    assert not (tmp_path / "runs").exists()


def test_historical_concurrency_deviation_requires_audit_mode():
    protocol, cohort, report, _ = _preflight()
    cell = formal_schedule(protocol, cohort)[0]
    spec = cohort.selected_cases[0]
    source_hash = report["bindings"]["source_semantic_sha256"][cell["case_id"]]
    item = _failed_item(
        protocol, SimpleNamespace(spec=spec), cell, source_semantic_hash=source_hash
    )
    item["provider_concurrency_limit"] = 4
    with pytest.raises(ValueError, match="concurrency"):
        validate_transfer_run_item(item, cell, protocol, spec, source_hash)
    validate_transfer_run_item(
        item, cell, protocol, spec, source_hash, allow_recorded_concurrency_deviation=True
    )


def test_preflight_uses_the_paid_runner_environment_concurrency(tmp_path: Path) -> None:
    protocol, cohort = load_transfer_protocol()
    lock = Lock()
    preparing = 0
    peak_preparing = 0
    live = 0
    peak_live = 0

    @contextmanager
    def prepare(_protocol, spec, _config):
        nonlocal preparing, peak_preparing, live, peak_live
        with lock:
            preparing += 1
            peak_preparing = max(peak_preparing, preparing)
        time.sleep(0.01)
        with lock:
            preparing -= 1
            live += 1
            peak_live = max(peak_live, live)
        try:
            yield SimpleNamespace(source_audit=_source_audit(spec.opaque_case_id))
        finally:
            with lock:
                live -= 1

    audits = collect_source_audits_concurrently(
        protocol,
        cohort,
        cache_dir=tmp_path / "cache",
        manifest_path=tmp_path / "manifest.jsonl",
        greptimedb_repo=tmp_path / "greptimedb",
        run_root=tmp_path / "runs",
        prepare_environment_fn=prepare,
    )

    assert [audit["case"]["opaque_case_id"] for audit in audits] == [
        spec.opaque_case_id for spec in cohort.selected_cases
    ]
    assert peak_preparing == protocol.max_parallel_environment_preparations
    assert peak_live == protocol.parallel_runs


def test_concurrent_runner_bounds_local_and_same_provider_work(tmp_path: Path) -> None:
    protocol, cohort, report, audits = _preflight()
    lock = Lock()
    first_cells = Barrier(2)
    preparing = 0
    peak_preparing = 0
    active = 0
    peak_active = 0
    active_by_case: dict[str, int] = {}
    peak_by_case: dict[str, int] = {}
    active_by_provider: dict[str, int] = {}
    peak_by_provider: dict[str, int] = {}
    seen_cases: set[str] = set()
    preparation_overlapped_execution = False

    @contextmanager
    def prepare(_protocol, spec, _config):
        nonlocal preparing, peak_preparing
        with lock:
            preparing += 1
            peak_preparing = max(peak_preparing, preparing)
        time.sleep(0.01)
        with lock:
            preparing -= 1
        yield PreparedTransferEnvironment(
            client=SimpleNamespace(),
            case=SimpleNamespace(input=SimpleNamespace()),
            spec=spec,
            source_audit=next(
                audit for audit in audits if audit["case"]["opaque_case_id"] == spec.opaque_case_id
            ),
            semantic_coverage={},
            graph_window=(0, 1),
            split_stack=SimpleNamespace(),
        )

    def execute(protocol, prepared, cell, **kwargs):
        nonlocal active, peak_active, preparation_overlapped_execution
        case_id = str(cell["case_id"])
        provider = str(cell["provider"])
        with lock:
            preparation_overlapped_execution |= preparing > 0
            active += 1
            peak_active = max(peak_active, active)
            active_by_case[case_id] = active_by_case.get(case_id, 0) + 1
            peak_by_case[case_id] = max(peak_by_case.get(case_id, 0), active_by_case[case_id])
            active_by_provider[provider] = active_by_provider.get(provider, 0) + 1
            peak_by_provider[provider] = max(
                peak_by_provider.get(provider, 0), active_by_provider[provider]
            )
            first_for_case = case_id not in seen_cases
            seen_cases.add(case_id)
        if first_for_case:
            first_cells.wait(timeout=2)
            if int(cell["case_index"]) == 1:
                # The first case must merge while another worker still has a
                # legitimate active journal. Only startup recovery may treat
                # active journals as evidence of an interrupted invocation.
                time.sleep(0.05)
        try:
            return _failed_item(protocol, prepared, cell, **kwargs)
        finally:
            with lock:
                active -= 1
                active_by_case[case_id] -= 1
                active_by_provider[provider] -= 1

    execute_pending_runs_concurrently(
        report,
        protocol,
        DEFAULT_PROTOCOL_FIXTURE,
        cohort,
        cache_dir=tmp_path / "cache",
        manifest_path=tmp_path / "manifest.jsonl",
        greptimedb_repo=tmp_path / "greptimedb",
        run_root=tmp_path / "runs",
        paid_api_confirmed=True,
        max_new_runs=25,
        prepare_environment_fn=prepare,
        execute_cell_fn=execute,
    )

    assert [item["cell_index"] for item in report["runs"]] == list(range(25))
    assert peak_preparing == protocol.max_parallel_environment_preparations
    assert preparation_overlapped_execution is False
    assert peak_active <= protocol.parallel_runs
    assert max(peak_by_case.values()) == 1
    assert peak_by_provider[protocol.models[0].provider] == 2
    assert not list((tmp_path / "runs" / ".transfer-run-state").glob("*.json"))


def test_concurrent_runner_refuses_ambiguous_paid_cell(tmp_path: Path) -> None:
    protocol, cohort, report, _audits = _preflight()
    schedule = formal_schedule(protocol, cohort)
    run_root = tmp_path / "runs"
    state = TransferRunState(
        run_root / ".transfer-run-state",
        protocol_fixture_sha256=sha256_file(DEFAULT_PROTOCOL_FIXTURE),
        schedule=schedule,
    )
    state.mark_active(schedule[0])

    with pytest.raises(ValueError, match="refusing automatic retry"):
        execute_pending_runs_concurrently(
            report,
            protocol,
            DEFAULT_PROTOCOL_FIXTURE,
            cohort,
            cache_dir=tmp_path / "cache",
            manifest_path=tmp_path / "manifest.jsonl",
            greptimedb_repo=tmp_path / "greptimedb",
            run_root=run_root,
            paid_api_confirmed=True,
            prepare_environment_fn=lambda *_args: pytest.fail("must not prepare an environment"),
        )


def test_unconfirmed_run_does_not_create_its_run_root(tmp_path: Path) -> None:
    protocol, cohort, report, _audits = _preflight()
    run_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="has not been explicitly confirmed"):
        execute_pending_runs_concurrently(
            report,
            protocol,
            DEFAULT_PROTOCOL_FIXTURE,
            cohort,
            cache_dir=tmp_path / "cache",
            manifest_path=tmp_path / "manifest.jsonl",
            greptimedb_repo=tmp_path / "greptimedb",
            run_root=run_root,
            paid_api_confirmed=False,
        )

    assert not run_root.exists()


def test_environment_error_stops_other_cases_after_their_active_cell(tmp_path: Path) -> None:
    protocol, cohort, report, audits = _preflight()
    lock = Lock()
    calls: dict[str, int] = {}
    live = 0

    @contextmanager
    def prepare(_protocol, spec, _config):
        nonlocal live
        with lock:
            live += 1
        try:
            yield PreparedTransferEnvironment(
                client=SimpleNamespace(),
                case=SimpleNamespace(input=SimpleNamespace()),
                spec=spec,
                source_audit=next(
                    audit
                    for audit in audits
                    if audit["case"]["opaque_case_id"] == spec.opaque_case_id
                ),
                semantic_coverage={},
                graph_window=(0, 1),
                split_stack=SimpleNamespace(),
            )
        finally:
            with lock:
                live -= 1

    def execute(protocol, prepared, cell, **kwargs):
        case_id = str(cell["case_id"])
        with lock:
            calls[case_id] = calls.get(case_id, 0) + 1
        if int(cell["case_index"]) == 0:
            raise RuntimeError("synthetic orchestration failure")
        time.sleep(0.05)
        return _failed_item(protocol, prepared, cell, **kwargs)

    with pytest.raises(RuntimeError, match="synthetic orchestration failure"):
        execute_pending_runs_concurrently(
            report,
            protocol,
            DEFAULT_PROTOCOL_FIXTURE,
            cohort,
            cache_dir=tmp_path / "cache",
            manifest_path=tmp_path / "manifest.jsonl",
            greptimedb_repo=tmp_path / "greptimedb",
            run_root=tmp_path / "runs",
            paid_api_confirmed=True,
            max_new_runs=25,
            prepare_environment_fn=prepare,
            execute_cell_fn=execute,
        )

    assert calls == {
        cohort.selected_cases[0].opaque_case_id: 1,
        cohort.selected_cases[1].opaque_case_id: 1,
    }
    assert live == 0


def test_completed_journal_wins_over_an_active_journal_after_a_crash(tmp_path: Path) -> None:
    protocol, cohort, report, audits = _preflight()
    schedule = formal_schedule(protocol, cohort)
    state = TransferRunState(
        tmp_path / "state",
        protocol_fixture_sha256=sha256_file(DEFAULT_PROTOCOL_FIXTURE),
        schedule=schedule,
    )
    prepared = PreparedTransferEnvironment(
        client=SimpleNamespace(),
        case=SimpleNamespace(input=SimpleNamespace()),
        spec=cohort.selected_cases[0],
        source_audit=audits[0],
        semantic_coverage={},
        graph_window=(0, 1),
        split_stack=SimpleNamespace(),
    )
    source_hash = report["bindings"]["source_semantic_sha256"][schedule[0]["case_id"]]
    state.mark_active(schedule[0])
    active_payload = (tmp_path / "state" / "cell-000000.active.json").read_text()
    item = _failed_item(protocol, prepared, schedule[0], source_hash)
    state.mark_complete(item)
    (tmp_path / "state" / "cell-000000.active.json").write_text(active_payload)

    state.reconcile(
        report,
        protocol,
        cohort,
        None,
        reject_ambiguous_active=True,
    )

    assert report["runs"] == [item]
    assert not list((tmp_path / "state").glob("cell-*.json"))


def test_concurrent_runner_refuses_a_second_process_owner(tmp_path: Path) -> None:
    protocol, cohort, report, _audits = _preflight()
    run_root = tmp_path / "runs"

    with (
        _exclusive_invocation(run_root),
        pytest.raises(ValueError, match="another transfer runner owns"),
    ):
        execute_pending_runs_concurrently(
            report,
            protocol,
            DEFAULT_PROTOCOL_FIXTURE,
            cohort,
            cache_dir=tmp_path / "cache",
            manifest_path=tmp_path / "manifest.jsonl",
            greptimedb_repo=tmp_path / "greptimedb",
            run_root=run_root,
            paid_api_confirmed=True,
            prepare_environment_fn=lambda *_args: pytest.fail("must not prepare an environment"),
        )
