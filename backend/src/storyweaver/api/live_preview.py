"""单进程内的模型文本临时预览。

预览不是业务产物：服务重启、任务取消或结构化校验失败时都允许丢失。
数据库只保存可恢复的 Job 生命周期与最终产物，避免把长正文切成大量事件行。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dataclass_field
from typing import Any


@dataclass(slots=True)
class _Preview:
    text: str = ""
    agent_id: str = ""
    field: str = "text"
    subscribers: set[asyncio.Queue[dict[str, Any]]] = dataclass_field(default_factory=set)


class LivePreviewHub:
    """为单个 FastAPI 进程提供 Job 文本预览订阅。

    它不是持久消息队列，也不承担重放职责。浏览器重连时取得当前累计文本，
    随后只接收新的增量；服务重启后预览自然消失。
    """

    def __init__(self) -> None:
        self._previews: dict[tuple[str, str], _Preview] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(self, job_id: str) -> AsyncIterator[tuple[asyncio.Queue[dict[str, Any]], tuple[dict[str, Any], ...]]]:
        """注册订阅者并返回当前快照，防止刷新页面后丢失已生成部分。"""

        # 预览是可丢弃的临时 UI 数据。SSE 客户端过慢时不允许无限积压；会在
        # 队列满时退化为一条最新全文快照，浏览器仍可恢复到正确文本。
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        async with self._lock:
            snapshots: list[dict[str, Any]] = []
            for (preview_job_id, field), preview in self._previews.items():
                if preview_job_id != job_id:
                    continue
                preview.subscribers.add(queue)
                if preview.text:
                    snapshots.append(self._event(
                        "preview_snapshot", job_id, preview.agent_id, field, text=preview.text,
                    ))
        try:
            yield queue, tuple(snapshots)
        finally:
            async with self._lock:
                for (preview_job_id, _), preview in tuple(self._previews.items()):
                    if preview_job_id == job_id:
                        preview.subscribers.discard(queue)

    async def start(self, job_id: str, *, agent_id: str, field: str) -> None:
        await self._start(job_id, agent_id=agent_id, field=field)

    async def append(self, job_id: str, *, agent_id: str, field: str, delta: str) -> None:
        if delta:
            await self._append(job_id, agent_id=agent_id, field=field, delta=delta)

    async def reset(self, job_id: str, *, agent_id: str, field: str) -> None:
        await self._reset(job_id, agent_id=agent_id, field=field)

    async def complete(self, job_id: str, *, agent_id: str, field: str) -> None:
        await self._complete(job_id, agent_id=agent_id, field=field)

    async def clear_job(self, job_id: str) -> None:
        await self._clear_job(job_id)

    async def _start(self, job_id: str, *, agent_id: str, field: str) -> None:
        async with self._lock:
            key = (job_id, field)
            preview = self._previews.setdefault(key, _Preview())
            preview.agent_id = agent_id
            preview.field = field
            event = self._event("preview_started", job_id, agent_id, field)
            self._broadcast_unlocked(preview, event)

    async def _append(self, job_id: str, *, agent_id: str, field: str, delta: str) -> None:
        async with self._lock:
            preview = self._previews.setdefault((job_id, field), _Preview())
            preview.agent_id = agent_id
            preview.field = field
            preview.text += delta
            self._broadcast_unlocked(preview, self._event(
                "preview_delta", job_id, preview.agent_id, field, delta=delta,
            ))

    async def _reset(self, job_id: str, *, agent_id: str, field: str) -> None:
        async with self._lock:
            preview = self._previews.get((job_id, field))
            if preview is None:
                return
            preview.text = ""
            self._broadcast_unlocked(preview, self._event(
                "preview_reset", job_id, agent_id, field,
            ))

    async def _complete(self, job_id: str, *, agent_id: str, field: str) -> None:
        """在所有已 await 的 delta 之后，发送明确的预览完成边界。"""

        async with self._lock:
            preview = self._previews.get((job_id, field))
            if preview is None:
                return
            self._broadcast_unlocked(preview, self._event(
                "preview_completed", job_id, agent_id, field,
            ))

    async def _clear_job(self, job_id: str) -> None:
        async with self._lock:
            for key in tuple(self._previews):
                if key[0] == job_id:
                    self._previews.pop(key, None)

    def _broadcast_unlocked(self, preview: _Preview, event: dict[str, Any]) -> None:
        for queue in tuple(preview.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 预览不是可靠事件日志。慢客户端直接丢弃旧增量，下一条快照
                # 携带完整累计文本，避免模型速度把服务内存拖垮。
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self._event(
                    "preview_snapshot",
                    str(event["job_id"]),
                    preview.agent_id,
                    preview.field,
                    text=preview.text,
                ))

    @staticmethod
    def _event(
        event_type: str,
        job_id: str,
        agent_id: str,
        field: str,
        *,
        delta: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "field": field,
            "transient": True,
        }
        if delta is not None:
            payload["delta"] = delta
        if text is not None:
            payload["text"] = text
        return {
            "sequence": 0,
            "job_id": job_id,
            "event_type": event_type,
            "payload": payload,
        }
