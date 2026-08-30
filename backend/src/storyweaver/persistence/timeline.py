"""会话原始事件到门户展示时间线的确定性投影。"""

from __future__ import annotations

from collections.abc import Iterable

from ..application.models import TranscriptEvent


class TimelineProjector:
    """保留一套用户可见协议，前端不再猜测计划、确认和任务的最终状态。"""

    VISIBLE_TYPES = frozenset({
        "message_added", "book_bound", "story_timeline_rewritten",
        "action_started", "action_completed", "action_failed",
        "chapter_plan_prepared", "chapter_plan_revised", "chapter_plan_approved",
        "chapter_plan_confirmed",
        "chapter_plan_rejected", "chapter_plan_cancelled", "chapter_plan_expired",
        "action_proposal_pending", "action_proposal_confirmed", "action_proposal_cancelled",
    })

    def project(self, events: Iterable[TranscriptEvent]) -> tuple[TranscriptEvent, ...]:
        """每个动作、计划、确认请求只留下最新投影；顺序仍以原事件序号为准。"""

        latest: dict[tuple[str, str], TranscriptEvent] = {}
        passthrough: list[TranscriptEvent] = []
        for event in events:
            if event.event_type not in self.VISIBLE_TYPES:
                continue
            key = self._entity_key(event)
            if key is None:
                passthrough.append(event)
            else:
                latest[key] = event
        return tuple(sorted([*passthrough, *latest.values()], key=lambda item: item.sequence))

    @staticmethod
    def _entity_key(event: TranscriptEvent) -> tuple[str, str] | None:
        payload = event.payload
        if event.event_type.startswith("chapter_plan_"):
            proposal_id = payload.get("proposal_id")
            return ("plan", str(proposal_id)) if proposal_id else None
        if event.event_type.startswith("action_proposal_"):
            proposal_id = payload.get("action_proposal_id")
            return ("confirmation", str(proposal_id)) if proposal_id else None
        if event.event_type.startswith("action_"):
            run_id = payload.get("root_run_id") or payload.get("run_id")
            return ("action", str(run_id)) if run_id else None
        return None
