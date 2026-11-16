"""Pure same-winning-match equipment and whole-board aggregation policies."""
from copy import deepcopy
import math
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from src.services.jx3.jjc_kungfu_meta import get_jjc_kungfu_catalog
from src.services.jx3.weapon_quality import is_jjc_legendary_weapon
from src.utils.jjc_equipment_schema import content_revision, equipment_view_revision

SOURCE = "local_win_v2"
RANGES = {"top_50": 50, "top_100": 100, "top_200": 200, "top_1000": 1000}
_KUNGFUS = {int(row["id"]) for row in get_jjc_kungfu_catalog()} | {10026}


def positive_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= 9223372036854775807 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        result = int(value)
        return result if 0 < result <= 9223372036854775807 else None
    return None


def finite_time(value):
    if isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _text(value, limit=256):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else None


def _icon(value):
    value = _text(value, 2048)
    if value:
        try:
            url = urlsplit(value)
            if url.scheme in ("http", "https") and url.hostname and not url.username and not url.password:
                return value
        except ValueError:
            pass
    return None


def classify_equipment(weapon, *, legendary_names=None):
    result = {"equipment_state": "unknown", "weapon_name": None, "weapon_quality": None,
              "weapon_icon": None, "missing_reasons": {}}
    if not isinstance(weapon, dict):
        result["missing_reasons"]["weapon"] = "weapon_missing"
        return result
    name = _text(weapon.get("name"))
    quality = positive_int(weapon.get("quality"))
    if not name or name.lower() in ("未知", "unknown", "none", "暂无", "未知武器") or quality not in (1, 2, 3, 4, 5):
        result["missing_reasons"]["weapon"] = "weapon_invalid"
        return result
    legendary = (quality == 5 and name in legendary_names) if legendary_names is not None else is_jjc_legendary_weapon(quality, name)
    result.update(equipment_state="legendary" if legendary else "purple", weapon_name=name,
                  weapon_quality=quality, weapon_icon=_icon(weapon.get("icon")))
    if result["weapon_icon"] is None:
        result["missing_reasons"]["icon"] = "icon_missing"
    return result


def _player_equipment(player, snapshots):
    armors = player.get("armors")
    if not isinstance(armors, list) or not armors:
        snapshot_hash = player.get("equipment_snapshot_hash")
        snapshot = snapshots.get(snapshot_hash, {}) if isinstance(snapshot_hash, str) else {}
        armors = snapshot.get("armors") if isinstance(snapshot, dict) else None
    return classify_equipment(armors[0] if isinstance(armors, list) and armors else None)


def required_local_win_snapshot_hashes(member, selected_doc):
    """Return only equipment evidence consumed after complete winning-match validation."""
    gid = str(member.get("global_id") or "")
    if positive_int(gid) is None or not isinstance(selected_doc, dict):
        return []
    data = selected_doc.get("data")
    detail = data.get("detail") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return []
    teams = [detail.get(side) for side in ("team1", "team2")]
    rosters = [team.get("players_info") if isinstance(team, dict) else None for team in teams]
    if any(not isinstance(team, list) or len(team) != 3 or any(not isinstance(p, dict) for p in team) for team in rosters):
        return []
    located = [side for side, team in enumerate(rosters) for player in team
               if str(player.get("global_id") or player.get("globalId") or "") == gid]
    if len(located) != 1:
        return []
    side = located[0]
    if teams[side].get("won") is not True or teams[1-side].get("won") is not False:
        return []
    hashes = set()
    for player in rosters[side]:
        armors = player.get("armors")
        if isinstance(armors, list) and armors:
            continue
        value = player.get("equipment_snapshot_hash")
        if isinstance(value, str) and value:
            hashes.add(value)
    return sorted(hashes)


def validate_local_win(*, member, candidates, scope, checked_at, equipment_snapshots=None):
    """Select before hydration. Never identify by nickname or borrow another match."""
    gid = str(member.get("global_id") or "")
    if positive_int(gid) is None or member.get("binding_valid") is False:
        return {"state": "binding_invalid", "packet": None}
    snapshots = equipment_snapshots or {}
    start, now = finite_time(scope.get("season_start")), finite_time(checked_at)
    if start is None or now is None or start > now or scope.get("pvp_type") != 3:
        raise ValueError("invalid_equipment_scope")
    valid, inconsistent = [], []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        data = row.get("data") or {}
        if not isinstance(data, dict):
            continue
        detail = data.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        rosters = [(detail[k].get("players_info") or []) if isinstance(detail.get(k), dict) else [] for k in ("team1", "team2")]
        located = [(side, player) for side, roster in enumerate(rosters) if isinstance(roster, list)
                   for player in roster if isinstance(player, dict) and str(player.get("global_id") or player.get("globalId") or "") == gid]
        if not located:
            continue
        mid = positive_int(row.get("match_id"))
        basic = detail.get("basic_info") if isinstance(detail.get("basic_info"), dict) else {}
        # A positively identified different bracket is outside this query even
        # when its unrelated timestamp/ID payload is incomplete.
        types = [source[k] for source in (data, detail, basic) for k in ("match_type", "pvp_type", "type") if source.get(k) is not None]
        normalized_types = [positive_int(value) for value in types]
        if normalized_types and all(value in (2, 5) for value in normalized_types) and len(set(normalized_types)) == 1:
            continue
        time_values = [finite_time(source[k]) for source in (data, detail, basic)
                       for k in ("match_time", "matchTime", "start_time", "startTime", "time")
                       if source.get(k) is not None]
        stamp = time_values[0] if time_values else None
        time_conflict = any(value is None or value != stamp for value in time_values)
        if stamp is not None and stamp < start and not time_conflict:
            continue
        if mid is None or stamp is None or stamp > now or data.get("unavailable") or time_conflict:
            inconsistent.append((stamp, mid) if stamp is not None and mid is not None and not time_conflict else None)
            continue
        mismatch = any(source.get("match_id") is not None and positive_int(source["match_id"]) != mid
                       for source in (data, detail))
        mismatch |= any(source.get("season_id") is not None and str(source["season_id"]) != str(scope["season_id"])
                        for source in (data, detail))
        if any(value != 3 for value in normalized_types) or mismatch:
            inconsistent.append((stamp, mid))
            continue
        own_side, target = located[0]
        own = detail.get(("team1", "team2")[own_side]) or {}
        other = detail.get(("team2", "team1")[own_side]) or {}
        won = own.get("won")
        if type(won) is not bool:
            inconsistent.append((stamp, mid))
            continue
        if not won:
            continue
        players = [p for roster in rosters if isinstance(roster, list) for p in roster]
        globals_ = [str(p.get("global_id") or p.get("globalId") or "") for p in players if isinstance(p, dict)]
        if (len(located) != 1 or any(not isinstance(roster, list) or len(roster) != 3 for roster in rosters)
                or len(players) != 6 or len(globals_) != 6 or len(set(globals_)) != 6
                or any(positive_int(value) is None for value in globals_) or other.get("won") is not False):
            inconsistent.append((stamp, mid))
            continue
        valid.append((stamp, mid, target, rosters[own_side]))
    latest_key = max(((item[0], item[1]) for item in valid), default=None)
    if any(key is None or latest_key is None or key >= latest_key for key in inconsistent):
        return {"state": "data_inconsistent", "packet": None}
    if not valid:
        return {"state": "no_local_win", "packet": None}
    stamp, mid, target, team = max(valid, key=lambda item: (item[0], item[1]))
    packet = {"source": SOURCE, "match_id": mid, "match_time": stamp,
              "self_kungfu_id": positive_int(target.get("kungfu_id")),
              "self_icon_id": positive_int(target.get("kungfu_id")) if positive_int(target.get("kungfu_id")) in _KUNGFUS else None,
              "team_ready": True, **_player_equipment(target, snapshots), "teammates": []}
    for player in team:
        if player is target:
            continue
        kungfu = positive_int(player.get("kungfu_id"))
        mate = {k: deepcopy(player[k]) for k in ("global_id", "role_id", "role_name", "server", "zone") if k in player}
        mate.update(kungfu_id=kungfu, icon_id=kungfu if kungfu in _KUNGFUS else None,
                    **_player_equipment(player, snapshots))
        packet["teammates"].append(mate)
        if mate["icon_id"] is None:
            packet["missing_reasons"]["teammate_kungfu"] = "unknown_kungfu"
    return {"state": "partial" if packet["missing_reasons"] else "available", "packet": packet}


def _metrics(members):
    states = [((m.get("local_win_packet") or {}).get("equipment_state")) for m in members]
    n = len(members)
    l, p = states.count("legendary"), states.count("purple")
    return {"N": n, "L": l, "P": p, "U": n-l-p,
            "legendary_ratio": l/n if n else None, "equipment_coverage": (l+p)/n if n else None,
            "purple_ranks": [m["rank"] for m in members if (m.get("local_win_packet") or {}).get("equipment_state") == "purple"]}


def aggregate_equipment_view(*, timestamp, scope, members, checked_at, completed_at, member_revision, policy_revision=None):
    if (positive_int(timestamp) is None or not isinstance(members, list) or len(members) > 1000
            or not isinstance(member_revision, str) or not member_revision):
        raise ValueError("invalid_equipment_view")
    rows = sorted(deepcopy(members), key=lambda m: m.get("rank", 0))
    ranks = [m.get("rank") for m in rows]
    if any(positive_int(rank) is None or not isinstance(rank, int) or rank > 1000 for rank in ranks) or len(set(ranks)) != len(ranks):
        raise ValueError("invalid_equipment_ranks")
    # Packets were classified while selecting/hydrating this complete view.
    # Re-aggregation and frozen reads must preserve that policy decision.
    ranges = {}
    for key, ceiling in RANGES.items():
        selected = [m for m in rows if m["rank"] <= ceiling]
        metrics = _metrics(selected)
        groups = {}
        for m in selected:
            lane = m.get("lane") or "unknown"
            name = m.get("kungfu") or "未知"
            groups.setdefault(lane, {}).setdefault(name, []).append(m)
        metrics["groups"] = {lane: {name: _metrics(items) for name, items in group.items()} for lane, group in groups.items()}
        ranges[key] = metrics
    if policy_revision is None:
        import config
        policy_revision = content_revision({"algorithm": "local_win_v2:1",
            "legendary_names": sorted(config.JJC_LEGENDARY_WEAPON_NAMES)})
    view = {"timestamp": timestamp, "schema_version": 2, "source": SOURCE, "scope": deepcopy(scope),
            "equipment_policy_revision": policy_revision,
            "member_revision": member_revision, "members": rows, "ranges": ranges,
            "checked_at": checked_at, "completed_at": completed_at}
    view["equipment_view_revision"] = equipment_view_revision(view)
    return view


_VIEW_FIELDS = ("timestamp", "schema_version", "source", "scope", "member_revision", "equipment_view_revision", "equipment_policy_revision", "checked_at", "completed_at")
_MEMBER_FIELDS = ("rank", "name", "server", "score", "kungfu", "kungfu_id", "lane", "found", "binding_revision")
_PACKET_FIELDS = ("source", "match_id", "match_time", "self_kungfu_id", "self_icon_id", "team_ready", "equipment_state", "weapon_name", "weapon_quality", "weapon_icon")
_MATE_FIELDS = ("role_name", "server", "kungfu_id", "icon_id", "equipment_state", "weapon_name", "weapon_quality", "weapon_icon")


def _scalars(item, fields):
    return {k: (int(item[k]) if isinstance(item[k], int) and not isinstance(item[k], bool) else deepcopy(item[k]))
            for k in fields if k in item and (item[k] is None or isinstance(item[k], (str, int, float, bool)))
            and (not isinstance(item[k], float) or math.isfinite(item[k]))}


def public_equipment_view(view):
    result = _scalars(view, _VIEW_FIELDS)
    result["scope"] = _scalars(view.get("scope") or {}, ("season_id", "season_start", "pvp_type", "algorithm_version"))
    result["members"] = []
    for m in view.get("members") or []:
        row = _scalars(m, _MEMBER_FIELDS)
        packet = m.get("local_win_packet")
        if isinstance(packet, dict):
            public = _scalars(packet, _PACKET_FIELDS)
            if positive_int(packet.get("match_id")) is not None:
                # JSON numbers lose precision in browsers above 2**53 - 1.
                public["match_id"] = str(packet["match_id"])
            public["missing_reasons"] = _scalars(packet.get("missing_reasons") or {}, ("weapon", "icon", "teammate_kungfu", "binding"))
            public["teammates"] = [_scalars(mate, _MATE_FIELDS) for mate in packet.get("teammates") or [] if isinstance(mate, dict)]
            row["local_win_packet"] = public
        else:
            row["local_win_packet"] = None
        if isinstance(m.get("equipment_status"), str):
            row["equipment_status"] = m["equipment_status"]
        result["members"].append(row)
    # Aggregate keys are computed by our pure policy, never copied from arbitrary documents.
    result["ranges"] = aggregate_equipment_view(timestamp=view["timestamp"], scope=result["scope"],
        members=result["members"], checked_at=view.get("checked_at"), completed_at=view.get("completed_at"),
        member_revision=view["member_revision"])["ranges"]
    return result


def apply_revocations(view, revocations):
    result = deepcopy(view)
    for instruction in revocations:
        if (not isinstance(instruction, dict) or instruction.get("timestamp") != view.get("timestamp")
                or instruction.get("scope") != view.get("scope") or instruction.get("member_revision") != view.get("member_revision")
                or instruction.get("reason") not in ("binding_invalid", "data_inconsistent", "no_local_win")
                or finite_time(instruction.get("confirmed_at")) is None):
            continue
        for member in result["members"]:
            if member.get("rank") != instruction.get("rank") or member.get("binding_revision") != instruction.get("binding_revision"):
                continue
            packet = member.get("local_win_packet") or {"source": SOURCE, "teammates": []}
            packet.update(equipment_state="unknown", weapon_name=None, weapon_quality=None, weapon_icon=None,
                          missing_reasons={"binding": instruction["reason"]})
            packet.update(teammates=[], self_kungfu_id=None, self_icon_id=None, match_id=None, match_time=None)
            member["local_win_packet"] = packet
            member["equipment_status"] = instruction["reason"]
    return aggregate_equipment_view(timestamp=result["timestamp"], scope=result["scope"], members=result["members"],
        checked_at=result.get("checked_at"), completed_at=result.get("completed_at"), member_revision=result["member_revision"], policy_revision=result.get("equipment_policy_revision"))


def equipment_view_statistics(view):
    """Adapt one effective view to existing ranking consumers, without reselecting."""
    result = {}
    for key, metrics in view["ranges"].items():
        selected = [deepcopy(m) for m in view["members"] if m["rank"] <= RANGES[key]]
        for member in selected:
            packet = member.get("local_win_packet") or {}
            member.update({field: deepcopy(packet.get(field)) for field in ("weapon_name", "weapon_quality", "weapon_icon", "self_kungfu_id")})
            member.update(teammates=deepcopy(packet.get("teammates") or []),
                          weapon_checked=packet.get("equipment_state") in ("legendary", "purple"),
                          teammates_checked=bool(packet.get("team_ready")),
                          equipment_state=packet.get("equipment_state", "unknown"))
        scores = [m["score"] for m in selected if isinstance(m.get("score"), (int, float)) and not isinstance(m["score"], bool)]
        current = {"equipment_source": SOURCE, "equipment_view_revision": view["equipment_view_revision"],
                   "equipment_metrics": deepcopy({k: v for k, v in metrics.items() if k != "groups"}),
                   "total_players": len(selected), "unknown_members": [m for m in selected if m.get("lane") not in ("healer", "dps")],
                   "invalid_details": []}
        for lane in ("healer", "dps"):
            buckets = {}
            for member in selected:
                if member.get("lane") == lane:
                    buckets.setdefault(member.get("kungfu") or "未知", []).append(member)
            groups = metrics.get("groups", {}).get(lane, {})
            ordered = sorted(buckets, key=lambda name: (-len(buckets[name]), buckets[name][0]["rank"]))
            current[lane] = {"equipment_source": SOURCE, "equipment_view_revision": view["equipment_view_revision"],
                "valid_count": sum(map(len, buckets.values())), "distribution": {name: len(buckets[name]) for name in ordered},
                "list": [(name, len(buckets[name])) for name in ordered], "members": buckets,
                "min_score": min(scores) if scores else None, "equipment_incomplete": False,
                "equipment_metrics": deepcopy(groups),
                "legendary_count_map": {name: groups.get(name, {}).get("L", 0) for name in buckets},
                "purple_ranks": [m["rank"] for m in selected if m.get("lane") == lane and m["rank"] in metrics["purple_ranks"]]}
        current["total_valid_count"] = current["healer"]["valid_count"] + current["dps"]["valid_count"]
        current["invalid_count"] = len(current["unknown_members"])
        current["unclassified_count"] = 0
        result[key] = current
    return result
