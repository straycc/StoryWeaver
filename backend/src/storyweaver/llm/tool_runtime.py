"""SDK 只读 FunctionTool 的统一运行时治理。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agents.tool import FunctionTool
from pydantic import BaseModel

from ..context.budget import TokenEstimator


ToolCompletedCallback = Callable[
    [str, int, bool, bool, float, str | None, bool, Mapping[str, object]],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class ReadToolSpec:
    """领域只读函数转换为 SDK Tool 所需的最小契约。"""

    name: str
    description: str
    input_model: type[BaseModel]
    execute: Callable[[Mapping[str, Any]], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ToolRuntimePolicy:
    """所有只读工具共享的故障、调用和结果限制。"""

    max_tool_calls: int
    timeout_seconds: float = 8.0
    result_token_limit: int = 800
    transient_attempts: int = 2

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls 必须大于 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if self.result_token_limit < 64:
            raise ValueError("result_token_limit 不能小于 64")
        if self.transient_attempts < 1:
            raise ValueError("transient_attempts 必须大于 0")


def build_read_tools(
    *,
    specs: tuple[ReadToolSpec, ...],
    policy: ToolRuntimePolicy,
    on_completed: ToolCompletedCallback | None = None,
    evidence: list[dict[str, object]] | None = None,
) -> list[FunctionTool]:
    """应用参数校验、超时、临时重试、去重、预算与结果截断。"""

    lock = asyncio.Lock()
    counter = 0
    completed_results: dict[str, str] = {}
    in_flight: dict[str, asyncio.Future[str]] = {}
    estimator = TokenEstimator()

    def build(spec: ReadToolSpec) -> FunctionTool:
        async def invoke(_context: Any, arguments: str) -> str:
            nonlocal counter
            started_at = time.perf_counter()
            try:
                raw_arguments = json.loads(arguments)
                if not isinstance(raw_arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                cache_key = (
                    f"{spec.name}:"
                    + json.dumps(raw_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
            except Exception as exc:
                return await finish_error(
                    spec.name, counter, arguments, {}, exc, started_at,
                    exhausted=False,
                )

            duplicate_result: str | None = None
            duplicate_future: asyncio.Future[str] | None = None
            own_future: asyncio.Future[str] | None = None
            async with lock:
                if cache_key in completed_results:
                    duplicate_result = completed_results[cache_key]
                    call_index = counter
                elif cache_key in in_flight:
                    duplicate_future = in_flight[cache_key]
                    call_index = counter
                elif counter >= policy.max_tool_calls:
                    call_index = counter
                else:
                    counter += 1
                    call_index = counter
                    own_future = asyncio.get_running_loop().create_future()
                    in_flight[cache_key] = own_future

            if duplicate_result is not None or duplicate_future is not None:
                result_text = duplicate_result if duplicate_result is not None else await duplicate_future
                if on_completed is not None:
                    await on_completed(
                        spec.name, call_index, True, False,
                        time.perf_counter() - started_at, None, True, raw_arguments,
                    )
                return result_text
            if own_future is None:
                result_text = json.dumps(
                    {"error": "工具调用预算已耗尽", "status": "budget_exhausted"},
                    ensure_ascii=False,
                )
                if on_completed is not None:
                    await on_completed(
                        spec.name, call_index, True, True,
                        time.perf_counter() - started_at, None, False, raw_arguments,
                    )
                return result_text

            try:
                validated = spec.input_model.model_validate(raw_arguments)
                result = await _execute_with_transient_retry(
                    spec.execute,
                    validated.model_dump(),
                    timeout_seconds=policy.timeout_seconds,
                    attempts=policy.transient_attempts,
                )
                result, truncated = _limit_result(
                    result,
                    token_limit=policy.result_token_limit,
                    estimator=estimator,
                )
                status = _result_status(result)
                item = {
                    "tool_name": spec.name,
                    "arguments": validated.model_dump(),
                    "raw_arguments": arguments,
                    "result": result,
                    "status": status,
                    "succeeded": True,
                    "truncated": truncated,
                }
                if evidence is not None:
                    evidence.append(item)
                serialized = json.dumps(result, ensure_ascii=False, default=str)
                async with lock:
                    completed_results[cache_key] = serialized
                    in_flight.pop(cache_key, None)
                    if not own_future.done():
                        own_future.set_result(serialized)
                if on_completed is not None:
                    await on_completed(
                        spec.name, call_index, True, False,
                        time.perf_counter() - started_at, None, False,
                        validated.model_dump(),
                    )
                return serialized
            except Exception as exc:
                result_text = await finish_error(
                    spec.name, call_index, arguments, raw_arguments, exc,
                    started_at, exhausted=False,
                )
                async with lock:
                    completed_results[cache_key] = result_text
                    in_flight.pop(cache_key, None)
                    if not own_future.done():
                        own_future.set_result(result_text)
                return result_text

        async def finish_error(
            name: str,
            call_index: int,
            raw: str,
            arguments: Mapping[str, object],
            error: BaseException,
            started_at: float,
            *,
            exhausted: bool,
        ) -> str:
            detail = f"{type(error).__name__}: {error}"
            status = "timeout" if isinstance(error, TimeoutError) else "invalid_arguments"
            if not isinstance(error, (ValueError, TypeError, TimeoutError)):
                status = "temporary_failure" if isinstance(error, (ConnectionError, OSError)) else "error"
            result = {"error": detail, "status": status}
            if evidence is not None:
                evidence.append({
                    "tool_name": name,
                    "arguments": dict(arguments),
                    "raw_arguments": raw,
                    "result": result,
                    "status": status,
                    "succeeded": False,
                    "truncated": False,
                })
            if on_completed is not None:
                await on_completed(
                    name, call_index, False, exhausted,
                    time.perf_counter() - started_at, detail, False, arguments,
                )
            return json.dumps(result, ensure_ascii=False)

        schema = spec.input_model.model_json_schema()
        schema.pop("title", None)
        return FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema=schema,
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    return [build(spec) for spec in specs]


async def _execute_with_transient_retry(
    execute: Callable[[Mapping[str, Any]], Awaitable[object]],
    arguments: Mapping[str, Any],
    *,
    timeout_seconds: float,
    attempts: int,
) -> object:
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(execute(arguments), timeout=timeout_seconds)
        except (TimeoutError, ConnectionError, OSError):
            if attempt >= attempts:
                raise
    raise AssertionError("临时重试循环未返回")


def _limit_result(
    result: object,
    *,
    token_limit: int,
    estimator: TokenEstimator,
) -> tuple[object, bool]:
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if estimator.estimate(serialized) <= token_limit:
        return result, bool(result.get("truncated", False)) if isinstance(result, Mapping) else False
    wrapper = {
        "content_excerpt": "",
        "truncated": True,
        "notice": "工具结果超过单次预算，仅返回摘录",
    }
    wrapper_tokens = estimator.estimate(json.dumps(wrapper, ensure_ascii=False))
    maximum_chars = max(0, (token_limit - wrapper_tokens) * 2)
    wrapper["content_excerpt"] = serialized[:maximum_chars]
    while maximum_chars > 0 and estimator.estimate(
        json.dumps(wrapper, ensure_ascii=False)
    ) > token_limit:
        maximum_chars -= 1
        wrapper["content_excerpt"] = serialized[:maximum_chars]
    return wrapper, True


def _result_status(result: object) -> str:
    if isinstance(result, Mapping):
        if result.get("matched") is False:
            return "empty"
        collections = [value for value in result.values() if isinstance(value, (list, tuple))]
        if collections and all(not value for value in collections):
            return "empty"
    return "success"
