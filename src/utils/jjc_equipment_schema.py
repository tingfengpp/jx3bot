"""Pure equipment-view integrity and BSON equality primitives (no clients)."""
from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Dict
from urllib.parse import urlsplit

from bson import BSON

SOURCE = "local_win_v2"
MAX_SNAPSHOT_BSON_BYTES = 8 * 1024 * 1024
RANGES = {"top_50": 50, "top_100": 100, "top_200": 200, "top_1000": 1000}


def content_revision(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def equipment_view_revision(view):
    rows = [{k: v for k, v in m.items() if k not in
             ("weapon_name", "weapon_quality", "weapon_icon", "teammates")}
            for m in view["members"]]
    return content_revision({"timestamp": view["timestamp"], "source": view["source"],
        "scope": view["scope"], "member_revision": view["member_revision"],
        "equipment_policy_revision": view.get("equipment_policy_revision"),
        "members": rows, "ranges": view["ranges"]})


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 9223372036854775807


def _time(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _text(value, maximum=256):
    return isinstance(value, str) and 0 < len(value) <= maximum


def _field_limits(row):
    for key in ("name", "server", "role_name", "person_name", "weapon_name"):
        if row.get(key) is not None and not _text(row[key]):
            raise ValueError("invalid_equipment_text")
    for key in ("weapon_icon",):
        value = row.get(key)
        if value is not None:
            if not _text(value, 2048):
                raise ValueError("invalid_equipment_icon")
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("invalid_equipment_icon")


def _metrics(rows):
    states = [(row.get("local_win_packet") or {}).get("equipment_state") for row in rows]
    n, l, p = len(rows), states.count("legendary"), states.count("purple")
    return {"N": n, "L": l, "P": p, "U": n-l-p,
            "legendary_ratio": l/n if n else None,
            "equipment_coverage": (l+p)/n if n else None,
            "purple_ranks": [row["rank"] for row in rows
                             if (row.get("local_win_packet") or {}).get("equipment_state") == "purple"]}


def validate_equipment_view(view):
    """Validate the internal stored view; public projections retain its evidence ID.

    This function does not reinterpret an already stripped public projection as
    the original internal content. It validates schema and aggregation integrity,
    not upstream facts or role identity ownership.
    """
    if (not isinstance(view, dict) or type(view.get("schema_version")) is not int
            or view["schema_version"] != 2 or view.get("source") != SOURCE
            or not _positive(view.get("timestamp")) or not _text(view.get("member_revision"), 256)
            or not _time(view.get("checked_at")) or not _time(view.get("completed_at"))
            or view["checked_at"] > view["completed_at"]):
        raise ValueError("invalid_equipment_view")
    scope = view.get("scope")
    if (not isinstance(scope, dict) or not _text(scope.get("season_id"))
            or not _positive(scope.get("season_start")) or type(scope.get("pvp_type")) is not int
            or scope["pvp_type"] != 3 or not _positive(scope.get("algorithm_version"))
            or scope["season_start"] > view["checked_at"]):
        raise ValueError("invalid_equipment_scope")
    rows = view.get("members")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("invalid_equipment_members")
    ranks = []
    for row in rows:
        if not isinstance(row, dict) or not _positive(row.get("rank")) or row["rank"] > 1000:
            raise ValueError("invalid_equipment_member")
        ranks.append(row["rank"])
        _field_limits(row)
        packet = row.get("local_win_packet")
        if packet is None:
            continue
        if (not isinstance(packet, dict) or packet.get("source") != SOURCE
                or packet.get("equipment_state") not in ("legendary", "purple", "unknown")):
            raise ValueError("invalid_equipment_packet")
        _field_limits(packet)
        if packet["equipment_state"] != "unknown":
            quality = packet.get("weapon_quality")
            if (not _text(packet.get("weapon_name")) or type(quality) is not int
                    or quality not in (1, 2, 3, 4, 5)):
                raise ValueError("invalid_equipment_weapon")
        for key, predicate in (("match_id", _positive), ("match_time", _time)):
            if packet.get(key) is not None and not predicate(packet[key]):
                raise ValueError("invalid_equipment_match")
        if packet.get("match_time") is not None and not scope["season_start"] <= packet["match_time"] <= view["checked_at"]:
            raise ValueError("invalid_equipment_match")
        mates = packet.get("teammates", [])
        if not isinstance(mates, list) or len(mates) > 2:
            raise ValueError("invalid_equipment_teammates")
        for mate in mates:
            if not isinstance(mate, dict):
                raise ValueError("invalid_equipment_teammate")
            _field_limits(mate)
    if len(set(ranks)) != len(ranks) or ranks != sorted(ranks):
        raise ValueError("invalid_equipment_ranks")
    expected_ranges = {}
    for key, ceiling in RANGES.items():
        selected = [row for row in rows if row["rank"] <= ceiling]
        stats = _metrics(selected)
        groups = {}
        for row in selected:
            lane, name = row.get("lane") or "unknown", row.get("kungfu") or "未知"
            if not isinstance(lane, str) or not isinstance(name, str):
                raise ValueError("invalid_equipment_group")
            groups.setdefault(lane, {}).setdefault(name, []).append(row)
        stats["groups"] = {lane: {name: _metrics(group) for name, group in entries.items()}
                           for lane, entries in groups.items()}
        expected_ranges[key] = stats
    if view.get("ranges") != expected_ranges:
        raise ValueError("invalid_equipment_aggregation")
    if view.get("equipment_view_revision") != equipment_view_revision(view):
        raise ValueError("invalid_equipment_revision")
    return view


_BSON_TYPES = {1: "double", 2: "string", 3: "object", 4: "array", 5: "binData",
               7: "objectId", 8: "bool", 9: "date", 10: "null", 11: "regex",
               13: "javascript", 15: "javascriptWithScope", 16: "int", 17: "timestamp",
               18: "long", 19: "decimal", 127: "maxKey", 255: "minKey"}


def _exact_node(expression, value, depth=0):
    """Mongo4.4 aggregation equality with existence, literal values and types.

    Query equality's array membership and null/missing coercions are unsuitable.
    Each nested object/array node is typed as well, so int -> long changes inside
    an array cannot evade the original-document compare-and-swap.
    """
    if depth > 60:
        raise ValueError("legacy_document_too_deep")
    kind = _BSON_TYPES[BSON.encode({"v": value})[4]]
    variable = "e%d" % depth
    reference = "$$" + variable
    conditions = [{"$eq": [{"$type": reference}, kind]},
                  {"$eq": [reference, {"$literal": deepcopy(value)}]}]
    if isinstance(value, dict):
        entries = {"$cond": [{"$eq": [{"$type": reference}, "object"]},
                              {"$objectToArray": reference}, []]}
        keys = {"$map": {"input": entries, "as": "entry", "in": "$$entry.k"}}
        conditions.append({"$setEquals": [keys, {"$literal": list(value)}]})
        for key, item in value.items():
            values = {"$map": {"input": {"$filter": {"input": entries, "as": "field",
                       "cond": {"$eq": ["$$field.k", {"$literal": key}]}}},
                       "as": "field", "in": "$$field.v"}}
            conditions.append(_exact_node({"$arrayElemAt": [values, 0]}, item, depth+1))
    elif isinstance(value, list):
        array = {"$cond": [{"$isArray": reference}, reference, []]}
        conditions.append({"$eq": [{"$size": array}, len(value)]})
        conditions.extend(_exact_node({"$arrayElemAt": [array, index]}, item, depth+1)
                          for index, item in enumerate(value))
    return {"$let": {"vars": {variable: expression}, "in": {"$and": conditions}}}


def exact_legacy_guard(document):
    if not isinstance(document, dict) or "_id" not in document:
        raise ValueError("legacy_document_requires_id")
    # Root $objectToArray is legitimate; the root itself is not compared to a
    # giant literal. Recursion compares its explicit fields individually.
    entries = {"$objectToArray": "$$ROOT"}
    conditions = [{"$setEquals": [{"$map": {"input": entries, "as": "entry", "in": "$$entry.k"}},
                                    {"$literal": list(document)}]}]
    conditions.extend(_exact_node("$" + key, value) for key, value in document.items())
    return {"_id": document["_id"], "$expr": {"$and": conditions}}
