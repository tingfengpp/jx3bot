"""Mongo4.4 candidate reduction with a conservative positive certificate.

Only proven current 3v3 wins are reduced. Every uncertified source document is
retained, and the domain validator remains authoritative after the read.
"""
import math
from bson import BSON

TIME_PATHS = [prefix + "." + key for prefix in ("data", "data.detail", "data.detail.basic_info")
              for key in ("match_time", "matchTime", "start_time", "startTime", "time")]

def _literal(value):
    return {"$literal": value}

def _type_in(path, names):
    return {"$in": [{"$type": path}, names]}

def _null(path):
    return {"$eq": [{"$ifNull": [path, None]}, None]}

def _optional(path, predicate):
    return {"$or": [_null(path), predicate]}

def _array(path):
    return {"$cond": [{"$isArray": path}, path, []]}

def build_local_win_candidate_pipeline(batch, *, season_start, checked_at, season_id=None, source_ids=None):
    if (not isinstance(batch, list) or not 0 < len(batch) <= (1000 if source_ids is not None else 100)
            or any(not isinstance(gid, str) or not gid.isascii() or not gid.isdigit()
                   or not 0 < len(gid) <= 19 or not 0 < int(gid) <= 9223372036854775807 for gid in batch)
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                   for v in (season_start, checked_at)) or checked_at < season_start
            or (season_id is not None and (not isinstance(season_id, str) or not 0 < len(season_id) <= 256))):
        raise ValueError("invalid_local_win_read")
    if source_ids is not None:
        if (not isinstance(source_ids, list) or not 0 < len(source_ids) <= 256
                or any(isinstance(value, (list, tuple)) for value in source_ids)):
            raise ValueError("invalid_candidate_sources")
        try:
            for value in source_ids:
                BSON.encode({"_id": value})
        except Exception:
            raise ValueError("invalid_candidate_sources") from None
    identity = {"$or": [{"data.detail." + side + ".players_info.global_id": {"$in": batch}}
                        for side in ("team1", "team2")]}
    times = {"$filter": {"input": [{"$ifNull": ["$" + path, None]} for path in TIME_PATHS],
                         "as": "stamp", "cond": {"$ne": ["$$stamp", None]}}}
    first = {"$arrayElemAt": ["$$times", 0]}
    proven_old = {"$let": {"vars": {"times": times}, "in": {"$and": [
        {"$gt": [{"$size": "$$times"}, 0]}, {"$gt": [first, 0]}, {"$lt": [first, season_start]},
        {"$allElementsTrue": [{"$map": {"input": "$$times", "as": "stamp", "in": {"$and": [
            {"$in": [{"$type": "$$stamp"}, ["int", "long", "double"]]}, {"$eq": ["$$stamp", first]},
        ]}}}]},
    ]}}}
    # Keep the common scalar shape cheap, and force short-circuit evaluation.
    # Every other BSON shape still executes the original conservative predicate.
    common_paths = ("data.match_time", "data.detail.match_time", "data.detail.basic_info.start_time")
    common_stamp = {"$ifNull": ["$data.match_time", {"$ifNull": ["$data.detail.match_time", "$data.detail.basic_info.start_time"]}]}
    common_old = {"$let": {"vars": {"stamp": common_stamp}, "in": {"$and": [
        *[{"$eq": [{"$type": "$" + path}, "object"]} for path in ("data", "data.detail", "data.detail.basic_info")],
        _type_in("$$stamp", ["int", "long", "double"]),
        {"$gt": ["$$stamp", 0]}, {"$lt": ["$$stamp", season_start]},
        *[_optional("$" + path, {"$and": [_type_in("$" + path, ["int", "long", "double"]),
                                          {"$eq": ["$" + path, "$$stamp"]}]}) for path in common_paths],
        *[_null("$" + path) for path in TIME_PATHS if path not in common_paths],
    ]}}}
    fast_old = {"$and": [
        {"data.match_time": {"$type": ["int", "long", "double"], "$gt": 0, "$lt": season_start}},
        *[{path: {"$not": {"$type": "array"}}} for path in
          ("data", "data.detail", "data.detail.basic_info", "data.match_time")],
        *[{path: {"$exists": False}} for path in TIME_PATHS if path != "data.match_time"],
    ]}
    projection = {"_id": 1, "match_id": 1, "cached_at": 1, "data.unavailable": 1}
    for prefix in ("data", "data.detail", "data.detail.basic_info"):
        for key in ("match_id", "match_time", "matchTime", "start_time", "startTime", "time", "season_id", "match_type", "pvp_type", "type"):
            projection[prefix + "." + key] = 1
    projection.pop("data.detail.basic_info.match_id", None)
    for side in ("team1", "team2"):
        projection["data.detail." + side + ".won"] = 1
        for field in ("global_id", "globalId", "role_id", "roleId", "role_name", "server", "zone", "kungfu_id", "armors", "equipment_snapshot_hash"):
            projection["data.detail." + side + ".players_info." + field] = 1
    one, two = "$data.detail.team1.players_info", "$data.detail.team2.players_info"
    stamp, mid = "$__stamp", "$match_id"
    gids = {"$map": {"input": "$__players", "as": "player", "in": "$$player.global_id"}}
    id_certificate = {"$allElementsTrue": [{"$map": {"input": "$__players", "as": "player", "in": {"$and": [
        {"$eq": [{"$type": "$$player"}, "object"]},
        {"$eq": [{"$type": "$$player.global_id"}, "string"]},
        {"$regexMatch": {"input": {"$cond": [{"$eq": [{"$type": "$$player.global_id"}, "string"]}, "$$player.global_id", ""]}, "regex": r"\A[0-9]{1,19}\z"}},
        {"$gt": [{"$convert": {"input": "$$player.global_id", "to": "long", "onError": 0, "onNull": 0}}, 0]},
    ]}}}]}
    certificate = [
        {"$eq": [{"$type": "$data"}, "object"]}, {"$eq": [{"$type": "$data.detail"}, "object"]},
        _type_in("$data.detail.basic_info", ["object", "missing", "null"]),
        _type_in(mid, ["int", "long"]), {"$gt": [mid, 0]},
        _type_in(stamp, ["int", "long", "double"]), {"$gte": [stamp, season_start]}, {"$lte": [stamp, checked_at]},
        {"$eq": [{"$size": "$__one"}, 3]}, {"$eq": [{"$size": "$__two"}, 3]}, id_certificate,
        {"$eq": [{"$size": {"$setUnion": [gids, []]}}, 6]},
        _type_in("$data.detail.team1.won", ["bool"]), _type_in("$data.detail.team2.won", ["bool"]),
        {"$ne": ["$data.detail.team1.won", "$data.detail.team2.won"]},
        _optional("$data.unavailable", {"$eq": ["$data.unavailable", False]}),
    ]
    for path in TIME_PATHS:
        certificate.append(_optional("$" + path, {"$and": [_type_in("$" + path, ["int", "long", "double"]), {"$eq": ["$" + path, stamp]}]}))
    for prefix in ("data", "data.detail"):
        certificate.append(_optional("$" + prefix + ".match_id", {"$and": [
            _type_in("$" + prefix + ".match_id", ["int", "long"]), {"$eq": ["$" + prefix + ".match_id", mid]}]}))
        season = "$" + prefix + ".season_id"
        certificate.append(_null(season) if season_id is None else _optional(season, {"$and": [
            _type_in(season, ["string"]), {"$eq": [season, _literal(season_id)]}]}))
    for prefix in ("data", "data.detail", "data.detail.basic_info"):
        for key in ("match_type", "pvp_type", "type"):
            path = "$" + prefix + "." + key
            certificate.append(_optional(path, {"$and": [_type_in(path, ["int", "long"]), {"$eq": [path, 3]}]}))
    winners = {"$map": {"input": {"$cond": ["$data.detail.team1.won", "$__one", "$__two"]},
                        "as": "player", "in": "$$player.global_id"}}
    return [
        {"$match": {"$and": [identity, {"$nor": [fast_old]}, {"$expr": {"$cond": [common_old, False, {"$not": [proven_old]}]}}]
                    + ([{"_id": {"$in": source_ids}}] if source_ids is not None else [])}},
        {"$project": projection},
        {"$addFields": {"__row": "$$ROOT", "__one": _array(one), "__two": _array(two),
                        "__players": {"$concatArrays": [_array(one), _array(two)]},
                        "__stamp": {"$arrayElemAt": [times, 0]}}},
        {"$addFields": {"__certified": {"$and": certificate}}},
        {"$addFields": {"__sort_stamp": {"$cond": ["$__certified", stamp, 0]},
                        "__sort_mid": {"$cond": ["$__certified", mid, 0]},
                        "__keys": {"$cond": ["$__certified",
            {"$map": {"input": {"$setIntersection": [winners, _literal(batch)]}, "as": "gid",
                      "in": {"kind": _literal("win"), "gid": "$$gid"}}},
            [{"kind": _literal("uncertain"), "source": "$_id"}]]}}},
        {"$project": {"_id": 0, "__row": 1, "__keys": 1, "__sort_stamp": 1, "__sort_mid": 1}},
        {"$unwind": "$__keys"},
        {"$sort": {"__sort_stamp": -1, "__sort_mid": -1}},
        {"$group": {"_id": "$__keys", "row": {"$first": "$__row"}}},
        {"$group": {"_id": "$row._id", "row": {"$first": "$row"}}},
        {"$replaceRoot": {"newRoot": "$row"}},
    ]
