"""Real isolated Mongo metadata completeness contracts; no default database."""
import asyncio
from copy import deepcopy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from uuid import uuid4
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ExecutionTimeout
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo, EquipmentSnapshotError
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_publish import candidate
from tests.jjc_equipment_fixtures import SCOPE, TIMESTAMP

@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated Mongo required")
class MetadataCompleteness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]))
        self.db = self.client["jjc_equipment_metadata_test_" + uuid4().hex]
        self.repo = JjcRankingStatsRepo(db=self.db, suppress_errors=False)
        valid = candidate(timestamp=TIMESTAMP)
        legacy = {"timestamp": TIMESTAMP-1, "current_season": SCOPE["season_id"]}
        broken = []
        for i, mode in enumerate(("flag", "revision", "members", "missing", "source"), 1):
            doc = candidate(timestamp=TIMESTAMP+i)
            if mode == "flag": doc["equipment_complete"] = False
            if mode == "revision": doc["equipment_view_revision"] = "damaged"
            if mode == "members": doc["equipment_view"]["members"].pop()
            if mode == "missing": doc.pop("equipment_view")
            if mode == "source": doc["equipment_source"] = "legacy_v1"
            broken.append(doc)
        await self.db.jjc_ranking_stat_summaries.insert_many([legacy, valid] + broken)

    async def asyncTearDown(self):
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def test_latest_skips_all_damaged_candidates(self):
        row = await self.repo.get_latest_current_summary(SCOPE)
        self.assertEqual(TIMESTAMP, row["timestamp"])
        self.assertEqual(2, row["schema_version"])
        self.assertEqual("local_win_v2", row["equipment_source"])

    async def test_latest_keeps_legacy_and_excludes_historical(self):
        await self.db.jjc_ranking_stat_summaries.delete_one({"timestamp": TIMESTAMP})
        historical = candidate(timestamp=TIMESTAMP+10)
        historical["ranking_display_kind"] = "historical"
        await self.db.jjc_ranking_stat_summaries.insert_one(historical)
        self.assertEqual(TIMESTAMP-1, (await self.repo.get_latest_current_summary(SCOPE))["timestamp"])

    async def test_unpaged_and_paginated_counts_only_readable_snapshots(self):
        self.assertEqual([TIMESTAMP, TIMESTAMP-1], await self.repo.list_timestamps())
        for meta in (False, True):
            first = await self.repo.list_timestamps(page=1, page_size=1, with_meta=meta)
            second = await self.repo.list_timestamps(page=2, page_size=1, with_meta=meta)
            self.assertEqual(2, first["total"])
            self.assertTrue(first["has_more"])
            self.assertFalse(second["has_more"])
            self.assertEqual(TIMESTAMP, first["items"][0]["timestamp"] if meta else first["items"][0])
            if meta:
                self.assertEqual(2, first["items"][0]["schema_version"])
                self.assertEqual("local_win_v2", first["items"][0]["equipment_source"])
                self.assertTrue(first["items"][0]["equipment_view_revision"])

    async def test_seasons_and_history_exclude_incomplete_new_seasons(self):
        invalid = candidate(timestamp=TIMESTAMP+20)
        invalid["current_season"] = "invalid-season"
        await self.db.jjc_ranking_stat_summaries.insert_one(invalid)
        seasons = await self.repo.list_seasons()
        self.assertEqual([{ "name": str(SCOPE["season_id"]), "latest_timestamp": TIMESTAMP}], seasons["seasons"])
        history = await self.repo.list_season_history(str(SCOPE["season_id"]))
        self.assertEqual([TIMESTAMP, TIMESTAMP-1], [row["timestamp"] for row in history["items"]])
        self.assertEqual(2, history["items"][0]["schema_version"])
        self.assertEqual([], (await self.repo.list_season_history("invalid-season"))["items"])


class BudgetCursor:
    def __init__(self, *, blocked=False, rows=None):
        self.blocked = blocked
        self.rows = rows or []
        self.closed = False
        self.max_time = None
    def max_time_ms(self, milliseconds):
        self.max_time = milliseconds
        return self
    def batch_size(self, count): return self
    def __aiter__(self): return self
    async def __anext__(self):
        if self.blocked: await asyncio.Event().wait()
        if not self.rows: raise StopAsyncIteration
        return self.rows.pop(0)
    async def close(self): self.closed = True


class MetadataScanBudgets(unittest.IsolatedAsyncioTestCase):
    def repo(self, cursor):
        self.query = None
        def find(query):
            self.query = query
            return cursor
        return JjcRankingStatsRepo(db=SimpleNamespace(jjc_ranking_stat_summaries=SimpleNamespace(find=find)))

    async def test_budget_failure_is_explicit_even_when_errors_suppressed(self):
        cursor = BudgetCursor(blocked=True)
        repo = self.repo(cursor)
        with patch("src.storage.mongo_repos.jjc_ranking_stats_repo.METADATA_READ_TIMEOUT", .02, create=True):
            with self.assertRaises(EquipmentSnapshotError) as error:
                await repo.list_seasons()
        self.assertEqual("read_failed", error.exception.code)
        self.assertTrue(cursor.closed)
        self.assertLessEqual(cursor.max_time, 1500)

    async def test_cancelled_scan_closes_cursor_and_propagates_cancellation(self):
        cursor = BudgetCursor(blocked=True)
        repo = self.repo(cursor)
        task = asyncio.create_task(repo.list_timestamps())
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cursor.closed)

    async def test_scan_has_hard_volume_budget_and_closes(self):
        cursor = BudgetCursor(rows=[{"schema_version": 2}] * 513)
        repo = self.repo(cursor)
        with self.assertRaises(EquipmentSnapshotError) as error:
            await repo.list_timestamps()
        self.assertEqual("read_failed", error.exception.code)
        self.assertTrue(cursor.closed)

    async def test_history_validation_scan_is_scoped_to_requested_season(self):
        cursor = BudgetCursor(blocked=True)
        repo = self.repo(cursor)
        with patch("src.storage.mongo_repos.jjc_ranking_stats_repo.METADATA_READ_TIMEOUT", .02, create=True):
            with self.assertRaises(EquipmentSnapshotError): await repo.list_season_history("17")
        self.assertEqual({"schema_version": 2, "current_season": {"$in": ["17", 17]}}, self.query)
        self.assertTrue(cursor.closed)

    async def test_count_server_budget_failure_is_not_an_empty_success(self):
        collection = SimpleNamespace(count_documents=AsyncMock(side_effect=ExecutionTimeout("budget")))
        repo = JjcRankingStatsRepo(db=SimpleNamespace(jjc_ranking_stat_summaries=collection))
        with patch.object(JjcRankingStatsRepo, "_readable_snapshot_query", AsyncMock(return_value={})):
            with self.assertRaises(EquipmentSnapshotError) as error:
                await repo.list_timestamps(page=1)
        self.assertEqual("read_failed", error.exception.code)
        self.assertEqual(1500, collection.count_documents.call_args.kwargs["maxTimeMS"])


class BlockedCloseCursor(BudgetCursor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.close_release = asyncio.Event()

    async def close(self):
        self.close_started.set()
        try:
            await self.close_release.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        finally:
            self.closed = True


class MetadataCleanupBudgets(unittest.IsolatedAsyncioTestCase):
    repo = MetadataScanBudgets.repo

    async def test_blocked_close_has_short_budget_and_explicit_failure(self):
        cursor = BlockedCloseCursor()
        repo = self.repo(cursor)
        task = None
        try:
            with patch("src.storage.mongo_repos.jjc_ranking_stats_repo.METADATA_CLEANUP_TIMEOUT", .02, create=True):
                task = asyncio.create_task(repo.list_timestamps())
                await asyncio.wait_for(cursor.close_started.wait(), 1)
                await asyncio.sleep(.08)
                self.assertTrue(task.done(), "cursor.close must not outlive its short cleanup budget")
                with self.assertRaises(EquipmentSnapshotError) as error: await task
                self.assertEqual("read_failed", error.exception.code)
                self.assertTrue(cursor.close_cancelled.is_set())
        finally:
            cursor.close_release.set()
            if task and not task.done(): task.cancel()
            if task: await asyncio.gather(task, return_exceptions=True)

    async def test_scan_timeout_does_not_wait_forever_for_blocked_close(self):
        cursor = BlockedCloseCursor(blocked=True)
        repo = self.repo(cursor)
        task = None
        try:
            with patch("src.storage.mongo_repos.jjc_ranking_stats_repo.METADATA_READ_TIMEOUT", .02), patch("src.storage.mongo_repos.jjc_ranking_stats_repo.METADATA_CLEANUP_TIMEOUT", .02, create=True):
                task = asyncio.create_task(repo.list_timestamps())
                await asyncio.wait_for(cursor.close_started.wait(), 1)
                await asyncio.sleep(.08)
                self.assertTrue(task.done(), "overall deadline must also bound cancellation cleanup")
                with self.assertRaises(EquipmentSnapshotError) as error: await task
                self.assertEqual("read_failed", error.exception.code)
                self.assertTrue(cursor.close_cancelled.is_set())
        finally:
            cursor.close_release.set()
            if task and not task.done(): task.cancel()
            if task: await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_during_close_propagates_and_cancels_close_waiter(self):
        cursor = BlockedCloseCursor()
        repo = self.repo(cursor)
        task = asyncio.create_task(repo.list_timestamps())
        try:
            await asyncio.wait_for(cursor.close_started.wait(), 1)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertTrue(task.done())
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(cursor.close_cancelled.is_set())
        finally:
            cursor.close_release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class LatestCursor(BudgetCursor):
    def sort(self, *args): return self
    def batch_size(self, count):
        self.batch = count
        return self


class LatestCursorLifecycle(unittest.IsolatedAsyncioTestCase):
    def repo(self,cursor):
        async def find_one(query,**kwargs):
            self.query=query;self.budgets.append(kwargs['max_time_ms'])
            try:return await cursor.__anext__()
            except StopAsyncIteration:return None
        self.budgets=[]
        return JjcRankingStatsRepo(db=SimpleNamespace(jjc_ranking_stat_summaries=SimpleNamespace(find_one=find_one)))

    async def test_early_valid_return_uses_single_document_without_prefetch_or_cleanup(self):
        cursor=LatestCursor(rows=[{'timestamp':TIMESTAMP},{'timestamp':TIMESTAMP-1}])
        self.assertEqual(TIMESTAMP,(await self.repo(cursor).get_latest_current_summary(SCOPE))['timestamp'])
        self.assertFalse(cursor.closed);self.assertEqual(1,len(cursor.rows))
        self.assertEqual([1500],self.budgets)

    async def test_damaged_candidates_are_not_arbitrarily_truncated(self):
        cursor=LatestCursor(rows=[{'_id':'bad-'+str(index),'schema_version':2} for index in range(520)]+[{'timestamp':TIMESTAMP}])
        self.assertEqual(TIMESTAMP,(await self.repo(cursor).get_latest_current_summary(SCOPE))['timestamp'])
        self.assertFalse(cursor.closed);self.assertEqual(521,len(self.budgets))
        self.assertTrue(all(0<value<=1500 for value in self.budgets))

    async def test_single_document_exception_requires_no_cursor_cleanup(self):
        class Broken(LatestCursor):
            async def __anext__(self):raise RuntimeError('synthetic-latest-find-one')
        cursor=Broken()
        with self.assertRaises(RuntimeError):await self.repo(cursor).get_latest_current_summary(SCOPE)
        self.assertFalse(cursor.closed)

    async def test_cancelled_latest_single_document_propagates_without_cursor_cleanup(self):
        cursor=LatestCursor(blocked=True);task=asyncio.create_task(self.repo(cursor).get_latest_current_summary(SCOPE))
        await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(cursor.closed)
