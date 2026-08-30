"""临时文本预览不写入数据库的单进程行为测试。"""

from __future__ import annotations

import asyncio
import unittest

from storyweaver.api.live_preview import LivePreviewHub


class LivePreviewHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_before_preview_receives_later_stream(self) -> None:
        """浏览器先建立 SSE 时，也必须收到随后创建的 Preview。"""

        hub = LivePreviewHub()
        async with hub.subscribe("job-early") as (queue, snapshots):
            self.assertEqual(snapshots, ())

            await hub.start("job-early", agent_id="creative-discussion", field="reply")
            await hub.append(
                "job-early",
                agent_id="creative-discussion",
                field="reply",
                delta="逐字生成",
            )

            started = await asyncio.wait_for(queue.get(), timeout=0.2)
            delta = await asyncio.wait_for(queue.get(), timeout=0.2)
            self.assertEqual(started["event_type"], "preview_started")
            self.assertEqual(delta["event_type"], "preview_delta")
            self.assertEqual(delta["payload"]["delta"], "逐字生成")

    async def test_reconnect_receives_accumulated_snapshot_then_new_delta(self) -> None:
        hub = LivePreviewHub()
        await hub.start("job-1", agent_id="novel-writer", field="content")
        await hub.append("job-1", agent_id="novel-writer", field="content", delta="开头")

        async with hub.subscribe("job-1") as (queue, snapshots):
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["event_type"], "preview_snapshot")
            self.assertEqual(snapshots[0]["payload"]["text"], "开头")

            await hub.append("job-1", agent_id="novel-writer", field="content", delta="续写")
            event = await asyncio.wait_for(queue.get(), timeout=0.2)
            self.assertEqual(event["event_type"], "preview_delta")
            self.assertEqual(event["payload"]["delta"], "续写")

    async def test_new_stream_resets_previous_stage_text(self) -> None:
        hub = LivePreviewHub()
        async with hub.subscribe("job-stages") as (queue, _snapshots):
            await hub.start("job-stages", agent_id="main-agent", field="reply")
            await hub.append(
                "job-stages", agent_id="main-agent", field="reply", delta="路由临时回复"
            )
            await hub.start(
                "job-stages", agent_id="creative-discussion", field="reply"
            )
            await hub.append(
                "job-stages",
                agent_id="creative-discussion",
                field="reply",
                delta="正式回复",
            )

            events = [
                await asyncio.wait_for(queue.get(), timeout=0.2)
                for _ in range(5)
            ]
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "preview_started",
                    "preview_delta",
                    "preview_reset",
                    "preview_started",
                    "preview_delta",
                ],
            )

        async with hub.subscribe("job-stages") as (_queue, snapshots):
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["payload"]["text"], "正式回复")

    async def test_clear_discards_preview_for_later_reconnect(self) -> None:
        hub = LivePreviewHub()
        await hub.append("job-2", agent_id="main-agent", field="reply", delta="临时回复")
        await hub.clear_job("job-2")

        async with hub.subscribe("job-2") as (_queue, snapshots):
            self.assertEqual(snapshots, ())


if __name__ == "__main__":
    unittest.main()
