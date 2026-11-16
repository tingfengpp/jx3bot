"""DEP10 scope-independent positive source certificates and atomic writers."""
from copy import deepcopy
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4
from bson import BSON, Int64

from src.utils import jjc_equipment_time as policy
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.services.jx3.jjc_ranking_equipment import validate_local_win
from tests import test_jjc_candidate_certified_reduce_mongo as corpus
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, frozen_member
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_mongo import test_client

TIME="equipment_read_time_v1"
VALID="equipment_read_valid_v1"
WINNERS="equipment_winner_gids_v1"
MID="equipment_match_id_v1"
FIELDS={TIME,VALID,WINNERS,MID}
STAMP=SCOPE["season_start"]+100


def source_index(mid,data):
    function=getattr(policy,"equipment_source_index",None)
    if function is None:
        # Valid existing time-only policy provides business RED, never import failure.
        return {TIME:policy.equipment_read_time(data),VALID:False,WINNERS:[],MID:None}
    return function(mid,data)


def update_pipeline():
    function=getattr(policy,"build_equipment_source_update_pipeline",None)
    return function() if function else [{"$set":{TIME:policy.build_equipment_time_expression()}}]


def expected(row):
    return {TIME:STAMP,VALID:True,WINNERS:[
        player["global_id"] for player in row["data"]["detail"]["team1"]["players_info"]],MID:900}


def assert_valid_domain(test,row,meta,label=""):
    test.assertIs(meta[VALID],True,label)
    test.assertIsInstance(row["data"],dict,label)
    detail=row["data"].get("detail")
    test.assertIsInstance(detail,dict,label)
    test.assertEqual(row["match_id"],meta[MID],label)
    for side in ("team1","team2"):
        test.assertIsInstance(detail.get(side),dict,label)
        test.assertIs(type(detail[side].get("won")),bool,label)
        players=detail[side].get("players_info")
        test.assertIsInstance(players,list,label)
        test.assertEqual(3,len(players),label)
        if detail[side]["won"]:test.assertEqual([p.get("global_id") for p in players],meta[WINNERS],label)
        for player in players:
            test.assertIsInstance(player,dict,label)
            test.assertIn("global_id",player,label)
            test.assertIsInstance(player["global_id"],str,label)
            outcome=validate_local_win(member=frozen_member(global_id=player["global_id"]),
                candidates=[row],scope=SCOPE,checked_at=CHECKED_AT)
            if detail[side]["won"]:
                test.assertIn(outcome["state"],("available","partial"),label)
                test.assertEqual(row["match_id"],outcome["packet"]["match_id"],label)
            else:test.assertEqual("no_local_win",outcome["state"],label)


class EquipmentSourcePolicyTests(unittest.TestCase):
    def test_canonical_positive_four_fields_and_six_role_domain_semantics(self):
        row=corpus.canonical(900,STAMP);original=deepcopy(row)
        meta=source_index(row["match_id"],row["data"])
        self.assertEqual(expected(row),meta)
        self.assertEqual(FIELDS,set(meta))
        assert_valid_domain(self,row,meta)
        self.assertEqual(original,row)

    def test_missing_weapon_unknown_kungfu_and_second_team_winner_stay_positive(self):
        row=corpus.canonical(900,STAMP,equipment=None)
        row["data"]["detail"]["team1"]["players_info"][0]["kungfu_id"]=999999
        meta=source_index(900,row["data"])
        self.assertTrue(meta[VALID],"equipment availability and catalog are not certificate predicates")
        assert_valid_domain(self,row,meta)
        detail=row["data"]["detail"]
        detail["team1"]["won"]=False;detail["team2"]["won"]=True
        meta=source_index(900,row["data"])
        self.assertTrue(meta[VALID])
        self.assertEqual([p["global_id"] for p in detail["team2"]["players_info"]],meta[WINNERS])
        assert_valid_domain(self,row,meta)

    def test_nonnull_season_is_unknown_and_invalid_always_clears_three_structural_fields(self):
        for path in ("data.season_id","data.detail.season_id"):
            for value in (SCOPE["season_id"],"older",3,[],{}):
                row=corpus.canonical(900,STAMP)
                parent=row
                keys=path.split(".")
                for key in keys[:-1]:parent=parent[key]
                parent[keys[-1]]=value
                meta=source_index(900,row["data"])
                self.assertFalse(meta[VALID]);self.assertEqual([],meta[WINNERS]);self.assertIsNone(meta[MID])
                self.assertEqual(STAMP,meta[TIME])
        for mid in (None,True,0,-1,"900",900.0):
            meta=source_index(mid,corpus.canonical(900,STAMP)["data"])
            self.assertFalse(meta[VALID]);self.assertEqual([],meta[WINNERS]);self.assertIsNone(meta[MID])

    def test_unavailable_requires_exact_null_or_false_not_python_falsey_values(self):
        for value in (0,0.0,[],{},"",True,"false"):
            row=corpus.canonical(900,STAMP);row["data"]["unavailable"]=value
            meta=source_index(900,row["data"])
            self.assertFalse(meta[VALID],repr(value))
            self.assertEqual([],meta[WINNERS])
            self.assertIsNone(meta[MID])
        for value in (None,False):
            row=corpus.canonical(900,STAMP);row["data"]["unavailable"]=value
            self.assertTrue(source_index(900,row["data"])[VALID])

    def test_generated_bson_variants_never_create_false_positive_certificates(self):
        for label,mutate in corpus.variants():
            row=corpus.canonical(900,STAMP);mutate(row)
            with self.subTest(label=label):
                meta=source_index(row["match_id"],row["data"])
                self.assertEqual(FIELDS,set(meta))
                self.assertIsInstance(meta[VALID],bool)
                if meta[VALID]:assert_valid_domain(self,row,meta,label)
                else:
                    self.assertEqual([],meta[WINNERS],label)
                    self.assertIsNone(meta[MID],label)
                self.assertEqual(policy.equipment_read_time(row["data"]),meta[TIME])

    def test_conflicting_time_clears_all_four_and_future_time_remains_certified(self):
        row=corpus.canonical(900,STAMP);row["data"]["detail"]["match_time"]=STAMP+1
        self.assertEqual({TIME:None,VALID:False,WINNERS:[],MID:None},source_index(900,row["data"]))
        future=corpus.canonical(900,CHECKED_AT+100)
        self.assertTrue(source_index(900,future["data"])[VALID],"certificate is scope independent, reader handles future evidence")


class EquipmentSourceWriterTests(unittest.IsolatedAsyncioTestCase):
    def repo(self,fail_snapshot=False):
        collection=SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
        snapshots=SimpleNamespace(save_equipment_snapshot=AsyncMock(
            side_effect=RuntimeError("synthetic-source-snapshot-failure") if fail_snapshot else None),
            save_talent_snapshot=AsyncMock())
        return JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection),snapshot_repo=snapshots),collection

    async def test_raw_and_compact_writes_each_set_complete_source_index_atomically(self):
        row=corpus.canonical(900,STAMP);payload={"cached_at":1234567890,"data":row["data"]}
        original=deepcopy(payload);repo,collection=self.repo()
        await repo.save_match_detail("900",payload)
        self.assertEqual(2,collection.update_one.await_count)
        for call in collection.update_one.await_args_list:
            fields=call.args[1]["$set"]
            self.assertTrue(FIELDS.issubset(fields))
            self.assertEqual(expected(row),{key:fields[key] for key in FIELDS})
        compact=collection.update_one.await_args_list[1]
        self.assertEqual({"match_id":900,"cached_at":original["cached_at"],"data":original["data"]},compact.args[0])
        self.assertFalse(compact.kwargs["upsert"])
        self.assertEqual(original,payload)

    async def test_snapshot_failure_still_has_complete_raw_index(self):
        row=corpus.canonical(900,STAMP);repo,collection=self.repo(True)
        self.assertTrue(await repo.save_match_detail(900,{"data":row["data"]}))
        self.assertEqual(1,collection.update_one.await_count)
        fields=collection.update_one.await_args.args[1]["$set"]
        self.assertTrue(FIELDS.issubset(fields))
        self.assertEqual(expected(row),{key:fields[key] for key in FIELDS})

    async def test_later_invalid_structure_clears_structural_certificate_but_keeps_time(self):
        row=corpus.canonical(900,STAMP);repo,collection=self.repo()
        await repo.save_match_detail(900,{"data":row["data"]})
        collection.update_one.reset_mock();row["data"]["season_id"]="non-null-season"
        await repo.save_match_detail(900,{"data":row["data"]})
        for call in collection.update_one.await_args_list:
            fields=call.args[1]["$set"]
            self.assertTrue(FIELDS.issubset(fields))
            self.assertEqual({TIME:STAMP,VALID:False,WINNERS:[],MID:None},{key:fields[key] for key in FIELDS})

    async def test_later_conflict_overwrites_all_old_index_metadata(self):
        row=corpus.canonical(900,STAMP);repo,collection=self.repo()
        await repo.save_match_detail(900,{"data":row["data"]})
        collection.update_one.reset_mock();row["data"]["detail"]["match_time"]=STAMP+1
        await repo.save_match_detail(900,{"data":row["data"]})
        for call in collection.update_one.await_args_list:
            fields=call.args[1]["$set"]
            self.assertTrue(FIELDS.issubset(fields))
            self.assertEqual({TIME:None,VALID:False,WINNERS:[],MID:None},{key:fields[key] for key in FIELDS})


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class EquipmentSourcePipelineMongoTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_atomic_backfill_pipeline_matches_python_or_is_conservatively_unknown(self):
        uri=checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        name="jjc_kungfu_test_"+uuid4().hex;client=test_client(uri)
        try:
            info=await client.admin.command("buildInfo");self.assertTrue(info["version"].startswith("4.4."))
            rows=[corpus.canonical(900,STAMP),corpus.canonical(901,STAMP,equipment=None)]
            for label,mutate in corpus.variants():
                row=corpus.canonical(1000+len(rows),STAMP);mutate(row);rows.append(row)
            for index,row in enumerate(rows):row["_id"]=index
            rows.append({"_id":len(rows),"match_id":Int64(9007199254740993),
                "data":corpus.canonical(Int64(9007199254740993),STAMP)["data"]})
            raw={row["_id"]:BSON.encode({"data":row["data"]}) for row in rows}
            await client[name].source_rows.insert_many(rows)
            stages=update_pipeline()
            self.assertEqual(3,len(stages),"three ordered $set stages derive time, validity, then winner/mid atomically")
            self.assertTrue(all(set(stage)=={"$set"} for stage in stages))
            await client[name].source_rows.update_many({},stages)
            actual=[row async for row in client[name].source_rows.find({})]
            for row in actual:
                self.assertEqual(raw[row["_id"]],BSON.encode({"data":row["data"]}))
                self.assertTrue(FIELDS.issubset(row))
                expected_meta=source_index(row["match_id"],row["data"])
                self.assertEqual(expected_meta[TIME],row[TIME])
                if row[VALID]:
                    self.assertTrue(expected_meta[VALID])
                    self.assertEqual(expected_meta[WINNERS],row[WINNERS])
                    self.assertEqual(expected_meta[MID],row[MID])
                else:self.assertEqual([],row[WINNERS]);self.assertIsNone(row[MID])
            for row in actual[:2]:self.assertTrue(row[VALID],"canonical and missing-equipment rows must be usefully certified")
        finally:
            checked_test_uri(uri);await client.drop_database(name);client.close()


if __name__=="__main__":unittest.main()
