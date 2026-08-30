"""会话时间线的新工作流状态投影测试。"""

from __future__ import annotations

import unittest

from storyweaver.application.models import TranscriptEvent
from storyweaver.persistence.timeline import TimelineProjector


def event(sequence: int, event_type: str, payload: dict[str, object]) -> TranscriptEvent:
    return TranscriptEvent(
        schema_version=2,
        event_id=f"event-{sequence}",
        session_id="session-1",
        sequence=sequence,
        event_type=event_type,
        created_at="2026-08-29T00:00:00+00:00",
        payload=payload,
    )


class TimelineProjectionTests(unittest.TestCase):
    def test_approved_plan_replaces_pending_projection(self) -> None:
        projected = TimelineProjector().project((
            event(1, "chapter_plan_prepared", {"proposal_id": "plan-1", "status": "pending"}),
            event(2, "chapter_plan_approved", {"proposal_id": "plan-1", "status": "approved"}),
        ))

        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].event_type, "chapter_plan_approved")

    def test_action_proposal_events_are_valid_transcript_events(self) -> None:
        pending = event(
            1,
            "action_proposal_pending",
            {"action_proposal_id": "action-1", "status": "pending"},
        )
        confirmed = event(
            2,
            "action_proposal_confirmed",
            {"action_proposal_id": "action-1", "status": "confirmed"},
        )

        projected = TimelineProjector().project((pending, confirmed))
        self.assertEqual(tuple(item.event_type for item in projected), ("action_proposal_confirmed",))


if __name__ == "__main__":
    unittest.main()
