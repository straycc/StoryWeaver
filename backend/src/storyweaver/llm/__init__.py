"""统一的 OpenAI Agents SDK 基础设施。

本模块只保存 Provider 配置、调用事件、重试和结构化输出辅助能力；
不实现第二套 Agent Runtime 或工具注册表。
"""

from .events import LlmEvent, LlmEventSink, LlmEventType
from .retry import RetryContext, WorkerRetryPolicy, run_with_retry
from .sdk import (
    OpenAICompatibleProviderSettings,
    WorkerExecutionError,
    WorkerSettings,
    run_text_worker,
    run_structured_worker,
)
from .novel_outputs import NOVEL_OUTPUT_TYPES
from .two_phase import run_research_then_submit
from .messages import LlmMessage, LlmMessageRole
from .usage import LlmUsage
from .errors import ConfigurationError, ModelError

__all__ = [
    "LlmEvent",
    "LlmEventSink",
    "LlmEventType",
    "RetryContext",
    "WorkerRetryPolicy",
    "run_with_retry",
    "OpenAICompatibleProviderSettings",
    "WorkerExecutionError",
    "WorkerSettings",
    "NOVEL_OUTPUT_TYPES",
    "run_structured_worker",
    "run_text_worker",
    "run_research_then_submit",
    "LlmMessage",
    "LlmMessageRole",
    "LlmUsage",
    "ConfigurationError",
    "ModelError",
]
