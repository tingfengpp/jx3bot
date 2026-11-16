"""DEP04 dedicated compact identity reads; synthetic/owned Mongo only.

Until implementation exists, baseline fallback deliberately exercises the old
broad getter to obtain behavioral RED rather than missing-method errors. A
separate mandatory-entrypoint assertion prevents that fallback passing the suite.
"""
import asyncio
from copy import deepcopy
import os
from types import SimpleNamespace
from uuid import uuid4
import unittest
from unittest.mock import patch
from bson import BSON, ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from tests.test_jjc_ranking_equipment_repo import RecordingCollection, InjectedReadOnlyDB
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_shutdown import service_for, operation
from tests.test_jjc_ranking_equipment_view import FakeStats, FakeIdentities

FIELDS = ("_id", "server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")


def getter(repo):
    return getattr(repo, "get_equipment_bindings_by_ids", repo.get_display_by_ids)


def identity(oid=None):
    return {"_id": oid or ObjectId(), "server": "synthetic-server", "name": "synthetic-role",
        "zone": "synthetic-zone", "game_role_id": "123", "role_id": "456",
        "global_id": "700000000000000001", "global_role_id": "789",
        "jjc_display": {"unrelated": "heavy"*20000}, "unknown_field": "not-a-binding"}


class CompactIdentityBindingTests(unittest.IsolatedAsyncioTestCase):
    def repo(self, rows=None, **kwargs):
        self.collection = RecordingCollection(rows or [], **kwargs)
        return RoleIdentityRepo(db=InjectedReadOnlyDB(role_identities=self.collection))

    async def test_dedicated_method_must_exist_without_changing_old_display_contract(self):
        repo = self.repo()
        self.assertTrue(callable(getattr(repo, "get_equipment_bindings_by_ids", None)))

    async def test_exact_id_index_query_narrow_projection_dedupe_and_server_budget(self):
        row = identity()
        repo = self.repo([row])
        actual = await getter(repo)([str(row["_id"]), row["_id"], "invalid", None])
        self.assertEqual(set(FIELDS), set(actual[0]))
        self.assertEqual([{key: row[key] for key in FIELDS}], actual)
        self.assertEqual(1, len(self.collection.calls))
        call = self.collection.calls[0]
        self.assertEqual({"_id": {"$in": [row["_id"]]}}, call["query"])
        self.assertEqual({key: 1 for key in FIELDS}, call["projection"])
        cursor = self.collection.cursors[0]
        budget = call["kwargs"].get("max_time_ms")
        self.assertTrue(budget == 1500 or cursor.max_time_values == [1500])
        self.assertEqual([], cursor.limit_calls, "do not truncate ID bindings")

    async def test_empty_or_invalid_ids_perform_zero_collection_access(self):
        repo = RoleIdentityRepo(db=InjectedReadOnlyDB())
        for ids in ([], [None, "not-an-objectid"]):
            self.assertEqual([], await getter(repo)(ids))
        self.assertEqual([], repo.db.accessed)

    async def test_cursor_fault_propagates_without_best_effort_empty_result(self):
        repo = self.repo([identity()], fail_after=0, exception=RuntimeError("synthetic-binding-getMore"))
        with self.assertRaisesRegex(RuntimeError, "synthetic-binding-getMore"):
            await getter(repo)([ObjectId()])

    async def test_cancelled_read_propagates_cancellation(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        class Cursor:
            def max_time_ms(self, value): return self
            async def to_list(self, length):
                entered.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
        col = SimpleNamespace(find=lambda *a, **k: Cursor())
        repo = RoleIdentityRepo(db=SimpleNamespace(role_identities=col))
        task = asyncio.create_task(getter(repo)([ObjectId()]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cancelled.is_set())

    async def test_equipment_service_uses_dedicated_binding_path(self):
        stats = FakeStats()
        service = service_for(stats)
        rows = deepcopy(FakeIdentities(stats.context["members"]).rows)
        class DedicatedOnly:
            async def get_equipment_bindings_by_ids(self, ids): return deepcopy(rows)
            async def get_display_by_ids(self, ids):
                raise AssertionError("equipment read must not load jjc_display packets")
        service.identity_repo = DedicatedOnly()
        try:
            result = await operation(service, stats, "dynamic")
            self.assertTrue(result["overlay_enabled"])
        except Exception as error:
            self.fail("equipment service still depends on broad display getter: %s" % type(error).__name__)
        finally:
            await service.close()


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated Mongo required")
class CompactIdentityBindingMongo(unittest.IsolatedAsyncioTestCase):
    async def test_large_display_packet_is_omitted_but_old_getter_and_identity_are_unchanged(self):
        client = AsyncIOMotorClient(checked_test_uri(os.environ["JJC_TEST_MONGO_URI"]),
            serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, socketTimeoutMS=2000)
        name = "jjc_kungfu_test_"+uuid4().hex
        db = client[name]
        row = identity()
        row["jjc_display"]["unrelated"] = "x"*(2*1024*1024)
        try:
            await db.role_identities.insert_one(deepcopy(row))
            repo = RoleIdentityRepo(db=db)
            compact = await getter(repo)([str(row["_id"])])
            self.assertEqual(set(FIELDS), set(compact[0]))
            self.assertEqual([{key: row[key] for key in FIELDS}], compact)
            self.assertLess(len(BSON.encode({"rows": compact})), 2048)
            legacy = await repo.get_display_by_ids([row["_id"]])
            self.assertEqual(row["jjc_display"], legacy[0]["jjc_display"])
            for key in FIELDS: self.assertEqual(row[key], legacy[0][key])
            explain = await db.command("explain", {"find": "role_identities", "filter": {"_id": {"$in": [row["_id"]]}},
                "projection": {key: 1 for key in FIELDS}}, verbosity="executionStats")
            plan = str(explain["queryPlanner"]["winningPlan"])
            self.assertIn("_id_", plan)
            self.assertEqual(1, explain["executionStats"]["totalDocsExamined"])
        finally:
            await client.drop_database(name)
            client.close()
