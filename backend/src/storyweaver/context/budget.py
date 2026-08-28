"""上下文预算的轻量估算和边界检查。"""

from __future__ import annotations

from dataclasses import dataclass


class ContextBudgetExceededError(ValueError):
    """受保护内容无法装入指定分区时显式失败。"""


class TokenEstimator:
    """无 Provider tokenizer 时对中英文混合内容进行统一保守估算。"""

    @staticmethod
    def estimate(content: str) -> int:
        return max(1, (len(content.strip()) + 1) // 2)


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """一次选择或编译后的预算使用情况。"""

    budget: int
    estimated_tokens: int

    def __post_init__(self) -> None:
        if self.budget < 1:
            raise ValueError("budget 必须大于 0")
        if self.estimated_tokens < 0:
            raise ValueError("estimated_tokens 不能小于 0")

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.estimated_tokens)

    @property
    def exceeded(self) -> bool:
        return self.estimated_tokens > self.budget


def require_within_budget(
    content: str,
    *,
    budget: int,
    label: str,
    estimator: TokenEstimator | None = None,
) -> BudgetUsage:
    """校验不可静默裁剪的完整载荷。"""

    active_estimator = estimator or TokenEstimator()
    usage = BudgetUsage(budget, active_estimator.estimate(content))
    if usage.exceeded:
        raise ContextBudgetExceededError(
            f"{label}估算 {usage.estimated_tokens} Token，超过预算 {budget}；"
            "拒绝静默删除受保护内容"
        )
    return usage

