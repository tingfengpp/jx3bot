"""Full public-request drain contracts; gates hold outside the build owner."""
import asyncio
from copy import deepcopy
import unittest
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from tests.test_jjc_ranking_equipment_view import Clock, FakeStats, FakeIdentities, FakeInspect, FakeSnapshots
from tests.jjc_equipment_fixtures import SCOPE, TIMESTAMP, MEMBER_REVISION


class GatedStats(FakeStats):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.active = 0
        self.counts = {}

    async def gate(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self.stage != (kind, self.counts[kind]): return
        self.active += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
            await self.release_cleanup.wait()
            self.active -= 1

    async def load_publication_baseline(self, timestamp):
        await self.gate("baseline")
        return await super().load_publication_baseline(timestamp)

    async def load_frozen_equipment_context(self, timestamp):
        await self.gate("context")
        return await super().load_frozen_equipment_context(timestamp)

    async def get_latest_current_summary(self, scope):
        await self.gate("latest")
        return await super().get_latest_current_summary(scope)


def service_for(stats):
    clock = Clock()
    return JjcRankingEquipmentViewService(stats_repo=stats,
        identity_repo=FakeIdentities(stats.context["members"]), inspect_repo=FakeInspect(),
        snapshot_repo=FakeSnapshots(), scope=deepcopy(SCOPE),
        clock=clock.wall, monotonic=clock.monotonic)


def operation(service, stats, mode):
    if mode == "build":
        return service.build_frozen_view(timestamp=TIMESTAMP, members=deepcopy(stats.context["members"]))
    if mode == "frozen": return service.read_frozen_snapshot(TIMESTAMP)
    return service.read_dynamic_view(timestamp=TIMESTAMP,
        expected_member_revision=MEMBER_REVISION, expected_frozen_source="local_win_v2")


class FullRequestShutdown(unittest.IsolatedAsyncioTestCase):
    async def gated_close(self, stage, mode="dynamic"):
        stats = GatedStats(stage)
        service = service_for(stats)
        request = asyncio.create_task(operation(service, stats, mode))
        closing = None
        try:
            await asyncio.wait_for(stats.started.wait(), 1)
            # Cancellation and asynchronous cleanup share the original 1s
            # close budget; synchronize on the event rather than a 20ms scheduler guess.
            loop=asyncio.get_running_loop()
            close_deadline=loop.time()+1
            closing = asyncio.create_task(service.close())
            await asyncio.wait_for(stats.cancelled.wait(),max(0,close_deadline-loop.time()))
            self.assertTrue(stats.cancelled.is_set(), "close must cancel the full request outside build owners")
            self.assertFalse(closing.done(), "close must await async request cleanup before runtime closure")
            stats.release_cleanup.set()
            await asyncio.wait_for(closing,max(0,close_deadline-loop.time()))
            self.assertEqual(0, stats.active)
            self.assertTrue(request.done())
        finally:
            stats.release_cleanup.set()
            if not request.done(): request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if closing: await asyncio.gather(closing, return_exceptions=True)
            await service.close()

    async def test_frozen_build_baseline_is_owned_and_drained(self):
        await self.gated_close(("baseline", 1), "build")
    async def test_dynamic_initial_context_is_owned_and_drained(self):
        await self.gated_close(("context", 1))
    async def test_dynamic_initial_latest_is_owned_and_drained(self):
        await self.gated_close(("latest", 1))
    async def test_dynamic_final_context_is_owned_and_drained(self):
        await self.gated_close(("context", 2))
    async def test_dynamic_final_latest_is_owned_and_drained(self):
        await self.gated_close(("latest", 2))
    async def test_frozen_reader_context_is_owned_and_drained(self):
        await self.gated_close(("context", 1), "frozen")


    async def test_concurrent_close_shares_one_cancellation_and_drains_cleanup(self):
        await self.concurrent_close(cancel_first=False)

    async def test_cancelled_close_waiter_does_not_cancel_shared_cleanup(self):
        await self.concurrent_close(cancel_first=True)

    async def concurrent_close(self, *, cancel_first):
        stats = GatedStats(("baseline", 1))
        service = service_for(stats)
        request = asyncio.create_task(operation(service, stats, "build"))
        first = second = None
        try:
            await asyncio.wait_for(stats.started.wait(), 1)
            first = asyncio.create_task(service.close())
            await asyncio.wait_for(stats.cancelled.wait(), 1)
            second = asyncio.create_task(service.close())
            await asyncio.sleep(.02)
            if cancel_first:
                first.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(first.done(), "cancelled close waiter must drain before caller finally can close runtime")
            self.assertFalse(second.done(), "concurrent close must join the first drain without re-cancelling requests")
            self.assertFalse(request.done(), "second cancellation must not interrupt asynchronous finally cleanup")
            self.assertEqual(1, stats.active)
            stats.release_cleanup.set()
            await asyncio.wait_for(second, 1)
            if cancel_first:
                with self.assertRaises(asyncio.CancelledError): await first
            else: await asyncio.wait_for(first, 1)
            self.assertEqual(0, stats.active)
            self.assertTrue(request.done())
        finally:
            stats.release_cleanup.set()
            if not request.done(): request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            for closing in (first, second):
                if closing: await asyncio.gather(closing, return_exceptions=True)
            await service.close()


class InspectCleanupGate(FakeInspect):
    def __init__(self):
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_interrupted = asyncio.Event()
        self.cleanup_completed = False

    async def list_local_win_candidates_batch(self, *args, **kwargs):
        self.active += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleanup_started.set()
            try:
                await self.cleanup_release.wait()
                self.cleanup_completed = True
            except asyncio.CancelledError:
                self.cleanup_interrupted.set()
                raise
            finally:
                self.active -= 1


class RequestTreeShutdown(unittest.IsolatedAsyncioTestCase):
    async def check_inspect_cleanup(self, mode):
        stats = FakeStats()
        inspector = InspectCleanupGate()
        service = service_for(stats)
        service.inspect_repo = inspector
        request = asyncio.create_task(operation(service, stats, mode))
        closing = None
        try:
            await asyncio.wait_for(inspector.started.wait(), 1)
            self.assertTrue(service._owners, "exercise an actual descendant construct task")
            closing = asyncio.create_task(service.close())
            await asyncio.wait_for(inspector.cleanup_started.wait(), 1)
            await asyncio.sleep(.03)
            self.assertFalse(inspector.cleanup_interrupted.is_set(), "close must not cancel ancestor and descendant independently")
            self.assertFalse(closing.done())
            self.assertEqual(1, inspector.active)
            inspector.cleanup_release.set()
            await asyncio.wait_for(closing, 1)
            self.assertTrue(inspector.cleanup_completed)
            self.assertTrue(request.done())
        finally:
            inspector.cleanup_release.set()
            if not request.done(): request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if closing: await asyncio.gather(closing, return_exceptions=True)
            await service.close()

    async def test_frozen_build_descendant_cleanup_is_not_cancelled_twice(self):
        await self.check_inspect_cleanup("build")

    async def test_dynamic_build_descendant_cleanup_is_not_cancelled_twice(self):
        await self.check_inspect_cleanup("dynamic")
