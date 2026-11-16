"""DEP05: certified subset reduction must preserve the authoritative Python result."""
import asyncio
from copy import deepcopy
import os
import unittest
from uuid import uuid4
from types import SimpleNamespace

from bson import Decimal128, Int64
from src.storage.mongo_repos import jjc_inspect_repo as product
from src.services.jx3.jjc_ranking_equipment import validate_local_win
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID, match_doc, frozen_member, weapon
from tests.test_jjc_candidate_fast_prune_mongo import previous_proven_old, set_path, TIME_PATHS
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_mongo import Commands, test_client, create_indexes
from tests.test_jjc_ranking_equipment_repo import RecordingCursor, matches, project


def identity_filter(ids):
    return {"$or":[{"data.detail."+side+".players_info.global_id":{"$in":ids}} for side in ("team1","team2")]}


def original_query(ids):
    return {"$and":[identity_filter(ids),{"$expr":{"$not":[previous_proven_old()]}}]}


def pipeline(ids,season_id=None):
    factory=getattr(product,"build_local_win_candidate_pipeline",None)
    if factory is None:
        # Existing business read remains executable: missing compression is RED,
        # never an ImportError counted as a failed business test.
        return [{"$match":original_query(ids)}]
    return factory(ids,season_start=SCOPE["season_start"],checked_at=CHECKED_AT,season_id=season_id)


def canonical(mid,when,*,won=True,equipment="purple"):
    row=match_doc(mid,when=when,won=won,equipment=equipment)
    row["data"]["detail"].pop("season_id",None)
    # Production's common three timestamp copies must all be certifiable.
    row["data"]["detail"]["match_time"]=when
    row["data"]["detail"]["basic_info"]["start_time"]=when
    row["cached_at"]=4000000000+mid
    return row


def validate(rows,gid=TARGET_GLOBAL_ID):
    return validate_local_win(member=frozen_member(global_id=gid),candidates=rows,
        scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={"hash-purple":{"armors":[weapon("purple")]}})


def variants():
    values=[]
    def add(label,fn):values.append((label,fn))
    for path in TIME_PATHS:
        for name,value in (("conflict",CHECKED_AT+1),("null",None),("string",str(SCOPE["season_start"]+50)),
                           ("array",[SCOPE["season_start"]+50]),("decimal",Decimal128(str(SCOPE["season_start"]+50))),
                           ("nan",float("nan")),("inf",float("inf")),("bool",True)):
            # Null aliases may be certifiable; all other generated variants must
            # remain as evidence unless a strict certificate explicitly proves them.
            if name!="null":add(path+":"+name,lambda r,p=path,v=value:set_path(r,p,v))
    for path,value in (("match_id","400"),("match_id",True),("match_id",Decimal128("400")),
                       ("data.match_id",399),("data.detail.match_id",399),
                       ("match_id",0),("match_id",float(400)),
                       ("data.unavailable",True),("data.unavailable","false"),
                       ("data.detail.basic_info",[]),("data.detail.basic_info",[{}]),
                       ("data.detail.team1.won",1),("data.detail.team2.won",True),
                       ("data.detail.team2.won",None)):
        add(path+":"+repr(value),lambda r,p=path,v=value:set_path(r,p,v))
    for prefix in ("data","data.detail","data.detail.basic_info"):
        for key in ("match_type","pvp_type","type"):
            for value in ("3",True,[],2,5,None):
                add(prefix+"."+key+":"+repr(value),lambda r,p=prefix+"."+key,v=value:set_path(r,p,v))
    for prefix in ("data","data.detail"):
        for value in ("different-season",3,[],{}):
            add(prefix+".season_id:"+repr(value),lambda r,p=prefix+".season_id",v=value:set_path(r,p,v))
    for value in (0,True,"0","-1","9223372036854775808","000000000000000000001","１２３","700000000000000001\n","700000000000000001\r\n"," 700000000000000001","700000000000000001 ",["700000000000000005"],None):
        add("player-id:"+repr(value),lambda r,v=value:r["data"]["detail"]["team2"]["players_info"][2].update(global_id=v))
    for side in ("team1","team2"):
        add(side+"-array",lambda r,k=side:r["data"]["detail"].update({k:[deepcopy(r["data"]["detail"][k])]}))
    add("detail-array",lambda r:r["data"].update(detail=[deepcopy(r["data"]["detail"])]))
    add("data-array",lambda r:r.update(data=[deepcopy(r["data"])]))
    add("duplicate-players",lambda r:r["data"]["detail"]["team2"]["players_info"][2].update(global_id=TARGET_GLOBAL_ID))
    add("player-non-object",lambda r:r["data"]["detail"]["team2"]["players_info"].__setitem__(2,"bad-player"))
    add("short-roster",lambda r:r["data"]["detail"]["team2"]["players_info"].pop())
    add("long-roster",lambda r:r["data"]["detail"]["team2"]["players_info"].append(deepcopy(r["data"]["detail"]["team2"]["players_info"][0])))
    add("array-roster-member",lambda r:r["data"]["detail"]["team2"]["players_info"].__setitem__(2,[]))
    return values


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class CertifiedReductionMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri=checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        self.name="jjc_kungfu_test_"+uuid4().hex;self.recorder=Commands();self.client=test_client(uri,self.recorder)
        info=await self.client.admin.command("buildInfo");self.assertTrue(info["version"].startswith("4.4."))
        self.db=self.client[self.name];await create_indexes(self.db)
    async def asyncTearDown(self):
        self.assertTrue(self.name.startswith("jjc_kungfu_test_"))
        await self.client.drop_database(self.name);self.client.close()
    async def original(self,ids):
        return [r async for r in self.db.jjc_match_detail.find(original_query(ids))]
    async def reduced(self,ids,season_id=None):
        return [r async for r in self.db.jjc_match_detail.aggregate(pipeline(ids,season_id),maxTimeMS=1500)]

    async def test_canonical_history_reduces_to_latest_win_without_equipment_or_catalog_gates(self):
        start=SCOPE["season_start"]
        rows=[canonical(100+i,start+100+i,equipment="legendary") for i in range(120)]
        newest=canonical(900,start+500,equipment=None)
        newest["data"]["detail"]["team1"]["players_info"][0].update(kungfu_id=999999,equipment_snapshot_hash="missing-equipment-hash")
        rows.extend([newest,canonical(901,start+600,won=False)])
        await self.db.jjc_match_detail.insert_many(rows)
        original=await self.original([TARGET_GLOBAL_ID]);reduced=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual(validate(original),validate(reduced))
        self.assertEqual([900],[r["match_id"] for r in reduced],"certified history and newer losses must not inflate wire payload")
        self.assertEqual("unknown",validate(reduced)["packet"]["equipment_state"])
        self.assertEqual(999999,validate(reduced)["packet"]["self_kungfu_id"])

    async def test_shared_match_targets_and_sort_ties_choose_per_winner_then_dedupe(self):
        start=SCOPE["season_start"]
        rows=[canonical(100,start+100),canonical(101,start+100),canonical(102,start+200,won=False)]
        await self.db.jjc_match_detail.insert_many(rows)
        ids=[TARGET_GLOBAL_ID,str(int(TARGET_GLOBAL_ID)+1),str(int(TARGET_GLOBAL_ID)+3)]
        original=await self.original(ids);reduced=await self.reduced(ids)
        self.assertEqual({101,102},{r["match_id"] for r in reduced})
        self.assertEqual(len(reduced),len({r["match_id"] for r in reduced}))
        for gid in ids:self.assertEqual(validate(original,gid),validate(reduced,gid),gid)

    async def test_basic_info_only_start_time_is_certified_and_equal_aliases_stay_equivalent(self):
        stamp=SCOPE["season_start"]+100
        rows=[]
        for index in range(4):
            row=canonical(600+index,stamp+index)
            row["data"].pop("match_time")
            row["data"]["detail"].pop("match_time")
            rows.append(row)
        await self.db.jjc_match_detail.insert_many(rows)
        before=await self.original([TARGET_GLOBAL_ID]);after=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual(validate(before),validate(after))
        self.assertEqual([603],[r["match_id"] for r in after])
        equal=canonical(604,stamp+4)
        for path in TIME_PATHS:set_path(equal,path,float(stamp+4))
        await self.db.jjc_match_detail.insert_one(equal)
        after=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual([604],[r["match_id"] for r in after])
        self.assertEqual(validate(await self.original([TARGET_GLOBAL_ID])),validate(after))

    async def test_uncertified_parallel_time_and_match_id_arrays_survive_sort(self):
        malformed=canonical(700,SCOPE["season_start"]+50)
        malformed["match_id"]=[700,701]
        malformed["data"]["match_time"]=[SCOPE["season_start"]+50]
        healthy=canonical(900,SCOPE["season_start"]+500)
        await self.db.jjc_match_detail.insert_many([malformed,healthy])
        before=await self.original([TARGET_GLOBAL_ID]);after=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual(validate(before),validate(after))
        self.assertIn(malformed["cached_at"],{r["cached_at"] for r in after})

    async def test_long_match_ids_keep_exact_tie_order_without_double_rounding(self):
        stamp=float(SCOPE["season_start"])+100.25
        low=canonical(Int64(9007199254740992),stamp)
        high=canonical(Int64(9007199254740993),stamp,equipment=None)
        await self.db.jjc_match_detail.insert_many([low,high])
        before=await self.original([TARGET_GLOBAL_ID]);after=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual(validate(before),validate(after))
        self.assertEqual([9007199254740993],[r["match_id"] for r in after])
        self.assertEqual("unknown",validate(after)["packet"]["equipment_state"])

    async def test_uncertified_variant_matrix_is_preserved_and_python_results_are_identical(self):
        start=SCOPE["season_start"]
        for index,(label,mutate) in enumerate(variants()):
            with self.subTest(label=label):
                await self.db.jjc_match_detail.delete_many({})
                dubious=canonical(400,start+50);mutate(dubious)
                healthy=canonical(900,start+500)
                await self.db.jjc_match_detail.insert_many([dubious,healthy])
                original=await self.original([TARGET_GLOBAL_ID]);reduced=await self.reduced([TARGET_GLOBAL_ID])
                self.assertEqual(validate(original),validate(reduced),label)
                if not label.endswith(":None"):
                    self.assertIn(dubious["cached_at"],{r["cached_at"] for r in reduced},label)

    async def test_null_missing_optional_metadata_and_matching_season_are_safe(self):
        start=SCOPE["season_start"]
        rows=[canonical(100+i,start+100+i) for i in range(8)]
        for index,row in enumerate(rows):
            row["data"]["unavailable"]=None if index%2 else False
            row["data"]["detail"]["basic_info"]["pvp_type"]=3
            row["data"]["season_id"]=SCOPE["season_id"]
            row["data"]["detail"]["season_id"]=SCOPE["season_id"]
        await self.db.jjc_match_detail.insert_many(rows)
        before=await self.original([TARGET_GLOBAL_ID])
        after=await self.reduced([TARGET_GLOBAL_ID],SCOPE["season_id"])
        self.assertEqual(validate(before),validate(after))
        self.assertEqual([107],[r["match_id"] for r in after])
        # Without known season, all non-null season evidence must be retained.
        unknown_scope=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual(len(rows),len(unknown_scope))

    async def test_empty_and_unknown_conflicting_history_are_not_silently_limited(self):
        self.assertEqual([],await self.reduced([TARGET_GLOBAL_ID]))
        start=SCOPE["season_start"]
        rows=[]
        for index in range(1201):
            row=canonical(1000+index,start+index+1)
            row["data"]["detail"]["match_time"]=start+index+2
            rows.append(row)
        await self.db.jjc_match_detail.insert_many(rows)
        before=await self.original([TARGET_GLOBAL_ID]);after=await self.reduced([TARGET_GLOBAL_ID])
        self.assertEqual(1201,len(after),"all uncertain evidence must exhaust the aggregate cursor")
        self.assertEqual(validate(before),validate(after))


class AggregateRecordingCollection:
    def __init__(self,rows,*,fail_after=None):
        self.rows=deepcopy(rows);self.calls=[];self.finds=[];self.cursors=[];self.fail_after=fail_after
        for index,row in enumerate(self.rows):row.setdefault("_id","physical-source-"+str(index))
    def aggregate(self,pipeline,**kwargs):
        self.calls.append((deepcopy(pipeline),deepcopy(kwargs)))
        from tests.test_jjc_candidate_two_phase_repo import metadata_winner_union_rows
        selected=metadata_winner_union_rows(self,pipeline)
        cursor=RecordingCursor(selected,fail_after=self.fail_after)
        self.cursors.append(cursor);return cursor
    def find(self,query,projection=None,**kwargs):
        self.finds.append((deepcopy(query),deepcopy(projection)))
        selected=[project(row,projection) for row in self.rows if matches(row,query)]
        cursor=RecordingCursor(selected,fail_after=self.fail_after if "_id" in query else None)
        cursor.allow_winner_limit="equipment_winner_gids_v1" in query
        self.cursors.append(cursor);return cursor


class CertifiedReductionReadContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_repo_uses_bounded_identity_aggregate_and_exhausts_cursor(self):
        rows=[canonical(1000+i,SCOPE["season_start"]+i+1) for i in range(1201)]
        collection=AggregateRecordingCollection(rows)
        repo=product.JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection))
        result=await repo.list_local_win_candidates_batch([TARGET_GLOBAL_ID],
            season_start=SCOPE["season_start"],checked_at=CHECKED_AT)
        self.assertEqual(1201,len(result))
        self.assertTrue(any("_id" in query for query,projection in collection.finds),"candidate path must fetch complete source versions")
        self.assertTrue(collection.calls,"winner lookup uses bounded aggregation")
        for stages,kwargs in collection.calls:
            self.assertLessEqual(kwargs.get("maxTimeMS",1501),1500)
            self.assertIn(TARGET_GLOBAL_ID,repr(stages))
            from tests.test_jjc_candidate_two_phase_repo import winner_union_branches
            for branch in winner_union_branches(stages):
                self.assertEqual(1,branch[2]['$limit'],'only proved per-gid maximum may limit')
                self.assertEqual('equipment_winner_gids_v1',next(iter(branch[0]['$match'])))
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_aggregate_partial_failure_propagates_and_closes_cursor(self):
        collection=AggregateRecordingCollection([canonical(100+i,SCOPE["season_start"]+i+1) for i in range(5)],fail_after=2)
        repo=product.JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection))
        with self.assertRaisesRegex(RuntimeError,"synthetic-getMore-failure"):
            await repo.list_local_win_candidates_batch([TARGET_GLOBAL_ID],season_start=SCOPE["season_start"],checked_at=CHECKED_AT)
        self.assertTrue(any("_id" in query for query,projection in collection.finds),"failure must come from the raw source path")
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_identity_batches_cover_all_ids_and_never_exceed_one_hundred(self):
        collection=AggregateRecordingCollection([])
        ids=[str(int(TARGET_GLOBAL_ID)+index) for index in range(251)]
        result=await product.JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection)).list_local_win_candidates_batch(
            ids,season_start=SCOPE["season_start"],checked_at=CHECKED_AT)
        self.assertEqual([],result)
        self.assertTrue(collection.finds,"candidate read must enumerate identity-bounded source batches")
        covered=set()
        def identity_ids(value):
            answer=[]
            if isinstance(value,dict):
                for key,child in value.items():
                    if key.endswith("players_info.global_id") and isinstance(child,dict):
                        answer.extend(child.get("$in",[]))
                    answer.extend(identity_ids(child))
            elif isinstance(value,list):
                for child in value:answer.extend(identity_ids(child))
            return answer
        for query,projection in collection.finds:
            if "_id" in query or "equipment_winner_gids_v1" in query:continue
            self.assertEqual({"_id":1},projection)
            batch=set(identity_ids(query))
            self.assertTrue(batch)
            self.assertLessEqual(len(batch),100)
            self.assertTrue(batch.issubset(set(ids)))
            covered.update(batch)
        self.assertEqual(set(ids),covered)

    async def test_one_batch_failure_drains_siblings_without_cancelling_async_cursor_close(self):
        reading=asyncio.Event();closing=asyncio.Event();cancelled_read=asyncio.Event()
        release=asyncio.Event();second_closing=asyncio.Event();cursors=[]
        class Cursor:
            def __init__(self,index):self.index=index;self.closed=False;self.close_cancelled=False
            def hint(self,*args):return self
            def max_time_ms(self,*args):return self
            def batch_size(self,*args):return self
            def __aiter__(self):return self
            async def __anext__(self):
                if self.index==0:
                    await reading.wait();await closing.wait()
                    raise RuntimeError("one-aggregate-failed")
                if self.index==1:
                    reading.set()
                    try:await asyncio.Event().wait()
                    except asyncio.CancelledError:cancelled_read.set();raise
                raise StopAsyncIteration
            async def close(self):
                if self.index:
                    if self.index==2:closing.set()
                    else:second_closing.set()
                    try:await release.wait()
                    except asyncio.CancelledError:self.close_cancelled=True;raise
                self.closed=True
        class Collection:
            def find(self,query,projection=None,**kwargs):
                if "_id" in query:
                    cursor=Cursor(len(cursors));cursors.append(cursor);return cursor
                sources=list(range(768)) if query.get("equipment_read_valid_v1","missing") is None else []
                cursor=RecordingCursor([{"_id":value} for value in sources])
                cursor.allow_winner_limit="equipment_winner_gids_v1" in query
                return cursor
            def aggregate(self,pipeline,**kwargs):
                if 'equipment_winner_gids_v1' not in pipeline[0].get('$match',{}):raise AssertionError('no aggregate raw source reads')
                return RecordingCursor([])
        repo=product.JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=Collection()))
        task=asyncio.create_task(repo.list_local_win_candidates_batch(
            [str(int(TARGET_GLOBAL_ID)+i) for i in range(75)],
            season_start=SCOPE["season_start"],checked_at=CHECKED_AT))
        try:
            await asyncio.wait_for(second_closing.wait(),2)
            self.assertFalse(task.done(),"failure must not escape before sibling cleanup drains")
            self.assertTrue(cancelled_read.is_set())
            release.set()
            with self.assertRaisesRegex(RuntimeError,"one-aggregate-failed"):await asyncio.wait_for(task,2)
            self.assertEqual(3,len(cursors))
            self.assertTrue(all(cursor.closed for cursor in cursors))
            self.assertFalse(any(cursor.close_cancelled for cursor in cursors))
        finally:
            release.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_empty_batch_does_not_access_collection(self):
        class Forbidden:
            def __getattr__(self,name):raise AssertionError("empty candidate read must perform no DB access")
        self.assertEqual([],await product.JjcInspectRepo(db=Forbidden()).list_local_win_candidates_batch([],
            season_start=SCOPE["season_start"],checked_at=CHECKED_AT))


if __name__=="__main__":unittest.main()
