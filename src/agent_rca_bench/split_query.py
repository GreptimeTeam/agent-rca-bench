from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_rca_bench.contracts import DatabaseLoad

DEFAULT_NATIVE_MAX_ITEMS = 200
MAX_NATIVE_MAX_ITEMS = 1000


class NativeQueryError(RuntimeError):
    pass


class NativeQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str
    backend: Literal["prometheus", "loki", "tempo"]
    operation: str
    columns: list[str]
    rows: list[list[object]]
    elapsed_seconds: float
    returned_items: int = Field(ge=0)
    result_bytes: int = Field(ge=0)
    truncated: bool = False


class SplitQueryGateway:
    def __init__(
        self,
        *,
        prometheus_endpoint: str,
        loki_endpoint: str,
        tempo_endpoint: str,
        timeout: float = 30.0,
    ) -> None:
        self.endpoints = {
            "prometheus": prometheus_endpoint.rstrip("/"),
            "loki": loki_endpoint.rstrip("/"),
            "tempo": tempo_endpoint.rstrip("/"),
        }
        self.http = httpx.Client(timeout=timeout, trust_env=False)
        self.client = self
        self._load_lock = Lock()
        self._load: DatabaseLoad | None = None
        self._active_queries = 0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> SplitQueryGateway:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def measure_query_load(self) -> Iterator[DatabaseLoad]:
        with self._load_lock:
            if self._load is not None:
                raise RuntimeError("query load measurement is already active")
            # `rows_returned` stays None here: this arm reads samples, entries
            # and traces, which are not the unit the endpoint counts.
            self._load = DatabaseLoad(rows_returned=None)
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

    def execute_tool(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        limit = max_items(arguments)
        if tool_name == "query_metrics":
            result = self._query_prometheus(arguments, limit)
        elif tool_name == "query_logs":
            result = self._query_loki(arguments, limit)
        elif tool_name == "query_traces":
            result = self._query_tempo(arguments, limit)
        else:
            raise NativeQueryError(f"unknown split-pillar tool: {tool_name}")
        return result.model_dump(mode="json")

    def _query_prometheus(
        self,
        arguments: dict[str, object],
        max_items: int,
    ) -> NativeQueryResult:
        operation, path, params = prometheus_query_plan(arguments)
        payload, elapsed = self._request("prometheus", path, params)
        columns, rows = prometheus_rows(operation, payload)
        return native_result("prometheus", operation, columns, rows, elapsed, max_items)

    def _query_loki(
        self,
        arguments: dict[str, object],
        max_items: int,
    ) -> NativeQueryResult:
        operation = str(arguments.get("operation") or "")
        params: dict[str, str] = {}
        if operation == "query_range":
            params = {
                "query": _required_text(arguments, "query"),
                "start": _required_text(arguments, "start"),
                "end": _required_text(arguments, "end"),
                # One more than the cap so a backend that honours `limit`
                # can still reveal that the result was cut. Asking for exactly
                # `max_items` makes `truncated` unreachable.
                "limit": str(max_items + 1),
                "direction": str(arguments.get("direction") or "backward"),
            }
            _copy_argument(arguments, params, "step")
            path = "/loki/api/v1/query_range"
        elif operation == "labels":
            path = "/loki/api/v1/labels"
            _copy_time_arguments(arguments, params)
        elif operation == "label_values":
            label = quote(_required_text(arguments, "label"), safe="")
            path = f"/loki/api/v1/label/{label}/values"
            _copy_time_arguments(arguments, params)
        else:
            raise NativeQueryError(f"unsupported Loki operation: {operation}")
        payload, elapsed = self._request("loki", path, params)
        columns, rows = loki_rows(operation, payload)
        return native_result("loki", operation, columns, rows, elapsed, max_items)

    def _query_tempo(
        self,
        arguments: dict[str, object],
        max_items: int,
    ) -> NativeQueryResult:
        operation = str(arguments.get("operation") or "")
        params: dict[str, str] = {}
        if operation == "search":
            params = {
                "q": _required_text(arguments, "query"),
                "start": _tempo_epoch_seconds(arguments, "start"),
                "end": _tempo_epoch_seconds(arguments, "end"),
                # One more than the cap, for the same reason as Loki.
                "limit": str(max_items + 1),
            }
            path = "/api/search"
        elif operation == "get_trace":
            trace_id = quote(_required_text(arguments, "trace_id"), safe="")
            path = f"/api/traces/{trace_id}"
            _copy_tempo_time_arguments(arguments, params)
        elif operation == "tags":
            path = "/api/v2/search/tags"
            _copy_tempo_time_arguments(arguments, params)
        elif operation == "tag_values":
            tag = quote(_required_text(arguments, "tag"), safe="")
            path = f"/api/v2/search/tag/{tag}/values"
            _copy_tempo_time_arguments(arguments, params)
        elif operation == "metrics_range":
            params = {
                "q": _required_text(arguments, "query"),
                "start": _required_text(arguments, "start"),
                "end": _required_text(arguments, "end"),
                "step": _required_text(arguments, "step"),
            }
            path = "/api/metrics/query_range"
        else:
            raise NativeQueryError(f"unsupported Tempo operation: {operation}")
        payload, elapsed = self._request("tempo", path, params)
        columns, rows = tempo_rows(operation, payload)
        return native_result("tempo", operation, columns, rows, elapsed, max_items)

    def _request(
        self,
        backend: Literal["prometheus", "loki", "tempo"],
        path: str,
        params: dict[str, str],
    ) -> tuple[dict[str, object], float]:
        started = time.monotonic()
        self._query_started()
        try:
            response = self.http.get(f"{self.endpoints[backend]}{path}", params=params)
            if response.is_error:
                detail = response.text.strip()[:2000] or response.reason_phrase
                raise NativeQueryError(f"{backend} query failed ({response.status_code}): {detail}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise NativeQueryError(f"{backend} response is not an object")
            if payload.get("status") == "error":
                raise NativeQueryError(
                    f"{backend} query failed: {payload.get('errorType')}: {payload.get('error')}"
                )
        except Exception:
            self._query_finished(started, failed=True)
            raise
        elapsed = self._query_finished(started, failed=False)
        return payload, elapsed

    def _query_started(self) -> None:
        with self._load_lock:
            if self._load is None:
                return
            self._active_queries += 1
            self._load.query_count += 1
            self._load.max_concurrency = max(self._load.max_concurrency, self._active_queries)

    def _query_finished(self, started: float, *, failed: bool) -> float:
        elapsed = time.monotonic() - started
        with self._load_lock:
            if self._load is not None:
                self._active_queries -= 1
                self._load.query_elapsed_seconds += elapsed
                if failed:
                    self._load.failed_query_count += 1
        return elapsed


def prometheus_query_plan(arguments: dict[str, object]) -> tuple[str, str, dict[str, str]]:
    """The operation, HTTP path and params for one `query_metrics` call.

    Shared so the split arm and GreptimeDB's Prometheus-compatible endpoint
    answer the identical request; only the base URL differs.
    """
    operation = str(arguments.get("operation") or "")
    allowed_arguments = {
        "query": {"operation", "query", "time", "max_items"},
        "query_range": {"operation", "query", "start", "end", "step", "max_items"},
        "metadata": {"operation", "metric", "max_items"},
        "labels": {"operation", "match", "start", "end", "max_items"},
        "label_values": {
            "operation",
            "label",
            "match",
            "start",
            "end",
            "max_items",
        },
        "series": {"operation", "match", "start", "end", "max_items"},
    }
    allowed = allowed_arguments.get(operation)
    if allowed is None:
        raise NativeQueryError(f"unsupported Prometheus operation: {operation}")
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        fields = ", ".join(unexpected)
        raise NativeQueryError(f"unsupported arguments for Prometheus {operation}: {fields}")
    params: dict[str, str] = {}
    if operation in {"query", "query_range"}:
        params["query"] = _required_text(arguments, "query")
    if operation == "query":
        _copy_argument(arguments, params, "time")
        path = "/api/v1/query"
    elif operation == "query_range":
        for field in ("start", "end", "step"):
            params[field] = _required_text(arguments, field)
        path = "/api/v1/query_range"
    elif operation == "metadata":
        path = "/api/v1/metadata"
        _copy_argument(arguments, params, "metric")
    elif operation == "labels":
        path = "/api/v1/labels"
        _copy_prometheus_match(arguments, params)
        _copy_time_arguments(arguments, params)
    elif operation == "label_values":
        label = quote(_required_text(arguments, "label"), safe="")
        path = f"/api/v1/label/{label}/values"
        _copy_prometheus_match(arguments, params)
        _copy_time_arguments(arguments, params)
    elif operation == "series":
        path = "/api/v1/series"
        match = arguments.get("match")
        if not isinstance(match, str) or not match:
            raise NativeQueryError("Prometheus series requires one non-empty match selector")
        params["match[]"] = match
        _copy_time_arguments(arguments, params)
    return operation, path, params


METRIC_OPERATIONS = ("query", "query_range", "metadata", "labels", "label_values", "series")

# GreptimeDB answers `/api/v1/metadata` from `greptime.semantic.metric.type`, so
# the operation exposes declared metric semantics. The raw arm is denied
# `information_schema.table_semantics` for the same reason and must not reach the
# same facts through PromQL instead.
METRIC_OPERATIONS_WITHOUT_SEMANTICS = tuple(
    operation for operation in METRIC_OPERATIONS if operation != "metadata"
)


def metrics_query_tool(
    *,
    native_stack: bool,
    operations: tuple[str, ...] = METRIC_OPERATIONS,
) -> dict[str, object]:
    """The `query_metrics` tool, worded for whichever store answers PromQL."""
    store = (
        "the Prometheus metrics store with its native HTTP API"
        if native_stack
        else "the metric tables with PromQL through GreptimeDB's Prometheus-compatible API"
    )
    discovery = ", ".join(item for item in operations if item not in {"query", "query_range"})
    return {
        "name": "query_metrics",
        "description": (
            f"Query {store}. Use PromQL query or query_range for values, and {discovery} for "
            "discovery. query uses time; query_range uses start, end, and step. series requires "
            "match. label_values requires label and accepts match to restrict the series. "
            "Results are capped and explicitly marked when truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": list(operations)},
                "query": {
                    "type": "string",
                    "description": "PromQL expression for query or query_range.",
                },
                "time": {
                    "type": "string",
                    "description": "Unix seconds or RFC 3339 evaluation time for query.",
                },
                "step": {"type": "string", "description": "Step required by query_range."},
                "metric": {
                    "type": "string",
                    "description": "Optional metric filter for metadata.",
                },
                "label": {
                    "type": "string",
                    "description": "Label name required by label_values.",
                },
                "match": {
                    "type": "string",
                    "description": (
                        "One Prometheus series selector; required by series and available to "
                        "restrict labels or label_values."
                    ),
                },
                **_COMMON_TIME_PROPERTIES,
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    }


_COMMON_TIME_PROPERTIES = {
    "start": {"type": "string", "description": "Unix seconds or RFC 3339 start."},
    "end": {"type": "string", "description": "Unix seconds or RFC 3339 end."},
    "max_items": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_NATIVE_MAX_ITEMS,
        "default": DEFAULT_NATIVE_MAX_ITEMS,
    },
}


def split_investigation_tools() -> list[dict[str, object]]:
    common_time = _COMMON_TIME_PROPERTIES
    tools = [
        metrics_query_tool(native_stack=True),
        {
            "name": "query_logs",
            "description": (
                "Query the Loki log store with LogQL query_range or discover labels and label "
                "values. Loki range queries include the start and exclude the end. Results are "
                "capped and explicitly marked when truncated."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["query_range", "labels", "label_values"],
                    },
                    "query": {"type": "string"},
                    "label": {"type": "string"},
                    "step": {"type": "string"},
                    "direction": {"type": "string", "enum": ["forward", "backward"]},
                    **common_time,
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
        {
            "name": "query_traces",
            "description": (
                "Query the Tempo trace store. Use TraceQL search for traces, get_trace for all "
                "spans in one trace, tags or tag_values for discovery, and metrics_range for "
                "TraceQL metrics. Results are capped and explicitly marked when truncated."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["search", "get_trace", "tags", "tag_values", "metrics_range"],
                    },
                    "query": {"type": "string"},
                    "trace_id": {"type": "string"},
                    "tag": {"type": "string"},
                    "step": {"type": "string"},
                    **common_time,
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
    ]

    tools = deepcopy(tools)
    for tool in tools:
        tool["input_schema"]["properties"]["max_items"]["description"] = (
            "Maximum items returned for this query. Raise it only when a complete "
            "result cannot be obtained with aggregation or narrower filters."
        )
    return tools


def native_result(
    backend: Literal["prometheus", "loki", "tempo"],
    operation: str,
    columns: list[str],
    rows: list[list[object]],
    elapsed: float,
    max_items: int,
) -> NativeQueryResult:
    truncated = len(rows) > max_items
    returned = rows[:max_items]
    payload = json.dumps(
        {"columns": columns, "rows": returned},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return NativeQueryResult(
        query_id=uuid.uuid4().hex,
        backend=backend,
        operation=operation,
        columns=columns,
        rows=returned,
        elapsed_seconds=elapsed,
        returned_items=len(returned),
        result_bytes=len(payload),
        truncated=truncated,
    )


def _spread_labels(
    rows: list[list[object]],
    sample_columns: list[str],
) -> tuple[list[str], list[list[object]]]:
    """Expand label maps into columns while preserving one row per sample."""
    names = list(dict.fromkeys(name for row in rows for name in row[0]))
    taken = set(names)
    columns = []
    for column in sample_columns:
        # Preserve label names; escape colliding sample columns instead.
        while column in taken:
            column += "_"
        taken.add(column)
        columns.append(column)
    return [*names, *columns], [[*(row[0].get(name) for name in names), *row[1:]] for row in rows]


def prometheus_rows(
    operation: str,
    payload: dict[str, object],
) -> tuple[list[str], list[list[object]]]:
    data = payload.get("data")
    if operation in {"labels", "label_values"}:
        values = data if isinstance(data, list) else []
        return ["value"], [[value] for value in values]
    if operation == "metadata":
        rows = []
        if isinstance(data, dict):
            for metric, entries in data.items():
                if isinstance(entries, list):
                    rows.extend([[metric, entry] for entry in entries])
        return ["metric", "metadata"], rows
    if operation == "series":
        values = data if isinstance(data, list) else []
        return _spread_labels([[value] for value in values if isinstance(value, dict)], [])
    if not isinstance(data, dict):
        return _spread_labels([], ["timestamp", "value"])
    result = data.get("result")
    result_type = data.get("resultType")
    rows: list[list[object]] = []
    if result_type in {"scalar", "string"} and isinstance(result, list) and len(result) >= 2:
        rows.append([{}, result[0], result[1]])
    elif isinstance(result, list):
        for series in result:
            if not isinstance(series, dict):
                continue
            labels = series.get("metric") if isinstance(series.get("metric"), dict) else {}
            samples = series.get("values")
            if isinstance(samples, list):
                rows.extend(
                    [labels, sample[0], sample[1]]
                    for sample in samples
                    if isinstance(sample, list) and len(sample) >= 2
                )
            else:
                sample = series.get("value")
                if isinstance(sample, list) and len(sample) >= 2:
                    rows.append([labels, sample[0], sample[1]])
    return _spread_labels(rows, ["timestamp", "value"])


def loki_rows(
    operation: str,
    payload: dict[str, object],
) -> tuple[list[str], list[list[object]]]:
    data = payload.get("data")
    if operation in {"labels", "label_values"}:
        values = data if isinstance(data, list) else []
        return ["value"], [[value] for value in values]
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        return _spread_labels([], ["timestamp", "value"])
    result_type = data.get("resultType")
    rows: list[list[object]] = []
    for series in data["result"]:
        if not isinstance(series, dict):
            continue
        labels = series.get("stream") if result_type == "streams" else series.get("metric")
        labels = labels if isinstance(labels, dict) else {}
        samples = series.get("values")
        if isinstance(samples, list):
            # Loki returns per-entry structured metadata as a third element.
            # `trace_id` and `span_id` travel there, and GreptimeDB holds them as
            # ordinary columns, so dropping them would leave the split arm unable
            # to pivot from a log line to its trace.
            rows.extend(
                [
                    {
                        **labels,
                        **(sample[2] if len(sample) > 2 and isinstance(sample[2], dict) else {}),
                    },
                    sample[0],
                    sample[1],
                ]
                for sample in samples
                if isinstance(sample, list) and len(sample) >= 2
            )
    return _spread_labels(rows, ["timestamp", "line" if result_type == "streams" else "value"])


def tempo_rows(
    operation: str,
    payload: dict[str, object],
) -> tuple[list[str], list[list[object]]]:
    if operation == "search":
        traces = payload.get("traces")
        return ["trace"], [[trace] for trace in traces] if isinstance(traces, list) else []
    if operation == "get_trace":
        return ["trace"], [[payload]]
    if operation == "tags":
        names = payload.get("tagNames") or payload.get("scopes")
        return ["tag"], [[name] for name in names] if isinstance(names, list) else []
    if operation == "tag_values":
        values = payload.get("tagValues") or payload.get("values")
        return ["value"], [[value] for value in values] if isinstance(values, list) else []
    series = payload.get("series")
    return ["series"], [[item] for item in series] if isinstance(series, list) else []


def max_items(arguments: dict[str, object]) -> int:
    value = arguments.get("max_items", DEFAULT_NATIVE_MAX_ITEMS)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NativeQueryError("max_items must be an integer")
    if not 1 <= value <= MAX_NATIVE_MAX_ITEMS:
        raise NativeQueryError(f"max_items must be between 1 and {MAX_NATIVE_MAX_ITEMS}")
    return value


def _required_text(arguments: dict[str, object], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise NativeQueryError(f"{field} is required")
    return value


def _copy_tempo_time_arguments(
    arguments: dict[str, object],
    params: dict[str, str],
) -> None:
    for field in ("start", "end"):
        if field in arguments:
            params[field] = _tempo_epoch_seconds(arguments, field)


def _tempo_epoch_seconds(arguments: dict[str, object], field: str) -> str:
    value = _required_text(arguments, field)
    try:
        float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise NativeQueryError(
                f"Tempo {field} must be Unix seconds or an RFC 3339 timestamp"
            ) from error
        if parsed.tzinfo is None:
            raise NativeQueryError(
                f"Tempo {field} RFC 3339 timestamp must include an offset"
            ) from None
        return str(int(parsed.timestamp()))
    return value


def _copy_argument(
    arguments: dict[str, object],
    params: dict[str, str],
    field: str,
) -> None:
    value = arguments.get(field)
    if value not in (None, ""):
        params[field] = str(value)


def _copy_prometheus_match(arguments: dict[str, object], params: dict[str, str]) -> None:
    value = arguments.get("match")
    if value not in (None, ""):
        params["match[]"] = _required_text(arguments, "match")


def _copy_time_arguments(arguments: dict[str, object], params: dict[str, str]) -> None:
    for field in ("start", "end"):
        _copy_argument(arguments, params, field)
