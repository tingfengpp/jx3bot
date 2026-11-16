"""E2 strict candidate-reader contracts. Fake query recording is not Mongo proof.

Only synthetic injected DB objects are reachable; global database fallback fails.
The real Mongo4.4 query/driver/explain tests belong to the separate mongo suite.
"""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import patch

from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID, match_doc, weapon


def path_values(value, parts):
    if not parts:
        return [value]
    if isinstance(value, list):
        return [x for item in value for x in path_values(item, parts)]
    if not isinstance(value, dict) or parts[0] not in value:
        return []
    return path_values(value[parts[0]], parts[1:])


def matches(doc, query):
    for key, value in query.items():
        if key == "$or":
            if not any(matches(doc, item) for item in value):
                return False
        elif key == "$and":
            if not all(matches(doc, item) for item in value):
                return False
        elif key in ("$expr", "$nor"):
            # Deliberately over-approximate complex server expressions and the
            # redundant ordinary-query fast exclusion. The real Mongo suites
            # prove both old-time pruning and fastOld implication/equivalence;
            # this fake only proves identity bounding and read-side contracts.
            continue
        else:
            values = path_values(doc, key.split("."))
            if isinstance(value, dict) and "$in" in value:
                if not any(item in value["$in"] for item in values):
                    return False
            elif value is None and not values:
                continue
            elif not any(value==item or (isinstance(item,list) and value in item) for item in values):
                return False
    return True


def project(doc, projection):
    if not projection:
        return deepcopy(doc)
    tree = {}
    for path, include in projection.items():
        if not include:
            continue
        if include != 1:
            raise AssertionError("Fake only supports ordinary include projections; real expressions need Mongo tests")
        target = tree
        for part in path.split("."):
            target = target.setdefault(part, {})
    def select(value, branch):
        if not branch:
            return deepcopy(value)
        if isinstance(value, list):
            return [select(item, branch) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        return {key: select(value[key], child) for key, child in branch.items() if key in value}
    return select(doc, tree)


class RecordingCursor:
    def __init__(self, rows, *, fail_after=None, exception=None):
        self.rows = rows
        self.fail_after = fail_after
        self.exception = exception
        self.offset = 0
        self.limit_calls = []
        self.max_time_values = []
        self.hint_values = []
        self.batch_size_values = []
        self.allow_winner_limit = False
        self.closed = False
        self.to_list_lengths = []

    def hint(self, value):
        self.hint_values.append(value)
        return self

    def max_time_ms(self, value):
        self.max_time_values.append(value)
        return self

    def limit(self, value):
        self.limit_calls.append(value)
        if self.allow_winner_limit and value==1:
            self.rows=self.rows[:1];return self
        raise AssertionError("Arbitrary candidate limit can silently discard the latest local win")

    def sort(self, specification, *args, **kwargs):
        if self.allow_winner_limit:
            from bson import BSON
            for field,order in reversed(specification):
                self.rows.sort(key=lambda row:BSON.encode({"_id":row[field]}) if field=="_id" else row[field],reverse=order<0)
        return self

    def batch_size(self, value):
        self.batch_size_values.append(value)
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.fail_after is not None and self.offset >= self.fail_after:
            raise self.exception or RuntimeError("synthetic-getMore-failure")
        if self.offset >= len(self.rows):
            raise StopAsyncIteration
        result = deepcopy(self.rows[self.offset]); self.offset += 1
        return result

    async def to_list(self, length=None):
        self.to_list_lengths.append(length)
        result = []
        while length is None or len(result) < length:
            try:
                result.append(await self.__anext__())
            except StopAsyncIteration:
                break
        return result

    async def close(self):
        self.closed = True


class RecordingCollection:
    def __init__(self, rows, *, error=None, fail_after=None, exception=None):
        self.rows = deepcopy(rows)
        self.error = error
        self.fail_after = fail_after
        self.exception = exception
        self.calls = []
        self.cursors = []

    def find(self, query, projection=None, **kwargs):
        if projection == {"_id": 1}:
            for index,row in enumerate(self.rows):row.setdefault("_id","physical-source-"+str(index))
        self.calls.append({"query": deepcopy(query), "projection": deepcopy(projection), "kwargs": kwargs})
        if self.error:
            raise self.error
        selected = [project(row, projection) for row in self.rows if matches(row, query)]
        cursor = RecordingCursor(selected, fail_after=self.fail_after, exception=self.exception)
        cursor.allow_winner_limit = "equipment_winner_gids_v1" in query
        self.cursors.append(cursor)
        return cursor

    def aggregate(self, pipeline, **kwargs):
        if 'equipment_winner_gids_v1' in pipeline[0].get('$match',{}):
            from tests.test_jjc_candidate_two_phase_repo import metadata_winner_union_rows
            self.calls.append({'query':deepcopy(pipeline[0]['$match']),'projection':deepcopy(pipeline[3]['$project']),'kwargs':kwargs,'pipeline':deepcopy(pipeline)})
            if self.error:raise self.error
            cursor=RecordingCursor(metadata_winner_union_rows(self,pipeline),fail_after=self.fail_after,exception=self.exception)
            self.cursors.append(cursor);return cursor
        # Only model existing identity bounding/projection. Real Mongo tests
        # prove the positive certificate, reduction and BSON edge semantics.
        for index,row in enumerate(self.rows):row.setdefault("_id","physical-source-"+str(index))
        query=pipeline[0]["$match"]
        projection=next((stage["$project"] for stage in pipeline if "$project" in stage),None)
        return self.find(query,projection,**kwargs)

    def __getattr__(self, name):
        raise AssertionError("Unexpected collection operation: " + name)


class InjectedReadOnlyDB:
    def __init__(self, **collections):
        self.collections = collections
        self.accessed = []

    def __getattr__(self, name):
        self.accessed.append(name)
        if name not in self.collections:
            raise AssertionError("New candidate path accessed forbidden collection: " + name)
        return self.collections[name]

    def __getitem__(self, name):
        return getattr(self, name)


class LocalWinCandidateRepoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.global_guard = patch("src.storage.mongo_repos.jjc_inspect_repo._get_db", side_effect=AssertionError("default database forbidden"))
        self.global_guard.start(); self.addCleanup(self.global_guard.stop)

    def repo(self, collection):
        db = InjectedReadOnlyDB(jjc_match_detail=collection)
        return JjcInspectRepo(db=db, snapshot_repo=None, participant_repo=None), db

    async def read(self, repo, ids=None, **overrides):
        kwargs = dict(season_start=SCOPE["season_start"], checked_at=CHECKED_AT, max_time_ms=777)
        kwargs.update(overrides)
        return await repo.list_local_win_candidates_batch([TARGET_GLOBAL_ID] if ids is None else ids, **kwargs)

    async def test_empty_identity_set_has_zero_database_access(self):
        collection = RecordingCollection([match_doc()]); repo, db = self.repo(collection)
        self.assertEqual([], await self.read(repo, []))
        self.assertEqual([], db.accessed)

    async def test_invalid_ids_and_limits_fail_closed_before_database_access(self):
        for ids in ([""], ["not-a-global-id"], [True], ["1"]*1001):
            collection = RecordingCollection([]); repo, db = self.repo(collection)
            with self.subTest(ids=str(ids[:2]), count=len(ids)):
                with self.assertRaises(ValueError):
                    await self.read(repo, ids)
                self.assertEqual([], db.accessed)
        for options in ({"season_start": 0}, {"checked_at": float("nan")},
                        {"checked_at": SCOPE["season_start"]-1}, {"max_time_ms": 0}, {"max_time_ms": 1501}):
            collection = RecordingCollection([]); repo, db = self.repo(collection)
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    await self.read(repo, **options)
                self.assertEqual([], db.accessed)

    async def test_tc08_authoritative_candidates_do_not_need_participant_rows(self):
        own, other = match_doc(101), match_doc(102)
        for team in ("team1", "team2"):
            for player in other["data"]["detail"][team]["players_info"]:
                player["global_id"] = str(int(player["global_id"])+100)
        collection = RecordingCollection([own, other]); repo, db = self.repo(collection)
        rows = await self.read(repo)
        self.assertEqual([101], [row["match_id"] for row in rows])
        self.assertTrue(db.accessed)
        self.assertEqual({"jjc_match_detail"}, set(db.accessed))
        self.assertTrue(collection.calls)
        for call in collection.calls:
            if "_id" in call["query"]:
                self.assertTrue(call["query"]["_id"]["$in"])
                continue
            self.assertTrue("global_id" in repr(call["query"]) or "equipment_winner_gids_v1" in call["query"])
            self.assertIn(TARGET_GLOBAL_ID, repr(call["query"]))
            self.assertNotEqual({}, call["query"])

    async def test_target_can_be_on_either_team_and_query_does_not_guess_by_name(self):
        collection = RecordingCollection([match_doc(101, target_team="team1"), match_doc(102, target_team="team2")])
        repo, unused = self.repo(collection)
        rows = await self.read(repo)
        self.assertEqual({101, 102}, {row["match_id"] for row in rows})
        for call in collection.calls:
            self.assertNotIn("role_name", repr(call["query"]))
            self.assertNotIn("role_id", repr(call["query"]))

    async def test_tc04_tc05_uncertain_time_win_or_structure_reaches_domain_validator(self):
        rows = [match_doc(100, won="false"), match_doc(101, when=CHECKED_AT+1), match_doc(102)]
        rows[2]["data"]["detail"]["team1"]["players_info"].pop()
        collection = RecordingCollection(rows); repo, unused = self.repo(collection)
        candidates = await self.read(repo)
        self.assertEqual({100, 101, 102}, {row["match_id"] for row in candidates})
        for call in collection.calls:
            self.assertNotIn("won", repr(call["query"]))
            self.assertNotIn("equipment_snapshot_hash", repr(call["query"]))
            self.assertNotIn("armors", repr(call["query"]))
            self.assertNotIn("kungfu", repr(call["query"]))
            self.assertNotIn("detail_available", repr(call["query"]))

    async def test_tc09_latest_candidate_missing_equipment_is_not_filtered_out(self):
        collection = RecordingCollection([match_doc(100, equipment="legendary"), match_doc(101, equipment=None)])
        repo, unused = self.repo(collection)
        self.assertEqual({100, 101}, {row["match_id"] for row in await self.read(repo)})

    async def test_tc11_projection_retains_equipment_and_provenance_but_not_talents(self):
        row = match_doc(); target = row["data"]["detail"]["team1"]["players_info"][0]
        target["equipment_snapshot_hash"] = "equipment-hash"
        target["talents"] = [{"private_expensive_payload": "not-required"}]
        target["talent_snapshot_hash"] = "obsolete-talents"
        collection = RecordingCollection([row]); repo, unused = self.repo(collection)
        result = await self.read(repo)
        self.assertEqual(1, len(result))
        self.assertEqual(row["data"]["match_time"], result[0]["data"]["match_time"])
        self.assertEqual(100, result[0]["data"]["detail"]["match_id"])
        players = result[0]["data"]["detail"]["team1"]["players_info"]
        self.assertEqual("equipment-hash", players[0]["equipment_snapshot_hash"])
        self.assertEqual(target["armors"], players[0]["armors"])
        self.assertNotIn("talents", repr(result))
        self.assertNotIn("talent_snapshot_hash", repr(result))
        self.assertNotIn("not-required", repr(result))

    async def test_all_candidate_pages_are_consumed_not_arbitrarily_limited(self):
        rows = [match_doc(i+1) for i in range(1241)]
        collection = RecordingCollection(rows); repo, unused = self.repo(collection)
        result = await self.read(repo)
        self.assertEqual(1241, len(result))
        self.assertEqual(1241, len({row["match_id"] for row in result}))
        self.assertTrue(collection.cursors)
        self.assertTrue(all(cursor.offset == len(cursor.rows) for cursor in collection.cursors))
        self.assertTrue(all(not cursor.limit_calls for cursor in collection.cursors if not cursor.allow_winner_limit))

    async def test_global_id_batches_are_bounded_and_shared_matches_deduplicated(self):
        # One match contains identities from two distinct 100-ID batches.
        ids = [str(int(TARGET_GLOBAL_ID)+i) for i in range(205)]
        shared = match_doc()
        shared["data"]["detail"]["team2"]["players_info"][-1]["global_id"] = ids[150]
        collection = RecordingCollection([shared]); repo, unused = self.repo(collection)
        result = await self.read(repo, ids)
        self.assertEqual([100], [row["match_id"] for row in result])
        enumeration=[call for call in collection.calls if call["projection"]=={"_id":1} and "_id" not in call["query"]]
        self.assertGreaterEqual(len(enumeration),24)
        covered={"team1":set(),"team2":set()}
        for call in enumeration:
            for side in covered:
                path="data.detail."+side+".players_info.global_id"
                if path in call["query"]:
                    batch=call["query"][path]["$in"]
                    self.assertLessEqual(len(batch),100)
                    covered[side].update(batch)
        for values in covered.values():self.assertEqual(set(ids),values)

    async def test_every_command_has_requested_server_time_limit(self):
        collection = RecordingCollection([match_doc()]); repo, unused = self.repo(collection)
        await self.read(repo, max_time_ms=777)
        self.assertTrue(collection.calls)
        for call, cursor in zip(collection.calls, collection.cursors):
            limits = cursor.max_time_values + [value for key, value in call["kwargs"].items() if key in ("max_time_ms", "maxTimeMS")]
            self.assertIn(777, limits)

    async def test_initial_find_error_is_not_an_empty_successful_search(self):
        collection = RecordingCollection([], error=RuntimeError("synthetic-find-failure")); repo, unused = self.repo(collection)
        with self.assertRaisesRegex(RuntimeError, "synthetic-find-failure"):
            await self.read(repo)

    async def test_getmore_failure_discards_partial_success_and_propagates(self):
        collection = RecordingCollection([match_doc(100), match_doc(101)], fail_after=1)
        repo, unused = self.repo(collection)
        with self.assertRaisesRegex(RuntimeError, "synthetic-getMore-failure"):
            await self.read(repo)

    async def test_external_cancellation_is_not_caught_as_missing_data(self):
        collection = RecordingCollection([match_doc()], fail_after=0, exception=asyncio.CancelledError())
        repo, unused = self.repo(collection)
        with self.assertRaises(asyncio.CancelledError):
            await self.read(repo)


class StrictEquipmentSnapshotReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_strict_equipment_reader_uses_only_injected_snapshot_database(self):
        row = {"snapshot_hash": "hash-a", "armors": [weapon()]}
        collection = RecordingCollection([row]); db = InjectedReadOnlyDB(jjc_equipment_snapshot=collection)
        repo = JjcMatchSnapshotRepo(db=db)
        with patch("src.storage.mongo_repos.jjc_match_snapshot_repo._get_db", side_effect=AssertionError("default database forbidden")):
            result = await repo.load_equipment_snapshots(["hash-a"], strict=True)
        self.assertEqual(row, result["hash-a"])
        self.assertEqual({"jjc_equipment_snapshot"}, set(db.accessed))

    async def test_snapshot_missing_is_empty_but_strict_driver_error_propagates(self):
        empty_db = InjectedReadOnlyDB(jjc_equipment_snapshot=RecordingCollection([]))
        self.assertEqual({}, await JjcMatchSnapshotRepo(db=empty_db).load_equipment_snapshots(["absent"], strict=True))
        broken_db = InjectedReadOnlyDB(jjc_equipment_snapshot=RecordingCollection([], error=RuntimeError("synthetic-snapshot-failure")))
        with self.assertRaisesRegex(RuntimeError, "synthetic-snapshot-failure"):
            await JjcMatchSnapshotRepo(db=broken_db).load_equipment_snapshots(["hash-a"], strict=True)


if __name__ == "__main__":
    unittest.main()
