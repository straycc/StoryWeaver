"""小说创作领域异常。"""


class NovelCreationError(Exception):
    """小说创作模块的基础异常。"""


class NovelAgentError(NovelCreationError):
    """小说专业 Agent 调用失败。"""


class NovelFoundationValidationError(NovelCreationError, ValueError):
    """Architect 生成的小说基础资料不满足建书约束。"""


class ChapterPlanValidationError(NovelCreationError, ValueError):
    """Planner 生成的章节计划不满足项目约束。"""


class ChapterDraftValidationError(NovelCreationError, ValueError):
    """Writer 生成的章节正文不满足确定性约束。"""


class ChapterReviewError(NovelCreationError, ValueError):
    """Reviewer 或 Reviser 的输出不满足审查契约。"""


class ChapterAnalysisError(NovelCreationError, ValueError):
    """ChapterAnalyzer 的输出无法形成有效状态增量。"""


class ChapterPipelineError(NovelCreationError):
    """写下一章 Pipeline 的阶段输出或编排无效。"""


class BookBusyError(NovelCreationError):
    """同一本作品已有章节生成任务正在执行。"""


class StateTransitionError(NovelCreationError, ValueError):
    """状态增量违反领域不变量。"""


class ProjectStoreError(NovelCreationError):
    """项目存储错误。"""


class ProjectAlreadyExistsError(ProjectStoreError):
    """项目 ID 已经存在。"""


class ProjectNotFoundError(ProjectStoreError):
    """项目不存在。"""


class ProjectPersistenceError(ProjectStoreError):
    """项目文件无法可靠读取或写入。"""


class ChapterCommitError(ProjectStoreError):
    """章节提交失败。"""


class SerializationError(ProjectPersistenceError):
    """持久化数据不符合当前数据契约。"""
