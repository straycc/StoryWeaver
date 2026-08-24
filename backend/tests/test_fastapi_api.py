"""FastAPI API 的轻量边界测试。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from storyweaver.api import create_app
from storyweaver.novel_creation.application import NovelApplicationSettings
from storyweaver.persistence import Database, DatabaseSettings


class FastApiTests(unittest.TestCase):
    def test_health_and_empty_books(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(base_url="https://example.invalid/v1", model="test", api_key="test"),
                database_url=url,
            )
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/health").json(), {"status": "ok"})
                self.assertEqual(client.get("/api/v1/books").json(), {"books": []})

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


if __name__ == "__main__":
    unittest.main()
