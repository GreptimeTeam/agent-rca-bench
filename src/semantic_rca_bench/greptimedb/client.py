from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any

import httpx

from semantic_rca_bench.contracts import DatabaseLoad, QueryResult


class GreptimeError(RuntimeError):
    pass


class GreptimeClient:
    def __init__(
        self,
        endpoint: str,
        database: str = "public",
        *,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.database = database
        self.http = httpx.Client(timeout=timeout, trust_env=False)
        self._load_lock = Lock()
        self._active_queries = 0
        self._load: DatabaseLoad | None = None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> GreptimeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def sql(self, statement: str, *, database: str | None = None) -> dict[str, Any]:
        response = self.http.post(
            f"{self.endpoint}/v1/sql",
            params={"db": database or self.database},
            data={"sql": statement},
        )
        if response.is_error:
            raise GreptimeError(f"SQL failed ({response.status_code}): {response.text[:2000]}")
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise GreptimeError(f"SQL failed: {payload}")
        return payload

    def query(self, statement: str, *, max_rows: int | None = 200) -> QueryResult:
        started = time.monotonic()
        self._query_started()
        try:
            payload = self.sql(statement)
        except Exception:
            self._query_finished(started, rows_returned=0, failed=True)
            raise
        output = payload.get("output") or []
        if not output:
            elapsed = self._query_finished(started, rows_returned=0, failed=False)
            return QueryResult(
                query_id=uuid.uuid4().hex,
                columns=[],
                rows=[],
                elapsed_seconds=elapsed,
            )
        records = output[0].get("records") or {}
        schema = records.get("schema") or {}
        column_schemas = schema.get("column_schemas") or schema.get("columns") or []
        columns = [
            str(column.get("name") or column.get("column_name") or f"column_{index}")
            for index, column in enumerate(column_schemas)
        ]
        all_rows = records.get("rows") or []
        elapsed = self._query_finished(started, rows_returned=len(all_rows), failed=False)
        if not columns and all_rows:
            columns = [f"column_{index}" for index in range(len(all_rows[0]))]
        rows = all_rows if max_rows is None else all_rows[:max_rows]
        return QueryResult(
            query_id=uuid.uuid4().hex,
            columns=columns,
            rows=rows,
            elapsed_seconds=elapsed,
            truncated=max_rows is not None and len(all_rows) > max_rows,
        )

    @contextmanager
    def measure_query_load(self) -> Iterator[DatabaseLoad]:
        with self._load_lock:
            if self._load is not None:
                raise RuntimeError("query load measurement is already active")
            self._load = DatabaseLoad()
            self._active_queries = 0
            load = self._load
        try:
            yield load
        finally:
            with self._load_lock:
                self._load = None
                self._active_queries = 0

    def query_load_snapshot(self) -> DatabaseLoad | None:
        with self._load_lock:
            return self._load.model_copy(deep=True) if self._load is not None else None

    def _query_started(self) -> None:
        with self._load_lock:
            if self._load is None:
                return
            self._active_queries += 1
            self._load.query_count += 1
            self._load.max_concurrency = max(
                self._load.max_concurrency,
                self._active_queries,
            )

    def _query_finished(self, started: float, *, rows_returned: int, failed: bool) -> float:
        elapsed = time.monotonic() - started
        with self._load_lock:
            if self._load is not None:
                self._active_queries -= 1
                self._load.rows_returned += rows_returned
                self._load.query_elapsed_seconds += elapsed
                if failed:
                    self._load.failed_query_count += 1
        return elapsed

    def create_database(self, database: str) -> None:
        if not database.replace("_", "").isalnum():
            raise ValueError(f"invalid database name: {database}")
        self.sql(f'CREATE DATABASE IF NOT EXISTS "{database}"', database="public")

    def status(self) -> dict[str, Any]:
        response = self.http.get(f"{self.endpoint}/status")
        if response.is_error:
            raise GreptimeError(f"status failed ({response.status_code}): {response.text[:2000]}")
        return response.json()

    def post_bytes(
        self,
        path: str,
        body: bytes,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> httpx.Response:
        response = self.http.post(
            f"{self.endpoint}{path}",
            params=params,
            headers=headers,
            content=body,
            timeout=timeout,
        )
        if response.is_error:
            raise GreptimeError(
                f"ingestion failed for {path} ({response.status_code}): {response.text[:2000]}"
            )
        return response
