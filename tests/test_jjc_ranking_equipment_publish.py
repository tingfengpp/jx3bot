"""E3 publication contracts (TC19-26), written before implementation.

Unit collections exercise acknowledgement/atomic-operation boundaries only. They
are NOT a Mongo expression interpreter. Complex schema1 CAS semantics are tested
against explicit isolated Mongo below; missing imports are not a business RED.
No client is created unless JJC_TEST_MONGO_URI passes checked_test_uri.
"""
import asyncio
from copy import deepcopy
import datetime
import os
import unittest
from uuid import uuid4

from bson import BSON, ObjectId
from pymongo.errors import AutoReconnect, DuplicateKeyError

from src.services.jx3.jjc_ranking_equipment import aggregate_equipment_view
from src.storage.mongo_repos.jjc_ranking_stats_repo import (
    EquipmentSnapshotError, JjcRankingStatsRepo,
)
from tests.jjc_equipment_fixtures import (
    SCOPE, CHECKED_AT, TIMESTAMP, MEMBER_REVISION, ten_member_golden,
)

LIMIT = 8 * 1024 * 1024


def candidate(*, timestamp=TIMESTAMP, member_revision=MEMBER_REVISION, members=None):
    view = aggregate_equipment_view(
        timestamp=timestamp, scope=deepcopy(SCOPE),
        members=ten_member_golden() if members is None else deepcopy(members),
        checked_at=CHECKED_AT, completed_at=CHECKED_AT + .1,
        member_revision=member_revision,
    )
    return {
        "timestamp": timestamp, "schema_version": 2,
        "current_season": SCOPE["season_id"], "default_week": 1,
        "ranking_display_kind": "current", "generated_at": CHECKED_AT,
        "ranking_cache_time": timestamp, "week_info": "合成当前榜",
        "kungfu_cache_scope": deepcopy(SCOPE),
        "member_revision": view["member_revision"],
        "equipment_source": "local_win_v2", "equipment_complete": True,
        "equipment_view_revision": view["equipment_view_revision"],
        "equipment_view": view,
    }


def legacy():
    return {
        "_id": ObjectId(), "timestamp": TIMESTAMP, "schema_version": 1,
        "current_season": SCOPE["season_id"], "ranking_display_kind": "current",
        "kungfu_cache_scope": deepcopy(SCOPE), "week_info": "合成当前榜",
        "nullable": None, "samples": [None, {"literal": "$not_a_field"}, [1, 2]],
        "nested": {"a": None, "b": [1, 2]},
    }


class _Result:
    def __init__(self, matched=1, inserted_id=None):
        self.matched_count = matched
        self.modified_count = matched
        self.inserted_id = inserted_id
        self.acknowledged = True


class _Cursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
    def sort(self, key, direction=None):
        specs = key if isinstance(key, list) else [(key, direction or 1)]
        for field, order in reversed(specs):
            self.rows.sort(key=lambda row: row.get(field, 0), reverse=order < 0)
        return self
    def limit(self, n):
        self.rows = self.rows[:n]
        return self
    async def to_list(self, length=None):
        return deepcopy(self.rows if length is None else self.rows[:length])


class _SummaryCollection:
    """Scripted unit storage, intentionally NOT proof of Mongo CAS expressions."""
    def __init__(self, document=None):
        self.docs = [] if document is None else [deepcopy(document)]
        self.writes = []
        self.fail_write = None
        self.failed_write = False
        self.fail_readback = False
        self.force_conflict = False

    @staticmethod
    def matches(row, query):
        # Scalar field guards are useful for unit schema2 races. Complex $expr
        # guards are exercised by the real-Mongo tests, not simulated here.
        for key, wanted in query.items():
            if key.startswith("$"):
                continue
            actual = row.get(key)
            if isinstance(wanted, dict):
                if "$ne" in wanted and actual == wanted["$ne"]:
                    return False
                if "$in" in wanted and actual not in wanted["$in"]:
                    return False
                if "$exists" in wanted and (key in row) != wanted["$exists"]:
                    return False
            elif actual != wanted:
                return False
        return True

    async def find_one(self, query=None, projection=None, sort=None, **kwargs):
        if self.failed_write and self.fail_readback:
            raise AutoReconnect("synthetic readback unavailable")
        rows = [r for r in self.docs if self.matches(r, query or {})]
        if sort:
            rows = _Cursor(rows).sort(sort).rows
        return deepcopy(rows[0]) if rows else None

    def find(self, query=None, projection=None, **kwargs):
        return _Cursor([r for r in self.docs if self.matches(r, query or {})])

    def _before_write(self, operation, query, doc):
        self.writes.append((operation, deepcopy(query), deepcopy(doc)))
        if self.fail_write == "before":
            self.failed_write = True
            raise AutoReconnect("synthetic write outcome unknown")

    def _after_write(self):
        if self.fail_write == "after":
            self.failed_write = True
            raise AutoReconnect("synthetic acknowledgement lost")

    async def insert_one(self, document, **kwargs):
        self._before_write("insert_one", {}, document)
        if any(r["timestamp"] == document["timestamp"] for r in self.docs):
            raise DuplicateKeyError("synthetic unique timestamp")
        row = deepcopy(document)
        row.setdefault("_id", ObjectId())
        self.docs.append(row)
        self._after_write()
        return _Result(inserted_id=row["_id"])

    async def replace_one(self, query, replacement, upsert=False, **kwargs):
        self._before_write("replace_one", query, replacement)
        if self.force_conflict:
            return _Result(0)
        for index, row in enumerate(self.docs):
            if self.matches(row, query):
                updated = deepcopy(replacement)
                updated.setdefault("_id", row["_id"])
                self.docs[index] = updated
                self._after_write()
                return _Result(1)
        return _Result(0)

    async def update_one(self, *args, **kwargs):
        raise AssertionError("V2 must publish one complete document, not patch fragments")


class _ForbiddenDetails:
    def __getattr__(self, name):
        raise AssertionError("V2 must not read/write legacy details: " + name)


class _Database:
    def __init__(self, collection):
        self.jjc_ranking_stat_summaries = collection
        self.jjc_ranking_stat_details = _ForbiddenDetails()
    def __getitem__(self, name):
        return getattr(self, name)


class EquipmentPublicationUnitTests(unittest.IsolatedAsyncioTestCase):
    def repo(self, document=None):
        col = _SummaryCollection(document)
        # Default suppress_errors=True must not swallow V2 publication failures.
        return JjcRankingStatsRepo(db=_Database(col)), col

    async def assert_code(self, code, operation):
        with self.assertRaises(EquipmentSnapshotError) as raised:
            await operation
        self.assertEqual(code, raised.exception.code)

    async def test_tc19_first_publication_is_single_complete_insert(self):
        repo, col = self.repo()
        doc = candidate()
        original = deepcopy(doc)
        result = await repo.publish_equipment_snapshot(TIMESTAMP, doc)
        self.assertEqual("published", result["status"])
        self.assertEqual(doc["equipment_view_revision"], result["revision"])
        self.assertFalse(result["recovered"])
        self.assertEqual(["insert_one"], [w[0] for w in col.writes])
        saved = col.docs[0]
        self.assertTrue(saved["equipment_complete"])
        self.assertEqual(2, saved["schema_version"])
        self.assertEqual(doc["equipment_view"], saved["equipment_view"])
        self.assertEqual(original, doc, "publication may not mutate caller's candidate")
        loaded = await repo.load_complete_view(TIMESTAMP)
        self.assertEqual(doc["equipment_view"], loaded)

    async def test_tc19_complete_reader_ignores_absent_or_stale_legacy_details(self):
        repo, col = self.repo(candidate())
        loaded = await repo.load_complete_view(TIMESTAMP)
        self.assertIsInstance(loaded, dict, "a complete V2 view must be returned")
        self.assertEqual(10, loaded["ranges"]["top_50"]["N"])
        self.assertEqual([3, 4, 5, 6, 7], loaded["ranges"]["top_50"]["purple_ranks"])
        self.assertEqual([], col.writes)

    async def test_tc20_actual_bson_over_limit_is_rejected_before_write(self):
        old = candidate()
        repo, col = self.repo(old)
        oversized = candidate()
        oversized["week_info"] = "测" * (LIMIT // 2)
        self.assertGreater(len(BSON.encode(oversized)), LIMIT)
        await self.assert_code("snapshot_too_large", repo.publish_equipment_snapshot(
            TIMESTAMP, oversized, expected_revision=old["equipment_view_revision"]))
        self.assertEqual([], col.writes)
        self.assertEqual([old], col.docs)

    async def test_tc20_near_limit_legal_document_includes_publication_metadata(self):
        repo, col = self.repo()
        doc = candidate()
        doc["week_info"] = "x" * (LIMIT - len(BSON.encode(doc)) - 1024)
        await repo.publish_equipment_snapshot(TIMESTAMP, doc)
        self.assertEqual(1, len(col.docs), "publication must create a complete document")
        self.assertLessEqual(len(BSON.encode(col.docs[0])), LIMIT)
        self.assertGreater(len(BSON.encode(col.docs[0])), LIMIT - 2048)

    async def test_tc20_final_bson_document_exact_boundary_and_one_byte_over(self):
        for size in (LIMIT - 1, LIMIT, LIMIT + 1):
            with self.subTest(final_bson_bytes=size):
                repo, col = self.repo()
                doc = candidate()
                doc["_id"] = ObjectId()
                doc["published_at"] = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)
                doc["week_info"] = ""
                doc["week_info"] = "x" * (size - len(BSON.encode(doc)))
                self.assertEqual(size, len(BSON.encode(doc)))
                if size > LIMIT:
                    await self.assert_code("snapshot_too_large", repo.publish_equipment_snapshot(TIMESTAMP, doc))
                    self.assertEqual([], col.writes)
                else:
                    await repo.publish_equipment_snapshot(TIMESTAMP, doc)
                    self.assertEqual(size, len(BSON.encode(col.docs[0])))

    async def test_tc20_invalid_member_and_field_limits_reject_without_write(self):
        good = candidate()
        cases = []
        many = deepcopy(good)
        many["equipment_view"]["members"] = [deepcopy(good["equipment_view"]["members"][0]) for _ in range(1001)]
        cases.append(many)
        long_name = deepcopy(good)
        long_name["equipment_view"]["members"][0]["name"] = "n" * 257
        cases.append(long_name)
        long_icon = deepcopy(good)
        long_icon["equipment_view"]["members"][0]["local_win_packet"]["weapon_icon"] = "https://example.invalid/" + "x" * 2048
        cases.append(long_icon)
        for doc in cases:
            with self.subTest(kind=len(doc["equipment_view"]["members"])):
                repo, col = self.repo()
                await self.assert_code("invalid_snapshot", repo.publish_equipment_snapshot(TIMESTAMP, doc))
                self.assertEqual([], col.writes)

    async def test_tc21_existing_timestamp_cannot_be_silently_overwritten_as_first_insert(self):
        old = candidate()
        repo, col = self.repo(old)
        await self.assert_code("conflict", repo.publish_equipment_snapshot(TIMESTAMP, candidate(member_revision="other-members")))
        self.assertEqual([old], col.docs)

    async def test_tc21_schema2_compare_revision_and_member_fences(self):
        old = candidate()
        old["_id"] = ObjectId()
        new = candidate(member_revision=old["member_revision"])
        new["equipment_view"]["members"][0]["local_win_packet"]["match_time"] += 1
        # Rebuild the aggregate, so this is a valid new content revision.
        new = candidate(members=new["equipment_view"]["members"])
        repo, col = self.repo(old)
        await repo.publish_equipment_snapshot(TIMESTAMP, new, expected_revision=old["equipment_view_revision"])
        replacement = [w for w in col.writes if w[0] == "replace_one"]
        self.assertEqual(1, len(replacement))
        guard = replacement[0][1]
        self.assertEqual(old["_id"], guard["_id"])
        self.assertEqual(2, guard["schema_version"])
        self.assertEqual(old["equipment_view_revision"], guard["equipment_view_revision"])
        self.assertEqual(old["member_revision"], guard["member_revision"])
        self.assertEqual(new["equipment_view"], col.docs[0]["equipment_view"])

    async def test_tc21_lost_compare_and_swap_does_not_retry_old_candidate(self):
        old = candidate()
        old["_id"] = ObjectId()
        repo, col = self.repo(old)
        col.force_conflict = True
        await self.assert_code("conflict", repo.publish_equipment_snapshot(
            TIMESTAMP, candidate(), expected_revision=old["equipment_view_revision"]))
        self.assertEqual(1, len(col.writes))
        self.assertEqual([old], col.docs)

    async def test_tc22_historical_candidate_and_existing_history_are_protected(self):
        doc = candidate()
        doc["ranking_display_kind"] = "historical"
        repo, col = self.repo()
        await self.assert_code("historical", repo.publish_equipment_snapshot(TIMESTAMP, doc))
        self.assertEqual([], col.writes)
        old = legacy()
        old["ranking_display_kind"] = "historical"
        repo, col = self.repo(old)
        await self.assert_code("historical", repo.publish_equipment_snapshot(TIMESTAMP, candidate(), expected_legacy=deepcopy(old)))
        self.assertEqual([], col.writes)

    async def test_tc23_lost_acknowledgement_is_confirmed_by_complete_readback(self):
        repo, col = self.repo()
        col.fail_write = "after"
        doc = candidate()
        result = await repo.publish_equipment_snapshot(TIMESTAMP, doc)
        self.assertEqual("published", result["status"])
        self.assertTrue(result["recovered"])
        self.assertEqual(doc["equipment_view_revision"], result["revision"])
        self.assertEqual(1, len(col.writes), "unknown outcome must not blindly replay writes")
        self.assertEqual(doc["equipment_view"], col.docs[0]["equipment_view"])

    async def test_tc23_different_or_unreadable_outcome_is_not_success(self):
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                repo, col = self.repo()
                col.fail_write = "before"
                col.fail_readback = unreadable
                await self.assert_code("publish_unknown", repo.publish_equipment_snapshot(TIMESTAMP, candidate()))
                self.assertEqual(1, len(col.writes))

    async def test_tc23_cancellation_propagates_without_confirming_success(self):
        repo, col = self.repo()
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        col.insert_one = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await repo.publish_equipment_snapshot(TIMESTAMP, candidate())

    async def test_tc24_schema1_upgrade_requires_complete_scope_and_matching_snapshot(self):
        old = legacy()
        old.pop("kungfu_cache_scope")
        repo, col = self.repo(old)
        await self.assert_code("invalid_snapshot", repo.publish_equipment_snapshot(
            TIMESTAMP, candidate(), expected_legacy=deepcopy(old)))
        self.assertEqual([], col.writes)
        old = legacy()
        repo, col = self.repo(old)
        result = await repo.publish_equipment_snapshot(TIMESTAMP, candidate(), expected_legacy=deepcopy(old))
        self.assertEqual("published", result["status"])
        self.assertEqual(["replace_one"], [w[0] for w in col.writes])

    async def test_tc24_legacy_writer_cannot_downgrade_published_schema2(self):
        old = candidate()
        repo, col = self.repo(old)
        await self.assert_code("conflict", repo.upsert_summary(TIMESTAMP, {"current_season": SCOPE["season_id"], "kungfu_statistics": {}}))
        self.assertEqual([old], col.docs)
        self.assertEqual([], col.writes)

    async def test_tc25_marked_complete_but_damaged_view_is_rejected(self):
        for defect in ("duplicate_rank", "bad_counts", "root_revision", "source", "incomplete"):
            with self.subTest(defect=defect):
                doc = candidate()
                if defect == "duplicate_rank":
                    doc["equipment_view"]["members"][1]["rank"] = 1
                elif defect == "bad_counts":
                    doc["equipment_view"]["ranges"]["top_50"]["L"] += 1
                elif defect == "root_revision":
                    doc["equipment_view_revision"] = "different-revision"
                elif defect == "source":
                    doc["equipment_source"] = "legacy_v1"
                else:
                    doc["equipment_complete"] = False
                repo, col = self.repo(doc)
                await self.assert_code("invalid_snapshot", repo.load_complete_view(TIMESTAMP))
                self.assertEqual([], col.writes)

    async def test_tc25_historical_complete_frozen_view_is_readable_but_not_republished(self):
        doc = candidate()
        doc["ranking_display_kind"] = "historical"
        repo, col = self.repo(doc)
        self.assertEqual(doc["equipment_view"], await repo.load_complete_view(TIMESTAMP))
        self.assertEqual([], col.writes)
        await self.assert_code("historical", repo.publish_equipment_snapshot(
            TIMESTAMP, candidate(), expected_revision=doc["equipment_view_revision"]))

    async def test_tc24_legacy_failure_suppression_is_preserved_except_downgrade(self):
        repo, col = self.repo()
        async def unavailable(*args, **kwargs):
            raise OSError("synthetic legacy store unavailable")
        col.find_one = unavailable
        # Default legacy behavior remains best-effort; only the new schema2
        # downgrade conflict is intentionally strict.
        await repo.upsert_summary(TIMESTAMP, {})
        strict = JjcRankingStatsRepo(db=_Database(col), suppress_errors=False)
        with self.assertRaises(OSError):
            await strict.upsert_summary(TIMESTAMP, {})

    async def test_tc26_expected_revision_mismatch_and_dynamic_revision_are_not_guessed(self):
        doc = candidate()
        repo, col = self.repo(doc)
        for revision in ("older-frozen-revision", "dynamic-view-revision"):
            await self.assert_code("version_mismatch", repo.load_complete_view(TIMESTAMP, expected_view_revision=revision))
        view = await repo.load_complete_view(TIMESTAMP, expected_view_revision=doc["equipment_view_revision"])
        self.assertEqual(doc["equipment_view"], view)
        self.assertEqual([], col.writes)


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated Mongo URI required")
class EquipmentPublicationMongoSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
        self.uri = checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        self.client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, socketTimeoutMS=2000)
        self.db_name = "jjc_kungfu_test_" + uuid4().hex
        self.db = self.client[self.db_name]
        self.addAsyncCleanup(self.cleanup_owned_database)
        self.version = (await self.client.server_info())["version"]
        await self.db.jjc_ranking_stat_summaries.create_index("timestamp", unique=True)
        self.repo = JjcRankingStatsRepo(db=self.db)

    async def cleanup_owned_database(self):
        try:
            if self.db_name.startswith("jjc_kungfu_test_") and len(self.db_name) == len("jjc_kungfu_test_") + 32:
                await self.client.drop_database(self.db_name)
            else:
                raise AssertionError("refusing cleanup of unowned database")
        finally:
            self.client.close()

    async def test_tc19_tc21_first_insert_and_real_two_connection_revision_race(self):
        first = candidate()
        await self.repo.publish_equipment_snapshot(TIMESTAMP, first)
        from motor.motor_asyncio import AsyncIOMotorClient
        other_client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=1500,
                                         connectTimeoutMS=1500, socketTimeoutMS=2000)
        self.addCleanup(other_client.close)
        gate = asyncio.Event()
        ready = []

        class RacingCollection:
            def __init__(self, real):
                self.real = real
            def __getattr__(self, name):
                return getattr(self.real, name)
            async def replace_one(self, query, replacement, **kwargs):
                # Both independent clients must have built their guards before
                # either write reaches Mongo; a merely sequential test would
                # not prove the compare-and-swap survives the race.
                ready.append(deepcopy(query))
                if len(ready) == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 5)
                return await self.real.replace_one(query, replacement, **kwargs)

        class RacingDatabase:
            def __init__(self, real):
                self.real = real
                self.jjc_ranking_stat_summaries = RacingCollection(real.jjc_ranking_stat_summaries)
            def __getattr__(self, name):
                return getattr(self.real, name)
            def __getitem__(self, name):
                return getattr(self, name)

        first_writer = JjcRankingStatsRepo(db=RacingDatabase(self.db))
        other = JjcRankingStatsRepo(db=RacingDatabase(other_client[self.db_name]))
        members_a, members_b = ten_member_golden(), ten_member_golden()
        members_a[0]["local_win_packet"]["match_time"] += 10
        members_b[0]["local_win_packet"]["match_time"] += 20
        a, b = candidate(members=members_a), candidate(members=members_b)
        results = await asyncio.gather(
            first_writer.publish_equipment_snapshot(TIMESTAMP, a, expected_revision=first["equipment_view_revision"]),
            other.publish_equipment_snapshot(TIMESTAMP, b, expected_revision=first["equipment_view_revision"]),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, dict) and r.get("status") == "published"]
        failures = [r for r in results if isinstance(r, EquipmentSnapshotError)]
        self.assertEqual(2, len(ready), "both independently read old revision before writes")
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(failures))
        self.assertEqual("conflict", failures[0].code)
        saved = await self.db.jjc_ranking_stat_summaries.find_one({"timestamp": TIMESTAMP})
        self.assertEqual(successes[0]["revision"], saved["equipment_view_revision"])
        self.assertIn(saved["equipment_view"], [a["equipment_view"], b["equipment_view"]])
        self.assertEqual(0, await self.db.jjc_ranking_stat_details.count_documents({}))

    async def test_tc24_schema1_cas_rejects_null_missing_same_key_count_array_nested_and_literals(self):
        mutations = [
            lambda d: (d.pop("nullable"), d.update({"replacement_null": None})),
            lambda d: d.update({"new_null": None}),
            lambda d: d["samples"].append(3),
            lambda d: d.update({"samples": [d["samples"]]}),
            lambda d: d["nested"].update({"b": [2, 1]}),
            lambda d: d.update({"week_info": "$different_literal"}),
        ]
        for mutate in mutations:
            old = legacy()
            await self.db.jjc_ranking_stat_summaries.delete_many({})
            await self.db.jjc_ranking_stat_summaries.insert_one(deepcopy(old))
            changed = deepcopy(old)
            mutate(changed)
            await self.db.jjc_ranking_stat_summaries.replace_one({"_id": old["_id"]}, changed)
            with self.assertRaises(EquipmentSnapshotError) as raised:
                await self.repo.publish_equipment_snapshot(TIMESTAMP, candidate(), expected_legacy=old)
            self.assertEqual("conflict", raised.exception.code)
            self.assertEqual(changed, await self.db.jjc_ranking_stat_summaries.find_one({"timestamp": TIMESTAMP}))
        # The exact original document, including null/array/literal values, IS
        # admissible; over-restrictive CAS that rejects every upgrade is invalid.
        old = legacy()
        await self.db.jjc_ranking_stat_summaries.delete_many({})
        await self.db.jjc_ranking_stat_summaries.insert_one(deepcopy(old))
        result = await self.repo.publish_equipment_snapshot(TIMESTAMP, candidate(), expected_legacy=old)
        self.assertEqual("published", result["status"])
        self.assertEqual(2, (await self.db.jjc_ranking_stat_summaries.find_one({"timestamp": TIMESTAMP}))["schema_version"])

    async def test_tc24_schema1_cas_preserves_bson_type_not_numeric_coercion(self):
        from bson.int64 import Int64
        old = legacy()
        old["typed_number"] = 7
        await self.db.jjc_ranking_stat_summaries.insert_one(deepcopy(old))
        await self.db.jjc_ranking_stat_summaries.update_one({"_id": old["_id"]}, {"$set": {"typed_number": Int64(7)}})
        with self.assertRaises(EquipmentSnapshotError) as raised:
            await self.repo.publish_equipment_snapshot(TIMESTAMP, candidate(), expected_legacy=old)
        self.assertEqual("conflict", raised.exception.code)

    async def test_tc24_legacy_upgrade_race_cannot_downgrade_schema2(self):
        old = legacy()
        await self.db.jjc_ranking_stat_summaries.insert_one(deepcopy(old))
        upgraded = candidate()
        upgraded["_id"] = old["_id"]
        collection = self.db.jjc_ranking_stat_summaries
        armed = [True]
        class UpgradeRaceCollection:
            async def find_one(inner, *args, **kwargs):
                before = await collection.find_one(*args, **kwargs)
                if armed[0]:
                    armed[0] = False
                    await collection.replace_one({"_id": old["_id"]}, deepcopy(upgraded))
                return before
            def __getattr__(inner, name):
                return getattr(collection, name)
        class UpgradeRaceDatabase:
            jjc_ranking_stat_summaries = UpgradeRaceCollection()
        repo = JjcRankingStatsRepo(db=UpgradeRaceDatabase())
        with self.assertRaises(EquipmentSnapshotError) as raised:
            await repo.upsert_summary(TIMESTAMP, {"current_season": SCOPE["season_id"]})
        self.assertEqual("conflict", raised.exception.code)
        self.assertEqual(upgraded, await collection.find_one({"timestamp": TIMESTAMP}))

    async def test_tc24_recursive_bson_type_changes_are_not_erased_by_equality(self):
        from bson.int64 import Int64
        for key in ("nested", "samples"):
            with self.subTest(container=key):
                old = legacy()
                await self.db.jjc_ranking_stat_summaries.delete_many({})
                await self.db.jjc_ranking_stat_summaries.insert_one(deepcopy(old))
                changed = deepcopy(old)
                if key == "nested":
                    changed["nested"]["b"][0] = Int64(1)
                else:
                    changed["samples"][2][0] = Int64(1)
                await self.db.jjc_ranking_stat_summaries.replace_one({"_id": old["_id"]}, changed)
                with self.assertRaises(EquipmentSnapshotError) as raised:
                    await self.repo.publish_equipment_snapshot(TIMESTAMP, candidate(), expected_legacy=old)
                self.assertEqual("conflict", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
