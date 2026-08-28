import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from semantic_rca_bench.datasets import aegis
from semantic_rca_bench.datasets.aegis import AegisAuditError, AegisRepository, audit_cohort

_NORMAL_START = 1_700_000_000
_ABNORMAL_START = _NORMAL_START + 300
_ABNORMAL_END = _ABNORMAL_START + 300


def _write_parquet(path: Path, values: dict[str, list[object]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pydict(values, schema=schema), path)


def _write_case(
    cases_dir: Path,
    name: str,
    fault_type: str,
    source: str,
    destination: str,
    display_config: dict[str, object],
    *,
    invalid: bool = False,
) -> None:
    root = cases_dir / name
    root.mkdir(parents=True)
    (root / ".finished").touch()
    if invalid:
        (root / ".invalid").touch()
    (root / "env.json").write_text(
        json.dumps(
            {
                "NORMAL_START": str(_NORMAL_START),
                "NORMAL_END": str(_ABNORMAL_START),
                "ABNORMAL_START": str(_ABNORMAL_START),
                "ABNORMAL_END": str(_ABNORMAL_END),
            }
        )
    )
    (root / "injection.json").write_text(
        json.dumps(
            {
                "injection_name": name,
                "status": 2,
                "start_time": datetime.fromtimestamp(_ABNORMAL_START, UTC).isoformat(),
                "display_config": json.dumps(
                    {
                        "injection_point": {
                            "app_name": source,
                            "server_address": destination,
                            "method": "POST" if fault_type != "HTTPRequestReplaceMethod" else "GET",
                            "route": "/fault",
                        },
                        **display_config,
                    }
                ),
                "ground_truth": {"service": [source, destination]},
            }
        )
    )

    trace_schema = pa.schema(
        [
            ("trace_id", pa.large_string()),
            ("span_id", pa.large_string()),
            ("parent_span_id", pa.large_string()),
            ("span_name", pa.large_string()),
            ("attr.span_kind", pa.large_string()),
            ("service_name", pa.large_string()),
            ("duration", pa.uint64()),
            ("attr.status_code", pa.large_string()),
            ("attr.http.request.content_length", pa.int64()),
            ("attr.http.response.content_length", pa.int64()),
            ("attr.http.request.method", pa.large_string()),
            ("attr.http.response.status_code", pa.int64()),
        ]
    )
    for period in ("normal", "abnormal"):
        original = "GET" if fault_type == "HTTPRequestReplaceMethod" else "POST"
        server_method = (
            "OPTIONS"
            if fault_type == "HTTPRequestReplaceMethod" and period == "abnormal"
            else original
        )
        duration = (
            4_000_000_000
            if fault_type == "HTTPRequestDelay" and period == "abnormal"
            else 1_000_000_000
        )
        trace_id = f"{name}-{period}"
        _write_parquet(
            root / f"{period}_traces.parquet",
            {
                "trace_id": [trace_id, trace_id],
                "span_id": ["client", "server"],
                "parent_span_id": ["", "client"],
                "span_name": [original, f"{server_method} /fault"],
                "attr.span_kind": ["Client", "Server"],
                "service_name": [source, destination],
                "duration": [duration, duration],
                "attr.status_code": ["Unset", "Unset"],
                "attr.http.request.content_length": [None, None],
                "attr.http.response.content_length": [None, None],
                "attr.http.request.method": [original, server_method],
                "attr.http.response.status_code": [500, 500],
            },
            trace_schema,
        )


def _write_meta(meta_dir: Path, cases: list[tuple[str, str, str, str]]) -> None:
    meta_dir.mkdir(parents=True)
    _write_parquet(
        meta_dir / "index.parquet",
        {
            "dataset": ["rcabench"] * len(cases),
            "datapack": [case[0] for case in cases],
        },
        pa.schema([("dataset", pa.large_string()), ("datapack", pa.large_string())]),
    )
    _write_parquet(
        meta_dir / "attributes.parquet",
        {
            "datapack": [case[0] for case in cases],
            "injection.fault_type": [case[1] for case in cases],
            "ground_truth.service_count": [2] * len(cases),
        },
        pa.schema(
            [
                ("datapack", pa.large_string()),
                ("injection.fault_type", pa.large_string()),
                ("ground_truth.service_count", pa.int64()),
            ]
        ),
    )
    labels = [
        {"datapack": name, "gt.level": "service", "gt.name": service}
        for name, _, source, destination in cases
        for service in (source, destination)
    ]
    _write_parquet(
        meta_dir / "labels.parquet",
        {key: [row[key] for row in labels] for key in labels[0]},
        pa.schema(
            [
                ("datapack", pa.large_string()),
                ("gt.level", pa.large_string()),
                ("gt.name", pa.large_string()),
            ]
        ),
    )


def test_aegis_audit_selects_first_source_scoreable_ranked_case(tmp_path: Path) -> None:
    cases = [
        (
            "ts3-ts-food-service-response-replace-body-skvngv",
            "HTTPResponseReplaceBody",
            "food",
            "station-food",
        ),
        (
            "ts0-ts-security-service-request-replace-method-j6gpxx",
            "HTTPRequestReplaceMethod",
            "security",
            "order-other",
        ),
        (
            "ts8-ts-route-plan-service-request-delay-5dmjfm",
            "HTTPRequestDelay",
            "route-plan",
            "travel2",
        ),
    ]
    cases_dir = tmp_path / "cases"
    _write_case(cases_dir, *cases[0], {"body_type": 1})
    _write_case(cases_dir, *cases[1], {"replace_method": "OPTIONS"})
    _write_case(cases_dir, *cases[2], {"delay_duration": 3_070})
    meta_dir = tmp_path / "meta"
    _write_meta(meta_dir, cases)

    audit = audit_cohort(cases_dir, meta_dir)
    reports = {case["source_case"]: case for case in audit["cases"]}

    assert audit["selection"]["ranked_candidates"] == [cases[0][0], cases[1][0], cases[2][0]]
    assert audit["selection"]["consumed_candidates"] == [cases[0][0], cases[1][0]]
    assert audit["selection"]["selected_case"] == cases[1][0]
    assert reports[cases[0][0]]["mechanism_evidence"]["scoreable"] is False
    assert reports[cases[1][0]]["mechanism_evidence"]["predicate_match"] is True
    assert reports[cases[2][0]]["mechanism_evidence"]["predicate_match"] is True

    abnormal_edge = reports[cases[1][0]]["declared_edge_stats"]["abnormal"]
    assert abnormal_edge["request_count"] == 1
    assert abnormal_edge["error_count"] == 0


def test_aegis_audit_excludes_publisher_invalid_case(tmp_path: Path) -> None:
    case = (
        "ts0-ts-security-service-request-replace-method-j6gpxx",
        "HTTPRequestReplaceMethod",
        "security",
        "order-other",
    )
    cases_dir = tmp_path / "cases"
    _write_case(cases_dir, *case, {"replace_method": "OPTIONS"}, invalid=True)
    meta_dir = tmp_path / "meta"
    _write_meta(meta_dir, [case])

    audit = audit_cohort(cases_dir, meta_dir)
    report = audit["cases"][0]

    assert audit["selection"]["eligible_set"] == []
    assert audit["selection"]["selected_case"] is None
    assert report["graph_source_eligible"] is False
    assert report["graph_source_rejection_reasons"] == ["publisher_invalid_marker"]


def test_aegis_repository_verifies_and_extracts_only_dataset(tmp_path: Path, monkeypatch) -> None:
    case = (
        "ts0-ts-security-service-request-replace-method-j6gpxx",
        "HTTPRequestReplaceMethod",
        "security",
        "order-other",
    )
    artifact_root = (
        tmp_path
        / "payload"
        / "FSE_26_RCA_dataset_study_artifact_clean"
        / "reproduction"
        / "data"
        / "rcabench-platform-v2"
    )
    _write_case(artifact_root / "data" / "rcabench", *case, {"replace_method": "OPTIONS"})
    _write_meta(artifact_root / "meta" / "rcabench", [case])
    unrelated = artifact_root.parents[2] / "vendor" / "large.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("must not be extracted")

    archive = tmp_path / "artifact.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(artifact_root.parents[2], arcname=artifact_root.parents[2].name)
    digest = hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest()
    monkeypatch.setattr(aegis, "ARTIFACT_SIZE", archive.stat().st_size)
    monkeypatch.setattr(aegis, "ARTIFACT_MD5", digest)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    archive.replace(cache_dir / aegis.ARTIFACT_FILENAME)
    cases_dir, meta_dir = AegisRepository(cache_dir).fetch()
    audit = audit_cohort(cases_dir, meta_dir, archive_checksum_verified=True)

    assert audit["source"]["archive_checksum_verified"] is True
    assert audit["selection"]["selected_case"] == case[0]
    assert not (cache_dir / "rcabench-platform-v2" / "vendor").exists()
    assert (cache_dir / "rcabench-platform-v2" / ".artifact-md5").read_text().strip() == digest


def test_aegis_extraction_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    member = tarfile.TarInfo(f"{aegis._ARCHIVE_DATASET_PREFIX}../escaped.txt")
    member.size = 1
    with tarfile.open(archive, "w:gz") as output:
        output.addfile(member, io.BytesIO(b"x"))

    try:
        aegis._extract_dataset(archive, tmp_path / "dataset")
    except AegisAuditError as error:
        assert "unsafe path" in str(error)
    else:
        raise AssertionError("archive path traversal must fail")
    assert not (tmp_path / "escaped.txt").exists()
