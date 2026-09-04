from typing import Any

import pytest

from agent_rca_bench.greptimedb.client import GreptimeClient, GreptimeError


def test_query_load_measures_database_rows_and_failures() -> None:
    client = GreptimeClient("http://127.0.0.1:1")

    def successful(_: str, *, database: str | None = None) -> dict[str, Any]:
        return {
            "output": [
                {
                    "records": {
                        "schema": {"column_schemas": [{"name": "value"}]},
                        "rows": [[1], [2]],
                    }
                }
            ]
        }

    client.sql = successful  # type: ignore[method-assign]
    with client.measure_query_load() as load:
        assert client.query_load_snapshot().query_count == 0
        result = client.query("SELECT value", max_rows=1)
        snapshot = client.query_load_snapshot()
        assert snapshot is not None
        assert snapshot.query_count == 1
        assert snapshot.rows_returned == 2

        def failing(_: str, *, database: str | None = None) -> dict[str, Any]:
            raise GreptimeError("query failed")

        client.sql = failing  # type: ignore[method-assign]
        with pytest.raises(GreptimeError):
            client.query("SELECT missing")

    client.close()
    assert result.truncated
    assert len(result.rows) == 1
    assert load.query_count == 2
    assert load.failed_query_count == 1
    assert load.rows_returned == 2
    assert load.max_concurrency == 1
    assert load.query_elapsed_seconds >= 0
    assert client.query_load_snapshot() is None
