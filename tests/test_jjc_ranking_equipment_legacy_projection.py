"""DEP01: narrow bootstrap roster reads; legacy public APIs retain raw data.

Only explicit loopback test Mongo and random owned databases are permitted.
"""
from copy import deepcopy
import os
from types import SimpleNamespace
from uuid import uuid4
import unittest
from unittest.mock import AsyncMock, MagicMock
from bson import BSON
from motor.motor_asyncio import AsyncIOMotorClient
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_view import Clock, FakeIdentities, FakeInspect, FakeSnapshots
from tests.jjc_equipment_fixtures import SCOPE, TIMESTAMP, frozen_member

FROZEN_FIELDS = (
    "rank", "name", "server", "score", "kungfu", "kungfu_id", "lane", "found",
    "global_id", "display_identity_id", "identity_id", "display_locator", "binding_revision",
    "display_scope", "kungfu_verified_at", "kungfu_source", "kungfu_freshness",
    "kungfu_unknown_reason", "kungfu_publish_version", "kungfu_acquisition", "kungfu_selected_source",
)
HEAVY_FIELDS = ("raw", "armors", "display_packet", "weapon_name", "weapon_quality", "weapon_icon", "cached_match_detail_win_samples")


def summary():
    return {"timestamp": TIMESTAMP, "schema_version": 1,
        "current_season": SCOPE["season_id"], "ranking_display_kind": "current",
        "kungfu_cache_scope": deepcopy(SCOPE),
        "kungfu_statistics": {"top_1000": {"total_players": 1000}}}


def roster():
    members = []
    for rank in range(1, 1001):
        member = frozen_member(rank)
        member.update(display_identity_id="identity-"+str(rank), display_scope=deepcopy(SCOPE),
            kungfu_verified_at=1800000100, kungfu_source="local_detail", kungfu_freshness="fresh",
            kungfu_unknown_reason=None, kungfu_publish_version="frozen-version",
            kungfu_acquisition="cache", kungfu_selected_source="local_detail",
            raw={"unrelated_payload": "synthetic-heavy-"+"x"*2048}, armors=[{"irrelevant": "old-armor"}],
            display_packet={"old": "must-not-drive-v2"}, weapon_name="legacy-name",
            weapon_quality=5, weapon_icon="https://example.invalid/legacy.png",
            cached_match_detail_win_samples=[{"raw": "legacy-replay"}])
        members.append(member)
    return members


class LegacyProjectionShape(unittest.IsolatedAsyncioTestCase):
    async def test_context_find_uses_only_dotted_frozen_member_fields(self):
        cursor = MagicMock()
        cursor.max_time_ms.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[])
        details = SimpleNamespace(find=MagicMock(return_value=cursor))
        db = SimpleNamespace(jjc_ranking_stat_summaries=SimpleNamespace(find_one=AsyncMock(return_value=summary())),
            jjc_ranking_stat_details=details)
        await JjcRankingStatsRepo(db=db).load_frozen_equipment_context(TIMESTAMP)
        projection = details.find.call_args.args[1]
        self.assertNotIn("members", projection, "full raw member arrays caused the production native timeout")
        self.assertEqual(0, projection.get("_id"))
        self.assertEqual(1, projection.get("lane"))
        self.assertEqual(1, projection.get("kungfu"))
        for field in FROZEN_FIELDS:
            self.assertEqual(1, projection.get("members."+field), field)
        self.assertFalse(any("members."+field in projection for field in HEAVY_FIELDS))


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated Mongo required")
class LegacyProjectionMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]),
            serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, socketTimeoutMS=2000)
        self.db = self.client["jjc_eq_projection_test_"+uuid4().hex]
        self.repo = JjcRankingStatsRepo(db=self.db, suppress_errors=False)
        self.members = roster()
        await self.db.jjc_ranking_stat_summaries.insert_one(summary())
        await self.db.jjc_ranking_stat_details.insert_one({"timestamp": TIMESTAMP, "range": "top_1000",
            "lane": "dps", "kungfu": "山居剑意", "members": deepcopy(self.members)})

    async def asyncTearDown(self):
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def test_thousand_member_context_is_compact_complete_and_preserves_binding_classification(self):
        context = await self.repo.load_frozen_equipment_context(TIMESTAMP)
        self.assertTrue(context["complete"])
        self.assertEqual(1000, len(context["members"]))
        self.assertLess(len(BSON.encode({"members": context["members"]})), 1024*1024)
        for actual, original in zip(context["members"], self.members):
            for field in FROZEN_FIELDS:
                if field != "binding_revision": self.assertEqual(original[field], actual[field], field)
            self.assertTrue(actual["binding_revision"])
            self.assertTrue(all(field not in actual for field in HEAVY_FIELDS))

    async def test_unrelated_payload_changes_do_not_change_member_revision_or_bindings(self):
        before = await self.repo.load_frozen_equipment_context(TIMESTAMP)
        await self.db.jjc_ranking_stat_details.update_one({"timestamp": TIMESTAMP},
            {"$set": {"members.0.raw": {"changed": "irrelevant"}, "members.0.armors": [{"changed": 1}]}})
        after = await self.repo.load_frozen_equipment_context(TIMESTAMP)
        self.assertTrue(after["complete"])
        self.assertEqual(before["member_revision"], after["member_revision"])
        self.assertEqual([m["binding_revision"] for m in before["members"]], [m["binding_revision"] for m in after["members"]])
        self.assertNotIn("raw", after["members"][0])

    async def test_narrow_context_supports_subsequent_complete_dynamic_view(self):
        context = await self.repo.load_frozen_equipment_context(TIMESTAMP)
        inspector = FakeInspect()
        inspector.rows = []
        clock = Clock()
        service = JjcRankingEquipmentViewService(stats_repo=self.repo,
            identity_repo=FakeIdentities(context["members"]), inspect_repo=inspector,
            snapshot_repo=FakeSnapshots(), scope=deepcopy(SCOPE), clock=clock.wall, monotonic=clock.monotonic)
        try:
            result = await service.read_dynamic_view(timestamp=TIMESTAMP,
                expected_member_revision=context["member_revision"], expected_frozen_source="legacy_v1")
            self.assertTrue(result["overlay_enabled"])
            self.assertEqual(1000, len(result["view"]["members"]))
            self.assertTrue(result["view"]["equipment_view_revision"])
        finally:
            await service.close()

    async def test_legacy_summary_detail_and_flat_outputs_keep_original_raw_member_fields(self):
        public_summary = await self.repo.load_summary(TIMESTAMP)
        self.assertEqual(summary(), public_summary)
        detail = await self.repo.load_detail(TIMESTAMP, "top_1000", "dps", "山居剑意")
        self.assertEqual(self.members, detail["members"])
        flat = await self.repo.list_flat_members(TIMESTAMP, "top_1000", limit=1)
        self.assertEqual(1000, flat["total"])
        for field in HEAVY_FIELDS:
            self.assertEqual(self.members[0][field], flat["items"][0][field], field)
