"""Winner-index feasibility and bounded-time read counterexample; Mongo4.4 only.

The indexed valid flag is the agreed equipment_read_valid_v1 metadata.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
import unittest
from bson import Int64
from tests.test_jjc_candidate_certified_reduce_mongo import canonical,validate,identity_filter
from tests.test_jjc_ranking_equipment_mongo import test_client
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import SCOPE,CHECKED_AT,TARGET_GLOBAL_ID

WIN="equipment_winner_gids_v1"
TIME="equipment_read_time_v1"
MID="equipment_match_id_v1"
VALID="equipment_read_valid_v1"
INDEX="idx_equipment_winner_latest_v1"
SORT={TIME:-1,MID:-1,"_id":1}


def certify(row):
    detail=row["data"]["detail"]
    side="team1" if detail["team1"]["won"] else "team2"
    row.update({WIN:[p["global_id"] for p in detail[side]["players_info"]],TIME:row["data"]["match_time"],MID:Int64(row["match_id"]),VALID:True})
    return row


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class WinnerCoveredLatestMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=test_client(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]))
        self.name="jjc_kungfu_test_"+uuid4().hex;self.db=self.client[self.name]
        self._cleaned=False;self.addAsyncCleanup(self.cleanup_owned)
        self.rows=[];stamp=SCOPE["season_start"]+100
        for label,mid,equipment in (("low",Int64(9007199254740992),"purple"),("tie-A",Int64(9007199254740993),None),("tie-Z",Int64(9007199254740993),None)):
            row=canonical(mid,stamp,equipment=equipment);row["_id"]=label;self.rows.append(certify(row))
        future_win=canonical(2000,CHECKED_AT+100);future_win["_id"]="future-win";self.rows.append(certify(future_win))
        future_loss=canonical(2001,CHECKED_AT+200,won=False);future_loss["_id"]="future-loss";self.rows.append(certify(future_loss))
        for label,flag in (("unknown-false",False),("unknown-null",None)):
            row=canonical(3000+len(self.rows),SCOPE["season_start"]+50);row["_id"]=label
            row["data"]["detail"]["match_time"]=[SCOPE["season_start"]+50]
            row.update({WIN:None,TIME:None,MID:None,VALID:flag});self.rows.append(row)
        await self.db.jjc_match_detail.insert_many(deepcopy(self.rows))
        await self.db.jjc_match_detail.create_index([(WIN,1),(TIME,-1),(MID,-1),("_id",1)],name=INDEX)
        for side in ("team1","team2"):
            await self.db.jjc_match_detail.create_index([("data.detail."+side+".players_info.global_id",1),(TIME,1),("_id",1)],name="idx_local_"+side+"_future")

    async def cleanup_owned(self):
        if self._cleaned:return
        self._cleaned=True
        try:await self.client.drop_database(self.name)
        finally:self.client.close()
    async def asyncTearDown(self):await self.cleanup_owned()

    def query(self,bounded=True):
        stamp={"$gte":SCOPE["season_start"]}
        if bounded:stamp["$lte"]=CHECKED_AT
        return {WIN:TARGET_GLOBAL_ID,TIME:stamp}

    async def picked(self,bounded=True):
        return await self.db.jjc_match_detail.find(self.query(bounded),{"_id":1}).sort(list(SORT.items())).limit(1).hint(INDEX).max_time_ms(1500).to_list(None)

    async def test_winner_max_is_covered_without_fetch_sort_and_keeps_int64_tie(self):
        plans=[]
        for bounded in (False,True):
            plan=await self.db.command("explain",{"find":"jjc_match_detail","filter":self.query(bounded),"projection":{"_id":1},"sort":SORT,"limit":1,"hint":INDEX,"maxTimeMS":1500},verbosity="executionStats")
            winning=str(plan["queryPlanner"]["winningPlan"])
            self.assertIn("PROJECTION_COVERED",winning);self.assertIn("IXSCAN",winning)
            self.assertNotIn("FETCH",winning);self.assertNotIn("'stage': 'SORT'",winning)
            self.assertEqual(0,plan["executionStats"]["totalDocsExamined"])
            plans.append({"bounded":bounded,"keys_examined":plan["executionStats"]["totalKeysExamined"],"docs_examined":0,"winning_plan":plan["queryPlanner"]["winningPlan"]})
        self.assertEqual([{"_id":"tie-A"}],await self.picked(True))
        picked=await self.db.jjc_match_detail.find_one({"_id":"tie-A"})
        self.assertEqual(Int64(9007199254740993),picked[MID])
        Path("/tmp/jjc-equipment-test-20261113/winner-index-plans-py%d%d.json"%sys.version_info[:2]).write_text(json.dumps(plans,indent=2,default=str))

    async def test_unbounded_and_bounded_query_characterize_future_separately(self):
        # The domain full-history state can already be data_inconsistent for
        # future facts. This observes source selection, not an output bug.
        self.assertEqual([{"_id":"future-win"}],await self.picked(False))
        self.assertEqual([{"_id":"tie-A"}],await self.picked(True))

    async def test_bounded_current_win_keeps_latest_missing_weapon_without_fallback(self):
        chosen=await self.picked(True)
        selected=await self.db.jjc_match_detail.find({"_id":{"$in":[r["_id"] for r in chosen]}}).to_list(None)
        canonical_current=await self.db.jjc_match_detail.find({TIME:{"$gte":SCOPE["season_start"],"$lte":CHECKED_AT},VALID:True}).to_list(None)
        self.assertEqual(validate(canonical_current),validate(selected))
        self.assertEqual(9007199254740993,validate(selected)["packet"]["match_id"])
        self.assertEqual("unknown",validate(selected)["packet"]["equipment_state"])

    async def test_unknown_and_future_anyside_include_future_loser_and_match_full_python_oracle(self):
        unknown=set();future=set()
        for side in ("team1","team2"):
            identity={"data.detail."+side+".players_info.global_id":TARGET_GLOBAL_ID}
            for key,predicate,output in (("unknown",{VALID:{"$in":[False,None]}},unknown),("future",{VALID:True,TIME:{"$gt":CHECKED_AT}},future)):
                rows=await self.db.jjc_match_detail.find({"$and":[identity,predicate]},{"_id":1}).hint("idx_local_"+side+"_future").max_time_ms(1500).to_list(None)
                output.update(r["_id"] for r in rows)
        self.assertEqual({"unknown-false","unknown-null"},unknown)
        self.assertEqual({"future-win","future-loss"},future)
        selected={r["_id"] for r in await self.picked(True)}|unknown|future
        self.assertIn("future-loss",selected)
        rows=await self.db.jjc_match_detail.find({"_id":{"$in":list(selected)}}).to_list(None)
        original=await self.db.jjc_match_detail.find(identity_filter([TARGET_GLOBAL_ID])).to_list(None)
        self.assertEqual(validate(original),validate(rows))

    async def test_winner_expected_time_and_mid_can_be_projected_without_fetch(self):
        projection={"_id":1,TIME:1,MID:1}
        plan=await self.db.command("explain",{"find":"jjc_match_detail","filter":self.query(True),"projection":projection,"sort":SORT,"limit":1,"hint":INDEX,"maxTimeMS":1500},verbosity="executionStats")
        winning=str(plan["queryPlanner"]["winningPlan"])
        self.assertIn("PROJECTION_COVERED",winning);self.assertIn("IXSCAN",winning)
        self.assertNotIn("FETCH",winning);self.assertNotIn("'stage': 'SORT'",winning)
        self.assertEqual(0,plan["executionStats"]["totalDocsExamined"])
        rows=await self.db.jjc_match_detail.find(self.query(True),projection).sort(list(SORT.items())).limit(1).hint(INDEX).max_time_ms(1500).to_list(None)
        self.assertEqual([{"_id":"tie-A",TIME:SCOPE["season_start"]+100,MID:Int64(9007199254740993)}],rows)
        # gid is recorded from the equality predicate, not returned as an array
        # projection which would defeat index covering.
        expected=(TARGET_GLOBAL_ID,rows[0][TIME],rows[0][MID])
        self.assertEqual(Int64(9007199254740993),expected[2])
