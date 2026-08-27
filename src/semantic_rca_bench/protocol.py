from __future__ import annotations


def benchmark_protocol() -> dict[str, object]:
    return {
        "version": 11,
        "table_profile": "greptimedb-mcp-compatible-samples-opt-in-limit-1-v2",
        "table_catalog": "predicate-aligned-semantic-metadata-search-v3",
        "semantic_graph": "empty-coverage-gated-windowed-deduplicated-query-tool-v4",
        "semantic_context": "benchmark-preflight-v1",
        "sql_contract": "greptimedb-basic-v1",
        "citation": "compact-run-local-query-id-v1",
        "evaluator": "dataset-taxonomy-exact-fault-type-and-category-v1",
        "repetition_schedule": "seeded-rotating-order-with-position-v2",
        "tool_budget": "fixed-shared-cap-with-exhaustion-recording-v1",
        "case_role": "explicit-development-or-measurement-v1",
        "case_context": "baseline-availability-v1",
        "database_load": "client-query-boundary-v1",
        "alert": "dataset-native-when-available-v1",
    }
