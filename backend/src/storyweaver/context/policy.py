"""Agent Context Policy v1 的统一配置。

这里描述“一个 Agent 最多能看多少、何时允许检索”，不负责选择具体小说资料。
领域 ContextBuilder 仍决定哪些来源与当前任务相关。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class ContextPriority(IntEnum):
    """跨 ContextBuilder 共用的四级选择优先级。"""

    OPTIONAL = 40
    SUPPORTING = 60
    REQUIRED = 80
    PROTECTED = 100


@dataclass(frozen=True, slots=True)
class ChatContextPolicy:
    """Web Chat 的滚动摘要与动态上下文限制。"""

    token_budget: int = 6000
    recent_message_limit: int = 12
    summary_target_tokens: int = 1000
    tool_inline_token_limit: int = 800
    memory_limit: int = 5

    def __post_init__(self) -> None:
        if self.token_budget < 1:
            raise ValueError("token_budget 必须大于 0")
        if self.recent_message_limit < 2:
            raise ValueError("recent_message_limit 不能小于 2")
        if self.summary_target_tokens < 1:
            raise ValueError("summary_target_tokens 必须大于 0")
        if self.tool_inline_token_limit < 1:
            raise ValueError("tool_inline_token_limit 必须大于 0")
        if self.memory_limit < 0:
            raise ValueError("memory_limit 不能小于 0")


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """一次 Worker 调用的分区预算，单位均为估算 Token。"""

    operational_window: int = 64_000
    fixed_context: int = 2_000
    initial_dynamic_context: int = 6_000
    runtime_tool_context: int = 0
    evidence_package: int = 0
    output_reserve: int = 6_000
    safety_reserve: int = 4_000

    def __post_init__(self) -> None:
        values = (
            self.operational_window,
            self.fixed_context,
            self.initial_dynamic_context,
            self.runtime_tool_context,
            self.evidence_package,
            self.output_reserve,
            self.safety_reserve,
        )
        if any(value < 0 for value in values):
            raise ValueError("Context 分区预算不能小于 0")
        if self.operational_window < 1:
            raise ValueError("operational_window 必须大于 0")
        if self.peak_reserved > self.operational_window:
            raise ValueError(
                "Context 阶段峰值预算超过 operational_window："
                f"{self.peak_reserved}/{self.operational_window}"
            )

    @property
    def research_phase_total(self) -> int:
        """Research 单次调用的最大规划占用。"""

        return (
            self.fixed_context
            + self.initial_dynamic_context
            + self.runtime_tool_context
            + self.safety_reserve
        )

    @property
    def report_phase_total(self) -> int:
        """Report 单次调用的最大规划占用。"""

        return (
            self.fixed_context
            + self.initial_dynamic_context
            + self.evidence_package
            + self.output_reserve
            + self.safety_reserve
        )

    @property
    def peak_reserved(self) -> int:
        """返回两个隔离阶段中较大的窗口占用。"""

        return max(self.research_phase_total, self.report_phase_total)

    @property
    def unallocated(self) -> int:
        """未分配空间只作为运行余量，不能自动转给动态上下文。"""

        return self.operational_window - self.peak_reserved


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """只读检索及阶段隔离的硬限制。"""

    enabled: bool = False
    two_phase: bool = False
    max_tool_calls: int = 0
    max_research_turns: int = 0
    tool_timeout_seconds: float = 8.0
    per_tool_result_tokens: int = 800
    evidence_package_tokens: int = 0
    transient_attempts: int = 2

    def __post_init__(self) -> None:
        if self.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds 必须大于 0")
        if self.per_tool_result_tokens < 64:
            raise ValueError("per_tool_result_tokens 不能小于 64")
        if self.transient_attempts < 1:
            raise ValueError("transient_attempts 必须大于 0")
        if self.enabled:
            if self.max_tool_calls < 1:
                raise ValueError("启用检索时 max_tool_calls 必须大于 0")
            if self.max_research_turns < 1:
                raise ValueError("启用检索时 max_research_turns 必须大于 0")
            if self.evidence_package_tokens < 1:
                raise ValueError("启用检索时 evidence_package_tokens 必须大于 0")
        elif any((self.max_tool_calls, self.max_research_turns, self.evidence_package_tokens)):
            raise ValueError("未启用检索时不应配置检索预算")


@dataclass(frozen=True, slots=True)
class AgentContextPolicy:
    """一个 Agent 的执行模式、上下文预算和检索策略。"""

    role: str
    budget: ContextBudget
    retrieval: RetrievalPolicy = field(default_factory=RetrievalPolicy)
    policy_version: str = "context-policy-v1"

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("role 不能为空")
        if self.retrieval.evidence_package_tokens > self.budget.evidence_package:
            raise ValueError("检索 Evidence 预算不能超过 Context 的 evidence_package 分区")


def default_agent_context_policies(
    *,
    operational_window: int = 64_000,
    safety_reserve: int = 4_000,
    fixed_context: int = 2_000,
) -> dict[str, AgentContextPolicy]:
    """返回 Context Policy v1 的初始值。

    这些数值是运行基线，不代表模型窗口上限；后续应根据 usage、截断率和
    质量门禁数据调整。
    """

    def budget(
        *,
        initial: int,
        runtime: int = 0,
        evidence: int = 0,
        output: int,
    ) -> ContextBudget:
        return ContextBudget(
            operational_window=operational_window,
            fixed_context=fixed_context,
            initial_dynamic_context=initial,
            runtime_tool_context=runtime,
            evidence_package=evidence,
            output_reserve=output,
            safety_reserve=safety_reserve,
        )

    return {
        "planner": AgentContextPolicy(
            role="planner",
            budget=budget(initial=4_000, runtime=4_000, evidence=4_000, output=32_000),
            retrieval=RetrievalPolicy(
                enabled=True, two_phase=True, max_tool_calls=4,
                max_research_turns=2, per_tool_result_tokens=900,
                evidence_package_tokens=4_000,
            ),
        ),
        "writer": AgentContextPolicy(
            role="writer",
            budget=budget(initial=6_000, output=32_000),
        ),
        "reviewer": AgentContextPolicy(
            role="reviewer",
            budget=budget(initial=10_000, runtime=6_000, evidence=6_000, output=32_000),
            retrieval=RetrievalPolicy(
                enabled=True, two_phase=True, max_tool_calls=6,
                max_research_turns=2, per_tool_result_tokens=1_000,
                evidence_package_tokens=6_000,
            ),
        ),
        "reviewer_verification": AgentContextPolicy(
            role="reviewer_verification",
            budget=budget(initial=10_000, runtime=3_000, evidence=3_000, output=32_000),
            retrieval=RetrievalPolicy(
                enabled=True, two_phase=True, max_tool_calls=3,
                max_research_turns=1, per_tool_result_tokens=900,
                evidence_package_tokens=3_000,
            ),
        ),
        "reviser": AgentContextPolicy(
            role="reviser",
            budget=budget(initial=12_000, output=32_000),
        ),
        "analyzer": AgentContextPolicy(
            role="analyzer",
            budget=budget(initial=15_000, runtime=4_000, evidence=4_000, output=32_000),
            retrieval=RetrievalPolicy(
                enabled=True, two_phase=True, max_tool_calls=4,
                max_research_turns=2, per_tool_result_tokens=900,
                evidence_package_tokens=4_000,
            ),
        ),
    }
