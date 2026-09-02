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


def benchmark_protocol() -> dict[str, object]:
    return {
        "version": 32,
        "treatments": ["raw", "semantic_graph"],
        "treatment_estimand": "complete-agent-facing-interface-v1",
        "treatment_components": {
            "raw": ["telemetry", "ordinary-schema-metadata", "read-only-sql"],
            "semantic_graph": [
                "telemetry",
                "ordinary-schema-metadata",
                "read-only-sql",
                "table-semantics",
                "semantic-entities",
                "semantic-relationships",
                "usage-guidance",
                "runtime-recovery-guidance",
                "coverage-snapshot",
            ],
        },
        "table_profile": "greptimedb-mcp-compatible-samples-opt-in-limit-1-declared-service-identity-v4",  # noqa: E501
        "table_catalog": "token-safe-punctuation-aware-semantic-metadata-search-v4",
        "semantic_graph": "bounded-range-window-bucket-complete-column-diagnostics-query-tool-v8",
        "semantic_context": "benchmark-preflight-v1",
        "sql_contract": "greptimedb-read-only-default-200-explicit-1000-v2",
        "citation": "typed-claim-successful-nonmetadata-nontruncated-query-result-v5",
        "evaluator": "diagnosis-and-execution-valid-citation-headline-v28",
        "primary_metrics": "case-median-end-to-end-resource-headline-v7",
        "repetition_schedule": "seeded-rotating-order-with-position-v2",
        "tool_budget": "shared-visible-cap-api-turn-limit-subscription-timeout-v5",
        "case_role": "explicit-development-or-measurement-v1",
        "case_context": "baseline-availability-v1",
        "database_load": "client-query-boundary-v1",
        "alert": "dataset-native-when-available-v1",
        "agent_runner": "repairable-final-citation-validation-v13",
        "model_usage": "provider-total-input-and-reasoning-output-breakdown-v6",
        "provider_network": "openai-anthropic-environment-domestic-direct-v1",
        "tool_error_feedback": "anthropic-native-flag-openai-json-envelope-v1",
        "diagnosis": "scope-isomorphic-locus-operation-diagnostic-v6",
        "mechanism_ontology": "case-independent-restart-delay-cpu-memory-v3",
        "investigation_prompt": "case-invariant-symmetric-hypothesis-discrimination-v4",
    }


def require_current_protocol(version: int) -> None:
    current = int(benchmark_protocol()["version"])
    if version != current:
        raise ValueError(
            f"benchmark protocol v{version} does not match current protocol v{current}"
        )


def discovery_protocol() -> dict[str, object]:
    return {
        "version": 3,
        "treatments": ["raw", "semantic_graph"],
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
        "version": 6,
        "treatments": ["raw", "semantic_graph"],
        "task": "direct-callee-max-error-red-evidence-v1",
        "scorer": "intention-to-treat-canonical-edge-set-v4",
        "prompt": "dual-surface-canonical-edge-evidence-v2",
        "window": "minute-aligned-half-open-v1",
        "tool_budget": 12,
        "api_turn_limit": 22,
        "subscription_turn_limit": None,
        "subscription_process_timeout_seconds": 1800,
        "case_role": "explicit-development-or-measurement-v1",
        "fixture_binding": "source-case-matched-external-fixture-v1",
        "selection": "manifest-ranked-prior-trajectory-excluded-v1",
    }
