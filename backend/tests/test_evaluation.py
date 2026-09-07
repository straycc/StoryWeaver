from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import ModelSettings

from storyweaver.evaluation.case_loader import load_evaluation_case
from storyweaver.evaluation.cli import _grade_result
from storyweaver.evaluation.graders import (
    PairwiseGrade,
    PairwiseGradeRecord,
    SampleQualityGrade,
    PairwiseModelGrader,
    build_quality_summary,
)
from storyweaver.evaluation.models import (
    EvaluationRunResult,
    GroupRunResult,
    QualityChapterMetrics,
)
from storyweaver.evaluation.runner import BareNovelWriter, EvaluationRunner
from storyweaver.evaluation.storage import EvaluationStore
from storyweaver.llm import StructuredOutputError, WorkerSettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = PROJECT_ROOT / "backend" / "evals" / "cases" / "novel_ab_v1.yaml"
COMPLEX_CASE_PATH = (
    PROJECT_ROOT / "backend" / "evals" / "cases" / "snow-harbor-ledger-12.yaml"
)


class EvaluationCaseLoaderTests(unittest.TestCase):
    def test_loads_twelve_chapter_case(self) -> None:
        case = load_evaluation_case(CASE_PATH)

        self.assertEqual(case.case_id, "yanmen-scroll-12")
        self.assertEqual(case.request.target_chapters, 12)
        self.assertEqual(len(case.chapters), 12)
        self.assertEqual(case.chapter(7).title, "断剑")  # type: ignore[union-attr]
        self.assertEqual(len(case.initial_canon), 5)

    def test_bare_prompt_contains_public_task_but_not_hidden_expectations(self) -> None:
        case = load_evaluation_case(CASE_PATH)

        prompt = BareNovelWriter.build_prompt(
            case=case,
            chapter_number=1,
            previous_chapters=(),
        )

        self.assertIn("孙七刻意遮挡自己的靴子", prompt)
        self.assertIn("全书章节框架", prompt)
        self.assertNotIn("隐藏评分标准", prompt)
        self.assertNotIn("孙七主动交代全部真相", prompt)

    def test_rejects_unknown_continuity_source(self) -> None:
        content = CASE_PATH.read_text(encoding="utf-8").replace(
            "continuity_sources: [canon-soil, canon-letters, canon-sunqi]",
            "continuity_sources: [missing-canon]",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "未知 continuity_sources"):
                load_evaluation_case(path)

    def test_loads_complex_cross_chapter_case(self) -> None:
        case = load_evaluation_case(COMPLEX_CASE_PATH)

        self.assertEqual(case.case_id, "snow-harbor-ledger-12")
        self.assertEqual(len(case.chapters), 12)
        self.assertEqual(len(case.initial_canon), 8)
        self.assertEqual(case.chapter(6).title, "钥匙易手")  # type: ignore[union-attr]
        self.assertEqual(case.chapter(9).title, "白鹭开口")  # type: ignore[union-attr]
        self.assertEqual(case.chapter(12).title, "港灯重燃")  # type: ignore[union-attr]
        state_updates = sum(
            len(chapter.expectations.state_updates) for chapter in case.chapters
        )
        self.assertEqual(state_updates, 12)


class EvaluationStorageTests(unittest.TestCase):
    def test_exports_hidden_rubric_separately_from_blind_samples(self) -> None:
        case = load_evaluation_case(CASE_PATH)
        bare = _group("bare", ("裸模型正文",))
        storyweaver = _group("storyweaver", ("系统正文",))
        with tempfile.TemporaryDirectory() as directory:
            case_directory = Path(directory) / "case"
            store = EvaluationStore(directory)
            store.export_blind_samples(
                case_directory,
                case_id=case.case_id,
                run_id="run-1",
                bare=bare,
                storyweaver=storyweaver,
                chapter_specs=case.chapters[:1],
            )

            sample = (case_directory / "blind" / "sample-A.md").read_text(
                encoding="utf-8"
            )
            rubric = (case_directory / "grading" / "rubric.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("forbidden_events", sample)
            self.assertIn("forbidden_events", rubric)

    def test_restores_existing_run_for_judge_only(self) -> None:
        result = EvaluationRunResult(
            run_id="run-existing",
            case_id="yanmen-scroll-12",
            requested_chapters=1,
            bare=_group("bare", ("裸模型正文",)),
            storyweaver=_group("storyweaver", ("系统正文",)),
            output_directory="ignored",
        )
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run-existing"
            store = EvaluationStore(directory)
            store.write_manifest(run_directory, {"result": result})

            restored = store.load_run_result(run_directory)

            self.assertEqual(restored.run_id, result.run_id)
            self.assertEqual(restored.bare.chapters, ("裸模型正文",))
            self.assertEqual(restored.storyweaver.chapters, ("系统正文",))
            self.assertEqual(restored.output_directory, str(run_directory.resolve()))


class EvaluationQualitySummaryTests(unittest.TestCase):
    def test_summarizes_constraints_and_maps_anonymous_winner(self) -> None:
        case = load_evaluation_case(CASE_PATH)
        bare = _group("bare", ("短正文",))
        storyweaver = _group("storyweaver", ("更完整的系统正文",))
        record = PairwiseGradeRecord(
            chapter_number=1,
            pass_index=1,
            mapping={"A": "bare", "B": "storyweaver"},
            grade=PairwiseGrade(
                winner="B",
                sample_a=_sample_grade(
                    beats=(True, False, True),
                    facts=(True, False),
                    forbidden=(False, True, False),
                    conflicts=1,
                    score=3,
                ),
                sample_b=_sample_grade(
                    beats=(True, True, True),
                    facts=(True, True),
                    forbidden=(False, False, False),
                    conflicts=0,
                    score=5,
                ),
                reason="样本 B 更完整地遵守了章节要求。",
            ),
        )

        summary = build_quality_summary(
            case=case,
            records=(record,),
            bare=bare,
            storyweaver=storyweaver,
        )

        self.assertEqual(summary["win_counts"]["storyweaver"], 1)  # type: ignore[index]
        self.assertEqual(
            summary["groups"]["storyweaver"]["hard_constraint_adherence"],  # type: ignore[index]
            1.0,
        )
        self.assertEqual(
            summary["groups"]["storyweaver"]["canon_conflicts_per_1000_chars"],  # type: ignore[index]
            0.0,
        )


class PairwiseModelGraderTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_exposes_exact_output_schema(self) -> None:
        case = load_evaluation_case(CASE_PATH)
        prompt = PairwiseModelGrader._prompt(
            case=case,
            spec=case.chapters[0],
            sample_a="样本甲",
            sample_b="样本乙",
        )

        self.assertIn('"canon_conflict_count"', prompt)
        self.assertIn('"instruction_adherence"', prompt)
        self.assertIn('"continuity"', prompt)
        self.assertIn('"plot_coherence"', prompt)
        self.assertIn('"prose_quality"', prompt)
        self.assertIn("禁止放入 scores", prompt)

    async def test_invalid_json_is_repaired_with_thinking_disabled(self) -> None:
        case = load_evaluation_case(CASE_PATH)
        grade = PairwiseGrade(
            winner="B",
            sample_a=_sample_grade(
                beats=(True, True, True),
                facts=(True, True),
                forbidden=(False, False, False),
                conflicts=0,
                score=4,
            ),
            sample_b=_sample_grade(
                beats=(True, True, True),
                facts=(True, True),
                forbidden=(False, False, False),
                conflicts=0,
                score=5,
            ),
            reason="样本 B 的执行更加完整。",
        )
        worker = WorkerSettings(
            worker_id="evaluation-pairwise-judge",
            name="Judge",
            instructions="评分",
            model=object(),
            model_settings=ModelSettings(max_tokens=16_000),
            timeout_seconds=30,
        )
        grader = PairwiseModelGrader(worker)
        mocked = AsyncMock(
            side_effect=[
                StructuredOutputError("模型 JSON 无法解析", raw_output=""),
                grade,
            ]
        )

        with patch(
            "storyweaver.evaluation.graders.run_structured_worker",
            new=mocked,
        ):
            records = await grader.grade(
                case=case,
                bare=_group("bare", ("裸模型正文",)),
                storyweaver=_group("storyweaver", ("系统正文",)),
                passes=1,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(mocked.await_count, 2)
        first_settings = mocked.await_args_list[0].kwargs["settings"]
        repair_settings = mocked.await_args_list[1].kwargs["settings"]
        repair_prompt = mocked.await_args_list[1].kwargs["prompt"]
        self.assertEqual(
            first_settings.model_settings.extra_body["reasoning_effort"],
            "low",
        )
        self.assertEqual(
            repair_settings.model_settings.extra_body["thinking"],
            {"type": "disabled"},
        )
        self.assertNotIn(
            "reasoning_effort",
            repair_settings.model_settings.extra_body,
        )
        self.assertIn('"canon_conflict_count"', repair_prompt)
        self.assertIn("不得沿用自定义 scores 对象", repair_prompt)

    async def test_grading_failure_is_persisted_without_losing_generation(self) -> None:
        case = load_evaluation_case(CASE_PATH)
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run-failed-judge"
            result = EvaluationRunResult(
                run_id="run-failed-judge",
                case_id=case.case_id,
                requested_chapters=1,
                bare=_group("bare", ("裸模型正文",)),
                storyweaver=_group("storyweaver", ("系统正文",)),
                output_directory=str(run_directory),
            )
            store = EvaluationStore(directory)
            store.write_manifest(
                run_directory,
                {"status": "completed", "result": result},
            )
            judge = SimpleNamespace(
                grade=AsyncMock(side_effect=StructuredOutputError(
                    "模型 JSON 无法解析",
                    raw_output="",
                ))
            )

            succeeded = await _grade_result(
                judge=judge,  # type: ignore[arg-type]
                case=case,
                result=result,
                passes=1,
                store=store,
            )

            self.assertFalse(succeeded)
            manifest = store.read_json(run_directory / "manifest.json")
            summary = store.read_json(run_directory / "summary.json")
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["grading"]["status"], "failed")  # type: ignore[index]
            self.assertEqual(
                summary["quality"]["status"],  # type: ignore[index]
                "model_grading_failed",
            )


class EvaluationRunnerWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_storyweaver_chapter_is_approved_before_writing(self) -> None:
        service = _ProposalWorkflowService()

        result = await EvaluationRunner._write_approved_storyweaver_chapter(
            service=service,  # type: ignore[arg-type]
            book_id="book-eval",
            user_instruction="生成第一章",
        )

        self.assertEqual(result, "chapter-result")
        self.assertEqual(
            service.calls,
            [
                ("prepare", "book-eval", "生成第一章"),
                ("approve", "book-eval", "proposal-1"),
                ("write", "book-eval", "proposal-1"),
            ],
        )


class _ProposalWorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def prepare_next_chapter(
        self,
        *,
        book_id: str,
        user_instruction: str | None,
    ) -> SimpleNamespace:
        self.calls.append(("prepare", book_id, user_instruction))
        return SimpleNamespace(proposal_id="proposal-1", status="pending")

    def approve_chapter_plan(
        self,
        *,
        book_id: str,
        proposal_id: str,
    ) -> SimpleNamespace:
        self.calls.append(("approve", book_id, proposal_id))
        return SimpleNamespace(proposal_id=proposal_id, status="approved")

    async def write_from_plan(
        self,
        *,
        book_id: str,
        proposal_id: str,
    ) -> str:
        self.calls.append(("write", book_id, proposal_id))
        return "chapter-result"


def _sample_grade(
    *,
    beats: tuple[bool, ...],
    facts: tuple[bool, ...],
    forbidden: tuple[bool, ...],
    conflicts: int,
    score: int,
) -> SampleQualityGrade:
    return SampleQualityGrade(
        required_beats_met=list(beats),
        required_facts_respected=list(facts),
        forbidden_events_present=list(forbidden),
        canon_conflict_count=conflicts,
        instruction_adherence=score,
        continuity=score,
        plot_coherence=score,
        prose_quality=score,
    )


def _group(group: str, chapters: tuple[str, ...]) -> GroupRunResult:
    return GroupRunResult(
        group=group,
        chapters=chapters,
        metrics=tuple(
            QualityChapterMetrics(
                group=group,
                chapter_number=index,
                succeeded=True,
                committed=True if group == "storyweaver" else None,
                status="committed" if group == "storyweaver" else "generated",
                model_calls=1,
                failed_model_calls=0,
                retry_count=0,
                input_tokens=100,
                output_tokens=100,
                elapsed_seconds=1.0,
            )
            for index in range(1, len(chapters) + 1)
        ),
    )


if __name__ == "__main__":
    unittest.main()
