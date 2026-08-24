"""Context V2 的共享来源、预算与审计协议。

该模块不决定任一 Agent 应读取什么；各 Context View 自己产出候选来源，
这里仅负责标识、版本、Token 预算、压缩与可重现 Trace。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Literal


Disposition = Literal["selected", "excluded", "compressed", "tool_result"]


class ContextBudgetExceededError(ValueError):
    """所有 protected 来源仍无法装入预算时显式失败。"""


@dataclass(frozen=True, slots=True)
class ContextSourceRef:
    source_id: str
    source_type: str
    content_hash: str
    revision: str | None = None
    book_version: int | None = None
    source_version: int | None = None
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    source: ContextSourceRef
    content: str
    reason: str
    protected: bool
    priority: int
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class ContextSelectionRecord:
    source: ContextSourceRef
    disposition: Disposition
    protected: bool
    priority: int
    estimated_tokens: int
    reason: str
    rendered_content: str | None = None
    # 仅工具证据使用；普通 Context 来源保持为空，避免人为制造第二套模型。
    tool_name: str | None = None
    tool_arguments: object | None = None
    raw_tool_arguments: str | None = None
    succeeded: bool | None = None
    truncated: bool | None = None


@dataclass(frozen=True, slots=True)
class ContextTraceV2:
    agent_role: str
    policy_version: str
    book_version: int | None
    renderer_version: str
    dynamic_budget: int
    estimated_tokens: int
    selected: tuple[ContextSelectionRecord, ...]
    excluded: tuple[ContextSelectionRecord, ...]
    tool_results: tuple[ContextSelectionRecord, ...] = ()
    notes: tuple[str, ...] = ()

    def to_data(self) -> dict[str, object]:
        return asdict(self)


class TokenEstimator:
    """无 tokenizer 依赖时的统一保守估算。"""

    @staticmethod
    def estimate(content: str) -> int:
        return max(1, (len(content.strip()) + 1) // 2)


class Budgeter:
    """普通来源裁剪、保护来源 Digest 与超限检测。"""

    def __init__(self, *, token_budget: int, estimator: TokenEstimator | None = None) -> None:
        if token_budget < 1:
            raise ValueError("token_budget 必须大于 0")
        self.token_budget = token_budget
        self.estimator = estimator or TokenEstimator()

    def select(
        self,
        candidates: tuple[ContextCandidate, ...],
    ) -> tuple[tuple[ContextCandidate, ...], tuple[ContextSelectionRecord, ...], tuple[ContextSelectionRecord, ...]]:
        protected = [item for item in candidates if item.protected]
        ordinary = [item for item in candidates if not item.protected]
        selected: list[ContextCandidate] = []
        compressed: list[ContextSelectionRecord] = []

        for item in protected:
            selected.append(item)
        used = self._total(selected)
        if used > self.token_budget:
            # 只压缩明确提供 digest 的 protected 来源；绝不删除它。
            for index, item in enumerate(tuple(selected)):
                if used <= self.token_budget or not item.digest:
                    continue
                # content_hash 必须代表模型实际看到的 Digest，而不是被压缩前原文。
                digest_source = replace(
                    item.source,
                    content_hash="sha256:" + sha256(item.digest.encode("utf-8")).hexdigest(),
                )
                digest_candidate = ContextCandidate(
                    source=digest_source,
                    content=item.digest,
                    reason=item.reason,
                    protected=True,
                    priority=item.priority,
                    digest=None,
                )
                selected[index] = digest_candidate
                compressed.append(self._record(item, "compressed", rendered=item.digest))
                used = self._total(selected)
        if used > self.token_budget:
            raise ContextBudgetExceededError(
                f"protected Context 估算 {used} Token，超过预算 {self.token_budget}；拒绝静默丢弃硬约束"
            )

        excluded: list[ContextSelectionRecord] = []
        for item in sorted(ordinary, key=lambda value: (-value.priority, value.source.source_id)):
            tokens = self._tokens(item.content)
            if used + tokens <= self.token_budget:
                selected.append(item)
                used += tokens
            else:
                excluded.append(self._record(item, "excluded"))
        selected_records = tuple(self._record(item, "selected", rendered=item.content) for item in selected)
        return tuple(selected), selected_records + tuple(compressed), tuple(excluded)

    def _total(self, candidates: list[ContextCandidate]) -> int:
        return sum(self._tokens(item.content) for item in candidates)

    def _tokens(self, content: str) -> int:
        return self.estimator.estimate(content) + 8

    def _record(self, candidate: ContextCandidate, disposition: Disposition, *, rendered: str | None = None) -> ContextSelectionRecord:
        return ContextSelectionRecord(
            source=candidate.source,
            disposition=disposition,
            protected=candidate.protected,
            priority=candidate.priority,
            estimated_tokens=self._tokens(rendered if rendered is not None else candidate.content),
            reason=candidate.reason,
            rendered_content=rendered,
        )


def source_ref(
    *,
    source_id: str,
    source_type: str,
    content: str,
    book_version: int | None,
    revision: str | None = None,
    source_version: int | None = None,
    locator: str | None = None,
) -> ContextSourceRef:
    """以实际渲染内容计算 hash，保证 Snapshot 可验证。"""

    return ContextSourceRef(
        source_id=source_id,
        source_type=source_type,
        content_hash="sha256:" + sha256(content.encode("utf-8")).hexdigest(),
        revision=revision,
        book_version=book_version,
        source_version=source_version,
        locator=locator,
    )


def trace_from_candidates(
    *,
    agent_role: str,
    policy_version: str,
    book_version: int | None,
    token_budget: int,
    candidates: tuple[ContextCandidate, ...],
    renderer_version: str = "context-renderer-v2.1",
    notes: tuple[str, ...] = (),
) -> tuple[tuple[ContextCandidate, ...], ContextTraceV2]:
    budgeter = Budgeter(token_budget=token_budget)
    selected, selected_records, excluded_records = budgeter.select(candidates)
    estimated = sum(record.estimated_tokens for record in selected_records if record.disposition == "selected")
    return selected, ContextTraceV2(
        agent_role=agent_role,
        policy_version=policy_version,
        book_version=book_version,
        renderer_version=renderer_version,
        dynamic_budget=token_budget,
        estimated_tokens=estimated,
        selected=selected_records,
        excluded=excluded_records,
        notes=notes,
    )


def trace_from_selected_entries(
    *,
    agent_role: str,
    policy_version: str,
    book_version: int | None,
    token_budget: int,
    entries: object,
    excluded_source_ids: tuple[str, ...] = (),
    renderer_version: str = "context-renderer-v2.1",
    notes: tuple[str, ...] = (),
) -> ContextTraceV2:
    """把既有确定性 Selector 的选中结果投影为 V2 Trace。

    Context V2 不替换 Writer 等领域 Selector；这个函数只为实际入模的
    ContextEntry 计算内容哈希和可复现的选择记录。
    """

    estimator = TokenEstimator()
    selected: list[ContextSelectionRecord] = []
    for entry in entries:
        content = str(getattr(entry, "content"))
        source = source_ref(
            source_id=str(getattr(entry, "source_id")),
            source_type=str(getattr(entry, "source_type")),
            content=content,
            book_version=book_version,
        )
        selected.append(
            ContextSelectionRecord(
                source=source,
                disposition="selected",
                protected=bool(getattr(entry, "protected", False)),
                priority=int(getattr(entry, "priority", 0)),
                estimated_tokens=estimator.estimate(content) + 8,
                reason=str(getattr(entry, "reason", "已由领域 Selector 选中")),
                rendered_content=content,
            )
        )
    excluded = tuple(
        ContextSelectionRecord(
            source=source_ref(
                source_id=source_id,
                source_type="excluded_source",
                content=source_id,
                book_version=book_version,
            ),
            disposition="excluded",
            protected=False,
            priority=0,
            estimated_tokens=0,
            reason="领域 Selector 的预算或相关性规则排除",
        )
        for source_id in excluded_source_ids
    )
    return ContextTraceV2(
        agent_role=agent_role,
        policy_version=policy_version,
        book_version=book_version,
        renderer_version=renderer_version,
        dynamic_budget=token_budget,
        estimated_tokens=sum(item.estimated_tokens for item in selected),
        selected=tuple(selected),
        excluded=excluded,
        notes=notes,
    )


def with_tool_results(
    trace: ContextTraceV2,
    *,
    evidence: list[dict[str, object]],
) -> ContextTraceV2:
    """追加 Planner/Reviewer 检索证据，不改写初始 Context 选择。"""

    records = tuple(
        tool_result_record(
            tool_name=str(item.get("tool_name", "unknown")),
            arguments=item.get("arguments", {}),
            result=item.get("result"),
            book_version=trace.book_version,
            succeeded=bool(item.get("succeeded", True)),
            truncated=bool(item.get("truncated", False)),
            raw_arguments=(
                str(item["raw_arguments"])
                if item.get("raw_arguments") is not None
                else None
            ),
        )
        for item in evidence
    )
    return replace(trace, tool_results=records)


def tool_result_record(
    *,
    tool_name: str,
    arguments: object,
    result: object,
    book_version: int | None,
    succeeded: bool = True,
    truncated: bool = False,
    raw_arguments: str | None = None,
) -> ContextSelectionRecord:
    """将运行中检索到的证据纳入最终 Trace。"""

    rendered = repr(result)
    source = source_ref(
        source_id=f"tool:{tool_name}:{sha256(repr(arguments).encode('utf-8')).hexdigest()[:16]}",
        source_type="tool_result",
        content=rendered,
        book_version=book_version,
        locator=tool_name,
    )
    suffix = "成功" if succeeded else "失败"
    return ContextSelectionRecord(
        source=source,
        disposition="tool_result",
        protected=False,
        priority=0,
        estimated_tokens=TokenEstimator.estimate(rendered),
        reason=f"{tool_name} {suffix}" + ("；结果已截断" if truncated else ""),
        rendered_content=rendered,
        tool_name=tool_name,
        tool_arguments=arguments,
        raw_tool_arguments=raw_arguments,
        succeeded=succeeded,
        truncated=truncated,
    )
