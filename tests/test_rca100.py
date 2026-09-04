from datetime import UTC, datetime

from agent_rca_bench.datasets.rca100 import (
    _component_contract,
    _fault_taxonomy,
    _iso_to_nanoseconds,
    _metric_sample_audit,
)


def test_conflicting_structured_component_labels_disable_component_scoring() -> None:
    scoreable, alternatives = _component_contract(
        "cart-64944cd445-8pbgx",
        '{"outcome":{"target_entities":[{"entity_name":"cart"}]}}',
    )

    assert not scoreable
    assert alternatives == ["cart"]


def test_matching_structured_component_labels_remain_scoreable() -> None:
    scoreable, alternatives = _component_contract(
        "payment",
        '{"outcome":{"target_entities":[{"entity_name":"payment"}]}}',
    )

    assert scoreable
    assert alternatives == []


def test_one_matching_target_does_not_hide_a_conflicting_target() -> None:
    scoreable, alternatives = _component_contract(
        "payment",
        '{"outcome":{"target_entities":[{"entity_name":"payment"},{"entity_name":"redis"}]}}',
    )

    assert not scoreable
    assert alternatives == ["redis"]


def test_iso_timestamp_preserves_nanoseconds_and_offset() -> None:
    assert _iso_to_nanoseconds("2026-04-25T13:20:23.123456789+08:00") == (
        int(datetime(2026, 4, 25, 5, 20, 23, tzinfo=UTC).timestamp()) * 1_000_000_000 + 123_456_789
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
