"""OpenAI Agents SDK 的本地 Trace 导出器。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.tracing import TracingProcessor, set_trace_provider
from agents.tracing.provider import DefaultTraceProvider


_LOGGER = logging.getLogger(__name__)
_SENSITIVE_FIELDS = frozenset({"input", "output", "instructions", "messages", "api_key", "authorization"})
_configured_directory: Path | None = None


class LocalJsonlTraceProcessor(TracingProcessor):
    """仅落盘摘要化 Trace，不使用 SDK 默认的远端导出器。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / "agents-sdk-traces.jsonl"
        self._lock = threading.RLock()

    def on_trace_start(self, trace: Any) -> None:
        self._write("trace_started", trace.to_json())
        _LOGGER.info("SDK Trace 开始 trace=%s workflow=%s", trace.trace_id, trace.name)

    def on_trace_end(self, trace: Any) -> None:
        self._write("trace_finished", trace.to_json())
        _LOGGER.info("SDK Trace 完成 trace=%s", trace.trace_id)

    def on_span_start(self, span: Any) -> None:
        self._write("span_started", span.export())

    def on_span_end(self, span: Any) -> None:
        payload = span.export()
        self._write("span_finished", payload)
        data = (payload or {}).get("span_data") or {}
        _LOGGER.info(
            "SDK Span 完成 trace=%s span=%s type=%s",
            span.trace_id,
            span.span_id,
            data.get("type", "unknown"),
        )

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None

    def _write(self, event: str, payload: dict[str, Any] | None) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "payload": self._sanitize(payload or {}),
        }
        try:
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            _LOGGER.exception("写入本地 SDK Trace 失败")

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    cls._summary(item)
                    if str(key).casefold() in _SENSITIVE_FIELDS
                    else cls._sanitize(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize(item) for item in value]
        return value

    @staticmethod
    def _summary(value: Any) -> dict[str, Any]:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return {"redacted": True, "characters": len(text)}


def configure_local_sdk_tracing(directory: Path) -> None:
    """以本地 Processor 替换全局默认导出器，避免 Trace 上传。"""

    global _configured_directory
    resolved = directory.resolve()
    if _configured_directory == resolved:
        return
    # ``set_trace_processors`` 会惰性初始化 SDK 默认远端 exporter。直接先注册
    # 空 Provider，再挂载本地 Processor，确保代理环境下也绝不创建上传客户端。
    provider = DefaultTraceProvider()
    provider.set_processors([LocalJsonlTraceProcessor(resolved)])
    set_trace_provider(provider)
    _configured_directory = resolved
