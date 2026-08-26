"""SDK 调用向业务日志投影的最小事件契约。"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class LlmEventType(StrEnum):
    """业务层关心的模型运行事件。"""

    RUN_STARTED = "run_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_COMPLETED = "tool_completed"
    MODEL_REPAIRING = "model_repairing"
    STREAM_STARTED = "stream_started"
    TEXT_DELTA = "text_delta"
    STREAM_COMPLETED = "stream_completed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


@dataclass(frozen=True, slots=True)
class LlmEvent:
    """一次 SDK 调用的脱敏进度事件。"""

    type: LlmEventType
    worker_id: str
    data: Mapping[str, Any]


class LlmEventSink(Protocol):
    """接收 SDK 调用进度的业务观察器。"""

    async def on_event(self, event: LlmEvent) -> None:
        ...
