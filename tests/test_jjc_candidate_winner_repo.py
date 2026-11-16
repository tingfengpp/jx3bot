"""Winner-index repo protocol and enum/fetch race contracts; no default DB.

Collection reuses the two-phase source fixture. It filters only explicit metadata
query predicates and records IO; aggregate deliberately does not emulate domain.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from bson import Int64, ObjectId
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.services.jx3.jjc_ranking_equipment import validate_local_win
from tests.test_jjc_candidate_two_phase_repo import Collection as SourceCollection, Cursor as SourceCursor, key, in_values
from tests.test_jjc_ranking_equipment_repo import project
from tests.jjc_equipment_fixtures import TARGET_GLOBAL_ID,SCOPE,CHECKED_AT,match_doc,frozen_member

WIN="equipment_winner_gids_v1";TIME="equipment_read_time_v1";MID="equipment_match_id_v1";VALID="equipment_read_valid_v1"
SORT=[(TIME,-1),(MID,-1),("_id",1)]
META={"_id":1,TIME:1,MID:1,WIN:1,VALID:1}


def flatten(query):
    result={}
    for name,value in query.items():
        if name=="$and":
            for child in value:result.update(flatten(child))
        else:result[name]=value
    return result


def matches(value,predicate):
    if not isinstance(predicate,dict):
        return predicate in value if isinstance(value,list) else value==predicate
    for operator,wanted in predicate.items():
        if operator=="$in":
            if not (any(item in wanted for item in value) if isinstance(value,list) else value in wanted):return False
        elif operator=="$gte":
            if value is None or value<wanted:return False
        elif operator=="$lte":
            if value is None or value>wanted:return False
        elif operator=="$gt":
            if value is None or value<=wanted:return False
        else:raise AssertionError("unsupported metadata predicate: "+operator)
    return True


class WinnerCursor(SourceCursor):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.sorts=[];self.limits=[]
    def sort(self,specification):
        if self.kind!="winner":raise AssertionError("only positive winner cursor may sort for latest")
        self.sorts.append(deepcopy(specification))
        for field,order in reversed(specification):self.rows.sort(key=lambda row:row[field],reverse=order<0)
        return self
    def limit(self,value):
        if self.kind!="winner" or value!=1:raise AssertionError("literal limit1 is allowed only for certified positive winner")
        self.limits.append(value);self.rows=self.rows[:value];return self
    async def __anext__(self):
        kind=self.kind
        if kind in ("winner","source"):self.kind="enumeration"
        try:return await super().__anext__()
        finally:self.kind=kind


class WinnerCollection(SourceCollection):
    def __init__(self,rows):
        super().__init__(rows);self.before_source_read=None;self.after_source_snapshot=None;self.before_winner_find=None
    def find(self,query,projection=None,**kwargs):
        flat=flatten(query)
        if "_id" in flat:
            kind="source" if projection and ("match_id" in projection or any(field=="data" or field.startswith("data.") for field in projection)) else "presence"
            if kind=="source" and self.before_source_read:
                change=self.before_source_read;self.before_source_read=None;change(self)
            sources={key(value) for value in flat["_id"]["$in"]}
            selected=[row for row in self.rows if key(row["_id"]) in sources]
        elif WIN in flat:
            kind="winner"
            if self.before_winner_find:self.before_winner_find(self,flat)
            selected=[row for row in self.rows if all(matches(row.get(field),predicate) for field,predicate in flat.items())]
        else:
            kind="enumeration";selected=[]
            for side in ("team1","team2"):
                path="data.detail."+side+".players_info.global_id"
                if path not in flat:continue
                targets=flat[path]["$in"]
                for row in self.rows:
                    if any(player["global_id"] in targets for player in row["data"]["detail"][side]["players_info"]) and all(matches(row.get(field),predicate) for field,predicate in flat.items() if field!=path):selected.append(row)
                break
            else:raise AssertionError("source enumeration must query one indexed side")
        rows=[project(row,projection) for row in selected]
        if kind=="source" and self.after_source_snapshot:
            change=self.after_source_snapshot;self.after_source_snapshot=None;change(self)
        cursor=WinnerCursor(self,rows,kind,len(self.finds))
        self.finds.append({"kind":kind,"query":deepcopy(query),"projection":deepcopy(projection),"kwargs":kwargs,"cursor":cursor})
        self.cursors.append(cursor);return cursor
    def aggregate(self,pipeline,**kwargs):
        return super().aggregate(pipeline,**kwargs)


def certified(mid,stamp,*,equipment="purple",won=True,source=None):
    row=match_doc(mid,when=stamp,equipment=equipment,won=won)
    row["_id"]=source if source is not None else ObjectId();row["cached_at"]=mid
    detail=row["data"]["detail"]
    # Positive scope-independent metadata cannot certify a non-null season ID.
    detail.pop("season_id",None)
    side="team1" if won else "team2"
    row.update({VALID:True,TIME:stamp,MID:Int64(mid),WIN:[p["global_id"] for p in detail[side]["players_info"]]})
    return row


def corpus():
    start=SCOPE["season_start"]
    rows=[certified(100,start+100),certified(200,start+200,equipment=None),certified(300,CHECKED_AT+10,won=False)]
    for index,stamp in enumerate((start+50,None,None)):
        row=match_doc(777,when=start+50);row["_id"]=ObjectId();row["cached_at"]=400+index
        row.update({VALID:False if index<2 else None,TIME:stamp,MID:None,WIN:None})
        rows.append(row)
    rows[-1]["_id"]=str(rows[-2]["_id"])
    return rows


class WinnerCandidateRepoTests(unittest.IsolatedAsyncioTestCase):
    async def read(self,collection,ids=None):
        return await JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection)).list_local_win_candidates_batch(
            ids or [TARGET_GLOBAL_ID],season_start=SCOPE["season_start"],checked_at=CHECKED_AT,season_id=SCOPE["season_id"])

    async def test_exact_winner_and_all_unknown_future_source_protocol(self):
        rows=corpus();collection=WinnerCollection(rows)
        ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(205)]
        result=await self.read(collection,ids)
        from tests.test_jjc_candidate_two_phase_repo import winner_union_branches
        self.assertFalse(any(call['kind']=='winner' for call in collection.finds))
        self.assertEqual(9,len(collection.aggregates))
        observed=[]
        for call in collection.aggregates:
            self.assertEqual('idx_equipment_winner_latest_v1',call['kwargs']['hint'])
            self.assertTrue(0<call['kwargs']['maxTimeMS']<=1500)
            self.assertEqual(64,call['kwargs']['batchSize'])
            for branch in winner_union_branches(call['pipeline']):
                query=branch[0]['$match'];observed.append(query[WIN])
                self.assertEqual({'$gte':SCOPE['season_start'],'$lte':CHECKED_AT},query[TIME])
                self.assertEqual(dict(SORT),branch[1]['$sort']);self.assertEqual(1,branch[2]['$limit'])
                self.assertEqual({'_id':1,TIME:1,MID:1,'__equipment_requested_gid':{'$literal':query[WIN]}},branch[3]['$project'])
        self.assertEqual(ids,observed)
        enumerations=[call for call in collection.finds if call["kind"]=="enumeration"]
        expected=set()
        for side in ("team1","team2"):
            for offset in range(0,len(ids),100):
                for branch in ("invalid_current","invalid_null","legacy","future"):expected.add((side,tuple(ids[offset:offset+100]),branch))
        actual=set()
        for call in enumerations:
            query=flatten(call["query"]);side=next(side for side in ("team1","team2") if "data.detail."+side+".players_info.global_id" in query)
            batch=query["data.detail."+side+".players_info.global_id"]["$in"]
            if query.get(VALID) is False:branch="invalid_null" if query.get(TIME)==None else "invalid_current"
            elif query.get(VALID) is True:branch="future"
            else:branch="legacy"
            actual.add((side,tuple(batch),branch))
            self.assertEqual({"_id":1},call["projection"])
            self.assertIn("idx_equipment_"+side+"_state_time_source_v1",call["cursor"].hints)
            self.assertTrue(any(0<value<=8192 for value in call["cursor"].wire_batches))
            self.assertTrue(any(0<value<=1500 for value in call["cursor"].budgets))
            self.assertEqual([],call["cursor"].limits)
        self.assertEqual(expected,actual)
        expected_sources={key(row["_id"]) for row in rows if row["match_id"]!=100}
        self.assertTrue(all(call.get("kind")=="winner_batch" for call in collection.aggregates),"phase2 must not aggregate-prune required source versions")
        self.assertFalse(any(call["kind"]=="presence" for call in collection.finds),"do not validate metadata using a later doc version")
        read_sources=set()
        for call in collection.finds:
            if call["kind"]=="source":
                sources=call["query"]["_id"]["$in"]
                self.assertLessEqual(len(sources),256);read_sources.update(key(value) for value in sources)
                self.assertTrue(all(call["projection"].get(field)==1 for field in META))
                self.assertEqual(1,call["projection"].get("match_id"))
                self.assertTrue(any(field=="data" or field.startswith("data.") for field in call["projection"]))
                self.assertIn("_id_",call["cursor"].hints)
                self.assertIn(64,call["cursor"].wire_batches)
                self.assertTrue(any(0<value<=1500 for value in call["cursor"].budgets))
                self.assertEqual([],call["cursor"].limits)
        self.assertEqual(expected_sources,read_sources)
        self.assertEqual({200,300,400,401,402},{row["cached_at"] for row in result})
        self.assertLessEqual(collection.peak,4)
        self.assertTrue(all(cursor.closed for cursor in collection.cursors))

    async def test_selected_winner_time_becomes_older_before_fetch_is_whole_read_failure(self):
        rows=[certified(100,SCOPE["season_start"]+100),certified(200,SCOPE["season_start"]+200)]
        collection=WinnerCollection(rows)
        collection.before_source_read=lambda owner:owner.rows[1].update({TIME:SCOPE["season_start"]+50})
        with self.assertRaisesRegex(ValueError,"candidate_source_changed"):await self.read(collection)

    async def test_selected_winner_loses_expected_gid_before_fetch_is_whole_read_failure(self):
        rows=[certified(100,SCOPE["season_start"]+100),certified(200,SCOPE["season_start"]+200)]
        collection=WinnerCollection(rows)
        collection.before_source_read=lambda owner:owner.rows[1].update({WIN:["999999"]})
        with self.assertRaisesRegex(ValueError,"candidate_source_changed"):await self.read(collection)

    async def test_selected_winner_mid_or_valid_change_is_whole_read_failure(self):
        for field,value in ((MID,Int64(201)),(VALID,False)):
            with self.subTest(field=field):
                collection=WinnerCollection([certified(200,SCOPE["season_start"]+200)])
                collection.before_source_read=lambda owner,k=field,v=value:owner.rows[0].update({k:v})
                with self.assertRaisesRegex(ValueError,"candidate_source_changed"):await self.read(collection)

    async def test_same_source_conflicting_winner_observations_fail_before_fetch(self):
        row=certified(200,SCOPE["season_start"]+200);collection=WinnerCollection([row])
        seen=[]
        def change_second(owner,query):
            if seen:owner.rows[0][TIME]+=1
            seen.append(query[WIN])
        collection.before_winner_find=change_second
        gid2=row[WIN][1]
        with self.assertRaisesRegex(ValueError,"candidate_source_changed"):await self.read(collection,[TARGET_GLOBAL_ID,gid2])
        self.assertFalse(any(call["kind"]=="source" for call in collection.finds),"conflicting expected source metadata cannot become a fetch candidate")

    async def test_selected_source_disappearance_still_reports_existing_error(self):
        collection=WinnerCollection([certified(200,SCOPE["season_start"]+200)])
        collection.before_source_read=lambda owner:owner.rows.clear()
        with self.assertRaisesRegex(ValueError,"candidate_source_disappeared"):await self.read(collection)

    async def test_future_loser_is_retained_and_blocks_original_domain_state(self):
        rows=[certified(200,SCOPE["season_start"]+200,equipment=None),certified(300,CHECKED_AT+10,won=False)]
        result=await self.read(WinnerCollection(rows))
        self.assertEqual({200,300},{row["cached_at"] for row in result})
        oracle=lambda candidates:validate_local_win(member=frozen_member(),candidates=candidates,scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={})
        self.assertEqual("data_inconsistent",oracle(result)["state"])
        self.assertEqual(oracle(rows),oracle(result))

    async def test_latest_missing_weapon_does_not_fall_back_to_older_equipment(self):
        rows=[certified(100,SCOPE["season_start"]+100),certified(200,SCOPE["season_start"]+200,equipment=None)]
        collection=WinnerCollection(rows);result=await self.read(collection)
        self.assertEqual([200],[row["cached_at"] for row in result])
        packet=validate_local_win(member=frozen_member(),candidates=result,scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={})["packet"]
        self.assertEqual(200,packet["match_id"]);self.assertEqual("unknown",packet["equipment_state"])

    async def test_raw_winner_change_with_unchanged_stored_metadata_is_rejected(self):
        collection=WinnerCollection([certified(200,SCOPE["season_start"]+200)])
        def mutate(owner):
            detail=owner.rows[0]["data"]["detail"]
            detail["team1"]["won"]=False;detail["team2"]["won"]=True
        collection.before_source_read=mutate
        with self.assertRaisesRegex(ValueError,"candidate_source_changed"):await self.read(collection)

    async def test_aba_restoration_after_raw_snapshot_cannot_validate_a_later_version(self):
        original=certified(200,SCOPE["season_start"]+200)
        collection=WinnerCollection([original])
        def mutate(owner):
            owner.rows[0][VALID]=False
            detail=owner.rows[0]["data"]["detail"]
            detail["team1"]["won"]=False;detail["team2"]["won"]=True
        collection.before_source_read=mutate
        collection.after_source_snapshot=lambda owner:owner.rows.__setitem__(0,deepcopy(original))
        with self.assertRaisesRegex(ValueError,"candidate_source_changed"):await self.read(collection)
        self.assertEqual(1,sum(call["kind"]=="source" for call in collection.finds))
        self.assertFalse(any(call["kind"]=="presence" for call in collection.finds))
        self.assertTrue(all(call.get("kind")=="winner_batch" for call in collection.aggregates))
