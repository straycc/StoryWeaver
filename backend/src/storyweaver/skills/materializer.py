"""把 Skill Resolution 投影为原子 Context Candidate。"""

from __future__ import annotations

from ..context import ContextCandidate
from .models import (
    CreativeTaskContext,
    SkillMaterialization,
    render_applied_skill,
    render_skill_catalog,
)
from .resolver import SkillResolver


class SkillMaterializer:
    def __init__(self, resolver: SkillResolver) -> None:
        self.resolver = resolver

    async def materialize(
        self,
        task: CreativeTaskContext,
        *,
        objective: str,
        token_budget: int | None = None,
    ) -> SkillMaterialization:
        resolution = await self.resolver.resolve(
            task,
            objective=objective,
            token_budget=token_budget,
        )
        catalog = self.catalog(task)
        by_id = {item.skill_id: item for item in task.applied_skills}
        contents = tuple(
            (
                by_id[skill_id],
                render_applied_skill(by_id[skill_id]),
            )
            for skill_id in resolution.materialized_ids
        )
        return SkillMaterialization(catalog, contents, resolution)

    @staticmethod
    def catalog(task: CreativeTaskContext) -> str:
        """渲染始终完整暴露的 metadata catalog。"""

        return render_skill_catalog(task)

    @staticmethod
    def observation(
        task: CreativeTaskContext,
        materialized: SkillMaterialization,
    ) -> dict[str, object]:
        """生成不含 Skill 正文的运行观测数据。"""

        return {
            "activated_skills": [
                {"skill_id": item.skill_id, "content_hash": item.content_hash}
                for item in task.applied_skills
            ],
            "materialized_ids": list(materialized.resolution.materialized_ids),
            "metadata_only_ids": list(materialized.resolution.metadata_only_ids),
            "reasons": [list(item) for item in materialized.resolution.reasons],
            "token_budget": materialized.resolution.token_budget,
            "estimated_tokens": materialized.resolution.estimated_tokens,
            "strategy": materialized.resolution.strategy,
            "fallback": materialized.resolution.fallback,
        }

    async def candidates(
        self,
        task: CreativeTaskContext,
        *,
        objective: str,
        token_budget: int | None = None,
    ) -> tuple[ContextCandidate, ...]:
        materialized = await self.materialize(
            task,
            objective=objective,
            token_budget=token_budget,
        )
        candidates = [
            ContextCandidate(
                source_id="skill-catalog",
                source_type="skill_catalog",
                content=materialized.catalog,
                reason="所有已激活 Skill 的 metadata catalog",
                protected=True,
                priority=80,
            )
        ]
        candidates.extend(
            ContextCandidate(
                source_id=f"skill:{skill.skill_id}:{skill.content_hash[:16]}",
                source_type="skill",
                content=content,
                reason="Resolver 判定与本次 invocation 相关的完整 Skill",
                protected=False,
                priority=90,
            )
            for skill, content in materialized.contents
        )
        return tuple(candidates)
