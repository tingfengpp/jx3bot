"""T2 source-packet normalization: pure fixtures, no service or upstream calls."""
from copy import deepcopy
import unittest

from bson import ObjectId
from bson.int64 import Int64

from src.services.jx3.jjc_ranking_display import build_display_update


DISPLAY_SCOPE = {"season_id": "season-a", "season_start": 100,
                 "pvp_type": 3, "algorithm_version": 1}
LOCATOR_FIELDS = ("server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")


def display_identity():
    return {"_id": ObjectId(), "server": "server-a", "name": "role-a", "zone": "zone-a",
            "game_role_id": "42", "role_id": "42", "global_role_id": "SK01-a"}


def source_evidence():
    return {
        "requested_at": 300.0, "verified_at": 301.0,
        "indicator_data": {"role_info": {"role_id": "42", "global_role_id": "SK01-a",
                                         "server": "server-a", "zone": "zone-a"}},
        "matches": [{"match_id": 700, "match_time": 200, "won": True}],
        "display_evidence": {"status": "ready", "match_id": 700, "match_time": 200,
            "detail_data": {"team1": {"players_info": [
                {"role_id": "42", "server": "server-a", "role_name": "role-a",
                 "kungfu_id": 10021,
                 "armors": [{"name": "weapon-a", "quality": 5,
                             "icon": "https://example.invalid/weapon.png"}]},
                {"role_id": "43", "role_name": "mate-b", "server": "server-a",
                 "kungfu_id": 10081},
                {"role_id": "44", "role_name": "mate-c", "server": "server-a",
                 "kungfu_id": 10080},
            ]}, "team2": {"players_info": [
                {"role_id": "99", "role_name": "role-a", "server": "server-b",
                 "kungfu_id": 10175},
            ]}}},
    }


class DisplayPacketNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.identity = display_identity()
        self.evidence = source_evidence()

    def build(self, generation=7, evidence=None, identity=None, verified_at=301.0, scope=None):
        return build_display_update(self.identity if identity is None else identity,
            DISPLAY_SCOPE if scope is None else scope, generation,
            self.evidence if evidence is None else evidence, verified_at)

    def attempt(self, update, state):
        self.assertIn("attempt", update, "every enabled attempt needs an explicit outcome")
        attempt = update["attempt"]
        self.assertEqual(attempt["state"], state)
        self.assertEqual(attempt["publish_version"], 7)
        self.assertEqual(attempt["scope"], DISPLAY_SCOPE)
        self.assertEqual(attempt["identity_id"], str(self.identity["_id"]))
        self.assertEqual(attempt["locator"], {key: self.identity[key] for key in LOCATOR_FIELDS
                                              if key in self.identity})
        self.assertEqual((attempt["requested_at"], attempt["verified_at"]), (300.0, 301.0))
        return attempt

    def test_full_packet_keeps_match_self_kungfu_and_complete_source_metadata(self):
        before = deepcopy((self.identity, self.evidence))
        update = self.build()
        attempt = self.attempt(update, "full")
        self.assertEqual(attempt["missing_reasons"], {})
        self.assertFalse(attempt.get("error_code"))
        packet = update["current"]
        self.assertEqual(update["last_complete"], packet)
        for key in ("publish_version", "scope", "identity_id", "locator", "requested_at", "verified_at"):
            self.assertEqual(packet[key], attempt[key])
        self.assertEqual((packet["match_id"], packet["match_time"]), (700, 200))
        self.assertEqual(packet["self_kungfu_id"], 10021)
        self.assertTrue(packet["team_ready"])
        self.assertTrue(packet["weapon_ready"])
        self.assertEqual(packet["weapon_name"], "weapon-a")
        self.assertEqual(str(packet["weapon_quality"]), "5")
        self.assertEqual(packet["weapon_icon"], "https://example.invalid/weapon.png")
        self.assertEqual({mate["kungfu_id"] for mate in packet["teammates"]}, {10081, 10080})
        self.assertEqual({mate["role_id"] for mate in packet["teammates"]}, {"43", "44"})
        self.assertNotIn("no_win_version", update)
        self.assertEqual((self.identity, self.evidence), before)

    def test_team_ready_does_not_require_teammate_weapons(self):
        update = self.build()
        self.attempt(update, "full")
        for mate in update["current"]["teammates"]:
            self.assertFalse(mate.get("weapon_name"))
            self.assertFalse(mate.get("weapon_quality"))

    def test_role_id_alias_is_preserved_as_canonical_teammate_identity(self):
        team = self.evidence["display_evidence"]["detail_data"]["team1"]["players_info"]
        for player in team[1:]:
            player["roleId"] = player.pop("role_id")
        update = self.build()
        self.assertEqual(update["attempt"]["state"], "full")
        self.assertEqual([mate.get("role_id") for mate in update["current"]["teammates"]], ["43", "44"])

    def test_local_role_ids_are_scoped_but_global_ids_still_detect_duplicates(self):
        for kind, expected in (("different_zone", "full"), ("same_zone", "partial"),
                               ("same_global", "partial"), ("same_sk01", "partial"),
                               ("mixed_scope_duplicate", "partial")):
            with self.subTest(kind=kind):
                evidence = source_evidence()
                team = evidence["display_evidence"]["detail_data"]["team1"]["players_info"]
                team[1].update(role_id="43", zone="zone-a", server="server-a", global_id="90043")
                team[2].update(role_id="43", zone="zone-b", server="server-b", global_id="90044")
                if kind == "same_zone":
                    team[2]["zone"] = "zone-a"
                if kind == "same_global":
                    team[2].update(role_id="44", global_id="90043")
                if kind == "same_sk01":
                    team[1]["global_role_id"] = team[2]["global_role_id"] = "SK01-mate"
                    team[2]["role_id"] = "44"
                if kind == "mixed_scope_duplicate":
                    team[1].pop("zone")
                    team[2].update(zone="zone-a", server="server-a")
                update = self.build(evidence=evidence)
                self.assertEqual(update["attempt"]["state"], expected)
                self.assertEqual(len(update["current"]["teammates"]), 2 if expected == "full" else 0)
                if expected == "full":
                    self.assertEqual([mate.get("zone") for mate in update["current"]["teammates"]], ["zone-a", "zone-b"])

    def test_unscoped_local_id_without_any_global_id_is_not_a_stable_teammate(self):
        team = self.evidence["display_evidence"]["detail_data"]["team1"]["players_info"]
        team[1].pop("server")
        self.assertEqual(self.build()["attempt"]["state"], "partial")

    def test_partial_packet_keeps_its_own_missing_reasons(self):
        self.evidence["display_evidence"]["detail_data"]["team1"]["players_info"][0]["armors"] = []
        update = self.build()
        self.assertEqual(update["attempt"]["state"], "partial")
        self.assertEqual(update["current"].get("missing_reasons"), update["attempt"]["missing_reasons"])
        update["attempt"]["missing_reasons"].clear()
        self.assertTrue(update["current"]["missing_reasons"])

    def test_missing_each_weapon_component_yields_team_only_partial(self):
        for field, value in (("name", ""), ("quality", None), ("icon", ""),
                             ("icon", "javascript:alert(1)")):
            with self.subTest(field=field, value=value):
                evidence = source_evidence()
                weapon = evidence["display_evidence"]["detail_data"]["team1"]["players_info"][0]["armors"][0]
                weapon[field] = value
                update = self.build(evidence=evidence)
                attempt = self.attempt(update, "partial")
                self.assertIn("weapon", attempt["missing_reasons"])
                self.assertTrue(update["current"]["team_ready"])
                self.assertFalse(update["current"]["weapon_ready"])
                self.assertNotIn("last_complete", update)

    def test_unknown_missing_or_duplicate_teammates_yield_weapon_only_partial(self):
        for defect in ("unknown_kungfu", "missing_id", "duplicate_id", "extra_player", "missing_player"):
            with self.subTest(defect=defect):
                evidence = source_evidence()
                team = evidence["display_evidence"]["detail_data"]["team1"]["players_info"]
                if defect == "unknown_kungfu":
                    team[1]["kungfu_id"] = 999999999
                elif defect == "missing_id":
                    team[1].pop("role_id")
                elif defect == "duplicate_id":
                    team[2]["role_id"] = team[1]["role_id"]
                elif defect == "extra_player":
                    team.append({"role_id": "45", "kungfu_id": 10003})
                else:
                    team.pop()
                update = self.build(evidence=evidence)
                attempt = self.attempt(update, "partial")
                self.assertIn("team", attempt["missing_reasons"])
                self.assertFalse(update["current"]["team_ready"])
                self.assertTrue(update["current"]["weapon_ready"])
                self.assertFalse(update["current"].get("teammates"))
                self.assertNotIn("last_complete", update)

    def test_names_alone_and_conflicting_ids_never_select_a_target(self):
        for defect in ("name_only", "conflicting_role", "duplicate_target"):
            with self.subTest(defect=defect):
                evidence = source_evidence()
                detail = evidence["display_evidence"]["detail_data"]
                target = detail["team1"]["players_info"][0]
                if defect == "name_only":
                    target.pop("role_id")
                elif defect == "conflicting_role":
                    target.update(role_id="wrong", global_role_id="SK01-a")
                else:
                    detail["team2"]["players_info"].append(deepcopy(target))
                update = self.build(evidence=evidence)
                self.attempt(update, "failed")
                self.assertNotIn("current", update)
                self.assertNotIn("last_complete", update)

    def test_indicator_and_replay_can_supply_strong_identity_without_mutation(self):
        identity = deepcopy(self.identity)
        identity.pop("global_role_id")
        for mapping in ("indicator", "replay"):
            with self.subTest(mapping=mapping):
                evidence = source_evidence()
                target = evidence["display_evidence"]["detail_data"]["team1"]["players_info"][0]
                target.pop("role_id")
                if mapping == "indicator":
                    target["global_role_id"] = "SK01-a"
                else:
                    target["global_id"] = "90042"
                    evidence["display_evidence"]["replay_data"] = {"players": [
                        {"role_id": "42", "role_name": "role-a·server-a", "global_id": "90042"}]}
                before = deepcopy((identity, evidence))
                update = self.build(identity=identity, evidence=evidence)
                self.assertEqual(update.get("attempt", {}).get("state"), "full")
                self.assertEqual(update["current"]["locator"], {key: identity[key] for key in LOCATOR_FIELDS if key in identity})
                self.assertEqual((identity, evidence), before)

    def test_replay_timeout_still_allows_independently_verified_detail(self):
        self.evidence["display_evidence"].update(status="failed", error_code="request_timeout")
        update = self.build()
        self.assertEqual(update.get("attempt", {}).get("state"), "full")
        self.assertTrue(update["current"]["team_ready"])
        self.assertTrue(update["current"]["weapon_ready"])

    def test_failure_preserves_specific_control_error_without_publishing_a_packet(self):
        for error, auth, retry_after in (("request_timeout", False, None),
                                        ("authentication_failed", True, 300),
                                        ("rate_limited", False, 17)):
            with self.subTest(error=error):
                evidence = source_evidence()
                evidence["display_evidence"] = {"status": "failed", "error_code": error,
                                                "auth": auth, "retry_after": retry_after}
                update = self.build(evidence=evidence)
                attempt = self.attempt(update, "failed")
                self.assertEqual(attempt["error_code"], error)
                self.assertEqual(attempt.get("auth", False), auth)
                self.assertEqual(attempt.get("retry_after"), retry_after)
                self.assertNotIn("current", update)
                self.assertNotIn("last_complete", update)
                self.assertNotIn("no_win_version", update)

    def test_no_win_only_advances_bound_suppression_and_attempt(self):
        self.evidence["matches"] = []
        self.evidence["display_evidence"] = {"status": "no_win"}
        update = self.build()
        self.attempt(update, "no_win")
        self.assertEqual(update["no_win_version"], 7)
        self.assertEqual(update["no_win_context"], {
            "scope": DISPLAY_SCOPE, "identity_id": str(self.identity["_id"]),
            "locator": {key: self.identity[key] for key in LOCATOR_FIELDS if key in self.identity}})
        self.assertNotIn("current", update)
        self.assertNotIn("last_complete", update)

    def test_invalid_match_provenance_cannot_create_an_available_packet(self):
        for field, bad in (("match_id", 0), ("match_id", True), ("match_time", 99),
                           ("match_time", 302), ("match_time", float("nan"))):
            with self.subTest(field=field, bad=bad):
                evidence = source_evidence()
                evidence["display_evidence"][field] = bad
                update = self.build(evidence=evidence)
                self.attempt(update, "failed")
                self.assertNotIn("current", update)

    def test_invalid_generation_scope_or_attempt_times_are_rejected(self):
        for generation in (0, -1, True, 1.5, "7"):
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                self.build(generation=generation)
        for verified in (299, float("nan"), float("inf"), True):
            with self.subTest(verified=verified), self.assertRaises(ValueError):
                self.build(verified_at=verified)
        with self.assertRaises(ValueError):
            self.build(scope=dict(DISPLAY_SCOPE, pvp_type=2))

    def test_bson_int64_generation_and_null_locator_are_preserved(self):
        self.identity["global_id"] = None
        update = self.build(generation=Int64(7))
        self.attempt(update, "full")
        self.assertIn("global_id", update["current"]["locator"])
        self.assertIsNone(update["current"]["locator"]["global_id"])

    def test_malformed_enabled_evidence_is_failed_instead_of_disabled(self):
        for value in (None, [], "ready"):
            with self.subTest(value=value):
                evidence = source_evidence()
                evidence["display_evidence"] = value
                update = self.build(evidence=evidence)
                self.attempt(update, "failed")
                self.assertNotIn("current", update)
                self.assertNotIn("no_win_version", update)

    def test_no_win_cannot_override_an_observed_current_season_win(self):
        self.evidence["display_evidence"] = {"status": "no_win"}
        update = self.build()
        self.attempt(update, "failed")
        self.assertNotIn("no_win_version", update)

    def test_explicit_detail_or_replay_match_id_conflict_rejects_packet(self):
        for source in ("detail_data", "replay_data"):
            with self.subTest(source=source):
                evidence = source_evidence()
                if source == "replay_data":
                    evidence["display_evidence"][source] = {"players": []}
                evidence["display_evidence"][source]["match_id"] = 701
                update = self.build(evidence=evidence)
                self.attempt(update, "failed")
                self.assertNotIn("current", update)
