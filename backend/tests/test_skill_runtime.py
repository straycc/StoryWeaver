"""本地 Skill Runtime 生命周期与原子上下文测试。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from novel_fixtures import (
    create_chapter_plan,
    create_foundation,
    create_initial_state,
    create_metadata,
)
from storyweaver.novel_creation import ChapterPlanProposal, NovelProject
from storyweaver.novel_creation.pipeline import WriteNextChapterPipeline
from storyweaver.novel_creation.serialization import (
    decode_chapter_plan_proposal,
    to_data,
)

from storyweaver.skills import (
    LocalFilesystemSkillSource,
    SkillInvocationResolver,
    SkillMaterializer,
    SkillPolicy,
    SkillRegistry,
    SkillResolver,
    SkillService,
    creative_task_from_data,
    creative_task_to_data,
    load_configured_skills,
)


def write_skill(
    root: Path,
    skill_id: str,
    *,
    name: str | None = None,
    description: str = "测试创作方法。",
    body: str = "# 方法\n\n保持人物行动具体。",
    display_name: str | None = None,
    short_description: str | None = None,
) -> Path:
    directory = root / skill_id
    directory.mkdir(parents=True)
    manifest = directory / "SKILL.md"
    metadata = ""
    if display_name is not None or short_description is not None:
        metadata = (
            "metadata:\n"
            f"  display_name: {display_name or name or skill_id}\n"
            f"  short_description: {short_description or description}\n"
        )
    manifest.write_text(
        "---\n"
        f"name: {name or skill_id}\n"
        f"description: {description}\n"
        f"{metadata}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return manifest


class SkillSourceTests(unittest.TestCase):
    def test_project_builtins_are_curated_and_self_contained(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        registry = load_configured_skills(project_root)
        expected = {
            "chapter-planning",
            "fiction-quality-review",
            "natural-fiction-prose-zh",
            "novel-conception",
            "story-continuity-review",
            "wuxia-serial-writing",
        }
        self.assertEqual({item.skill_id for item in registry.list()}, expected)

        forbidden = (
            "references/",
            "scripts/",
            "story.md",
            "story validate",
            "story continuity",
        )
        for skill_id in expected:
            content = registry.load(skill_id).content.lower()
            self.assertFalse(
                any(marker in content for marker in forbidden),
                f"builtin Skill {skill_id} 不能依赖 V1 未加载的外部资源",
            )

    def test_discover_returns_metadata_and_load_reads_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_skill(root, "natural-prose", name="自然表达")
            source = LocalFilesystemSkillSource(root)

            metadata = source.discover()
            self.assertEqual(metadata[0].skill_id, "natural-prose")
            self.assertEqual(metadata[0].display_name, "自然表达")
            self.assertEqual(metadata[0].short_description, "测试创作方法。")
            self.assertFalse(hasattr(metadata[0], "content"))

            package = source.load("natural-prose")
            self.assertIn("保持人物行动具体", package.content)
            registry = SkillRegistry((source,))
            manifest.write_text(
                package.content.replace("description: 测试创作方法。", "description: 已修改描述。"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "metadata 在启动后发生变化"):
                registry.load("natural-prose")

    def test_ui_metadata_is_parsed_without_changing_runtime_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(
                root,
                "natural-prose",
                name="natural-prose",
                description="供 Resolver 使用的完整触发描述。",
                display_name="中文自然表达",
                short_description="供用户快速浏览的简短说明。",
            )

            metadata = LocalFilesystemSkillSource(root).discover()[0]

            self.assertEqual(metadata.display_name, "中文自然表达")
            self.assertEqual(metadata.short_description, "供用户快速浏览的简短说明。")
            self.assertEqual(metadata.description, "供 Resolver 使用的完整触发描述。")

    def test_invalid_id_and_duplicate_source_ids_fail_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            write_skill(Path(first), "same-skill")
            write_skill(Path(second), "same-skill")
            with self.assertRaisesRegex(ValueError, "重复 Skill ID"):
                SkillRegistry(
                    (
                        LocalFilesystemSkillSource(Path(first), source_name="a"),
                        LocalFilesystemSkillSource(Path(second), source_name="b"),
                    )
                )

        with tempfile.TemporaryDirectory() as directory:
            write_skill(Path(directory), "Invalid_ID")
            with self.assertRaisesRegex(ValueError, "非法 Skill ID"):
                LocalFilesystemSkillSource(Path(directory)).discover()

    def test_invalid_frontmatter_encoding_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / "broken-skill"
            broken.mkdir()
            (broken / "SKILL.md").write_text(
                "---\nname: 缺少闭合\ndescription: 测试",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "frontmatter 未闭合"):
                LocalFilesystemSkillSource(root).discover()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / "binary-skill"
            broken.mkdir()
            (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                LocalFilesystemSkillSource(root).discover()

        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(directory)
            target = Path(outside) / "escaped-skill"
            write_skill(target.parent, target.name)
            (root / "escaped-skill").symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "路径越界"):
                LocalFilesystemSkillSource(root).discover()

    def test_references_do_not_change_v1_snapshot_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, "self-contained")
            references = root / "self-contained" / "references"
            references.mkdir()
            reference = references / "extra.md"
            reference.write_text("补充一", encoding="utf-8")
            service = SkillService(SkillRegistry((LocalFilesystemSkillSource(root),)))
            first = service.activate(
                request="写作",
                skill_ids=("self-contained",),
            )
            reference.write_text("补充二", encoding="utf-8")
            second = service.activate(
                request="写作",
                skill_ids=("self-contained",),
            )
            self.assertEqual(
                first.applied_skills[0].content_hash,
                second.applied_skills[0].content_hash,
            )


class SkillActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_skill(self.root, "skill-a", name="能力甲")
        write_skill(self.root, "skill-b", name="能力乙")
        self.registry = SkillRegistry((LocalFilesystemSkillSource(self.root),))
        self.service = SkillService(self.registry)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_activate_freezes_content_and_round_trips(self) -> None:
        task = self.service.activate(
            request="创建当前场景",
            skill_ids=("skill-b", "skill-a"),
        )
        frozen = task.applied_skills[0].content
        write_skill_path = self.root / "skill-b" / "SKILL.md"
        write_skill_path.write_text(frozen.replace("具体", "变化"), encoding="utf-8")

        restored = creative_task_from_data(creative_task_to_data(task))
        self.assertEqual(restored, task)
        self.assertEqual(restored.applied_skills[0].content, frozen)
        self.assertEqual(
            [item.skill_id for item in restored.applied_skills],
            ["skill-b", "skill-a"],
        )

        damaged = creative_task_to_data(task)
        damaged["applied_skills"][0]["content"] += "已损坏"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "内容哈希不匹配"):
            creative_task_from_data(damaged)

    def test_duplicate_request_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "重复 Skill"):
            self.service.activate(
                request="写作",
                skill_ids=("skill-a", "SKILL-A"),
            )

    def test_catalog_budget_is_checked_during_activation(self) -> None:
        service = SkillService(
            self.registry,
            policy=SkillPolicy(metadata_catalog_tokens=1),
        )
        with self.assertRaisesRegex(ValueError, "metadata catalog"):
            service.activate(request="写作", skill_ids=("skill-a",))


class SkillInvocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        write_skill(root, "skill-a", description="用于新书立项和作品简报。")
        write_skill(root, "skill-b", description="用于章节质量审查。")
        self.registry = SkillRegistry((LocalFilesystemSkillSource(root),))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_explicit_selection_wins_without_calling_implicit_selector(self) -> None:
        selector = AsyncMock(return_value=("skill-b",))
        resolver = SkillInvocationResolver(
            registry=self.registry,
            selector=selector,
        )

        selected = await resolver.resolve(
            request="整理作品简报",
            recent_conversation="正在讨论身份线",
            explicit_skill_ids=("skill-a",),
        )

        self.assertEqual(selected, ("skill-a",))
        selector.assert_not_awaited()

    async def test_implicit_selection_uses_current_request_and_recent_conversation(self) -> None:
        selector = AsyncMock(return_value=("skill-a",))
        resolver = SkillInvocationResolver(
            registry=self.registry,
            selector=selector,
        )

        selected = await resolver.resolve(
            request="身份线",
            recent_conversation="用户正在构思新书，并要求稍后整理作品简报。",
        )

        self.assertEqual(selected, ("skill-a",))
        arguments = selector.await_args.args
        self.assertEqual(arguments[0], "身份线")
        self.assertIn("作品简报", arguments[1])

    async def test_invalid_or_failed_implicit_selection_falls_back_to_no_skill(self) -> None:
        selector = AsyncMock(return_value=("unknown",))
        resolver = SkillInvocationResolver(
            registry=self.registry,
            selector=selector,
        )

        selected = await resolver.resolve(
            request="普通聊天",
            recent_conversation="无",
        )

        self.assertEqual(selected, ())

    async def test_transient_implicit_failure_is_not_cached(self) -> None:
        selector = AsyncMock(
            side_effect=[RuntimeError("临时超时"), ("skill-a",)]
        )
        resolver = SkillInvocationResolver(
            registry=self.registry,
            selector=selector,
        )

        first = await resolver.resolve(
            request="继续整理简报",
            recent_conversation="正在讨论身份线",
        )
        second = await resolver.resolve(
            request="继续整理简报",
            recent_conversation="正在讨论身份线",
        )

        self.assertEqual(first, ())
        self.assertEqual(second, ("skill-a",))
        self.assertEqual(selector.await_count, 2)


class SkillResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_set_materializes_every_complete_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, "skill-a")
            write_skill(root, "skill-b")
            task = SkillService(
                SkillRegistry((LocalFilesystemSkillSource(root),))
            ).activate(request="写场景", skill_ids=("skill-a", "skill-b"))
            materialized = await SkillMaterializer(SkillResolver()).materialize(
                task,
                objective="创作正文",
                token_budget=4_000,
            )
            self.assertEqual(
                materialized.resolution.materialized_ids,
                ("skill-a", "skill-b"),
            )
            self.assertIn("skill-a", materialized.catalog)
            self.assertEqual(len(materialized.contents), 2)

    async def test_large_set_uses_selector_and_keeps_whole_skill_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                write_skill(root, f"skill-{index}", body="# 方法\n\n" + "内容" * 40)
            task = SkillService(
                SkillRegistry((LocalFilesystemSkillSource(root),))
            ).activate(
                request="写自然对白",
                skill_ids=tuple(f"skill-{index}" for index in range(4)),
            )

            async def select(*_args: object) -> tuple[str, ...]:
                return ("skill-3", "skill-1")

            resolver = SkillResolver(selector=select)
            result = await resolver.resolve(
                task,
                objective="写对白",
                token_budget=4_000,
            )
            self.assertEqual(result.materialized_ids, ("skill-3", "skill-1"))
            self.assertEqual(result.strategy, "model")

            too_small = await resolver.resolve(
                task,
                objective="预算极小的调用",
                token_budget=1,
            )
            self.assertEqual(too_small.materialized_ids, ())
            self.assertEqual(too_small.estimated_tokens, 0)
            self.assertEqual(
                set(too_small.metadata_only_ids),
                {"skill-0", "skill-1", "skill-2", "skill-3"},
            )

    async def test_invalid_model_selection_falls_back_in_user_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                write_skill(root, f"skill-{index}")
            task = SkillService(
                SkillRegistry((LocalFilesystemSkillSource(root),))
            ).activate(
                request="写作",
                skill_ids=tuple(f"skill-{index}" for index in range(4)),
            )

            async def invalid(*_args: object) -> tuple[str, ...]:
                return ("unknown",)

            result = await SkillResolver(selector=invalid).resolve(
                task,
                objective="写作",
                token_budget=4_000,
            )
            self.assertTrue(result.fallback)
            self.assertEqual(result.materialized_ids[0], "skill-0")

    async def test_resolution_is_cached_by_task_objective_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                write_skill(root, f"skill-{index}")
            task = SkillService(
                SkillRegistry((LocalFilesystemSkillSource(root),))
            ).activate(
                request="写作",
                skill_ids=tuple(f"skill-{index}" for index in range(4)),
            )
            calls = 0

            async def select(*_args: object) -> tuple[str, ...]:
                nonlocal calls
                calls += 1
                return ("skill-2",)

            resolver = SkillResolver(selector=select)
            first = await resolver.resolve(task, objective="写正文", token_budget=4000)
            second = await resolver.resolve(task, objective="写正文", token_budget=4000)
            self.assertIs(first, second)
            self.assertEqual(calls, 1)


class SkillPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        write_skill(root, "skill-a")
        self.task = SkillService(
            SkillRegistry((LocalFilesystemSkillSource(root),))
        ).activate(request="写下一章", skill_ids=("skill-a",))
        self.project = NovelProject(
            metadata=create_metadata(),
            foundation=create_foundation(),
            state=create_initial_state(),
        )
        self.proposal = ChapterPlanProposal(
            proposal_id="proposal-skill",
            book_id=self.project.metadata.book_id,
            chapter_number=1,
            base_chapter_number=0,
            base_current_time=self.project.state.current_time,
            version=1,
            status="pending",
            plan=create_chapter_plan(),
            user_instruction="写下一章",
            feedback_history=(),
            selected_memory_ids=(),
            selected_memory_descriptions=(),
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:00:00+00:00",
            creative_task_context=self.task,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_proposal_snapshot_round_trip_and_old_json_compatibility(self) -> None:
        data = to_data(self.proposal)
        restored = decode_chapter_plan_proposal(data)
        self.assertEqual(restored.creative_task_context, self.task)

        assert isinstance(data, dict)
        data.pop("creative_task_context")
        legacy = decode_chapter_plan_proposal(data)
        self.assertIsNone(legacy.creative_task_context)

    async def test_proposal_revision_uses_its_own_frozen_snapshot(self) -> None:
        class Store:
            def __init__(self, project: NovelProject) -> None:
                self.project = project

            def load_project(self, _book_id: str) -> NovelProject:
                return self.project

        planner = AsyncMock()
        planner.plan.return_value = replace(
            create_chapter_plan(),
            goal="修订后的章节目标",
        )
        pipeline = WriteNextChapterPipeline(
            store=Store(self.project),  # type: ignore[arg-type]
            planner=planner,
            context_builder=object(),  # type: ignore[arg-type]
            writing=object(),  # type: ignore[arg-type]
            reviewer=object(),  # type: ignore[arg-type]
            analyzer=object(),  # type: ignore[arg-type]
        )

        revised = await pipeline.revise(self.proposal, feedback="加强压迫感")

        self.assertIs(revised.creative_task_context, self.task)
        self.assertIs(planner.plan.await_args.kwargs["creative_task"], self.task)


if __name__ == "__main__":
    unittest.main()
