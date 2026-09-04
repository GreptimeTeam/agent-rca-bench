from __future__ import annotations

import hashlib
from collections.abc import Iterable


def deterministic_rank(candidates: Iterable[str], seed: str) -> list[str]:
    unique = set(candidates)
    if not unique:
        raise ValueError("candidate set must not be empty")
    if not seed:
        raise ValueError("selection seed must not be empty")
    return sorted(
        unique,
        key=lambda candidate: hashlib.sha256(f"{seed}\0{candidate}".encode()).digest(),
    )
