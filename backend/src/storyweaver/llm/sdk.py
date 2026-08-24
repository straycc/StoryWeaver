"""OpenAI Agents SDK 的函数式调用入口。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agents import Agent, ModelSettings, Runner, RunHooks
from agents.models.openai_provider import OpenAIProvider
from agents.run import RunConfig
from pydantic import BaseModel

from .events import LlmEvent, LlmEventSink, LlmEventType


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProviderSettings:
    """OpenAI-compatible Chat Completions Provider 的连接配置。"""

    base_url: str
    model_name: str
    api_key: str | None

    def create_provider(self) -> OpenAIProvider:
        return OpenAIProvider(
            base_url=self.base_url.rstrip("/"),
            api_key=self.api_key,
            use_responses=False,
            strict_feature_validation=False,
        )


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """单个小说 Worker 的 SDK 执行配置。"""

    worker_id: str
    name: str
    instructions: str
    model: Any
    model_settings: ModelSettings
    timeout_seconds: float
    max_turns: int = 1


class WorkerExecutionError(RuntimeError):
    """SDK 调用未返回可用的结构化结果。"""


async def run_structured_worker(
    *,
    settings: WorkerSettings,
    prompt: str,
    output_type: type[BaseModel],
    event_sinks: Sequence[LlmEventSink] = (),
    tracing_enabled: bool = False,
) -> BaseModel:
    """运行一个无工具、Pydantic 结构化输出的 SDK Worker。"""

    await _emit(event_sinks, LlmEventType.RUN_STARTED, settings.worker_id, {
        "max_steps": settings.max_turns,
    })
    started_at = time.perf_counter()
    try:
        hooks = _WorkerHooks(settings.worker_id, event_sinks)
        agent = Agent(
            name=settings.name,
            instructions=(
                settings.instructions
                + "\n只返回一个完整 JSON 对象，不要输出 Markdown、代码围栏或说明文字。"
            ),
            model=settings.model,
            model_settings=settings.model_settings,
        )
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                prompt,
                max_turns=settings.max_turns,
                run_config=RunConfig(
                    tracing_disabled=not tracing_enabled,
                    workflow_name=settings.worker_id,
                ),
                hooks=hooks,
            ),
            timeout=settings.timeout_seconds,
        )
        output = _parse_structured_output(result.final_output, output_type)
        await _emit(event_sinks, LlmEventType.RUN_FINISHED, settings.worker_id, {
            "elapsed_seconds": max(0.0, time.perf_counter() - started_at),
        })
        return output
    except asyncio.CancelledError:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": "模型调用已取消",
        })
        raise
    except Exception as exc:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise WorkerExecutionError(f"{type(exc).__name__}: {exc}") from exc


def _parse_structured_output(raw: Any, output_type: type[BaseModel]) -> BaseModel:
    """在本地完成 JSON 提取与 Pydantic 校验，兼容不支持 response_format 的端点。"""

    if isinstance(raw, output_type):
        return raw
    if isinstance(raw, BaseModel):
        return output_type.model_validate(raw.model_dump())
    if not isinstance(raw, str):
        raise WorkerExecutionError("模型没有返回 JSON 文本")
    value = _extract_json_object(raw)
    try:
        return output_type.model_validate(value)
    except Exception as exc:
        raise WorkerExecutionError(f"结构化输出校验失败：{exc}") from exc


def _extract_json_object(raw: str) -> object:
    """提取模型文本中的最终 JSON 对象。

    DeepSeek 等 OpenAI-compatible 端点未必支持 response_format；即使提示词
    要求纯 JSON，模型仍可能在对象前保留简短推理。因此优先解析全文和代码块，
    最后只接受位于文本末尾的完整 JSON，避免误把过程中的示例当成交付结果。
    """

    candidate = raw.strip()
    parse_error: json.JSONDecodeError | None = None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        parse_error = exc

    fenced_blocks = re.findall(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        re.DOTALL | re.IGNORECASE,
    )
    for block in reversed(fenced_blocks):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError as exc:
            parse_error = exc

    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"\{", candidate))):
        try:
            value, end = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError as exc:
            parse_error = exc
            continue
        if candidate[match.start() + end :].strip():
            continue
        return value

    detail = parse_error or json.JSONDecodeError("未找到 JSON 对象", candidate, 0)
    raise WorkerExecutionError(f"模型 JSON 无法解析：{detail}") from detail


async def run_text_worker(
    *,
    settings: WorkerSettings,
    prompt: str,
    event_sinks: Sequence[LlmEventSink] = (),
    tracing_enabled: bool = False,
) -> str:
    """运行无工具文本 Worker，供聊天和摘要类调用复用。"""

    await _emit(event_sinks, LlmEventType.RUN_STARTED, settings.worker_id, {
        "max_steps": settings.max_turns,
    })
    try:
        result = await asyncio.wait_for(
            Runner.run(
                Agent(
                    name=settings.name,
                    instructions=settings.instructions,
                    model=settings.model,
                    model_settings=settings.model_settings,
                ),
                prompt,
                max_turns=settings.max_turns,
                run_config=RunConfig(
                    tracing_disabled=not tracing_enabled,
                    workflow_name=settings.worker_id,
                ),
                hooks=_WorkerHooks(settings.worker_id, event_sinks),
            ),
            timeout=settings.timeout_seconds,
        )
        output = result.final_output
        if not isinstance(output, str) or not output.strip():
            raise WorkerExecutionError("模型没有返回非空文本")
        await _emit(event_sinks, LlmEventType.RUN_FINISHED, settings.worker_id, {})
        return output.strip()
    except Exception as exc:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise WorkerExecutionError(f"{type(exc).__name__}: {exc}") from exc


class _WorkerHooks(RunHooks[Any]):
    """将 SDK 模型回合投影为脱敏业务事件。"""

    def __init__(
        self,
        worker_id: str,
        event_sinks: Sequence[LlmEventSink],
        *,
        response_kind: str = "final",
    ) -> None:
        self._worker_id = worker_id
        self._event_sinks = event_sinks
        self._turn = 0
        self._started_at: float | None = None
        self._response_kind = response_kind

    async def on_llm_start(
        self,
        context: Any,
        agent: Any,
        system_prompt: Any,
        input_items: Any,
    ) -> None:
        del context, agent, system_prompt, input_items
        self._turn += 1
        self._started_at = time.perf_counter()

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        del context, agent
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        await _emit(self._event_sinks, LlmEventType.MODEL_COMPLETED, self._worker_id, {
            "step": self._turn,
            "tool_call_count": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "elapsed_seconds": max(0.0, time.perf_counter() - (self._started_at or time.perf_counter())),
            "response_kind": self._response_kind,
        })


async def _emit(
    event_sinks: Sequence[LlmEventSink],
    event_type: LlmEventType,
    worker_id: str,
    data: dict[str, Any],
) -> None:
    event = LlmEvent(event_type, worker_id, data)
    for sink in event_sinks:
        try:
            await sink.on_event(event)
        except Exception:
            continue
