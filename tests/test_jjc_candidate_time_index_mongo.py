"""DEP09 fixed scalar-time index feasibility, before time utility exists.

Unknown raw aliases are represented by a null technical certificate. These
fixtures do not certify arbitrary user-written technical field contents.
"""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
from uuid import uuid4
import unittest
from bson import Decimal128
from tests.test_jjc_candidate_certified_reduce_mongo import canonical, validate, identity_filter
from tests.test_jjc_ranking_equipment_mongo import test_client
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID

FIELD="equipment_read_time_v1"


def enumeration(side, variant="nor"):
    identity={"data.detail."+side+".players_info.global_id":{"$in":[TARGET_GLOBAL_ID]}}
    old={"$gt":0,"$lt":SCOPE["season_start"]}
    if variant=="nor": return {"$and":[identity,{"$nor":[{FIELD:old}]}]}
    return {"$and":[identity,{FIELD:{"$not":old}}]}


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback required")
class ScalarTimeCoveredIndexMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=test_client(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]))
        self.name="jjc_kungfu_test_"+uuid4().hex;self.db=self.client[self.name]
        self._cleaned=False
        self.addAsyncCleanup(self.cleanup_owned)
        self.rows=[];self.labels={}
        start=SCOPE["season_start"]
        def add(label,stamp,certificate="null",mutate=None):
            row=canonical(8000+len(self.rows),stamp)
            row["_id"]=label
            if certificate!="missing":row[FIELD]=None if certificate=="null" else certificate
            if mutate:mutate(row)
            self.rows.append(row);self.labels[label]=row
        add("missing",start+10,"missing")
        add("null",start+11)
        add("old-positive",start-1,start-1)
        add("current",start+20,start+20)
        add("future",CHECKED_AT+10,CHECKED_AT+10)
        add("zero",0,0)
        add("negative",-1,-1)
        add("decimal-raw",start-2,mutate=lambda r:r["data"].update(match_time=Decimal128(str(start-2))))
        add("array-raw",start+30,mutate=lambda r:r["data"]["detail"].update(match_time=[start+30]))
        add("nan-raw",start+31,mutate=lambda r:r["data"].update(match_time=float("nan")))
        add("inf-raw",start+32,mutate=lambda r:r["data"].update(match_time=float("inf")))
        add("bool-raw",start+33,mutate=lambda r:r["data"].update(match_time=True))
        add("data-ancestor-array",start+34,mutate=lambda r:r.update(data=[deepcopy(r["data"])]))
        add("detail-ancestor-array",start+35,mutate=lambda r:r["data"].update(detail=[deepcopy(r["data"]["detail"])]))
        await self.db.jjc_match_detail.insert_many(deepcopy(self.rows))
        self.names={}
        for side in ("team1","team2"):
            name="idx_equipment_"+side+"_global_time_source"
            self.names[side]=name
            await self.db.jjc_match_detail.create_index([("data.detail."+side+".players_info.global_id",1),(FIELD,1),("_id",1)],name=name)

    async def cleanup_owned(self):
        if self._cleaned:return
        self._cleaned=True
        try:await self.client.drop_database(self.name)
        finally:self.client.close()

    async def asyncTearDown(self):
        await self.cleanup_owned()

    async def test_scalar_certificate_negative_range_is_conservative_but_not_covered_on_44(self):
        results=[]
        for variant in ("nor","not"):
            for side in ("team1","team2"):
                for projection in ({"_id":1},{"_id":1,FIELD:1}):
                    query=enumeration(side,variant)
                    plan=await self.db.command("explain",{"find":"jjc_match_detail","filter":query,"projection":projection,"hint":self.names[side]},verbosity="executionStats")
                    winning=str(plan["queryPlanner"]["winningPlan"])
                    docs=plan["executionStats"]["totalDocsExamined"]
                    covered="PROJECTION_COVERED" in winning and "IXSCAN" in winning and "FETCH" not in winning and docs==0
                    found=await self.db.jjc_match_detail.find(query,projection).hint(self.names[side]).max_time_ms(1500).to_list(None)
                    ids={row["_id"] for row in found}
                    expected=set(self.labels)-{"old-positive"} if side=="team1" else set()
                    self.assertEqual(expected,ids)
                    results.append({"variant":variant,"side":side,"projection":projection,"covered":covered,"docs_examined":docs,"winning_plan":plan["queryPlanner"]["winningPlan"],"ids":sorted(ids)})
        path=Path("/tmp/jjc-equipment-test-20261113")/("time-index-feasibility-py%d%d.json"%sys.version_info[:2])
        path.write_text(json.dumps(results,indent=2,default=str))
        self.assertFalse(any(r["covered"] for r in results), "fixed nor/not predicates require FETCH on the tested4.4 planner")

    async def test_pruning_only_positive_certified_old_preserves_python_oracle_and_unknown_ancestors(self):
        original=await self.db.jjc_match_detail.find(identity_filter([TARGET_GLOBAL_ID])).to_list(None)
        selected=await self.db.jjc_match_detail.find(enumeration("team1"),{"_id":1}).hint(self.names["team1"]).to_list(None)
        ids=[row["_id"] for row in selected]
        remaining=await self.db.jjc_match_detail.find({"_id":{"$in":ids}}).to_list(None)
        self.assertEqual(len(original)-1,len(remaining))
        self.assertIn("data-ancestor-array",ids)
        self.assertIn("detail-ancestor-array",ids)
        self.assertIn("array-raw",ids)
        self.assertIn("decimal-raw",ids)
        self.assertEqual(validate(original),validate(remaining))

    async def test_index_only_metadata_then_strict_python_pruning_preserves_all_unknowns(self):
        discovered=[]
        for side in ("team1","team2"):
            query={"data.detail."+side+".players_info.global_id":{"$in":[TARGET_GLOBAL_ID]}}
            projection={"_id":1,FIELD:1}
            plan=await self.db.command("explain",{"find":"jjc_match_detail","filter":query,"projection":projection,"hint":self.names[side]},verbosity="executionStats")
            winning=str(plan["queryPlanner"]["winningPlan"])
            self.assertIn("PROJECTION_COVERED",winning)
            self.assertIn("IXSCAN",winning)
            self.assertNotIn("FETCH",winning)
            self.assertEqual(0,plan["executionStats"]["totalDocsExamined"])
            discovered.extend(await self.db.jjc_match_detail.find(query,projection).hint(self.names[side]).max_time_ms(1500).to_list(None))
        kept=set()
        for row in discovered:
            value=row.get(FIELD)
            known_old=(isinstance(value,(int,float)) and not isinstance(value,bool)
                and math.isfinite(value) and 0<value<SCOPE["season_start"])
            if not known_old:kept.add(row["_id"])
        self.assertEqual(set(self.labels)-{"old-positive"},kept)
        original=await self.db.jjc_match_detail.find(identity_filter([TARGET_GLOBAL_ID])).to_list(None)
        remaining=await self.db.jjc_match_detail.find({"_id":{"$in":list(kept)}}).to_list(None)
        self.assertEqual(validate(original),validate(remaining))

    async def test_separate_current_null_and_nonpositive_queries_avoid_old_positive_fetch(self):
        split=[];merged=set()
        for side in ("team1","team2"):
            identity={"data.detail."+side+".players_info.global_id":{"$in":[TARGET_GLOBAL_ID]}}
            for label,predicate in (("current",{FIELD:{"$gte":SCOPE["season_start"]}}),
                                    ("unknown",{FIELD:None}), ("invalid_nonpositive",{FIELD:{"$lte":0}})):
                query={"$and":[identity,predicate]}
                projection={"_id":1}
                plan=await self.db.command("explain",{"find":"jjc_match_detail","filter":query,"projection":projection,"hint":self.names[side]},verbosity="executionStats")
                rows=await self.db.jjc_match_detail.find(query,projection).hint(self.names[side]).max_time_ms(1500).to_list(None)
                ids={row["_id"] for row in rows};merged.update(ids)
                winning=str(plan["queryPlanner"]["winningPlan"])
                stats=plan["executionStats"]
                if label!="unknown":
                    self.assertIn("PROJECTION_COVERED",winning)
                    self.assertNotIn("FETCH",winning)
                    self.assertEqual(0,stats["totalDocsExamined"])
                else:
                    self.assertIn("IXSCAN",winning)
                    self.assertIn("FETCH",winning)
                    self.assertEqual(len(ids),stats["totalDocsExamined"],"only required null/missing documents should be fetched")
                self.assertNotIn("old-positive",ids)
                split.append({"side":side,"branch":label,"ids":sorted(ids),
                    "keys_examined":stats["totalKeysExamined"],"docs_examined":stats["totalDocsExamined"],
                    "winning_plan":plan["queryPlanner"]["winningPlan"]})
        self.assertEqual(set(self.labels)-{"old-positive"},merged)
        # Under the controlled positive-finite-or-null schema, current+unknown
        # alone is the exact population. Nonpositive branch is optional defense
        # for intentionally corrupted scalar0/-1 fixtures, never valid output.
        valid_schema=set(self.labels)-{"old-positive","zero","negative"}
        self.assertEqual(valid_schema,set().union(*(set(r["ids"]) for r in split if r["branch"]!="invalid_nonpositive")))
        original=await self.db.jjc_match_detail.find(identity_filter([TARGET_GLOBAL_ID])).to_list(None)
        remaining=await self.db.jjc_match_detail.find({"_id":{"$in":list(merged)}}).to_list(None)
        self.assertEqual(validate(original),validate(remaining))
        Path("/tmp/jjc-equipment-test-20261113/time-index-split-branches-py%d%d.json"%sys.version_info[:2]).write_text(json.dumps(split,indent=2,default=str))
