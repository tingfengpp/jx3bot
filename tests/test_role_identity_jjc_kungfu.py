"""I01/I02/I03/I06: narrow result publication contract; Mongo races are separate."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bson import ObjectId

from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo, _snapshot_filter


_MISSING = object()


def field_value(doc, path):
    current = doc
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def expression_value(doc, expression):
    if isinstance(expression, str) and expression.startswith("$"):
        return field_value(doc, expression[1:])
    if not isinstance(expression, dict):
        return expression
    operator, value = next(iter(expression.items()))
    if operator == "$type":
        item = expression_value(doc, value)
        if item is _MISSING:
            return "missing"
        if type(item) is int:
            return "int"
        return {dict: "object", list: "array", bool: "bool", float: "double",
                str: "string", type(None): "null"}.get(type(item), "other")
    if operator == "$and":
        return all(expression_value(doc, item) for item in value)
    left, right = [expression_value(doc, item) for item in value]
    if operator == "$eq":
        return left == right
    if operator == "$in":
        return left in right
    if operator == "$gt":
        return left > right
    if operator == "$lt":
        return left < right
    raise AssertionError("test expression needs support for %s" % operator)


def matches(doc, query):
    for key, condition in query.items():
        if key == "$expr":
            if not expression_value(doc, condition):
                return False
            continue
        if key == "$and":
            if not all(matches(doc, term) for term in condition):
                return False
            continue
        if key == "$or":
            if not any(matches(doc, term) for term in condition):
                return False
            continue
        value = field_value(doc, key)
        if not isinstance(condition, dict):
            if value is _MISSING or value != condition:
                return False
            continue
        for op, expected in condition.items():
            if op == "$exists":
                if (value is not _MISSING) != expected:
                    return False
            elif op == "$eq":
                if value is _MISSING or value != expected:
                    return False
            elif op == "$lt":
                if value is _MISSING or isinstance(value, bool) or not isinstance(value, (int, float)) or value >= expected:
                    return False
            elif op == "$gt":
                if value is _MISSING or isinstance(value, bool) or not isinstance(value, (int, float)) or value <= expected:
                    return False
            elif op == "$gte":
                if value is _MISSING or isinstance(value, bool) or not isinstance(value, (int, float)) or value < expected:
                    return False
            elif op == "$type":
                allowed = expected if isinstance(expected, list) else [expected]
                if not any((name in ("int", "long", 16, 18) and type(value) is int)
                           or (name in ("object", 3) and isinstance(value, dict))
                           for name in allowed):
                    return False
            else:
                raise AssertionError("test matcher needs support for operator %s" % op)
    return True


class ResultCollection:
    def __init__(self, doc):
        self.doc = copy.deepcopy(doc)
        self.update_one = AsyncMock(side_effect=self.update)
        self.find_one = AsyncMock(side_effect=self.find)

    async def find(self, query, *args, **kwargs):
        return copy.deepcopy(self.doc) if self.doc is not None and matches(self.doc, query) else None

    async def update(self, query, update, *args, **kwargs):
        if self.doc is None or not matches(self.doc, query):
            return SimpleNamespace(matched_count=0, modified_count=0)
        if set(update) != {"$set"} or set(update["$set"]) != {"jjc_kungfu"}:
            raise AssertionError("publication must only set jjc_kungfu")
        self.doc["jjc_kungfu"] = copy.deepcopy(update["$set"]["jjc_kungfu"])
        return SimpleNamespace(matched_count=1, modified_count=1)


class RoleIdentityKungfuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity_id = ObjectId()
        self.before = {"_id": self.identity_id, "identity_key": "game:z:role-a",
                       "identity_level": "game_role", "server": "server-a", "name": "role-a",
                       "zone": "z", "game_role_id": "role-a", "role_id": "role-a",
                       "updated_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
                       "last_seen_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
                       "role_info_observed_match_time": 100}
        self.locator = {key: self.before[key] for key in
                        ("server", "name", "zone", "game_role_id", "role_id")}
        self.collection = ResultCollection(self.before)
        self.history = SimpleNamespace(insert_one=AsyncMock())
        self.repo = RoleIdentityRepo(db=SimpleNamespace(role_identities=self.collection,
                                                        role_identities_history=self.history))
        self.result = {"name": "冰心诀", "kungfu_id": 10081, "season_id": "season-a",
                       "season_start": 1000, "pvp_type": 3, "algorithm_version": 1,
                       "source": "indicator", "requested_at": 1900.0, "verified_at": 1901.0,
                       "win_sample_count": 0, "publish_version": 1}

    async def publish(self, generation=1, locator=None, result=None):
        method = getattr(self.repo, "publish_jjc_kungfu_if_newer", None)
        self.assertTrue(callable(method), "narrow role kungfu publication method is required")
        return await method(self.identity_id, self.locator if locator is None else locator,
                            generation, self.result if result is None else result)

    async def test_publication_changes_only_kungfu_and_does_not_archive_or_touch_identity_clock(self):
        self.assertTrue(await self.publish())
        expected = dict(self.before, jjc_kungfu=self.result)
        self.assertEqual(self.collection.doc, expected)
        self.history.insert_one.assert_not_awaited()
        query, update = self.collection.update_one.call_args.args[:2]
        self.assertNotIn("updated_at", query)
        self.assertEqual(update, {"$set": {"jjc_kungfu": self.result}})
        self.assertFalse(self.collection.update_one.call_args.kwargs.get("upsert", False))

    async def test_old_or_same_version_cannot_overwrite_success(self):
        self.collection.doc["jjc_kungfu"] = dict(self.result, publish_version=2, name="云裳心经")
        before = copy.deepcopy(self.collection.doc)
        self.assertFalse(await self.publish())
        self.assertEqual(self.collection.doc, before)
        self.result["publish_version"] = 2
        self.assertFalse(await self.publish(generation=2))
        self.assertEqual(self.collection.doc, before)

    async def test_higher_version_can_replace_different_season_success(self):
        self.collection.doc["jjc_kungfu"] = dict(self.result, season_id="old-season", publish_version=1)
        self.result["publish_version"] = 2
        self.assertTrue(await self.publish(generation=2))
        self.assertEqual(self.collection.doc["jjc_kungfu"], self.result)

    async def test_locator_change_during_query_rejects_publication(self):
        for field in self.locator:
            with self.subTest(field=field):
                self.collection.doc = dict(self.before, **{field: "changed"})
                self.assertFalse(await self.publish())
                self.assertNotIn("jjc_kungfu", self.collection.doc)

    async def test_missing_identity_is_not_recreated(self):
        self.collection.doc = None
        self.assertFalse(await self.publish())
        self.assertIsNone(self.collection.doc)

    async def test_malformed_existing_subobject_is_not_automatically_overwritten(self):
        for previous in (None, {}, {"publish_version": "0"}, {"publish_version": True},
                         {"publish_version": -1}, {"publish_version": 1.5}):
            with self.subTest(previous=previous):
                self.collection.doc = dict(self.before, jjc_kungfu=copy.deepcopy(previous))
                self.assertFalse(await self.publish())
                self.assertEqual(self.collection.doc["jjc_kungfu"], previous)

    async def test_result_version_must_match_claimed_generation(self):
        try:
            actual = await self.publish(generation=2)
        except ValueError:
            actual = False
        self.assertFalse(actual)
        self.collection.update_one.assert_not_awaited()

    async def test_null_and_missing_locator_values_are_distinct(self):
        locator = dict(self.locator, global_role_id=None)
        self.assertFalse(await self.publish(locator=locator))
        self.collection.doc["global_role_id"] = None
        self.assertTrue(await self.publish(locator=locator))

    async def test_successful_kungfu_change_does_not_invalidate_existing_identity_snapshot_cas(self):
        identity_cas = _snapshot_filter(self.before)
        self.assertTrue(await self.publish())
        self.assertTrue(matches(self.collection.doc, identity_cas))


def display_publication_fixture(identity, scope, generation, state="full"):
    """Independent valid publication input, so repo RED does not depend on builder RED."""
    locator = {key: identity[key] for key in
               ("server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")
               if key in identity}
    metadata = {"publish_version": generation, "scope": copy.deepcopy(scope),
                "identity_id": str(identity["_id"]), "locator": locator,
                "requested_at": 1900.0 + generation, "verified_at": 1901.0 + generation}
    attempt = dict(copy.deepcopy(metadata), state=state, missing_reasons={}, error_code=None)
    update = {"attempt": attempt}
    if state in ("full", "partial"):
        packet = dict(copy.deepcopy(metadata), match_id=700 + generation, match_time=1800,
                      self_kungfu_id=10021, team_ready=state == "full", weapon_ready=True,
                      weapon_name="weapon-%s" % generation, weapon_quality=5,
                      weapon_icon="https://example.invalid/weapon.png",
                      teammates=[{"role_id": "43", "kungfu_id": 10081},
                                 {"role_id": "44", "kungfu_id": 10080}] if state == "full" else [])
        update["current"] = packet
        if state == "full":
            update["last_complete"] = copy.deepcopy(packet)
        else:
            attempt.update(missing_reasons={"team": "team_unresolved"}, error_code="team_unresolved")
    elif state == "failed":
        attempt.update(missing_reasons={"team": "request_timeout", "weapon": "request_timeout"},
                       error_code="request_timeout")
    elif state == "no_win":
        update["no_win_version"] = generation
        update["no_win_context"] = {key: copy.deepcopy(metadata[key])
                                    for key in ("scope", "identity_id", "locator")}
    else:
        raise ValueError("unknown fixture state")
    return update


class DisplayResultCollection(ResultCollection):
    async def update(self, query, update, *args, **kwargs):
        if self.doc is None or not matches(self.doc, query):
            return SimpleNamespace(matched_count=0, modified_count=0)
        allowed = {"jjc_kungfu", "jjc_display.current", "jjc_display.last_complete",
                   "jjc_display.attempt", "jjc_display.no_win_version", "jjc_display.no_win_context"}
        if set(update) != {"$set"} or not set(update["$set"]).issubset(allowed):
            raise AssertionError("display publication must use only narrow allowed $set paths")
        for path, value in update["$set"].items():
            target = self.doc
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = copy.deepcopy(value)
        return SimpleNamespace(matched_count=1, modified_count=1)


class RoleIdentityDisplayPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = {"_id": ObjectId(), "name": "role-a", "server": "server-a", "zone": "zone-a",
                         "game_role_id": "42", "global_id": None, "updated_at": 100,
                         "last_seen_at": 101, "profile_observed_at": 99,
                         "role_info_observed_match_time": 98,
                         "jjc_display": {"unrelated_marker": "preserve"}}
        self.scope = {"season_id": "season-a", "season_start": 1000,
                      "pvp_type": 3, "algorithm_version": 1}
        self.locator = {key: self.identity[key] for key in
                        ("name", "server", "zone", "game_role_id", "global_id")}
        self.collection = DisplayResultCollection(self.identity)
        self.history = SimpleNamespace(insert_one=AsyncMock())
        self.repo = RoleIdentityRepo(db=SimpleNamespace(role_identities=self.collection,
                                                       role_identities_history=self.history))

    def result(self, generation):
        return dict(self.scope, publish_version=generation, name="冰心诀", kungfu_id=10081,
                    requested_at=1900.0 + generation, verified_at=1901.0 + generation)

    async def publish(self, generation, update):
        return await self.repo.publish_jjc_kungfu_if_newer(
            self.identity["_id"], self.locator, generation, self.result(generation), display_update=update)

    async def test_full_partial_failure_no_win_failure_full_preserves_bounded_packets(self):
        expected_display = copy.deepcopy(self.identity["jjc_display"])
        expected_paths = {"jjc_kungfu"}
        for generation, state in enumerate(("full", "partial", "failed", "no_win", "failed", "full"), 1):
            with self.subTest(generation=generation, state=state):
                update = display_publication_fixture(self.identity, self.scope, generation, state)
                self.assertTrue(await self.publish(generation, update))
                expected_display.update(copy.deepcopy(update))
                self.assertEqual(self.collection.doc.get("jjc_display"), expected_display)
                self.assertEqual(self.collection.doc["jjc_kungfu"], self.result(generation))
                query, mutation = self.collection.update_one.call_args.args[:2]
                self.assertEqual(set(mutation["$set"]), expected_paths | {"jjc_display." + key for key in update})
                self.assertNotIn("updated_at", query)
                self.assertFalse(self.collection.update_one.call_args.kwargs.get("upsert", False))
        for key in self.identity:
            if key != "jjc_display":
                self.assertEqual(self.collection.doc[key], self.identity[key])
        self.collection.find_one.assert_not_awaited()
        self.history.insert_one.assert_not_awaited()

    async def test_legacy_none_keeps_existing_display_and_exact_old_mutation(self):
        before = copy.deepcopy(self.collection.doc["jjc_display"])
        self.assertTrue(await self.publish(1, None))
        self.assertEqual(self.collection.doc["jjc_display"], before)
        self.assertEqual(self.collection.update_one.call_args.args[1], {"$set": {"jjc_kungfu": self.result(1)}})

    async def test_partial_packet_reasons_survive_later_failed_attempt(self):
        from src.services.jx3.jjc_ranking_display import build_display_update
        from tests.test_jjc_ranking_display import source_evidence
        evidence = source_evidence()
        evidence["requested_at"] = self.result(1)["requested_at"]
        evidence["display_evidence"]["match_time"] = 1800
        evidence["display_evidence"]["detail_data"]["team1"]["players_info"][0]["armors"] = []
        partial = build_display_update(self.identity, self.scope, 1, evidence, self.result(1)["verified_at"])
        self.assertTrue(await self.publish(1, partial))
        packet = copy.deepcopy(self.collection.doc["jjc_display"]["current"])
        self.assertEqual(packet.get("missing_reasons"), {"weapon": "weapon_incomplete"})
        self.assertTrue(await self.publish(2, display_publication_fixture(self.identity, self.scope, 2, "failed")))
        self.assertEqual(self.collection.doc["jjc_display"]["current"], packet)
        self.assertEqual(self.collection.doc["jjc_display"]["attempt"]["error_code"], "request_timeout")

    async def test_first_partial_publishes_without_inventing_a_complete_packet(self):
        update = display_publication_fixture(self.identity, self.scope, 1, "partial")
        self.assertTrue(await self.publish(1, update))
        self.assertEqual(self.collection.doc.get("jjc_display", {}).get("current"), update["current"])
        self.assertNotIn("last_complete", self.collection.doc["jjc_display"])
        self.assertNotIn("no_win_version", self.collection.doc["jjc_display"])

    async def test_invalid_display_binding_and_unapproved_fields_never_reach_database(self):
        base = display_publication_fixture(self.identity, self.scope, 1)
        mutations = []
        for key, value in (("publish_version", 2), ("publish_version", True),
                           ("identity_id", str(ObjectId())), ("scope", dict(self.scope, season_id="other")),
                           ("locator", dict(self.locator, global_id="changed")),
                           ("verified_at", float("nan")), ("requested_at", 9999)):
            for child in ("attempt", "current", "last_complete"):
                update = copy.deepcopy(base)
                update[child][key] = value
                mutations.append((child + "." + key, update))
        injected = copy.deepcopy(base)
        injected["updated_at"] = 999
        mutations.append(("unapproved root key", injected))
        inconsistent = copy.deepcopy(base)
        inconsistent["attempt"]["state"] = "failed"
        mutations.append(("failed with new packet", inconsistent))
        for label, update in mutations:
            with self.subTest(label=label):
                self.collection.update_one.reset_mock()
                try:
                    accepted = await self.publish(1, update)
                except ValueError:
                    accepted = False
                self.assertFalse(accepted, "invalid publication metadata must not be accepted")
                self.collection.update_one.assert_not_awaited()

    async def test_old_version_or_changed_locator_cannot_publish_any_display_subfield(self):
        newer = display_publication_fixture(self.identity, self.scope, 2)
        self.assertTrue(await self.publish(2, newer))
        self.assertEqual(self.collection.doc.get("jjc_display", {}).get("attempt"), newer["attempt"])
        before = copy.deepcopy(self.collection.doc)
        self.assertFalse(await self.publish(1, display_publication_fixture(self.identity, self.scope, 1)))
        self.assertEqual(self.collection.doc, before)
        self.collection.doc["global_id"] = "changed"
        before = copy.deepcopy(self.collection.doc)
        self.assertFalse(await self.publish(3, display_publication_fixture(self.identity, self.scope, 3)))
        self.assertEqual(self.collection.doc, before)
