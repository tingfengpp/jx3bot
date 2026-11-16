"""Two-stage physical source discovery/chunks; owned Mongo4.4 only."""
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
import unittest
from bson import BSON, ObjectId
from src.utils.jjc_equipment_candidates import build_local_win_candidate_pipeline as factory
from tests.test_jjc_candidate_certified_reduce_mongo import canonical, validate, identity_filter
from tests.test_jjc_ranking_equipment_mongo import test_client
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID


def build(ids, **kwargs):
    return factory(ids, season_start=SCOPE["season_start"], checked_at=CHECKED_AT,
        season_id=SCOPE["season_id"], **kwargs)


def require_source_support(test):
    test.assertIn("source_ids", inspect.signature(factory).parameters,
        "utility must support bounded physical source chunks without dropping any target gid")


class SourceChunkUtilityContracts(unittest.TestCase):
    def test_source_chunk_first_match_keeps_identity_and_old_filters(self):
        require_source_support(self)
        ids = [TARGET_GLOBAL_ID]
        source_ids = [ObjectId(), "physical-string", 42, {"physical": "composite"}]
        original = build(ids)
        actual = build(ids, source_ids=source_ids)
        query = actual[0]["$match"]
        def contains(value, expected):
            return value == expected or (isinstance(value, dict) and any(contains(v,expected) for v in value.values())) or (isinstance(value,list) and any(contains(v,expected) for v in value))
        self.assertTrue(contains(query, {"_id": {"$in": source_ids}}))
        for clause in original[0]["$match"]["$and"]:
            self.assertTrue(contains(query, clause), "source restriction must preserve identity and conservative old guards")
        self.assertEqual(original[1:], actual[1:])

    def test_all_thousand_targets_are_supported_only_with_bounded_source_ids(self):
        require_source_support(self)
        ids = [str(int(TARGET_GLOBAL_ID)+i) for i in range(1000)]
        build(ids, source_ids=[ObjectId()])
        with self.assertRaises(ValueError): build(ids)
        with self.assertRaises(ValueError): build(ids+[str(int(ids[-1])+1)], source_ids=[ObjectId()])

    def test_source_chunk_cardinality_and_array_ids_are_rejected(self):
        require_source_support(self)
        for source_ids in ([], list(range(257)), "not-a-list", [[1]], [ObjectId(), []]):
            with self.subTest(kind=type(source_ids).__name__, length=len(source_ids)):
                with self.assertRaises(ValueError): build([TARGET_GLOBAL_ID], source_ids=source_ids)

    def test_legacy_no_source_path_still_accepts_one_hundred_and_rejects_101(self):
        ids = [str(int(TARGET_GLOBAL_ID)+i) for i in range(101)]
        build(ids[:100])
        with self.assertRaises(ValueError): build(ids)


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated loopback URI required")
class SourceDiscoveryAndChunkMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = test_client(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]))
        self.name = "jjc_kungfu_test_"+uuid4().hex
        self.db = self.client[self.name]
        self.assertTrue((await self.client.admin.command("buildInfo"))["version"].startswith("4.4."))
        start = SCOPE["season_start"]
        a = canonical(101,start+100);a["_id"]="source-a"
        b = canonical(301,start+300,equipment=None);b["_id"]="source-b"
        c = canonical(201,start+200);c["_id"]="source-c"
        for side in ("team1","team2"):
            for index,player in enumerate(c["data"]["detail"][side]["players_info"]):
                player["global_id"] = str(int(TARGET_GLOBAL_ID)+700+index+(3 if side=="team2" else 0))
        loss = canonical(401,start+400,won=False);loss["_id"]="source-loss"
        u = canonical(999,start+50);u["_id"]="uncertain-same-mid-a"
        u["data"]["detail"]["match_time"]=[start+50]
        v = deepcopy(u);v["_id"]="uncertain-same-mid-b"
        v["data"]["detail"]["match_id"]=[999]
        self.rows=[a,b,c,loss,u,v]
        await self.db.jjc_match_detail.insert_many(deepcopy(self.rows))
        self.ids=[str(int(TARGET_GLOBAL_ID)+i) for i in range(1000)]

    async def asyncTearDown(self):
        await self.client.drop_database(self.name)
        self.client.close()

    async def test_side_compound_indexes_cover_id_discovery_and_union_is_exact(self):
        discovered=[];metrics=[]
        for side in ("team1","team2"):
            path="data.detail."+side+".players_info.global_id"
            name="idx_equipment_"+side+"_global_source"
            started=time.perf_counter()
            await self.db.jjc_match_detail.create_index([(path,1),("_id",1)],name=name)
            elapsed=time.perf_counter()-started
            query={path:{"$in":self.ids}}
            explain=await self.db.command("explain",{"find":"jjc_match_detail","filter":query,"projection":{"_id":1},"hint":name},verbosity="executionStats")
            plan=str(explain["queryPlanner"]["winningPlan"])
            self.assertIn("PROJECTION_COVERED",plan)
            self.assertIn("IXSCAN",plan)
            self.assertNotIn("FETCH",plan)
            self.assertEqual(0,explain["executionStats"]["totalDocsExamined"])
            found=await self.db.jjc_match_detail.find(query,{"_id":1}).hint(name).max_time_ms(1500).to_list(None)
            discovered.extend(row["_id"] for row in found)
            stats=await self.db.command("collStats","jjc_match_detail")
            metrics.append({"index":name,"build_seconds":elapsed,"index_bytes":stats["indexSizes"][name],
                "keys_examined":explain["executionStats"]["totalKeysExamined"],"docs_examined":0})
        original=await self.db.jjc_match_detail.find(identity_filter(self.ids),{"_id":1}).to_list(None)
        unique=list(dict.fromkeys(discovered))
        self.assertEqual({row["_id"] for row in original},set(unique))
        self.assertEqual(len(original),len(unique))
        self.assertGreater(len(discovered),len(unique),"same physical source must be deduped across both sides")
        evidence=Path("/tmp/jjc-equipment-test-20261113")/('source-index-reference-py%d%d.json'%sys.version_info[:2])
        evidence.write_text(json.dumps({"synthetic_documents":len(self.rows),"reference_only":True,"metrics":metrics},indent=2))

    async def test_source_chunks_preserve_all_target_local_maxima_then_python_global_result(self):
        require_source_support(self)
        original=await self.db.jjc_match_detail.find(identity_filter(self.ids)).to_list(None)
        chunks=[[row["_id"] for row in self.rows[index:index+2]] for index in range(0,len(self.rows),2)]
        merged=[]
        for sources in chunks:
            rows=await self.db.jjc_match_detail.aggregate(build(self.ids,source_ids=sources),maxTimeMS=1500).to_list(None)
            self.assertTrue({row["_id"] for row in rows} <= set(sources))
            merged.extend(rows)
        self.assertEqual(len(merged),len({row["_id"] for row in merged}))
        self.assertTrue({"uncertain-same-mid-a","uncertain-same-mid-b"} <= {row["_id"] for row in merged})
        for gid in self.ids: self.assertEqual(validate(original,gid),validate(merged,gid),gid)
        remote_gid=str(int(TARGET_GLOBAL_ID)+700)
        self.assertEqual(201,validate(merged,remote_gid)["packet"]["match_id"],"source chunks must keep roles beyond the first100-target batch")
        self.assertEqual("data_inconsistent",validate(merged)["state"])

    async def test_composite_object_id_can_be_restricted_without_array_id_support(self):
        require_source_support(self)
        row=canonical(601,SCOPE["season_start"]+600)
        row["_id"]={"physical":"composite"}
        await self.db.jjc_match_detail.insert_one(row)
        found=await self.db.jjc_match_detail.aggregate(build([TARGET_GLOBAL_ID],source_ids=[row["_id"]]),maxTimeMS=1500).to_list(None)
        self.assertEqual([row["_id"]],[value["_id"] for value in found])

    async def test_canonical_source_chunks_keep_newest_missing_weapon_without_history_fallback(self):
        require_source_support(self)
        await self.db.jjc_match_detail.delete_many({"_id":{"$in":["uncertain-same-mid-a","uncertain-same-mid-b"]}})
        original=await self.db.jjc_match_detail.find(identity_filter(self.ids)).to_list(None)
        merged=[]
        for chunk in (["source-a","source-c"],["source-b","source-loss"]):
            merged.extend(await self.db.jjc_match_detail.aggregate(build(self.ids,source_ids=chunk),maxTimeMS=1500).to_list(None))
        for gid in self.ids: self.assertEqual(validate(original,gid),validate(merged,gid),gid)
        self.assertEqual(301,validate(merged)["packet"]["match_id"])
        self.assertEqual("unknown",validate(merged)["packet"]["equipment_state"])
