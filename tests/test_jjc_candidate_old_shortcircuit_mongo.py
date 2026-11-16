"""DEP06: common-old expression shortcut must short-circuit, with exact fallback."""
import os
import unittest

from tests import test_jjc_candidate_fast_prune_mongo as corpus
previous_proven_old=corpus.previous_proven_old


def shortcut(expression):
    if isinstance(expression,dict):
        for key,value in expression.items():
            if key=="$cond" and isinstance(value,list) and len(value)==3 and value[1] is False:
                return value
            found=shortcut(value)
            if found is not None:return found
    elif isinstance(expression,list):
        for value in expression:
            found=shortcut(value)
            if found is not None:return found
    return None


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class CandidateOldShortcutMongoTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=corpus.CandidateFastPruneMongoTests.asyncSetUp
    asyncTearDown=corpus.CandidateFastPruneMongoTests.asyncTearDown
    ids=corpus.CandidateFastPruneMongoTests.ids

    def condition(self):
        expression=next(item["$expr"] for item in self.query["$and"] if "$expr" in item)
        condition=shortcut(expression)
        self.assertIsNotNone(condition,
            "DEP06 requires $cond(common_old, false, original_not_proven_old), not unordered query AND")
        return condition

    async def test_common_old_shapes_take_the_shortcut(self):
        guard=self.condition()[0]
        fast=await self.ids({"$expr":guard})
        for label in ("old_int","old_long","old_double","basic_only_old","three_copies_old"):
            self.assertIn(self.labels[label],fast,label)
        self.assertNotIn(self.labels["three_copies_conflicting_old"],fast)

    async def test_shortcut_implies_original_old_and_fallback_retains_all_ambiguous_bson(self):
        guard,unused,fallback=self.condition()
        self.assertEqual(set(),await self.ids({"$and":[
            {"$expr":guard},{"$expr":{"$not":[previous_proven_old()]}}]}))
        # A fast branch is only a shortcut: unresolved evidence keeps the exact
        # previous semantics, including old-time conflicts and ancestor arrays.
        expected=await self.ids({"$expr":{"$not":[previous_proven_old()]}})
        self.assertEqual(expected,await self.ids({"$expr":fallback}))
        self.assertEqual(expected,await self.ids({"$expr":{"$cond":[guard,False,fallback]}}))
        for label in ("three_copies_conflicting_old","array","nested_array","decimal_old",
                      "decimal_nan","decimal_inf","nan","inf","missing","ancestor_array:data",
                      "ancestor_array:data.detail","ancestor_array:data.detail.basic_info"):
            self.assertIn(self.labels[label],expected,label)


if __name__=="__main__":unittest.main()
