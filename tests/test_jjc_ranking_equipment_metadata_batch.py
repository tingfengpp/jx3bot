"""DEP13 metadata lean batches change transport only; all completeness budgets remain."""
import os,unittest
from uuid import uuid4
from motor.motor_asyncio import AsyncIOMotorClient
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo,EquipmentSnapshotError
from tests.test_jjc_ranking_equipment_metadata import BudgetCursor
from tests.test_jjc_ranking_equipment_latest_single_batch import Wire
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import TIMESTAMP,SCOPE

class BatchCursor(BudgetCursor):
    def __init__(self,rows):super().__init__(rows=rows);self.batches=[]
    def batch_size(self,size):self.batches.append(size);return self

class MetadataBatchContracts(unittest.IsolatedAsyncioTestCase):
    async def test_default_full_payload_batch_remains_sixteen(self):
        cursor=BatchCursor([{'schema_version':2}]);repo=JjcRankingStatsRepo(db=object())
        self.assertEqual([{'schema_version':2}],await repo._metadata_rows(cursor))
        self.assertEqual([16],cursor.batches);self.assertTrue(cursor.closed);self.assertEqual(1500,cursor.max_time)
    async def test_lean_full_batch_consumes_512_to_eof_without_limit(self):
        rows=[{'timestamp':TIMESTAMP+i} for i in range(512)];cursor=BatchCursor(list(rows))
        self.assertEqual(rows,await JjcRankingStatsRepo(db=object())._metadata_rows(cursor,batch_size=512))
        self.assertEqual([512],cursor.batches);self.assertTrue(cursor.closed)
    async def test_513th_document_is_whole_read_failure_not_truncated_success(self):
        cursor=BatchCursor([{'timestamp':i} for i in range(513)])
        with self.assertRaises(EquipmentSnapshotError) as error:await JjcRankingStatsRepo(db=object())._metadata_rows(cursor,batch_size=512)
        self.assertEqual('read_failed',error.exception.code);self.assertTrue(cursor.closed)
    async def test_original_32mib_byte_budget_still_fails_whole_read(self):
        cursor=BatchCursor([{'data':'x'*(11*1024*1024)} for i in range(3)])
        with self.assertRaises(EquipmentSnapshotError) as error:await JjcRankingStatsRepo(db=object())._metadata_rows(cursor,batch_size=512)
        self.assertEqual('read_failed',error.exception.code);self.assertTrue(cursor.closed)
    async def test_batch_parameter_is_strict_integer_in_range_and_cleans_invalid_cursor(self):
        for size in (False,True,0,-1,513,1.5,'512',None):
            with self.subTest(size=size):
                cursor=BatchCursor([])
                with self.assertRaises(EquipmentSnapshotError) as error:await JjcRankingStatsRepo(db=object())._metadata_rows(cursor,batch_size=size)
                self.assertEqual('read_failed',error.exception.code);self.assertTrue(cursor.closed);self.assertEqual([],cursor.batches)
        for size in (1,16,511,512):
            cursor=BatchCursor([]);self.assertEqual([],await JjcRankingStatsRepo(db=object())._metadata_rows(cursor,batch_size=size))
            self.assertEqual([size],cursor.batches);self.assertTrue(cursor.closed)

@unittest.skipUnless(os.environ.get('JJC_TEST_MONGO_URI'),'explicit owned loopback required')
class MetadataBatchMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wire=Wire();self.uri=checked_test_uri(os.environ['JJC_TEST_MONGO_URI']);self.client=AsyncIOMotorClient(self.uri,event_listeners=[self.wire]);self.db=self.client['jjc_equipment_lean_test_'+uuid4().hex]
        self.addAsyncCleanup(self.cleanup)
        self.assertTrue((await self.client.admin.command('buildInfo'))['version'].startswith('4.4.'))
        await self.db.jjc_ranking_stat_summaries.create_index([('timestamp',-1)])
        await self.db.jjc_ranking_stat_summaries.insert_many([{'timestamp':TIMESTAMP+i,'current_season':SCOPE['season_id']} for i in range(416)])
        self.repo=JjcRankingStatsRepo(db=self.db,suppress_errors=False)
    async def cleanup(self):await self.client.drop_database(self.db.name);self.client.close()
    def reset(self):self.wire.started_rows.clear();self.wire.replies.clear()
    def assert_transport(self):
        finds=[cmd for name,cmd in self.wire.started_rows if name=='find'];self.assertEqual(2,len(finds))
        full=[cmd for cmd in finds if cmd.get('filter')=={'schema_version':2} or cmd.get('filter',{}).get('schema_version')==2]
        lean=[cmd for cmd in finds if cmd not in full]
        self.assertEqual(1,len(full));self.assertEqual(16,full[0].get('batchSize'))
        self.assertEqual(1,len(lean));self.assertEqual(512,lean[0].get('batchSize'));self.assertTrue(lean[0].get('projection'))
        self.assertTrue(all(0<cmd.get('maxTimeMS',1501)<=1500 for cmd in finds))
        self.assertFalse(any(name in ('getMore','killCursors') for name,cmd in self.wire.started_rows))
    async def test_416_complete_metadata_rows_use_one_lean_batch_in_all_five_modes(self):
        expected=list(range(TIMESTAMP+415,TIMESTAMP-1,-1))
        for mode in ('seasons','history','list','paged','meta'):
            with self.subTest(mode=mode):
                self.reset()
                if mode=='seasons':
                    result=await self.repo.list_seasons();self.assertEqual({'seasons':[{'name':str(SCOPE['season_id']),'latest_timestamp':expected[0]}],'default_season':str(SCOPE['season_id'])},result)
                elif mode=='history':
                    result=await self.repo.list_season_history(str(SCOPE['season_id']));self.assertEqual(expected,[item['timestamp'] for item in result['items']]);self.assertEqual(416,result['total'])
                elif mode=='list':self.assertEqual(expected,await self.repo.list_timestamps())
                elif mode=='paged':
                    result=await self.repo.list_timestamps(page=1,page_size=100);self.assertEqual(expected[:100],result['items']);self.assertEqual(416,result['total']);self.assertTrue(result['has_more'])
                else:
                    result=await self.repo.list_timestamps(with_meta=True);self.assertEqual(expected[:100],[item['timestamp'] for item in result['items']]);self.assertEqual(416,result['total']);self.assertTrue(result['has_more'])
                self.assert_transport()
    async def test_exact_512_lean_wire_batch_gets_eof_without_losing_any_row(self):
        await self.db.jjc_ranking_stat_summaries.insert_many([{'timestamp':TIMESTAMP+i,'current_season':SCOPE['season_id']} for i in range(416,512)])
        self.reset();result=await self.repo.list_timestamps();self.assertEqual(list(range(TIMESTAMP+511,TIMESTAMP-1,-1)),result)
        lean=[cmd for name,cmd in self.wire.started_rows if name=='find' and cmd.get('projection')]
        self.assertEqual(1,len(lean));self.assertEqual(512,lean[0].get('batchSize'))
        # A full-sized firstBatch is allowed to require one getMore to establish EOF.
        self.assertLessEqual(sum(name=='getMore' for name,cmd in self.wire.started_rows),1)
        self.assertFalse(any(name=='killCursors' for name,cmd in self.wire.started_rows))
