"""每次模型调用前的 Skill 相关性解析。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from hashlib import sha256
import json
import logging

from agents import ModelSettings
from pydantic import BaseModel, ConfigDict

from ..context import TokenEstimator
from ..llm import WorkerSettings, run_structured_worker
from .models import CreativeTaskContext, SkillResolution, render_applied_skill
from .policy import SkillPolicy
from .registry import SkillRegistry


_LOGGER = logging.getLogger(__name__)


SkillSelector = Callable[
    [str, str, tuple[tuple[str, str, int], ...], int, int],
    Awaitable[tuple[str, ...]],
]


class _SkillSelectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_ids: list[str]


class SkillInvocationResolver:
    """在每条用户请求边界解析显式或隐式 Skill 调用。

    显式选择是确定性信号；只有没有显式 ID 时，才允许模型根据本轮请求、
    最近对话和启动时发现的 metadata 做隐式匹配。这里只返回 ID，正文仍由
    ``SkillService.activate`` 在创建 Job 前读取并冻结。
    """

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        selector: "ModelImplicitSkillSelector | None" = None,
        max_skills: int = 3,
    ) -> None:
        if max_skills < 1:
            raise ValueError("隐式 Skill 最大数量必须大于 0")
        self.registry = registry
        self.selector = selector
        self.max_skills = max_skills
        self._cache: dict[str, tuple[str, ...]] = {}

    async def resolve(
        self,
        *,
        request: str,
        recent_conversation: str,
        explicit_skill_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        normalized = tuple(item.strip().lower() for item in explicit_skill_ids)
        if normalized:
            if len(normalized) != len(set(normalized)):
                raise ValueError("skill_ids 不能包含重复 Skill")
            self._validate(normalized)
            return normalized
        if self.selector is None:
            return ()
        metadata = self.registry.list()
        if not metadata:
            return ()
        key = sha256(
            (request.strip() + "\n" + recent_conversation.strip()).encode("utf-8")
        ).hexdigest()
        if key in self._cache:
            return self._cache[key]
        try:
            selected = await self.selector(
                request.strip(),
                recent_conversation.strip(),
                tuple((item.skill_id, item.description) for item in metadata),
                self.max_skills,
            )
            selected = tuple(item.strip().lower() for item in selected)
            if len(selected) != len(set(selected)):
                raise ValueError("隐式 Resolver 返回重复 Skill ID")
            self._validate(selected)
            self._cache[key] = selected
        except Exception as exc:
            # 隐式匹配只是增强能力，解析服务抖动时不能阻断普通聊天。
            _LOGGER.warning(
                "隐式 Skill 匹配失败，按无 Skill 继续 error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )
            selected = ()
        return selected

    def _validate(self, skill_ids: tuple[str, ...]) -> None:
        unknown = tuple(
            skill_id for skill_id in skill_ids if self.registry.get(skill_id) is None
        )
        if unknown:
            raise ValueError(f"Resolver 返回未知 Skill：{', '.join(unknown)}")


class ModelImplicitSkillSelector:
    """根据当前请求与最近对话，从已安装 Skill metadata 中隐式选取能力。"""

    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings

    async def __call__(
        self,
        request: str,
        recent_conversation: str,
        catalog: tuple[tuple[str, str], ...],
        max_skills: int,
    ) -> tuple[str, ...]:
        prompt = (
            "判断当前用户请求是否应隐式调用某个 Skill。显式选择已由宿主处理；"
            "这里只处理没有显式选择的情况。结合最近对话识别简短的延续回答，"
            "但不要仅因以前使用过 Skill 就继续启用。普通闲聊或不匹配时返回空数组。"
            "只返回候选 ID，不得虚构。\n\n"
            f"当前请求：{request}\n"
            f"最近对话：\n{recent_conversation or '无'}\n"
            f"最多选择：{max_skills}\n"
            "候选 Skill metadata：\n"
            + json.dumps(
                [
                    {"skill_id": skill_id, "description": description}
                    for skill_id, description in catalog
                ],
                ensure_ascii=False,
            )
        )
        result = await run_structured_worker(
            settings=self.settings,
            prompt=prompt,
            output_type=_SkillSelectionOutput,
            tracing_enabled=True,
        )
        return tuple(result.skill_ids[:max_skills])


def build_model_implicit_skill_selector(
    *,
    model: object,
    timeout_seconds: float,
) -> ModelImplicitSkillSelector:
    return ModelImplicitSkillSelector(
        WorkerSettings(
            worker_id="skill-implicit-resolver",
            name="Skill 隐式调用解析器",
            instructions=(
                "你只负责根据当前请求、最近对话和 Skill description 判断本轮是否"
                "需要 Skill。宁可返回空数组，也不要牵强匹配。"
            ),
            model=model,
            model_settings=ModelSettings(temperature=0, max_tokens=256),
            timeout_seconds=min(timeout_seconds, 15.0),
        )
    )


class ModelSkillSelector:
    """用一次低温度结构化调用选择本次 invocation 的相关 Skill。"""

    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings

    async def __call__(
        self,
        task_request: str,
        objective: str,
        catalog: tuple[tuple[str, str, int], ...],
        token_budget: int,
        max_skills: int,
    ) -> tuple[str, ...]:
        prompt = (
            "根据当前 CreativeTask 和本次 invocation 目标，从候选 Skill 中选择"
            "真正相关的少量 Skill。只返回候选 ID，不得虚构 ID。允许返回空数组。\n\n"
            f"CreativeTask：{task_request}\n"
            f"Invocation objective：{objective}\n"
            f"完整 Skill Token 预算：{token_budget}\n"
            f"最多选择：{max_skills}\n"
            "候选 metadata：\n"
            + json.dumps(
                [
                    {
                        "skill_id": skill_id,
                        "description": description,
                        "estimated_tokens": estimated_tokens,
                    }
                    for skill_id, description, estimated_tokens in catalog
                ],
                ensure_ascii=False,
            )
        )
        result = await run_structured_worker(
            settings=self.settings,
            prompt=prompt,
            output_type=_SkillSelectionOutput,
            tracing_enabled=True,
        )
        return tuple(result.skill_ids[:max_skills])


def build_model_skill_selector(
    *,
    model: object,
    timeout_seconds: float,
) -> ModelSkillSelector:
    return ModelSkillSelector(
        WorkerSettings(
            worker_id="skill-resolver",
            name="Skill 相关性解析器",
            instructions=(
                "你只负责从已激活 Skill metadata 中选择与当前任务目标相关的 ID。"
                "Skill 不绑定 Agent 或 Workflow。"
            ),
            model=model,
            model_settings=ModelSettings(temperature=0, max_tokens=512),
            timeout_seconds=min(timeout_seconds, 30.0),
        )
    )


class SkillResolver:
    def __init__(
        self,
        *,
        selector: SkillSelector | None = None,
        policy: SkillPolicy | None = None,
    ) -> None:
        self.selector = selector
        self.policy = policy or SkillPolicy()
        self._cache: dict[str, SkillResolution] = {}

    async def resolve(
        self,
        task: CreativeTaskContext,
        *,
        objective: str,
        token_budget: int | None = None,
    ) -> SkillResolution:
        budget = (
            self.policy.default_materialization_tokens
            if token_budget is None
            else max(0, token_budget)
        )
        if not task.applied_skills:
            return SkillResolution((), (), (), budget, 0, "empty")
        key = self._cache_key(task, objective=objective, budget=budget)
        if key in self._cache:
            return self._cache[key]
        estimates = {
            item.skill_id: TokenEstimator.estimate(render_applied_skill(item)) + 8
            for item in task.applied_skills
        }
        direct = (
            len(task.applied_skills) <= self.policy.direct_materialization_limit
            and sum(estimates.values()) <= budget
        )
        fallback = False
        if direct:
            requested = tuple(item.skill_id for item in task.applied_skills)
            strategy = "direct"
        elif self.selector is not None:
            catalog = tuple(
                (item.skill_id, item.description, estimates[item.skill_id])
                for item in task.applied_skills
            )
            try:
                requested = await self.selector(
                    task.request,
                    objective,
                    catalog,
                    budget,
                    self.policy.max_materialized_skills,
                )
                self._validate_selection(requested, task)
                strategy = "model"
            except Exception:
                requested = tuple(item.skill_id for item in task.applied_skills)
                strategy = "ordered-fallback"
                fallback = True
        else:
            requested = tuple(item.skill_id for item in task.applied_skills)
            strategy = "ordered-fallback"
            fallback = True
        selected: list[str] = []
        reasons: list[tuple[str, str]] = []
        used = 0
        for skill_id in requested:
            estimate = estimates[skill_id]
            if (
                len(selected) < self.policy.max_materialized_skills
                and used + estimate <= budget
            ):
                selected.append(skill_id)
                used += estimate
                reasons.append((skill_id, "本次 invocation 完整加载"))
            else:
                reasons.append((skill_id, "完整 SKILL.md 无法装入本次预算"))
        metadata_only = tuple(
            item.skill_id
            for item in task.applied_skills
            if item.skill_id not in selected
        )
        resolution = SkillResolution(
            materialized_ids=tuple(selected),
            metadata_only_ids=metadata_only,
            reasons=tuple(reasons),
            token_budget=budget,
            estimated_tokens=used,
            strategy=strategy,
            fallback=fallback,
        )
        self._cache[key] = resolution
        return resolution

    @staticmethod
    def _validate_selection(
        selected: tuple[str, ...],
        task: CreativeTaskContext,
    ) -> None:
        if len(selected) != len(set(selected)):
            raise ValueError("Resolver 返回重复 Skill ID")
        available = {item.skill_id for item in task.applied_skills}
        unknown = set(selected) - available
        if unknown:
            raise ValueError(f"Resolver 返回未激活 Skill：{', '.join(sorted(unknown))}")

    @staticmethod
    def _cache_key(
        task: CreativeTaskContext,
        *,
        objective: str,
        budget: int,
    ) -> str:
        payload = "\n".join(
            [task.request, objective, str(budget)]
            + [f"{item.skill_id}:{item.content_hash}" for item in task.applied_skills]
        )
        return sha256(payload.encode("utf-8")).hexdigest()
