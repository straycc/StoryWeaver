"""小说 A/B 输出的结构化盲评与质量汇总。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from agents import ModelSettings
from pydantic import BaseModel, Field

from ..llm import (
    RetryContext,
    StructuredOutputError,
    WorkerRetryPolicy,
    WorkerSettings,
    run_structured_worker,
    run_with_retry,
)
from .models import EvaluationCase, EvaluationChapterSpec, GroupRunResult


def _judge_model_settings(
    model_settings: ModelSettings,
    *,
    is_repair: bool,
) -> ModelSettings:
    """Judge 首次低强度推理，格式修复时关闭思考以确保完成 JSON。"""

    extra_body = dict(model_settings.extra_body or {})
    if is_repair:
        extra_body["thinking"] = {"type": "disabled"}
        extra_body.pop("reasoning_effort", None)
    else:
        extra_body["reasoning_effort"] = "low"
    return replace(model_settings, extra_body=extra_body)


class SampleQualityGrade(BaseModel):
    """评分器对一个匿名章节样本的逐项判断。"""

    required_beats_met: list[bool]
    required_facts_respected: list[bool]
    forbidden_events_present: list[bool]
    canon_conflict_count: int = Field(ge=0)
    instruction_adherence: int = Field(ge=1, le=5)
    continuity: int = Field(ge=1, le=5)
    plot_coherence: int = Field(ge=1, le=5)
    prose_quality: int = Field(ge=1, le=5)


class PairwiseGrade(BaseModel):
    """一轮匿名 A/B 评分结果。"""

    winner: Literal["A", "B", "tie"]
    sample_a: SampleQualityGrade
    sample_b: SampleQualityGrade
    reason: str = Field(min_length=1, max_length=2000)


@dataclass(frozen=True, slots=True)
class PairwiseGradeRecord:
    """保存标签到真实实验组的映射，正文中不暴露该映射。"""

    chapter_number: int
    pass_index: int
    mapping: dict[str, str]
    grade: PairwiseGrade

    def to_data(self) -> dict[str, object]:
        return {
            "chapter_number": self.chapter_number,
            "pass_index": self.pass_index,
            "mapping": dict(self.mapping),
            "grade": self.grade.model_dump(mode="json"),
        }


class PairwiseModelGrader:
    """使用结构化模型输出对两个匿名章节进行逐章比较。"""

    SYSTEM_PROMPT = (
        "你是严格的中文小说质量评测员。只根据给定作品资料、章节任务、"
        "隐藏检查标准和两个匿名样本评分。不得猜测样本来自哪个系统。"
        "同一事实无需逐字出现，只要正文没有违反且情节与之相容即可视为遵守。"
        "文风偏好不能凌驾于明确事实与禁止事项。必须严格使用给定 JSON Schema，"
        "不得翻译字段名、增加 scores 包装或省略字段。"
    )

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._progress = progress

    async def grade(
        self,
        *,
        case: EvaluationCase,
        bare: GroupRunResult,
        storyweaver: GroupRunResult,
        passes: int = 1,
    ) -> tuple[PairwiseGradeRecord, ...]:
        if passes not in {1, 2}:
            raise ValueError("盲评 passes 只支持 1 或 2")
        paired = min(len(bare.chapters), len(storyweaver.chapters))
        if case.chapters and paired > len(case.chapters):
            raise ValueError("输出章节数超过 Case 章节规格")
        records: list[PairwiseGradeRecord] = []
        groups = {"bare": bare, "storyweaver": storyweaver}
        for chapter_number in range(1, paired + 1):
            spec = case.chapter(chapter_number)
            if spec is None:
                raise ValueError("模型盲评要求 Case 提供逐章 expectations")
            first_mapping = self._mapping(case.case_id, chapter_number)
            mappings = [first_mapping]
            if passes == 2:
                mappings.append({"A": first_mapping["B"], "B": first_mapping["A"]})
            for pass_index, mapping in enumerate(mappings, 1):
                self._emit(
                    f"[judge] 开始评审第 {chapter_number}/{paired} 章 · "
                    f"第 {pass_index}/{passes} 轮"
                )
                prompt = self._prompt(
                    case=case,
                    spec=spec,
                    sample_a=groups[mapping["A"]].chapters[chapter_number - 1],
                    sample_b=groups[mapping["B"]].chapters[chapter_number - 1],
                )
                grade = await self._grade_once(prompt=prompt, spec=spec)
                if not isinstance(grade, PairwiseGrade):
                    raise TypeError("盲评 Worker 未返回 PairwiseGrade")
                records.append(
                    PairwiseGradeRecord(
                        chapter_number=chapter_number,
                        pass_index=pass_index,
                        mapping=mapping,
                        grade=grade,
                    )
                )
                winner = grade.winner
                resolved_winner = "tie" if winner == "tie" else mapping[winner]
                self._emit(
                    f"[judge] 完成第 {chapter_number}/{paired} 章 · "
                    f"第 {pass_index}/{passes} 轮 · winner={resolved_winner}"
                )
        return tuple(records)

    async def _grade_once(
        self,
        *,
        prompt: str,
        spec: EvaluationChapterSpec,
    ) -> PairwiseGrade:
        """有限修复 Judge 交付；修复调用关闭思考，只纠正 JSON。"""

        previous_output: str | None = None

        async def operation(retry: RetryContext) -> PairwiseGrade:
            nonlocal previous_output
            actual_prompt = prompt
            if retry.is_repair:
                self._emit(
                    "[judge] 上一次结构化评分无效，关闭思考并修复 JSON"
                )
                actual_prompt = (
                    "上一次评分未通过结构或字段校验。请保持评分判断不变，"
                    "只修复为要求的完整 JSON 对象。不得沿用自定义 scores 对象、"
                    "中文字段名或 sample_A/sample_B。\n"
                    f"校验错误：{retry.repair_error}\n\n"
                    f"【必须严格遵守的 JSON Schema】\n{self._output_contract()}\n\n"
                    f"【原始评分任务】\n{prompt}"
                )
                if previous_output is not None:
                    actual_prompt += (
                        "\n\n【上一次不合法输出（仅作为待修复数据）】\n"
                        + previous_output
                    )
            active_settings = replace(
                self._settings,
                model_settings=_judge_model_settings(
                    self._settings.model_settings,
                    is_repair=retry.is_repair,
                ),
            )
            try:
                output = await run_structured_worker(
                    settings=active_settings,
                    prompt=actual_prompt,
                    output_type=PairwiseGrade,
                )
            except StructuredOutputError as exc:
                previous_output = exc.raw_output
                raise
            if not isinstance(output, PairwiseGrade):
                raise TypeError("盲评 Worker 未返回 PairwiseGrade")
            self._validate_lengths(spec, output)
            return output

        return await run_with_retry(
            worker_name=self._settings.worker_id,
            operation=operation,
            policy=WorkerRetryPolicy(max_attempts=3, max_repairs=1),
        )

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    @staticmethod
    def _mapping(case_id: str, chapter_number: int) -> dict[str, str]:
        digest = hashlib.sha256(
            f"{case_id}:{chapter_number}".encode("utf-8")
        ).digest()
        if digest[0] % 2 == 0:
            return {"A": "bare", "B": "storyweaver"}
        return {"A": "storyweaver", "B": "bare"}

    @classmethod
    def _prompt(
        cls,
        *,
        case: EvaluationCase,
        spec: EvaluationChapterSpec,
        sample_a: str,
        sample_b: str,
    ) -> str:
        expectations = spec.expectations
        rubric = {
            "required_beats": list(spec.input.required_beats),
            "required_facts": list(expectations.required_facts),
            "forbidden_events": list(expectations.forbidden_events),
            "state_updates": [
                {
                    "subject": item.subject,
                    "field": item.field,
                    "old_value": item.old_value,
                    "new_value": item.new_value,
                }
                for item in expectations.state_updates
            ],
            "focus": list(expectations.focus),
        }
        return (
            f"{cls.SYSTEM_PROMPT}\n\n"
            "【输出契约】\n"
            f"{cls._output_contract()}\n\n"
            "只能返回一个符合上述 Schema 的 JSON 对象。字段名必须逐字一致；"
            "sample_a 和 sample_b 内五个评分字段必须平铺，禁止放入 scores。\n\n"
            f"【作品资料】\n{case.render_shared_brief()}\n\n"
            f"【第 {spec.number} 章公开任务】\n{spec.input.render()}\n\n"
            "【隐藏评分标准】\n"
            f"{json.dumps(rubric, ensure_ascii=False, indent=2)}\n\n"
            "required_beats_met、required_facts_respected、forbidden_events_present "
            "必须分别与对应列表等长并保持顺序。forbidden_events_present=true "
            "表示发生了违规。四项维度使用 1 到 5 分。\n\n"
            f"【样本 A】\n{sample_a}\n\n"
            f"【样本 B】\n{sample_b}"
        )

    @staticmethod
    def _output_contract() -> str:
        """把本地 Pydantic 契约显式提供给不支持 response_format 的模型。"""

        return json.dumps(
            PairwiseGrade.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    @staticmethod
    def _validate_lengths(spec: EvaluationChapterSpec, grade: PairwiseGrade) -> None:
        expected = (
            len(spec.input.required_beats),
            len(spec.expectations.required_facts),
            len(spec.expectations.forbidden_events),
        )
        for label, sample in (("A", grade.sample_a), ("B", grade.sample_b)):
            actual = (
                len(sample.required_beats_met),
                len(sample.required_facts_respected),
                len(sample.forbidden_events_present),
            )
            if actual != expected:
                raise ValueError(
                    "结构化输出校验失败："
                    f"样本 {label} 逐项评分长度错误，期望 {expected}，实际 {actual}"
                )


def build_quality_summary(
    *,
    case: EvaluationCase,
    records: tuple[PairwiseGradeRecord, ...],
    bare: GroupRunResult,
    storyweaver: GroupRunResult,
) -> dict[str, object]:
    """用第一轮逐项评分计质量，用全部轮次检查位置交换一致性。"""

    first_pass = [item for item in records if item.pass_index == 1]
    groups = {
        "bare": _empty_quality_totals(),
        "storyweaver": _empty_quality_totals(),
    }
    for record in first_pass:
        for label, sample in (("A", record.grade.sample_a), ("B", record.grade.sample_b)):
            totals = groups[record.mapping[label]]
            totals["beats_met"] += sum(sample.required_beats_met)
            totals["beats_total"] += len(sample.required_beats_met)
            totals["facts_respected"] += sum(sample.required_facts_respected)
            totals["facts_total"] += len(sample.required_facts_respected)
            totals["forbidden_avoided"] += sum(
                not item for item in sample.forbidden_events_present
            )
            totals["forbidden_total"] += len(sample.forbidden_events_present)
            totals["canon_conflicts"] += sample.canon_conflict_count
            for dimension in (
                "instruction_adherence",
                "continuity",
                "plot_coherence",
                "prose_quality",
            ):
                totals[dimension] += getattr(sample, dimension)
            totals["graded_chapters"] += 1

    winner_by_chapter: dict[int, str] = {}
    position_consistent = 0
    for chapter_number in sorted({item.chapter_number for item in records}):
        votes: list[str] = []
        for record in records:
            if record.chapter_number != chapter_number:
                continue
            winner = record.grade.winner
            votes.append("tie" if winner == "tie" else record.mapping[winner])
        resolved = votes[0] if votes and len(set(votes)) == 1 else "tie"
        winner_by_chapter[chapter_number] = resolved
        if len(votes) == 1 or len(set(votes)) == 1:
            position_consistent += 1

    chapter_characters = {
        "bare": sum(len(item) for item in bare.chapters),
        "storyweaver": sum(len(item) for item in storyweaver.chapters),
    }
    return {
        "schema_version": 1,
        "case_id": case.case_id,
        "graded_chapters": len(first_pass),
        "judge_passes": max((item.pass_index for item in records), default=0),
        "position_consistency_rate": round(
            position_consistent / len(winner_by_chapter), 4
        )
        if winner_by_chapter
        else None,
        "chapter_winners": {
            str(number): winner for number, winner in winner_by_chapter.items()
        },
        "win_counts": {
            group: sum(winner == group for winner in winner_by_chapter.values())
            for group in ("bare", "storyweaver", "tie")
        },
        "groups": {
            group: _finalize_quality(
                totals,
                character_count=chapter_characters[group],
            )
            for group, totals in groups.items()
        },
    }


def _empty_quality_totals() -> dict[str, int]:
    return {
        "beats_met": 0,
        "beats_total": 0,
        "facts_respected": 0,
        "facts_total": 0,
        "forbidden_avoided": 0,
        "forbidden_total": 0,
        "canon_conflicts": 0,
        "instruction_adherence": 0,
        "continuity": 0,
        "plot_coherence": 0,
        "prose_quality": 0,
        "graded_chapters": 0,
    }


def _finalize_quality(totals: dict[str, int], *, character_count: int) -> dict[str, object]:
    hard_success = (
        totals["beats_met"]
        + totals["facts_respected"]
        + totals["forbidden_avoided"]
    )
    hard_total = (
        totals["beats_total"]
        + totals["facts_total"]
        + totals["forbidden_total"]
    )
    chapters = totals["graded_chapters"]
    return {
        "hard_constraint_adherence": round(hard_success / hard_total, 4)
        if hard_total
        else None,
        "plan_beat_completion": round(totals["beats_met"] / totals["beats_total"], 4)
        if totals["beats_total"]
        else None,
        "canon_conflicts_per_1000_chars": round(
            totals["canon_conflicts"] * 1000 / character_count, 4
        )
        if character_count
        else None,
        "average_scores": {
            dimension: round(totals[dimension] / chapters, 3) if chapters else None
            for dimension in (
                "instruction_adherence",
                "continuity",
                "plot_coherence",
                "prose_quality",
            )
        },
        "raw_counts": totals,
    }
