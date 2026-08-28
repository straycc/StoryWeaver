"""把研究阶段的原始工具结果编译成有限 EvidencePackage。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from .budget import TokenEstimator


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    """阶段隔离后交给 Report Agent 的唯一研究产物。"""

    evidence: tuple[Mapping[str, object], ...]
    estimated_tokens: int
    source_count: int
    excluded_count: int
    truncated_count: int

    def to_data(self) -> dict[str, object]:
        return {
            "evidence_count": len(self.evidence),
            "source_count": self.source_count,
            "excluded_count": self.excluded_count,
            "truncated_count": self.truncated_count,
            "estimated_tokens": self.estimated_tokens,
            "evidence": list(self.evidence),
        }


class EvidenceCompiler:
    """对 ToolRuntime 的有限结果去重、排序并执行 Evidence 总预算。"""

    def __init__(
        self,
        *,
        token_budget: int,
        estimator: TokenEstimator | None = None,
    ) -> None:
        if token_budget < 1:
            raise ValueError("Evidence token_budget 必须大于 0")
        self.token_budget = token_budget
        self.estimator = estimator or TokenEstimator()

    def compile(self, bounded_results: Sequence[Mapping[str, object]]) -> EvidencePackage:
        unique: list[Mapping[str, object]] = []
        seen: set[str] = set()
        for item in bounded_results:
            key = self._key(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        # 成功命中优先，其次为空结果，最后才是失败诊断；同级保持调用顺序。
        ranked = sorted(
            enumerate(unique),
            key=lambda pair: (-self._priority(pair[1]), pair[0]),
        )
        selected: list[Mapping[str, object]] = []
        used = 0
        truncated_count = 0
        for _, item in ranked:
            normalized = self._normalize(item)
            tokens = self.estimator.estimate(self._serialize(normalized)) + 8
            if used + tokens > self.token_budget:
                continue
            selected.append(normalized)
            used += tokens
            truncated_count += int(bool(normalized["truncated"]))
        return EvidencePackage(
            evidence=tuple(selected),
            estimated_tokens=used,
            source_count=len(bounded_results),
            excluded_count=len(unique) - len(selected),
            truncated_count=truncated_count,
        )

    @staticmethod
    def _normalize(
        item: Mapping[str, object],
    ) -> Mapping[str, object]:
        """规范化字段，但不再次截断 ToolRuntime 已限制的结果。"""

        return {
            "tool_name": str(item.get("tool_name", "unknown")),
            "arguments": item.get("arguments", {}),
            "status": str(item.get("status", "success" if item.get("succeeded", True) else "error")),
            "succeeded": bool(item.get("succeeded", True)),
            "result": item.get("result"),
            "truncated": bool(item.get("truncated", False)),
        }

    @staticmethod
    def _serialize(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def _key(cls, item: Mapping[str, object]) -> str:
        return f"{item.get('tool_name', 'unknown')}:{cls._serialize(item.get('arguments', {}))}"

    @staticmethod
    def _priority(item: Mapping[str, object]) -> int:
        if not bool(item.get("succeeded", True)):
            return 0
        result = item.get("result")
        if isinstance(result, Mapping):
            if result.get("matched") is False:
                return 1
            collections = [value for value in result.values() if isinstance(value, (list, tuple))]
            if collections and all(not value for value in collections):
                return 1
        return 2
