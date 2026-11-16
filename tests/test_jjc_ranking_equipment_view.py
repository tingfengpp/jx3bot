"""E5 service contracts: injected repositories only, no clients or app startup.

These tests exercise observable view/call/concurrency outcomes, not a second
implementation of the selection or aggregation algorithm.
"""
import asyncio
from copy import deepcopy
import unittest

from src.services.jx3.jjc_ranking_equipment import aggregate_equipment_view
from src.services.jx3.jjc_ranking_equipment_view import (
    EquipmentViewError, JjcRankingEquipmentViewService,
)
from tests.jjc_equipment_fixtures import (
    CHECKED_AT, MEMBER_REVISION, SCOPE, TIMESTAMP, equipped_member,
    frozen_member, match_doc,
)


class Clock:
    def __init__(self):
        self.now = float(CHECKED_AT)
        self.mono = 50.0

    def wall(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.now += seconds
        self.mono += seconds


class FakeStats:
    def __init__(self, member=None):
        member = deepcopy(member or equipped_member(1))
        view = aggregate_equipment_view(timestamp=TIMESTAMP, scope=SCOPE,
            members=[member], checked_at=CHECKED_AT-20, completed_at=CHECKED_AT-19,
            member_revision=MEMBER_REVISION)
        self.context = {"timestamp": TIMESTAMP, "scope": deepcopy(SCOPE),
            "member_revision": MEMBER_REVISION, "frozen_source": "local_win_v2",
            "members": [member], "frozen_view": view,
            "ranking_display_kind": "current", "complete": True}
        self.latest_timestamp = TIMESTAMP
        self.calls = []
        self.error = None
        self.after_context = None

    async def load_frozen_equipment_context(self, timestamp):
        self.calls.append(("context", timestamp))
        if self.error:
            raise self.error
        result = deepcopy(self.context) if timestamp == self.context["timestamp"] else None
        if self.after_context:
            self.after_context()
        return result

    async def load_publication_baseline(self, timestamp):
        return await self.load_summary(timestamp)

    async def load_summary(self, timestamp):
        self.calls.append(("summary", timestamp))
        if self.error:
            raise self.error
        if timestamp != self.context["timestamp"]:
            return None
        return {"timestamp": timestamp, "current_season": SCOPE["season_id"],
            "ranking_display_kind": self.context["ranking_display_kind"],
            "kungfu_cache_scope": deepcopy(self.context["scope"]),
            "schema_version": 2, "equipment_complete": self.context["complete"],
            "equipment_source": self.context["frozen_source"],
            "member_revision": self.context["member_revision"],
            "equipment_view_revision": self.context["frozen_view"]["equipment_view_revision"],
            "equipment_view": deepcopy(self.context["frozen_view"])}

    async def get_latest_current_summary(self, scope):
        self.calls.append(("latest", deepcopy(scope)))
        if self.error:
            raise self.error
        return {"timestamp": self.latest_timestamp, "current_season": scope["season_id"],
                "ranking_display_kind": "current", "kungfu_cache_scope": deepcopy(scope)}


class FakeIdentities:
    def __init__(self, members):
        self.calls = []
        self.error = None
        self.rows = [{"_id": m.get("display_identity_id") or m.get("identity_id"),
                      "global_id": m["global_id"], "name": m["name"],
                      "server": m["server"], **deepcopy(m.get("display_locator") or {})}
                     for m in members]

    async def get_equipment_bindings_by_ids(self, ids):
        # Preserve existing fake read/error/counter semantics for the dedicated path.
        return await self.get_display_by_ids(ids)

    async def get_display_by_ids(self, ids):
        self.calls.append(list(ids))
        if self.error:
            raise self.error
        return deepcopy(self.rows)


class FakeInspect:
    def __init__(self):
        self.rows = [match_doc(201, equipment="purple")]
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
        self.error = None
        self.active = 0
        self.peak = 0
        self.cancelled = 0
        self.after_read = None

    async def list_local_win_candidates_batch(self, global_ids, *, season_start,
                                               checked_at, max_time_ms, season_id=None):
        self.calls.append({"global_ids": list(global_ids), "season_start": season_start,
                           "checked_at": checked_at, "max_time_ms": max_time_ms, "season_id": season_id})
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.error:
                raise self.error
            result = deepcopy(self.rows)
            if self.after_read:
                self.after_read()
            return result
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


class FakeSnapshots:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.error = None

    async def load_local_win_equipment_snapshots(self, hashes):
        self.calls.append((list(hashes), "local_win"))
        if self.error:
            raise self.error
        return deepcopy(self.rows)

    async def load_equipment_snapshots(self, hashes, *, strict=False):
        raise AssertionError("Equipment view must use the dedicated local-win snapshot getter")


class EquipmentViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.stats = FakeStats()
        self.identities = FakeIdentities(self.stats.context["members"])
        self.inspect = FakeInspect()
        self.snapshots = FakeSnapshots()
        self.services = []
        self.tasks = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def service(self, **overrides):
        values = dict(stats_repo=self.stats, identity_repo=self.identities,
            inspect_repo=self.inspect, snapshot_repo=self.snapshots,
            scope=deepcopy(SCOPE), enabled=True, overlay_enabled=True,
            clock=self.clock.wall, monotonic=self.clock.monotonic,
            build_timeout=8, request_timeout=10, ttl=15, max_waiters=32)
        values.update(overrides)
        result = JjcRankingEquipmentViewService(**values)
        self.services.append(result)
        return result

    async def read(self, service, **overrides):
        values = dict(timestamp=TIMESTAMP, expected_member_revision=MEMBER_REVISION,
                      expected_frozen_source="local_win_v2")
        values.update(overrides)
        result = await service.read_dynamic_view(**values)
        self.assertIsInstance(result, dict, "A read must produce a typed complete response, not None")
        return result

    def spawn(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    async def wait_started(self):
        try:
            await asyncio.wait_for(self.inspect.started.wait(), 1)
        except asyncio.TimeoutError:
            self.fail("An enabled valid view request never entered the injected local reader")

    async def settle(self):
        for unused in range(12):
            await asyncio.sleep(0)

    def assert_code(self, caught, code):
        self.assertEqual(code, caught.exception.code)

    async def test_tc31_disabled_dynamic_is_zero_repository_access(self):
        for values in ({"enabled": False}, {"overlay_enabled": False},
                       {"enabled": lambda: False}, {"overlay_enabled": lambda: False}):
            with self.subTest(values=tuple(values)):
                service = self.service(**values)
                result = await self.read(service)
                self.assertFalse(result["overlay_enabled"])
                self.assertIsNone(result["view"])
                self.assertEqual([], self.stats.calls)
                self.assertEqual([], self.identities.calls)
                self.assertEqual([], self.inspect.calls)
                self.assertEqual([], self.snapshots.calls)

    async def test_tc31_overlay_off_still_allows_frozen_generation(self):
        service = self.service(overlay_enabled=False)
        member = frozen_member(1)
        before = deepcopy(member)
        view = await service.build_frozen_view(timestamp=TIMESTAMP, members=[member])
        self.assertEqual((1, 0, 1, 0), tuple(view["ranges"]["top_1000"][k] for k in ("N", "L", "P", "U")))
        self.assertEqual(201, view["members"][0]["local_win_packet"]["match_id"])
        self.assertEqual(before, member, "generation must not mutate the frozen caller inputs")
        self.assertTrue(self.inspect.calls)
        self.assertEqual(SCOPE["season_id"], self.inspect.calls[0]["season_id"])
        self.assertEqual(SCOPE["season_start"], self.inspect.calls[0]["season_start"])

    async def test_frozen_build_captures_publication_baseline_before_reading_matches(self):
        service = self.service()
        original_revision = self.stats.context["frozen_view"]["equipment_view_revision"]
        self.inspect.after_read = lambda: self.stats.context["frozen_view"].update(
            equipment_view_revision="synthetic-concurrent-publication")
        view = await service.build_frozen_view(timestamp=TIMESTAMP, members=[frozen_member(1)])
        self.assertIn("_publication_expected", view)
        expected = view["_publication_expected"]
        self.assertIsInstance(expected, dict)
        self.assertEqual(original_revision, expected["expected_revision"])
        self.assertNotEqual("synthetic-concurrent-publication", expected["expected_revision"])

    async def test_tc31_local_off_rejects_new_frozen_generation_without_reads(self):
        service = self.service(enabled=False)
        with self.assertRaises(EquipmentViewError) as caught:
            await service.build_frozen_view(timestamp=TIMESTAMP, members=[frozen_member(1)])
        self.assert_code(caught, "disabled")
        self.assertEqual([], self.identities.calls)
        self.assertEqual([], self.inspect.calls)

    async def test_tc33_expected_manifest_mismatch_is_reload_without_equipment_read(self):
        for options in ({"expected_member_revision": "old-members"},
                        {"expected_frozen_source": "legacy_v1"}):
            with self.subTest(options=options):
                service = self.service()
                result = await self.read(service, **options)
                self.assertTrue(result["reload_required"])
                self.assertFalse(result["retired"])
                self.assertIsNone(result["view"])
                self.assertEqual(MEMBER_REVISION, result["current_member_revision"])
                self.assertEqual("local_win_v2", result["current_frozen_source"])
                self.assertEqual([], self.inspect.calls)

    async def test_tc33_legacy_frozen_source_can_intentionally_produce_local_dynamic_source(self):
        self.stats.context["frozen_source"] = "legacy_v1"
        service = self.service(scope=lambda: deepcopy(SCOPE))
        result = await self.read(service, expected_frozen_source="legacy_v1")
        self.assertFalse(result["reload_required"])
        self.assertEqual("legacy_v1", result["current_frozen_source"])
        self.assertEqual("local_win_v2", result["view"]["source"])

    async def test_tc33_successful_cache_hit_revalidates_latest_without_rebuilding(self):
        service = self.service()
        first = await self.read(service)
        count = len(self.inspect.calls)
        latest_count = sum(call[0] == "latest" for call in self.stats.calls)
        self.clock.advance(1)
        second = await self.read(service)
        self.assertEqual(first["view"]["equipment_view_revision"], second["view"]["equipment_view_revision"])
        self.assertEqual(count, len(self.inspect.calls))
        self.assertGreater(sum(call[0] == "latest" for call in self.stats.calls), latest_count)
        self.assertTrue(second["overlay_enabled"])

    async def test_tc33_ttl_expiry_rebuilds_complete_view(self):
        service = self.service()
        first = await self.read(service)
        self.clock.advance(16)
        self.inspect.rows = [match_doc(202, equipment="legendary")]
        second = await self.read(service)
        metrics = second["view"]["ranges"]["top_1000"]
        self.assertEqual((1, 0, []), (metrics["L"], metrics["P"], metrics["purple_ranks"]))
        self.assertNotEqual(first["view"]["equipment_view_revision"], second["view"]["equipment_view_revision"])
        self.assertEqual(2, len(self.inspect.calls))

    async def test_tc34_retirement_is_checked_on_cache_hit(self):
        service = self.service()
        await self.read(service)
        self.stats.latest_timestamp = TIMESTAMP + 100
        result = await self.read(service)
        self.assertTrue(result["retired"])
        self.assertFalse(result["overlay_enabled"])
        self.assertEqual(TIMESTAMP + 100, result["latest_timestamp"])
        self.assertIsNone(result["view"])
        self.assertEqual(1, len(self.inspect.calls))

    async def test_tc34_new_board_published_during_build_discards_old_dynamic_result(self):
        self.inspect.after_read = lambda: setattr(self.stats, "latest_timestamp", TIMESTAMP+100)
        result = await self.read(self.service())
        self.assertTrue(result["retired"])
        self.assertIsNone(result["view"])

    async def test_tc33_same_timestamp_member_change_requires_reload_even_with_new_server_cache(self):
        service = self.service()
        await self.read(service)
        self.stats.context["member_revision"] = "members-v2"
        self.stats.context["frozen_view"]["member_revision"] = "members-v2"
        result = await self.read(service)
        self.assertTrue(result["reload_required"])
        self.assertFalse(result["retired"])
        self.assertEqual("members-v2", result["current_member_revision"])
        self.assertIsNone(result["view"])
        # A separate instance has no old-cache memory but must give the same answer.
        other = await self.read(self.service())
        self.assertTrue(other["reload_required"])
        self.assertEqual("members-v2", other["current_member_revision"])

    async def test_tc35_qualification_failure_is_not_retirement_or_no_win(self):
        self.stats.error = RuntimeError("synthetic-qualification-failure")
        with self.assertRaises(EquipmentViewError) as caught:
            await self.read(self.service())
        self.assert_code(caught, "read_failed")
        self.assertEqual([], self.inspect.calls)

    async def test_tc35_candidate_failure_does_not_poison_cache_or_create_unknown_success(self):
        service = self.service()
        first = await self.read(service)
        self.clock.advance(16)
        self.inspect.error = RuntimeError("synthetic-candidate-failure")
        with self.assertRaises(EquipmentViewError) as caught:
            await self.read(service)
        self.assert_code(caught, "read_failed")
        self.inspect.error = None
        self.inspect.rows = [match_doc(203, equipment="legendary")]
        next_view = await self.read(service)
        self.assertEqual(1, next_view["view"]["ranges"]["top_1000"]["L"])
        self.assertNotEqual(first["view"]["equipment_view_revision"], next_view["view"]["equipment_view_revision"])

    async def test_tc35_equipment_storage_failure_is_not_true_missing(self):
        row = match_doc(201)
        target = row["data"]["detail"]["team1"]["players_info"][0]
        target.pop("armors")
        target["equipment_snapshot_hash"] = "synthetic-armor-hash"
        self.inspect.rows = [row]
        self.snapshots.error = RuntimeError("synthetic-equipment-storage-failure")
        with self.assertRaises(EquipmentViewError) as caught:
            await self.read(self.service())
        self.assert_code(caught, "read_failed")
        self.assertTrue(self.snapshots.calls)
        self.assertEqual("local_win", self.snapshots.calls[0][1])

    async def test_tc36_successful_no_win_is_unknown_not_read_failed(self):
        self.inspect.rows = []
        result = await self.read(self.service())
        metrics = result["view"]["ranges"]["top_1000"]
        self.assertEqual((1, 0, 0, 1, []), tuple(metrics[k] for k in ("N", "L", "P", "U", "purple_ranks")))
        self.assertEqual("no_local_win", result["view"]["members"][0]["equipment_status"])

    async def test_tc37_invalid_binding_revokes_old_weapon_and_does_not_query_another_role(self):
        self.identities.rows[0]["global_id"] = "799999999999999999"
        result = await self.read(self.service())
        metrics = result["view"]["ranges"]["top_1000"]
        self.assertEqual((0, 0, 1), tuple(metrics[k] for k in ("L", "P", "U")))
        self.assertEqual("binding_invalid", result["view"]["members"][0]["equipment_status"])
        self.assertEqual([], self.inspect.calls)
        self.assertTrue(result.get("revocations"))
        instruction = result["revocations"][0]
        self.assertEqual((TIMESTAMP, MEMBER_REVISION, "binding-1", 1),
            tuple(instruction[k] for k in ("timestamp", "member_revision", "binding_revision", "rank")))

    async def test_tc37_verified_revocation_survives_later_read_failure(self):
        # Two members allow one confirmed invalid binding followed by another read failure.
        second = equipped_member(2)
        self.stats.context["members"].append(second)
        self.stats.context["frozen_view"] = aggregate_equipment_view(
            timestamp=TIMESTAMP, scope=SCOPE, members=self.stats.context["members"],
            checked_at=CHECKED_AT-20, completed_at=CHECKED_AT-19,
            member_revision=MEMBER_REVISION)
        self.identities.rows.append({"_id": second["identity_id"], "global_id": second["global_id"],
            "name": second["name"], "server": second["server"], **second["display_locator"]})
        self.identities.rows[0]["global_id"] = "799999999999999999"
        self.inspect.error = RuntimeError("synthetic-read-after-revocation")
        with self.assertRaises(EquipmentViewError) as caught:
            await self.read(self.service())
        self.assert_code(caught, "read_failed")
        revocations = getattr(caught.exception, "revocations", [])
        self.assertTrue(any(r.get("rank") == 1 and r.get("reason") == "binding_invalid" for r in revocations))
        self.assertFalse(any(r.get("rank") == 2 for r in revocations))

    async def test_tc32_twenty_same_key_readers_share_one_complete_build(self):
        self.inspect.release = asyncio.Event()
        service = self.service()
        readers = [self.spawn(self.read(service)) for unused in range(20)]
        await self.wait_started()
        await self.settle()
        self.assertEqual(1, len(self.inspect.calls))
        self.inspect.release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*readers), 2)
        self.assertEqual(1, self.inspect.peak)
        self.assertEqual(1, len({r["view"]["equipment_view_revision"] for r in outcomes}))

    async def test_tc32_cancelling_one_waiter_does_not_cancel_other_waiter(self):
        self.inspect.release = asyncio.Event()
        service = self.service()
        first = self.spawn(self.read(service))
        second = self.spawn(self.read(service))
        await self.wait_started()
        await self.settle()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(0, self.inspect.cancelled)
        self.inspect.release.set()
        self.assertTrue((await asyncio.wait_for(second, 1))["overlay_enabled"])

    async def test_tc32_last_dynamic_waiter_cancel_does_not_cancel_queued_frozen_owner(self):
        self.inspect.release = asyncio.Event()
        service = self.service()
        dynamic = self.spawn(self.read(service))
        await self.wait_started()
        frozen = self.spawn(service.build_frozen_view(timestamp=TIMESTAMP+1, members=[frozen_member(1)]))
        await self.settle()
        self.assertEqual(1, len(self.inspect.calls), "Frozen and dynamic must share the same build quota")
        dynamic.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await dynamic
        await self.settle()
        self.assertFalse(frozen.cancelled())
        self.inspect.release.set()
        result = await asyncio.wait_for(frozen, 1)
        self.assertEqual(TIMESTAMP+1, result["timestamp"])
        self.assertEqual(1, self.inspect.peak)

    async def test_tc32_waiter_cap_reports_busy_without_unbounded_builds(self):
        self.inspect.release = asyncio.Event()
        service = self.service(max_waiters=2)
        first = self.spawn(self.read(service)); second = self.spawn(self.read(service))
        await self.wait_started()
        await self.settle()
        with self.assertRaises(EquipmentViewError) as caught:
            await self.read(service)
        self.assert_code(caught, "busy")
        self.assertEqual(1, len(self.inspect.calls))
        self.inspect.release.set()
        await asyncio.gather(first, second)

    async def test_tc35_build_budget_cancels_failed_work_and_can_recover(self):
        self.inspect.release = asyncio.Event()
        service = self.service(build_timeout=.03, request_timeout=.1)
        with self.assertRaises(EquipmentViewError) as caught:
            await asyncio.wait_for(self.read(service), .3)
        self.assert_code(caught, "read_failed")
        self.assertEqual(0, self.inspect.active)
        self.assertGreaterEqual(self.inspect.cancelled, 1)
        self.inspect.release = None
        self.assertTrue((await self.read(service))["overlay_enabled"])

    async def test_close_rejects_new_work_and_drains_shared_builds(self):
        self.inspect.release = asyncio.Event()
        service = self.service()
        readers = [self.spawn(self.read(service)) for unused in range(2)]
        await self.wait_started()
        await service.close()
        outcomes = await asyncio.gather(*readers, return_exceptions=True)
        self.assertTrue(all(isinstance(r, (EquipmentViewError, asyncio.CancelledError)) for r in outcomes))
        self.assertEqual(0, self.inspect.active)
        await service.close()
        with self.assertRaises(EquipmentViewError) as caught:
            await self.read(service)
        self.assert_code(caught, "closed")
