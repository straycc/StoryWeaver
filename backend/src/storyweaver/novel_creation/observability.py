"""小说 Worker 的进度、耗时和模型用量观测。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..llm import LlmEvent, LlmEventType
from ..observability import get_log_context, logging_context


_LOGGER = logging.getLogger(__name__)


AGENT_DISPLAY_NAMES = {
    "novel-architect": "架构设计",
    "novel-planner": "章节规划",
    "novel-writer": "正文写作",
    "novel-reviewer": "章节审查",
    "novel-reviewer-verification": "章节定向复查",
    "novel-reviser": "正文修订",
    "chapter-analyzer": "状态分析",
}


@dataclass(frozen=True, slots=True)
class WorkerRunMetric:
    """一次专业 Worker 调用的最终指标。"""

    agent_id: str
    display_name: str
    succeeded: bool
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int
    model_calls: int = 1
    retry_count: int = 0
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """若干次 Worker 调用的聚合指标。"""

    run_count: int
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class _ActiveRun:
    started_at: float
    max_steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    retry_count: int = 0
    stream_field: str | None = None
    stream_preview_emitted: bool = False


class NovelRunObserver:
    """通过 Runtime Hook 收集指标，并可选择实时显示阶段进度。"""

    def __init__(
        self,
        *,
        output: Callable[[str], None] | None = print,
        clock: Callable[[], float] = time.perf_counter,
        event_sink: Callable[[str, str, Mapping[str, Any]], None] | None = None,
        live_preview_sink: Callable[[str, str, Mapping[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._output = output
        self._clock = clock
        self._event_sink = event_sink
        self._live_preview_sink = live_preview_sink
        self._lock = threading.RLock()
        self._active: dict[tuple[str, str], _ActiveRun] = {}
        self._records: list[WorkerRunMetric] = []

    @property
    def records(self) -> tuple[WorkerRunMetric, ...]:
        with self._lock:
            return tuple(self._records)

    def mark(self) -> int:
        """返回当前位置，用于统计一次应用动作产生的新指标。"""

        with self._lock:
            return len(self._records)

    def summarize(self, *, since: int = 0) -> UsageSummary:
        with self._lock:
            if since < 0 or since > len(self._records):
                raise ValueError("since 超出指标记录范围")
            records = tuple(self._records[since:])
        return UsageSummary(
            run_count=len(records),
            elapsed_seconds=sum(item.elapsed_seconds for item in records),
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
        )

    async def on_event(self, event: LlmEvent) -> None:
        """消费 SDK 生命周期事件。"""

        context = get_log_context()
        run_id = context.get("run_id", "")
        # 迁移期间允许旧包装层发出的同值事件进入观察器；生产调用切换后
        # 该回退分支会一并删除。
        worker_id = getattr(event, "worker_id", getattr(event, "agent_id", "unknown"))
        event_type = str(event.type)
        active_key = (run_id, worker_id)
        display_name = AGENT_DISPLAY_NAMES.get(worker_id, worker_id)
        if event_type == LlmEventType.RUN_STARTED.value:
            with self._lock:
                self._active[active_key] = _ActiveRun(
                    started_at=self._clock(),
                    max_steps=self._integer(event.data.get("max_steps")),
                )
            self._emit_progress(
                run_id,
                "stage_started",
                {"agent_id": worker_id, "display_name": display_name},
            )
            self._write(f"[开始] {display_name}")
            with logging_context(agent_id=worker_id):
                _LOGGER.info("▶ %s", display_name)
            return

        with self._lock:
            active = self._active.get(active_key)
            if event_type == LlmEventType.MODEL_COMPLETED.value and active is not None:
                active.input_tokens += self._integer(event.data.get("input_tokens"))
                active.output_tokens += self._integer(event.data.get("output_tokens"))
                active.model_calls += 1
        if event_type == LlmEventType.MODEL_COMPLETED.value:
            step = self._integer(event.data.get("step"))
            max_steps = active.max_steps if active is not None else 0
            input_tokens = self._integer(event.data.get("input_tokens"))
            output_tokens = self._integer(event.data.get("output_tokens"))
            tool_call_count = self._integer(event.data.get("tool_call_count"))
            elapsed_seconds = max(
                0.0,
                float(event.data.get("elapsed_seconds") or 0.0),
            )
            response_kind = str(event.data.get("response_kind") or "unknown")
            result_label = (
                f"请求工具 {tool_call_count} 个"
                if response_kind == "tool_calls"
                else "输出最终结果"
                if response_kind == "final"
                else "未返回有效结果"
            )
            payload = {
                "agent_id": worker_id,
                "display_name": display_name,
                "step": step or None,
                "max_steps": max_steps or None,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "tool_call_count": tool_call_count,
                "response_kind": response_kind,
            }
            self._emit_progress(run_id, "model_completed", payload)
            step_label = f"第 {step}/{max_steps} 回合" if step and max_steps else "模型回合"
            self._write(
                f"[模型] {display_name} · {step_label} · {result_label} · "
                f"{elapsed_seconds:.2f}s · 输入 {input_tokens:,} · 输出 {output_tokens:,}"
            )
            with logging_context(agent_id=worker_id):
                _LOGGER.info(
                    "模型 %s · %s · %s · %.2fs · 输入 %s · 输出 %s",
                    display_name,
                    step_label,
                    result_label,
                    elapsed_seconds,
                    f"{input_tokens:,}",
                    f"{output_tokens:,}",
                )
            return

        if event_type == LlmEventType.MODEL_REPAIRING.value:
            if active is not None:
                active.retry_count += 1
                # 旧 Runtime 在解析失败时没有发出 MODEL_COMPLETED；SDK 路径会
                # 显式标识该调用是否已经计入，避免未来重复统计。
                if not bool(event.data.get("previous_model_call_counted")):
                    active.model_calls += 1
            step = self._integer(event.data.get("step"))
            error = str(event.data.get("error") or "结构化输出无效")
            step_label = f"第 {step} 回合" if step else "最终回合"
            self._write(f"[模型格式修复] {display_name} · {step_label} · {error}")
            with logging_context(agent_id=event.worker_id):
                _LOGGER.warning("模型格式修复 %s · %s · %s", display_name, step_label, error)
            return

        if event_type in {
            LlmEventType.STREAM_STARTED.value,
            LlmEventType.TEXT_DELTA.value,
            LlmEventType.STREAM_COMPLETED.value,
        }:
            # 正文预览只进入进程内 Hub，不写入 job_events。最终产物仍必须经过
            # Pydantic 与领域校验后才会持久化。
            if event_type == LlmEventType.STREAM_STARTED.value:
                await self._emit_live_preview(run_id, "preview_started", {
                    "agent_id": worker_id, "field": event.data.get("field", "text"),
                })
                self._emit_progress(run_id, "stream_started", {
                    "agent_id": worker_id, "display_name": display_name,
                    **dict(event.data),
                })
                return
            if event_type == LlmEventType.TEXT_DELTA.value:
                delta = str(event.data.get("delta") or "")
                if delta:
                    with self._lock:
                        if active is not None:
                            active.stream_field = str(event.data.get("field") or active.stream_field or "text")
                            active.stream_preview_emitted = True
                    await self._emit_live_preview(run_id, "preview_delta", {
                        "agent_id": worker_id,
                        "field": event.data.get("field", "text"),
                        "delta": delta,
                    })
                return
            await self._emit_live_preview(run_id, "preview_completed", {
                "agent_id": worker_id, "field": event.data.get("field", "text"),
            })
            self._emit_progress(run_id, "stream_completed", {
                "agent_id": worker_id, "display_name": display_name,
                **dict(event.data),
            })
            return

        if event_type == LlmEventType.TOOL_COMPLETED.value:
            succeeded = bool(event.data.get("succeeded"))
            budget_exhausted = bool(event.data.get("budget_exhausted"))
            deduplicated = bool(event.data.get("deduplicated"))
            error = str(event.data.get("error") or "")
            tool_name = str(event.data.get("tool_name") or "未知工具")
            elapsed_seconds = max(
                0.0,
                float(event.data.get("elapsed_seconds") or 0.0),
            )
            call_index = self._integer(event.data.get("tool_call_index"))
            call_limit = event.data.get("tool_call_limit")
            limit = self._integer(call_limit) if call_limit is not None else None
            call_label = (
                f" · {call_index}/{limit}"
                if call_index and limit
                else f" · 第 {call_index} 次" if call_index else ""
            )
            status = "预算耗尽" if budget_exhausted else "复用" if deduplicated else "完成" if succeeded else "失败"
            payload = {
                "agent_id": worker_id,
                "display_name": display_name,
                "tool_name": tool_name,
                "succeeded": succeeded,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "tool_call_index": call_index or None,
                "tool_call_limit": limit,
                "budget_exhausted": budget_exhausted,
                "deduplicated": deduplicated,
                "error": error or None,
                # 面向前端的简短参数摘要；不输出工具原始结果。
                "tool_arguments": dict(event.data.get("tool_arguments") or {}),
            }
            self._emit_progress(run_id, "tool_completed", payload)
            self._write(
                f"[工具{status}] {display_name} · {tool_name}{call_label} · "
                f"{elapsed_seconds:.2f}s"
            )
            log_method = _LOGGER.info if succeeded or budget_exhausted else _LOGGER.error
            with logging_context(agent_id=worker_id):
                log_method(
                    "工具%s %s · %s%s · %.2fs",
                    status,
                    display_name,
                    tool_name,
                    call_label,
                    elapsed_seconds,
                )
            return

        if event_type not in {
            LlmEventType.RUN_FINISHED.value,
            LlmEventType.RUN_FAILED.value,
        }:
            return

        with self._lock:
            active = self._active.pop(active_key, None)
        if active is None:
            active = _ActiveRun(started_at=self._clock())
        elapsed = max(0.0, self._clock() - active.started_at)
        succeeded = event_type == LlmEventType.RUN_FINISHED.value
        if not succeeded and active.stream_preview_emitted:
            # 结构化解析或领域校验失败时，已显示的预览不具备提交资格。
            self._emit_progress(run_id, "stream_reset", {
                "agent_id": worker_id,
                "display_name": display_name,
                "field": active.stream_field,
            })
            await self._emit_live_preview(run_id, "preview_reset", {
                "agent_id": worker_id,
                "field": active.stream_field or "text",
            })
        error = None if succeeded else str(event.data.get("error") or "未知错误")
        metric = WorkerRunMetric(
            agent_id=worker_id,
            display_name=display_name,
            succeeded=succeeded,
            elapsed_seconds=elapsed,
            input_tokens=active.input_tokens,
            output_tokens=active.output_tokens,
            model_calls=max(1, active.model_calls),
            retry_count=active.retry_count,
            error=error,
        )
        with self._lock:
            self._records.append(metric)
        status = "完成" if succeeded else "失败"
        self._emit_progress(
            run_id,
            "stage_completed" if succeeded else "stage_failed",
            {
                "agent_id": worker_id,
                "display_name": display_name,
                "elapsed_seconds": round(elapsed, 2),
                "input_tokens": metric.input_tokens,
                "output_tokens": metric.output_tokens,
                "total_tokens": metric.total_tokens,
                "error": error,
            },
        )
        self._write(
            f"[{status}] {display_name} · {elapsed:.2f}s · "
            f"Token {metric.total_tokens}"
        )
        log_method = _LOGGER.info if succeeded else _LOGGER.error
        with logging_context(agent_id=worker_id):
            log_method(
                "%s %s · %.1fs · %s Token%s",
                status,
                display_name,
                elapsed,
                f"{metric.total_tokens:,}",
                f" · {error}" if error else "",
            )

    @staticmethod
    def _integer(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _write(self, message: str) -> None:
        if self._output is not None:
            self._output(message)

    def _emit_progress(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self._event_sink is not None and run_id:
            self._event_sink(run_id, event_type, payload)

    async def _emit_live_preview(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self._live_preview_sink is not None and run_id:
            await self._live_preview_sink(run_id, event_type, payload)
