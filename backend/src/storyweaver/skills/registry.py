"""只保存 metadata 与来源路由的 Skill 注册表。"""

from __future__ import annotations

import re
from pathlib import Path

from .models import SkillMetadata, SkillPackage
from .source import LocalFilesystemSkillSource, SkillSource


class SkillRegistry:
    def __init__(self, sources: tuple[SkillSource, ...] = ()) -> None:
        self._metadata: dict[str, SkillMetadata] = {}
        self._sources: dict[str, SkillSource] = {}
        for source in sources:
            for item in source.discover():
                if item.skill_id in self._metadata:
                    raise ValueError(f"发现重复 Skill ID：{item.skill_id}")
                self._metadata[item.skill_id] = item
                self._sources[item.skill_id] = source

    def list(self) -> tuple[SkillMetadata, ...]:
        return tuple(sorted(self._metadata.values(), key=lambda item: item.skill_id))

    def get(self, skill_id: str) -> SkillMetadata | None:
        return self._metadata.get(skill_id.strip().lower())

    def load(self, skill_id: str) -> SkillPackage:
        normalized = skill_id.strip().lower()
        source = self._sources.get(normalized)
        if source is None:
            raise KeyError(f"Skill 不存在：{normalized}")
        package = source.load(normalized)
        if package.metadata != self._metadata[normalized]:
            raise ValueError(
                f"Skill {normalized} metadata 在启动后发生变化，请重启 Backend"
            )
        return package


def applied_skill_markers(instruction: str | None) -> tuple[dict[str, str], ...]:
    """兼容读取旧 Proposal 内嵌的 Skill 标记。"""

    if not instruction:
        return ()
    matches = re.findall(
        r"\[已启用 Skill：([A-Za-z][A-Za-z0-9-]{0,63})\s*·\s*([0-9a-f]{8,64})\]",
        instruction,
    )
    return tuple(
        {"skill_id": skill_id, "content_hash": content_hash}
        for skill_id, content_hash in dict.fromkeys(matches)
    )


def load_configured_skills(project_root: Path) -> SkillRegistry:
    """首版只发现项目内置 Skill；新增内容后通过重启重新发现。"""

    return SkillRegistry(
        (LocalFilesystemSkillSource(project_root / "skills" / "builtin"),)
    )
