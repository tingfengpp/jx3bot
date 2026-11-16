"""DEP03: differential Mongo4.4 proof for redundant canonical-old fast pruning."""
from copy import deepcopy
import os
import unittest
from uuid import uuid4

from bson import Decimal128, Int64
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID, match_doc
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_mongo import Commands, test_client, create_indexes

TIME_PATHS = [prefix+"."+key for prefix in ("data", "data.detail", "data.detail.basic_info")
              for key in ("match_time", "matchTime", "start_time", "startTime", "time")]


def previous_proven_old():
    """Frozen pre-optimization expression is the differential reference, not product code."""
    times={"$filter":{"input":[{"$ifNull":["$"+path,None]} for path in TIME_PATHS],
                     "as":"stamp","cond":{"$ne":["$$stamp",None]}}}
    first={"$arrayElemAt":["$$times",0]}
    return {"$let":{"vars":{"times":times},"in":{"$and":[
        {"$gt":[{"$size":"$$times"},0]}, {"$gt":[first,0]}, {"$lt":[first,SCOPE["season_start"]]},
        {"$allElementsTrue":[{"$map":{"input":"$$times","as":"stamp","in":{"$and":[
            {"$in":[{"$type":"$$stamp"},["int","long","double"]]},
            {"$eq":["$$stamp",first]}]}}}]}]}}}


def set_path(document,path,value):
    parent=document
    for key in path.split(".")[:-1]:
        parent=parent.setdefault(key,{})
    parent[path.split(".")[-1]]=value


def ordinary_nor_parts(query):
    found=[]
    if isinstance(query,dict):
        for key,value in query.items():
            if key=="$nor":found.extend(value)
            found.extend(ordinary_nor_parts(value))
    elif isinstance(query,list):
        for value in query:found.extend(ordinary_nor_parts(value))
    return found


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class CandidateFastPruneMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri=checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        self.name="jjc_kungfu_test_"+uuid4().hex
        self.recorder=Commands();self.client=test_client(self.uri,self.recorder)
        info=await self.client.admin.command("buildInfo")
        self.assertTrue(info["version"].startswith("4.4."))
        self.db=self.client[self.name]
        await create_indexes(self.db)
        self.labels={};self.fast_expected=set();rows=[]
        old=SCOPE["season_start"]-1
        def add(label,value=old,*,alias=None,alias_value=None,fast=False,missing=False):
            mid=70000000+len(rows)
            row=match_doc(mid,when=old)
            if missing:row["data"].pop("match_time")
            else:row["data"]["match_time"]=value
            if alias:set_path(row,alias,alias_value)
            rows.append(row);self.labels[label]=mid
            if fast:self.fast_expected.add(mid)
        for label,value in (("old_int",old),("old_long",Int64(old)),("old_double",float(old))):
            add(label,value,fast=True)
        # A query $type matches elements of arrays: these must not enter fastOld.
        values=(("null",None),("bool",True),("string",str(old)),("array",[old]),
                ("nested_array",[[old]]),("mixed_array",[old,"x"]),("empty_array",[]),
                ("decimal_old",Decimal128(str(old))),("decimal_nan",Decimal128("NaN")),
                ("decimal_inf",Decimal128("Infinity")),("decimal_negative_inf",Decimal128("-Infinity")),
                ("nan",float("nan")),("inf",float("inf")),("negative_inf",float("-inf")),
                ("zero",0),("negative",-1),("boundary",SCOPE["season_start"]),
                ("future",CHECKED_AT+100),("object",{"value":old}))
        for label,value in values:add(label,value)
        add("missing",missing=True)
        # Even null aliases disqualify the fast path; the old expression handles them.
        alias_values=(None,old,float(old),CHECKED_AT+1,[old],str(old),Decimal128(str(old)),float("nan"))
        for path in TIME_PATHS[1:]:
            for index,value in enumerate(alias_values):
                add("alias:"+path+":"+str(index),alias=path,alias_value=value)
        # Dotted query traversal and aggregation traversal differ for ancestor arrays.
        # A leaf $not:$type=array does not prove that $data.match_time is scalar.
        for path in ("data","data.detail","data.detail.basic_info"):
            mid=70000000+len(rows)
            row=match_doc(mid,when=old)
            parent=row
            keys=path.split(".")
            for key in keys[:-1]:parent=parent[key]
            parent[keys[-1]]=[deepcopy(parent[keys[-1]])]
            rows.append(row);self.labels["ancestor_array:"+path]=mid
        for path in ("data.detail","data.detail.basic_info"):
            for value in ([],[None]):
                mid=70000000+len(rows)
                row=match_doc(mid,when=old)
                set_path(row,path,value)
                rows.append(row);self.labels["ancestor_empty:"+path+":"+repr(value)]=mid
        # Real storage shapes: older writers used only basic_info.start_time;
        # other writers duplicated the same timestamp across three locations.
        for label in ("basic_only_old","three_copies_old"):
            mid=70000000+len(rows);row=match_doc(mid,when=old)
            if label=="basic_only_old":row["data"].pop("match_time")
            else:row["data"]["detail"]["match_time"]=old
            row["data"]["detail"]["basic_info"]["start_time"]=old
            rows.append(row);self.labels[label]=mid
        mid=70000000+len(rows);row=match_doc(mid,when=old)
        row["data"]["detail"]["match_time"]=old-1
        row["data"]["detail"]["basic_info"]["start_time"]=old-2
        rows.append(row);self.labels["three_copies_conflicting_old"]=mid
        # Identity scope remains mandatory even for malformed/current rows.
        outsider=match_doc(79999999,when=CHECKED_AT)
        for side in ("team1","team2"):
            for player in outsider["data"]["detail"][side]["players_info"]:
                player["global_id"]="899999999999999999"
        rows.append(outsider)
        await self.db.jjc_match_detail.insert_many(rows)
        self.identity={"$or":[{"data.detail."+side+".players_info.global_id":{"$in":[TARGET_GLOBAL_ID]}}
                              for side in ("team1","team2")]}
        self.original={"$and":[self.identity,{"$expr":{"$not":[previous_proven_old()]}}]}
        from src.utils.jjc_equipment_candidates import build_local_win_candidate_pipeline
        stages=build_local_win_candidate_pipeline([TARGET_GLOBAL_ID],
            season_start=SCOPE["season_start"],checked_at=CHECKED_AT)
        self.found=[row async for row in self.db.jjc_match_detail.aggregate(stages,maxTimeMS=1500)]
        self.query=stages[0]["$match"]

    async def asyncTearDown(self):
        checked_test_uri(self.uri)
        self.assertTrue(self.name.startswith("jjc_kungfu_test_"))
        await self.client.drop_database(self.name)
        self.client.close()

    async def ids(self,query):
        return {row["match_id"] async for row in self.db.jjc_match_detail.find(query,{"match_id":1})}

    def fast(self):
        parts=ordinary_nor_parts(self.query)
        self.assertTrue(parts,"DEP03 requires a redundant ordinary-query $nor fastOld guard")
        self.assertEqual(1,len(parts),"one explicit fastOld condition must be independently provable")
        self.assertNotIn("$expr",repr(parts[0]),"fastOld must use ordinary query predicates")
        return parts[0]

    async def test_fast_guard_is_present_and_matches_only_canonical_scalar_old_rows(self):
        fast=self.fast()
        self.assertTrue(self.fast_expected.issubset(await self.ids(fast)),
            "common canonical old shapes must enter the cheap fast guard")
        self.assertIn("$expr",repr(self.query),"retain the full original proven_old guard")
        explain=await self.db.command({"explain":{"find":"jjc_match_detail","filter":self.query},
                                      "verbosity":"executionStats"})
        plan=repr(explain["queryPlanner"]["winningPlan"])
        self.assertIn("IXSCAN",plan)
        self.assertNotIn("COLLSCAN",plan)

    async def test_fast_old_implies_original_proven_old_on_every_bson_counterexample(self):
        fast=self.fast()
        counterexamples=await self.ids({"$and":[fast,{"$expr":{"$not":[previous_proven_old()]}}]})
        self.assertEqual(set(),counterexamples,"fast guard must never remove old-expression survivors")

    async def test_real_repo_results_are_identical_to_previous_expression(self):
        self.assertEqual(await self.ids(self.original),{row["match_id"] for row in self.found})
        # The optimization is mandatory, not a vacuous equivalence check of unchanged code.
        self.fast()
        for label in ("array","nested_array","mixed_array","empty_array","null","bool","string",
                      "decimal_old","decimal_nan","decimal_inf","decimal_negative_inf","nan","inf","negative_inf","zero","negative",
                      "boundary","future","object","missing","three_copies_conflicting_old"):
            self.assertIn(self.labels[label],{row["match_id"] for row in self.found},label)


if __name__=="__main__":unittest.main()
