"""Skill 激活、规范化内容冻结和任务快照创建。"""

from __future__ import annotations

from hashlib import sha256

from ..context import TokenEstimator
from .models import AppliedSkill, CreativeTaskContext, render_skill_catalog
from .policy import SkillPolicy
from .registry import SkillRegistry


class SkillService:
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        policy: SkillPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or SkillPolicy()

    def activate(
        self,
        *,
        request: str,
        skill_ids: tuple[str, ...] = (),
    ) -> CreativeTaskContext:
        normalized = tuple(item.strip().lower() for item in skill_ids)
        if len(normalized) != len(set(normalized)):
            raise ValueError("skill_ids 不能包含重复 Skill")
        if len(normalized) > self.policy.max_activated_skills:
            raise ValueError(
                f"一次最多启用 {self.policy.max_activated_skills} 个 Skill"
            )
        applied: list[AppliedSkill] = []
        total_bytes = 0
        for skill_id in normalized:
            package = self.registry.load(skill_id)
            encoded = package.content.encode("utf-8")
            total_bytes += len(encoded)
            applied.append(
                AppliedSkill(
                    skill_id=skill_id,
                    name=package.metadata.name,
                    description=package.metadata.description,
                    source=package.metadata.source,
                    content=package.content,
                    content_hash=sha256(encoded).hexdigest(),
                )
            )
        if total_bytes > self.policy.max_snapshot_bytes:
            raise ValueError(
                f"Skill Snapshot 共 {total_bytes} 字节，超过限制 "
                f"{self.policy.max_snapshot_bytes} 字节"
            )
        context = CreativeTaskContext(request=request, applied_skills=tuple(applied))
        catalog = render_skill_catalog(context)
        tokens = TokenEstimator.estimate(catalog)
        if tokens > self.policy.metadata_catalog_tokens:
            raise ValueError(
                f"Skill metadata catalog 估算 {tokens} Token，超过限制 "
                f"{self.policy.metadata_catalog_tokens} Token"
            )
        return context
