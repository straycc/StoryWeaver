"""评测产物的本地持久化与盲评导出。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import uuid4

from .models import (
    EvaluationCase,
    EvaluationChapterSpec,
    EvaluationRunResult,
    GroupRunResult,
    QualityChapterMetrics,
)


SCORE_FIELDS = (
    "winner",
    "instruction_adherence_a",
    "instruction_adherence_b",
    "continuity_a",
    "continuity_b",
    "plot_coherence_a",
    "plot_coherence_b",
    "prose_quality_a",
    "prose_quality_b",
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
        chapter_specs: tuple[EvaluationChapterSpec, ...] = (),
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
        self.write_json(
            case_directory / "grading" / "rubric.json",
            {
                "case_id": case_id,
                "chapters": chapter_specs,
                "note": "expectations 仅供评分，不得重新注入生成模型。",
            },
        )
        self._write_scorecard(
            blind_directory / "scorecard.csv",
            case_id=case_id,
            mapping=mapping,
            groups=groups,
        )

    def write_manifest(self, run_directory: Path, payload: object) -> None:
        self.write_json(run_directory / "manifest.json", payload)

    def load_run_result(self, run_directory: str | Path) -> EvaluationRunResult:
        """从已完成运行的 Manifest 恢复 Judge 所需的冻结生成结果。"""

        directory = Path(run_directory).resolve()
        manifest = self.read_json(directory / "manifest.json")
        raw_result = manifest.get("result")
        if not isinstance(raw_result, dict):
            raise ValueError("评测 Manifest 不含可恢复的 result")
        bare = self._load_group(raw_result.get("bare"), expected_group="bare")
        storyweaver = self._load_group(
            raw_result.get("storyweaver"), expected_group="storyweaver"
        )
        return EvaluationRunResult(
            run_id=self._required_text(raw_result, "run_id"),
            case_id=self._required_text(raw_result, "case_id"),
            requested_chapters=self._required_non_negative_int(
                raw_result, "requested_chapters", positive=True
            ),
            bare=bare,
            storyweaver=storyweaver,
            output_directory=str(directory),
        )

    def update_grading_status(
        self,
        run_directory: str | Path,
        grading: dict[str, object],
    ) -> None:
        """原子更新评分状态，同时保留生成阶段 Manifest。"""

        directory = Path(run_directory)
        manifest = self.read_json(directory / "manifest.json")
        manifest["grading"] = grading
        self.write_manifest(directory, manifest)

    @staticmethod
    def read_json(path: str | Path) -> dict[str, object]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"无法读取评测文件 {path}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"评测 JSON 无法解析 {path}：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"评测 JSON 根节点必须是对象：{path}")
        return value

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

    @classmethod
    def _load_group(
        cls,
        value: object,
        *,
        expected_group: str,
    ) -> GroupRunResult:
        if not isinstance(value, dict):
            raise ValueError(f"评测结果缺少 {expected_group} 实验组")
        group = cls._required_text(value, "group")
        if group != expected_group:
            raise ValueError(
                f"评测实验组错误：期望 {expected_group}，实际 {group}"
            )
        chapters = value.get("chapters")
        metrics = value.get("metrics")
        if not isinstance(chapters, list) or not all(
            isinstance(item, str) and item.strip() for item in chapters
        ):
            raise ValueError(f"{expected_group}.chapters 必须是非空文本数组")
        if not isinstance(metrics, list):
            raise ValueError(f"{expected_group}.metrics 必须是数组")
        return GroupRunResult(
            group=group,
            chapters=tuple(chapters),
            metrics=tuple(cls._load_metric(item, expected_group) for item in metrics),
            setup_model_calls=cls._optional_non_negative_int(value, "setup_model_calls"),
            setup_failed_model_calls=cls._optional_non_negative_int(
                value, "setup_failed_model_calls"
            ),
            setup_retry_count=cls._optional_non_negative_int(value, "setup_retry_count"),
            setup_input_tokens=cls._optional_non_negative_int(value, "setup_input_tokens"),
            setup_output_tokens=cls._optional_non_negative_int(value, "setup_output_tokens"),
            setup_elapsed_seconds=cls._optional_non_negative_number(
                value, "setup_elapsed_seconds"
            ),
            setup_error=cls._optional_text(value, "setup_error"),
        )

    @classmethod
    def _load_metric(cls, value: object, group: str) -> QualityChapterMetrics:
        if not isinstance(value, dict):
            raise ValueError(f"{group}.metrics 中存在非对象记录")
        succeeded = value.get("succeeded")
        committed = value.get("committed")
        if not isinstance(succeeded, bool):
            raise ValueError(f"{group}.metrics.succeeded 必须是布尔值")
        if committed is not None and not isinstance(committed, bool):
            raise ValueError(f"{group}.metrics.committed 必须是布尔值或 null")
        return QualityChapterMetrics(
            group=cls._required_text(value, "group"),
            chapter_number=cls._required_non_negative_int(
                value, "chapter_number", positive=True
            ),
            succeeded=succeeded,
            committed=committed,
            status=cls._required_text(value, "status"),
            model_calls=cls._required_non_negative_int(value, "model_calls"),
            failed_model_calls=cls._required_non_negative_int(
                value, "failed_model_calls"
            ),
            retry_count=cls._required_non_negative_int(value, "retry_count"),
            input_tokens=cls._required_non_negative_int(value, "input_tokens"),
            output_tokens=cls._required_non_negative_int(value, "output_tokens"),
            elapsed_seconds=cls._required_non_negative_number(
                value, "elapsed_seconds"
            ),
            revised=value.get("revised") if isinstance(value.get("revised"), bool) else None,
            initial_issue_count=cls._optional_int(value, "initial_issue_count"),
            final_issue_count=cls._optional_int(value, "final_issue_count"),
            initial_critical_count=cls._optional_int(value, "initial_critical_count"),
            final_critical_count=cls._optional_int(value, "final_critical_count"),
            candidate_id=cls._optional_text(value, "candidate_id"),
            error=cls._optional_text(value, "error"),
        )

    @staticmethod
    def _required_text(value: dict[str, object], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} 必须是非空字符串")
        return item

    @staticmethod
    def _optional_text(value: dict[str, object], key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} 必须是非空字符串或 null")
        return item

    @classmethod
    def _required_non_negative_int(
        cls,
        value: dict[str, object],
        key: str,
        *,
        positive: bool = False,
    ) -> int:
        item = value.get(key)
        minimum = 1 if positive else 0
        if (
            not isinstance(item, int)
            or isinstance(item, bool)
            or item < minimum
        ):
            label = "正整数" if positive else "非负整数"
            raise ValueError(f"{key} 必须是{label}")
        return item

    @classmethod
    def _optional_non_negative_int(cls, value: dict[str, object], key: str) -> int:
        if key not in value:
            return 0
        return cls._required_non_negative_int(value, key)

    @staticmethod
    def _optional_int(value: dict[str, object], key: str) -> int | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"{key} 必须是整数或 null")
        return item

    @staticmethod
    def _required_non_negative_number(value: dict[str, object], key: str) -> float:
        item = value.get(key)
        if (
            not isinstance(item, int | float)
            or isinstance(item, bool)
            or item < 0
        ):
            raise ValueError(f"{key} 必须是非负数字")
        return float(item)

    @classmethod
    def _optional_non_negative_number(
        cls,
        value: dict[str, object],
        key: str,
    ) -> float:
        if key not in value:
            return 0.0
        return cls._required_non_negative_number(value, key)

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
                    "chapter_number",
                    *SCORE_FIELDS,
                    "notes",
                )
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                paired_chapters = min(
                    len(groups["bare"].chapters),
                    len(groups["storyweaver"].chapters),
                )
                for chapter_number in range(1, paired_chapters + 1):
                    writer.writerow(
                        {
                            "case_id": case_id,
                            "chapter_number": chapter_number,
                        }
                    )
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
