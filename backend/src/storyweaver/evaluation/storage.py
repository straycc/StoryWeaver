"""评测产物的本地持久化与盲评导出。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import uuid4

from .models import EvaluationCase, GroupRunResult, QualityChapterMetrics


SCORE_FIELDS = (
    "character_consistency",
    "fact_consistency",
    "hook_continuity",
    "goal_following",
    "readability",
    "repetition_control",
    "continue_reading",
)


class EvaluationStore:
    """把每次 A/B 运行保存为独立且可人工检查的目录。"""

    def __init__(self, output_directory: str | Path) -> None:
        self.output_directory = Path(output_directory)

    def create_run_directory(self, run_id: str) -> Path:
        target = self.output_directory / run_id
        target.mkdir(parents=True, exist_ok=False)
        return target

    def save_case(self, case_directory: Path, case: EvaluationCase) -> None:
        self.write_json(case_directory / "case.json", case)

    def save_prompt(
        self,
        case_directory: Path,
        *,
        group: str,
        chapter_number: int,
        prompt: str,
    ) -> None:
        self.atomic_write_text(
            case_directory / group / "prompts" / f"{chapter_number:04d}.txt",
            prompt.rstrip() + "\n",
        )

    def save_chapter(
        self,
        case_directory: Path,
        *,
        group: str,
        chapter_number: int,
        title: str,
        content: str,
    ) -> None:
        document = f"# {title.strip()}\n\n{content.rstrip()}\n"
        self.atomic_write_text(
            case_directory / group / "chapters" / f"{chapter_number:04d}.md",
            document,
        )

    def save_metrics(
        self,
        case_directory: Path,
        result: GroupRunResult,
    ) -> None:
        self.write_json(
            case_directory / result.group / "metrics.json",
            result.metrics,
        )

    def export_blind_samples(
        self,
        case_directory: Path,
        *,
        case_id: str,
        run_id: str,
        bare: GroupRunResult,
        storyweaver: GroupRunResult,
    ) -> None:
        """导出整组连续章节，保持同一案例内 A/B 标签一致。"""

        digest = hashlib.sha256(f"{run_id}:{case_id}".encode("utf-8")).digest()
        mapping = (
            {"A": "bare", "B": "storyweaver"}
            if digest[0] % 2 == 0
            else {"A": "storyweaver", "B": "bare"}
        )
        groups = {"bare": bare, "storyweaver": storyweaver}
        blind_directory = case_directory / "blind"
        for label, group_name in mapping.items():
            group = groups[group_name]
            sections = [
                f"# 样本 {label}",
                *(
                    f"## 第 {index} 章\n\n{content.rstrip()}"
                    for index, content in enumerate(group.chapters, start=1)
                ),
            ]
            self.atomic_write_text(
                blind_directory / f"sample-{label}.md",
                "\n\n".join(sections).rstrip() + "\n",
            )
        self.write_json(
            blind_directory / "key.json",
            {"case_id": case_id, "mapping": mapping},
        )
        self._write_scorecard(
            blind_directory / "scorecard.csv",
            case_id=case_id,
            mapping=mapping,
            groups=groups,
        )

    def write_manifest(self, run_directory: Path, payload: object) -> None:
        self.write_json(run_directory / "manifest.json", payload)

    @classmethod
    def write_json(cls, path: Path, value: object) -> None:
        cls.atomic_write_text(
            path,
            json.dumps(
                cls._json_value(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    @staticmethod
    def atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @classmethod
    def _json_value(cls, value: object) -> object:
        if is_dataclass(value) and not isinstance(value, type):
            return {
                key: cls._json_value(item)
                for key, item in asdict(value).items()
            }
        if isinstance(value, tuple | list):
            return [cls._json_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if value is None or isinstance(value, str | int | float | bool):
            return value
        raise TypeError(f"不支持写入评测 JSON 的类型：{type(value).__name__}")

    @staticmethod
    def _write_scorecard(
        path: Path,
        *,
        case_id: str,
        mapping: dict[str, str],
        groups: dict[str, GroupRunResult],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as file:
                fieldnames = (
                    "case_id",
                    "sample",
                    "chapter_number",
                    *SCORE_FIELDS,
                    "notes",
                )
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                for label, group_name in mapping.items():
                    for chapter_number in range(1, len(groups[group_name].chapters) + 1):
                        writer.writerow(
                            {
                                "case_id": case_id,
                                "sample": label,
                                "chapter_number": chapter_number,
                            }
                        )
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
