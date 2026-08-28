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
_ARCHIVE_DATASET_PREFIX = (
    "FSE_26_RCA_dataset_study_artifact_clean/reproduction/data/rcabench-platform-v2/"
)

_PERIODS = ("normal", "abnormal")
_TRACE_COLUMNS = (
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
_REQUIRED_TRACE_COLUMNS = frozenset(_TRACE_COLUMNS[:8])
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
    return {
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
        "cases": cases,
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
    injection_start = int(datetime.fromisoformat(str(injection["start_time"])).timestamp())
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
        fault_type,
        display_config,
        declared_edge,
        observations,
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
    schema_names = set(pq.read_schema(path).names)
    missing = _REQUIRED_TRACE_COLUMNS - schema_names
    if missing:
        raise AegisAuditError(f"missing trace columns in {path}: {sorted(missing)}")
    columns = [column for column in _TRACE_COLUMNS if column in schema_names]
    rows = pq.read_table(path, columns=columns).to_pylist()
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
    fault_type: str,
    display_config: dict[str, object],
    edge: tuple[str, str] | None,
    observations: dict[str, dict[tuple[str, str], list[dict[str, object]]]],
) -> dict[str, object]:
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
        normal_max = max(normal_durations, default=None)
        abnormal_max = max(abnormal_durations, default=None)
        matched = bool(
            threshold_ns > 0
            and normal_max is not None
            and abnormal_max is not None
            and normal_max < threshold_ns <= abnormal_max
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
        }

    if fault_type == "HTTPResponseReplaceBody":
        body_fields = {
            key
            for item in (*normal, *abnormal)
            for side in ("client", "server")
            for key, value in item[side].items()
            if "body" in key.lower() and value not in (None, "")
        }
        return {
            "predicate": "source_declared_http_response_body_replacement",
            "scoreable": False,
            "predicate_match": False,
            "reason": "source traces do not retain a response body value",
            "observed_nonempty_body_fields": sorted(body_fields),
        }

    return {
        "predicate": None,
        "scoreable": False,
        "predicate_match": False,
        "reason": "no frozen transfer predicate for this fault type",
    }


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
