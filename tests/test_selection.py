from semantic_rca_bench.selection import deterministic_rank


def test_deterministic_rank_is_reproducible_and_deduplicates() -> None:
    candidates = ["case-c", "case-a", "case-b", "case-a"]

    first = deterministic_rank(candidates, "measurement-v1")
    second = deterministic_rank(reversed(candidates), "measurement-v1")

    assert first == second
    assert sorted(first) == ["case-a", "case-b", "case-c"]


def test_deterministic_rank_rejects_missing_selection_contract() -> None:
    try:
        deterministic_rank([], "measurement-v1")
    except ValueError as error:
        assert str(error) == "candidate set must not be empty"
    else:
        raise AssertionError("empty candidates must fail")

    try:
        deterministic_rank(["case-a"], "")
    except ValueError as error:
        assert str(error) == "selection seed must not be empty"
    else:
        raise AssertionError("empty seed must fail")
