"""带只读检索的小说 Worker 两阶段执行。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import Agent, Runner, ToolsToFinalOutputResult
from agents.run import RunConfig
from agents.tool import FunctionTool
from pydantic import BaseModel

from .sdk import WorkerExecutionError, WorkerSettings, _WorkerHooks, _emit
from .events import LlmEventSink, LlmEventType

_SENTINEL = "__storyweaver_research_complete__"


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
) -> BaseModel:
    """检索与交付隔离；报告阶段只获得 EvidencePackage。"""

    await _emit(event_sinks, LlmEventType.RUN_STARTED, settings.worker_id, {
        "max_steps": max_research_turns + 1,
    })
    try:
        turns = 0

        def stop_after_research(_context: object, _results: object) -> ToolsToFinalOutputResult:
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
            instructions=(settings.instructions + "\n此阶段只检索证据；不要输出最终结论。"),
            model=settings.model,
            model_settings=settings.model_settings,
            tools=read_tools,
            tool_use_behavior=stop_after_research,
        )
        await asyncio.wait_for(
            Runner.run(
                research, prompt, max_turns=max_research_turns,
                run_config=RunConfig(tracing_disabled=not tracing_enabled, workflow_name=f"{settings.worker_id}.research"),
                hooks=Hooks(settings.worker_id, event_sinks, response_kind="research"),
            ),
            timeout=settings.timeout_seconds,
        )
        submitted: BaseModel | None = None

        async def submit(_context: object, arguments: str) -> str:
            nonlocal submitted
            submitted = output_type.model_validate(json.loads(arguments))
            return "已接收结构化交付。"

        submit_tool = FunctionTool(
            name=submit_tool_name,
            description=submit_description,
            params_json_schema=output_type.model_json_schema(),
            on_invoke_tool=submit,
            strict_json_schema=False,
        )
        package = {"evidence_count": len(evidence), "evidence": evidence}
        report = Agent(
            name=f"{settings.name}（最终交付）",
            instructions=(settings.instructions + f"\n检索已经结束。不得查询或模拟查询；必须且只能调用 {submit_tool_name} 一次交付结果。"),
            model=settings.model,
            model_settings=settings.model_settings,
            tools=[submit_tool],
            tool_use_behavior={"stop_at_tool_names": [submit_tool_name]},
        )
        await asyncio.wait_for(
            Runner.run(
                report,
                prompt + "\n\n## EvidencePackage\n" + json.dumps(package, ensure_ascii=False),
                max_turns=1,
                run_config=RunConfig(tracing_disabled=not tracing_enabled, workflow_name=f"{settings.worker_id}.report"),
                hooks=Hooks(settings.worker_id, event_sinks),
            ),
            timeout=settings.timeout_seconds,
        )
        if submitted is None:
            raise WorkerExecutionError(f"最终交付阶段未调用 {submit_tool_name}")
        await _emit(event_sinks, LlmEventType.RUN_FINISHED, settings.worker_id, {})
        return submitted
    except Exception as exc:
        await _emit(event_sinks, LlmEventType.RUN_FAILED, settings.worker_id, {"error": f"{type(exc).__name__}: {exc}"})
        raise WorkerExecutionError(f"{type(exc).__name__}: {exc}") from exc
