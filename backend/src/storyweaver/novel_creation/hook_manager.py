"""伏笔生命周期的确定性治理入口。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .models import ChapterPlan, HookUpdate, NovelProject, StoryHook, StoryStateDelta


@dataclass(frozen=True, slots=True)
class HookGovernanceReport:
    """一次状态增量经过伏笔治理后的可观测结果。"""

    advanced_hook_ids: tuple[str, ...]
    created_hook_ids: tuple[str, ...]
    merged_pairs: tuple[tuple[str, str], ...]
    dropped_hook_ids: tuple[str, ...]
    resolved_hook_ids: tuple[str, ...]
    missed_resolution_ids: tuple[str, ...]
    ending_risk_hook_ids: tuple[str, ...]


class HookManager:
    """将 Analyzer 提议的伏笔变化收敛为可提交的权威增量。

    这个对象不判断正文是否成立；那仍是 Analyzer 与 Reviewer 的职责。
    它只实施跨章节一致的伏笔政策，避免同一线索被反复作为新伏笔入库。
    """

    max_new_hooks_per_chapter = 1
    _NON_WORD = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

    def reconcile(
        self,
        *,
        project: NovelProject,
        delta: StoryStateDelta,
        plan: ChapterPlan | None = None,
    ) -> StoryStateDelta:
        """兼容调用方：仅返回合并明显重复后的状态增量。"""

        return self.reconcile_with_report(project=project, delta=delta, plan=plan)[0]

    def reconcile_with_report(
        self,
        *,
        project: NovelProject,
        delta: StoryStateDelta,
        plan: ChapterPlan | None = None,
    ) -> tuple[StoryStateDelta, HookGovernanceReport]:
        """合并明显重复的候选伏笔，并同时返回可记录的治理摘要。"""

        existing = {
            hook.hook_id: hook
            for hook in project.state.hooks
            if hook.status != "resolved"
        }
        updates = list(delta.hook_updates)
        updated_ids = {update.hook_id for update in updates}
        accepted_new: list[StoryHook] = []
        merged_pairs: list[tuple[str, str]] = []
        dropped_hook_ids: list[str] = []
        new_hook_budget = self.new_hook_budget(project=project)
        if plan is not None:
            new_hook_budget = min(new_hook_budget, plan.hook_plan.new_hook_budget)

        for candidate in delta.new_hooks:
            matched = self._find_duplicate(candidate, tuple(existing.values()))
            if matched is not None:
                if matched.hook_id not in updated_ids:
                    updates.append(
                        HookUpdate(
                            hook_id=matched.hook_id,
                            status=self._merged_status(
                                current=matched.status,
                                proposed=candidate.status,
                            ),
                            note=(
                                f"合并候选伏笔「{candidate.display_name}」："
                                f"{candidate.description}"
                            ),
                        )
                    )
                    updated_ids.add(matched.hook_id)
                merged_pairs.append((candidate.hook_id, matched.hook_id))
                continue

            if len(accepted_new) >= new_hook_budget:
                # 超预算的候选不进入正史。Writer 仍可保留正文细节，后续章节若持续
                # 发展该线索，Analyzer 会再次提出，届时由管理器重新判断。
                dropped_hook_ids.append(candidate.hook_id)
                continue
            accepted_new.append(candidate)
            existing[candidate.hook_id] = candidate

        reconciled = replace(
            delta, new_hooks=tuple(accepted_new), hook_updates=tuple(updates)
        )
        report = HookGovernanceReport(
            advanced_hook_ids=tuple(update.hook_id for update in updates),
            created_hook_ids=tuple(hook.hook_id for hook in accepted_new),
            merged_pairs=tuple(merged_pairs),
            dropped_hook_ids=tuple(dropped_hook_ids),
            resolved_hook_ids=tuple(
                update.hook_id for update in updates if update.status == "resolved"
            ),
            missed_resolution_ids=self._missed_resolution_ids(plan, updates),
            ending_risk_hook_ids=self._ending_risk_hook_ids(
                project=project,
                updates=updates,
            ),
        )
        return reconciled, report

    def planning_guidance(self, *, project: NovelProject) -> dict[str, object]:
        """为 Planner 提供紧凑、非阻断式的伏笔节奏建议。"""

        next_chapter = project.state.last_committed_chapter + 1
        open_hooks = sorted(
            (hook for hook in project.state.hooks if hook.status != "resolved"),
            key=lambda hook: (
                -(next_chapter - hook.last_advanced_chapter),
                -hook.importance,
                hook.hook_id,
            ),
        )
        final_chapter = next_chapter >= project.metadata.target_chapters
        late_stage = next_chapter * 4 >= project.metadata.target_chapters * 3
        resolution_candidates = tuple(
            hook.hook_id for hook in open_hooks if hook.importance >= 4
        )[:3]
        return {
            "new_hook_budget": self.new_hook_budget(project=project),
            "stage": "final" if final_chapter else "late" if late_stage else "build",
            "instruction": (
                "本书最后一章：不得新增普通伏笔，必须在 hook_plan.resolve_hook_ids 列出本章要给出明确答案的主线伏笔。"
                if final_chapter
                else "优先选择 1 至 2 条未解决伏笔写入 hook_plan，并在本章推进或回收；不要无理由开新谜团。"
            ),
            "priority_hooks": tuple(open_hooks[:5]),
            "recommended_resolve_hook_ids": resolution_candidates if late_stage else (),
        }

    def new_hook_budget(self, *, project: NovelProject) -> int:
        """进入最后四分之一后禁止继续以普通伏笔扩张主线。"""

        next_chapter = project.state.last_committed_chapter + 1
        if next_chapter * 4 >= project.metadata.target_chapters * 3:
            return 0
        return self.max_new_hooks_per_chapter

    @staticmethod
    def _missed_resolution_ids(
        plan: ChapterPlan | None,
        updates: list[HookUpdate],
    ) -> tuple[str, ...]:
        if plan is None:
            return ()
        resolved_ids = {update.hook_id for update in updates if update.status == "resolved"}
        return tuple(
            hook_id
            for hook_id in plan.hook_plan.resolve_hook_ids
            if hook_id not in resolved_ids
        )

    @staticmethod
    def _ending_risk_hook_ids(
        *,
        project: NovelProject,
        updates: list[HookUpdate],
    ) -> tuple[str, ...]:
        if project.state.last_committed_chapter + 1 < project.metadata.target_chapters:
            return ()
        resolved_ids = {update.hook_id for update in updates if update.status == "resolved"}
        return tuple(
            hook.hook_id
            for hook in project.state.hooks
            if hook.importance >= 4
            and hook.status != "resolved"
            and hook.hook_id not in resolved_ids
        )

    @classmethod
    def _find_duplicate(
        cls,
        candidate: StoryHook,
        existing: tuple[StoryHook, ...],
    ) -> StoryHook | None:
        for hook in existing:
            if cls._strongly_overlaps(candidate, hook):
                return hook
        return None

    @classmethod
    def _strongly_overlaps(cls, left: StoryHook, right: StoryHook) -> bool:
        left_name = cls._normalize(left.display_name)
        right_name = cls._normalize(right.display_name)
        if left_name and right_name and (
            left_name in right_name or right_name in left_name
        ):
            return True

        left_text = cls._normalize(left.display_name + left.description)
        right_text = cls._normalize(right.display_name + right.description)
        # 六个连续字符通常已包含具体事件或物品，避免仅因同一角色姓名误合并。
        return cls._longest_common_substring(left_text, right_text) >= 6

    @classmethod
    def _normalize(cls, value: str) -> str:
        return cls._NON_WORD.sub("", value.casefold())

    @staticmethod
    def _longest_common_substring(left: str, right: str) -> int:
        if not left or not right:
            return 0
        previous = [0] * (len(right) + 1)
        longest = 0
        for left_char in left:
            current = [0]
            for index, right_char in enumerate(right, start=1):
                size = previous[index - 1] + 1 if left_char == right_char else 0
                current.append(size)
                longest = max(longest, size)
            previous = current
        return longest

    @staticmethod
    def _merged_status(*, current: str, proposed: str) -> str:
        if current == "resolved":
            return current
        if proposed == "resolved":
            return proposed
        if current == "deferred" and proposed == "open":
            return current
        return "progressing"
