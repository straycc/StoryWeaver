"""模型响应失败诊断文件。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .context import get_log_context


@dataclass(frozen=True, slots=True)
class ModelFailureDiagnosticWriter:
    """只保存模型输出和必要元数据，不保存请求 Prompt 或 API Key。"""

    directory: Path
    max_content_characters: int = 16_000

    def __post_init__(self) -> None:
        if not isinstance(self.directory, Path):
            raise TypeError("directory 必须是 Path")
        if self.max_content_characters < 1:
            raise ValueError("max_content_characters 必须大于 0")

    def write(
        self,
        *,
        model: str,
        error: Exception,
        raw_response: Mapping[str, Any],
    ) -> Path:
        """使用临时文件和原子替换写入一条诊断记录。"""

        diagnostic_id = str(uuid4())
        content = self._response_content(raw_response)
        truncated = len(content) > self.max_content_characters
        payload = {
            "schema_version": 1,
            "diagnostic_id": diagnostic_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "category": "model_response_error",
            "model": model,
            "error_type": type(error).__name__,
            "error": str(error),
            "context": get_log_context(),
            "response": {
                "content": content[: self.max_content_characters],
                "content_truncated": truncated,
                "finish_reason": self._finish_reason(raw_response),
                "usage": self._safe_usage(raw_response),
            },
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.directory / f"{timestamp}-{diagnostic_id}.json"
        temporary = self.directory / f".{target.name}.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _response_content(raw_response: Mapping[str, Any]) -> str:
        try:
            content = raw_response["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""
        return content if isinstance(content, str) else repr(content)

    @staticmethod
    def _finish_reason(raw_response: Mapping[str, Any]) -> str | None:
        try:
            value = raw_response["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
        return str(value) if value is not None else None

    @staticmethod
    def _safe_usage(raw_response: Mapping[str, Any]) -> dict[str, int]:
        usage = raw_response.get("usage")
        if not isinstance(usage, Mapping):
            return {}
        result: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = value
        return result
