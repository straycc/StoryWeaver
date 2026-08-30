"""Skill 内容来源抽象及本地只读实现。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import yaml

from .models import SkillMetadata, SkillPackage
from .policy import SkillPolicy


SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class SkillSource(Protocol):
    def discover(self) -> tuple[SkillMetadata, ...]: ...

    def load(self, skill_id: str) -> SkillPackage: ...


class LocalFilesystemSkillSource:
    """只读取固定根目录下的一级 Skill 包。"""

    def __init__(
        self,
        root: Path,
        *,
        source_name: str = "builtin",
        policy: SkillPolicy | None = None,
    ) -> None:
        self.root = root.resolve()
        self.source_name = source_name
        self.policy = policy or SkillPolicy()

    def discover(self) -> tuple[SkillMetadata, ...]:
        if not self.root.is_dir():
            return ()
        metadata: list[SkillMetadata] = []
        for discovered in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not discovered.is_dir():
                continue
            skill_id = discovered.name
            self._validate_skill_id(skill_id)
            directory = discovered.resolve()
            if directory.parent != self.root:
                raise ValueError(f"Skill {skill_id} 路径越界")
            manifest = self._manifest(directory, skill_id=skill_id)
            if not manifest.is_file():
                raise ValueError(f"Skill {skill_id} 缺少 SKILL.md")
            raw = self._read(manifest)
            name, description, display_name, short_description, _ = self._parse(
                raw,
                skill_id=skill_id,
            )
            metadata.append(
                SkillMetadata(
                    skill_id=skill_id,
                    name=name,
                    description=description,
                    source=self.source_name,
                    locator=str(manifest),
                    display_name=display_name,
                    short_description=short_description,
                )
            )
        return tuple(metadata)

    def load(self, skill_id: str) -> SkillPackage:
        normalized = skill_id.strip().lower()
        self._validate_skill_id(normalized)
        directory = (self.root / normalized).resolve()
        if directory.parent != self.root:
            raise ValueError("Skill 路径越界")
        manifest = self._manifest(directory, skill_id=normalized)
        if not manifest.is_file():
            raise KeyError(f"Skill 不存在：{normalized}")
        raw = self._read(manifest)
        name, description, display_name, short_description, canonical = self._parse(
            raw,
            skill_id=normalized,
        )
        return SkillPackage(
            metadata=SkillMetadata(
                skill_id=normalized,
                name=name,
                description=description,
                source=self.source_name,
                locator=str(manifest),
                display_name=display_name,
                short_description=short_description,
            ),
            content=canonical,
        )

    def _read(self, path: Path) -> str:
        size = path.stat().st_size
        if size > self.policy.max_skill_bytes:
            raise ValueError(
                f"{path} 大小 {size} 字节，超过限制 "
                f"{self.policy.max_skill_bytes} 字节"
            )
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path} 必须使用 UTF-8 编码") from exc

    @staticmethod
    def _parse(raw: str, *, skill_id: str) -> tuple[str, str, str, str, str]:
        canonical = (
            raw.removeprefix("\ufeff")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .rstrip()
            + "\n"
        )
        if not canonical.startswith("---\n"):
            raise ValueError(f"Skill {skill_id} 缺少 YAML frontmatter")
        end = canonical.find("\n---\n", 4)
        if end < 0:
            raise ValueError(f"Skill {skill_id} frontmatter 未闭合")
        try:
            value = yaml.safe_load(canonical[4:end])
        except yaml.YAMLError as exc:
            raise ValueError(f"Skill {skill_id} frontmatter 不是合法 YAML") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Skill {skill_id} frontmatter 必须是对象")
        name = value.get("name")
        description = value.get("description")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Skill {skill_id} 缺少 name")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"Skill {skill_id} 缺少 description")
        metadata = value.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError(f"Skill {skill_id} metadata 必须是对象")
        display_name = metadata.get("display_name", name)
        short_description = metadata.get("short_description", description)
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError(f"Skill {skill_id} metadata.display_name 不能为空")
        if not isinstance(short_description, str) or not short_description.strip():
            raise ValueError(
                f"Skill {skill_id} metadata.short_description 不能为空"
            )
        return (
            name.strip(),
            description.strip(),
            display_name.strip(),
            short_description.strip(),
            canonical,
        )

    @staticmethod
    def _validate_skill_id(skill_id: str) -> None:
        if not SKILL_ID_PATTERN.fullmatch(skill_id):
            raise ValueError(f"非法 Skill ID：{skill_id}")

    @staticmethod
    def _manifest(directory: Path, *, skill_id: str) -> Path:
        manifest = (directory / "SKILL.md").resolve()
        if manifest.parent != directory:
            raise ValueError(f"Skill {skill_id} 的 SKILL.md 路径越界")
        return manifest
