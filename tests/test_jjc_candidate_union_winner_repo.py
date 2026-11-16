"""DEP11 bounded $unionWith winner discovery; metadata-only fakes, no domain emulation."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from pymongo.errors import ExecutionTimeout
from bson import Int64,ObjectId
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.utils.jjc_equipment_time import equipment_source_index
from src.services.jx3.jjc_ranking_equipment import validate_local_win
from tests.jjc_equipment_fixtures import TARGET_GLOBAL_ID,SCOPE,CHECKED_AT,frozen_member
from tests.test_jjc_candidate_winner_repo import (WinnerCollection,WinnerCursor,certified,
    WIN,TIME,MID,VALID,SORT,flatten,matches)
from tests.test_jjc_ranking_equipment_repo import project

TAG='__equipment_requested_gid'

def branches(pipeline):
    first=[];others=[]
    for stage in pipeline:
        if '$unionWith' in stage:
            union=stage['$unionWith']
            if union.get('coll')!='jjc_match_detail':raise AssertionError('winner branches may only read authoritative match collection')
            others.append(union['pipeline'])
        else:first.append(stage)
    return [first]+others

def row_for(gid,mid,stamp,*,equipment='purple',won=True,source=None):
    row=certified(mid,stamp,equipment=equipment,won=won,source=source)
    for side in ('team1','team2'):
        for index,player in enumerate(row['data']['detail'][side]['players_info']):
            player['global_id']=gid if side=='team1' and index==0 else str(int(TARGET_GLOBAL_ID)+100000+mid*10+index+(3 if side=='team2' else 0))
    row.update(equipment_source_index(row['match_id'],row['data']))
    return row

class BatchCursor(WinnerCursor):
    async def __anext__(self):
        prior=self.kind
        if prior=='winner_batch':self.kind='enumeration'
        try:return await super().__anext__()
        finally:self.kind=prior

class UnionWinnerCollection(WinnerCollection):
    def __init__(self,rows):
        super().__init__(rows);self.batch_started=asyncio.Event();self.batch_gate=asyncio.Event()
        self.block_batch=False;self.fail_batch=False;self.result_mutator=None
        self.mongo_timeout=False;self.close_gate=asyncio.Event();self.close_started=asyncio.Event();self.block_close=False
    def aggregate(self,pipeline,**kwargs):
        rows=[]
        for branch in branches(pipeline):
            match=next(stage['$match'] for stage in branch if '$match' in stage)
            sort=next(stage['$sort'] for stage in branch if '$sort' in stage)
            limit=next(stage['$limit'] for stage in branch if '$limit' in stage)
            projection=next(stage['$project'] for stage in branch if '$project' in stage)
            selected=[deepcopy(row) for row in self.rows if all(matches(row.get(field),predicate) for field,predicate in flatten(match).items())]
            for field,order in reversed(list(sort.items())):selected.sort(key=lambda row:row[field],reverse=order<0)
            for source in selected[:limit]:
                result=project(source,{key:value for key,value in projection.items() if value==1})
                if TAG in projection:result[TAG]=projection[TAG]['$literal']
                rows.append(result)
        if self.result_mutator:rows=self.result_mutator(rows)
        owner=self
        class OwnedBatchCursor(BatchCursor):
            async def __anext__(self):
                owner.batch_started.set()
                if owner.block_batch:await owner.batch_gate.wait()
                if owner.mongo_timeout and self.index==0 and self.offset>=1:raise ExecutionTimeout('synthetic-timeout',code=50)
                if owner.fail_batch and self.index==0 and self.offset>=1:raise RuntimeError('synthetic-winner-union-failure')
                return await super().__anext__()
            async def close(self):
                self.close_calls=getattr(self,'close_calls',0)+1
                owner.close_started.set()
                if owner.block_close:await owner.close_gate.wait()
                await super().close()
        cursor=OwnedBatchCursor(self,rows,'winner_batch',len(self.aggregates))
        self.aggregates.append({'pipeline':deepcopy(pipeline),'kwargs':deepcopy(kwargs),'cursor':cursor})
        self.cursors.append(cursor);return cursor

class WinnerUnionRepoContracts(unittest.IsolatedAsyncioTestCase):
    async def read(self,collection,ids):
        return await JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection)).list_local_win_candidates_batch(
            ids,season_start=SCOPE['season_start'],checked_at=CHECKED_AT,season_id=SCOPE['season_id'])

    async def test_boundary_counts_cover_every_gid_once_without_missing_winner_io(self):
        for count in (0,1,24,25,26,1000):
            with self.subTest(count=count):
                ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(count)]
                collection=UnionWinnerCollection([])
                self.assertEqual([],await self.read(collection,ids))
                self.assertEqual((count+24)//25,len(collection.aggregates))
                covered=[branch[0]['$match'][WIN] for call in collection.aggregates for branch in branches(call['pipeline'])]
                self.assertEqual(ids,covered)
                self.assertLessEqual(collection.peak,4)
                self.assertFalse(any(call['kind']=='source' for call in collection.finds))
                self.assertTrue(all(cursor.closed for cursor in collection.cursors))
                if count==0:self.assertEqual([],collection.finds)

    async def test_mongo50_partial_batch_aborts_whole_read_and_drains(self):
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(100)]
        collection=UnionWinnerCollection([row_for(gid,500+i,SCOPE['season_start']+i+1) for i,gid in enumerate(ids)])
        collection.mongo_timeout=True
        with self.assertRaises(ExecutionTimeout) as error:await self.read(collection,ids)
        self.assertEqual(50,error.exception.code)
        self.assertFalse(any(call['kind']=='source' for call in collection.finds))
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_repeated_cancellation_waits_until_every_batch_cursor_closes(self):
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(100)]
        collection=UnionWinnerCollection([row_for(gid,700+i,SCOPE['season_start']+i+1) for i,gid in enumerate(ids)])
        collection.block_batch=True;collection.block_close=True
        task=asyncio.create_task(self.read(collection,ids))
        try:
            await asyncio.wait_for(collection.batch_started.wait(),1)
            task.cancel();await asyncio.wait_for(collection.close_started.wait(),1)
            task.cancel();await asyncio.sleep(0)
            self.assertFalse(task.done(),'repeated cancellation must not abandon cleanup')
            collection.close_gate.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,2)
            self.assertTrue(all(cursor.closed for cursor in collection.cursors))
            self.assertTrue(all(getattr(cursor,'close_calls',1)==1 for cursor in collection.cursors))
            self.assertFalse(any(call['kind']=='source' for call in collection.finds))
        finally:
            collection.batch_gate.set();collection.close_gate.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_all_ids_use_25_branch_batches_and_no_global_candidate_limit(self):
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(61)]
        rows=[row_for(gid,100+i,SCOPE['season_start']+100+i) for i,gid in enumerate(ids[:-1])]
        collection=UnionWinnerCollection(rows);result=await self.read(collection,ids+ids[:2])
        self.assertEqual({row['match_id'] for row in rows},{row['match_id'] for row in result})
        self.assertEqual(3,len(collection.aggregates),'61 stable IDs require 25/25/11 winner batches')
        observed=[]
        for call in collection.aggregates:
            chunks=branches(call['pipeline']);self.assertLessEqual(len(chunks),25)
            self.assertGreater(len(chunks),0)
            self.assertEqual('idx_equipment_winner_latest_v1',call['kwargs'].get('hint'))
            self.assertTrue(0<call['kwargs'].get('maxTimeMS',1501)<=1500)
            self.assertTrue(0<call['kwargs'].get('batchSize',0)<=64)
            for branch in chunks:
                self.assertEqual(['$match','$sort','$limit','$project'],[next(iter(stage)) for stage in branch])
                match,sort,limit,projection=[next(iter(stage.values())) for stage in branch]
                self.assertEqual(dict(SORT),sort);self.assertEqual(1,limit)
                self.assertEqual({'$gte':SCOPE['season_start'],'$lte':CHECKED_AT},match[TIME])
                self.assertEqual({'_id':1,TIME:1,MID:1,TAG:{'$literal':match[WIN]}},projection)
                observed.append(match[WIN])
        self.assertEqual(ids,observed)
        self.assertFalse(any(call['kind']=='winner' for call in collection.finds),'do not retain per-role find round trips')
        self.assertTrue(all(TAG not in row for row in result),'technical role mapping is never public/domain output')
        self.assertLessEqual(collection.peak,4);self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_unknown_physical_duplicates_future_loss_and_int64_max_are_preserved(self):
        gid=TARGET_GLOBAL_ID;start=SCOPE['season_start']
        rows=[row_for(gid,Int64(9007199254740992),start+100),row_for(gid,Int64(9007199254740993),start+100,equipment=None),
            row_for(gid,777,CHECKED_AT+10,won=False)]
        dubious=row_for(gid,888,start+50);dubious[VALID]=False;dubious[TIME]=None;dubious[MID]=None;dubious[WIN]=[]
        other=deepcopy(dubious);other['_id']=str(dubious['_id']);rows.extend([dubious,other])
        collection=UnionWinnerCollection(rows);result=await self.read(collection,[gid])
        self.assertTrue(collection.aggregates,'new behavior must use bounded union aggregation')
        self.assertEqual(4,len(result));self.assertEqual(2,sum(row['match_id']==888 for row in result))
        self.assertIn(Int64(9007199254740993),{row['match_id'] for row in result})
        oracle=lambda value:validate_local_win(member=frozen_member(),candidates=value,scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={})
        self.assertEqual(oracle(rows),oracle(result));self.assertEqual('data_inconsistent',oracle(result)['state'])

    async def test_large_unknown_population_is_not_truncated_by_winner_batching(self):
        gid=TARGET_GLOBAL_ID;rows=[]
        for index in range(1201):
            row=row_for(gid,1000+index,SCOPE['season_start']+index+1)
            row[VALID]=False;row[TIME]=None;row[MID]=None;row[WIN]=[]
            rows.append(row)
        collection=UnionWinnerCollection(rows);result=await self.read(collection,[gid])
        self.assertTrue(collection.aggregates)
        self.assertEqual(1201,len(result));self.assertEqual({row['match_id'] for row in rows},{row['match_id'] for row in result})
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_missing_unknown_or_duplicated_requested_gid_is_rejected(self):
        for label,mutator in [('missing',lambda rows:[{k:v for k,v in rows[0].items() if k!=TAG}]),
                              ('unknown',lambda rows:[dict(rows[0],**{TAG:'999999'})]),
                              ('duplicate',lambda rows:rows+deepcopy(rows))]:
            collection=UnionWinnerCollection([row_for(TARGET_GLOBAL_ID,200,SCOPE['season_start']+200)])
            collection.result_mutator=mutator
            with self.subTest(label=label),self.assertRaisesRegex(ValueError,'candidate_source_changed'):
                await self.read(collection,[TARGET_GLOBAL_ID])
            self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_partial_aggregate_failure_never_starts_raw_fetch_and_closes_siblings(self):
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(100)]
        collection=UnionWinnerCollection([row_for(gid,200+i,SCOPE['season_start']+i+1) for i,gid in enumerate(ids)])
        collection.fail_batch=True
        with self.assertRaisesRegex(RuntimeError,'synthetic-winner-union-failure'):await self.read(collection,ids)
        self.assertTrue(collection.aggregates)
        self.assertFalse(any(call['kind']=='source' for call in collection.finds))
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_cancellation_drains_winner_batch_cursors(self):
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(100)]
        collection=UnionWinnerCollection([row_for(gid,300+i,SCOPE['season_start']+i+1) for i,gid in enumerate(ids)])
        collection.block_batch=True;task=asyncio.create_task(self.read(collection,ids))
        try:
            started=False
            try:
                await asyncio.wait_for(collection.batch_started.wait(),1)
                started=True
            except asyncio.TimeoutError:
                pass
            self.assertTrue(started,'actual winner aggregation must have started')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,2)
            self.assertTrue(all(cursor.closed for cursor in collection.cursors))
            self.assertFalse(any(call['kind']=='source' for call in collection.finds))
        finally:
            collection.batch_gate.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

if __name__=='__main__':unittest.main()
