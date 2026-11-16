"""Real Mongo4.4 candidate/identity/snapshot/service tests; never default DB.

Publication CAS is already covered by test_jjc_ranking_equipment_publish.
This file owns only registered random test DBs and never stops the Mongo server.
"""
import asyncio
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
import time
import types
import unittest
from uuid import uuid4
from unittest.mock import patch, AsyncMock

from bson import ObjectId, BSON
import httpx
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.monitoring import CommandListener

import config
from src.services.jx3.jjc_ranking_equipment import equipment_view_statistics, validate_local_win
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from src.services.jx3.jjc_ranking import JjcRankingService
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TIMESTAMP, match_doc, frozen_member, weapon


class Commands(CommandListener):
    def __init__(self):
        self.rows=[];self.pending={}
    def started(self,event):
        command=event.command
        row={"name":event.command_name,"database":event.database_name,
            "collection":command.get(event.command_name),"request_id":event.request_id,
            "filter":deepcopy(command.get("filter")),"pipeline":deepcopy(command.get("pipeline")),"maxTimeMS":command.get("maxTimeMS"),"hint":deepcopy(command.get("hint"))}
        self.rows.append(row);self.pending[event.request_id]=row
    def succeeded(self,event):
        row=self.pending.pop(event.request_id,None)
        if row is not None:row["duration_micros"]=event.duration_micros
    def failed(self,event):
        row=self.pending.pop(event.request_id,None)
        if row is not None:row.update(duration_micros=event.duration_micros,failed=True,
            failure_code=event.failure.get("code") if isinstance(event.failure,dict) else None)
    def reset(self):
        if self.pending:raise AssertionError("cannot reset monitor over active driver operations")
        self.rows.clear()


class TestClock:
    def __init__(self): self.offset=0;self._frozen_mono=None
    def wall(self): return CHECKED_AT+self.offset
    def monotonic(self):return self._frozen_mono if self._frozen_mono is not None else time.monotonic()+self.offset
    def freeze_cache_time(self):self._frozen_mono=time.monotonic()+self.offset
    def resume_cache_time(self):self._frozen_mono=None
    def advance(self,seconds): self.offset+=seconds


def test_client(uri,recorder=None):
    safe=checked_test_uri(uri)
    return AsyncIOMotorClient(safe,serverSelectionTimeoutMS=1500,connectTimeoutMS=1500,
        socketTimeoutMS=2000,waitQueueTimeoutMS=500,maxPoolSize=4,retryReads=False,
        event_listeners=[recorder] if recorder is not None else [])


async def create_equipment_source_indexes(db):
    await db.jjc_match_detail.create_index([("equipment_winner_gids_v1",1),("equipment_read_time_v1",-1),
        ("equipment_match_id_v1",-1),("_id",1)],name="idx_equipment_winner_latest_v1")
    for side in ("team1","team2"):
        await db.jjc_match_detail.create_index([("data.detail."+side+".players_info.global_id",1),
            ("equipment_read_valid_v1",1),("equipment_read_time_v1",1),("_id",1)],
            name="idx_equipment_"+side+"_state_time_source_v1")


async def create_indexes(db):
    await db.jjc_match_detail.create_index("match_id",unique=True,name="idx_match_id")
    for side in ("team1","team2"):
        await db.jjc_match_detail.create_index("data.detail."+side+".players_info.global_id",name="idx_"+side+"_global_id")
        await db.jjc_match_detail.create_index([("data.detail."+side+".players_info.global_id",1),("_id",1)],name="idx_equipment_"+side+"_global_source")
    await create_equipment_source_indexes(db)
    await db.jjc_equipment_snapshot.create_index("snapshot_hash",unique=True)
    await db.jjc_ranking_stat_summaries.create_index("timestamp",unique=True)


def role_state(rank):
    bucket=(rank-1)%10
    return "legendary" if bucket<2 else "purple" if bucket<7 else "unknown"


def canonical_role_match(member,mid,when,*,won=True,state="purple",compressed=False):
    row=match_doc(mid,when=when,won=won,equipment=state if state!="unknown" else None)
    players=[p for side in ("team1","team2") for p in row["data"]["detail"][side]["players_info"]]
    for index,player in enumerate(players):
        player["global_id"]=str(800000000000000000+member["rank"]*10+index)
    target=players[0];target["global_id"]=member["global_id"]
    target["role_name"]=member["name"];target["server"]=member["server"]
    if compressed and state!="unknown":
        target.pop("armors",None);target["equipment_snapshot_hash"]="synthetic-"+state
    return row


async def populate(db,*,roles=10,history=6,current_history=4):
    """Stream bounded insert batches; each board role has its own six-player roster."""
    if not 1<=roles<=1000 or not 1<=current_history<=history:
        raise ValueError("invalid synthetic population")
    members=[];identities=[];batch=[];oracle={}
    for rank in range(1,roles+1):
        member=frozen_member(rank);oid=ObjectId()
        member["display_identity_id"]=str(oid);member.pop("identity_id",None)
        identity={"_id":oid,"global_id":member["global_id"],"name":member["name"],
            "server":member["server"],**deepcopy(member["display_locator"])}
        members.append(member);identities.append(identity)
        state=role_state(rank)
        for index in range(history):
            current=index<current_history
            when=SCOPE["season_start"]+100+index if current else SCOPE["season_start"]-1000-index
            won=not(current and index==current_history-1 and current_history>1)
            mid=rank*1000000+index+1
            row=canonical_role_match(member,mid,when,won=won,state=state if current else "purple",compressed=rank%2==0)
            if not current: row["data"]["detail"]["season_id"]="previous-equipment-season"
            batch.append(row)
            if current and won: oracle[rank]={"match_id":mid,"state":state}
            if len(batch)>=500:
                await db.jjc_match_detail.insert_many(batch,ordered=False);batch=[]
    if batch: await db.jjc_match_detail.insert_many(batch,ordered=False)
    await db.role_identities.insert_many(identities)
    await db.jjc_equipment_snapshot.insert_many([{"snapshot_hash":"synthetic-"+state,"armors":[weapon(state)]}
                                                for state in ("legendary","purple")])
    return members,oracle


def make_service(db,clock=None,service_class=JjcRankingEquipmentViewService):
    clock=clock or TestClock();snapshots=JjcMatchSnapshotRepo(db=db)
    service=service_class(stats_repo=JjcRankingStatsRepo(db=db,suppress_errors=False),
        identity_repo=RoleIdentityRepo(db=db),inspect_repo=JjcInspectRepo(db=db,snapshot_repo=snapshots,participant_repo=None),
        snapshot_repo=snapshots,scope=deepcopy(SCOPE),enabled=True,overlay_enabled=True,
        clock=clock.wall,monotonic=clock.monotonic)
    return service,clock


def snapshot_document(view):
    clean=deepcopy(view);clean.pop("_publication_expected",None)
    payload={"timestamp":clean["timestamp"],"schema_version":2,"equipment_complete":True,
        "equipment_source":"local_win_v2","equipment_view_revision":clean["equipment_view_revision"],
        "member_revision":clean["member_revision"],"current_season":clean["scope"]["season_id"],
        "kungfu_cache_scope":deepcopy(clean["scope"]),"ranking_display_kind":"current",
        "generated_at":CHECKED_AT,"ranking_cache_time":clean["timestamp"],"default_week":1,
        "week_info":"合成测试周","equipment_view":clean,"kungfu_statistics":equipment_view_statistics(clean)}
    # The actual production summary builder strips root/range lane members;
    # measuring a duplicated in-memory stats payload would inflate metadata IO.
    return JjcRankingService._build_summary_payload(None,payload)


def assert_view(test,view,oracle):
    test.assertEqual(len(oracle),len(view["members"]))
    for member in view["members"]:
        expected=oracle[member["rank"]];packet=member["local_win_packet"]
        test.assertIsNotNone(packet,member)
        test.assertEqual(expected["match_id"],int(packet["match_id"]))
        test.assertEqual(expected["state"],packet["equipment_state"])
    top=view["ranges"]["top_1000"]
    test.assertEqual(top["N"],top["L"]+top["P"]+top["U"])
    test.assertGreater(top["L"],0)
    if len(oracle)>=3: test.assertGreater(top["P"],0)
    test.assertEqual([rank for rank in sorted(oracle) if oracle[rank]["state"]=="purple"],top["purple_ranks"])


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class EquipmentActualMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri=checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        self.database="jjc_kungfu_test_"+uuid4().hex
        self.recorder=Commands();self.client=test_client(self.uri,self.recorder)
        info=await self.client.admin.command("buildInfo")
        self.assertTrue(info["version"].startswith("4.4."),"This mandatory suite proves Mongo4.4 semantics")
        self.db=self.client[self.database];await create_indexes(self.db)
        self.members,self.oracle=await populate(self.db)
        self.service,self.clock=make_service(self.db)
        for name in ("src.storage.mongo_repos.jjc_inspect_repo._get_db",
                     "src.storage.mongo_repos.jjc_match_snapshot_repo._get_db",
                     "src.storage.mongo_repos.role_identity_repo._get_db",
                     "src.storage.mongo_repos.jjc_ranking_stats_repo._get_db"):
            guard=patch(name,side_effect=AssertionError("default Mongo target forbidden"))
            guard.start();self.addCleanup(guard.stop)

    async def asyncTearDown(self):
        await self.service.close()
        checked_test_uri(self.uri)
        self.assertTrue(self.database.startswith("jjc_kungfu_test_"))
        self.assertEqual(32,len(self.database.split("jjc_kungfu_test_",1)[1]))
        await self.client.drop_database(self.database);self.client.close()

    async def frozen(self):
        return await self.service.build_frozen_view(timestamp=TIMESTAMP,members=deepcopy(self.members))

    async def test_snapshot_fixture_uses_actual_summary_builder_without_duplicate_lane_members(self):
        view=await self.frozen();document=snapshot_document(view)
        self.assertEqual(10,len(document["equipment_view"]["members"]))
        for current in document["kungfu_statistics"].values():
            self.assertNotIn("unknown_members",current)
            for lane in ("healer","dps"):
                self.assertNotIn("members",current[lane])
        await self.service.stats_repo.publish_equipment_snapshot(TIMESTAMP,document)
        self.assertEqual(view["equipment_view_revision"],(await self.service.stats_repo.load_complete_view(TIMESTAMP))["equipment_view_revision"])

    async def test_real_primary_details_find_new_wins_without_any_participant_projection(self):
        self.assertEqual(0,await self.db.jjc_match_participants.count_documents({}))
        view=await self.frozen();assert_view(self,view,self.oracle)
        self.assertEqual((10,2,5,3),tuple(view["ranges"]["top_1000"][key] for key in ("N","L","P","U")))
        namespaces={row["database"] for row in self.recorder.rows if row["name"] in ("find","aggregate","getMore")}
        self.assertEqual({self.database},namespaces)
        self.assertIs(self.db,self.service.inspect_repo.db)
        self.assertIs(self.db,self.service.snapshot_repo.db)

    async def test_real_both_team_winner_and_missing_newest_weapon_semantics(self):
        member=self.members[0]
        row=canonical_role_match(member,999001,CHECKED_AT-100,won=False,state="legendary")
        row["data"]["detail"]["team1"],row["data"]["detail"]["team2"]=row["data"]["detail"]["team2"],row["data"]["detail"]["team1"]
        latest=canonical_role_match(member,999002,CHECKED_AT-200,won=True,state="unknown")
        await self.db.jjc_match_detail.insert_many([row,latest])
        view=await self.frozen();packet=view["members"][0]["local_win_packet"]
        self.assertEqual(999002,packet["match_id"])
        self.assertEqual("unknown",packet["equipment_state"])
        self.assertEqual(2,len(packet["teammates"]))

    async def test_real_cursor_exhaustion_and_existing_global_indexes(self):
        member=self.members[0]
        rows=[canonical_role_match(member,30000000+i,SCOPE["season_start"]+1000+i) for i in range(1201)]
        await self.db.jjc_match_detail.insert_many(rows)
        candidates=await self.service.inspect_repo.list_local_win_candidates_batch([member["global_id"]],season_start=SCOPE["season_start"],checked_at=CHECKED_AT,max_time_ms=1500)
        self.assertEqual(1207,len(candidates))
        packet=validate_local_win(member=member,candidates=candidates,scope=SCOPE,checked_at=CHECKED_AT)["packet"]
        self.assertEqual(30001200,packet["match_id"])
        source=next(row for row in reversed(self.recorder.rows) if row["name"]=="find"
            and row.get("hint")=="_id_" and row["collection"]=="jjc_match_detail")
        self.assertLessEqual(len(source["filter"]["_id"]["$in"]),256)

    async def test_winner_index_is_covered_and_unknown_null_branch_fetches_only_unknown(self):
        from src.utils.jjc_equipment_time import equipment_source_index
        row=canonical_role_match(self.members[0],30009999,SCOPE["season_start"]+500)
        row["data"]["detail"].pop("season_id",None)
        row.update(equipment_source_index(row["match_id"],row["data"]))
        await self.db.jjc_match_detail.insert_one(row)
        query={"equipment_winner_gids_v1":self.members[0]["global_id"],
            "equipment_read_time_v1":{"$gte":SCOPE["season_start"],"$lte":CHECKED_AT}}
        covered=await self.db.command({"explain":{"find":"jjc_match_detail","filter":query,
            "projection":{"_id":1,"equipment_read_time_v1":1,"equipment_match_id_v1":1},
            "sort":{"equipment_read_time_v1":-1,"equipment_match_id_v1":-1,"_id":1},
            "limit":1,"hint":"idx_equipment_winner_latest_v1"},"verbosity":"executionStats"})
        self.assertEqual(1,covered["executionStats"]["nReturned"])
        self.assertEqual(0,covered["executionStats"]["totalDocsExamined"])
        self.assertIn("IXSCAN",repr(covered["queryPlanner"]["winningPlan"]))
        unknown=await self.db.command({"explain":{"find":"jjc_match_detail","filter":{
            "data.detail.team1.players_info.global_id":self.members[0]["global_id"],
            "equipment_read_valid_v1":None},"projection":{"_id":1},
            "hint":"idx_equipment_team1_state_time_source_v1"},"verbosity":"executionStats"})
        self.assertGreater(unknown["executionStats"]["nReturned"],0)
        self.assertGreater(unknown["executionStats"]["totalDocsExamined"],0)
        self.assertIn("IXSCAN",repr(unknown["queryPlanner"]["winningPlan"]))
        self.assertNotIn("COLLSCAN",repr(unknown["queryPlanner"]["winningPlan"]))

    async def test_server_prunes_only_proven_old_times_and_preserves_ambiguous_evidence(self):
        member=self.members[0];rows=[];expected=set()
        old=canonical_role_match(member,40000001,SCOPE["season_start"]-1)
        old["data"]["detail"].pop("season_id",None);rows.append(old)
        old_consistent=canonical_role_match(member,40000002,SCOPE["season_start"]-1)
        old_consistent["data"]["detail"]["basic_info"]["start_time"]=float(SCOPE["season_start"]-1);rows.append(old_consistent)
        for index,value in enumerate((None,True,"1799999999",float("nan"),[SCOPE["season_start"]-1])):
            row=canonical_role_match(member,40000010+index,SCOPE["season_start"]-1)
            if value is None:row["data"].pop("match_time")
            else:row["data"]["match_time"]=value
            rows.append(row);expected.add(row["match_id"])
        conflict=canonical_role_match(member,40000020,SCOPE["season_start"]-1)
        conflict["data"]["detail"]["basic_info"]["start_time"]=CHECKED_AT-10
        rows.append(conflict);expected.add(conflict["match_id"])
        await self.db.jjc_match_detail.insert_many(rows)
        from src.utils.jjc_equipment_candidates import build_local_win_candidate_pipeline
        stages=build_local_win_candidate_pipeline([member["global_id"]],
            season_start=SCOPE["season_start"],checked_at=CHECKED_AT)
        found=[row async for row in self.db.jjc_match_detail.aggregate(stages,maxTimeMS=1500)]
        found_ids={row["match_id"] for row in found}
        self.assertNotIn(40000001,found_ids)
        self.assertNotIn(40000002,found_ids)
        self.assertTrue(expected.issubset(found_ids),(expected,found_ids))
        self.assertIn(self.oracle[1]["match_id"],found_ids)

    async def test_real_snapshot_strict_distinguishes_missing_from_closed_driver(self):
        self.assertEqual({},await self.service.snapshot_repo.load_equipment_snapshots(["not-present"],strict=True))
        closed=test_client(self.uri);closed_db=closed[self.database];closed.close()
        with self.assertRaises(Exception) as caught:
            await JjcMatchSnapshotRepo(db=closed_db).load_equipment_snapshots(["synthetic-legendary"],strict=True)
        self.assertNotIsInstance(caught.exception,AssertionError)

    async def test_real_bson_int64_match_id_survives_public_dynamic_view(self):
        big_id = 900000000000001
        await self.db.jjc_match_detail.insert_one(canonical_role_match(self.members[0],big_id,CHECKED_AT-1,state="purple"))
        view = await self.frozen()
        await self.service.stats_repo.publish_equipment_snapshot(TIMESTAMP,snapshot_document(view))
        result = await self.service.read_dynamic_view(timestamp=TIMESTAMP,
            expected_member_revision=view["member_revision"],expected_frozen_source="local_win_v2")
        self.assertTrue(result["overlay_enabled"])
        self.assertEqual(str(big_id),result["view"]["members"][0]["local_win_packet"].get("match_id"))

    async def publish_via_actual_ranking_path(self,view):
        # This method uses only save/task/publication helpers; no ranking query fields.
        publisher=object.__new__(JjcRankingService)
        sync=AsyncMock()
        expected=deepcopy(view.get("_publication_expected"))
        with patch("src.services.jx3.jjc_ranking.JjcRankingStatsRepo",return_value=self.service.stats_repo), patch.object(
                JjcRankingService,"_enqueue_ranking_members_for_sync",sync):
            task=publisher._save_ranking_stats_to_mongo(timestamp=view["timestamp"],
                summary_payload=snapshot_document(view),detail_payloads=[],
                equipment_expected=expected)
            self.assertIsNotNone(task)
            await task
        sync.assert_awaited_once()

    async def test_actual_ranking_publish_B_retires_twenty_inflight_A_clients(self):
        first=await self.frozen()
        await self.publish_via_actual_ranking_path(first)
        second=await self.service.build_frozen_view(timestamp=TIMESTAMP+1,members=deepcopy(self.members))
        entered=asyncio.Event();release=asyncio.Event()
        original=self.service.inspect_repo.list_local_win_candidates_batch
        async def blocked(*args,**kwargs):
            entered.set()
            await release.wait()
            return await original(*args,**kwargs)
        tasks=[]
        try:
            with patch.object(JjcInspectRepo,"list_local_win_candidates_batch",side_effect=blocked):
                tasks=[asyncio.create_task(self.service.read_dynamic_view(timestamp=TIMESTAMP,
                    expected_member_revision=first["member_revision"],expected_frozen_source="local_win_v2")) for _ in range(20)]
                await asyncio.wait_for(entered.wait(),2)
                await self.publish_via_actual_ranking_path(second)
                release.set()
                responses=await asyncio.wait_for(asyncio.gather(*tasks),5)
            self.assertEqual(20,len(responses))
            for result in responses:
                self.assertTrue(result["retired"])
                self.assertFalse(result["overlay_enabled"])
                self.assertIsNone(result["view"])
                self.assertEqual(TIMESTAMP+1,result["latest_timestamp"])
            fresh=await self.service.read_dynamic_view(timestamp=TIMESTAMP+1,
                expected_member_revision=second["member_revision"],expected_frozen_source="local_win_v2")
            self.assertTrue(fresh["overlay_enabled"])
            assert_view(self,fresh["view"],self.oracle)
        finally:
            release.set()
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    async def test_actual_same_timestamp_publication_forces_old_manifest_reload_across_instances(self):
        first=await self.frozen()
        await self.publish_via_actual_ranking_path(first)
        members=deepcopy(self.members)
        members[0]["score"]+=1
        second=await self.service.build_frozen_view(timestamp=TIMESTAMP,members=members)
        self.assertNotEqual(first["member_revision"],second["member_revision"])
        await self.publish_via_actual_ranking_path(second)
        another,unused=make_service(self.db)
        try:
            for reader in (self.service,another):
                response=await reader.read_dynamic_view(timestamp=TIMESTAMP,
                    expected_member_revision=first["member_revision"],expected_frozen_source="local_win_v2")
                self.assertTrue(response["reload_required"])
                self.assertFalse(response["retired"])
                self.assertFalse(response["overlay_enabled"])
                self.assertIsNone(response["view"])
                self.assertEqual("member_revision_changed",response["reason"])
                self.assertEqual(second["member_revision"],response["current_member_revision"])
            fresh=await another.read_dynamic_view(timestamp=TIMESTAMP,
                expected_member_revision=second["member_revision"],expected_frozen_source="local_win_v2")
            self.assertTrue(fresh["overlay_enabled"])
            self.assertEqual(members[0]["score"],fresh["view"]["members"][0]["score"])
        finally:await another.close()

    async def test_actual_asgi_get_uses_real_repositories_and_changes_no_collections(self):
        view=await self.frozen()
        await self.service.stats_repo.publish_equipment_snapshot(TIMESTAMP,snapshot_document(view))
        root=Path(__file__).resolve().parents[1]
        singletons=types.ModuleType("src.services.jx3.singletons")
        singletons.jjc_ranking_inspect_service=types.SimpleNamespace()
        singletons.jjc_ranking_equipment_service=self.service
        singletons.jjc_ranking_equipment_view_service=self.service
        singletons.jjc_ranking_display_service=types.SimpleNamespace()
        api=types.ModuleType("src.api");api.__path__=[str(root/"src/api")]
        before=(await self.db.command({"dbHash":1}))["collections"]
        self.recorder.reset()
        with patch.dict(sys.modules,{"src.services.jx3.singletons":singletons,"src.api":api}), patch.object(config,"JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED",1,create=True), patch.object(config,"JJC_RANKING_DISPLAY_OVERLAY_ENABLED",1,create=True):
            spec=importlib.util.spec_from_file_location("_equipment_real_mongo_router",root/"src/api/routers/jjc_ranking_stats.py")
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            with patch.object(module,"JjcRankingStatsRepo",return_value=self.service.stats_repo):
                app=FastAPI();app.include_router(module.router)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test.invalid") as client:
                    response=await client.get("/api/jjc/ranking-stats/equipment-view",params={"timestamp":str(TIMESTAMP),"equipment_contract":"2","expected_member_revision":view["member_revision"],"expected_frozen_source":"local_win_v2"})
        self.assertEqual(200,response.status_code,response.text)
        body=response.json();self.assertEqual(0,body["status_code"],body)
        assert_view(self,body["data"]["view"],self.oracle)
        commands=list(self.recorder.rows)
        after=(await self.db.command({"dbHash":1}))["collections"]
        self.assertEqual(before,after)
        writes={"insert","update","delete","findAndModify","create","createIndexes","drop","dropDatabase"}
        self.assertFalse([row for row in commands if row["name"] in writes])
        self.assertTrue(any(row["name"] in ("find","aggregate") for row in commands))
        self.assertEqual({self.database},{row["database"] for row in commands if row["name"] in ("find","aggregate","getMore")})


if __name__=="__main__": unittest.main()
