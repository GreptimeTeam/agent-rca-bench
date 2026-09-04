from __future__ import annotations

import random
from collections import Counter
from itertools import permutations

from agent_rca_bench.contracts import Visibility


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


def counterbalanced_orders(
    levels: list[Visibility],
    *,
    orderings: int,
) -> list[tuple[Visibility, ...]]:
    """Treatment orders that spread every level evenly across every position.

    `run_orders` rotates one shuffled order, which balances two treatments
    exactly and three treatments not at all: with two repetitions it only ever
    emits two of the six permutations, and both share a middle element, so one
    treatment lands in the middle twice as often as the others.

    This walks all permutations and greedily takes the one that leaves the
    (treatment, position) counts closest together. Deterministic, and the
    resulting spread is asserted by the schedule tests rather than assumed.
    """
    if orderings < 1:
        raise ValueError("orderings must be at least 1")
    if not levels:
        raise ValueError("at least one visibility level is required")
    candidates = sorted(permutations(levels), key=lambda order: [level.value for level in order])
    counts: Counter[tuple[Visibility, int]] = Counter()
    chosen: list[tuple[Visibility, ...]] = []
    for _ in range(orderings):
        best: tuple[tuple[int, int, int], tuple[Visibility, ...], Counter] | None = None
        for index, candidate in enumerate(candidates):
            trial = Counter(counts)
            for position, level in enumerate(candidate):
                trial[(level, position)] += 1
            observed = [
                trial[(level, position)] for level in levels for position in range(len(levels))
            ]
            key = (max(observed) - min(observed), max(observed), index)
            if best is None or key < best[0]:
                best = (key, candidate, trial)
        assert best is not None
        chosen.append(best[1])
        counts = best[2]
    return chosen


def rotate_levels(
    order: tuple[Visibility, ...], levels: list[Visibility], shift: int
) -> tuple[Visibility, ...]:
    """Relabels an order so models do not share one treatment assignment."""
    mapping = {level: levels[(index + shift) % len(levels)] for index, level in enumerate(levels)}
    return tuple(mapping[level] for level in order)


def benchmark_protocol() -> dict[str, object]:
    return {
        "version": 34,
        "treatments": ["split_pillars", "raw", "semantic_graph"],
        "treatment_estimand": "complete-agent-facing-interface-v1",
        "treatment_components": {
            "split_pillars": [
                "telemetry",
                "prometheus-native-http-api",
                "loki-native-http-api",
                "tempo-native-http-api",
            ],
            "raw": [
                "telemetry",
                "ordinary-schema-metadata",
                "read-only-sql",
                "promql-without-metric-metadata",
            ],
            "semantic_graph": [
                "telemetry",
                "ordinary-schema-metadata",
                "read-only-sql",
                "promql",
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
        "evaluator": "diagnosis-and-execution-valid-citation-headline-tri-state-grounding-v29",
        "primary_metrics": "case-median-end-to-end-resource-headline-per-family-v8",
        "repetition_schedule": "counterbalanced-order-with-position-v3",
        "tool_budget": "shared-visible-cap-api-turn-limit-v6",
        "case_role": "explicit-development-or-measurement-v1",
        "case_context": "baseline-availability-v1",
        "database_load": "client-query-boundary-v1",
        "alert": "dataset-native-when-available-v1",
        "agent_runner": "repairable-final-citation-validation-v14",
        "model_usage": "provider-total-input-and-reasoning-output-breakdown-v6",
        "provider_network": "openai-anthropic-environment-domestic-direct-v1",
        "tool_error_feedback": "anthropic-native-flag-openai-json-envelope-v1",
        "diagnosis": "scope-isomorphic-component-edge-node-locus-operation-diagnostic-v7",
        "mechanism_ontology": "case-independent-restart-delay-cpu-memory-host-v4",
        "evidence_oracle": "deterministic-threshold-transition-optional-not-estimable-v1",
        "investigation_prompt": "case-invariant-symmetric-hypothesis-discrimination-v5",
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
        "case_role": "explicit-development-or-measurement-v1",
        "fixture_binding": "source-case-matched-external-fixture-v1",
        "selection": "manifest-ranked-prior-trajectory-excluded-v1",
    }
