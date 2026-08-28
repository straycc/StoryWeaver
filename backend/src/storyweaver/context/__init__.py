"""统一上下文预算、选择、检索证据与轻量 Trace。"""

from typing import Any

from .budget import BudgetUsage, ContextBudgetExceededError, TokenEstimator, require_within_budget
from .evidence import EvidenceCompiler, EvidencePackage
from .policy import (
    AgentContextPolicy,
    ChatContextPolicy,
    ContextBudget,
    ContextPriority,
    RetrievalPolicy,
    default_agent_context_policies,
)
from .selection import (
    ContextCandidate,
    ContextItem,
    ContextTrace,
    SelectedSource,
    select_context,
    trace_from_selected_entries,
    with_tool_results,
)


def __getattr__(name: str) -> Any:
    """延迟加载依赖 LLM 消息类型的 Chat Context Manager。"""

    if name == "SessionContextManager":
        from .manager import SessionContextManager

        return SessionContextManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "ChatContextPolicy",
    "ContextItem",
    "SessionContextManager",
    "AgentContextPolicy",
    "ContextBudget",
    "ContextPriority",
    "RetrievalPolicy",
    "default_agent_context_policies",
    "BudgetUsage",
    "require_within_budget",
    "EvidenceCompiler",
    "EvidencePackage",
    "ContextBudgetExceededError",
    "ContextCandidate",
    "ContextTrace",
    "SelectedSource",
    "TokenEstimator",
    "select_context",
    "trace_from_selected_entries",
    "with_tool_results",
]
