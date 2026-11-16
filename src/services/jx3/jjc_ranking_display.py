"""Same-match display packets and the local ranking adoption policy."""
from copy import deepcopy
import math
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from bson import ObjectId

from src.services.jx3.jjc_kungfu_meta import get_jjc_kungfu_catalog
from src.services.jx3.jjc_kungfu_policy import KungfuPolicy, positive_integer, valid_timestamp
from src.utils.jjc_display_identity import (
    duplicate_player as _duplicate_player, indicator_identity,
    player_ids as _player_ids, target_team, text,
)

LOCATOR_FIELDS = ("server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")
_KUNGFUS = {str(item["id"]): item["id"] for item in get_jjc_kungfu_catalog()}
# Raw aoxue evidence uses 10026; keep the legacy ranking catalog frozen.
_KUNGFUS["10026"] = 10026


def display_context(identity: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
    return {"identity_id": str(identity["_id"]), "scope": dict(scope),
            "locator": {key: identity[key] for key in LOCATOR_FIELDS if key in identity}}


def _weapon(player: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    armors = player.get("armors")
    weapon = armors[0] if isinstance(armors, list) and armors else None
    if not isinstance(weapon, dict):
        return None
    name, icon, quality = text(weapon.get("name")), text(weapon.get("icon")), weapon.get("quality")
    try:
        url = urlsplit(icon)
        renderable = url.scheme in ("https", "http") and bool(url.netloc) and not url.username and not url.password
    except ValueError:
        renderable = False
    if not name or str(quality) not in ("1", "2", "3", "4", "5") or not renderable:
        return None
    return {"weapon_name": name, "weapon_quality": int(quality), "weapon_icon": icon}


def build_display_update(identity: Dict[str, Any], scope: Dict[str, Any], generation: int,
                         evidence: Dict[str, Any], verified_at: float) -> Dict[str, Any]:
    """Build a bounded delta using only this attempt; never borrow a previous packet."""
    KungfuPolicy(scope)
    requested_at = evidence.get("requested_at")
    if (not positive_integer(generation) or not identity.get("_id")
            or not valid_timestamp(verified_at) or not valid_timestamp(requested_at)
            or requested_at > verified_at):
        raise ValueError("invalid display publication metadata")
    context = display_context(identity, scope)
    metadata = {**context, "publish_version": generation,
                "requested_at": requested_at, "verified_at": verified_at}
    attempt = {**deepcopy(metadata), "state": "failed", "missing_reasons": {}, "error_code": None}
    update = {"attempt": attempt}

    def fail(code: str) -> Dict[str, Any]:
        attempt.update(state="failed", error_code=code, missing_reasons={"team": code, "weapon": code})
        return update

    raw = evidence.get("display_evidence")
    if not isinstance(raw, dict) or raw.get("status") not in ("ready", "failed", "no_win"):
        return fail("invalid_display_evidence")
    for key in ("auth", "retry_after"):
        if key in raw:
            attempt[key] = raw[key]
    if raw["status"] == "no_win":
        # This status is only meaningful after a successful history read.
        rows = evidence.get("matches")
        if not isinstance(rows, list) or evidence.get("history_error_code"):
            return fail("invalid_no_win_evidence")
        for row in rows:
            if not isinstance(row, dict) or row.get("won") is not True:
                continue
            if any(row.get(k) is not None and str(row[k]) != str(scope[k]) for k in ("season_id", "pvp_type")):
                continue
            stamp = next((row[k] for k in ("match_time", "matchTime", "start_time", "startTime", "time") if row.get(k) is not None), None)
            try:
                if int(stamp) < scope["season_start"]:
                    continue
            except (ValueError, TypeError, OverflowError):
                pass
            return fail("invalid_no_win_evidence")
        attempt["state"] = "no_win"
        update.update(no_win_version=generation, no_win_context=deepcopy(context))
        return update
    match_id, match_time = raw.get("match_id"), raw.get("match_time")
    if (not positive_integer(match_id) or not valid_timestamp(match_time)
            or not scope["season_start"] <= match_time <= requested_at):
        return fail(raw.get("error_code") or "invalid_match_locator")
    detail, replay = raw.get("detail_data"), raw.get("replay_data")
    for source in (detail, replay):
        if isinstance(source, dict):
            for key, expected in (("match_id", match_id), ("season_id", scope["season_id"]),
                                  ("pvp_type", scope["pvp_type"])):
                if source.get(key) is not None and str(source[key]) != str(expected):
                    return fail("match_provenance_mismatch")
    if not isinstance(detail, dict):
        return fail(raw.get("error_code") or "invalid_detail")
    target, team = target_team(detail, indicator_identity(identity, evidence.get("indicator_data") or {}),
                               replay if isinstance(replay, dict) else None)
    if target is None:
        return fail(raw.get("error_code") or "target_unresolved")
    self_kungfu = _KUNGFUS.get(text(target.get("kungfu_id")))
    weapon = _weapon(target)
    teammates, seen = [], [target]
    team_ready = len(team) == 3 and self_kungfu is not None
    for player in team:
        if player is target:
            continue
        ids = _player_ids(player)
        kungfu = _KUNGFUS.get(text(player.get("kungfu_id")))
        if not ids or any(_duplicate_player(previous, player) for previous in seen) or kungfu is None:
            team_ready = False
        seen.append(player)
        mate = {key: player[key] for key in ("role_id", "global_id", "global_role_id", "zone", "server", "role_name", "person_name") if key in player}
        role_id = text(player.get("role_id") or player.get("roleId"))
        if role_id:
            mate["role_id"] = role_id
        mate["kungfu_id"] = kungfu
        mate.update(_weapon(player) or {})
        teammates.append(mate)
    team_ready = team_ready and len(teammates) == 2
    if not team_ready:
        attempt["missing_reasons"]["team"] = "team_unresolved"
    if weapon is None:
        attempt["missing_reasons"]["weapon"] = "weapon_incomplete"
    if not team_ready and weapon is None:
        return fail(raw.get("error_code") or "display_incomplete")
    attempt["state"] = "full" if team_ready and weapon else "partial"
    attempt["error_code"] = raw.get("error_code") or next(iter(attempt["missing_reasons"].values()), None)
    packet = {**deepcopy(metadata), "match_id": match_id, "match_time": match_time,
              "self_kungfu_id": self_kungfu, "team_ready": team_ready, "weapon_ready": weapon is not None,
              "teammates": teammates if team_ready else [],
              "missing_reasons": deepcopy(attempt["missing_reasons"]), **(weapon or {})}
    update["current"] = packet
    if attempt["state"] == "full":
        update["last_complete"] = deepcopy(packet)
    return update


class DisplayRequestError(ValueError):
    """Invalid snapshot membership or a bounded read request."""


_PACKET_FIELDS = ("publish_version", "requested_at", "verified_at", "match_id", "match_time",
                  "self_kungfu_id", "team_ready", "weapon_ready", "weapon_name", "weapon_quality", "weapon_icon")
_MATE_FIELDS = ("role_id", "global_id", "global_role_id", "zone", "server", "role_name", "person_name",
                "kungfu_id", "weapon_name", "weapon_quality", "weapon_icon")
_ATTEMPT_FIELDS = ("publish_version", "requested_at", "verified_at", "state", "error_code")
_RANGE_LIMITS = {"top_1000": 1000, "top_200": 200, "top_100": 100, "top_50": 50}


def _public_reasons(value: Any) -> Dict[str, str]:
    return {key: item for key, item in value.items() if key in ("team", "weapon") and isinstance(item, str)} if isinstance(value, dict) else {}


def _public_scalars(source: Dict[str, Any], fields: Any) -> Dict[str, Any]:
    # These schema fields are scalars. A malformed nested document must never
    # smuggle private binding data through an otherwise approved field name.
    return {key: source[key] for key in fields if key in source
            and (source[key] is None or isinstance(source[key], (str, bool, int))
                 or (isinstance(source[key], float) and math.isfinite(source[key])))}


def public_display_packet(packet: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(packet, dict):
        return None
    result = _public_scalars(packet, _PACKET_FIELDS)
    mates = packet.get("teammates")
    result["teammates"] = [_public_scalars(mate, _MATE_FIELDS)
                           for mate in (mates if isinstance(mates, list) else []) if isinstance(mate, dict)]
    result["missing_reasons"] = _public_reasons(packet.get("missing_reasons"))
    return result


def public_display_attempt(attempt: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(attempt, dict):
        return None
    result = _public_scalars(attempt, _ATTEMPT_FIELDS)
    result["missing_reasons"] = _public_reasons(attempt.get("missing_reasons"))
    return result


def public_display_member(member: Dict[str, Any]) -> Dict[str, Any]:
    # Keep the established public role fields; only the new internal binding is
    # private. Packet/attempt use explicit public schemas at every nesting level.
    result = {key: deepcopy(value) for key, value in member.items()
              if key not in ("display_identity_id", "display_locator", "display_scope")}
    for key, project in (("display_packet", public_display_packet), ("display_attempt", public_display_attempt)):
        if key in member:
            result[key] = project(member[key])
    return result


def _same_binding(value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return (isinstance(value, dict) and value.keys() == expected.keys()
                and all(_same_binding(value[key], item) for key, item in expected.items()))
    if isinstance(expected, int) and not isinstance(expected, bool):
        return isinstance(value, int) and not isinstance(value, bool) and value == expected
    return type(value) is type(expected) and value == expected


def _bound(item: Any, context: Dict[str, Any]) -> bool:
    return isinstance(item, dict) and all(key in item and _same_binding(item[key], value)
                                          for key, value in context.items())


def _valid_metadata(item: Any, context: Dict[str, Any], now: float) -> bool:
    return (_bound(item, context) and positive_integer(item.get("publish_version"))
            and valid_timestamp(item.get("requested_at")) and valid_timestamp(item.get("verified_at"))
            and item["requested_at"] <= item["verified_at"] <= now)


def _valid_packet(packet: Dict[str, Any], context: Dict[str, Any]) -> bool:
    if (type(packet.get("team_ready")) is not bool or type(packet.get("weapon_ready")) is not bool
            or not (packet["team_ready"] or packet["weapon_ready"])
            or not positive_integer(packet.get("match_id")) or not valid_timestamp(packet.get("match_time"))
            or not context["scope"]["season_start"] <= packet["match_time"] <= packet["requested_at"]):
        return False
    if packet["weapon_ready"] and not _weapon({"armors": [{
            "name": packet.get("weapon_name"), "quality": packet.get("weapon_quality"), "icon": packet.get("weapon_icon")}]}):
        return False
    mates = packet.get("teammates")
    if not isinstance(mates, list):
        return False
    if packet["team_ready"]:
        if packet.get("self_kungfu_id") not in _KUNGFUS.values() or len(mates) != 2:
            return False
        locator = context["locator"]
        seen = [{**locator, "role_id": locator.get("role_id") or locator.get("game_role_id")}]
        for mate in mates:
            if (not isinstance(mate, dict) or not _player_ids(mate)
                    or mate.get("kungfu_id") not in _KUNGFUS.values()
                    or any(_duplicate_player(previous, mate) for previous in seen)):
                return False
            seen.append(mate)
    elif mates:
        return False
    return True


def _adopt_display(member: Dict[str, Any], identity: Optional[Dict[str, Any]],
                   policy: KungfuPolicy, now: float) -> Dict[str, Any]:
    item = {"rank": member["rank"], "display_version": None, "attempt_version": None,
            "state": "unavailable", "packet": None, "attempt": None, "missing_reasons": {}}
    baseline = member.get("kungfu_publish_version")
    acquisition = member.get("kungfu_acquisition")
    if (identity is None or not positive_integer(baseline) or acquisition not in ("cache", "live")
            or not isinstance(member.get("display_locator"), dict)
            or not _same_binding(member.get("display_scope"), policy.scope)):
        return item
    context = {"identity_id": member.get("display_identity_id"), "locator": member["display_locator"],
               "scope": member["display_scope"]}
    if not _same_binding(display_context(identity, policy.scope), context):
        return item
    display = identity.get("jjc_display")
    if not isinstance(display, dict):
        return item

    def newer(version: Any) -> bool:
        return positive_integer(version) and (version > baseline if acquisition == "cache" else version >= baseline)

    attempt = display.get("attempt")
    if (_valid_metadata(attempt, context, now) and newer(attempt["publish_version"])
            and attempt.get("state") in ("full", "partial", "failed", "no_win")):
        item.update(attempt_version=attempt["publish_version"], attempt=public_display_attempt(attempt),
                    missing_reasons=_public_reasons(attempt.get("missing_reasons")))
    packet = display.get("current")
    if not (_valid_metadata(packet, context, now) and newer(packet["publish_version"])
            and _valid_packet(packet, context)):
        packet = None
    if packet is not None:
        item.update(display_version=packet["publish_version"],
                    **{key: packet[key] for key in ("match_id", "match_time", "requested_at", "verified_at")})
    watermark = display.get("no_win_version")
    if (newer(watermark) and _bound(display.get("no_win_context"), context)
            and (packet is None or watermark >= packet["publish_version"])):
        return {**item, "state": "no_win", "no_win_version": watermark,
                "missing_reasons": {"team": "no_win", "weapon": "no_win"}}
    if packet is None:
        return item
    if now - packet["verified_at"] > policy.max_stale_seconds:
        return {**item, "state": "expired", "missing_reasons": {"team": "expired", "weapon": "expired"}}
    return {**item, "state": "available" if packet["team_ready"] and packet["weapon_ready"] else "partial",
            "packet": public_display_packet(packet), "missing_reasons": _public_reasons(packet.get("missing_reasons"))}


class JjcRankingDisplayService:
    """Read already published local packets, without querying or scheduling roles."""

    def __init__(self, stats_repo: Any, identity_repo: Any, policy: Any, enabled: Any, clock: Any):
        self.stats_repo = stats_repo
        self.identity_repo = identity_repo
        self.policy = policy
        self.enabled = enabled
        self.clock = clock

    async def read(self, timestamp: int, range_key: str, ranks: List[int]) -> Dict[str, Any]:
        if (not positive_integer(timestamp) or range_key not in _RANGE_LIMITS or not isinstance(ranks, list)
                or not ranks or any(not positive_integer(rank) or rank > _RANGE_LIMITS[range_key] for rank in ranks)):
            raise DisplayRequestError("invalid_params")
        ranks = list(dict.fromkeys(ranks))
        if len(ranks) > 100:
            raise DisplayRequestError("too_many_ranks")
        now = self.clock()
        result = {"timestamp": timestamp, "range": range_key, "overlay_enabled": False,
                  "latest_timestamp": None, "checked_at": now, "items": []}
        if not (self.enabled() if callable(self.enabled) else self.enabled):
            return result
        policy = self.policy() if callable(self.policy) else self.policy
        if not isinstance(policy, KungfuPolicy):
            raise ValueError("invalid display policy")
        summary = await self.stats_repo.load_summary(timestamp)
        latest = await self.stats_repo.get_latest_current_summary(policy.scope)
        result["latest_timestamp"] = latest.get("timestamp") if latest else None
        if (not summary or not latest or latest.get("timestamp") != timestamp
                or any(row.get("ranking_display_kind") != "current"
                       or row.get("current_season") != policy.scope["season_id"]
                       or not _same_binding(row.get("kungfu_cache_scope"), policy.scope)
                       for row in (summary, latest))):
            return result
        members = await self.stats_repo.load_display_members(timestamp, range_key, ranks)
        by_rank = {}
        for member in members:
            rank = member.get("rank")
            if not positive_integer(rank) or rank not in ranks or rank in by_rank:
                raise DisplayRequestError("invalid_members")
            by_rank[rank] = member
        if set(by_rank) != set(ranks):
            raise DisplayRequestError("invalid_members")
        ids = list(dict.fromkeys(ObjectId(member["display_identity_id"]) for member in members
                                if isinstance(member.get("display_identity_id"), str)
                                and ObjectId.is_valid(member["display_identity_id"])))
        docs = await self.identity_repo.get_display_by_ids(ids) if ids else []
        identities = {str(doc["_id"]): doc for doc in docs}
        result.update(overlay_enabled=True, items=[_adopt_display(by_rank[rank],
            identities.get(by_rank[rank].get("display_identity_id")), policy, now) for rank in ranks])
        return result
