"""DEP07 two-phase strict source enumeration; no Mongo client or default DB."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from bson import BSON, ObjectId
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from tests.jjc_equipment_fixtures import TARGET_GLOBAL_ID, SCOPE, CHECKED_AT, match_doc


def key(value):return BSON.encode({"_id":value})


def in_values(value,field):
    result=[]
    if isinstance(value,dict):
        for name,child in value.items():
            if name==field and isinstance(child,dict):result.extend(child.get("$in",[]))
            result.extend(in_values(child,field))
    elif isinstance(value,list):
        for child in value:result.extend(in_values(child,field))
    return result


def metadata_matches(value,predicate):
    if isinstance(predicate,dict):
        for operator,wanted in predicate.items():
            if operator=="$in":
                if not (any(item in wanted for item in value) if isinstance(value,list) else value in wanted):return False
            elif operator=="$gte":
                if value is None or value<wanted:return False
            elif operator=="$lte":
                if value is None or value>wanted:return False
            elif operator=="$gt":
                if value is None or value<=wanted:return False
            else:raise AssertionError("unsupported metadata query predicate "+operator)
        return True
    if type(predicate) is bool:return value is predicate
    return predicate in value if isinstance(value,list) else value==predicate


def winner_union_branches(pipeline):
    first=[];others=[]
    for stage in pipeline:
        if '$unionWith' in stage:
            if stage['$unionWith'].get('coll')!='jjc_match_detail':raise AssertionError('unexpected union collection')
            others.append(stage['$unionWith']['pipeline'])
        else:first.append(stage)
    return [first]+others

def metadata_winner_union_rows(owner,pipeline):
    """Only Mongo match/sort/project behavior over explicit metadata, not domain proof."""
    from tests.test_jjc_ranking_equipment_repo import project
    result=[]
    for branch in winner_union_branches(pipeline):
        match=branch[0]['$match'];sort=branch[1]['$sort'];limit=branch[2]['$limit'];projection=branch[3]['$project']
        if limit!=1:raise AssertionError('only certified per-role maximum may limit1')
        hook=owner.__dict__.get('before_winner_find')
        if hook:hook(owner,match)
        selected=[deepcopy(row) for row in owner.rows if all(metadata_matches(row.get(field),predicate) for field,predicate in match.items())]
        for field,order in reversed(list(sort.items())):
            selected.sort(key=lambda row:key(row[field]) if field=='_id' else row[field],reverse=order<0)
        for row in selected[:1]:
            value=project(row,{name:spec for name,spec in projection.items() if spec==1})
            for name,spec in projection.items():
                if isinstance(spec,dict) and '$literal' in spec:value[name]=spec['$literal']
            result.append(value)
    return result


class Cursor:
    def __init__(self,owner,rows,kind,index):
        self.owner=owner;self.rows=rows;self.kind=kind;self.index=index;self.offset=0
        self.closed=False;self.hints=[];self.budgets=[];self.wire_batches=[];self.sorts=[];self.limits=[]
    def hint(self,value):self.hints.append(value);return self
    def max_time_ms(self,value):self.budgets.append(value);return self
    def batch_size(self,value):self.wire_batches.append(value);return self
    def sort(self,specification):
        if self.kind!="winner":raise AssertionError("sort only positive winner lookup")
        self.sorts.append(deepcopy(specification))
        for field,order in reversed(specification):
            self.rows.sort(key=lambda row:key(row[field]) if field=="_id" else row[field],reverse=order<0)
        return self
    def limit(self,value):
        if self.kind!="winner" or value!=1:raise AssertionError("no arbitrary source/candidate limit")
        self.limits.append(value);self.rows=self.rows[:value];return self
    def __aiter__(self):return self
    async def __anext__(self):
        if self.kind in ("enumeration","winner","winner_batch"):
            self.owner.enum_entered.set()
            self.owner.active+=1;self.owner.peak=max(self.owner.peak,self.owner.active)
            try:
                if self.owner.block_enum:await self.owner.enum_gate.wait()
                await asyncio.sleep(0)
                if self.owner.fail_enum and self.index==0:raise RuntimeError("enumeration-driver-failed")
            finally:self.owner.active-=1
        if self.offset==len(self.rows):
            if self.kind=="aggregate" and self.owner.delete_after_aggregate and not self.owner.deleted:
                self.owner.deleted=True;self.owner.rows=self.owner.rows[1:]
            raise StopAsyncIteration
        row=deepcopy(self.rows[self.offset]);self.offset+=1;return row
    async def close(self):
        await asyncio.sleep(0)
        self.closed=True


class Collection:
    def __init__(self,rows):
        self.rows=deepcopy(rows);self.finds=[];self.aggregates=[];self.cursors=[]
        self.block_enum=False;self.fail_enum=False;self.delete_after_aggregate=False;self.deleted=False
        self.enum_entered=asyncio.Event();self.enum_gate=asyncio.Event();self.active=0;self.peak=0
    def find(self,query,projection=None,**kwargs):
        from tests.test_jjc_ranking_equipment_repo import project
        if "_id" in query:
            kind="source" if projection and "match_id" in projection else "presence"
            if kind=="source" and self.delete_after_aggregate and not self.deleted:
                self.deleted=True;self.rows=self.rows[1:]
            selected={key(value) for value in query["_id"]["$in"]}
            rows=[row for row in self.rows if key(row["_id"]) in selected]
        elif "equipment_winner_gids_v1" in query:
            kind="winner";rows=[row for row in self.rows if all(metadata_matches(row.get(field),value) for field,value in query.items())]
        else:
            kind="enumeration";rows=[]
            for side in ("team1","team2"):
                path="data.detail."+side+".players_info.global_id"
                if path in query:
                    targets=query[path]["$in"]
                    rows=[row for row in self.rows if any(player["global_id"] in targets
                        for player in row["data"]["detail"][side]["players_info"]) and all(
                        metadata_matches(row.get(field),value) for field,value in query.items() if field!=path)]
                    break
            else:raise AssertionError("unknown enumeration must query one indexed side")
        cursor=Cursor(self,[project(row,projection) for row in rows],kind,len(self.finds))
        self.finds.append({"kind":kind,"query":deepcopy(query),"projection":deepcopy(projection),"kwargs":kwargs,"cursor":cursor})
        self.cursors.append(cursor);return cursor
    def aggregate(self,pipeline,**kwargs):
        if 'equipment_winner_gids_v1' in pipeline[0].get('$match',{}):
            rows=metadata_winner_union_rows(self,pipeline)
            cursor=Cursor(self,rows,'winner_batch',len(self.aggregates))
            self.aggregates.append({'pipeline':deepcopy(pipeline),'kwargs':deepcopy(kwargs),'kind':'winner_batch','cursor':cursor})
            self.cursors.append(cursor);return cursor
        ids=in_values(pipeline[0],"_id")
        selected={key(value) for value in ids}
        # Overapproximate certification: the real Mongo differential suite owns it.
        rows=[row for row in self.rows if not ids or key(row["_id"]) in selected]
        cursor=Cursor(self,rows,"aggregate",len(self.aggregates))
        self.aggregates.append({"pipeline":deepcopy(pipeline),"kwargs":kwargs,"source_ids":ids,"cursor":cursor})
        self.cursors.append(cursor);return cursor


def fixture(count=600,roles=205):
    ids=[str(int(TARGET_GLOBAL_ID)+index) for index in range(roles)]
    rows=[]
    for index in range(count):
        row=match_doc(1000+index,when=SCOPE["season_start"]+index+1)
        row["_id"]=ObjectId();row["cached_at"]=index
        row["data"]["detail"]["team1"]["players_info"][0]["global_id"]=ids[index%roles]
        row["data"]["detail"]["team2"]["players_info"][0]["global_id"]=ids[(index+100)%roles]
        rows.append(row)
    if count>=2:
        rows[1]["_id"]=str(rows[0]["_id"])
        rows[1]["match_id"]=rows[0]["match_id"]
    return ids,rows


class TwoPhaseCandidateRepoTests(unittest.IsolatedAsyncioTestCase):
    def repo(self,collection):return JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection))
    async def read(self,repo,ids):
        return await repo.list_local_win_candidates_batch(ids,season_start=SCOPE["season_start"],
            checked_at=CHECKED_AT,season_id=SCOPE["season_id"])

    async def test_all_physical_sources_both_sides_batches_and_presence_are_complete(self):
        ids,rows=fixture();collection=Collection(rows)
        result=await self.read(self.repo(collection),ids)
        self.assertEqual(set(range(600)),{row["cached_at"] for row in result})
        enum=[call for call in collection.finds if call["kind"]=="enumeration"]
        self.assertFalse(any(call['kind']=='winner' for call in collection.finds))
        winner_gids=[branch[0]['$match']['equipment_winner_gids_v1'] for call in collection.aggregates for branch in winner_union_branches(call['pipeline'])]
        self.assertEqual(ids,winner_gids)
        self.assertTrue(all(len(winner_union_branches(call['pipeline']))<=25 for call in collection.aggregates))
        covered={"team1":set(),"team2":set()}
        for call in enum:
            self.assertEqual({"_id":1},call["projection"])
            for side in covered:
                path="data.detail."+side+".players_info.global_id"
                if path in call["query"]:
                    batch=call["query"][path]["$in"];self.assertLessEqual(len(batch),100);covered[side].update(batch)
                    self.assertIn("idx_equipment_"+side+"_state_time_source_v1",call["cursor"].hints)
            self.assertTrue(any(0<v<=1500 for v in call["cursor"].budgets))
            self.assertTrue(any(type(v) is int and 0<v<=8192 for v in call["cursor"].wire_batches))
            self.assertEqual([],call["cursor"].limits)
        for batch in covered.values():self.assertEqual(set(ids),batch)
        sources=[call for call in collection.finds if call["kind"]=="source"]
        all_sources=set()
        for call in sources:
            batch=call["query"]["_id"]["$in"]
            self.assertLessEqual(len(batch),256);all_sources.update(key(v) for v in batch)
            self.assertIn("_id_",call["cursor"].hints)
            self.assertIn(64,call["cursor"].wire_batches)
            self.assertTrue(all(call["projection"].get(field)==1 for field in
                ("match_id","equipment_read_time_v1","equipment_read_valid_v1","equipment_winner_gids_v1","equipment_match_id_v1")))
        self.assertEqual({key(row["_id"]) for row in rows},all_sources)
        self.assertTrue(all(call.get("kind")=="winner_batch" for call in collection.aggregates))
        self.assertFalse(any(call["kind"]=="presence" for call in collection.finds))
        self.assertLessEqual(collection.peak,4)
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))
        self.assertEqual(2,sum(row["match_id"]==1000 for row in result))

    async def test_presence_wire_batches_are_explicitly_bounded(self):
        ids,rows=fixture(3,1);collection=Collection(rows)
        result=await self.read(self.repo(collection),ids)
        self.assertEqual(3,len(result))
        presence=[call for call in collection.finds if call["kind"]=="source"]
        self.assertTrue(presence)
        for call in presence:
            self.assertTrue(any(type(value) is int and 0<value<=256 for value in call["cursor"].wire_batches),
                "presence cursor must explicitly bound wire batches")

    async def test_empty_source_enumeration_skips_candidate_fetch_and_presence(self):
        collection=Collection([])
        result=await self.read(self.repo(collection),[TARGET_GLOBAL_ID])
        self.assertEqual([],result)
        self.assertEqual(8,len([call for call in collection.finds if call["kind"]=="enumeration"]))
        self.assertTrue(all(call.get("kind")=="winner_batch" for call in collection.aggregates))
        self.assertFalse(any(call["kind"]=="presence" for call in collection.finds))
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_large_source_population_uses_fixed_workers_without_truncation(self):
        source_count=25601
        class SmallCursor:
            def __init__(self,ids,candidates=False,winner=False):self.ids=iter(ids);self.candidates=candidates;self.closed=False;self.winner=winner
            def hint(self,value):return self
            def sort(self,value):
                if not self.winner:raise AssertionError("sort only winner")
                return self
            def limit(self,value):
                if not self.winner or value!=1:raise AssertionError("limit only certified winner")
                self.ids=iter(list(self.ids)[:1]);return self
            def max_time_ms(self,value):return self
            def batch_size(self,value):return self
            def __aiter__(self):return self
            async def __anext__(self):
                await asyncio.sleep(0)
                try:source=next(self.ids)
                except StopIteration:raise StopAsyncIteration
                row={"_id":source}
                if self.candidates:row.update(match_id=source+1,cached_at=source,data={})
                return row
            async def close(self):self.closed=True
        class BulkCollection:
            def __init__(self):self.cursors=[];self.chunks=[]
            def find(self,query,projection=None,**kwargs):
                if "_id" in query:
                    ids=query["_id"]["$in"];self.chunks.append(set(ids));candidate=True
                elif "equipment_winner_gids_v1" in query:
                    ids=[];candidate=False
                else:
                    ids=range(source_count) if query.get("equipment_read_valid_v1","missing") is None else []
                    candidate=False
                cursor=SmallCursor(ids,candidates=candidate,winner="equipment_winner_gids_v1" in query);self.cursors.append(cursor);return cursor
            def aggregate(self,pipeline,**kwargs):
                assert 'equipment_winner_gids_v1' in pipeline[0]['$match']
                cursor=SmallCursor([]);self.cursors.append(cursor);return cursor
        collection=BulkCollection();created=[];live_peak=0
        original_create=asyncio.create_task
        def tracked(coroutine,*args,**kwargs):
            nonlocal live_peak
            task=original_create(coroutine,*args,**kwargs);created.append(task)
            live_peak=max(live_peak,sum(not item.done() for item in created))
            return task
        with patch("src.storage.mongo_repos.jjc_inspect_repo.asyncio.create_task",side_effect=tracked):
            result=await self.read(self.repo(collection),[TARGET_GLOBAL_ID])
        self.assertEqual(set(range(source_count)),{row["cached_at"] for row in result})
        self.assertEqual(set(range(source_count)),set().union(*collection.chunks))
        self.assertEqual(101,len(collection.chunks))
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))
        self.assertLessEqual(len(created),8,"two phases must create at most four fixed workers each")
        self.assertLessEqual(live_peak,4,"queued source chunks must not become unbounded asyncio tasks")

    async def test_source_enumeration_is_not_cached_between_reads(self):
        ids,rows=fixture(2,1);collection=Collection(rows);repo=self.repo(collection)
        await self.read(repo,ids)
        before=len([call for call in collection.finds if call["kind"]=="enumeration"])
        row=deepcopy(rows[0]);row["_id"]=ObjectId();row["match_id"]=9000;row["cached_at"]=99
        collection.rows.append(row)
        result=await self.read(repo,ids)
        after=len([call for call in collection.finds if call["kind"]=="enumeration"])
        self.assertGreater(after,before,"fresh reads must re-enumerate source IDs")
        self.assertIn(99,{row["cached_at"] for row in result})

    async def test_phase_one_failure_discards_everything_and_closes_siblings(self):
        ids,rows=fixture(30,205);collection=Collection(rows);collection.fail_enum=True
        with self.assertRaisesRegex(RuntimeError,"enumeration-driver-failed"):
            await self.read(self.repo(collection),ids)
        self.assertFalse(any(call["kind"]=="source" for call in collection.finds))
        self.assertTrue(collection.cursors)
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_phase_one_cancellation_drains_cursors(self):
        ids,rows=fixture(30,205);collection=Collection(rows);collection.block_enum=True
        task=asyncio.create_task(self.read(self.repo(collection),ids))
        try:
            await asyncio.sleep(.02)
            self.assertTrue(collection.enum_entered.is_set(),"phase one must actually enumerate sources")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,2)
            self.assertTrue(all(cursor.closed for cursor in collection.cursors))
            self.assertFalse(any(call["kind"]=="source" for call in collection.finds))
        finally:
            collection.enum_gate.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_disappearing_source_never_returns_partial_candidates(self):
        ids,rows=fixture(3,1);collection=Collection(rows);collection.delete_after_aggregate=True
        with self.assertRaisesRegex(ValueError,"candidate_source_disappeared"):
            await self.read(self.repo(collection),ids)
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))


if __name__=="__main__":unittest.main()
