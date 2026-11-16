"""Whole dynamic-read singleflight, including both qualification reads."""
import asyncio
from copy import deepcopy
import unittest
from src.services.jx3.jjc_ranking_equipment_view import EquipmentViewError, JjcRankingEquipmentViewService
from tests.test_jjc_ranking_equipment_view import FakeStats, FakeIdentities, FakeInspect, FakeSnapshots, Clock
from tests.jjc_equipment_fixtures import SCOPE, TIMESTAMP, MEMBER_REVISION


class QualificationStats(FakeStats):
    def __init__(self):
        super().__init__()
        self.context_reads = self.latest_reads = 0
        self.active = self.cancelled = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def load_frozen_equipment_context(self, timestamp):
        self.context_reads += 1
        self.active += 1
        self.started.set()
        try:
            await self.release.wait()
            return await super().load_frozen_equipment_context(timestamp)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1

    async def get_latest_current_summary(self, scope):
        self.latest_reads += 1
        return await super().get_latest_current_summary(scope)


class WholeReadSingleflight(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stats = QualificationStats()
        self.inspector = FakeInspect()
        self.identities = FakeIdentities(self.stats.context["members"])
        clock = Clock()
        self.service = JjcRankingEquipmentViewService(stats_repo=self.stats,
            identity_repo=self.identities, inspect_repo=self.inspector,
            snapshot_repo=FakeSnapshots(), scope=deepcopy(SCOPE),
            clock=clock.wall, monotonic=clock.monotonic)
        self.tasks = []

    async def asyncTearDown(self):
        self.stats.release.set()
        if self.inspector.release is not None: self.inspector.release.set()
        for task in self.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.service.close()

    def spawn(self):
        task = asyncio.create_task(self.service.read_dynamic_view(timestamp=TIMESTAMP,
            expected_member_revision=MEMBER_REVISION, expected_frozen_source="local_win_v2"))
        self.tasks.append(task)
        return task

    async def test_twenty_readers_share_entire_prebuild_post_qualification(self):
        tasks = [self.spawn() for _ in range(20)]
        await asyncio.wait_for(self.stats.started.wait(), 1)
        await asyncio.sleep(.03)
        self.stats.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        self.assertEqual(2, self.stats.context_reads, "one qualification before and one after the complete shared read")
        self.assertEqual(2, self.stats.latest_reads)
        self.assertEqual(1, len(self.inspector.calls))
        self.assertTrue(all(result["overlay_enabled"] for result in results))
        self.assertEqual(1, len({result["view"]["equipment_view_revision"] for result in results}))
        self.assertTrue(all(len(result["view"]["members"]) == 1 for result in results))

    async def test_waiter_limit_is_enforced_before_initial_qualification_io(self):
        tasks = [self.spawn() for _ in range(33)]
        await asyncio.wait_for(self.stats.started.wait(), 1)
        await asyncio.sleep(.03)
        self.assertTrue(tasks[-1].done(), "33rd waiter must fail before qualification gate is released")
        with self.assertRaises(EquipmentViewError) as error: await tasks[-1]
        self.assertEqual("busy", error.exception.code)
        self.assertEqual(1, self.stats.context_reads)
        self.assertEqual(0, self.stats.latest_reads)

    async def test_one_waiter_cancellation_keeps_shared_read_alive(self):
        first, second = self.spawn(), self.spawn()
        await asyncio.wait_for(self.stats.started.wait(), 1)
        await asyncio.sleep(.02)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        self.assertEqual(0, self.stats.cancelled)
        self.assertEqual(1, self.stats.context_reads)
        self.assertFalse(second.done())
        self.stats.release.set()
        self.assertTrue((await asyncio.wait_for(second, 1))["overlay_enabled"])
        self.assertEqual(2, self.stats.context_reads)
        self.assertEqual(2, self.stats.latest_reads)

    async def test_last_waiter_cancellation_drains_whole_qualification_flight(self):
        first, second = self.spawn(), self.spawn()
        await asyncio.wait_for(self.stats.started.wait(), 1)
        await asyncio.sleep(.02)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        self.assertEqual(0, self.stats.active)
        self.assertEqual(1, self.stats.cancelled)
        self.assertEqual(1, self.stats.context_reads)
        self.assertEqual(0, self.stats.latest_reads)

    async def test_close_drains_shared_read_in_initial_qualification(self):
        tasks = [self.spawn() for _ in range(20)]
        await asyncio.wait_for(self.stats.started.wait(), 1)
        await asyncio.sleep(.02)
        await asyncio.wait_for(self.service.close(), 1)
        self.assertEqual(0, self.stats.active)
        self.assertEqual(1, self.stats.context_reads)
        self.assertEqual(1, self.stats.cancelled)
        self.assertTrue(all(task.done() for task in tasks))

    async def test_frozen_generation_is_distinct_and_shares_build_slot(self):
        self.inspector.release = asyncio.Event()
        dynamic = self.spawn()
        await asyncio.wait_for(self.stats.started.wait(), 1)
        frozen = asyncio.create_task(self.service.build_frozen_view(timestamp=TIMESTAMP,
            members=deepcopy(self.stats.context["members"])))
        self.tasks.append(frozen)
        await asyncio.wait_for(self.inspector.started.wait(), 1)
        self.stats.release.set()
        await asyncio.sleep(.03)
        self.assertEqual(1, self.inspector.peak)
        self.assertFalse(dynamic.done())
        self.assertFalse(frozen.done())
        self.inspector.release.set()
        dynamic_result, frozen_result = await asyncio.wait_for(asyncio.gather(dynamic, frozen), 2)
        self.assertTrue(dynamic_result["overlay_enabled"])
        self.assertIn("_publication_expected", frozen_result)
        self.assertEqual(2, len(self.inspector.calls), "frozen generation must not merge with the dynamic read")
        self.assertEqual(1, self.inspector.peak)

    async def test_disabled_whole_read_does_zero_repository_io(self):
        self.service.overlay_enabled = False
        results = await asyncio.gather(*(self.spawn() for _ in range(20)))
        self.assertTrue(all(result["reason"] == "disabled" for result in results))
        self.assertEqual(0, self.stats.context_reads)
        self.assertEqual(0, self.stats.latest_reads)
        self.assertEqual([], self.stats.calls)
        self.assertEqual([], self.identities.calls)
        self.assertEqual([], self.inspector.calls)
