"""DEP14 isolated dedicated snapshot getter and bootstrap native budget contracts."""
import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock
from pymongo.errors import ExecutionTimeout
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo,EquipmentSnapshotError
from tests.jjc_equipment_fixtures import SCOPE,TIMESTAMP,frozen_member,weapon
from tests.test_jjc_ranking_equipment_publish import candidate

class Cursor:
    def __init__(self,rows=(),error=None,blocked=False,close_error=None,close_blocked=False):
        self.rows=list(rows);self.error=error;self.blocked=blocked;self.close_error=close_error;self.close_blocked=close_blocked
        self.started=asyncio.Event();self.close_started=asyncio.Event();self.release_close=asyncio.Event();self.close_cancelled=False;self.closed=False;self.close_calls=0;self.batches=[];self.budgets=[]
    def max_time_ms(self,value):self.budgets.append(value);return self
    def batch_size(self,value):self.batches.append(value);return self
    def __aiter__(self):return self
    async def __anext__(self):
        self.started.set()
        if self.blocked:await asyncio.Event().wait()
        if self.rows:return self.rows.pop(0)
        if self.error:raise self.error
        raise StopAsyncIteration
    async def close(self):
        self.close_calls+=1;self.close_started.set()
        try:
            if self.close_blocked:await self.release_close.wait()
            if self.close_error:raise self.close_error
        except asyncio.CancelledError:self.close_cancelled=True;raise
        finally:self.closed=True

class DedicatedSnapshotRead(unittest.IsolatedAsyncioTestCase):
    def setup_repo(self,cursor):
        self.find=MagicMock(return_value=cursor)
        return JjcMatchSnapshotRepo(db=SimpleNamespace(jjc_equipment_snapshot=SimpleNamespace(find=self.find)))
    async def read(self,repo,hashes):
        method=getattr(repo,'load_local_win_equipment_snapshots',None)
        self.assertTrue(callable(method),'equipment view needs a dedicated bounded snapshot read')
        return await method(hashes)
    async def test_complete_full_documents_over_512_are_exhausted_without_projection_or_limit(self):
        rows=[{'snapshot_hash':'h'+str(index),'armors':[weapon()],'unknown_nested_payload':{'preserve':index}} for index in range(701)];cursor=Cursor(rows);repo=self.setup_repo(cursor)
        result=await self.read(repo,[row['snapshot_hash'] for row in rows])
        self.assertEqual({row['snapshot_hash']:row for row in rows},result);self.assertEqual([],cursor.rows)
        self.assertEqual(1,len(self.find.call_args.args));self.assertNotIn('projection',self.find.call_args.kwargs)
        self.assertEqual({'snapshot_hash':{'$in':[row['snapshot_hash'] for row in rows]}},self.find.call_args.args[0])
        self.assertEqual([1500],cursor.budgets);self.assertEqual([512],cursor.batches);self.assertTrue(cursor.closed);self.assertEqual(1,cursor.close_calls)
    async def test_empty_hashes_do_not_initialize_db(self):
        class Forbidden:
            def __getattr__(self,name):raise AssertionError('empty read accessed database')
        self.assertEqual({},await self.read(JjcMatchSnapshotRepo(db=Forbidden()),[]))
    async def test_partial_native_error_preserved_even_if_close_also_fails(self):
        error=ExecutionTimeout('synthetic-native',code=50);cursor=Cursor([{'snapshot_hash':'h'}],error=error,close_error=RuntimeError('synthetic-close'))
        with self.assertRaises(ExecutionTimeout) as actual:await self.read(self.setup_repo(cursor),['h'])
        self.assertIs(error,actual.exception);self.assertTrue(cursor.closed);self.assertEqual(1,cursor.close_calls)
    async def test_success_with_cleanup_error_is_not_partial_success(self):
        cursor=Cursor([{'snapshot_hash':'h'}],close_error=RuntimeError('synthetic-close'))
        with self.assertRaises(Exception):await self.read(self.setup_repo(cursor),['h'])
        self.assertTrue(cursor.closed)
    async def test_cleanup_deadline_stays_100ms_and_is_explicit(self):
        cursor=Cursor([{'snapshot_hash':'h'}],close_blocked=True);started=time.monotonic()
        try:
            with self.assertRaises(Exception):await asyncio.wait_for(self.read(self.setup_repo(cursor),['h']),.5)
            self.assertTrue(cursor.close_started.is_set());self.assertTrue(cursor.close_cancelled);self.assertLess(time.monotonic()-started,.4)
        finally:cursor.release_close.set()
    async def test_cancellation_waits_for_close_and_propagates(self):
        cursor=Cursor(blocked=True,close_blocked=True);repo=self.setup_repo(cursor)
        self.assertTrue(callable(getattr(repo,'load_local_win_equipment_snapshots',None)),'dedicated snapshot read required before lifecycle exercise')
        task=asyncio.create_task(self.read(repo,['h']))
        try:
            await asyncio.wait_for(cursor.started.wait(),1);task.cancel();await asyncio.wait_for(cursor.close_started.wait(),1)
            self.assertFalse(task.done());cursor.release_close.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
            self.assertTrue(cursor.closed);self.assertEqual(1,cursor.close_calls)
        finally:
            cursor.release_close.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    async def test_existing_loader_keeps_original_behavior_separate_from_new_path(self):
        cursor=Cursor([{'snapshot_hash':'h','armors':[weapon()]}]);repo=self.setup_repo(cursor)
        self.assertEqual({'h':{'snapshot_hash':'h','armors':[weapon()]}},await repo.load_equipment_snapshots(['h'],strict=True))
        self.assertEqual([],cursor.batches);self.assertEqual([],cursor.budgets);self.assertEqual(0,cursor.close_calls)

class FrozenNativeReadBudgets(unittest.IsolatedAsyncioTestCase):
    def setup_repo(self,*,v2=False,changed=False):
        summary=candidate(timestamp=TIMESTAMP) if v2 else {'timestamp':TIMESTAMP,'schema_version':1,'current_season':SCOPE['season_id'],'ranking_display_kind':'current','kungfu_cache_scope':deepcopy(SCOPE),'kungfu_statistics':{'top_1000':{'total_players':1}}}
        observed=deepcopy(summary)
        if changed:observed['generated_at']='concurrent-change'
        self.find_one=AsyncMock(side_effect=[summary,observed])
        cursor=MagicMock();cursor.max_time_ms.return_value=cursor;cursor.to_list=AsyncMock(return_value=[{'lane':'dps','kungfu':'山居剑意','members':[frozen_member()]}])
        return JjcRankingStatsRepo(db=SimpleNamespace(jjc_ranking_stat_summaries=SimpleNamespace(find_one=self.find_one),jjc_ranking_stat_details=SimpleNamespace(find=MagicMock(return_value=cursor))))
    def assert_budgets(self,count):
        self.assertEqual(count,self.find_one.await_count)
        for call in self.find_one.call_args_list:
            self.assertEqual(({'timestamp':TIMESTAMP},),call.args);self.assertEqual({'max_time_ms':1500},call.kwargs)
    async def test_both_legacy_summary_reads_have_native_1500_and_complete_cas(self):
        result=await self.setup_repo().load_frozen_equipment_context(TIMESTAMP);self.assertTrue(result['complete']);self.assertEqual(1,len(result['members']));self.assert_budgets(2)
    async def test_v2_first_full_document_read_keeps_validation_and_native_budget(self):
        result=await self.setup_repo(v2=True).load_frozen_equipment_context(TIMESTAMP);self.assertTrue(result['complete']);self.assertIsNotNone(result['frozen_view']);self.assert_budgets(1)
    async def test_concurrent_summary_change_remains_version_mismatch(self):
        with self.assertRaises(EquipmentSnapshotError) as error:await self.setup_repo(changed=True).load_frozen_equipment_context(TIMESTAMP)
        self.assertEqual('version_mismatch',error.exception.code);self.assert_budgets(2)
