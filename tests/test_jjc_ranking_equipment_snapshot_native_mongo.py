"""DEP14 actual Motor dedicated snapshot transport; random owned loopback only."""
from copy import deepcopy
import os,unittest
from uuid import uuid4
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from tests.test_jjc_ranking_equipment_latest_single_batch import Wire
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import weapon
@unittest.skipUnless(os.environ.get('JJC_TEST_MONGO_URI'),'explicit owned loopback required')
class ConsumedSnapshotsNativeMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri=checked_test_uri(os.environ['JJC_TEST_MONGO_URI']);self.wire=Wire();self.client=AsyncIOMotorClient(self.uri,event_listeners=[self.wire],serverSelectionTimeoutMS=1500,connectTimeoutMS=1500,socketTimeoutMS=2000);self.db=self.client['jjc_equipment_consumed_test_'+uuid4().hex]
        self.addAsyncCleanup(self.cleanup)
        self.assertTrue((await self.client.admin.command('buildInfo'))['version'].startswith('4.4.'))
        await self.db.jjc_equipment_snapshot.create_index('snapshot_hash',unique=True)
    async def cleanup(self):await self.client.drop_database(self.db.name);self.client.close()
    async def test_701_full_snapshots_are_read_in_512_wire_batches_without_public_data_projection(self):
        rows=[{'_id':ObjectId(),'snapshot_hash':'full-'+str(i),'armors':[weapon('legendary' if i%2 else 'purple')],'unknown_full_document_payload':{'nested':[{'integer':i,'array':[i,i+1]}]}} for i in range(701)]
        await self.db.jjc_equipment_snapshot.insert_many(deepcopy(rows));repo=JjcMatchSnapshotRepo(db=self.db)
        self.wire.started_rows.clear();self.wire.replies.clear()
        self.assertEqual({},await repo.load_local_win_equipment_snapshots([]))
        self.assertEqual([],self.wire.started_rows)
        result=await repo.load_local_win_equipment_snapshots([row['snapshot_hash'] for row in rows])
        self.assertEqual({row['snapshot_hash']:row for row in rows},result)
        finds=[cmd for name,cmd in self.wire.started_rows if name=='find']
        self.assertEqual(1,len(finds));self.assertEqual(1500,finds[0].get('maxTimeMS'));self.assertEqual(512,finds[0].get('batchSize'));self.assertNotIn('projection',finds[0]);self.assertNotIn('limit',finds[0])
        self.assertEqual(1,sum(name=='getMore' for name,cmd in self.wire.started_rows));self.assertFalse(any(name=='killCursors' for name,cmd in self.wire.started_rows))
