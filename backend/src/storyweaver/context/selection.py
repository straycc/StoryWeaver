"""轻量上下文候选选择与观测 Trace。

本模块只负责动态上下文的预算选择。内容如何检索、如何渲染以及如何持久化，
仍由各业务模块决定；Trace 不保存 Prompt、工具参数或工具返回正文。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256

from .budget import ContextBudgetExceededError, TokenEstimator


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    """一个可能进入当前 Agent 工作上下文的来源。"""

    source_id: str
    source_type: str
    content: str
    reason: str
    protected: bool
    priority: int
    digest: str | None = None

    @property
    def estimated_tokens(self) -> int:
        return TokenEstimator.estimate(self.content) + 8


# Chat 代码沿用这个更贴近其语义的名称，但不再维护第二套数据模型。
ContextItem = ContextCandidate


@dataclass(frozen=True, slots=True)
class SelectedSource:
    """Trace 中一条最小来源记录。"""

    source_id: str
    source_type: str
    estimated_tokens: int
    protected: bool = False


@dataclass(frozen=True, slots=True)
class ContextTrace:
    """上下文选择结果，仅用于观测、调试和离线评测。"""

    agent_role: str
    policy_version: str
    book_version: int | None
    renderer_version: str
    budget: int
    estimated_tokens: int
    selected_sources: tuple[SelectedSource, ...]
    excluded_source_ids: tuple[str, ...] = ()
    compressed_source_ids: tuple[str, ...] = ()
    tool_source_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    selected_memory_ids: tuple[str, ...] = ()
    summary_sequence: int | None = None
    source_reasons: tuple[tuple[str, str], ...] = ()

    @property
    def selected_source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.selected_sources)

    @property
    def protected_source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.selected_sources if item.protected)

    def to_data(self) -> dict[str, object]:
        """返回稳定、无上下文正文的可持久化投影。"""

        return {
            "agent_role": self.agent_role,
            "policy_version": self.policy_version,
            "book_version": self.book_version,
            "renderer_version": self.renderer_version,
            "budget": self.budget,
            "estimated_tokens": self.estimated_tokens,
            "selected_sources": [
                {
                    "source_id": item.source_id,
                    "source_type": item.source_type,
                    "estimated_tokens": item.estimated_tokens,
                    "protected": item.protected,
                }
                for item in self.selected_sources
            ],
            # 以下 ID 字段同时服务现有 Trace 抽屉，避免 UI 依赖内部对象。
            "selected_source_ids": list(self.selected_source_ids),
            "protected_source_ids": list(self.protected_source_ids),
            "excluded_source_ids": list(self.excluded_source_ids),
            "compressed_source_ids": list(self.compressed_source_ids),
            "tool_source_ids": list(self.tool_source_ids),
            "selected_memory_ids": list(self.selected_memory_ids),
            "summary_sequence": self.summary_sequence,
            "notes": list(self.notes),
            "source_reasons": [list(item) for item in self.source_reasons],
        }


def select_context(
    *,
    agent_role: str,
    policy_version: str,
    book_version: int | None,
    token_budget: int,
    candidates: tuple[ContextCandidate, ...],
    renderer_version: str = "context-renderer-v1",
    notes: tuple[str, ...] = (),
    compressed_source_ids: tuple[str, ...] = (),
    selected_memory_ids: tuple[str, ...] = (),
    summary_sequence: int | None = None,
) -> tuple[tuple[ContextCandidate, ...], ContextTrace]:
    """优先保留 protected 来源，再按优先级选择普通来源。"""

    if token_budget < 1:
        raise ValueError("token_budget 必须大于 0")
    protected = [item for item in candidates if item.protected]
    ordinary = sorted(
        (item for item in candidates if not item.protected),
        key=lambda item: (-item.priority, item.source_id),
    )
    selected: list[ContextCandidate] = list(protected)
    compressed = list(compressed_source_ids)
    used = sum(item.estimated_tokens for item in selected)

    if used > token_budget:
        for index, item in enumerate(tuple(selected)):
            if used <= token_budget or not item.digest:
                continue
            selected[index] = replace(item, content=item.digest, digest=None)
            compressed.append(item.source_id)
            used = sum(candidate.estimated_tokens for candidate in selected)
    if used > token_budget:
        raise ContextBudgetExceededError(
            f"protected Context 估算 {used} Token，超过预算 {token_budget}；"
            "拒绝静默丢弃硬约束"
        )

    excluded: list[str] = []
    for item in ordinary:
        if used + item.estimated_tokens <= token_budget:
            selected.append(item)
            used += item.estimated_tokens
        else:
            excluded.append(item.source_id)

    trace = ContextTrace(
        agent_role=agent_role,
        policy_version=policy_version,
        book_version=book_version,
        renderer_version=renderer_version,
        budget=token_budget,
        estimated_tokens=used,
        selected_sources=tuple(
            SelectedSource(
                source_id=item.source_id,
                source_type=item.source_type,
                estimated_tokens=item.estimated_tokens,
                protected=item.protected,
            )
            for item in selected
        ),
        excluded_source_ids=tuple(excluded),
        compressed_source_ids=tuple(dict.fromkeys(compressed)),
        notes=notes,
        selected_memory_ids=selected_memory_ids,
        summary_sequence=summary_sequence,
        source_reasons=tuple((item.source_id, item.reason) for item in selected)
        + tuple((source_id, "超过动态上下文预算") for source_id in excluded),
    )
    return tuple(selected), trace


def trace_from_selected_entries(
    *,
    agent_role: str,
    policy_version: str,
    book_version: int | None,
    token_budget: int,
    entries: object,
    excluded_source_ids: tuple[str, ...] = (),
    renderer_version: str = "context-renderer-v1",
    notes: tuple[str, ...] = (),
) -> ContextTrace:
    """把领域 Selector 已经选中的条目投影为统一 Trace。"""

    selected = tuple(
        SelectedSource(
            source_id=str(getattr(entry, "source_id")),
            source_type=str(getattr(entry, "source_type")),
            estimated_tokens=TokenEstimator.estimate(str(getattr(entry, "content"))) + 8,
            protected=bool(getattr(entry, "protected", False)),
        )
        for entry in entries
    )
    return ContextTrace(
        agent_role=agent_role,
        policy_version=policy_version,
        book_version=book_version,
        renderer_version=renderer_version,
        budget=token_budget,
        estimated_tokens=sum(item.estimated_tokens for item in selected),
        selected_sources=selected,
        excluded_source_ids=excluded_source_ids,
        notes=notes,
    )


def with_tool_results(
    trace: ContextTrace,
    *,
    evidence: list[dict[str, object]],
) -> ContextTrace:
    """只记录稳定工具来源 ID，不把参数或结果复制进 Trace。"""

    source_ids = tuple(
        "tool:"
        + str(item.get("tool_name", "unknown"))
        + ":"
        + sha256(repr(item.get("arguments", {})).encode("utf-8")).hexdigest()[:16]
        for item in evidence
    )
    return replace(trace, tool_source_ids=tuple(dict.fromkeys(source_ids)))
