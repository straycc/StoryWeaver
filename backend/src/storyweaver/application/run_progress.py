"""Web 长任务的进程内实时进度仓库。"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed"})


@dataclass(frozen=True, slots=True)
class RunProgressEvent:
    """一次运行中的单条可重放进度事件。"""

    sequence: int
    run_id: str
    event_type: str
    created_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(slots=True)
class _RunState:
    session_id: str
    action: str
    label: str
    events: list[RunProgressEvent] = field(default_factory=list)
    terminal: bool = False


class RunProgressStore:
    """支持 SSE 重放和阻塞等待的线程安全运行状态仓库。"""

    def __init__(self, *, maximum_runs: int = 100) -> None:
        if maximum_runs < 1:
            raise ValueError("maximum_runs 必须大于 0")
        self._maximum_runs = maximum_runs
        self._condition = threading.Condition(threading.RLock())
        self._runs: dict[str, _RunState] = {}

    def start_run(
        self,
        run_id: str,
        *,
        session_id: str,
        action: str,
        label: str,
    ) -> RunProgressEvent:
        self._validate_run_id(run_id)
        with self._condition:
            if run_id in self._runs:
                raise ValueError(f"运行已存在：{run_id}")
            self._discard_oldest_terminal_runs()
            self._runs[run_id] = _RunState(
                session_id=session_id,
                action=action,
                label=label,
            )
            return self._append_locked(
                run_id,
                "run_started",
                {
                    "session_id": session_id,
                    "action": action,
                    "label": label,
                },
            )

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> RunProgressEvent:
        with self._condition:
            state = self._require_run(run_id)
            if state.terminal:
                raise ValueError(f"运行已经结束：{run_id}")
            return self._append_locked(run_id, event_type, payload or {})

    def complete_run(
        self,
        run_id: str,
        *,
        summary: str | None = None,
    ) -> RunProgressEvent:
        return self._finish_run(
            run_id,
            "run_completed",
            {"summary": summary} if summary else {},
        )

    def fail_run(self, run_id: str, *, error: str) -> RunProgressEvent:
        return self._finish_run(run_id, "run_failed", {"error": error})

    def snapshot(self, run_id: str) -> tuple[RunProgressEvent, ...]:
        with self._condition:
            return tuple(self._require_run(run_id).events)

    def wait_for_run(self, run_id: str, *, timeout: float) -> bool:
        """等待 POST 请求注册运行，解决浏览器并发建立 SSE 的竞态。"""

        self._validate_run_id(run_id)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while run_id not in self._runs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wait_after(
        self,
        run_id: str,
        *,
        after_sequence: int,
        timeout: float,
    ) -> tuple[tuple[RunProgressEvent, ...], bool]:
        """返回游标后的事件；暂无事件时等待，终态时立即结束。"""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                state = self._require_run(run_id)
                events = tuple(
                    item for item in state.events if item.sequence > after_sequence
                )
                if events or state.terminal:
                    return events, state.terminal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (), state.terminal
                self._condition.wait(remaining)

    def run_metadata(self, run_id: str) -> dict[str, Any]:
        with self._condition:
            state = self._require_run(run_id)
            return {
                "run_id": run_id,
                "session_id": state.session_id,
                "action": state.action,
                "label": state.label,
                "terminal": state.terminal,
            }

    def _finish_run(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> RunProgressEvent:
        with self._condition:
            state = self._require_run(run_id)
            if state.terminal:
                return state.events[-1]
            event = self._append_locked(run_id, event_type, payload)
            state.terminal = True
            self._condition.notify_all()
            return event

    def _append_locked(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> RunProgressEvent:
        state = self._require_run(run_id)
        event = RunProgressEvent(
            sequence=len(state.events) + 1,
            run_id=run_id,
            event_type=event_type,
            created_at=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        state.events.append(event)
        if event_type in _TERMINAL_EVENT_TYPES:
            state.terminal = True
        self._condition.notify_all()
        return event

    def _discard_oldest_terminal_runs(self) -> None:
        overflow = len(self._runs) - self._maximum_runs + 1
        if overflow <= 0:
            return
        terminal_ids = [
            run_id for run_id, state in self._runs.items() if state.terminal
        ]
        for run_id in terminal_ids[:overflow]:
            del self._runs[run_id]
        if len(self._runs) >= self._maximum_runs:
            raise RuntimeError("运行进度仓库已满，请等待当前任务结束")

    def _require_run(self, run_id: str) -> _RunState:
        self._validate_run_id(run_id)
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"运行不存在：{run_id}") from exc

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id 格式不合法")


def progress_event_data(event: RunProgressEvent) -> dict[str, Any]:
    """转换为 HTTP/SSE 可序列化结构。"""

    return {
        "sequence": event.sequence,
        "run_id": event.run_id,
        "event_type": event.event_type,
        "created_at": event.created_at,
        "payload": dict(event.payload),
    }
