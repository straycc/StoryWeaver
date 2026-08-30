"""Skill 数量、存储和上下文预算策略。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillPolicy:
    max_activated_skills: int = 16
    max_skill_bytes: int = 64 * 1024
    max_snapshot_bytes: int = 512 * 1024
    metadata_catalog_tokens: int = 2_000
    default_materialization_tokens: int = 4_000
    direct_materialization_limit: int = 3
    max_materialized_skills: int = 3

