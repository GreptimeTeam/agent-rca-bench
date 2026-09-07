from __future__ import annotations

import fcntl
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, BoundedSemaphore, BrokenBarrierError, Event, Lock

from agent_rca_bench.agent import run_agent, run_split_agent
from agent_rca_bench.greptimedb.server import write_json
from agent_rca_bench.transfer_formal import (
    PreparedTransferEnvironment,
    RunAgent,
    TransferEnvironmentConfig,
    _execution_summary,
    execute_transfer_cell,
    prepare_transfer_environment,
    source_semantic_sha256,
    validate_private_report,
    validate_transfer_run_item,
)
from agent_rca_bench.transfer_protocol import (
    TransferCohort,
    TransferProtocolFixture,
    formal_schedule,
    sha256_file,
)

STATE_SCHEMA_VERSION = 1

PrepareEnvironment = Callable[
    [TransferProtocolFixture, object, TransferEnvironmentConfig],
    AbstractContextManager[PreparedTransferEnvironment],
]
ExecuteCell = Callable[..., dict[str, object]]
ReportUpdate = Callable[[dict[str, object]], None]


class TransferRunState:
    def __init__(
        self,
        root: Path,
        *,
        protocol_fixture_sha256: str,
        schedule: list[dict[str, object]],
    ) -> None:
        self.root = root
        self.protocol_fixture_sha256 = protocol_fixture_sha256
        self.schedule = schedule
        self.root.mkdir(parents=True, exist_ok=True)

    def reconcile(
        self,
        report: dict[str, object],
        protocol: TransferProtocolFixture,
        cohort: TransferCohort,
        on_update: ReportUpdate | None,
        *,
        reject_ambiguous_active: bool,
    ) -> None:
        runs = _report_runs(report)
        completed = self._completed_items(report, protocol, cohort)
        if reject_ambiguous_active:
            ambiguous = []
            for path in sorted(self.root.glob("cell-*.active.json")):
                index, payload = self._load(path, "active")
                if payload.get("cell") != self.schedule[index]:
                    raise ValueError(f"transfer active journal cell drifted: {path}")
                if index < len(runs) or index in completed:
                    path.unlink()
                else:
                    ambiguous.append(index)
            if ambiguous:
                cells = ", ".join(str(index) for index in ambiguous)
                raise ValueError(
                    "previous invocation stopped during paid transfer cells "
                    f"{cells}; refusing automatic retry"
                )
        merged_paths = []
        while len(runs) in completed:
            index = len(runs)
            runs.append(completed[index])
            merged_paths.append(self._complete_path(index))
        if merged_paths:
            report["execution"] = _execution_summary(runs, len(self.schedule))
            if on_update is not None:
                on_update(report)
            for path in merged_paths:
                path.unlink()

    def completed_indexes(
        self,
        report: dict[str, object],
        protocol: TransferProtocolFixture,
        cohort: TransferCohort,
    ) -> set[int]:
        return set(self._completed_items(report, protocol, cohort))

    def mark_active(self, cell: Mapping[str, object]) -> None:
        index = int(cell["cell_index"])
        active = self._active_path(index)
        complete = self._complete_path(index)
        if active.exists() or complete.exists():
            raise ValueError(f"transfer cell {index} already has run state")
        write_json(
            active,
            {
                "state_schema_version": STATE_SCHEMA_VERSION,
                "protocol_fixture_sha256": self.protocol_fixture_sha256,
                "cell": dict(cell),
                "started_at": datetime.now(UTC).isoformat(),
            },
        )

    def mark_complete(self, item: Mapping[str, object]) -> None:
        index = int(item["cell_index"])
        active = self._active_path(index)
        if not active.is_file():
            raise ValueError(f"transfer cell {index} has no active journal")
        write_json(
            self._complete_path(index),
            {
                "state_schema_version": STATE_SCHEMA_VERSION,
                "protocol_fixture_sha256": self.protocol_fixture_sha256,
                "item": dict(item),
            },
        )
        active.unlink()

    def _completed_items(
        self,
        report: dict[str, object],
        protocol: TransferProtocolFixture,
        cohort: TransferCohort,
    ) -> dict[int, dict[str, object]]:
        runs = _report_runs(report)
        specs = {case.opaque_case_id: case for case in cohort.selected_cases}
        source_hashes = _source_hashes(report)
        completed: dict[int, dict[str, object]] = {}
        for path in sorted(self.root.glob("cell-*.complete.json")):
            index, payload = self._load(path, "complete")
            item = payload.get("item")
            if not isinstance(item, Mapping):
                raise ValueError(f"transfer complete journal is malformed: {path}")
            item = dict(item)
            expected = self.schedule[index]
            case_id = str(expected["case_id"])
            validate_transfer_run_item(
                item,
                expected,
                protocol,
                specs[case_id],
                source_hashes[case_id],
            )
            if index < len(runs):
                if item != runs[index]:
                    raise ValueError(f"transfer complete journal conflicts with report: {path}")
                path.unlink()
                continue
            completed[index] = item
        return completed

    def _load(self, path: Path, kind: str) -> tuple[int, dict[str, object]]:
        try:
            index = int(path.name.removeprefix("cell-").removesuffix(f".{kind}.json"))
            payload = json.loads(path.read_text())
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid transfer run journal: {path}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("state_schema_version") != STATE_SCHEMA_VERSION
            or payload.get("protocol_fixture_sha256") != self.protocol_fixture_sha256
            or not 0 <= index < len(self.schedule)
        ):
            raise ValueError(f"transfer run journal binding drifted: {path}")
        return index, payload

    def _active_path(self, index: int) -> Path:
        return self.root / f"cell-{index:06d}.active.json"

    def _complete_path(self, index: int) -> Path:
        return self.root / f"cell-{index:06d}.complete.json"


def collect_source_audits_concurrently(
    protocol: TransferProtocolFixture,
    cohort: TransferCohort,
    *,
    cache_dir: Path,
    manifest_path: Path,
    greptimedb_repo: Path,
    run_root: Path,
    node_cache_dir: Path = Path(".data/rca100"),
    prepare_environment_fn: PrepareEnvironment = prepare_transfer_environment,
) -> list[dict[str, object]]:
    specs = list(cohort.selected_cases)
    audits: list[dict[str, object] | None] = [None] * len(specs)
    abort = Event()
    preparation_slots = BoundedSemaphore(protocol.max_parallel_environment_preparations)

    def inspect_case(
        index: int,
        spec: object,
        batch_ready: Barrier,
    ) -> tuple[int, dict[str, object]] | None:
        stack = ExitStack()
        source_audit: dict[str, object] | None = None
        try:
            preparation_slots.acquire()
            try:
                if abort.is_set():
                    return None
                prepared = stack.enter_context(
                    prepare_environment_fn(
                        protocol,
                        spec,
                        TransferEnvironmentConfig(
                            cache_dir=cache_dir,
                            manifest_path=manifest_path,
                            greptimedb_repo=greptimedb_repo,
                            run_dir=run_root / str(spec.opaque_case_id),
                            database=str(spec.opaque_case_id).replace("-", "_"),
                            node_cache_dir=node_cache_dir,
                        ),
                    )
                )
            except BaseException:
                abort.set()
                batch_ready.abort()
                raise
            finally:
                preparation_slots.release()
            source_audit = prepared.source_audit
            try:
                batch_ready.wait()
            except BrokenBarrierError:
                if abort.is_set():
                    return None
                raise
        finally:
            stack.close()
        if source_audit is None:
            raise ValueError("transfer no-model preflight produced no source audit")
        return index, source_audit

    for offset in range(0, len(specs), protocol.parallel_runs):
        batch = list(enumerate(specs[offset : offset + protocol.parallel_runs], start=offset))
        batch_ready = Barrier(len(batch))
        first_error: BaseException | None = None
        try:
            with ThreadPoolExecutor(
                max_workers=len(batch),
                thread_name_prefix="transfer-preflight",
            ) as executor:
                active = {
                    executor.submit(inspect_case, index, spec, batch_ready) for index, spec in batch
                }
                try:
                    while active:
                        done, active = wait(active, return_when=FIRST_COMPLETED)
                        for future in done:
                            try:
                                result = future.result()
                            except Exception as error:
                                abort.set()
                                batch_ready.abort()
                                if first_error is None:
                                    first_error = error
                            else:
                                if result is not None:
                                    index, audit = result
                                    audits[index] = audit
                except BaseException:
                    abort.set()
                    batch_ready.abort()
                    raise
        except BaseException:
            abort.set()
            batch_ready.abort()
            raise
        if first_error is not None:
            raise first_error

    if not all(isinstance(audit, dict) for audit in audits):
        raise ValueError("transfer no-model preflight did not complete every source audit")
    return [audit for audit in audits if audit is not None]


def execute_pending_runs_concurrently(
    report: dict[str, object],
    protocol: TransferProtocolFixture,
    protocol_path: Path,
    cohort: TransferCohort,
    *,
    cache_dir: Path,
    manifest_path: Path,
    greptimedb_repo: Path,
    run_root: Path,
    node_cache_dir: Path = Path(".data/rca100"),
    paid_api_confirmed: bool,
    max_new_runs: int | None = None,
    provider_concurrency_limit: int | None = None,
    run_agent_fn: RunAgent = run_agent,
    run_split_agent_fn: RunAgent = run_split_agent,
    prepare_environment_fn: PrepareEnvironment = prepare_transfer_environment,
    execute_cell_fn: ExecuteCell = execute_transfer_cell,
    on_update: ReportUpdate | None = None,
) -> dict[str, object]:
    validate_private_report(report, protocol, protocol_path, cohort)
    if paid_api_confirmed is not True:
        raise ValueError("transfer paid API execution has not been explicitly confirmed")
    if max_new_runs is not None and max_new_runs < 1:
        raise ValueError("max_new_runs must be positive")
    if provider_concurrency_limit is not None and not (
        type(provider_concurrency_limit) is int
        and 1 <= provider_concurrency_limit <= protocol.max_parallel_runs_per_provider
    ):
        raise ValueError("provider concurrency must be between 1 and the frozen provider limit")
    with _exclusive_invocation(run_root):
        return _execute_pending_runs_concurrently_locked(
            report,
            protocol,
            protocol_path,
            cohort,
            cache_dir=cache_dir,
            manifest_path=manifest_path,
            greptimedb_repo=greptimedb_repo,
            run_root=run_root,
            node_cache_dir=node_cache_dir,
            paid_api_confirmed=paid_api_confirmed,
            max_new_runs=max_new_runs,
            provider_concurrency_limit=provider_concurrency_limit,
            run_agent_fn=run_agent_fn,
            run_split_agent_fn=run_split_agent_fn,
            prepare_environment_fn=prepare_environment_fn,
            execute_cell_fn=execute_cell_fn,
            on_update=on_update,
        )


def _execute_pending_runs_concurrently_locked(
    report: dict[str, object],
    protocol: TransferProtocolFixture,
    protocol_path: Path,
    cohort: TransferCohort,
    *,
    cache_dir: Path,
    manifest_path: Path,
    greptimedb_repo: Path,
    run_root: Path,
    node_cache_dir: Path,
    paid_api_confirmed: bool,
    max_new_runs: int | None,
    provider_concurrency_limit: int | None,
    run_agent_fn: RunAgent,
    run_split_agent_fn: RunAgent,
    prepare_environment_fn: PrepareEnvironment,
    execute_cell_fn: ExecuteCell,
    on_update: ReportUpdate | None,
) -> dict[str, object]:
    schedule = formal_schedule(protocol, cohort)
    state = TransferRunState(
        run_root / ".transfer-run-state",
        protocol_fixture_sha256=sha256_file(protocol_path),
        schedule=schedule,
    )
    report_lock = Lock()

    def reconcile(*, reject_ambiguous_active: bool) -> None:
        with report_lock:
            state.reconcile(
                report,
                protocol,
                cohort,
                on_update,
                reject_ambiguous_active=reject_ambiguous_active,
            )

    reconcile(reject_ambiguous_active=True)
    completed = {int(item["cell_index"]) for item in _report_runs(report)}
    completed.update(state.completed_indexes(report, protocol, cohort))
    pending = [cell for cell in schedule if int(cell["cell_index"]) not in completed]
    if max_new_runs is not None:
        pending = pending[:max_new_runs]
    if not pending:
        return report

    jobs: dict[str, list[dict[str, object]]] = defaultdict(list)
    for cell in pending:
        jobs[str(cell["case_id"])].append(cell)
    specs = {case.opaque_case_id: case for case in cohort.selected_cases}
    source_hashes = _source_hashes(report)
    abort = Event()
    preparation_slots = BoundedSemaphore(protocol.max_parallel_environment_preparations)
    effective_provider_limit = provider_concurrency_limit or protocol.max_parallel_runs_per_provider
    provider_slots = {
        model.provider: BoundedSemaphore(effective_provider_limit) for model in protocol.models
    }

    def run_case(
        case_id: str,
        cells: list[dict[str, object]],
        batch_ready: Barrier,
    ) -> None:
        if abort.is_set():
            return
        spec = specs[case_id]
        stack = ExitStack()
        try:
            preparation_slots.acquire()
            try:
                if abort.is_set():
                    return
                prepared = stack.enter_context(
                    prepare_environment_fn(
                        protocol,
                        spec,
                        TransferEnvironmentConfig(
                            cache_dir=cache_dir,
                            manifest_path=manifest_path,
                            greptimedb_repo=greptimedb_repo,
                            run_dir=_next_attempt_dir(run_root / case_id),
                            database=case_id.replace("-", "_"),
                            node_cache_dir=node_cache_dir,
                        ),
                    )
                )
            except BaseException:
                abort.set()
                batch_ready.abort()
                raise
            finally:
                preparation_slots.release()
            expected_hash = source_hashes[case_id]
            if source_semantic_sha256(prepared.source_audit) != expected_hash:
                abort.set()
                batch_ready.abort()
                raise ValueError("live transfer environment differs from preflight")
            try:
                batch_ready.wait()
            except BrokenBarrierError:
                if abort.is_set():
                    return
                raise
            for cell in _case_execution_order(cells, len(protocol.models)):
                if abort.is_set():
                    return
                provider_slot = provider_slots[str(cell["provider"])]
                provider_slot.acquire()
                try:
                    if abort.is_set():
                        return
                    state.mark_active(cell)
                    item = execute_cell_fn(
                        protocol,
                        prepared,
                        cell,
                        source_semantic_hash=expected_hash,
                        run_agent_fn=run_agent_fn,
                        run_split_agent_fn=run_split_agent_fn,
                    )
                    if effective_provider_limit != protocol.max_parallel_runs_per_provider:
                        item["provider_concurrency_limit"] = effective_provider_limit
                    state.mark_complete(item)
                    reconcile(reject_ambiguous_active=False)
                finally:
                    provider_slot.release()
        finally:
            stack.close()

    job_items = list(jobs.items())
    first_error: BaseException | None = None
    for offset in range(0, len(job_items), protocol.parallel_runs):
        batch = job_items[offset : offset + protocol.parallel_runs]
        batch_ready = Barrier(len(batch))
        try:
            with ThreadPoolExecutor(
                max_workers=len(batch),
                thread_name_prefix="transfer-case",
            ) as executor:
                active: set[Future[None]] = {
                    executor.submit(run_case, case_id, cells, batch_ready)
                    for case_id, cells in batch
                }
                try:
                    while active:
                        done, active = wait(active, return_when=FIRST_COMPLETED)
                        for future in done:
                            try:
                                future.result()
                            except Exception as error:
                                abort.set()
                                batch_ready.abort()
                                if first_error is None:
                                    first_error = error
                except BaseException:
                    abort.set()
                    batch_ready.abort()
                    raise
        except BaseException:
            abort.set()
            batch_ready.abort()
            raise
        if first_error is not None:
            break
    reconcile(reject_ambiguous_active=False)
    if first_error is not None:
        raise first_error
    validate_private_report(report, protocol, protocol_path, cohort)
    return report


@contextmanager
def _exclusive_invocation(run_root: Path):
    run_root.mkdir(parents=True, exist_ok=True)
    path = run_root / ".transfer-run.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"another transfer runner owns {run_root}") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _case_execution_order(
    cells: list[dict[str, object]],
    model_count: int,
) -> list[dict[str, object]]:
    if not cells:
        return []
    case_index = int(cells[0]["case_index"])
    by_model: dict[int, list[dict[str, object]]] = defaultdict(list)
    for cell in cells:
        by_model[int(cell["model_index"])].append(cell)
    model_order = [
        (case_index + offset) % model_count
        for offset in range(model_count)
        if (case_index + offset) % model_count in by_model
    ]
    ordered: list[dict[str, object]] = []
    while any(by_model.values()):
        for model_index in model_order:
            if by_model[model_index]:
                ordered.append(by_model[model_index].pop(0))
    return ordered


def _next_attempt_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    attempts = [
        int(path.name.removeprefix("attempt-"))
        for path in root.glob("attempt-*")
        if path.is_dir() and path.name.removeprefix("attempt-").isdigit()
    ]
    return root / f"attempt-{max(attempts, default=0) + 1:03d}"


def _report_runs(report: Mapping[str, object]) -> list[dict[str, object]]:
    runs = report.get("runs")
    if not isinstance(runs, list) or not all(isinstance(item, dict) for item in runs):
        raise ValueError("transfer report runs are malformed")
    return runs


def _source_hashes(report: Mapping[str, object]) -> dict[str, str]:
    bindings = report.get("bindings")
    hashes = bindings.get("source_semantic_sha256") if isinstance(bindings, Mapping) else None
    if not isinstance(hashes, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in hashes.items()
    ):
        raise ValueError("transfer report source bindings are malformed")
    return dict(hashes)
