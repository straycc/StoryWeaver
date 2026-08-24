"""小说专业 Agent 的轻量调用基类。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, Mapping, TypeVar

from ...llm import (
    LlmEventSink,
    NOVEL_OUTPUT_TYPES,
    RetryContext,
    WorkerSettings,
    WorkerRetryPolicy,
    run_structured_worker,
    run_with_retry,
)
from ..exceptions import NovelAgentError


OutputT = TypeVar("OutputT")
ValidatedT = TypeVar("ValidatedT")


class BaseNovelAgent(Generic[OutputT]):
    """统一专业 Worker 的运行方式，但不引入新的 Agent Loop。"""

    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        system_prompt: str,
        output_schema: type[OutputT],
        retry_policy: WorkerRetryPolicy | None = None,
        sdk_settings: WorkerSettings | None = None,
        event_sinks: tuple[LlmEventSink, ...] = (),
    ) -> None:
        if sdk_settings is None:
            raise ValueError("小说 Worker 必须提供 SDK WorkerSettings")
        self._agent_id = agent_id
        self._name = name
        self._sdk_settings = sdk_settings
        self._sdk_retry_policy = retry_policy or WorkerRetryPolicy()
        self._event_sinks = event_sinks

    async def _generate(self, prompt: str) -> OutputT:
        """运行一次专业 Worker，并把运行时失败转换为领域异常。"""

        return await self._generate_validated(prompt, lambda output: output)

    async def _generate_validated(
        self,
        prompt: str,
        converter: Callable[[OutputT], ValidatedT],
        *,
        repair_instruction: str | None = None,
    ) -> ValidatedT:
        """将模型调用、结构转换和领域校验纳入同一个有限重试边界。"""

        return await self._generate_validated_with_sdk(prompt, converter, repair_instruction=repair_instruction)

    async def _generate_validated_with_sdk(
        self,
        prompt: str,
        converter: Callable[[OutputT], ValidatedT],
        *,
        repair_instruction: str | None,
    ) -> ValidatedT:
        """无工具 Worker 直接使用 SDK，并在领域校验失败后重投一次。"""

        output_type = NOVEL_OUTPUT_TYPES[self._agent_id]

        async def operation(context: RetryContext) -> ValidatedT:
            actual_prompt = self._retry_prompt(
                prompt,
                context=context,
                repair_instruction=repair_instruction,
            )
            output = await run_structured_worker(
                settings=self._sdk_settings,
                prompt=actual_prompt,
                output_type=output_type,
                event_sinks=self._event_sinks,
                tracing_enabled=True,
            )
            return converter(output.model_dump())

        return await run_with_retry(
            worker_name=self._agent_id,
            operation=operation,
            policy=self._sdk_retry_policy,
        )


    @staticmethod
    def _retry_prompt(
        original_prompt: str,
        *,
        context: RetryContext,
        repair_instruction: str | None,
    ) -> str:
        if not context.is_repair:
            return original_prompt
        instruction = repair_instruction or (
            "上一次输出未通过结构或领域校验。请根据错误修正输出，"
            "保持原任务目标不变，只返回任务要求的完整结果。"
        )
        return (
            f"{instruction}\n"
            f"校验错误：{context.repair_error}\n\n"
            f"## 原始任务\n{original_prompt}"
        )
