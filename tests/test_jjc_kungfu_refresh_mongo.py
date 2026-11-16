"""Real Mongo tests: explicit isolated loopback URI, random disposable database."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import os
import multiprocessing
import unittest
from urllib.parse import urlsplit
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bson import ObjectId
from bson.int64 import Int64
from motor.motor_asyncio import AsyncIOMotorClient

from src.storage.mongo_repos.jjc_kungfu_refresh_repo import JjcKungfuRefreshRepo, QueueIntegrityError
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from tests.test_jjc_kungfu_refresh_repo import SCOPE
from tests.test_role_identity_jjc_kungfu import display_publication_fixture


def checked_test_uri(uri):
    parsed = urlsplit(uri)
    if (parsed.scheme != "mongodb" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port != 27029 or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/", "/jjc_kungfu_test"}):
        raise ValueError("requires explicit isolated loopback Mongo on port 27029")
    return uri


def claim_process(uri, database, barrier, outcomes):
    async def run():
        client = AsyncIOMotorClient(checked_test_uri(uri), serverSelectionTimeoutMS=3000)
        try:
            await asyncio.to_thread(barrier.wait, 10)
            task = await JjcKungfuRefreshRepo(db=client[database]).claim(SCOPE, now=1000)
            outcomes.put(("ok", task is not None))
        except Exception as exc:
            outcomes.put(("error", type(exc).__name__))
        finally:
            client.close()
    asyncio.run(run())


class TestMongoUriSafety(unittest.TestCase):
    def test_rejects_production_remote_default_port_and_credentials(self):
        for uri in ["mongodb://production.invalid:27029/", "mongodb://localhost:27017/", "mongodb://localhost:27029/production", "mongodb://localhost:27029/?replicaSet=anything", "mongodb://%s:%s@localhost:27029/" % (uuid4().hex, uuid4().hex)]:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                checked_test_uri(uri)


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit local Mongo URI required")
class TestDisplayPublicationMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri = checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        self.db_name = "jjc_kungfu_test_" + uuid4().hex
        self.client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=3000)
        await self.client.admin.command("ping")
        self.db = self.client[self.db_name]
        self.roles = RoleIdentityRepo(db=self.db)
        self.identity = {"_id": ObjectId(), "name": "role-a", "server": "server-a", "zone": "zone-a",
                         "game_role_id": "42", "global_id": None,
                         "updated_at": 123, "last_seen_at": 122, "profile_observed_at": 121,
                         "role_info_observed_match_time": 120}
        self.scope = {"season_id": "season-a", "season_start": 1000,
                      "pvp_type": 3, "algorithm_version": 1}
        self.locator = {key: self.identity[key] for key in
                        ("name", "server", "zone", "game_role_id", "global_id")}
        await self.db.role_identities.insert_one(deepcopy(self.identity))

    async def asyncTearDown(self):
        checked_test_uri(self.uri)
        self.assertTrue(self.db_name.startswith("jjc_kungfu_test_"))
        self.assertEqual(len(self.db_name), len("jjc_kungfu_test_") + 32)
        await self.client.drop_database(self.db_name)
        self.client.close()

    def result(self, generation):
        return dict(self.scope, publish_version=generation, name="冰心诀", kungfu_id=10081,
                    requested_at=1900.0 + generation, verified_at=1901.0 + generation)

    async def publish(self, generation, state="full", repo=None):
        return await (repo or self.roles).publish_jjc_kungfu_if_newer(
            self.identity["_id"], self.locator, generation, self.result(generation),
            display_update=display_publication_fixture(self.identity, self.scope, generation, state))

    async def test_full_partial_failure_no_win_failure_full_is_one_bounded_document(self):
        expected = {}
        for generation, state in enumerate(("full", "partial", "failed", "no_win", "failed", "full"), 1):
            with self.subTest(generation=generation, state=state):
                expected.update(display_publication_fixture(self.identity, self.scope, generation, state))
                self.assertTrue(await self.publish(generation, state))
                row = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
                self.assertEqual(row.get("jjc_display"), expected)
                self.assertEqual(row["jjc_kungfu"], self.result(generation))
        for key, value in self.identity.items():
            self.assertEqual(row[key], value)
        self.assertEqual(await self.db.role_identities_history.count_documents({}), 0)
        self.assertEqual(await self.db.jjc_sync_identity_queue.count_documents({}), 0)

    async def test_independent_clients_reject_old_display_after_new_success(self):
        other_client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=3000)
        try:
            other = RoleIdentityRepo(db=other_client[self.db_name])
            outcomes = await asyncio.gather(self.publish(1), self.publish(2, repo=other))
            self.assertTrue(outcomes[1])
            self.assertFalse(await self.publish(1))
            row = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
            self.assertEqual(row["jjc_kungfu"]["publish_version"], 2)
            self.assertEqual(row.get("jjc_display"), display_publication_fixture(self.identity, self.scope, 2))
        finally:
            other_client.close()

    async def test_reader_never_observes_new_kungfu_without_same_attempt_and_full_packet(self):
        other_client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=3000)
        try:
            observed = []
            finished = asyncio.Event()
            async def write():
                try:
                    for generation in range(1, 21):
                        self.assertTrue(await self.publish(generation))
                finally:
                    finished.set()
            async def read():
                for _ in range(200):
                    row = await other_client[self.db_name].role_identities.find_one({"_id": self.identity["_id"]})
                    if "jjc_kungfu" in row:
                        display = row.get("jjc_display", {})
                        observed.append((row["jjc_kungfu"]["publish_version"],
                            display.get("attempt", {}).get("publish_version"),
                            display.get("current", {}).get("publish_version")))
                    if finished.is_set():
                        break
            await asyncio.gather(write(), read())
            self.assertTrue(observed)
            self.assertTrue(all(kungfu == attempt == packet for kungfu, attempt, packet in observed),
                            "a reader saw a mixed-generation role publication")
        finally:
            other_client.close()

    async def test_scalar_int64_versions_work_for_atomic_display_publication(self):
        await self.db.role_identities.update_one({"_id": self.identity["_id"]},
            {"$set": {"jjc_kungfu": {"publish_version": Int64(6)}}})
        self.assertTrue(await self.publish(Int64(7)))
        row = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
        self.assertEqual(row.get("jjc_display"), display_publication_fixture(self.identity, self.scope, Int64(7)))

    async def test_identity_change_blocks_both_kungfu_and_display_without_upsert(self):
        await self.db.role_identities.update_one({"_id": self.identity["_id"]},
            {"$set": {"global_id": "changed"}})
        before = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
        self.assertFalse(await self.publish(1))
        self.assertEqual(await self.db.role_identities.find_one({"_id": self.identity["_id"]}), before)
        await self.db.role_identities.delete_one({"_id": self.identity["_id"]})
        self.assertFalse(await self.publish(1))
        self.assertEqual(await self.db.role_identities.count_documents({}), 0)

    async def test_identity_upgrade_and_query_id_repair_preserve_both_result_subobjects(self):
        display = display_publication_fixture(self.identity, self.scope, 1)
        kungfu = self.result(1)
        await self.db.role_identities.update_one({"_id": self.identity["_id"]}, {"$set": {
            "identity_key": "game:zone-a:42", "identity_level": "game_role",
            "normalized_server": "server-a", "normalized_name": "role-a",
            "jjc_display": display, "jjc_kungfu": kungfu}})
        upgraded = await self.roles.upsert_from_ranking_with_id(
            server="server-a", name="role-a", zone="zone-a", game_role_id="42",
            role_id="42", global_role_id="SK01-a", global_id="90042")
        self.assertEqual(upgraded["_id"], self.identity["_id"])
        self.assertEqual(upgraded["identity_key"], "global_id:90042")
        self.assertEqual(upgraded["jjc_display"], display)
        self.assertEqual(upgraded["jjc_kungfu"], kungfu)
        self.assertTrue(await self.roles.replace_query_ids_if_unchanged(
            upgraded, game_role_id="420", role_id="420", observed_at=datetime.now(timezone.utc)))
        repaired = await self.roles.get_by_id(self.identity["_id"])
        self.assertEqual(repaired["game_role_id"], "420")
        self.assertEqual(repaired["jjc_display"], display)
        self.assertEqual(repaired["jjc_kungfu"], kungfu)


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit local Mongo URI required")
class TestRefreshQueueMongo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri = checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        self.db_name = "jjc_kungfu_test_" + uuid4().hex
        self.client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=3000)
        await self.client.admin.command("ping")
        self.db = self.client[self.db_name]
        self.repo = JjcKungfuRefreshRepo(db=self.db)
        self.identities = RoleIdentityRepo(db=self.db)
        self.identity = {"_id": ObjectId(), "server": "fixture-server", "zone": "fixture-zone", "name": "fixture-role", "game_role_id": "10001", "updated_at": 10.0}
        await self.db.role_identities.insert_one(deepcopy(self.identity))
        await self.repo.ensure_indexes()

    async def asyncTearDown(self):
        checked_test_uri(self.uri)
        self.assertTrue(self.db_name.startswith("jjc_kungfu_test_"))
        self.assertEqual(len(self.db_name), len("jjc_kungfu_test_") + 32)
        await self.client.drop_database(self.db_name)
        self.client.close()

    async def register(self, now=1000, scope=None):
        await self.repo.register([self.identity], scope or SCOPE, now=now)

    async def row(self):
        return await self.db.jjc_kungfu_refresh_queue.find_one({"_id": self.identity["_id"]})

    async def claim(self, now=1000, scope=None):
        return await self.repo.claim(scope or SCOPE, now=now, lease_seconds=90)

    async def publish(self, task, name="冰心诀"):
        locator = {key: self.identity[key] for key in ("server", "zone", "name", "game_role_id")}
        result = {**task["scope"], "name": name, "kungfu_id": "10080" if name == "云裳心经" else "10081", "source": "indicator", "requested_at": 1000.0, "verified_at": 1001.0, "win_sample_count": 0, "publish_version": task["generation"]}
        return await self.identities.publish_jjc_kungfu_if_newer(self.identity["_id"], locator, task["generation"], result)

    async def test_concurrent_registration_has_one_role_row(self):
        await asyncio.gather(*(self.register() for _ in range(100)))
        self.assertEqual(await self.db.jjc_kungfu_refresh_queue.count_documents({}), 1)
        self.assertEqual((await self.row())["generation"], 0)

    async def test_same_scope_relisting_preserves_running_and_retry(self):
        await self.register()
        task = await self.claim()
        await self.register(now=1050)
        current = await self.row()
        for key in ("generation", "lease_token", "lease_expires_at", "next_run_at", "status"):
            self.assertEqual(current[key], task[key], key)
        self.assertEqual(current["last_seen_in_ranking_at"], 1050)
        await self.repo.complete(task, now=1060, next_run_at=2000, error_code="network")
        await self.register(now=1040)
        current = await self.row()
        self.assertEqual(current["next_run_at"], 2000)
        self.assertEqual(current["consecutive_failures"], 1)
        self.assertEqual(current["last_seen_in_ranking_at"], 1050)

    async def test_independent_clients_compete_for_one_claim(self):
        await self.register()
        other_client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=3000)
        try:
            other = JjcKungfuRefreshRepo(db=other_client[self.db_name])
            claimed = await asyncio.gather(self.claim(), other.claim(SCOPE, now=1000))
            self.assertEqual(sum(row is not None for row in claimed), 1)
        finally:
            other_client.close()

    async def test_independent_processes_compete_for_one_claim(self):
        await self.register()
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        outcomes = context.Queue()
        processes = [context.Process(target=claim_process, args=(self.uri, self.db_name, barrier, outcomes)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                await asyncio.to_thread(process.join, 15)
                self.assertFalse(process.is_alive(), "isolated claim process did not exit")
                self.assertEqual(process.exitcode, 0)
            results = [outcomes.get(timeout=3) for _ in processes]
            self.assertEqual(sorted(results), [("ok", False), ("ok", True)])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 3)
            outcomes.close()

    async def test_not_due_and_inactive_tasks_are_not_claimed(self):
        await self.register()
        task = await self.claim()
        self.assertTrue(await self.repo.complete(task, now=1001, next_run_at=2000))
        self.assertIsNone(await self.claim(now=1999))
        self.assertIsNotNone(await self.claim(now=2000))
        await self.db.jjc_kungfu_refresh_queue.update_one({"_id": task["_id"]}, {"$set": {"status": "pending", "active_until": 2000}})
        self.assertIsNone(await self.claim(now=2000))

    async def test_expired_lease_reclaimed_and_old_completion_rejected(self):
        await self.register()
        first = await self.claim()
        self.assertFalse(await self.repo.complete(first, now=1090, next_run_at=2000))
        self.assertEqual(await self.repo.recover_expired(now=1090), 1)
        second = await self.claim(now=1090)
        self.assertGreater(second["generation"], first["generation"])
        self.assertNotEqual(second["lease_token"], first["lease_token"])
        self.assertFalse(await self.repo.complete(first, now=1091, next_run_at=3000, error_code="late"))
        self.assertEqual((await self.row())["lease_token"], second["lease_token"])

    async def test_scope_switch_revokes_old_claim_and_never_resets_generation(self):
        await self.register()
        first = await self.claim()
        new_scope = {**SCOPE, "season_id": "new-season", "season_start": 200}
        await self.register(now=1001, scope=new_scope)
        self.assertFalse(await self.repo.complete(first, now=1002, next_run_at=2000))
        second = await self.claim(now=1002, scope=new_scope)
        self.assertGreater(second["generation"], first["generation"])
        self.assertEqual(await self.db.jjc_kungfu_refresh_queue.count_documents({}), 1)

    async def test_relisting_cannot_rebuild_task_after_role_success(self):
        self.identity["jjc_kungfu"] = {"publish_version": 7}
        with self.assertRaises(QueueIntegrityError):
            await self.register()
        self.assertEqual(await self.db.jjc_kungfu_refresh_queue.count_documents({}), 0)

    async def test_queue_generation_behind_role_is_rejected(self):
        await self.register()
        self.identity["jjc_kungfu"] = {"publish_version": 7}
        with self.assertRaises(QueueIntegrityError):
            await self.register()
        self.assertEqual((await self.row())["generation"], 0)

    async def test_publish_before_ack_survives_crash_and_retry(self):
        await self.register()
        first = await self.claim()
        self.assertTrue(await self.publish(first))
        role = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
        self.assertEqual(role["updated_at"], 10.0)
        self.assertEqual((await self.row())["status"], "running")
        await self.repo.recover_expired(now=1090)
        second = await self.claim(now=1090)
        self.assertTrue(await self.publish(second))
        self.assertTrue(await self.repo.complete(second, now=1091, next_run_at=3000))
        self.assertEqual((await self.row())["status"], "pending")

    async def test_new_success_rejects_old_publish_and_old_failure(self):
        await self.register()
        first = await self.claim()
        await self.repo.recover_expired(now=1090)
        second = await self.claim(now=1090)
        self.assertTrue(await self.publish(second))
        self.assertFalse(await self.publish(first, name="云裳心经"))
        self.assertFalse(await self.repo.complete(first, now=1091, next_run_at=3000, error_code="late"))
        role = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
        self.assertEqual(role["jjc_kungfu"]["publish_version"], second["generation"])

    async def test_old_attempt_can_publish_before_new_success(self):
        await self.register()
        first = await self.claim()
        await self.repo.recover_expired(now=1090)
        second = await self.claim(now=1090)
        self.assertTrue(await self.publish(first, name="云裳心经"))
        self.assertTrue(await self.publish(second))
        self.assertFalse(await self.repo.complete(first, now=1091, next_run_at=3000))

    async def test_cross_scope_new_success_has_higher_publish_version(self):
        await self.register()
        first = await self.claim()
        new_scope = {**SCOPE, "algorithm_version": 2}
        await self.register(now=1001, scope=new_scope)
        second = await self.claim(now=1002, scope=new_scope)
        self.assertTrue(await self.publish(second))
        self.assertFalse(await self.publish(first))
        role = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
        self.assertEqual(role["jjc_kungfu"]["algorithm_version"], 2)

    async def test_publish_rejects_changed_locator_and_missing_role(self):
        await self.register()
        task = await self.claim()
        await self.db.role_identities.update_one({"_id": task["_id"]}, {"$set": {"game_role_id": "20002"}})
        self.assertFalse(await self.publish(task))
        await self.db.role_identities.delete_one({"_id": task["_id"]})
        self.assertFalse(await self.publish(task))
        self.assertEqual(await self.db.role_identities.count_documents({}), 0)

    async def test_changed_scope_order_does_not_create_duplicate_or_reset(self):
        await self.register()
        task = await self.claim()
        await self.register(now=1001, scope=dict(reversed(list(SCOPE.items()))))
        current = await self.row()
        self.assertEqual(current["lease_token"], task["lease_token"])
        self.assertEqual(current["generation"], task["generation"])

    async def test_registration_rechecks_role_when_given_stale_snapshot(self):
        await self.db.role_identities.update_one({"_id": self.identity["_id"]}, {"$set": {"jjc_kungfu": {"publish_version": 5}}})
        with self.assertRaises(QueueIntegrityError):
            await self.register()
        self.assertEqual(await self.db.jjc_kungfu_refresh_queue.count_documents({}), 0)

    async def test_existing_conflicting_index_does_not_get_dropped(self):
        await self.db.jjc_kungfu_refresh_queue.drop_index("idx_kungfu_next_run")
        await self.db.jjc_kungfu_refresh_queue.create_index([("next_run_at", -1)], name="idx_kungfu_next_run")
        with self.assertRaises(QueueIntegrityError):
            await self.repo.ensure_indexes()
        indexes = await self.db.jjc_kungfu_refresh_queue.index_information()
        self.assertEqual(indexes["idx_kungfu_next_run"]["key"], [("next_run_at", -1)])

    async def test_malformed_existing_publish_schema_is_not_overwritten(self):
        await self.register()
        task = await self.claim()
        await self.db.role_identities.update_one({"_id": task["_id"]}, {"$set": {"jjc_kungfu": {"name": "云裳心经"}}})
        self.assertFalse(await self.publish(task))

    async def test_publish_rejects_array_version_and_array_result_in_actual_mongo(self):
        locator = {key: self.identity[key] for key in ("server", "zone", "name", "game_role_id")}
        for malformed in ({"publish_version": [6]}, [{"publish_version": 6}]):
            with self.subTest(malformed=malformed):
                await self.db.role_identities.update_one({"_id": self.identity["_id"]},
                    {"$set": {"jjc_kungfu": malformed}})
                self.assertFalse(await self.identities.publish_jjc_kungfu_if_newer(
                    self.identity["_id"], locator, 7, {"publish_version": 7}))
                stored = await self.db.role_identities.find_one({"_id": self.identity["_id"]})
                self.assertEqual(stored["jjc_kungfu"], malformed)

    async def test_publish_accepts_scalar_int_and_long_versions_in_actual_mongo(self):
        locator = {key: self.identity[key] for key in ("server", "zone", "name", "game_role_id")}
        for previous in (6, Int64(6)):
            await self.db.role_identities.update_one({"_id": self.identity["_id"]},
                {"$set": {"jjc_kungfu": {"publish_version": previous}}})
            self.assertTrue(await self.identities.publish_jjc_kungfu_if_newer(
                self.identity["_id"], locator, 7, {"publish_version": 7}))

    async def test_direct_claim_ignores_backoff_and_fences_existing_holder(self):
        await self.register()
        old = await self.claim()
        direct = await self.repo.claim_identity(self.identity["_id"], SCOPE, now=1001)
        self.assertEqual(direct["generation"], old["generation"] + 1)
        self.assertNotEqual(direct["lease_token"], old["lease_token"])
        self.assertTrue(await self.publish(direct))
        self.assertFalse(await self.publish(old))
        self.assertFalse(await self.repo.complete(old, now=1002, next_run_at=9999))
        self.assertTrue(await self.repo.complete(direct, now=1002, next_run_at=9999, error_code="request_timeout"))
        retried = await self.repo.claim_identity(self.identity["_id"], SCOPE, now=1003)
        self.assertEqual(retried["generation"], direct["generation"] + 1)
        self.assertEqual(retried["status"], "running")
        self.assertIsNone(await self.repo.claim_identity(ObjectId(), SCOPE, now=1003))
        self.assertIsNone(await self.repo.claim_identity(self.identity["_id"], {**SCOPE, "season_id": "other"}, now=1003))

    async def test_direct_query_persists_success_then_failed_attempt_retains_it(self):
        from src.infra.tuilan_kungfu_client import KungfuQueryError
        from src.services.jx3.jjc_kungfu_policy import KungfuPolicy
        from src.services.jx3.jjc_kungfu_refresh import JjcKungfuRefreshService
        from tests.test_jjc_kungfu_classifier import indicator, metric, wins

        client = SimpleNamespace(attempt_timeout=25, query=AsyncMock(return_value={
            "indicator_data": indicator(metric("yunchang", 100)),
            "matches": wins(*(["bingxin"] * 10)), "requested_at": 1000.0}))
        service = JjcKungfuRefreshService(self.identities, self.repo, client,
            KungfuPolicy(SCOPE), {"bingxin": "冰心诀", "yunchang": "云裳心经"}, clock=lambda: 1000.0)
        saved = await service.query_now(self.identity)
        self.assertEqual((saved["name"], saved["publish_version"]), ("冰心诀", 1))
        stored = await self.identities.get_by_id(self.identity["_id"])
        self.assertEqual(stored["jjc_kungfu"], saved)
        self.assertEqual(stored["updated_at"], self.identity["updated_at"])
        self.assertEqual((await self.row())["status"], "pending")
        # The future normal refresh time must not defer this explicit call.
        client.query.side_effect = KungfuQueryError("request_timeout")
        self.assertIsNone(await service.query_now(stored))
        self.assertEqual((await self.identities.get_by_id(self.identity["_id"]))["jjc_kungfu"], saved)
        self.assertEqual((await self.row())["last_error_code"], "request_timeout")
        self.assertEqual((await self.row())["generation"], 2)
        self.assertEqual(client.query.await_count, 2)

    async def test_direct_query_returns_durable_result_when_ack_fails_without_retry(self):
        from src.services.jx3.jjc_kungfu_policy import KungfuPolicy
        from src.services.jx3.jjc_kungfu_refresh import JjcKungfuRefreshService
        from tests.test_jjc_kungfu_classifier import indicator, metric

        client = SimpleNamespace(attempt_timeout=25, query=AsyncMock(return_value={
            "indicator_data": indicator(metric("bingxin", 100)),
            "matches": [], "requested_at": 1000.0}))
        service = JjcKungfuRefreshService(self.identities, self.repo, client,
            KungfuPolicy(SCOPE), {"bingxin": "冰心诀"}, clock=lambda: 1000.0)
        with patch.object(JjcKungfuRefreshRepo, "complete", new_callable=AsyncMock,
                          side_effect=OSError("ack unavailable")) as ack:
            result = await service.query_now(self.identity)
            ack.assert_awaited_once()
        self.assertEqual(result["name"], "冰心诀")
        self.assertEqual(result, (await self.identities.get_by_id(self.identity["_id"]))["jjc_kungfu"])
        self.assertEqual((await self.row())["status"], "running")

    async def test_background_read_after_direct_preemption_is_discarded_without_integrity_failure(self):
        from src.services.jx3.jjc_kungfu_policy import KungfuPolicy
        from src.services.jx3.jjc_kungfu_refresh import JjcKungfuRefreshService
        from tests.test_jjc_kungfu_classifier import indicator, metric

        await self.register()
        client = SimpleNamespace(attempt_timeout=25, query=AsyncMock(return_value={
            "indicator_data": indicator(metric("bingxin", 100)),
            "matches": [], "requested_at": 1000.0}))
        service = JjcKungfuRefreshService(self.identities, self.repo, client,
            KungfuPolicy(SCOPE), {"bingxin": "冰心诀"}, clock=lambda: 1000.0)
        reading, resume = asyncio.Event(), asyncio.Event()
        original_read = RoleIdentityRepo.get_by_id
        background = None
        gate_next_read = True

        async def gated_read(repo, identity_id):
            nonlocal gate_next_read
            # Python 3.9 wait_for executes the attempt in a child Task.
            if gate_next_read:
                gate_next_read = False
                reading.set()
                await resume.wait()
            return await original_read(repo, identity_id)

        with patch.object(RoleIdentityRepo, "get_by_id", new=gated_read):
            background = asyncio.create_task(service.run_once())
            try:
                await asyncio.wait_for(reading.wait(), timeout=3)
                direct_service = JjcKungfuRefreshService(self.identities, self.repo, client, KungfuPolicy(SCOPE),
                    {"bingxin": "冰心诀"}, clock=lambda: 1000.0)
                direct = await direct_service.query_now(self.identity)
                self.assertEqual(direct["publish_version"], 2)
            finally:
                resume.set()
            self.assertTrue(await asyncio.wait_for(background, timeout=3))
        client.query.assert_awaited_once()
        row = await self.row()
        self.assertEqual((row["status"], row["generation"]), ("pending", 2))
        self.assertEqual((await self.identities.get_by_id(self.identity["_id"]))["jjc_kungfu"], direct)

    async def test_scheduling_indexes_support_claim_and_recovery(self):
        await self.register()
        indexes = await self.db.jjc_kungfu_refresh_queue.index_information()
        self.assertEqual(indexes["idx_kungfu_next_run"]["key"], [("status", 1), ("next_run_at", 1), ("_id", 1)])
        self.assertEqual(indexes["idx_kungfu_expired_lease"]["key"], [("status", 1), ("lease_expires_at", 1)])
        self.assertTrue(all("expireAfterSeconds" not in data for data in indexes.values()))
        for query in [{"status": "pending", "next_run_at": {"$lte": 1000}}, {"status": "running", "lease_expires_at": {"$lte": 1000}}]:
            plan = await self.db.command("explain", {"find": "jjc_kungfu_refresh_queue", "filter": query})
            self.assertNotIn("COLLSCAN", str(plan["queryPlanner"]["winningPlan"]))
