from __future__ import annotations

import random

from semantic_rca_bench.contracts import Visibility


def run_orders(
    levels: list[Visibility],
    repetitions: int,
    seed: int,
) -> list[list[Visibility]]:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if not levels:
        raise ValueError("at least one visibility level is required")
    shuffled = levels.copy()
    random.Random(seed).shuffle(shuffled)
    return [
        shuffled[offset:] + shuffled[:offset]
        for repetition in range(repetitions)
        for offset in [repetition % len(shuffled)]
    ]


def benchmark_protocol(version: int | None = None) -> dict[str, object]:
    protocols = {
        25: {
            "version": 25,
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
            "agent_runner": "provider-specific-prompt-cache-and-partial-audit-preserved-v7",
            "model_usage": "typed-cache-inclusion-and-provider-native-breakdown-v3",
            "diagnosis": "affected-component-plus-optional-causal-dependency-v1",
            "investigation_prompt": "provisional-hypothesis-triage-v1",
        },
        26: {
            "version": 26,
            "table_profile": "greptimedb-mcp-compatible-samples-opt-in-limit-1-v2",
            "table_catalog": "token-safe-punctuation-aware-semantic-metadata-search-v4",
            "semantic_graph": "half-open-window-key-deduplicated-query-tool-v6",
            "semantic_context": "benchmark-preflight-v1",
            "sql_contract": "greptimedb-basic-v1",
            "citation": "typed-claim-successful-nonmetadata-query-result-v3",
            "evaluator": "layered-diagnosis-evidence-citation-execution-v10",
            "primary_metrics": "correctness-preserving-case-median-rows-and-calls-v3",
            "repetition_schedule": "seeded-rotating-order-with-position-v2",
            "tool_budget": "shared-visible-cap-api-turn-limit-subscription-timeout-v5",
            "case_role": "explicit-development-or-measurement-v1",
            "case_context": "baseline-availability-v1",
            "database_load": "client-query-boundary-v1",
            "alert": "dataset-native-when-available-v1",
            "agent_runner": "provider-specific-prompt-cache-and-partial-audit-preserved-v7",
            "model_usage": "typed-cache-inclusion-and-provider-native-breakdown-v3",
            "diagnosis": "typed-causal-scope-operation-and-global-mechanism-v2",
            "investigation_prompt": "case-invariant-symmetric-hypothesis-discrimination-v2",
        },
    }
    selected = 26 if version is None else version
    try:
        return dict(protocols[selected])
    except KeyError as error:
        raise ValueError(f"unsupported benchmark protocol version: {selected}") from error


def require_current_protocol(version: int) -> None:
    current = int(benchmark_protocol()["version"])
    if version != current:
        raise ValueError(
            f"benchmark protocol v{version} is retired for new agent execution; "
            f"current protocol is v{current}"
        )


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
