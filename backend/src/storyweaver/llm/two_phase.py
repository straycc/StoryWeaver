"""带只读检索的小说 Worker 两阶段执行。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Sequence

from agents import Agent, ModelSettings, Runner, ToolsToFinalOutputResult
from agents.run import RunConfig
from agents.tool import FunctionTool
from pydantic import BaseModel

from ..context.evidence import EvidenceCompiler, EvidencePackage
from .events import LlmEventSink, LlmEventType
from .retry import RetryContext, WorkerRetryPolicy, run_with_retry
from .sdk import WorkerExecutionError, WorkerSettings, _WorkerHooks, _emit


_SENTINEL = "__storyweaver_research_complete__"


def _report_model_settings(
    model_settings: ModelSettings,
    *,
    is_repair: bool,
) -> ModelSettings:
    """为收敛型 Report 调用派生模型参数，不污染 Research 的原始配置。

    DeepSeek 的隐藏推理和可见交付共享输出额度。首次交付保留低强度推理；
    结构或领域校验失败后的修复只需按错误改正结果，因此关闭思考模式。
    这些兼容端点参数集中在公共运行器中，业务 Agent 无需感知。
    """

    extra_body = dict(model_settings.extra_body or {})
    if is_repair:
        extra_body["thinking"] = {"type": "disabled"}
        extra_body.pop("reasoning_effort", None)
    else:
        extra_body["reasoning_effort"] = "low"
    return replace(model_settings, extra_body=extra_body)


async def run_research(
    *,
    settings: WorkerSettings,
    prompt: str,
    read_tools: list[FunctionTool],
    max_research_turns: int,
    event_sinks: Sequence[LlmEventSink] = (),
    tracing_enabled: bool = False,
) -> None:
    """运行隔离的研究 Agent；只允许临时故障重投，不生产业务结论。"""

    if max_research_turns < 1:
        raise ValueError("max_research_turns 必须大于 0")

    async def operation(_retry: RetryContext) -> None:
        turns = 0

        def stop_after_research(
            _context: object,
            _results: object,
        ) -> ToolsToFinalOutputResult:
            return ToolsToFinalOutputResult(
                is_final_output=turns >= max_research_turns,
                final_output=_SENTINEL,
            )

        class Hooks(_WorkerHooks):
            async def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
                nonlocal turns
                turns += 1
                await super().on_llm_start(*args, **kwargs)

        research = Agent(
            name=f"{settings.name}（检索）",
            instructions=(
                settings.instructions
                + "\n此阶段只检索证据；不要输出最终结论。"
                + f"本次研究最多 {max_research_turns} 个模型回合；工具次数由运行时限制。"
                + "工具失败、空结果或预算耗尽时，使用已有证据结束研究，不要循环调用。"
            ),
            model=settings.model,
            model_settings=settings.model_settings,
            tools=read_tools,
            tool_use_behavior=stop_after_research,
        )
        await asyncio.wait_for(
            Runner.run(
                research,
                prompt,
                max_turns=max_research_turns,
                run_config=RunConfig(
                    tracing_disabled=not tracing_enabled,
                    workflow_name=f"{settings.worker_id}.research",
                ),
                hooks=Hooks(settings.worker_id, event_sinks, response_kind="research"),
            ),
            timeout=settings.timeout_seconds,
        )

    # Research 没有待修复的结构化交付；只允许一次临时故障重投。
    await run_with_retry(
        worker_name=f"{settings.worker_id}.research",
        operation=operation,
        policy=WorkerRetryPolicy(max_attempts=2, max_repairs=0),
    )


async def run_report(
    *,
    settings: WorkerSettings,
    prompt: str,
    evidence_package: EvidencePackage,
    output_type: type[BaseModel],
    submit_tool_name: str,
    submit_description: str,
    event_sinks: Sequence[LlmEventSink] = (),
    tracing_enabled: bool = False,
    retry_policy: WorkerRetryPolicy | None = None,
    output_validator: Callable[[BaseModel], object] | None = None,
) -> object:
    """只使用 EvidencePackage 交付，并在本阶段内修复结构化输出。"""

    package_text = json.dumps(
        evidence_package.to_data(),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )

    async def operation(retry: RetryContext) -> object:
        submitted: BaseModel | None = None
        submission_error: BaseException | None = None

        async def submit(_context: object, arguments: str) -> str:
            nonlocal submitted, submission_error
            try:
                submitted = output_type.model_validate(json.loads(arguments))
            except Exception as exc:
                submission_error = exc
                raise
            return "已接收结构化交付。"

        submit_tool = FunctionTool(
            name=submit_tool_name,
            description=submit_description,
            params_json_schema=output_type.model_json_schema(),
            on_invoke_tool=submit,
            strict_json_schema=False,
        )
        repair = ""
        if retry.is_repair:
            await _emit(event_sinks, LlmEventType.MODEL_REPAIRING, settings.worker_id, {
                "error": retry.repair_error or "结构化输出或领域校验失败",
                "phase": "report",
            })
            repair = (
                "\n\n## 上一次交付错误\n"
                f"{retry.repair_error}\n"
                "请针对错误修复结构或违反业务规则的内容，不改变已知证据，不重新研究。"
            )
        report = Agent(
            name=f"{settings.name}（最终交付）",
            instructions=(
                settings.instructions
                + f"\n检索已经结束。不得查询或模拟查询；必须且只能调用 {submit_tool_name} 一次交付结果。"
            ),
            model=settings.model,
            model_settings=_report_model_settings(
                settings.model_settings,
                is_repair=retry.is_repair,
            ),
            tools=[submit_tool],
            tool_use_behavior={"stop_at_tool_names": [submit_tool_name]},
        )
        try:
            await asyncio.wait_for(
                Runner.run(
                    report,
                    prompt + "\n\n## EvidencePackage\n" + package_text + repair,
                    max_turns=1,
                    run_config=RunConfig(
                        tracing_disabled=not tracing_enabled,
                        workflow_name=f"{settings.worker_id}.report",
                    ),
                    hooks=_WorkerHooks(settings.worker_id, event_sinks),
                ),
                timeout=settings.timeout_seconds,
            )
        except Exception as run_error:
            if submission_error is not None:
                raise ValueError(
                    "结构化输出校验失败："
                    f"{type(submission_error).__name__}: {submission_error}"
                ) from submission_error
            raise
        if submitted is None:
            if submission_error is not None:
                raise ValueError(
                    "结构化输出校验失败："
                    f"{type(submission_error).__name__}: {submission_error}"
                ) from submission_error
            raise ValueError(f"结构化输出缺少 {submit_tool_name} 调用")
        return output_validator(submitted) if output_validator is not None else submitted

    return await run_with_retry(
        worker_name=f"{settings.worker_id}.report",
        operation=operation,
        policy=retry_policy or WorkerRetryPolicy(),
    )


async def run_research_then_submit(
    *,
    settings: WorkerSettings,
    prompt: str,
    read_tools: list[FunctionTool],
    output_type: type[BaseModel],
    submit_tool_name: str,
    submit_description: str,
    max_research_turns: int,
    evidence: list[dict[str, object]],
    event_sinks: tuple[LlmEventSink, ...] = (),
    tracing_enabled: bool = False,
    evidence_token_budget: int = 4_000,
    report_retry_policy: WorkerRetryPolicy | None = None,
    output_validator: Callable[[BaseModel], object] | None = None,
) -> object:
    """执行 Research → EvidencePackage → Report，并保持阶段重试隔离。"""

    await _emit(
        event_sinks,
        LlmEventType.RUN_STARTED,
        settings.worker_id,
        {"max_steps": max_research_turns + 1},
    )
    try:
        await run_research(
            settings=settings,
            prompt=prompt,
            read_tools=read_tools,
            max_research_turns=max_research_turns,
            event_sinks=event_sinks,
            tracing_enabled=tracing_enabled,
        )
        package = EvidenceCompiler(
            token_budget=evidence_token_budget,
        ).compile(evidence)
        result = await run_report(
            settings=settings,
            prompt=prompt,
            evidence_package=package,
            output_type=output_type,
            submit_tool_name=submit_tool_name,
            submit_description=submit_description,
            event_sinks=event_sinks,
            tracing_enabled=tracing_enabled,
            retry_policy=report_retry_policy,
            output_validator=output_validator,
        )
        await _emit(event_sinks, LlmEventType.RUN_FINISHED, settings.worker_id, {
            "evidence_source_count": package.source_count,
            "evidence_count": len(package.evidence),
            "evidence_estimated_tokens": package.estimated_tokens,
            "evidence_excluded_count": package.excluded_count,
            "evidence_truncated_count": package.truncated_count,
        })
        return result
    except asyncio.CancelledError:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": "两阶段调用已取消",
        })
        raise
    except Exception as exc:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise WorkerExecutionError(f"{type(exc).__name__}: {exc}") from exc
