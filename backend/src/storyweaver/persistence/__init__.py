"""PostgreSQL 持久化实现。

领域层只依赖仓储提供的同步方法；HTTP 与后台任务层不直接操作 ORM。
"""

from .database import Database, DatabaseSettings
from .jobs import Job, JobEvent, JobRepository
from .memory_store import PostgresLongTermMemoryStore
from .project_store import PostgresStoryProjectRepository
from .session_store import PostgresChatSessionRepository
from .action_proposals import ActionProposal, ActionProposalRepository
from .creative_control import CreativeControl, CreativeControlRepository
from .context_snapshots import ContextSnapshot, ContextSnapshotRepository
from .simulations import SimulationRepository
from .deletion import BookDeletionResult, DeletionConflictError, PostgresDeletionRepository

__all__ = [
    "Database",
    "DatabaseSettings",
    "Job",
    "JobEvent",
    "JobRepository",
    "PostgresLongTermMemoryStore",
    "PostgresStoryProjectRepository",
    "PostgresChatSessionRepository",
    "ActionProposal",
    "ActionProposalRepository",
    "CreativeControl",
    "CreativeControlRepository",
    "ContextSnapshot",
    "ContextSnapshotRepository",
    "SimulationRepository",
    "BookDeletionResult",
    "DeletionConflictError",
    "PostgresDeletionRepository",
]
