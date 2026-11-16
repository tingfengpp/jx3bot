"""DEP12 latest-summary pagination must not allocate/kill a server cursor."""
import asyncio
from copy import deepcopy
import os,unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
from src.storage.mongo_repos.jjc_ranking_stats_repo import EquipmentSnapshotError
from bson import ObjectId,Decimal128,Binary
from pymongo.monitoring import CommandListener
from pymongo.errors import ExecutionTimeout
from motor.motor_asyncio import AsyncIOMotorClient
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo
from tests.test_jjc_ranking_equipment_metadata import LatestCursor
from tests.test_jjc_ranking_equipment_publish import candidate
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import SCOPE,TIMESTAMP

class SingleDocumentCollection:
    def __init__(self,rows,*,slow_close=False,error=None,blocked=False):
        self.rows=deepcopy(rows);self.calls=[];self.cursor_calls=0;self.close_calls=0
        self.slow_close=slow_close;self.error=error;self.blocked=blocked;self.started=asyncio.Event()
    async def find_one(self,query,**kwargs):
        self.calls.append((deepcopy(query),deepcopy(kwargs)));self.started.set()
        if self.blocked:await asyncio.Event().wait()
        if self.error:raise self.error
        return self.rows.pop(0) if self.rows else None
    def find(self,*args,**kwargs):
        self.cursor_calls+=1;owner=self
        class LegacyCursor(LatestCursor):
            async def __anext__(self):
                owner.started.set()
                if owner.blocked:await asyncio.Event().wait()
                if owner.error:raise owner.error
                return await super().__anext__()
            async def close(self):
                owner.close_calls+=1
                if owner.slow_close:await asyncio.Event().wait()
                await super().close()
        return LegacyCursor(rows=deepcopy(self.rows))

class LatestSingleBatchContracts(unittest.IsolatedAsyncioTestCase):
    def repo(self,collection):return JjcRankingStatsRepo(db=SimpleNamespace(jjc_ranking_stat_summaries=collection))
    async def test_valid_result_never_waits_for_server_cursor_cleanup(self):
        collection=SingleDocumentCollection([{'timestamp':TIMESTAMP,'current_season':SCOPE['season_id']}],slow_close=True)
        result=await asyncio.wait_for(self.repo(collection).get_latest_current_summary(SCOPE),.5)
        self.assertEqual(TIMESTAMP,result['timestamp'])
        self.assertEqual(1,len(collection.calls));self.assertEqual(0,collection.cursor_calls);self.assertEqual(0,collection.close_calls)
        self.assertTrue(0<collection.calls[0][1]['max_time_ms']<=1500)
    async def test_all_invalid_candidates_are_visited_to_valid_or_eof_without_cutoff(self):
        invalid=[{'_id':ObjectId(),'timestamp':TIMESTAMP+1000-i,'schema_version':2} for i in range(520)]
        invalid.append({'_id':ObjectId(),'timestamp':TIMESTAMP+1,'schema_version':99})
        valid=candidate(timestamp=TIMESTAMP)
        for tail,expected in (([valid],TIMESTAMP),([],None)):
            collection=SingleDocumentCollection(invalid+tail)
            result=await self.repo(collection).get_latest_current_summary(SCOPE)
            self.assertEqual(expected,result['timestamp'] if result else None)
            self.assertEqual(len(invalid)+1,len(collection.calls))
            self.assertEqual(0,collection.cursor_calls);self.assertEqual(0,collection.close_calls)
        tie=SingleDocumentCollection([{'_id':ObjectId(),'timestamp':TIMESTAMP,'schema_version':2},candidate(timestamp=TIMESTAMP)])
        self.assertEqual(TIMESTAMP,(await self.repo(tie).get_latest_current_summary(SCOPE))['timestamp'])
        self.assertEqual(2,len(tie.calls),'deterministic bad-first timestamp tie must continue')
    async def test_remaining_native_budget_is_not_reset_between_bad_candidates(self):
        clock=SimpleNamespace(now=0.0)
        class Timed(SingleDocumentCollection):
            async def find_one(self,*args,**kwargs):
                result=await super().find_one(*args,**kwargs);clock.now+=.4;return result
        rows=[{'_id':ObjectId(),'timestamp':TIMESTAMP+2,'schema_version':99},{'_id':ObjectId(),'timestamp':TIMESTAMP+1,'schema_version':99},candidate(timestamp=TIMESTAMP)]
        collection=Timed(rows)
        with patch('src.storage.mongo_repos.jjc_ranking_stats_repo.time',SimpleNamespace(monotonic=lambda:clock.now)):
            self.assertEqual(TIMESTAMP,(await self.repo(collection).get_latest_current_summary(SCOPE))['timestamp'])
        budgets=[kwargs['max_time_ms'] for query,kwargs in collection.calls]
        self.assertEqual(3,len(budgets));self.assertEqual(1500,budgets[0])
        self.assertTrue(1099<=budgets[1]<=1100,budgets);self.assertTrue(699<=budgets[2]<=700,budgets)

    async def test_exhausted_io_budget_is_failure_before_another_find_not_eof(self):
        clock=SimpleNamespace(now=0.0)
        class Timed(SingleDocumentCollection):
            async def find_one(self,*args,**kwargs):
                result=await super().find_one(*args,**kwargs);clock.now+=1.501;return result
        collection=Timed([{'_id':ObjectId(),'timestamp':TIMESTAMP+1,'schema_version':99},candidate(timestamp=TIMESTAMP)])
        with patch('src.storage.mongo_repos.jjc_ranking_stats_repo.time',SimpleNamespace(monotonic=lambda:clock.now)):
            with self.assertRaises(EquipmentSnapshotError) as error:await self.repo(collection).get_latest_current_summary(SCOPE)
        self.assertEqual('read_failed',error.exception.code);self.assertEqual(1,len(collection.calls))

    async def test_valid_native_first_read_does_not_require_another_io_budget(self):
        clock=SimpleNamespace(now=0.0)
        class Timed(SingleDocumentCollection):
            async def find_one(self,*args,**kwargs):
                result=await super().find_one(*args,**kwargs);clock.now+=1.501;return result
        collection=Timed([candidate(timestamp=TIMESTAMP)])
        with patch('src.storage.mongo_repos.jjc_ranking_stats_repo.time',SimpleNamespace(monotonic=lambda:clock.now)):
            self.assertEqual(TIMESTAMP,(await self.repo(collection).get_latest_current_summary(SCOPE))['timestamp'])
        self.assertEqual(1,len(collection.calls));self.assertEqual(1500,collection.calls[0][1]['max_time_ms'])

    async def test_repeated_physical_bad_source_cannot_loop_or_return_success(self):
        bad={'_id':ObjectId(),'timestamp':TIMESTAMP+1,'schema_version':99}
        collection=SingleDocumentCollection([bad,deepcopy(bad),candidate(timestamp=TIMESTAMP)])
        with self.assertRaises(EquipmentSnapshotError) as error:await self.repo(collection).get_latest_current_summary(SCOPE)
        self.assertEqual('read_failed',error.exception.code);self.assertEqual(2,len(collection.calls))

    async def test_native_mongo50_is_not_converted_to_empty_success(self):
        collection=SingleDocumentCollection([],error=ExecutionTimeout('synthetic-budget',code=50))
        with self.assertRaises(ExecutionTimeout):await self.repo(collection).get_latest_current_summary(SCOPE)
        self.assertEqual(0,collection.cursor_calls);self.assertEqual(0,collection.close_calls)
    async def test_cancelled_single_document_query_propagates_without_cleanup_io(self):
        collection=SingleDocumentCollection([],blocked=True)
        task=asyncio.create_task(self.repo(collection).get_latest_current_summary(SCOPE))
        await asyncio.wait_for(collection.started.wait(),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(0,collection.cursor_calls);self.assertEqual(0,collection.close_calls)

class Wire(CommandListener):
    def __init__(self):self.started_rows=[];self.replies=[]
    def started(self,event):self.started_rows.append((event.command_name,deepcopy(dict(event.command))))
    def succeeded(self,event):
        if event.command_name=='find':self.replies.append(deepcopy(dict(event.reply)))
    def failed(self,event):pass

@unittest.skipUnless(os.environ.get('JJC_TEST_MONGO_URI'),'explicit owned loopback required')
class LatestSingleBatchMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wire=Wire();self.uri=checked_test_uri(os.environ['JJC_TEST_MONGO_URI'])
        self.client=AsyncIOMotorClient(self.uri,event_listeners=[self.wire]);self.db=self.client['jjc_equipment_latest_test_'+uuid4().hex]
        self.addAsyncCleanup(self.cleanup)
        self.assertTrue((await self.client.admin.command('buildInfo'))['version'].startswith('4.4.'))
        await self.db.jjc_ranking_stat_summaries.create_index([('timestamp',-1)])
        self.repo=JjcRankingStatsRepo(db=self.db,suppress_errors=False)
    async def cleanup(self):await self.client.drop_database(self.db.name);self.client.close()
    def assert_no_server_cursor(self):
        finds=[cmd for name,cmd in self.wire.started_rows if name=='find']
        self.assertTrue(finds)
        for cmd in finds:
            self.assertEqual(1,cmd.get('limit'));self.assertTrue(cmd.get('singleBatch'))
            self.assertTrue(0<cmd.get('maxTimeMS',1501)<=1500)
        self.assertTrue(all(reply.get('cursor',{}).get('id')==0 for reply in self.wire.replies))
        self.assertFalse(any(name in ('getMore','killCursors') for name,cmd in self.wire.started_rows))
    async def test_real_wire_falls_back_across_26_bad_rows_and_unknown_schema(self):
        invalid=[{'timestamp':TIMESTAMP+1000-i,'current_season':SCOPE['season_id'],'schema_version':2} for i in range(26)]
        invalid.append({'timestamp':TIMESTAMP+1,'current_season':SCOPE['season_id'],'schema_version':99})
        valid=candidate(timestamp=TIMESTAMP)
        history=candidate(timestamp=TIMESTAMP+2000);history['ranking_display_kind']='historical'
        other=candidate(timestamp=TIMESTAMP+3000);other['current_season']='other-season'
        await self.db.jjc_ranking_stat_summaries.insert_many(invalid+[valid,history,other])
        self.wire.started_rows.clear();self.wire.replies.clear()
        result=await self.repo.get_latest_current_summary(SCOPE)
        self.assertEqual(TIMESTAMP,result['timestamp']);self.assertEqual(2,result['schema_version'])
        self.assertEqual(28,len([1 for name,cmd in self.wire.started_rows if name=='find']))
        self.assert_no_server_cursor()
    async def test_real_520_bad_chain_with_slow_io_exhausts_budget_as_failure(self):
        bad=[{'timestamp':TIMESTAMP+1000-i,'current_season':SCOPE['season_id'],'schema_version':2} for i in range(520)]
        await self.db.jjc_ranking_stat_summaries.insert_many(bad+[candidate(timestamp=TIMESTAMP)])
        collection=self.db.jjc_ranking_stat_summaries
        class SlowCollection:
            async def find_one(self,*args,**kwargs):
                await asyncio.sleep(.01)  # Local network/scheduler delay remains charged to the unchanged budget.
                return await collection.find_one(*args,**kwargs)
        repo=JjcRankingStatsRepo(db=SimpleNamespace(jjc_ranking_stat_summaries=SlowCollection()))
        self.wire.started_rows.clear();self.wire.replies.clear()
        with self.assertRaises(EquipmentSnapshotError) as error:await repo.get_latest_current_summary(SCOPE)
        self.assertEqual('read_failed',error.exception.code)
        finds=[cmd for name,cmd in self.wire.started_rows if name=='find']
        self.assertGreater(len(finds),1);self.assertLess(len(finds),521)
        self.assertLess(finds[-1]['maxTimeMS'],finds[0]['maxTimeMS'])
        self.assert_no_server_cursor()

    async def test_mixed_bson_timestamp_and_id_match_complete_original_cursor_oracle(self):
        valid=candidate(timestamp=TIMESTAMP);valid['_id']=ObjectId('000000000000000000000010')
        shapes=[('zz-string','s'),({'broken':1},7),([TIMESTAMP+20,'zz-array'],Binary(b'binary-source')),(Decimal128(str(TIMESTAMP+10)),{'source':'object'}),(None,'null-source')]
        bad=[{'_id':source,'timestamp':stamp,'schema_version':99,'current_season':SCOPE['season_id']} for stamp,source in shapes]
        bad.append({'_id':'missing-time','schema_version':99,'current_season':SCOPE['season_id']})
        await self.db.jjc_ranking_stat_summaries.insert_many(bad+[valid])
        base={'current_season':SCOPE['season_id'],'ranking_display_kind':{'$ne':'historical'}}
        original=await self.db.jjc_ranking_stat_summaries.find(base).sort([('timestamp',-1)]).to_list(None)
        expected=next(doc for doc in original if doc.get('schema_version',1)==2)
        self.wire.started_rows.clear();self.wire.replies.clear()
        result=await self.repo.get_latest_current_summary(SCOPE)
        self.assertEqual(expected['equipment_view_revision'],result['equipment_view_revision'])
        self.assertEqual(expected['timestamp'],result['timestamp']);self.assert_no_server_cursor()
        # All remaining mixed/null/missing documents must reach actual EOF, with no numeric filter.
        await self.db.jjc_ranking_stat_summaries.delete_one({'_id':valid['_id']})
        self.wire.started_rows.clear();self.wire.replies.clear()
        self.assertIsNone(await self.repo.get_latest_current_summary(SCOPE))
        self.assertEqual(len(bad)+1,len([1 for name,cmd in self.wire.started_rows if name=='find']))
        self.assert_no_server_cursor()

    async def test_same_timestamp_damaged_source_does_not_skip_readable_tie(self):
        valid=candidate(timestamp=TIMESTAMP);valid['_id']=ObjectId('000000000000000000000001')
        bad={'_id':ObjectId('000000000000000000000002'),'timestamp':TIMESTAMP,'current_season':SCOPE['season_id'],'schema_version':2}
        await self.db.jjc_ranking_stat_summaries.insert_many([bad,valid])
        self.wire.started_rows.clear();self.wire.replies.clear()
        result=await self.repo.get_latest_current_summary(SCOPE)
        self.assertEqual(2,result['schema_version']);self.assertEqual(TIMESTAMP,result['timestamp'])
        count=len([1 for name,cmd in self.wire.started_rows if name=='find'])
        self.assertIn(count,(1,2),'timestamp sort does not promise a tie order')
        print('DEP12 real same timestamp bad-first path: '+('observed' if count==2 else 'not observed; deterministic fake covers it'))
        self.assert_no_server_cursor()
