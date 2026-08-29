import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from semantic_rca_bench.datasets import aegis
from semantic_rca_bench.datasets.aegis import AegisAuditError, AegisRepository, audit_cohort
from semantic_rca_bench.selection import deterministic_rank

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
    response_body: str | None = None,
    start_time: str | None = None,
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
                "start_time": start_time
                or datetime.fromtimestamp(_ABNORMAL_START, UTC).isoformat(),
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

    trace_fields = [
        ("time", pa.uint64()),
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
    if response_body is not None:
        trace_fields.append(("attr.http.response.body", pa.large_string()))
    trace_schema = pa.schema(trace_fields)
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
        client_start = (
            _NORMAL_START + 1 if period == "normal" else _ABNORMAL_START + 1
        ) * 1_000_000_000
        server_start = client_start + (
            3_070_000_000
            if fault_type == "HTTPRequestDelay" and period == "abnormal"
            else 1_000_000
        )
        values = {
            "time": [client_start, server_start],
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
        }
        if response_body is not None:
            values["attr.http.response.body"] = [None, response_body]
        _write_parquet(
            root / f"{period}_traces.parquet",
            values,
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
    assert reports[cases[0][0]]["trace_windows"]["normal"]["observed_body_columns"] == []
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


def test_sequential_selection_gate_binds_first_parent_unconsumed_case() -> None:
    selection_path = Path("fixtures/reference/aegis-transfer-v25-selection.json")
    audit = {
        "source": {
            "artifact_record": aegis.ARTIFACT_RECORD,
            "artifact_filename": aegis.ARTIFACT_FILENAME,
            "expected_artifact_md5": aegis.ARTIFACT_MD5,
            "source_dataset_record": aegis.SOURCE_DATASET_RECORD,
            "data_redistributed": False,
        },
        "selection": {
            "seed": aegis.SELECTION_SEED,
            "ranked_candidates": [
                "ts3-ts-food-service-response-replace-body-skvngv",
                "ts0-ts-security-service-request-replace-method-j6gpxx",
                "ts8-ts-route-plan-service-request-delay-5dmjfm",
            ],
        },
        "cases": [
            {
                "source_case": "ts8-ts-route-plan-service-request-delay-5dmjfm",
                "fault_type": "HTTPRequestDelay",
                "ground_truth_services": ["ts-route-plan-service", "ts-travel2-service"],
                "source_windows": {
                    "normal": [1753014770, 1753015010],
                    "abnormal": [1753015010, 1753015249],
                },
                "declared_edge": ["ts-route-plan-service", "ts-travel2-service"],
                "directed_graph_candidate": True,
                "mechanism_evidence": {
                    "predicate": "source_declared_http_delay_threshold",
                    "predicate_match": True,
                    "span_name": "POST /api/v1/travel2service/trips/left",
                    "declared_delay_ns": 3_070_000_000,
                    "normal_count": 37,
                    "normal_max_duration_ns": 846_092_899,
                    "abnormal_count": 25,
                    "abnormal_max_duration_ns": 3_252_068_825,
                },
                "trace_windows": {
                    "normal": {
                        "edge_set": [None] * 40,
                        "client_server_witness_count": 7922,
                        "edge_set_sha256": (
                            "62e8fc4240592258702a55e713413f77b058795edf9d98f7e5d7c3a28bb59acb"
                        ),
                    },
                    "abnormal": {
                        "edge_set": [None] * 38,
                        "client_server_witness_count": 4090,
                        "edge_set_sha256": (
                            "d6c291279c28f4b1a4b36d6d5b9ac23ec19ad2c8638938bac6de3b0094c992bc"
                        ),
                    },
                },
            }
        ],
    }

    assert aegis._validate_frozen_selection(audit, selection_path)["pass"]
    audit["selection"]["ranked_candidates"].reverse()
    with pytest.raises(AegisAuditError, match="frozen sequential selection drift"):
        aegis._validate_frozen_selection(audit, selection_path)


def test_aegis_body_rejection_reads_source_schema(tmp_path: Path) -> None:
    case = (
        "ts3-ts-food-service-response-replace-body-skvngv",
        "HTTPResponseReplaceBody",
        "food",
        "station-food",
    )
    cases_dir = tmp_path / "cases"
    _write_case(cases_dir, *case, {"body_type": 1}, response_body="retained")
    meta_dir = tmp_path / "meta"
    _write_meta(meta_dir, [case])

    with pytest.raises(AegisAuditError, match="body values are now present"):
        audit_cohort(cases_dir, meta_dir)


def test_aegis_audit_rejects_timezone_dependent_injection_time(tmp_path: Path) -> None:
    case = (
        "ts8-ts-route-plan-service-request-delay-5dmjfm",
        "HTTPRequestDelay",
        "route-plan",
        "travel2",
    )
    cases_dir = tmp_path / "cases"
    _write_case(
        cases_dir,
        *case,
        {"delay_duration": 3.07},
        start_time=datetime.fromtimestamp(_ABNORMAL_START, UTC).replace(tzinfo=None).isoformat(),
    )
    meta_dir = tmp_path / "meta"
    _write_meta(meta_dir, [case])

    with pytest.raises(AegisAuditError, match="explicit timezone"):
        audit_cohort(cases_dir, meta_dir)


def test_aegis_audit_rejects_frozen_selection_drift(tmp_path: Path) -> None:
    case = (
        "ts0-ts-security-service-request-replace-method-j6gpxx",
        "HTTPRequestReplaceMethod",
        "security",
        "order-other",
    )
    cases_dir = tmp_path / "cases"
    _write_case(cases_dir, *case, {"replace_method": "OPTIONS"})
    meta_dir = tmp_path / "meta"
    _write_meta(meta_dir, [case])
    manifest = tmp_path / "selection.json"
    manifest.write_text(
        json.dumps(
            {
                "selection_seed": "wrong-seed",
                "agent_case_id": "aegis-transfer-001",
                "source": {
                    "artifact_record": aegis.ARTIFACT_RECORD,
                    "artifact_filename": aegis.ARTIFACT_FILENAME,
                    "artifact_md5": aegis.ARTIFACT_MD5,
                    "source_dataset_record": aegis.SOURCE_DATASET_RECORD,
                    "data_redistributed": False,
                },
                "eligibility": {
                    "eligible_directed_graph_cases": [case[0]],
                    "ranked_candidates": [case[0]],
                    "consumed_candidates": [{"source_case": case[0], "decision": "selected"}],
                    "unconsumed_candidates": [],
                },
                "selected_case": {"source_case": case[0]},
            }
        )
    )

    with pytest.raises(AegisAuditError, match="selection_seed"):
        audit_cohort(cases_dir, meta_dir, selection_path=manifest)


def test_fresh_selection_manifest_binds_consumed_parents_before_trajectory() -> None:
    root = Path("fixtures/reference")
    manifest = json.loads((root / "aegis-transfer-v27-selection.json").read_text())

    assert manifest["selection_phase"] == "before_agent_trajectory"
    assert manifest["selection_strategy"] == ("fresh-source-observable-supported-mechanism-v2")
    assert manifest["case_role"] == "measurement"
    assert manifest["agent_case_id"] not in {"aegis-transfer-001", "aegis-transfer-002"}
    for parent in manifest["consumed_parent_manifests"]:
        assert hashlib.sha256((root / parent["name"]).read_bytes()).hexdigest() == parent["sha256"]
    assert (
        deterministic_rank(manifest["eligible_unconsumed_candidates"], manifest["selection_seed"])
        == manifest["ranked_unconsumed_candidates"]
    )
    assert manifest["selected_case"]["source_case"] == manifest["ranked_unconsumed_candidates"][0]


@pytest.mark.parametrize(
    ("mechanism", "supported"),
    [
        (
            {
                "start_gap_predicate": "source_declared_http_client_server_start_gap",
                "start_gap_predicate_match": True,
            },
            True,
        ),
        (
            {
                "predicate": "source_declared_jvm_exception",
                "predicate_match": True,
            },
            True,
        ),
        (
            {
                "predicate": "source_declared_workload_restart",
                "predicate_match": True,
            },
            False,
        ),
        (
            {
                "predicate": "source_declared_memory_pressure",
                "predicate_match": True,
            },
            False,
        ),
    ],
)
def test_fresh_selection_only_admits_end_to_end_supported_mechanisms(
    mechanism: dict[str, object], supported: bool
) -> None:
    assert aegis._transfer_mechanism_supported(mechanism) is supported


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
