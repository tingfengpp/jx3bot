"""E1 business contracts: pure local wins, equipment, aggregation and revocation.

TC01-18 cover the domain portion; Mongo/driver/query implementation belongs to E2.
No Mongo clients or external requests are created here. Missing imports are not RED.
"""
from copy import deepcopy
import random
import unittest

import config
from bson.int64 import Int64
from src.services.jx3.jjc_ranking_equipment import (
    validate_local_win, classify_equipment, aggregate_equipment_view,
    public_equipment_view, apply_revocations,
)
from tests.jjc_equipment_fixtures import (
    SCOPE, CHECKED_AT, TIMESTAMP, MEMBER_REVISION, LEGENDARY_NAME, ICON,
    TARGET_GLOBAL_ID, weapon, frozen_member, match_doc, equipped_member,
    ten_member_golden, revoked,
)


def validate(rows, member=None, **kwargs):
    return validate_local_win(member=member or frozen_member(), candidates=rows,
                             scope=deepcopy(SCOPE), checked_at=CHECKED_AT, **kwargs)


def aggregate(members, **kwargs):
    options = dict(timestamp=TIMESTAMP, scope=deepcopy(SCOPE), members=members,
                   checked_at=CHECKED_AT, completed_at=CHECKED_AT + .1,
                   member_revision=MEMBER_REVISION)
    options.update(kwargs)
    return aggregate_equipment_view(**options)


class LocalWinningMatchPolicyTests(unittest.TestCase):
    def test_tc01_latest_loss_does_not_replace_latest_win(self):
        rows = [match_doc(90, when=CHECKED_AT-900),
                match_doc(110, when=CHECKED_AT-100, won=False),
                match_doc(100, when=CHECKED_AT-400)]
        result = validate(rows)
        self.assertIn(result["state"], ("available", "partial"))
        self.assertEqual(100, result["packet"]["match_id"])
        self.assertEqual(CHECKED_AT-400, result["packet"]["match_time"])

    def test_tc01_local_selection_has_no_forty_match_sampling_limit(self):
        rows = [match_doc(i+1, when=CHECKED_AT-500+i, won=False) for i in range(40)]
        rows.append(match_doc(90, when=CHECKED_AT-600))
        self.assertEqual(90, validate(rows)["packet"]["match_id"])

    def test_tc02_same_time_uses_match_id_independent_of_input_order(self):
        rows = [match_doc(100), match_doc(101)]
        self.assertEqual(101, validate(rows)["packet"]["match_id"])
        self.assertEqual(101, validate(list(reversed(rows)))["packet"]["match_id"])

    def test_tc03_successful_empty_and_all_losses_are_local_absence(self):
        for rows in ([], [match_doc(won=False)]):
            with self.subTest(rows=bool(rows)):
                result = validate(rows)
                self.assertEqual("no_local_win", result["state"])
                self.assertIsNone(result["packet"])

    def test_tc04_start_boundary_is_inclusive_and_previous_season_excluded(self):
        self.assertEqual(101, validate([match_doc(100, when=SCOPE["season_start"]-1),
                                       match_doc(101, when=SCOPE["season_start"])])["packet"]["match_id"])
        self.assertEqual("no_local_win", validate([match_doc(100, when=SCOPE["season_start"]-1)])["state"])

    def test_tc04_future_invalid_time_and_ids_cannot_be_winning_packets(self):
        cases = []
        for when in (CHECKED_AT+1, float("nan"), float("inf"), "not-a-time"):
            cases.append(match_doc(100, when=when))
        for match_id in (0, -1, True, "not-an-id"):
            cases.append(match_doc(match_id))
        missing = match_doc(); missing["data"].pop("match_time"); cases.append(missing)
        for row in cases:
            with self.subTest(row=row):
                result = validate([row])
                self.assertNotIn(result["state"], ("available", "partial"))
                self.assertIsNone(result["packet"])

    def test_tc04_explicit_scope_or_match_provenance_conflict_not_published(self):
        mutations = [("season_id", "another-season"), ("match_id", 999), ("pvp_type", 2)]
        for key, value in mutations:
            row = match_doc(); row["data"]["detail"][key] = value
            with self.subTest(key=key):
                self.assertIsNone(validate([row])["packet"])

    def test_tc05_winner_must_be_the_target_team_on_either_side(self):
        for side in ("team1", "team2"):
            with self.subTest(side=side):
                self.assertIsNone(validate([match_doc(target_team=side, won=False)])["packet"])
                self.assertIsNotNone(validate([match_doc(target_team=side, won=True)])["packet"])

    def test_tc05_non_boolean_win_is_never_coerced_to_victory(self):
        for won in ("false", "true", 1, 0, None, ""):
            with self.subTest(won=won):
                result = validate([match_doc(won=won)])
                self.assertIsNone(result["packet"])
                self.assertNotEqual("available", result["state"])

    def test_tc06_rosters_must_have_three_players_per_team_and_unique_stable_ids(self):
        malformed = []
        row = match_doc(); row["data"]["detail"]["team1"]["players_info"].pop(); malformed.append(row)
        row = match_doc(); detail = row["data"]["detail"]
        detail["team2"]["players_info"].append(detail["team1"]["players_info"].pop()); malformed.append(row)
        row = match_doc(); detail = row["data"]["detail"]
        detail["team2"]["players_info"][0]["global_id"] = TARGET_GLOBAL_ID; malformed.append(row)
        row = match_doc(); row["data"]["detail"]["team1"]["players_info"][1].pop("global_id"); malformed.append(row)
        row = match_doc(); row["data"]["detail"]["basic_info"]["match_type"] = 2; malformed.append(row)
        for row in malformed:
            with self.subTest(row=row):
                self.assertIsNone(validate([row])["packet"])

    def test_tc07_name_and_local_role_id_cannot_substitute_stable_global_identity(self):
        row = match_doc()
        impostor = row["data"]["detail"]["team1"]["players_info"][0]
        impostor.update(global_id="799999999999999999", role_name=frozen_member()["name"], role_id="101")
        self.assertEqual("no_local_win", validate([row])["state"])
        member = frozen_member(); member.pop("global_id")
        self.assertEqual("binding_invalid", validate([match_doc()], member)["state"])

    def test_tc08_canonical_detail_without_participant_projection_is_sufficient(self):
        row = match_doc(200)
        self.assertNotIn("participants", row)
        self.assertEqual(200, validate([row])["packet"]["match_id"])

    def test_tc09_latest_win_missing_weapon_keeps_that_match_and_teammates(self):
        rows = [match_doc(100, when=CHECKED_AT-200, equipment="legendary"),
                match_doc(101, when=CHECKED_AT-100, equipment=None)]
        result = validate(rows)
        self.assertEqual("partial", result["state"])
        self.assertEqual(101, result["packet"]["match_id"])
        self.assertEqual("unknown", result["packet"]["equipment_state"])
        self.assertEqual(2, len(result["packet"]["teammates"]))
        self.assertIsNone(result["packet"].get("weapon_name"))

    def test_tc10_source_kungfu_does_not_mutate_frozen_classification(self):
        member, rows = frozen_member(), [match_doc()]
        original = deepcopy((member, rows))
        packet = validate(rows, member)["packet"]
        self.assertEqual(10144, packet["self_kungfu_id"])
        self.assertIn(10626, [mate["kungfu_id"] for mate in packet["teammates"]])
        self.assertEqual(original, (member, rows))
        self.assertEqual(10145, member["kungfu_id"])

    def test_tc10_unknown_teammate_kungfu_preserves_confirmed_identity(self):
        for unknown_id in (None, 99999999):
            row = match_doc()
            mate = row["data"]["detail"]["team1"]["players_info"][1]
            mate["kungfu_id"] = unknown_id
            with self.subTest(unknown_id=unknown_id):
                packet = validate([row])["packet"]
                self.assertEqual(2, len(packet["teammates"]))
                self.assertIn(mate["role_name"], [p["role_name"] for p in packet["teammates"]])

    def test_tc11_hash_hydration_is_pure_and_independent_of_talent_schema(self):
        row = match_doc(); target = row["data"]["detail"]["team1"]["players_info"][0]
        target.pop("armors"); target["equipment_snapshot_hash"] = "synthetic-equipment-hash"
        target["talent_snapshot_hash"] = "obsolete-talents-not-loaded"
        snapshots = {"synthetic-equipment-hash": {"armors": [weapon("legendary")]}}
        original = deepcopy((row, snapshots))
        packet = validate([row], equipment_snapshots=snapshots)["packet"]
        self.assertEqual("legendary", packet["equipment_state"])
        self.assertEqual(LEGENDARY_NAME, packet["weapon_name"])
        self.assertEqual(original, (row, snapshots))


    def test_tc06_explicit_type_alias_is_not_overridden_by_six_player_inference(self):
        for source_name in ("data", "detail", "basic_info"):
            row = match_doc(); detail = row["data"]["detail"]
            detail["basic_info"].pop("match_type")
            source = {"data": row["data"], "detail": detail, "basic_info": detail["basic_info"]}[source_name]
            source["type"] = 2
            with self.subTest(source=source_name):
                result = validate([row])
                self.assertIsNone(result["packet"], "Explicit 2v2 must not become inferred 3v3")

    def test_tc04_conflicting_canonical_time_aliases_cannot_prove_latest_win(self):
        row = match_doc(100, when=CHECKED_AT-100)
        row["data"]["detail"]["basic_info"]["start_time"] = CHECKED_AT-1000
        result = validate([row])
        self.assertEqual("data_inconsistent", result["state"])
        self.assertIsNone(result["packet"])

    def test_malformed_detail_containers_do_not_crash_whole_board_policy(self):
        rows = []
        row = match_doc(); row["data"] = ["malformed-data"]; rows.append(row)
        row = match_doc(); row["data"]["detail"]["team1"] = ["malformed-team"]; rows.append(row)
        for row in rows:
            with self.subTest(row=row):
                result = validate([row])
                self.assertIn(result["state"], ("no_local_win", "data_inconsistent"))
                self.assertIsNone(result["packet"])

    def test_invalid_snapshot_hash_is_a_missing_weapon_not_an_unhashable_exception(self):
        row = match_doc(); target = row["data"]["detail"]["team1"]["players_info"][0]
        target["armors"] = []
        target["equipment_snapshot_hash"] = ["not-a-valid-hash"]
        result = validate([row])
        self.assertEqual("partial", result["state"])
        self.assertEqual("unknown", result["packet"]["equipment_state"])
        self.assertEqual(2, len(result["packet"]["teammates"]))

    def test_old_uncertain_win_does_not_hide_a_provably_later_valid_win(self):
        old = match_doc(100, when=CHECKED_AT-1000, won="true")
        latest = match_doc(101, when=CHECKED_AT-100)
        result = validate([old, latest])
        self.assertEqual(101, (result.get("packet") or {}).get("match_id"))


class EquipmentClassificationTests(unittest.TestCase):
    def test_tc12_all_existing_allowlisted_names_require_quality_five(self):
        self.assertTrue(config.JJC_LEGENDARY_WEAPON_NAMES)
        for name in config.JJC_LEGENDARY_WEAPON_NAMES:
            for quality in (5, "5"):
                with self.subTest(name=name, quality=quality):
                    self.assertEqual("legendary", classify_equipment(weapon(name=name, quality=quality))["equipment_state"])
            self.assertEqual("purple", classify_equipment(weapon(name=name, quality=4))["equipment_state"])

    def test_tc12_complete_non_allowlisted_quality_five_uses_existing_non_legendary_rule(self):
        self.assertEqual("purple", classify_equipment(weapon(name="合成非白名单武器", quality=5))["equipment_state"])

    def test_tc13_missing_placeholder_or_invalid_weapon_is_unknown_not_purple(self):
        cases = [None, {}, weapon(name=""), weapon(name="未知"), weapon(name="unknown"),
                 weapon(name="x"*257), weapon(quality=True), weapon(quality=False),
                 weapon(quality=None), weapon(quality=0), weapon(quality=6), weapon(quality="garbage")]
        for item in cases:
            with self.subTest(item=item):
                result = classify_equipment(item)
                self.assertEqual("unknown", result["equipment_state"])
                self.assertTrue(result["missing_reasons"])

    def test_tc14_missing_or_unsafe_icon_does_not_change_known_equipment_state(self):
        for state in ("legendary", "purple"):
            for icon in (None, "", "javascript:alert(1)", "data:text/html,unsafe"):
                with self.subTest(state=state, icon=icon):
                    result = classify_equipment(weapon(state, icon=icon))
                    self.assertEqual(state, result["equipment_state"])
                    self.assertIsNone(result["weapon_icon"])
                    self.assertTrue(result["missing_reasons"])

    def test_classification_does_not_mutate_supplied_weapon(self):
        item = weapon("legendary", icon=None); original = deepcopy(item)
        classify_equipment(item)
        self.assertEqual(original, item)


class EquipmentAggregationTests(unittest.TestCase):
    def test_tc15_golden_whole_view_has_two_legendary_five_purple_three_unknown(self):
        members = ten_member_golden(); original = deepcopy(members)
        view = aggregate(members)
        self.assertEqual(2, view["schema_version"])
        self.assertEqual("local_win_v2", view["source"])
        for key in ("top_50", "top_100", "top_200", "top_1000"):
            with self.subTest(key=key):
                stats = view["ranges"][key]
                self.assertEqual((10, 2, 5, 3), tuple(stats[k] for k in ("N", "L", "P", "U")))
                self.assertAlmostEqual(.2, stats["legendary_ratio"])
                self.assertAlmostEqual(.7, stats["equipment_coverage"])
                self.assertEqual([3, 4, 5, 6, 7], stats["purple_ranks"])
        self.assertEqual(original, members)

    def test_tc15_no_members_and_no_known_equipment_have_explicit_denominators(self):
        empty = aggregate([])["ranges"]["top_50"]
        self.assertEqual(0, empty["N"])
        self.assertIsNone(empty["legendary_ratio"])
        self.assertIsNone(empty["equipment_coverage"])
        unknown = aggregate([equipped_member(1, "unknown")])["ranges"]["top_50"]
        self.assertEqual((1, 0, 0, 1), tuple(unknown[k] for k in ("N", "L", "P", "U")))
        self.assertEqual(0, unknown["legendary_ratio"])
        self.assertEqual([], unknown["purple_ranks"])

    def test_tc15_unknown_kungfu_classification_is_a_real_equipment_group(self):
        members = [equipped_member(1, "purple", lane="unknown", kungfu="未知")]
        stats = aggregate(members)["ranges"]["top_50"]
        group = stats["groups"]["unknown"]["未知"]
        self.assertEqual((1, 0, 1, 0), tuple(group[k] for k in ("N", "L", "P", "U")))
        self.assertEqual([1], group["purple_ranks"])

    def test_tc16_ranges_use_original_rank_before_purple_filtering(self):
        ranks = [49, 50, 51, 99, 100, 101, 199, 200, 201, 999, 1000]
        members = [equipped_member(rank, "purple" if rank % 2 else "legendary") for rank in ranks]
        ranges = aggregate(members)["ranges"]
        expected = {"top_50": [49, 50], "top_100": [49, 50, 51, 99, 100],
                    "top_200": [49, 50, 51, 99, 100, 101, 199, 200], "top_1000": ranks}
        for key, included in expected.items():
            with self.subTest(key=key):
                self.assertEqual(len(included), ranges[key]["N"])
                self.assertEqual([rank for rank in included if rank % 2], ranges[key]["purple_ranks"])

    def test_tc17_equipment_change_preserves_rank_score_frozen_kungfu_and_group_population(self):
        members = ten_member_golden(); before = aggregate(members)
        changed = deepcopy(members)
        changed[2]["local_win_packet"].update(equipped_member(3, "legendary")["local_win_packet"])
        after = aggregate(changed)
        self.assertEqual((3, 4, 3), tuple(after["ranges"]["top_50"][k] for k in ("L", "P", "U")))
        self.assertNotEqual(before["equipment_view_revision"], after["equipment_view_revision"])
        frozen = lambda view: [(m["rank"], m["score"], m["kungfu"], m["kungfu_id"], m["lane"]) for m in view["members"]]
        self.assertEqual(frozen(before), frozen(after))
        self.assertEqual(before["ranges"]["top_50"]["groups"]["dps"]["山居剑意"]["N"],
                         after["ranges"]["top_50"]["groups"]["dps"]["山居剑意"]["N"])

    def test_missing_packet_never_falls_back_to_legacy_legendary_equipment(self):
        member = equipped_member(1, "purple"); member.pop("local_win_packet")
        stats = aggregate([member])["ranges"]["top_50"]
        self.assertEqual((1, 0, 0, 1), tuple(stats[k] for k in ("N", "L", "P", "U")))

    def test_content_revision_ignores_check_times_and_input_order(self):
        members = ten_member_golden()
        first = aggregate(members)
        second = aggregate(list(reversed(members)), checked_at=CHECKED_AT+10, completed_at=CHECKED_AT+11)
        self.assertEqual(first["equipment_view_revision"], second["equipment_view_revision"])

    def test_duplicate_out_of_range_rank_or_more_than_thousand_members_is_rejected(self):
        for members in ([equipped_member(1), equipped_member(1)], [equipped_member(0)],
                        [equipped_member(1001)], [equipped_member(i) for i in range(1, 1002)]):
            with self.subTest(count=len(members)):
                with self.assertRaises(ValueError):
                    aggregate(members)

    def test_seeded_members_keep_exact_purple_membership_for_every_range_and_group(self):
        rng = random.Random(20261113)
        states = [rng.choice(("legendary", "purple", "unknown")) for _ in range(125)]
        members = [equipped_member(i+1, state, lane="healer" if i % 3 == 0 else "dps",
                                   kungfu="灵素" if i % 3 == 0 else "山居剑意") for i, state in enumerate(states)]
        view = aggregate(members)
        # Oracle is a hand-labelled state roster, independent of production classifier.
        for key, ceiling in (("top_50", 50), ("top_100", 100), ("top_200", 200), ("top_1000", 1000)):
            selected = members[:ceiling]; stats = view["ranges"][key]
            self.assertEqual(len(selected), stats["N"])
            self.assertEqual(stats["N"], stats["L"]+stats["P"]+stats["U"])
            self.assertEqual([m["rank"] for m in selected if states[m["rank"]-1] == "purple"], stats["purple_ranks"])
            self.assertEqual(len([s for s in states[:ceiling] if s == "legendary"]), stats["L"])


class EquipmentPublicProjectionAndRevocationTests(unittest.TestCase):
    def test_tc18_private_fields_and_nested_scalar_injection_never_escape(self):
        view = aggregate(ten_member_golden())
        view["private_key"] = "DO_NOT_EXPOSE"
        view["members"][0]["display_locator"] = {"token": "DO_NOT_EXPOSE"}
        view["members"][0]["identity_id"] = "DO_NOT_EXPOSE"
        view["members"][0]["name"] = {"token": "DO_NOT_EXPOSE"}
        view["members"][0]["local_win_packet"]["weapon_name"] = {"token": "DO_NOT_EXPOSE"}
        view["members"][0]["local_win_packet"]["teammates"] = [
            {"role_name": "可公开队友", "kungfu_id": 10626, "token": "DO_NOT_EXPOSE", "private_locator": {"token": "DO_NOT_EXPOSE"}}]
        original = deepcopy(view)
        public = public_equipment_view(view)
        self.assertNotIn("DO_NOT_EXPOSE", repr(public))
        self.assertNotIn("private_key", public)
        self.assertNotIn("identity_id", public["members"][0])
        self.assertEqual(original, view)
        self.assertEqual("可公开队友", public["members"][0]["local_win_packet"]["teammates"][0]["role_name"])

    def test_bson_int64_scalars_are_public_integers_not_silently_dropped(self):
        for field in ("timestamp", "score", "match_id"):
            view = aggregate(ten_member_golden())
            with self.subTest(field=field):
                if field == "timestamp":
                    value = TIMESTAMP; view[field] = Int64(value)
                elif field == "score":
                    value = 2401; view["members"][0][field] = Int64(value)
                else:
                    value = 900000000000001; view["members"][0]["local_win_packet"][field] = Int64(value)
                public = public_equipment_view(view)
                result = public if field == "timestamp" else public["members"][0] if field == "score" else public["members"][0]["local_win_packet"]
                self.assertEqual(str(value) if field == "match_id" else value, result.get(field))
                self.assertIs(type(result[field]), str if field == "match_id" else int)

    def test_tc18_markup_names_are_preserved_as_plain_data_for_renderer_to_escape(self):
        view = aggregate([equipped_member(1)])
        view["members"][0]["name"] = "<img src=x onerror=alert(1)>"
        self.assertEqual(view["members"][0]["name"], public_equipment_view(view)["members"][0]["name"])

    def test_revocation_reclassifies_card_and_every_range_without_mutating_original(self):
        view = aggregate(ten_member_golden()); original = deepcopy(view)
        result = apply_revocations(view, [revoked(view, view["members"][0])])
        for key in ("top_50", "top_100", "top_200", "top_1000"):
            self.assertEqual((10, 1, 5, 4), tuple(result["ranges"][key][k] for k in ("N", "L", "P", "U")))
        packet = result["members"][0]["local_win_packet"]
        self.assertEqual("unknown", packet["equipment_state"])
        self.assertIsNone(packet.get("weapon_name"))
        self.assertNotEqual(view["equipment_view_revision"], result["equipment_view_revision"])
        self.assertEqual(original, view)

    def test_revoked_purple_member_is_removed_from_purple_set(self):
        view = aggregate(ten_member_golden())
        result = apply_revocations(view, [revoked(view, view["members"][2])])
        self.assertEqual([4, 5, 6, 7], result["ranges"]["top_50"]["purple_ranks"])
        self.assertEqual((2, 4, 4), tuple(result["ranges"]["top_50"][k] for k in ("L", "P", "U")))

    def test_revocation_is_bound_to_snapshot_scope_member_and_binding_not_only_rank(self):
        view = aggregate(ten_member_golden())
        changes = [{"timestamp": TIMESTAMP+1}, {"member_revision": "other-members"},
                   {"binding_revision": "other-binding"}, {"rank": 999},
                   {"scope": dict(SCOPE, season_id="other-season")}]
        for changeset in changes:
            with self.subTest(changes=changeset):
                result = apply_revocations(view, [revoked(view, view["members"][0], **changeset)])
                self.assertEqual(view["equipment_view_revision"], result["equipment_view_revision"])
                self.assertEqual(2, result["ranges"]["top_50"]["L"])

    def test_revocation_is_idempotent(self):
        view = aggregate(ten_member_golden()); instruction = revoked(view, view["members"][0])
        once = apply_revocations(view, [instruction])
        twice = apply_revocations(once, [instruction])
        self.assertEqual(once["equipment_view_revision"], twice["equipment_view_revision"])
        self.assertEqual(once["ranges"], twice["ranges"])


if __name__ == "__main__":
    unittest.main()
