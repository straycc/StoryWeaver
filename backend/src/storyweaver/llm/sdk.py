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
from ..observability import ModelFailureDiagnosticWriter


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
    diagnostic_writer: ModelFailureDiagnosticWriter | None = None


class WorkerExecutionError(RuntimeError):
    """SDK 调用未返回可用的结构化结果。"""


class StructuredOutputError(WorkerExecutionError):
    """保留无法解析的原始输出，供一次无工具修复与诊断使用。"""

    def __init__(self, message: str, *, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


async def run_structured_worker(
    *,
    settings: WorkerSettings,
    prompt: str,
    output_type: type[BaseModel],
    event_sinks: Sequence[LlmEventSink] = (),
    tracing_enabled: bool = False,
    stream_text_field: str | None = None,
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
                + "\n输出必须符合以下 JSON Schema；这是返回值结构，不是需要原样输出的内容：\n"
                + json.dumps(output_type.model_json_schema(), ensure_ascii=False)
            ),
            model=settings.model,
            model_settings=settings.model_settings,
        )
        run_config = RunConfig(
            tracing_disabled=not tracing_enabled,
            workflow_name=settings.worker_id,
        )
        if stream_text_field is None:
            result = await asyncio.wait_for(
                Runner.run(agent, prompt, max_turns=settings.max_turns,
                           run_config=run_config, hooks=hooks),
                timeout=settings.timeout_seconds,
            )
        else:
            result = await asyncio.wait_for(
                _consume_structured_stream(
                    agent=agent, prompt=prompt, settings=settings, hooks=hooks,
                    run_config=run_config, event_sinks=event_sinks,
                    text_field=stream_text_field,
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
    except StructuredOutputError as exc:
        _write_structured_diagnostic(settings, error=exc, raw_output=exc.raw_output)
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": f"{type(exc).__name__}: {exc}",
        })
        # 保留 raw_output，供 BaseNovelAgent 的下一次无工具修复调用使用。
        raise
    except Exception as exc:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise WorkerExecutionError(f"{type(exc).__name__}: {exc}") from exc


async def _consume_structured_stream(
    *,
    agent: Agent[Any],
    prompt: str,
    settings: WorkerSettings,
    hooks: RunHooks[Any],
    run_config: RunConfig,
    event_sinks: Sequence[LlmEventSink],
    text_field: str,
) -> Any:
    """从结构化 JSON 中仅投影一个字符串字段的可见预览。

    Provider 的真实输出仍完整交给 `_parse_structured_output` 与 Pydantic；投影器
    只是 UI 预览，绝不参与提交决策。这样正文很长时可边生成边阅读，也不会让
    前端接收到 ``{\"chapter_number\": ...`` 一类内部协议文本。
    """

    projector = _JsonStringFieldProjector(text_field)
    result = Runner.run_streamed(
        agent, prompt, max_turns=settings.max_turns,
        run_config=run_config, hooks=hooks,
    )
    await _emit(event_sinks, LlmEventType.STREAM_STARTED, settings.worker_id, {
        "field": text_field,
    })
    async for event in result.stream_events():
        data = getattr(event, "data", None)
        if (
            getattr(event, "type", None) == "raw_response_event"
            and getattr(data, "type", None) == "response.output_text.delta"
        ):
            delta = getattr(data, "delta", None)
            if isinstance(delta, str):
                preview = projector.feed(delta)
                if preview:
                    await _emit(event_sinks, LlmEventType.TEXT_DELTA, settings.worker_id, {
                        "field": text_field,
                        "delta": preview,
                        "preview": True,
                    })
    await _emit(event_sinks, LlmEventType.STREAM_COMPLETED, settings.worker_id, {
        "field": text_field,
    })
    return result


class _JsonStringFieldProjector:
    """跨 SDK 增量切片提取 JSON 字符串字段，供非权威文本预览使用。"""

    def __init__(self, field: str) -> None:
        self._marker = re.compile(rf'"{re.escape(field)}"\s*:\s*"')
        self._buffer = ""
        self._started = False
        self._finished = False
        self._escaped = False
        self._unicode: str | None = None

    def feed(self, chunk: str) -> str:
        if self._finished:
            return ""
        if not self._started:
            self._buffer += chunk
            match = self._marker.search(self._buffer)
            if match is None:
                # marker 的最长长度很短；保留尾部以支持跨分片匹配，避免无限缓存。
                self._buffer = self._buffer[-max(64, len(self._marker.pattern) * 2):]
                return ""
            self._started = True
            chunk = self._buffer[match.end():]
            self._buffer = ""
        output: list[str] = []
        for char in chunk:
            if self._unicode is not None:
                self._unicode += char
                if len(self._unicode) == 4:
                    try:
                        output.append(chr(int(self._unicode, 16)))
                    except ValueError:
                        output.append("\\u" + self._unicode)
                    self._unicode = None
                    self._escaped = False
                continue
            if self._escaped:
                if char == "u":
                    self._unicode = ""
                    continue
                output.append({"n": "\n", "r": "\r", "t": "\t"}.get(char, char))
                self._escaped = False
                continue
            if char == "\\":
                self._escaped = True
            elif char == '"':
                self._finished = True
                break
            else:
                output.append(char)
        return "".join(output)


def _parse_structured_output(raw: Any, output_type: type[BaseModel]) -> BaseModel:
    """在本地完成 JSON 提取与 Pydantic 校验，兼容不支持 response_format 的端点。"""

    if isinstance(raw, output_type):
        return raw
    if isinstance(raw, BaseModel):
        return output_type.model_validate(raw.model_dump())
    if not isinstance(raw, str):
        raise StructuredOutputError("模型没有返回 JSON 文本", raw_output=repr(raw))
    try:
        value = _extract_json_object(raw)
    except WorkerExecutionError as exc:
        raise StructuredOutputError(str(exc), raw_output=raw) from exc
    try:
        return output_type.model_validate(value)
    except Exception as exc:
        raise StructuredOutputError(
            f"结构化输出校验失败：{exc}", raw_output=raw
        ) from exc


def _write_structured_diagnostic(
    settings: WorkerSettings,
    *,
    error: StructuredOutputError,
    raw_output: str,
) -> None:
    """解析失败时落盘原始输出；写诊断失败不得遮蔽模型错误。"""

    writer = settings.diagnostic_writer
    if writer is None:
        return
    try:
        writer.write(
            model=settings.worker_id,
            error=error,
            raw_response={
                "choices": [{"message": {"content": raw_output}, "finish_reason": "stop"}],
            },
        )
    except Exception:
        return


def _extract_json_object(raw: str) -> object:
    """提取模型文本中的最终 JSON 对象。

    DeepSeek 等 OpenAI-compatible 端点未必支持 response_format；即使提示词
    要求纯 JSON，模型仍可能在对象前保留简短推理。因此优先解析全文和代码块，
    最后只接受位于文本末尾的完整 JSON，避免误把过程中的示例当成交付结果。
    """

    candidate = raw.strip()
    parse_error: json.JSONDecodeError | None = None
    try:
        # 正文模型偶尔会在 JSON 字符串中直接输出换行或制表符。它们不改变
        # 对象结构，允许后再由 Pydantic 与领域校验继续把关，避免无谓重写正文。
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError as exc:
        parse_error = exc

    fenced_blocks = re.findall(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        re.DOTALL | re.IGNORECASE,
    )
    for block in reversed(fenced_blocks):
        try:
            return json.loads(block.strip(), strict=False)
        except json.JSONDecodeError as exc:
            parse_error = exc

    decoder = json.JSONDecoder(strict=False)
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
    """运行无工具文本 Worker，供聊天和摘要类调用复用。

    纯文本任务可以安全地把 SDK 的文本增量投影出去。结构化 Worker 则仍然只在
    Pydantic 校验完成后交付，避免把半截 JSON 当作用户可见内容。
    """

    await _emit(event_sinks, LlmEventType.RUN_STARTED, settings.worker_id, {
        "max_steps": settings.max_turns,
    })
    try:
        async def consume_stream() -> Any:
            result = Runner.run_streamed(
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
            )
            await _emit(event_sinks, LlmEventType.STREAM_STARTED, settings.worker_id, {})
            async for event in result.stream_events():
                # OpenAI Agents SDK 对 Responses 与 Chat Completions 都统一投影为
                # response.output_text.delta，因此不依赖具体 Provider 的原始格式。
                data = getattr(event, "data", None)
                if (
                    getattr(event, "type", None) == "raw_response_event"
                    and getattr(data, "type", None) == "response.output_text.delta"
                ):
                    delta = getattr(data, "delta", None)
                    if isinstance(delta, str) and delta:
                        await _emit(event_sinks, LlmEventType.TEXT_DELTA, settings.worker_id, {
                            "delta": delta,
                        })
            await _emit(event_sinks, LlmEventType.STREAM_COMPLETED, settings.worker_id, {})
            return result

        result = await asyncio.wait_for(consume_stream(), timeout=settings.timeout_seconds)
        output = result.final_output
        if not isinstance(output, str) or not output.strip():
            raise WorkerExecutionError("模型没有返回非空文本")
        await _emit(event_sinks, LlmEventType.RUN_FINISHED, settings.worker_id, {})
        return output.strip()
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
