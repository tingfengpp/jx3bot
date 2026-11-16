"""Conservative, scope-independent proof used only to skip old source rows."""
import math

EQUIPMENT_TIME_FIELD = "equipment_read_time_v1"
TIME_KEYS = ("match_time", "matchTime", "start_time", "startTime", "time")
TIME_PATHS = tuple(prefix + "." + key for prefix in
    ("data", "data.detail", "data.detail.basic_info") for key in TIME_KEYS)


def equipment_read_time(data):
    if not isinstance(data, dict) or not isinstance(data.get("detail"), dict):
        return None
    detail = data["detail"]
    basic = detail.get("basic_info")
    if basic is None:
        basic = {}
    if not isinstance(basic, dict):
        return None
    values = [source[key] for source in (data, detail, basic)
              for key in TIME_KEYS if source.get(key) is not None]
    if not values:
        return None
    first = values[0]
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            if not math.isfinite(value) or value <= 0 or value != first:
                return None
        except (ValueError, OverflowError):
            return None
    return first


def build_equipment_time_expression():
    # The server backfill is deliberately conservative beyond exact binary64
    # integers. Such far-future values remain unknown, never silently old.
    times = {"$filter": {"input": [{"$ifNull": ["$" + path, None]} for path in TIME_PATHS],
        "as": "stamp", "cond": {"$ne": ["$$stamp", None]}}}
    first = {"$arrayElemAt": ["$$times", 0]}
    proof = {"$and": [
        {"$eq": [{"$type": "$data"}, "object"]},
        {"$eq": [{"$type": "$data.detail"}, "object"]},
        {"$in": [{"$type": "$data.detail.basic_info"}, ["object", "missing", "null"]]},
        {"$gt": [{"$size": "$$times"}, 0]},
        {"$allElementsTrue": [{"$map": {"input": "$$times", "as": "stamp", "in": {"$and": [
            {"$in": [{"$type": "$$stamp"}, ["int", "long", "double"]]},
            {"$gt": ["$$stamp", 0]}, {"$lt": ["$$stamp", 9007199254740992]},
            {"$eq": ["$$stamp", first]},
        ]}}}]},
    ]}
    return {"$let": {"vars": {"times": times}, "in": {"$cond": [proof, first, None]}}}


EQUIPMENT_VALID_FIELD = "equipment_read_valid_v1"
EQUIPMENT_WINNERS_FIELD = "equipment_winner_gids_v1"
EQUIPMENT_MATCH_FIELD = "equipment_match_id_v1"


def _positive_integer(value):
    return (isinstance(value, int) and not isinstance(value, bool)
        and 0 < value <= 9223372036854775807)


def equipment_source_index(match_id, data):
    stamp = equipment_read_time(data)
    result = {EQUIPMENT_TIME_FIELD: stamp, EQUIPMENT_VALID_FIELD: False,
              EQUIPMENT_WINNERS_FIELD: [], EQUIPMENT_MATCH_FIELD: None}
    if stamp is None or not _positive_integer(match_id):
        return result
    detail = data["detail"]
    basic = detail.get("basic_info") or {}
    for source in (data, detail):
        if source.get("season_id") is not None:
            return result
        inner = source.get("match_id")
        if inner is not None and (not _positive_integer(inner) or inner != match_id):
            return result
    for source in (data, detail, basic):
        for name in ("match_type", "pvp_type", "type"):
            value = source.get(name)
            if value is not None and (not _positive_integer(value) or value != 3):
                return result
    if data.get("unavailable") is not None and data.get("unavailable") is not False:
        return result
    teams = [detail.get(side) for side in ("team1", "team2")]
    if any(not isinstance(team, dict) or type(team.get("won")) is not bool
           or not isinstance(team.get("players_info"), list) or len(team["players_info"]) != 3 for team in teams):
        return result
    if teams[0]["won"] == teams[1]["won"]:
        return result
    gids = []
    for team in teams:
        for player in team["players_info"]:
            if not isinstance(player, dict):
                return result
            gid = player.get("global_id")
            if (type(gid) is not str or not gid.isascii() or not gid.isdigit()
                    or not 0 < len(gid) <= 19 or not 0 < int(gid) <= 9223372036854775807):
                return result
            gids.append(gid)
    if len(set(gids)) != 6:
        return result
    result.update({EQUIPMENT_VALID_FIELD: True, EQUIPMENT_MATCH_FIELD: match_id,
        EQUIPMENT_WINNERS_FIELD: [p["global_id"] for p in teams[0 if teams[0]["won"] else 1]["players_info"]]})
    return result


def build_equipment_source_update_pipeline():
    def typed(path, types):
        return {"$in": [{"$type": path}, types]}
    def null(path):
        return {"$eq": [{"$ifNull": [path, None]}, None]}
    def optional(path, condition):
        return {"$or": [null(path), condition]}
    def array(path):
        return {"$cond": [{"$isArray": path}, path, []]}
    stamp, mid = "$" + EQUIPMENT_TIME_FIELD, "$match_id"
    gids = {"$map": {"input": "$$players", "as": "player", "in": "$$player.global_id"}}
    rules = [
        typed(stamp, ["int", "long", "double"]), {"$gt": [stamp, 0]},
        typed(mid, ["int", "long"]), {"$gt": [mid, 0]},
        {"$eq": [{"$size": "$$one"}, 3]}, {"$eq": [{"$size": "$$two"}, 3]},
        typed("$data.detail.team1.won", ["bool"]), typed("$data.detail.team2.won", ["bool"]),
        {"$ne": ["$data.detail.team1.won", "$data.detail.team2.won"]},
        optional("$data.unavailable", {"$eq": ["$data.unavailable", False]}),
        {"$allElementsTrue": [{"$map": {"input": "$$players", "as": "player", "in": {"$and": [
            {"$eq": [{"$type": "$$player"}, "object"]}, typed("$$player.global_id", ["string"]),
            {"$regexMatch": {"input": {"$cond": [typed("$$player.global_id", ["string"]), "$$player.global_id", ""]},
                "regex": r"\A[0-9]{1,19}\z"}},
            {"$gt": [{"$convert": {"input": "$$player.global_id", "to": "long", "onError": 0, "onNull": 0}}, 0]},
        ]}}}]},
        {"$eq": [{"$size": {"$setUnion": [gids, []]}}, 6]},
    ]
    for prefix in ("data", "data.detail"):
        path = "$" + prefix + ".match_id"
        rules.append(optional(path, {"$and": [typed(path, ["int", "long"]), {"$eq": [path, mid]}]}))
        rules.append(null("$" + prefix + ".season_id"))
    for prefix in ("data", "data.detail", "data.detail.basic_info"):
        for name in ("match_type", "pvp_type", "type"):
            path = "$" + prefix + "." + name
            rules.append(optional(path, {"$and": [typed(path, ["int", "long"]), {"$eq": [path, 3]}]}))
    valid = {"$let": {"vars": {"one": array("$data.detail.team1.players_info"),
                                "two": array("$data.detail.team2.players_info")},
        "in": {"$let": {"vars": {"players": {"$concatArrays": ["$$one", "$$two"]}}, "in": {"$and": rules}}}}}
    winners = {"$map": {"input": {"$cond": ["$data.detail.team1.won",
        "$data.detail.team1.players_info", "$data.detail.team2.players_info"]},
        "as": "player", "in": "$$player.global_id"}}
    # One update is atomic; stages only expose prior-stage values internally.
    return [
        {"$set": {EQUIPMENT_TIME_FIELD: build_equipment_time_expression()}},
        {"$set": {EQUIPMENT_VALID_FIELD: valid}},
        {"$set": {EQUIPMENT_WINNERS_FIELD: {"$cond": ["$" + EQUIPMENT_VALID_FIELD, winners, []]},
                  EQUIPMENT_MATCH_FIELD: {"$cond": ["$" + EQUIPMENT_VALID_FIELD, "$match_id", None]}}},
    ]
