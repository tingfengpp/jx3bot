"""DEP11 actual Mongo4.4 union branches, index plans and full-domain equivalence."""
from copy import deepcopy
import json,os,sys,unittest
from pathlib import Path
from uuid import uuid4
from bson import Int64,ObjectId,BSON
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.services.jx3.jjc_ranking_equipment import validate_local_win
from src.utils.jjc_equipment_time import equipment_source_index
from tests.test_jjc_ranking_equipment_mongo import Commands,test_client,create_equipment_source_indexes
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_candidate_union_winner_repo import TAG,branches,row_for
from tests.test_jjc_candidate_winner_repo import WIN,TIME,MID,VALID
from tests.jjc_equipment_fixtures import TARGET_GLOBAL_ID,SCOPE,CHECKED_AT,frozen_member

@unittest.skipUnless(os.environ.get('JJC_TEST_MONGO_URI'),'explicit isolated loopback URI required')
class WinnerUnionMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.recorder=Commands();self.uri=checked_test_uri(os.environ['JJC_TEST_MONGO_URI'])
        self.client=test_client(self.uri,self.recorder);self.name='jjc_kungfu_test_'+uuid4().hex;self.db=self.client[self.name]
        self.addAsyncCleanup(self.cleanup)
        self.assertTrue((await self.client.admin.command('buildInfo'))['version'].startswith('4.4.'))
        await create_equipment_source_indexes(self.db)
    async def cleanup(self):
        checked_test_uri(self.uri);await self.client.drop_database(self.name);self.client.close()
    async def read(self,ids):
        return await JjcInspectRepo(db=self.db).list_local_win_candidates_batch(ids,season_start=SCOPE['season_start'],checked_at=CHECKED_AT,season_id=SCOPE['season_id'])
    def domain(self,rows,gid):
        member=frozen_member();member['global_id']=gid
        return validate_local_win(member=member,candidates=rows,scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={})
    def aggregates(self):
        rows=[row for row in self.recorder.rows if row['name']=='aggregate' and row['collection']=='jjc_match_detail']
        self.assertTrue(rows,'actual repository must execute batched winner aggregation')
        return rows

    async def test_all_branches_are_covered_and_int64_domain_results_match_original(self):
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(61)];start=SCOPE['season_start']
        rows=[]
        for i,gid in enumerate(ids[:-1]):
            rows.extend([row_for(gid,100+i,start+10+i),row_for(gid,200+i,start+100+i)])
        shared=row_for(ids[0],5000,start+500)
        shared['data']['detail']['team1']['players_info'][1]['global_id']=ids[1]
        shared.update(equipment_source_index(shared['match_id'],shared['data']));rows.append(shared)
        rows.extend([row_for(ids[2],Int64(9007199254740992),start+600,source='int64-low'),
            row_for(ids[2],Int64(9007199254740993),start+600,source='int64-high')])
        rows.append(row_for(ids[4],6000,CHECKED_AT+10,won=False))
        dubious=row_for(ids[5],777,start+900)
        dubious['data']['detail']['match_time']=start+901;dubious.update(equipment_source_index(dubious['match_id'],dubious['data']))
        other=deepcopy(dubious);other['_id']=str(dubious['_id']);rows.extend([dubious,other])
        legacy=row_for(ids[6],888,start+50)
        for field in (TIME,VALID,WIN,MID):legacy.pop(field)
        rows.append(legacy)
        rows.append(row_for(ids[7],9000,start+700,equipment=None))
        unusual=row_for(ids[8],9001,start+800);unusual['data']['detail']['team1']['players_info'][0]['kungfu_id']=999999
        rows.append(unusual)
        await self.db.jjc_match_detail.insert_many(deepcopy(rows));self.recorder.reset()
        original=await self.db.jjc_match_detail.find({}).to_list(None)
        actual=await self.read(ids)
        calls=self.aggregates();self.assertEqual(3,len(calls))
        self.assertFalse(any(row['name']=='find' and row.get('filter') and WIN in row['filter'] for row in self.recorder.rows))
        requested=[];plans=[]
        for call in calls:
            pipeline=call['pipeline'];pieces=branches(pipeline)
            self.assertLessEqual(len(pieces),25);requested.extend(piece[0]['$match'][WIN] for piece in pieces)
            self.assertEqual(1500,call['maxTimeMS']);self.assertEqual('idx_equipment_winner_latest_v1',call['hint'])
            plan=await self.db.command({'explain':{'aggregate':'jjc_match_detail','pipeline':pipeline,'cursor':{},
                'hint':'idx_equipment_winner_latest_v1','maxTimeMS':1500},'verbosity':'executionStats'})
            cursors=[]
            def visit(value):
                if isinstance(value,dict):
                    self.assertNotEqual('COLLSCAN',value.get('stage'),'every recursive explain branch must use an index')
                    if '$cursor' in value:cursors.append(value['$cursor'])
                    for child in value.values():visit(child)
                elif isinstance(value,list):
                    for child in value:visit(child)
            visit(plan);self.assertEqual(len(pieces),len(cursors))
            for cursor in cursors:
                text=str(cursor['queryPlanner']['winningPlan'])
                self.assertIn('IXSCAN',text);self.assertIn('idx_equipment_winner_latest_v1',text)
                self.assertNotIn("'stage': 'FETCH'",text);self.assertNotIn("'stage': 'SORT'",text)
                self.assertNotIn("'stage': 'COLLSCAN'",text)
                self.assertEqual(0,cursor['executionStats']['totalDocsExamined'])
                self.assertLessEqual(cursor['executionStats']['nReturned'],1)
            plans.append(plan)
        self.assertCountEqual(ids,requested)  # Fixed workers may begin batches in any order.
        for gid in ids:self.assertEqual(self.domain(original,gid),self.domain(actual,gid),gid)
        self.assertEqual(Int64(9007199254740993),self.domain(actual,ids[2])['packet']['match_id'])
        self.assertEqual('data_inconsistent',self.domain(actual,ids[4])['state'])
        self.assertEqual('unknown',self.domain(actual,ids[7])['packet']['equipment_state'])
        self.assertEqual(9001,self.domain(actual,ids[8])['packet']['match_id'])
        self.assertEqual(2,sum(row['match_id']==777 for row in actual))
        self.assertTrue(all(TAG not in row for row in actual))
        base=Path(__file__).resolve().parents[1]/'openspec/changes/jjc-ranking-local-latest-match/evidence/2026-11-16-resume-tests'
        base.mkdir(parents=True,exist_ok=True)
        (base/('dep11-union-explain-py%d%d.json'%sys.version_info[:2])).write_text(json.dumps({'branches':len(requested),'plans':plans},default=str,indent=2))

    async def test_same_time_mid_uses_mongo_mixed_bson_source_id_tie_order(self):
        gid=TARGET_GLOBAL_ID;stamp=SCOPE['season_start']+100
        sources=[ObjectId('0123456789abcdef01234567'),'0123456789abcdef01234567',Int64(7)]
        rows=[]
        for index,source in enumerate(sources):
            row=row_for(gid,500,stamp,source=source);row['cached_at']=100+index;rows.append(row)
        await self.db.jjc_match_detail.insert_many(deepcopy(rows))
        # Mongo BSON order is numeric < string < ObjectId; fake BSON bytes are not this oracle.
        for expected in (Int64(7),'0123456789abcdef01234567',ObjectId('0123456789abcdef01234567')):
            self.recorder.reset()
            reference=await self.db.jjc_match_detail.find({WIN:gid,TIME:{'$gte':SCOPE['season_start'],'$lte':CHECKED_AT}}).sort([(TIME,-1),(MID,-1),('_id',1)]).limit(1).to_list(None)
            self.assertEqual(BSON.encode({'_id':expected}),BSON.encode({'_id':reference[0]['_id']}))
            actual=await self.read([gid]);self.assertEqual(1,len(actual))
            self.assertEqual(reference[0]['cached_at'],actual[0]['cached_at'])
            self.assertEqual(self.domain(reference,gid),self.domain(actual,gid))
            call=self.aggregates()[0]
            plan=await self.db.command({'explain':{'aggregate':'jjc_match_detail','pipeline':call['pipeline'],'cursor':{},'hint':'idx_equipment_winner_latest_v1','maxTimeMS':1500},'verbosity':'executionStats'})
            def verify(value):
                if isinstance(value,dict):
                    if value.get('stage') in ('COLLSCAN','FETCH','SORT'):self.fail('unexpected stage '+value['stage'])
                    for child in value.values():verify(child)
                elif isinstance(value,list):
                    for child in value:verify(child)
            verify(plan)
            await self.db.jjc_match_detail.delete_one({'_id':expected})

    async def test_over_one_thousand_uncertain_sources_are_still_complete(self):
        rows=[];gid=TARGET_GLOBAL_ID;start=SCOPE['season_start']
        for index in range(1201):
            row=row_for(gid,1000+index,start+index+1)
            row['data']['detail']['match_time']=start+index+2
            row.update(equipment_source_index(row['match_id'],row['data']));rows.append(row)
        await self.db.jjc_match_detail.insert_many(deepcopy(rows));self.recorder.reset()
        actual=await self.read([gid]);self.aggregates()
        self.assertEqual(1201,len(actual));self.assertEqual({row['match_id'] for row in rows},{row['match_id'] for row in actual})
        self.assertEqual(self.domain(rows,gid),self.domain(actual,gid))
        self.assertEqual('data_inconsistent',self.domain(actual,gid)['state'])

if __name__=='__main__':unittest.main()
