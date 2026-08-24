"""兼容标准 SKILL.md 的只读 Skill 发现与按需加载。"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Skill:
    skill_id: str
    name: str
    description: str
    body: str
    source: str
    content_hash: str


class SkillRegistry:
    def __init__(self, skills: tuple[Skill, ...] = ()) -> None:
        self._skills = {item.skill_id: item for item in skills}

    def list(self) -> tuple[Skill, ...]:
        return tuple(sorted(self._skills.values(), key=lambda item: item.skill_id))

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id.strip().lower())

    def resolve_requested(self, instruction: str | None) -> tuple[str | None, tuple[Skill, ...]]:
        """解析 @skill-id；未指定时不隐式加载任何正文。"""
        if not instruction:
            return instruction, ()
        ids = tuple(dict.fromkeys(match.lower() for match in re.findall(r"@([A-Za-z][A-Za-z0-9-]{0,63})", instruction)))
        missing = [skill_id for skill_id in ids if self.get(skill_id) is None]
        if missing:
            raise ValueError(f"未找到 Skill：{', '.join(missing)}")
        cleaned = re.sub(r"@([A-Za-z][A-Za-z0-9-]{0,63})", "", instruction)
        cleaned = re.sub(r"\s+", " ", cleaned).strip() or None
        return cleaned, tuple(self.get(skill_id) for skill_id in ids if self.get(skill_id) is not None)


def applied_skill_markers(instruction: str | None) -> tuple[dict[str, str], ...]:
    """从已冻结的计划指令中提取实际注入过的 Skill 版本标记。"""

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
    roots = [project_root / ".agents" / "skills", project_root / "skills", Path.home() / ".agents" / "skills", Path.home() / ".openclaw" / "skills"]
    roots.extend(Path(item) for item in os.getenv("STORYWEAVER_SKILL_DIRS", "").split(os.pathsep) if item.strip())
    skills: list[Skill] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root] if (root / "SKILL.md").is_file() else [item for item in root.iterdir() if item.is_dir()]
        for directory in candidates:
            manifest = directory / "SKILL.md"
            if not manifest.is_file():
                continue
            try:
                skills.append(_load(manifest, "project" if str(directory).startswith(str(project_root)) else "external"))
            except ValueError:
                continue
    unique = {item.skill_id: item for item in skills}
    return SkillRegistry(tuple(unique.values()))


def _load(path: Path, source: str) -> Skill:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter")
    end = raw.find("\n---", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter 未闭合")
    frontmatter, body = raw[4:end], raw[end + 4:].lstrip("\r\n")
    values = dict(re.findall(r"^(name|description):\s*['\"]?(.*?)['\"]?\s*$", frontmatter, re.MULTILINE))
    name, description = values.get("name", "").strip(), values.get("description", "").strip()
    if not name or not description:
        raise ValueError("SKILL.md 必须含 name 与 description")
    skill_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or path.parent.name.lower()
    if not skill_id[0].isalpha(): skill_id = f"skill-{skill_id}"
    return Skill(skill_id, name, description, body.strip(), source, hashlib.sha256(raw.encode()).hexdigest()[:16])
