from __future__ import annotations


def benchmark_protocol() -> dict[str, object]:
    return {
        "version": 23,
        "table_profile": "greptimedb-mcp-compatible-samples-opt-in-limit-1-v2",
        "table_catalog": "token-safe-punctuation-aware-semantic-metadata-search-v4",
        "semantic_graph": "half-open-window-key-deduplicated-query-tool-v6",
        "semantic_context": "benchmark-preflight-v1",
        "sql_contract": "greptimedb-basic-v1",
        "citation": "unique-successful-nonmetadata-query-result-v2",
        "evaluator": "canonical-valid-completion-and-rejection-disposition-v9",
        "primary_metrics": "case-median-rows-and-calls-holm-sign-test-v2",
        "repetition_schedule": "seeded-rotating-order-with-position-v2",
        "tool_budget": "shared-visible-cap-api-turn-limit-subscription-timeout-v5",
        "case_role": "explicit-development-or-measurement-v1",
        "case_context": "baseline-availability-v1",
        "database_load": "client-query-boundary-v1",
        "alert": "dataset-native-when-available-v1",
        "agent_runner": "isolated-provider-environment-partial-audit-preserved-v6",
        "model_usage": "typed-cache-inclusion-and-verified-breakdown-v2",
        "diagnosis": "affected-component-plus-optional-causal-dependency-v1",
    }


def discovery_protocol() -> dict[str, object]:
    return {
        "version": 2,
        "treatments": ["raw", "table_semantics"],
        "task": "frozen-table-localization-and-temporal-evidence-v1",
        "scorer": "current-database-qualified-cited-query-canonical-result-v2",
        "tool_budget": 12,
        "api_turn_limit": 22,
        "subscription_turn_limit": None,
        "subscription_process_timeout_seconds": 1800,
        "catalog": "token-safe-io-direction-aware-v1",
        "prompt": "case-preserving-double-quoted-identifiers-v2",
        "case_role": "explicit-development-or-measurement-v1",
        "fixture_binding": "source-case-matched-external-fixture-v1",
    }


def graph_protocol() -> dict[str, object]:
    return {
        "version": 3,
        "treatments": ["table_semantics", "semantic_graph"],
        "task": "direct-callee-max-error-red-evidence-v1",
        "scorer": "result-proven-destination-type-canonical-edge-set-v2",
        "window": "minute-aligned-half-open-v1",
        "tool_budget": 12,
        "api_turn_limit": 22,
        "subscription_turn_limit": None,
        "subscription_process_timeout_seconds": 1800,
        "case_role": "explicit-development-or-measurement-v1",
        "fixture_binding": "source-case-matched-external-fixture-v1",
        "selection": "manifest-ranked-prior-trajectory-excluded-v1",
    }
