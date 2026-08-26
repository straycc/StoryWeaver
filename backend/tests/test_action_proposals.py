"""主 Agent ActionProposal 的持久状态边界。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storyweaver.persistence import ActionProposalRepository, Database, DatabaseSettings, PostgresChatSessionRepository


class ActionProposalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.database = Database(DatabaseSettings(f"sqlite+pysqlite:///{Path(self._directory.name) / 'test.db'}"))
        self.database.create_schema()
        self.sessions = PostgresChatSessionRepository(self.database)
        self.session = self.sessions.create_session(book_id="book-1")
        self.repository = ActionProposalRepository(self.database)

    def tearDown(self) -> None:
        self.database.dispose()
        self._directory.cleanup()

    def test_confirm_then_project_terminal_result(self) -> None:
        proposal = self.repository.create(
            session_id=self.session.session_id, book_id="book-1", action_type="rewrite_chapter",
            payload={"chapter_number": 2}, summary="重写第 2 章",
        )
        self.assertEqual(proposal.status, "pending")
        confirmed = self.repository.confirm(proposal.proposal_id, job_id="job-1")
        self.assertEqual((confirmed.status, confirmed.job_id), ("confirmed", "job-1"))
        completed = self.repository.project_job_terminal(proposal.proposal_id, succeeded=True)
        self.assertEqual(completed.status, "completed")

    def test_cancelled_proposal_cannot_be_confirmed_again(self) -> None:
        proposal = self.repository.create(
            session_id=self.session.session_id, book_id="book-1", action_type="rewrite_chapter",
            payload={"chapter_number": 2}, summary="重写第 2 章",
        )
        self.repository.cancel(proposal.proposal_id)
        with self.assertRaises(ValueError):
            self.repository.confirm(proposal.proposal_id, job_id="job-1")


if __name__ == "__main__":
    unittest.main()
