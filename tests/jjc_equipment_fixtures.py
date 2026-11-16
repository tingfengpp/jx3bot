"""Synthetic, immutable-by-convention fixtures for local-win equipment tests.

No clients, environment lookup, persisted credentials, or network operations.
The canonical detail shape matches jjc_match_detail; all identities are invented.
"""
from copy import deepcopy

SCOPE = {"season_id": "equipment-test-season", "season_start": 1800000000,
         "pvp_type": 3, "algorithm_version": 1}
CHECKED_AT = SCOPE["season_start"] + 86400
TIMESTAMP = SCOPE["season_start"] + 7200
MEMBER_REVISION = "synthetic-members-v1"
LEGENDARY_NAME = "烂柯"
PURPLE_NAME = "合成测试轻剑"
ICON = "https://example.invalid/equipment.png"
TARGET_GLOBAL_ID = "700000000000000001"


def weapon(state="purple", **overrides):
    result = {"name": LEGENDARY_NAME if state == "legendary" else PURPLE_NAME,
              "quality": 5 if state == "legendary" else 4, "icon": ICON}
    result.update(overrides)
    return result


def frozen_member(rank=1, *, lane="dps", kungfu="山居剑意", global_id=None):
    return {"rank": rank, "name": "合成角色%d" % rank, "server": "合成服务器",
            "score": 2400 + rank, "kungfu": kungfu,
            "kungfu_id": {"山居剑意": 10145, "问水诀": 10144, "灵素": 10626}.get(kungfu),
            "lane": lane, "found": lane != "unknown",
            "global_id": global_id or str(int(TARGET_GLOBAL_ID) + rank - 1),
            "identity_id": "synthetic-identity-%d" % rank,
            "binding_revision": "binding-%d" % rank,
            "display_locator": {"server": "合成服务器", "game_role_id": str(100 + rank)}}


def player(index, *, global_id=None, kungfu_id=None, equipment="purple"):
    return {"global_id": global_id or str(int(TARGET_GLOBAL_ID) + index),
            "role_id": str(101 + index), "role_name": "合成参赛者%d" % index,
            "server": "合成服务器", "zone": "合成大区",
            "kungfu_id": kungfu_id if kungfu_id is not None else (10144, 10786, 10626, 10003, 10176, 10698)[index],
            "armors": [weapon(equipment)] if equipment else []}


def match_doc(match_id=100, *, when=None, won=True, equipment="purple", target_team="team1"):
    team1 = [player(i, equipment=equipment if i == 0 else "purple") for i in range(3)]
    team2 = [player(i) for i in range(3, 6)]
    if target_team == "team2":
        team1, team2 = team2, team1
    own_won = won
    opponent_won = not won if type(won) is bool else False
    return {"match_id": match_id, "cached_at": CHECKED_AT - 10,
            "data": {"match_id": match_id, "match_time": when if when is not None else SCOPE["season_start"] + 100,
                     "detail": {"match_id": match_id, "season_id": SCOPE["season_id"],
                                "basic_info": {"match_type": 3},
                                "team1": {"won": own_won if target_team == "team1" else opponent_won, "players_info": team1},
                                "team2": {"won": own_won if target_team == "team2" else opponent_won, "players_info": team2}}}}


def equipped_member(rank, state="purple", **member_kwargs):
    result = frozen_member(rank, **member_kwargs)
    item = weapon(state) if state != "unknown" else {}
    result["local_win_packet"] = {
        "source": "local_win_v2", "match_id": 1000 + rank,
        "match_time": SCOPE["season_start"] + rank,
        "self_kungfu_id": 10144, "equipment_state": state,
        "weapon_name": item.get("name"), "weapon_quality": item.get("quality"),
        "weapon_icon": item.get("icon"), "teammates": [],
        "missing_reasons": {} if state != "unknown" else {"weapon": "weapon_missing"}}
    # Opposite old equipment makes an accidental legacy fallback observable.
    legacy = weapon("purple" if state == "legendary" else "legendary")
    result.update(weapon_name=legacy["name"], weapon_quality=legacy["quality"], weapon_icon=legacy["icon"])
    return result


def ten_member_golden():
    return [equipped_member(i, "legendary" if i <= 2 else "purple" if i <= 7 else "unknown")
            for i in range(1, 11)]


def revoked(view, member, *, reason="binding_invalid", **overrides):
    item = {"timestamp": view["timestamp"], "scope": deepcopy(view["scope"]),
            "member_revision": view["member_revision"],
            "binding_revision": member["binding_revision"], "rank": member["rank"],
            "confirmed_at": CHECKED_AT + 1, "reason": reason}
    item.update(overrides)
    return item
