from datetime import UTC, datetime

from semantic_rca_bench.datasets.rca100 import (
    _fault_taxonomy,
    _iso_to_nanoseconds,
    _metric_sample_audit,
)


def test_iso_timestamp_preserves_nanoseconds_and_offset() -> None:
    assert _iso_to_nanoseconds("2026-04-25T13:20:23.123456789+08:00") == (
        int(datetime(2026, 4, 25, 5, 20, 23, tzinfo=UTC).timestamp()) * 1_000_000_000
        + 123_456_789
    )


def test_fault_taxonomy_uses_all_official_fault_definitions() -> None:
    taxonomy = {
        "fault_definitions": {
            "F014-httpError5xx": {},
            "F016-rateLimiting": {},
        }
    }

    assert _fault_taxonomy(taxonomy) == ["httpError5xx", "rateLimiting"]


def test_metric_audit_distinguishes_duplicates_from_conflicts() -> None:
    series = [
        (
            "cpu",
            [(1000, 1.0), (1000, 1.0), (2000, 2.0), (2000, 3.0)],
            {"service": "checkout"},
        )
    ]

    assert _metric_sample_audit(series, source_rows=5) == {
        "source_rows": 5,
        "valid_samples": 4,
        "unique_samples": 2,
        "duplicate_samples": 2,
        "conflicting_timestamps": 1,
    }
