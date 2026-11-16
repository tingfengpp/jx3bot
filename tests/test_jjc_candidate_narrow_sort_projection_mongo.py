"""Pre-unwind narrowing: physical Mongo output and Python oracle differential.

The reference removes only the proposed temporary-field projection from the
current exact pipeline. Candidate inserts that sole projection. No product edit.
"""
from copy import deepcopy
import os
from uuid import uuid4
import unittest
from bson import BSON
from src.utils.jjc_equipment_candidates import build_local_win_candidate_pipeline
from tests.test_jjc_candidate_certified_reduce_mongo import canonical, variants, validate
from tests.test_jjc_ranking_equipment_mongo import test_client
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID

NARROW = {"$project": {"_id": 0, "__row": 1, "__keys": 1, "__sort_stamp": 1, "__sort_mid": 1}}
FIELDS = {"__row", "__keys", "__sort_stamp", "__sort_mid"}


def actual_pipeline(ids):
    return build_local_win_candidate_pipeline(ids, season_start=SCOPE["season_start"],
        checked_at=CHECKED_AT, season_id=SCOPE["season_id"])


def reference_and_candidate(ids):
    baseline = [deepcopy(stage) for stage in actual_pipeline(ids)
        if not ("$project" in stage and FIELDS <= set(stage["$project"]))]
    candidate = deepcopy(baseline)
    position = next(index for index, stage in enumerate(candidate) if stage.get("$unwind") == "$__keys")
    candidate.insert(position, deepcopy(NARROW))
    return baseline, candidate


class NarrowSortProjectionStructure(unittest.TestCase):
    def test_product_drops_redundant_payload_after_key_generation_before_unwind(self):
        stages = actual_pipeline([TARGET_GLOBAL_ID])
        unwind = next(index for index, stage in enumerate(stages) if stage.get("$unwind") == "$__keys")
        self.assertIn("$project", stages[unwind-1], "production sort must not carry original data and duplicate temporary players")
        projection = stages[unwind-1]["$project"]
        self.assertEqual(FIELDS, set(projection)-{"_id"})
        self.assertEqual(0, projection.get("_id"))
        self.assertTrue(all(projection[key] == 1 for key in FIELDS))
        self.assertIn("__keys", stages[unwind-2]["$addFields"])


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated loopback URI required")
class NarrowSortProjectionMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = test_client(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]))
        self.name = "jjc_kungfu_test_"+uuid4().hex
        self.db = self.client[self.name]
        self.assertTrue((await self.client.admin.command("buildInfo"))["version"].startswith("4.4."))
        # Deliberately no unique match_id index: distinct physical docs with the
        # same logical ID must remain independent evidence for uncertain rows.
        for side in ("team1", "team2"):
            await self.db.jjc_match_detail.create_index("data.detail."+side+".players_info.global_id")

    async def asyncTearDown(self):
        await self.client.drop_database(self.name)
        self.client.close()

    async def compare(self, rows, ids=None):
        ids = ids or [TARGET_GLOBAL_ID]
        await self.db.jjc_match_detail.delete_many({})
        await self.db.jjc_match_detail.insert_many(deepcopy(rows))
        baseline, candidate = reference_and_candidate(ids)
        before = await self.db.jjc_match_detail.aggregate(baseline, maxTimeMS=1500).to_list(None)
        after = await self.db.jjc_match_detail.aggregate(candidate, maxTimeMS=1500).to_list(None)
        # BSON preserves NaN, Int64 and physical source IDs; Python NaN equality
        # cannot be used to compare this deliberately adversarial corpus.
        self.assertEqual({str(row["_id"]): BSON.encode(row) for row in before},
                         {str(row["_id"]): BSON.encode(row) for row in after})
        self.assertEqual(len(before), len(after))
        for gid in ids: self.assertEqual(validate(before, gid), validate(after, gid))
        return before, after

    async def test_legal_wins_losses_and_newest_missing_weapon_keep_full_result(self):
        start = SCOPE["season_start"]
        rows = [canonical(100+i, start+100+i) for i in range(20)]
        rows += [canonical(900, start+500, equipment=None), canonical(901, start+600, won=False)]
        before, after = await self.compare(rows)
        self.assertEqual([900], [row["match_id"] for row in after])
        self.assertEqual("unknown", validate(after)["packet"]["equipment_state"])

    async def test_existing_uncertified_variant_corpus_preserves_physical_rows_and_python_results(self):
        start = SCOPE["season_start"]
        for label, mutate in variants():
            with self.subTest(label=label):
                dubious = canonical(400, start+50)
                dubious["_id"] = "uncertified-source"
                mutate(dubious)
                await self.compare([dubious, canonical(900, start+500)])

    async def test_1201_uncertified_parallel_arrays_are_not_truncated(self):
        start = SCOPE["season_start"]
        rows = []
        for index in range(1201):
            row = canonical(10000+index, start+50)
            row["_id"] = "physical-array-"+str(index)
            row["data"]["detail"]["match_time"] = [start+50]
            row["data"]["detail"]["match_id"] = [10000+index]
            rows.append(row)
        rows.append(canonical(900, start+500))
        before, after = await self.compare(rows)
        self.assertEqual(1202, len(after))
        self.assertEqual(1201, sum(isinstance(row["data"]["detail"]["match_time"], list) for row in after))

    async def test_distinct_physical_sources_with_same_mid_survive_uncertain_sort(self):
        start = SCOPE["season_start"]
        first = canonical(400, start+50); first["_id"] = "same-mid-source-a"
        second = deepcopy(first); second["_id"] = "same-mid-source-b"
        first["data"]["detail"]["match_time"] = [start+50]
        second["data"]["detail"]["match_id"] = [400]
        before, after = await self.compare([first, second, canonical(900, start+500)])
        self.assertTrue({"same-mid-source-a", "same-mid-source-b"} <= {str(row["_id"]) for row in after})

    async def test_multiple_target_winners_do_not_duplicate_physical_result(self):
        row = canonical(500, SCOPE["season_start"]+100)
        ids = [p["global_id"] for p in row["data"]["detail"]["team1"]["players_info"]]
        before, after = await self.compare([row], ids)
        self.assertEqual(1, len(after))
