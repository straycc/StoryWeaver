"""FastAPI API 的轻量边界测试。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from storyweaver.api import create_app
from storyweaver.novel_creation.application import NovelApplicationSettings
from storyweaver.novel_creation.models import ChapterPlanProposal, NovelProject
from storyweaver.persistence import Database, DatabaseSettings

from novel_fixtures import create_chapter_plan, create_foundation, create_initial_state, create_metadata


class FastApiTests(unittest.TestCase):
    def test_nonempty_session_can_bind_existing_book(self) -> None:
        """建书讨论会话可以原地关联作品，不能被迫新建空会话。"""

        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            metadata = create_metadata()
            app.state.novels.store.create_project(
                metadata=metadata,
                foundation=create_foundation(),
                initial_state=create_initial_state(),
            )
            with TestClient(app) as client:
                created = client.post("/api/v1/sessions", json={}).json()
                app.state.sessions.append_message(
                    created["session_id"], role="user", content="先讨论一下故事设定。"
                )
                bound = client.patch(
                    f"/api/v1/sessions/{created['session_id']}",
                    json={"book_id": metadata.book_id},
                )
                self.assertEqual(bound.status_code, 200)
                self.assertEqual(bound.json()["session_id"], created["session_id"])
                self.assertEqual(bound.json()["book_id"], metadata.book_id)
                restored = client.get(f"/api/v1/sessions/{created['session_id']}").json()
                self.assertEqual(restored["message_count"], 1)

    def test_session_action_is_a_durable_job_and_replays_timeline(self) -> None:
        """不访问模型的预设动作也必须经过 Job，并在刷新后能恢复时间线。"""

        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            with TestClient(app) as client:
                session = client.post("/api/v1/sessions", json={}).json()
                accepted = client.post(
                    f"/api/v1/sessions/{session['session_id']}/messages",
                    json={"action": "list_projects", "content": ""},
                )
                self.assertEqual(accepted.status_code, 202)
                job_id = accepted.json()["job_id"]
                for _ in range(40):
                    job = client.get(f"/api/v1/jobs/{job_id}").json()
                    if job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.025)
                self.assertEqual(job["status"], "succeeded")
                restored = client.get(f"/api/v1/sessions/{session['session_id']}").json()
                self.assertTrue(any(item["event_type"] == "action_completed" for item in restored["timeline"]))
                self.assertTrue(any(item["payload"].get("role") == "assistant" for item in restored["timeline"] if item["event_type"] == "message_added"))

    def test_create_novel_requires_foundation_confirmation_before_binding_book(self) -> None:
        """Architect 候选必须待确认，确认后才创建正式作品并绑定会话。"""

        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            metadata = create_metadata()
            foundation = create_foundation()
            candidate = NovelProject(
                metadata=metadata,
                foundation=foundation,
                state=create_initial_state(),
            )
            app.state.novels.generate_project_candidate = AsyncMock(return_value=candidate)
            with TestClient(app) as client:
                session = client.post("/api/v1/sessions", json={}).json()
                accepted = client.post(
                    f"/api/v1/sessions/{session['session_id']}/messages",
                    json={
                        "action": "create_novel",
                        "content": "创建小说《雨夜旅馆》",
                        "payload": {
                            "title": "雨夜旅馆",
                            "genre": "悬疑",
                            "premise": "年轻侦探进入废弃旅馆调查失踪案。",
                            "protagonist": "林默",
                            "central_conflict": "林默必须在谎言中找到失踪案真相。",
                            "tone": "克制、压迫",
                            "target_chapters": 6,
                            "chapter_target_words": 1200,
                        },
                    },
                )
                self.assertEqual(accepted.status_code, 202)
                job_id = accepted.json()["job_id"]
                for _ in range(40):
                    job = client.get(f"/api/v1/jobs/{job_id}").json()
                    if job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.025)
                self.assertEqual(job["status"], "succeeded")
                restored = client.get(f"/api/v1/sessions/{session['session_id']}").json()
                self.assertIsNone(restored["book_id"])
                pending = next(
                    item for item in restored["timeline"]
                    if item["event_type"] == "action_proposal_pending"
                )
                proposal = pending["payload"]
                self.assertEqual(proposal["action_type"], "confirm_foundation")
                self.assertEqual(proposal["status"], "pending")

                confirmed = client.post(
                    f"/api/v1/action-proposals/{proposal['action_proposal_id']}/confirm",
                    json={"version": proposal["payload"]["version"]},
                )
                self.assertEqual(confirmed.status_code, 202)
                confirmation_job_id = confirmed.json()["job_id"]
                for _ in range(40):
                    confirmation_job = client.get(f"/api/v1/jobs/{confirmation_job_id}").json()
                    if confirmation_job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.025)
                self.assertEqual(confirmation_job["status"], "succeeded")
                bound = client.get(f"/api/v1/sessions/{session['session_id']}").json()
                self.assertEqual(bound["book_id"], metadata.book_id)

    def test_foundation_revision_updates_only_confirmed_foundation(self) -> None:
        """设定修订须经 Proposal 确认，且不重置当前 StoryState。"""

        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            metadata = create_metadata()
            foundation = create_foundation()
            app.state.novels.store.create_project(
                metadata=metadata,
                foundation=foundation,
                initial_state=create_initial_state(),
            )
            with TestClient(app) as client:
                session = client.post("/api/v1/sessions", json={}).json()
                created = client.post(
                    f"/api/v1/books/{metadata.book_id}/foundation-revisions",
                    json={"session_id": session["session_id"], "scope": "setting"},
                )
                self.assertEqual(created.status_code, 200)
                proposal = created.json()["proposal"]
                reopened = client.post(
                    f"/api/v1/books/{metadata.book_id}/foundation-revisions",
                    json={"session_id": session["session_id"], "scope": "setting"},
                )
                self.assertEqual(reopened.status_code, 200)
                self.assertEqual(
                    reopened.json()["proposal"]["action_proposal_id"],
                    proposal["action_proposal_id"],
                )
                updated = client.patch(
                    f"/api/v1/action-proposals/{proposal['action_proposal_id']}/foundation",
                    json={"version": 1, "patch": {"premise": "修订后的故事前提。"}},
                )
                self.assertEqual(updated.status_code, 200)
                confirmed = client.post(
                    f"/api/v1/action-proposals/{proposal['action_proposal_id']}/confirm",
                    json={"version": 2},
                )
                self.assertEqual(confirmed.status_code, 202)
                job_id = confirmed.json()["job_id"]
                for _ in range(40):
                    job = client.get(f"/api/v1/jobs/{job_id}").json()
                    if job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.025)
                self.assertEqual(job["status"], "succeeded")
                project = client.get(f"/api/v1/books/{metadata.book_id}").json()
                self.assertEqual(project["foundation"]["premise"], "修订后的故事前提。")
                self.assertEqual(project["last_committed_chapter"], 0)

    def test_outline_revision_expires_pending_chapter_plan(self) -> None:
        """确认总纲修订后，旧总纲产生的候选章节计划不可继续执行。"""

        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            metadata = create_metadata()
            app.state.novels.store.create_project(
                metadata=metadata,
                foundation=create_foundation(),
                initial_state=create_initial_state(),
            )
            pending_plan = ChapterPlanProposal(
                proposal_id="pending-plan-after-old-outline",
                book_id=metadata.book_id,
                chapter_number=1,
                base_chapter_number=0,
                base_current_time="深夜十一点",
                version=1,
                status="pending",
                plan=create_chapter_plan(),
                user_instruction=None,
                feedback_history=(),
                selected_memory_ids=(),
                selected_memory_descriptions=(),
                created_at="2026-08-13T00:00:00+00:00",
                updated_at="2026-08-13T00:00:00+00:00",
            )
            app.state.novels.store.save_plan_proposal(pending_plan)
            with TestClient(app) as client:
                session = client.post("/api/v1/sessions", json={}).json()
                created = client.post(
                    f"/api/v1/books/{metadata.book_id}/foundation-revisions",
                    json={"session_id": session["session_id"], "scope": "outline"},
                )
                proposal = created.json()["proposal"]
                confirmed = client.post(
                    f"/api/v1/action-proposals/{proposal['action_proposal_id']}/confirm",
                    json={"version": 1},
                )
                self.assertEqual(confirmed.status_code, 202)
                job_id = confirmed.json()["job_id"]
                for _ in range(40):
                    job = client.get(f"/api/v1/jobs/{job_id}").json()
                    if job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.025)
                self.assertEqual(job["status"], "succeeded")
                stored = app.state.novels.store.load_plan_proposal(
                    metadata.book_id, pending_plan.proposal_id
                )
                self.assertEqual(stored.status, "expired")
                timeline = client.get(f"/api/v1/sessions/{session['session_id']}").json()["timeline"]
                self.assertTrue(any(item["event_type"] == "chapter_plan_expired" for item in timeline))

    def test_session_and_book_can_be_deleted_through_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            app.state.novels.store.create_project(
                metadata=create_metadata(),
                foundation=create_foundation(),
                initial_state=create_initial_state(),
            )
            with TestClient(app) as client:
                chat = client.post("/api/v1/sessions", json={}).json()
                self.assertEqual(
                    client.delete(f"/api/v1/sessions/{chat['session_id']}").json(),
                    {"ok": True},
                )
                self.assertEqual(client.get(f"/api/v1/sessions/{chat['session_id']}").status_code, 404)

                book_id = create_metadata().book_id
                deleted = client.delete(f"/api/v1/books/{book_id}")
                self.assertEqual(deleted.status_code, 200)
                self.assertTrue(deleted.json()["ok"])
                self.assertEqual(client.get("/api/v1/books").json(), {"books": []})


if __name__ == "__main__":
    unittest.main()
