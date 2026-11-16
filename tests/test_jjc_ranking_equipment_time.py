"""DEP09 pure time certificate and atomic existing-writer contracts."""
from copy import deepcopy
import importlib
import importlib.util
import os
from uuid import uuid4
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bson import Decimal128, Int64
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from tests.jjc_equipment_fixtures import SCOPE, match_doc
from tests.test_jjc_candidate_fast_prune_mongo import TIME_PATHS, set_path, previous_proven_old
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_mongo import test_client

STAMP=SCOPE["season_start"]-10


def module():
    name="src.utils.jjc_equipment_time"
    return importlib.import_module(name) if importlib.util.find_spec(name) is not None else None


def read_time(data):
    api=module()
    return api.equipment_read_time(data) if api is not None else None


def data():
    return {"match_time":STAMP,"detail":{"match_time":float(STAMP),"basic_info":{"start_time":Int64(STAMP)}}}


class EquipmentTimePolicyTests(unittest.TestCase):
    def test_required_apis_and_positive_canonical_shapes(self):
        api=module()
        self.assertIsNotNone(api,"DEP09 requires executable time-certificate policy")
        self.assertTrue(callable(getattr(api,"equipment_read_time",None)))
        self.assertIsInstance(api.build_equipment_time_expression(),dict)

    def test_every_alias_and_null_missing_optional_fields(self):
        for path in TIME_PATHS:
            row={"data":{"detail":{}}};set_path(row,path,STAMP)
            with self.subTest(path=path):self.assertEqual(STAMP,read_time(row["data"]))
        self.assertEqual(STAMP,read_time(data()))
        for basic in (None,{}):
            self.assertEqual(STAMP,read_time({"match_time":STAMP,"detail":{"basic_info":basic}}))
        null_alias=data();null_alias["matchTime"]=None
        self.assertEqual(STAMP,read_time(null_alias))
        self.assertIsNone(read_time({"detail":{}}))

    def test_non_numeric_nonfinite_conflict_and_bad_ancestors_are_unknown(self):
        for path in TIME_PATHS:
            for value in (True,False,str(STAMP),Decimal128(str(STAMP)),[STAMP],[],{},0,-1,
                          float("nan"),float("inf"),float("-inf"),STAMP+1):
                row={"data":data()};set_path(row,path,value)
                with self.subTest(path=path,value=repr(value)):self.assertIsNone(read_time(row["data"]))
        for row in (None,[],{"detail":[]},{"detail":"bad"},{"detail":{"basic_info":[]}},
                    {"detail":{"basic_info":"bad"}},{"detail":None},{}):
            with self.subTest(row=row):self.assertIsNone(read_time(row))

    def test_no_input_mutation_or_integer_precision_loss(self):
        for value in (Int64(9007199254740993),Int64(9223372036854775807),1e300):
            row={"match_time":value,"detail":{}}
            original=deepcopy(row)
            self.assertEqual(value,read_time(row))
            self.assertEqual(original,row)


class EquipmentTimeAtomicWriterTests(unittest.IsolatedAsyncioTestCase):
    def make(self,fail_snapshot=False):
        collection=SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
        snapshots=SimpleNamespace(save_equipment_snapshot=AsyncMock(
            side_effect=RuntimeError("synthetic-snapshot-failure") if fail_snapshot else None),
            save_talent_snapshot=AsyncMock())
        return JjcInspectRepo(db=SimpleNamespace(jjc_match_detail=collection),snapshot_repo=snapshots),collection

    def payload(self):
        row=match_doc(900,when=STAMP)
        row["data"]["detail"]["match_time"]=STAMP
        row["data"]["detail"]["basic_info"]["start_time"]=STAMP
        return {"cached_at":1234567890.0,"data":row["data"]}

    async def test_raw_and_compact_each_atomically_set_time_and_preserve_cas(self):
        repo,collection=self.make();payload=self.payload();original=deepcopy(payload)
        self.assertTrue(await repo.save_match_detail(900,payload))
        self.assertEqual(2,collection.update_one.await_count)
        raw,compact=collection.update_one.await_args_list
        for call in (raw,compact):
            self.assertIn("data",call.args[1]["$set"])
            self.assertEqual(STAMP,call.args[1]["$set"].get("equipment_read_time_v1"))
        self.assertEqual({"match_id":900},raw.args[0]);self.assertTrue(raw.kwargs["upsert"])
        self.assertEqual({"match_id":900,"cached_at":payload["cached_at"],"data":original["data"]},compact.args[0])
        self.assertFalse(compact.kwargs["upsert"])
        self.assertIn("armors",raw.args[1]["$set"]["data"]["detail"]["team1"]["players_info"][0])
        self.assertNotIn("armors",compact.args[1]["$set"]["data"]["detail"]["team1"]["players_info"][0])
        self.assertEqual(original,payload)

    async def test_snapshot_failure_leaves_raw_time_certificate(self):
        repo,collection=self.make(fail_snapshot=True)
        self.assertTrue(await repo.save_match_detail(900,self.payload()))
        self.assertEqual(1,collection.update_one.await_count)
        stored=collection.update_one.await_args.args[1]["$set"]
        self.assertEqual(STAMP,stored.get("equipment_read_time_v1"))
        self.assertIn("armors",stored["data"]["detail"]["team1"]["players_info"][0])

    async def test_new_conflicting_evidence_explicitly_clears_old_certificate(self):
        repo,collection=self.make()
        await repo.save_match_detail(900,self.payload())
        collection.update_one.reset_mock()
        changed=self.payload();changed["data"]["detail"]["match_time"]=STAMP+1
        await repo.save_match_detail(900,changed)
        self.assertEqual(2,collection.update_one.await_count)
        for call in collection.update_one.await_args_list:
            fields=call.args[1]["$set"]
            self.assertIn("equipment_read_time_v1",fields,"unknown must overwrite a previously certified timestamp")
            self.assertIsNone(fields["equipment_read_time_v1"])

    async def test_unavailable_primary_write_still_sets_certificate_without_snapshots(self):
        repo,collection=self.make()
        payload=self.payload();payload["data"]["unavailable"]=True
        await repo.save_match_detail(900,payload)
        self.assertEqual(1,collection.update_one.await_count)
        self.assertEqual(STAMP,collection.update_one.await_args.args[1]["$set"].get("equipment_read_time_v1"))
        repo.snapshot_repo.save_equipment_snapshot.assert_not_awaited()


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class EquipmentTimeExpressionMongoTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_expression_is_conservative_and_old_certificates_imply_original_proof(self):
        uri=checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        name="jjc_kungfu_test_"+uuid4().hex;client=test_client(uri)
        try:
            info=await client.admin.command("buildInfo");self.assertTrue(info["version"].startswith("4.4."))
            db=client[name]
            values=[data(),{"detail":{"basic_info":{"start_time":STAMP}}},{"match_time":STAMP,"detail":{}}]
            for path in TIME_PATHS:
                for value in (None,True,False,str(STAMP),Decimal128(str(STAMP)),[STAMP],[],{},0,-1,
                              float("nan"),float("inf"),float("-inf"),STAMP+1):
                    row={"data":data()};set_path(row,path,value);values.append(row["data"])
            values.extend((None,[],{"detail":[]},{"match_time":STAMP,"detail":{"basic_info":[]}},
                           {"detail":None},{"detail":{}},{"match_time":Int64(9007199254740993),"detail":{}},
                           {"match_time":Int64(9223372036854775807),"detail":{}},{"match_time":1e300,"detail":{}}))
            await db.time_sources.insert_many([{"_id":index,"data":value} for index,value in enumerate(values)])
            api=module();expression=api.build_equipment_time_expression() if api is not None else {"$literal":None}
            actual=[row async for row in db.time_sources.aggregate([{"$project":{
                "certificate":expression,"old":previous_proven_old()}}])]
            self.assertEqual(len(values),len(actual))
            by_id={row["_id"]:row for row in actual}
            for index,value in enumerate(values):
                expected=read_time(value);observed=by_id[index].get("certificate")
                if observed is not None:
                    self.assertIsNotNone(expected,index)
                    self.assertEqual(expected,observed,index)
                    if observed<SCOPE["season_start"]:self.assertTrue(by_id[index]["old"],index)
                elif expected is None:self.assertIsNone(observed)
            for index in (0,1,2):
                self.assertEqual(STAMP,by_id[index].get("certificate"),
                    "the common canonical shapes must actually produce useful certificates")
        finally:
            checked_test_uri(uri)
            await client.drop_database(name);client.close()


if __name__=="__main__":unittest.main()
