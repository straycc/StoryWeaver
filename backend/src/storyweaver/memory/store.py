"""记忆存储协议和首版内存实现。"""

from __future__ import annotations

import re
import json
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .record import MemoryRecord, MemoryType


class MemoryStore(Protocol):
    """记忆持久层需要提供的最小接口。"""

    def add(self, memory: MemoryRecord) -> bool:
        """保存记忆；重复记忆返回 False。"""

    def list_for_owner(self, session_id: str, owner_id: str) -> tuple[MemoryRecord, ...]:
        """按产生顺序返回角色在当前会话中的全部记忆。"""


class InMemoryMemoryStore:
    """进程内记忆存储，适合原型与单元测试。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[MemoryRecord]] = defaultdict(list)
        self._fingerprints: set[tuple[str, str, str, str]] = set()

    def add(self, memory: MemoryRecord) -> bool:
        fingerprint = (
            memory.session_id,
            memory.owner_id,
            memory.memory_type.value,
            self._normalize(memory.content),
        )
        if fingerprint in self._fingerprints:
            return False
        self._fingerprints.add(fingerprint)
        self._records[(memory.session_id, memory.owner_id)].append(memory)
        return True

    def list_for_owner(self, session_id: str, owner_id: str) -> tuple[MemoryRecord, ...]:
        return tuple(self._records.get((session_id, owner_id), ()))

    @staticmethod
    def _normalize(content: str) -> str:
        """忽略空白和常见标点，避免同一事实被反复写入。"""

        return re.sub(r"[^\w\u4e00-\u9fff]+", "", content).casefold()


class JsonCharacterMemoryStore:
    """按模拟会话和角色隔离的一条记录一个 JSON 的持久化 Store。"""

    _SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

    def __init__(self, simulations_directory: str | Path) -> None:
        self.simulations_directory = Path(simulations_directory)
        self._lock = threading.RLock()

    def add(self, memory: MemoryRecord) -> bool:
        with self._lock:
            records = self.list_for_owner(memory.session_id, memory.owner_id)
            fingerprint = (
                memory.memory_type.value,
                InMemoryMemoryStore._normalize(memory.content),
            )
            if any(
                (
                    item.memory_type.value,
                    InMemoryMemoryStore._normalize(item.content),
                )
                == fingerprint
                for item in records
            ):
                return False
            directory = self._owner_directory(memory.session_id, memory.owner_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{memory.memory_id}.json"
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            data = {
                "memory_id": memory.memory_id,
                "session_id": memory.session_id,
                "owner_id": memory.owner_id,
                "content": memory.content,
                "memory_type": memory.memory_type.value,
                "importance": memory.importance,
                "created_sequence": memory.created_sequence,
                "source_event_id": memory.source_event_id,
            }
            try:
                temporary.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            except OSError:
                temporary.unlink(missing_ok=True)
                raise
            return True

    def list_for_owner(
        self,
        session_id: str,
        owner_id: str,
    ) -> tuple[MemoryRecord, ...]:
        directory = self._owner_directory(session_id, owner_id)
        if not directory.exists():
            return ()
        records: list[MemoryRecord] = []
        with self._lock:
            for path in directory.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    records.append(
                        MemoryRecord(
                            memory_id=str(data["memory_id"]),
                            session_id=str(data["session_id"]),
                            owner_id=str(data["owner_id"]),
                            content=str(data["content"]),
                            memory_type=MemoryType(str(data["memory_type"])),
                            importance=int(data["importance"]),
                            created_sequence=int(data["created_sequence"]),
                            source_event_id=(
                                str(data["source_event_id"])
                                if data.get("source_event_id") is not None
                                else None
                            ),
                        )
                    )
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"无法读取角色记忆 {path}：{exc}") from exc
        return tuple(
            sorted(records, key=lambda item: (item.created_sequence, item.memory_id))
        )

    def _owner_directory(self, session_id: str, owner_id: str) -> Path:
        self._validate_id(session_id, "session_id")
        self._validate_id(owner_id, "owner_id")
        return (
            self.simulations_directory
            / session_id
            / "character_memories"
            / owner_id
        )

    @classmethod
    def _validate_id(cls, value: str, field_name: str) -> None:
        if not isinstance(value, str) or not cls._SAFE_ID.fullmatch(value):
            raise ValueError(f"{field_name} 格式不合法")
