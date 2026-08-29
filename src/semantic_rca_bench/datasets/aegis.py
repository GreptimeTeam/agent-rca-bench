from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from semantic_rca_bench.selection import deterministic_rank

ARTIFACT_RECORD = "https://zenodo.org/records/19522409"
ARTIFACT_FILENAME = "FSE_26_RCA_dataset_study_reviewer.tar.gz"
ARTIFACT_MD5 = "16f0743b6feb20838f856d6e441e0c7b"
ARTIFACT_SIZE = 91_418_171
ARTIFACT_URL = (
    "https://zenodo.org/api/records/19522409/files/FSE_26_RCA_dataset_study_reviewer.tar.gz/content"
)
SOURCE_DATASET_RECORD = "https://zenodo.org/records/17105974"
SELECTION_SEED = "semantic-rca-v1-aegis-transfer"
TRANSFER_AGENT_CASE_ID = "aegis-transfer-001"
_ARCHIVE_DATASET_PREFIX = (
    "FSE_26_RCA_dataset_study_artifact_clean/reproduction/data/rcabench-platform-v2/"
)

_PERIODS = ("normal", "abnormal")
_TRACE_COLUMNS = (
    "time",
    "trace_id",
    "span_id",
    "parent_span_id",
    "span_name",
    "attr.span_kind",
    "service_name",
    "duration",
    "attr.status_code",
    "attr.http.request.content_length",
    "attr.http.response.content_length",
    "attr.http.request.method",
    "attr.http.response.status_code",
)
_REQUIRED_TRACE_COLUMNS = frozenset(
    {
        "trace_id",
        "span_id",
        "parent_span_id",
        "span_name",
        "attr.span_kind",
        "service_name",
        "duration",
        "attr.status_code",
    }
)
_SPAN_KINDS = {"Unspecified", "Internal", "Server", "Client", "Producer", "Consumer"}
_STATUS_CODES = {"Unset", "Ok", "Error"}


class AegisAuditError(RuntimeError):
    pass


class AegisRepository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def fetch(self) -> tuple[Path, Path]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        archive = self.cache_dir / ARTIFACT_FILENAME
        self._download(archive)
        _verify_archive(archive)
        dataset_root = self.cache_dir / "rcabench-platform-v2"
        _extract_dataset(archive, dataset_root)
        cases_dir = dataset_root / "data" / "rcabench"
        meta_dir = dataset_root / "meta" / "rcabench"
        return cases_dir, meta_dir

    @staticmethod
    def _download(target: Path) -> None:
        if target.is_file():
            _verify_archive(target)
            return
        partial = target.with_suffix(f"{target.suffix}.part")
        with (
            httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(600, connect=30),
                trust_env=False,
            ) as client,
            client.stream("GET", ARTIFACT_URL) as response,
        ):
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        try:
            _verify_archive(partial)
        except AegisAuditError:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(target)


def audit_cohort(
    cases_dir: Path,
    meta_dir: Path,
    *,
    archive_checksum_verified: bool = False,
    selection_path: Path | None = None,
) -> dict[str, object]:
    index = _meta_rows(meta_dir / "index.parquet")
    attributes = _rows_by_case(_meta_rows(meta_dir / "attributes.parquet"))
    labels = _labels_by_case(_meta_rows(meta_dir / "labels.parquet"))
    case_names = [str(row["datapack"]) for row in index if row.get("dataset") == "rcabench"]
    if not case_names or len(case_names) != len(set(case_names)):
        raise AegisAuditError("publisher index must contain unique rcabench cases")
    if set(attributes) != set(case_names) or set(labels) != set(case_names):
        raise AegisAuditError("publisher index, attributes, and labels disagree")

    cases = [
        _audit_case(cases_dir / case_name, attributes[case_name], labels[case_name])
        for case_name in case_names
    ]
    candidates = [
        str(case["source_case"]) for case in cases if case["directed_graph_candidate"] is True
    ]
    ranked = deterministic_rank(candidates, SELECTION_SEED) if candidates else []
    cases_by_name = {str(case["source_case"]): case for case in cases}
    selected = next(
        (
            case_name
            for case_name in ranked
            if cases_by_name[case_name]["mechanism_evidence"]["predicate_match"] is True
        ),
        None,
    )
    consumed = ranked[: ranked.index(selected) + 1] if selected is not None else ranked
    audit = {
        "source": {
            "artifact_record": ARTIFACT_RECORD,
            "artifact_filename": ARTIFACT_FILENAME,
            "expected_artifact_md5": ARTIFACT_MD5,
            "archive_checksum_verified": archive_checksum_verified,
            "source_dataset_record": SOURCE_DATASET_RECORD,
            "data_redistributed": False,
        },
        "selection": {
            "seed": SELECTION_SEED,
            "eligible_set": sorted(candidates),
            "ranked_candidates": ranked,
            "consumed_candidates": consumed,
            "selected_case": selected,
        },
        "observable_mechanism_selection": {
            "seed": SELECTION_SEED,
            "eligible_set": sorted(
                str(case["source_case"])
                for case in cases
                if case["graph_source_eligible"] is True
                and case["mechanism_evidence"]["predicate_match"] is True
            ),
        },
        "cases": cases,
    }
    observable = audit["observable_mechanism_selection"]
    observable["ranked_candidates"] = (
        deterministic_rank(observable["eligible_set"], SELECTION_SEED)
        if observable["eligible_set"]
        else []
    )
    if selection_path is not None:
        audit["frozen_selection_gate"] = _validate_frozen_selection(audit, selection_path)
    return audit


def _validate_frozen_selection(audit: dict[str, object], selection_path: Path) -> dict[str, object]:
    manifest = json.loads(selection_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise AegisAuditError("frozen selection manifest must be an object")
    if manifest.get("selection_strategy") == "next-unconsumed-from-parent-v1":
        return _validate_sequential_selection(audit, selection_path, manifest)
    if manifest.get("selection_strategy") == "consumed-v25-case-development-recalibration-v1":
        return _validate_recalibration_selection(audit, selection_path, manifest)
    if manifest.get("selection_strategy") == "fresh-source-observable-supported-mechanism-v2":
        return _validate_fresh_observable_selection(audit, selection_path, manifest)
    selection = audit["selection"]
    if not isinstance(selection, dict):
        raise AegisAuditError("source audit selection must be an object")
    source = audit["source"]
    if not isinstance(source, dict):
        raise AegisAuditError("source audit provenance must be an object")
    frozen_source = manifest.get("source")
    eligibility = manifest.get("eligibility")
    selected_case = manifest.get("selected_case")
    if (
        not isinstance(frozen_source, dict)
        or not isinstance(eligibility, dict)
        or not isinstance(selected_case, dict)
    ):
        raise AegisAuditError("frozen selection manifest is incomplete")

    consumed = eligibility.get("consumed_candidates")
    if not isinstance(consumed, list) or any(not isinstance(item, dict) for item in consumed):
        raise AegisAuditError("frozen consumed candidates must be objects")
    consumed_projection = [
        {"source_case": item.get("source_case"), "decision": item.get("decision")}
        for item in consumed
    ]
    observed_consumed = [
        {
            "source_case": source_case,
            "decision": "selected" if source_case == selection["selected_case"] else "rejected",
        }
        for source_case in selection["consumed_candidates"]
    ]
    ranked = selection["ranked_candidates"]
    consumed_names = selection["consumed_candidates"]
    observed = {
        "source": {
            "artifact_record": source["artifact_record"],
            "artifact_filename": source["artifact_filename"],
            "artifact_md5": source["expected_artifact_md5"],
            "source_dataset_record": source["source_dataset_record"],
            "data_redistributed": source["data_redistributed"],
        },
        "selection_seed": selection["seed"],
        "agent_case_id": TRANSFER_AGENT_CASE_ID,
        "eligible_directed_graph_cases": selection["eligible_set"],
        "ranked_candidates": ranked,
        "consumed_candidates": observed_consumed,
        "unconsumed_candidates": ranked[len(consumed_names) :],
        "selected_case": selection["selected_case"],
    }
    expected = {
        "source": {
            key: frozen_source.get(key)
            for key in (
                "artifact_record",
                "artifact_filename",
                "artifact_md5",
                "source_dataset_record",
                "data_redistributed",
            )
        },
        "selection_seed": manifest.get("selection_seed"),
        "agent_case_id": manifest.get("agent_case_id"),
        "eligible_directed_graph_cases": eligibility.get("eligible_directed_graph_cases"),
        "ranked_candidates": eligibility.get("ranked_candidates"),
        "consumed_candidates": consumed_projection,
        "unconsumed_candidates": eligibility.get("unconsumed_candidates"),
        "selected_case": selected_case.get("source_case"),
    }
    if observed != expected:
        mismatches = sorted(key for key in expected if observed[key] != expected[key])
        raise AegisAuditError(f"frozen selection drift: {mismatches}")
    return {
        "manifest_name": selection_path.name,
        "observed": observed,
        "expected": expected,
        "pass": True,
    }


def _validate_sequential_selection(
    audit: dict[str, object],
    selection_path: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    parent_name = manifest.get("parent_manifest")
    parent_sha256 = manifest.get("parent_manifest_sha256")
    if not isinstance(parent_name, str) or Path(parent_name).name != parent_name:
        raise AegisAuditError("sequential selection has an invalid parent manifest name")
    parent_path = selection_path.parent / parent_name
    if not parent_path.is_file() or not isinstance(parent_sha256, str):
        raise AegisAuditError("sequential selection parent manifest is missing")
    observed_parent_sha256 = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    if observed_parent_sha256 != parent_sha256:
        raise AegisAuditError("sequential selection parent manifest checksum drifted")
    parent = _read_json(parent_path)
    parent_eligibility = parent.get("eligibility")
    if not isinstance(parent_eligibility, dict):
        raise AegisAuditError("sequential selection parent eligibility is missing")
    unconsumed = parent_eligibility.get("unconsumed_candidates")
    selected = manifest.get("selected_case")
    if (
        not isinstance(unconsumed, list)
        or not unconsumed
        or not isinstance(selected, dict)
        or selected.get("source_case") != unconsumed[0]
    ):
        raise AegisAuditError("sequential selection is not the first parent-unconsumed candidate")

    selection = audit.get("selection")
    source = audit.get("source")
    cases = audit.get("cases")
    if (
        not isinstance(selection, dict)
        or not isinstance(source, dict)
        or not isinstance(cases, list)
    ):
        raise AegisAuditError("source audit is incomplete for sequential selection")
    matching = [case for case in cases if case.get("source_case") == selected.get("source_case")]
    if len(matching) != 1:
        raise AegisAuditError("sequential selection source case is not unique")
    case = matching[0]
    mechanism = case.get("mechanism_evidence")
    trace_windows = case.get("trace_windows")
    if (
        case.get("directed_graph_candidate") is not True
        or not isinstance(mechanism, dict)
        or mechanism.get("predicate_match") is not True
        or not isinstance(trace_windows, dict)
    ):
        raise AegisAuditError("sequential selection candidate is not source-scoreable")

    observed_selected = {
        "source_case": case.get("source_case"),
        "fault_type": case.get("fault_type"),
        "ground_truth_services": case.get("ground_truth_services"),
        "normal_window": case.get("source_windows", {}).get("normal"),
        "abnormal_window": case.get("source_windows", {}).get("abnormal"),
        "declared_edge": case.get("declared_edge"),
        "mechanism_evidence": _selected_mechanism_projection(
            mechanism,
            str(selected.get("mechanism_evidence", {}).get("predicate")),
        ),
        "normal_raw_edge_set": _edge_set_summary(trace_windows.get("normal")),
        "abnormal_raw_edge_set": _edge_set_summary(trace_windows.get("abnormal")),
    }
    frozen_source = manifest.get("source")
    observed = {
        "source": {
            "artifact_record": source.get("artifact_record"),
            "artifact_filename": source.get("artifact_filename"),
            "artifact_md5": source.get("expected_artifact_md5"),
            "source_dataset_record": source.get("source_dataset_record"),
            "data_redistributed": source.get("data_redistributed"),
        },
        "selection_strategy": manifest.get("selection_strategy"),
        "selection_seed": selection.get("seed"),
        "parent_manifest": parent_name,
        "parent_manifest_sha256": observed_parent_sha256,
        "parent_ranked_candidates": selection.get("ranked_candidates"),
        "parent_unconsumed_candidates": unconsumed,
        "agent_case_id": manifest.get("agent_case_id"),
        "selected_case": observed_selected,
    }
    expected = {
        "source": frozen_source,
        "selection_strategy": "next-unconsumed-from-parent-v1",
        "selection_seed": manifest.get("selection_seed"),
        "parent_manifest": parent_name,
        "parent_manifest_sha256": parent_sha256,
        "parent_ranked_candidates": manifest.get("parent_ranked_candidates"),
        "parent_unconsumed_candidates": manifest.get("parent_unconsumed_candidates"),
        "agent_case_id": manifest.get("agent_case_id"),
        "selected_case": selected,
    }
    if observed != expected:
        mismatches = sorted(key for key in expected if observed[key] != expected[key])
        raise AegisAuditError(f"frozen sequential selection drift: {mismatches}")
    if observed["agent_case_id"] == parent.get("agent_case_id"):
        raise AegisAuditError("sequential selection must use a new opaque agent case ID")
    return {
        "manifest_name": selection_path.name,
        "observed": observed,
        "expected": expected,
        "pass": True,
    }


def _validate_recalibration_selection(
    audit: dict[str, object],
    selection_path: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    parent_name = manifest.get("parent_manifest")
    parent_sha256 = manifest.get("parent_manifest_sha256")
    if not isinstance(parent_name, str) or Path(parent_name).name != parent_name:
        raise AegisAuditError("recalibration selection has an invalid parent manifest")
    parent_path = selection_path.parent / parent_name
    if not parent_path.is_file() or not isinstance(parent_sha256, str):
        raise AegisAuditError("recalibration parent manifest is missing")
    observed_parent_sha256 = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    if observed_parent_sha256 != parent_sha256:
        raise AegisAuditError("recalibration parent manifest checksum drifted")
    parent = _read_json(parent_path)
    selected = manifest.get("selected_case")
    parent_selected = parent.get("selected_case")
    cases = audit.get("cases")
    source = audit.get("source")
    if (
        manifest.get("case_role") != "development"
        or not isinstance(selected, dict)
        or not isinstance(parent_selected, dict)
        or selected.get("source_case") != parent_selected.get("source_case")
        or manifest.get("agent_case_id") != parent.get("agent_case_id")
        or not isinstance(cases, list)
        or not isinstance(source, dict)
    ):
        raise AegisAuditError("recalibration selection is not bound to the consumed v25 case")
    matching = [case for case in cases if case.get("source_case") == selected.get("source_case")]
    if len(matching) != 1:
        raise AegisAuditError("recalibration source case is not unique")
    case = matching[0]
    mechanism = case.get("mechanism_evidence")
    trace_windows = case.get("trace_windows")
    selected_mechanism = selected.get("mechanism_evidence")
    if (
        case.get("directed_graph_candidate") is not True
        or not isinstance(mechanism, dict)
        or mechanism.get("start_gap_predicate_match") is not True
        or not isinstance(trace_windows, dict)
        or not isinstance(selected_mechanism, dict)
    ):
        raise AegisAuditError("recalibration case lacks source-faithful start-gap evidence")
    observed_selected = {
        "source_case": case.get("source_case"),
        "fault_type": case.get("fault_type"),
        "ground_truth_services": case.get("ground_truth_services"),
        "normal_window": case.get("source_windows", {}).get("normal"),
        "abnormal_window": case.get("source_windows", {}).get("abnormal"),
        "declared_edge": case.get("declared_edge"),
        "mechanism_evidence": _selected_mechanism_projection(
            mechanism, str(selected_mechanism.get("predicate"))
        ),
        "normal_raw_edge_set": _edge_set_summary(trace_windows.get("normal")),
        "abnormal_raw_edge_set": _edge_set_summary(trace_windows.get("abnormal")),
    }
    observed_source = {
        "artifact_record": source.get("artifact_record"),
        "artifact_filename": source.get("artifact_filename"),
        "artifact_md5": source.get("expected_artifact_md5"),
        "source_dataset_record": source.get("source_dataset_record"),
        "data_redistributed": source.get("data_redistributed"),
    }
    if observed_source != manifest.get("source") or observed_selected != selected:
        raise AegisAuditError("frozen recalibration selection drifted")
    return {
        "manifest_name": selection_path.name,
        "observed": {
            "source": observed_source,
            "selection_strategy": manifest.get("selection_strategy"),
            "parent_manifest": parent_name,
            "parent_manifest_sha256": observed_parent_sha256,
            "case_role": manifest.get("case_role"),
            "agent_case_id": manifest.get("agent_case_id"),
            "selected_case": observed_selected,
        },
        "expected": {
            "source": manifest.get("source"),
            "selection_strategy": manifest.get("selection_strategy"),
            "parent_manifest": parent_name,
            "parent_manifest_sha256": parent_sha256,
            "case_role": "development",
            "agent_case_id": manifest.get("agent_case_id"),
            "selected_case": selected,
        },
        "pass": True,
    }


def _validate_fresh_observable_selection(
    audit: dict[str, object],
    selection_path: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    parents = manifest.get("consumed_parent_manifests")
    if not isinstance(parents, list) or not parents:
        raise AegisAuditError("fresh selection has no consumed parent manifests")
    observed_parents = []
    consumed_cases = []
    consumed_agent_ids = []
    for item in parents:
        if not isinstance(item, dict):
            raise AegisAuditError("fresh selection parent manifest entry is malformed")
        name = item.get("name")
        expected_sha256 = item.get("sha256")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(expected_sha256, str)
        ):
            raise AegisAuditError("fresh selection parent manifest identity is invalid")
        path = selection_path.parent / name
        if not path.is_file():
            raise AegisAuditError("fresh selection parent manifest is missing")
        observed_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed_sha256 != expected_sha256:
            raise AegisAuditError("fresh selection parent manifest checksum drifted")
        parent = _read_json(path)
        selected = parent.get("selected_case")
        agent_case_id = parent.get("agent_case_id")
        if (
            not isinstance(selected, dict)
            or not isinstance(selected.get("source_case"), str)
            or not isinstance(agent_case_id, str)
        ):
            raise AegisAuditError("fresh selection parent manifest is incomplete")
        consumed_cases.append(selected["source_case"])
        consumed_agent_ids.append(agent_case_id)
        observed_parents.append({"name": name, "sha256": observed_sha256})

    observable = audit.get("observable_mechanism_selection")
    source = audit.get("source")
    cases = audit.get("cases")
    selected = manifest.get("selected_case")
    if (
        not isinstance(observable, dict)
        or not isinstance(source, dict)
        or not isinstance(cases, list)
        or not isinstance(selected, dict)
    ):
        raise AegisAuditError("source audit is incomplete for fresh selection")
    consumed_set = set(consumed_cases)
    eligible = sorted(
        str(case["source_case"])
        for case in cases
        if case.get("graph_source_eligible") is True
        and str(case.get("source_case")) not in consumed_set
        and _v26_transfer_mechanism_supported(case.get("mechanism_evidence"))
    )
    ranked = deterministic_rank(eligible, str(observable.get("seed"))) if eligible else []
    if not ranked or selected.get("source_case") != ranked[0]:
        raise AegisAuditError("fresh selection is not the first unconsumed eligible candidate")
    matching = [case for case in cases if case.get("source_case") == ranked[0]]
    if len(matching) != 1:
        raise AegisAuditError("fresh selection source case is not unique")
    case = matching[0]
    mechanism = case.get("mechanism_evidence")
    trace_windows = case.get("trace_windows")
    selected_mechanism = selected.get("mechanism_evidence")
    if (
        case.get("graph_source_eligible") is not True
        or not isinstance(mechanism, dict)
        or not _v26_transfer_mechanism_supported(mechanism)
        or not isinstance(trace_windows, dict)
        or not isinstance(selected_mechanism, dict)
    ):
        raise AegisAuditError("fresh selection candidate is not source-scoreable")
    observed_selected = {
        "source_case": case.get("source_case"),
        "fault_type": case.get("fault_type"),
        "ground_truth_services": case.get("ground_truth_services"),
        "normal_window": case.get("source_windows", {}).get("normal"),
        "abnormal_window": case.get("source_windows", {}).get("abnormal"),
        "declared_edge": case.get("declared_edge"),
        "mechanism_evidence": _selected_mechanism_projection(
            mechanism, str(selected_mechanism.get("predicate"))
        ),
        "normal_raw_edge_set": _edge_set_summary(trace_windows.get("normal")),
        "abnormal_raw_edge_set": _edge_set_summary(trace_windows.get("abnormal")),
    }
    observed_source = {
        "artifact_record": source.get("artifact_record"),
        "artifact_filename": source.get("artifact_filename"),
        "artifact_md5": source.get("expected_artifact_md5"),
        "source_dataset_record": source.get("source_dataset_record"),
        "data_redistributed": source.get("data_redistributed"),
    }
    observed = {
        "source": observed_source,
        "selection_strategy": manifest.get("selection_strategy"),
        "selection_seed": observable.get("seed"),
        "consumed_parent_manifests": observed_parents,
        "consumed_source_cases": consumed_cases,
        "eligible_unconsumed_candidates": sorted(eligible),
        "ranked_unconsumed_candidates": ranked,
        "case_role": manifest.get("case_role"),
        "selection_phase": manifest.get("selection_phase"),
        "agent_case_id": manifest.get("agent_case_id"),
        "selected_case": observed_selected,
    }
    expected = {
        "source": manifest.get("source"),
        "selection_strategy": "fresh-source-observable-supported-mechanism-v2",
        "selection_seed": manifest.get("selection_seed"),
        "consumed_parent_manifests": parents,
        "consumed_source_cases": manifest.get("consumed_source_cases"),
        "eligible_unconsumed_candidates": manifest.get("eligible_unconsumed_candidates"),
        "ranked_unconsumed_candidates": manifest.get("ranked_unconsumed_candidates"),
        "case_role": "measurement",
        "selection_phase": "before_agent_trajectory",
        "agent_case_id": manifest.get("agent_case_id"),
        "selected_case": selected,
    }
    if observed != expected:
        mismatches = sorted(key for key in expected if observed[key] != expected[key])
        raise AegisAuditError(f"frozen fresh selection drift: {mismatches}")
    if observed["agent_case_id"] in consumed_agent_ids:
        raise AegisAuditError("fresh selection must use a new opaque agent case ID")
    return {
        "manifest_name": selection_path.name,
        "observed": observed,
        "expected": expected,
        "pass": True,
    }


def _selected_mechanism_projection(
    mechanism: dict[str, object], predicate: str
) -> dict[str, object]:
    if predicate == "source_declared_http_delay_threshold":
        fields = (
            "predicate",
            "span_name",
            "declared_delay_ns",
            "normal_count",
            "normal_max_duration_ns",
            "abnormal_count",
            "abnormal_max_duration_ns",
        )
    elif predicate == "source_declared_http_client_server_start_gap":
        fields = (
            "start_gap_predicate",
            "span_name",
            "declared_delay_ns",
            "normal_count",
            "normal_min_start_gap_ns",
            "normal_max_start_gap_ns",
            "normal_at_or_above_threshold",
            "abnormal_count",
            "abnormal_min_start_gap_ns",
            "abnormal_max_start_gap_ns",
            "abnormal_at_or_above_threshold",
        )
    elif predicate == "source_declared_workload_restart":
        fields = (
            "predicate",
            "pod_name",
            "metric",
            "normal_count",
            "normal_min_restarts",
            "normal_max_restarts",
            "abnormal_count",
            "abnormal_min_restarts",
            "abnormal_max_restarts",
        )
    elif predicate == "source_declared_jvm_exception":
        fields = (
            "predicate",
            "service_name",
            "method_name",
            "normal_error_span_count",
            "normal_exception_log_count",
            "abnormal_error_span_count",
            "abnormal_exception_log_count",
        )
    else:
        raise AegisAuditError(f"unsupported fresh transfer mechanism predicate: {predicate}")
    projected = {key: mechanism.get(key) for key in fields}
    if "start_gap_predicate" in projected:
        projected["predicate"] = projected.pop("start_gap_predicate")
    return projected


def _v26_transfer_mechanism_supported(mechanism: object) -> bool:
    if not isinstance(mechanism, dict):
        return False
    if mechanism.get("predicate") == "source_declared_jvm_exception":
        return mechanism.get("predicate_match") is True
    if mechanism.get("start_gap_predicate") == "source_declared_http_client_server_start_gap":
        return mechanism.get("start_gap_predicate_match") is True
    return False


def _edge_set_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    edge_set = value.get("edge_set")
    if not isinstance(edge_set, list):
        return None
    return {
        "edge_count": len(edge_set),
        "witness_count": value.get("client_server_witness_count"),
        "sha256": value.get("edge_set_sha256"),
    }


def _verify_archive(path: Path) -> None:
    if not path.is_file() or path.stat().st_size != ARTIFACT_SIZE:
        size = path.stat().st_size if path.is_file() else None
        raise AegisAuditError(
            f"Aegis archive size mismatch: expected {ARTIFACT_SIZE}, observed {size}"
        )
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != ARTIFACT_MD5:
        raise AegisAuditError(
            f"Aegis archive checksum mismatch: expected {ARTIFACT_MD5}, "
            f"observed {digest.hexdigest()}"
        )


def _extract_dataset(archive: Path, dataset_root: Path) -> None:
    marker = dataset_root / ".artifact-md5"
    if marker.is_file() and marker.read_text().strip() == ARTIFACT_MD5:
        return
    if dataset_root.exists():
        raise AegisAuditError(f"unverified Aegis extraction already exists: {dataset_root}")

    with tempfile.TemporaryDirectory(prefix="aegis-extract-", dir=dataset_root.parent) as temp:
        staging = Path(temp) / dataset_root.name
        extracted: set[Path] = set()
        with tarfile.open(archive, mode="r:gz") as source:
            for member in source:
                if not member.name.startswith(_ARCHIVE_DATASET_PREFIX):
                    continue
                relative_text = member.name.removeprefix(_ARCHIVE_DATASET_PREFIX)
                if not relative_text:
                    continue
                relative = Path(relative_text)
                if relative.is_absolute() or ".." in relative.parts:
                    raise AegisAuditError(f"unsafe path in Aegis archive: {member.name}")
                target = staging / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise AegisAuditError(f"unsupported member in Aegis dataset: {member.name}")
                if relative in extracted:
                    raise AegisAuditError(f"duplicate member in Aegis archive: {member.name}")
                extracted.add(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                fileobj = source.extractfile(member)
                if fileobj is None:
                    raise AegisAuditError(f"cannot read Aegis archive member: {member.name}")
                with fileobj, target.open("wb") as output:
                    shutil.copyfileobj(fileobj, output)
        required = {
            Path("data/rcabench"),
            Path("meta/rcabench/index.parquet"),
            Path("meta/rcabench/attributes.parquet"),
            Path("meta/rcabench/labels.parquet"),
        }
        missing = [path for path in required if not (staging / path).exists()]
        if missing:
            raise AegisAuditError(f"Aegis archive is missing dataset paths: {missing}")
        marker_path = staging / marker.name
        marker_path.write_text(f"{ARTIFACT_MD5}\n")
        staging.replace(dataset_root)


def _audit_case(
    root: Path,
    attributes: dict[str, object],
    published_labels: set[str],
) -> dict[str, object]:
    if not root.is_dir():
        raise AegisAuditError(f"missing case directory: {root}")
    injection = _read_json(root / "injection.json")
    env = _read_json(root / "env.json")
    if injection.get("injection_name") != root.name:
        raise AegisAuditError(f"injection identity disagrees for {root.name}")
    if injection.get("status") != 2:
        raise AegisAuditError(f"injection did not succeed for {root.name}")
    if not (root / ".finished").is_file():
        raise AegisAuditError(f"case has no finished marker: {root.name}")

    normal_start = _env_epoch(env, "NORMAL_START")
    normal_end = _env_epoch(env, "NORMAL_END")
    abnormal_start = _env_epoch(env, "ABNORMAL_START")
    abnormal_end = _env_epoch(env, "ABNORMAL_END")
    if not normal_start < normal_end == abnormal_start < abnormal_end:
        raise AegisAuditError(f"non-contiguous source windows for {root.name}")
    injection_start_value = datetime.fromisoformat(
        str(injection["start_time"]).replace("Z", "+00:00")
    )
    if injection_start_value.tzinfo is None:
        raise AegisAuditError("injection start time must include an explicit timezone")
    injection_start = int(injection_start_value.timestamp())
    if injection_start != abnormal_start:
        raise AegisAuditError(f"injection time disagrees with source window for {root.name}")

    ground_truth = injection.get("ground_truth")
    if not isinstance(ground_truth, dict):
        raise AegisAuditError(f"missing injection ground truth for {root.name}")
    services = ground_truth.get("service")
    if not isinstance(services, list) or not services:
        raise AegisAuditError(f"missing service ground truth for {root.name}")
    ground_truth_services = {str(service) for service in services}
    if ground_truth_services != published_labels:
        raise AegisAuditError(f"injection and published labels disagree for {root.name}")
    if attributes.get("ground_truth.service_count") != len(ground_truth_services):
        raise AegisAuditError(f"published service count disagrees for {root.name}")

    fault_type = str(attributes.get("injection.fault_type") or "")
    if not fault_type:
        raise AegisAuditError(f"missing published fault type for {root.name}")
    display_config = _json_object(injection.get("display_config"), "display_config", root.name)
    declared_edge = _declared_edge(display_config)
    windows: dict[str, dict[str, object]] = {}
    observations: dict[str, dict[tuple[str, str], list[dict[str, object]]]] = {}
    for period in _PERIODS:
        report, endpoint_observations = _trace_window_audit(root / f"{period}_traces.parquet")
        windows[period] = report
        observations[period] = endpoint_observations

    reasons = []
    if (root / ".invalid").exists():
        reasons.append("publisher_invalid_marker")
    if not all(window["identity_complete"] for window in windows.values()):
        reasons.append("incomplete_trace_identity")
    if not all(window["span_identity_unique"] for window in windows.values()):
        reasons.append("duplicate_trace_span_identity")
    if not all(window["native_roles_and_statuses_valid"] for window in windows.values()):
        reasons.append("invalid_native_trace_semantics")
    if not all(window["client_server_witness_count"] > 0 for window in windows.values()):
        reasons.append("missing_client_server_witnesses")
    if not all(
        ground_truth_services <= set(window["service_names"]) for window in windows.values()
    ):
        reasons.append("ground_truth_service_missing_from_trace_window")

    graph_source_eligible = not reasons
    declared_stats = {
        period: _edge_for_pair(windows[period]["edge_set"], declared_edge) for period in _PERIODS
    }
    directed_reasons = list(reasons)
    if len(ground_truth_services) != 2:
        directed_reasons.append("ground_truth_is_not_a_service_pair")
    if declared_edge is None:
        directed_reasons.append("no_source_declared_directed_endpoint")
    elif set(declared_edge) != ground_truth_services:
        directed_reasons.append("declared_endpoint_disagrees_with_ground_truth")
    elif any(declared_stats[period] is None for period in _PERIODS):
        directed_reasons.append("declared_endpoint_not_witnessed_in_both_windows")
    directed_graph_candidate = not directed_reasons
    mechanism = _mechanism_evidence(
        root,
        fault_type,
        display_config,
        declared_edge,
        observations,
        ground_truth,
        {field for window in windows.values() for field in window["observed_nonempty_body_fields"]},
    )
    return {
        "source_case": root.name,
        "fault_type": fault_type,
        "ground_truth_services": sorted(ground_truth_services),
        "source_windows": {
            "normal": [normal_start, normal_end],
            "abnormal": [abnormal_start, abnormal_end],
        },
        "publisher_invalid_marker": (root / ".invalid").exists(),
        "graph_source_eligible": graph_source_eligible,
        "graph_source_rejection_reasons": reasons,
        "declared_edge": list(declared_edge) if declared_edge else None,
        "declared_edge_stats": declared_stats,
        "directed_graph_candidate": directed_graph_candidate,
        "directed_graph_rejection_reasons": directed_reasons,
        "mechanism_evidence": mechanism,
        "trace_windows": windows,
        "reference_causal_graph_ingested": False,
        "source_data_modified": False,
    }


def _trace_window_audit(
    path: Path,
) -> tuple[dict[str, object], dict[tuple[str, str], list[dict[str, object]]]]:
    source_schema_columns = pq.read_schema(path).names
    schema_names = set(source_schema_columns)
    missing = _REQUIRED_TRACE_COLUMNS - schema_names
    if missing:
        raise AegisAuditError(f"missing trace columns in {path}: {sorted(missing)}")
    body_columns = sorted(column for column in source_schema_columns if "body" in column.lower())
    columns = [column for column in _TRACE_COLUMNS if column in schema_names]
    columns.extend(column for column in body_columns if column not in columns)
    table = pq.read_table(path, columns=columns)
    time_index = table.schema.get_field_index("time")
    if time_index >= 0:
        table = table.set_column(time_index, "time", pc.cast(table.column(time_index), pa.int64()))
    rows = table.to_pylist()
    nonempty_body_fields = sorted(
        column for column in body_columns if any(row.get(column) not in (None, "") for row in rows)
    )
    identities = [(row["trace_id"], row["span_id"]) for row in rows]
    identity_complete = all(
        row[field] not in (None, "")
        for row in rows
        for field in ("trace_id", "span_id", "service_name", "attr.span_kind", "attr.status_code")
    )
    kinds = {str(row["attr.span_kind"]) for row in rows}
    statuses = {str(row["attr.status_code"]) for row in rows}
    index = {identity: row for identity, row in zip(identities, rows, strict=True)}
    edges: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    observations: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    unmatched_server_parents = 0
    witnesses = 0
    for server in rows:
        if server["attr.span_kind"] != "Server" or not server.get("parent_span_id"):
            continue
        client = index.get((server["trace_id"], server["parent_span_id"]))
        if client is None:
            unmatched_server_parents += 1
            continue
        if client["attr.span_kind"] != "Client":
            continue
        pair = (str(client["service_name"]), str(server["service_name"]))
        edges[pair][0] += 1
        edges[pair][1] += server["attr.status_code"] == "Error"
        observations[pair].append({"client": client, "server": server})
        witnesses += 1
    edge_set = [
        {
            "src_type": "service",
            "src_id": source,
            "dst_type": "service",
            "dst_id": destination,
            "rel_type": "calls",
            "provenance": "trace",
            "request_count": counts[0],
            "error_count": counts[1],
        }
        for (source, destination), counts in sorted(edges.items())
    ]
    digest = hashlib.sha256(
        json.dumps(edge_set, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        {
            "source_rows": len(rows),
            "source_schema_columns": source_schema_columns,
            "observed_body_columns": body_columns,
            "observed_nonempty_body_fields": nonempty_body_fields,
            "identity_complete": identity_complete,
            "span_identity_unique": len(identities) == len(set(identities)),
            "native_roles_and_statuses_valid": kinds <= _SPAN_KINDS and statuses <= _STATUS_CODES,
            "span_kinds": sorted(kinds),
            "status_codes": sorted(statuses),
            "service_names": sorted({str(row["service_name"]) for row in rows}),
            "unmatched_server_parent_count": unmatched_server_parents,
            "client_server_witness_count": witnesses,
            "edge_set_sha256": digest,
            "edge_set": edge_set,
        },
        dict(observations),
    )


def _mechanism_evidence(
    root: Path,
    fault_type: str,
    display_config: dict[str, object],
    edge: tuple[str, str] | None,
    observations: dict[str, dict[tuple[str, str], list[dict[str, object]]]],
    ground_truth: dict[str, object],
    nonempty_body_fields: set[str],
) -> dict[str, object]:
    component = _component_mechanism_evidence(root, fault_type, display_config, ground_truth)
    if component is not None:
        return component
    if edge is None:
        return {
            "predicate": None,
            "scoreable": False,
            "predicate_match": False,
            "reason": "fault has no source-declared directed endpoint",
        }
    normal = observations["normal"].get(edge, [])
    abnormal = observations["abnormal"].get(edge, [])
    injection_point = display_config.get("injection_point")
    if not isinstance(injection_point, dict):
        return {
            "predicate": None,
            "scoreable": False,
            "predicate_match": False,
            "reason": "fault has no structured injection point",
        }

    if fault_type == "HTTPRequestReplaceMethod":
        original = str(injection_point.get("method") or "")
        replacement = str(display_config.get("replace_method") or "")
        normal_server_methods = Counter(
            str(item["server"].get("attr.http.request.method") or "") for item in normal
        )
        abnormal_server_methods = Counter(
            str(item["server"].get("attr.http.request.method") or "") for item in abnormal
        )
        abnormal_client_methods = Counter(
            str(item["client"].get("attr.http.request.method") or "") for item in abnormal
        )
        matched = bool(
            original
            and replacement
            and normal_server_methods[original] > 0
            and normal_server_methods[replacement] == 0
            and abnormal_client_methods[original] > 0
            and abnormal_server_methods[replacement] > 0
        )
        return {
            "predicate": "source_declared_http_method_replacement",
            "scoreable": True,
            "predicate_match": matched,
            "original_method": original,
            "replacement_method": replacement,
            "normal_server_methods": dict(sorted(normal_server_methods.items())),
            "abnormal_client_methods": dict(sorted(abnormal_client_methods.items())),
            "abnormal_server_methods": dict(sorted(abnormal_server_methods.items())),
        }

    if fault_type == "HTTPRequestDelay":
        method = str(injection_point.get("method") or "")
        route = str(injection_point.get("route") or "")
        threshold_ns = int(display_config.get("delay_duration") or 0) * 1_000_000
        span_name = f"{method} {route}"
        normal_durations = [
            int(item["server"]["duration"])
            for item in normal
            if item["server"].get("span_name") == span_name
        ]
        abnormal_durations = [
            int(item["server"]["duration"])
            for item in abnormal
            if item["server"].get("span_name") == span_name
        ]
        normal_start_gaps = [
            int(item["server"]["time"]) - int(item["client"]["time"])
            for item in normal
            if item["server"].get("span_name") == span_name
            and item["server"].get("time") is not None
            and item["client"].get("time") is not None
        ]
        abnormal_start_gaps = [
            int(item["server"]["time"]) - int(item["client"]["time"])
            for item in abnormal
            if item["server"].get("span_name") == span_name
            and item["server"].get("time") is not None
            and item["client"].get("time") is not None
        ]
        normal_max = max(normal_durations, default=None)
        abnormal_max = max(abnormal_durations, default=None)
        matched = bool(
            threshold_ns > 0
            and normal_max is not None
            and abnormal_max is not None
            and normal_max < threshold_ns <= abnormal_max
        )
        normal_gap_min = min(normal_start_gaps, default=None)
        normal_gap_max = max(normal_start_gaps, default=None)
        abnormal_gap_min = min(abnormal_start_gaps, default=None)
        abnormal_gap_max = max(abnormal_start_gaps, default=None)
        normal_threshold_count = sum(value >= threshold_ns for value in normal_start_gaps)
        abnormal_threshold_count = sum(value >= threshold_ns for value in abnormal_start_gaps)
        start_gap_matched = bool(
            threshold_ns > 0
            and normal_gap_max is not None
            and abnormal_gap_min is not None
            and normal_gap_max < threshold_ns <= abnormal_gap_min
            and normal_threshold_count == 0
            and abnormal_threshold_count == len(abnormal_start_gaps)
            and abnormal_start_gaps
        )
        return {
            "predicate": "source_declared_http_delay_threshold",
            "scoreable": True,
            "predicate_match": matched,
            "span_name": span_name,
            "declared_delay_ns": threshold_ns,
            "normal_count": len(normal_durations),
            "normal_max_duration_ns": normal_max,
            "abnormal_count": len(abnormal_durations),
            "abnormal_max_duration_ns": abnormal_max,
            "start_gap_predicate": "source_declared_http_client_server_start_gap",
            "start_gap_predicate_match": start_gap_matched,
            "normal_min_start_gap_ns": normal_gap_min,
            "normal_max_start_gap_ns": normal_gap_max,
            "normal_at_or_above_threshold": normal_threshold_count,
            "abnormal_min_start_gap_ns": abnormal_gap_min,
            "abnormal_max_start_gap_ns": abnormal_gap_max,
            "abnormal_at_or_above_threshold": abnormal_threshold_count,
        }

    if fault_type == "HTTPResponseReplaceBody":
        if nonempty_body_fields:
            raise AegisAuditError(
                "response body values are now present; the frozen transfer predicate must be "
                "reviewed before selecting a case"
            )
        return {
            "predicate": "source_declared_http_response_body_replacement",
            "scoreable": False,
            "predicate_match": False,
            "reason": "source traces do not retain a response body value",
            "observed_nonempty_body_fields": sorted(nonempty_body_fields),
        }

    return {
        "predicate": None,
        "scoreable": False,
        "predicate_match": False,
        "reason": "no frozen transfer predicate for this fault type",
    }


def _component_mechanism_evidence(
    root: Path,
    fault_type: str,
    display_config: dict[str, object],
    ground_truth: dict[str, object],
) -> dict[str, object] | None:
    pods = ground_truth.get("pod")
    services = ground_truth.get("service")
    if not isinstance(pods, list) or len(pods) != 1 or not isinstance(services, list):
        return None
    pod_name = str(pods[0])
    service_name = str(services[0]) if len(services) == 1 else ""
    if fault_type in {"ContainerKill", "PodFailure"}:
        values = {
            period: _metric_values(
                root / f"{period}_metrics.parquet",
                metric="k8s.container.restarts",
                pod_name=pod_name,
            )
            for period in _PERIODS
        }
        normal = values["normal"]
        abnormal = values["abnormal"]
        matched = bool(normal and abnormal and max(normal) == 0 and min(abnormal) >= 1)
        return {
            "predicate": "source_declared_workload_restart",
            "scoreable": matched,
            "predicate_match": matched,
            "pod_name": pod_name,
            "metric": "k8s.container.restarts",
            "normal_count": len(normal),
            "normal_min_restarts": min(normal) if normal else None,
            "normal_max_restarts": max(normal) if normal else None,
            "abnormal_count": len(abnormal),
            "abnormal_min_restarts": min(abnormal) if abnormal else None,
            "abnormal_max_restarts": max(abnormal) if abnormal else None,
        }
    if fault_type == "JVMMemoryStress":
        threshold = 0.85
        values = {
            period: _metric_values(
                root / f"{period}_metrics.parquet",
                metric="k8s.pod.memory_limit_utilization",
                pod_name=pod_name,
            )
            for period in _PERIODS
        }
        normal = values["normal"]
        abnormal = values["abnormal"]
        abnormal_hits = sum(value >= threshold for value in abnormal)
        matched = bool(normal and abnormal and max(normal) < threshold and abnormal_hits >= 2)
        return {
            "predicate": "source_declared_memory_pressure",
            "scoreable": matched,
            "predicate_match": matched,
            "pod_name": pod_name,
            "metric": "k8s.pod.memory_limit_utilization",
            "threshold": threshold,
            "normal_count": len(normal),
            "normal_max": max(normal) if normal else None,
            "normal_at_or_above_threshold": sum(value >= threshold for value in normal),
            "abnormal_count": len(abnormal),
            "abnormal_max": max(abnormal) if abnormal else None,
            "abnormal_at_or_above_threshold": abnormal_hits,
        }
    if fault_type == "JVMException":
        injection_point = display_config.get("injection_point")
        if not isinstance(injection_point, dict):
            return None
        method_name = str(injection_point.get("method_name") or "")
        if not method_name or not service_name:
            return None
        trace_counts = {
            period: _exception_trace_count(
                root / f"{period}_traces.parquet", service_name, method_name
            )
            for period in _PERIODS
        }
        log_counts = {
            period: _exception_log_count(root / f"{period}_logs.parquet", service_name)
            for period in _PERIODS
        }
        matched = (
            trace_counts["normal"] == 0
            and log_counts["normal"] == 0
            and trace_counts["abnormal"] >= 2
            and log_counts["abnormal"] >= 2
        )
        return {
            "predicate": "source_declared_jvm_exception",
            "scoreable": matched,
            "predicate_match": matched,
            "service_name": service_name,
            "method_name": method_name,
            "normal_error_span_count": trace_counts["normal"],
            "normal_exception_log_count": log_counts["normal"],
            "abnormal_error_span_count": trace_counts["abnormal"],
            "abnormal_exception_log_count": log_counts["abnormal"],
        }
    return None


def _metric_values(path: Path, *, metric: str, pod_name: str) -> list[float]:
    table = pq.read_table(path, columns=["metric", "value", "attr.k8s.pod.name"])
    return [
        float(row["value"])
        for row in table.to_pylist()
        if row.get("metric") == metric
        and row.get("attr.k8s.pod.name") == pod_name
        and isinstance(row.get("value"), (int, float))
    ]


def _exception_trace_count(path: Path, service_name: str, method_name: str) -> int:
    table = pq.read_table(
        path,
        columns=["service_name", "span_name", "attr.status_code"],
    )
    return sum(
        row.get("service_name") == service_name
        and row.get("attr.status_code") == "Error"
        and method_name.lower() in str(row.get("span_name") or "").lower()
        for row in table.to_pylist()
    )


def _exception_log_count(path: Path, service_name: str) -> int:
    table = pq.read_table(path, columns=["service_name", "level", "message"])
    return sum(
        row.get("service_name") == service_name
        and str(row.get("level") or "").upper() in {"ERROR", "SEVERE", "FATAL"}
        and "exception" in str(row.get("message") or "").lower()
        for row in table.to_pylist()
    )


def _edge_for_pair(
    edges: object,
    pair: tuple[str, str] | None,
) -> dict[str, object] | None:
    if pair is None or not isinstance(edges, list):
        return None
    return next(
        (
            edge
            for edge in edges
            if isinstance(edge, dict) and (edge.get("src_id"), edge.get("dst_id")) == pair
        ),
        None,
    )


def _declared_edge(display_config: dict[str, object]) -> tuple[str, str] | None:
    injection_point = display_config.get("injection_point")
    if not isinstance(injection_point, dict):
        return None
    source = injection_point.get("app_name")
    destination = injection_point.get("server_address")
    if source in (None, "") or destination in (None, ""):
        return None
    return str(source), str(destination)


def _meta_rows(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise AegisAuditError(f"missing publisher metadata: {path}")
    return pq.read_table(path).to_pylist()


def _rows_by_case(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        case_name = str(row.get("datapack") or "")
        if not case_name or case_name in result:
            raise AegisAuditError("publisher attributes contain invalid case identities")
        result[case_name] = row
    return result


def _labels_by_case(rows: list[dict[str, object]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.get("gt.level") != "service" or not row.get("gt.name"):
            raise AegisAuditError("publisher labels contain a non-service or empty label")
        result[str(row.get("datapack") or "")].add(str(row["gt.name"]))
    return dict(result)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AegisAuditError(f"cannot read source JSON: {path}") from error
    if not isinstance(value, dict):
        raise AegisAuditError(f"expected JSON object in {path}")
    return value


def _json_object(value: object, field: str, case_name: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise AegisAuditError(f"invalid {field} for {case_name}") from error
    if not isinstance(decoded, dict):
        raise AegisAuditError(f"invalid {field} for {case_name}")
    return decoded


def _env_epoch(env: dict[str, object], key: str) -> int:
    try:
        return int(str(env[key]))
    except (KeyError, ValueError) as error:
        raise AegisAuditError(f"invalid {key} in source env") from error
