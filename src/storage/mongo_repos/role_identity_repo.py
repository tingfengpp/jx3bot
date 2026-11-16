from __future__ import annotations

import re
import time
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from nonebot import logger
from src.utils.jjc_query_observability import foreground_safe_logger

logger = foreground_safe_logger(logger, component="identity_repo")
from pymongo.errors import DuplicateKeyError

from src.infra.mongo import get_db as _get_db
from src.services.jx3.role_identity_matching import (
    STRICT_QUERY_ID_REFRESH_SOURCE,
    build_guarded_profile_set_fields,
    build_identity_key as _build_role_identity_key,
    coerce_match_time,
    has_verified_query_ids,
    legacy_identity_keys,
    should_overwrite_profile_fields,
)

SCHEMA_VERSION = 1

QUERY_ID_CAS_FIELDS = (
    "_id", "identity_key", "identity_level", "global_id", "global_role_id", "server",
    "normalized_server", "name", "normalized_name", "zone", "role_id", "game_role_id",
    "person_id", "profile_observed_at", "role_info_observed_match_time", "role_info_source",
    "role_info_updated_at", "global_role_id_refreshed_at", "global_role_id_refresh_source",
    "updated_at",
)
_QUERY_ID_UPDATE_FIELDS = (
    "role_id", "game_role_id", "global_role_id_refreshed_at",
    "global_role_id_refresh_source", "updated_at",
)


def _snapshot_filter(expected: Dict[str, Any]) -> Dict[str, Any]:
    """Match the observed profile, including the distinction between null and absent."""
    query: Dict[str, Any] = {}
    if "_id" in expected:
        query["_id"] = expected["_id"]
    for field in QUERY_ID_CAS_FIELDS:
        if field == "_id":
            continue
        if field not in expected:
            query[field] = {"$exists": False}
        elif expected[field] is None:
            query[field] = {"$eq": None, "$exists": True}
        else:
            query[field] = {"$eq": expected[field]}
    return query

_LEVEL_ORDER = {"name": 0, "game_role": 1, "global": 2, "global_id": 3}

MATCH_DETAIL_IDENTITY_PROJECTION: Dict[str, int] = {
    "_id": 1,
    "identity_key": 1,
    "identity_level": 1,
    "server": 1,
    "normalized_server": 1,
    "name": 1,
    "normalized_name": 1,
    "zone": 1,
    "game_role_id": 1,
    "role_id": 1,
    "person_id": 1,
    "global_role_id": 1,
    "global_id": 1,
    "aliases": 1,
    "sources": 1,
    "profile_observed_at": 1,
    "role_info_observed_match_time": 1,
    "last_seen_at": 1,
    "updated_at": 1,
    "role_info_source": 1,
    "role_info_updated_at": 1,
    "global_role_id_refreshed_at": 1,
    "global_role_id_refresh_source": 1,
}


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _coerce_object_id(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return None


def _display_publication_fields(identity_id: ObjectId, locator: Dict[str, Any], generation: int,
                                result: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the semantic delta before constructing constant Mongo field paths."""
    allowed = {"attempt", "current", "last_complete", "no_win_version", "no_win_context"}
    if not isinstance(update, dict) or "attempt" not in update or set(update) - allowed:
        raise ValueError("invalid display delta")
    scope = {key: result.get(key) for key in ("season_id", "season_start", "pvp_type", "algorithm_version")}
    context = {"identity_id": str(identity_id), "scope": scope, "locator": locator}
    for key in ("attempt", "current", "last_complete"):
        if key not in update:
            continue
        item = update[key]
        if (not isinstance(item, dict) or not isinstance(item.get("publish_version"), int)
                or isinstance(item.get("publish_version"), bool) or item["publish_version"] != generation
                or any(item.get(k) != v for k, v in context.items())
                or not isinstance(item.get("scope"), dict)
                or any(type(item["scope"].get(k)) is not type(v) for k, v in scope.items())):
            raise ValueError("invalid display binding")
        for stamp in ("requested_at", "verified_at"):
            value = item.get(stamp)
            if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    or value != result.get(stamp)):
                raise ValueError("invalid display time")
        if item["requested_at"] > item["verified_at"]:
            raise ValueError("invalid display time order")
    state = update["attempt"].get("state")
    if state not in ("full", "partial", "failed", "no_win"):
        raise ValueError("invalid display state")
    packet = update.get("current")
    if (state in ("full", "partial")) != (packet is not None):
        raise ValueError("invalid display packet state")
    if packet is not None:
        if (type(packet.get("team_ready")) is not bool or type(packet.get("weapon_ready")) is not bool
                or not (packet["team_ready"] or packet["weapon_ready"])
                or (state == "full") != (packet["team_ready"] and packet["weapon_ready"])):
            raise ValueError("invalid display completeness")
    if (state == "full") != ("last_complete" in update) or (
            "last_complete" in update and update["last_complete"] != packet):
        raise ValueError("invalid complete display packet")
    if state == "no_win":
        version = update.get("no_win_version")
        if (not isinstance(version, int) or isinstance(version, bool) or version != generation
                or update.get("no_win_context") != context):
            raise ValueError("invalid no-win watermark")
    elif "no_win_version" in update or "no_win_context" in update:
        raise ValueError("unexpected no-win watermark")
    return {"jjc_display." + key: value for key, value in update.items()}


def _global_id_string_filter(global_id: str) -> Dict[str, str]:
    return {"$eq": global_id, "$type": "string"}


def _identity_strength(doc: Dict[str, Any]) -> int:
    identity_level = str(doc.get("identity_level") or "").strip()
    if identity_level in _LEVEL_ORDER:
        return _LEVEL_ORDER[identity_level]
    identity_key = str(doc.get("identity_key") or "")
    if doc.get("global_id") or identity_key.startswith("global_id:"):
        return _LEVEL_ORDER["global_id"]
    if doc.get("global_role_id") or identity_key.startswith("global:"):
        return _LEVEL_ORDER["global"]
    if doc.get("game_role_id") or doc.get("role_id") or identity_key.startswith("game:"):
        return _LEVEL_ORDER["game_role"]
    return _LEVEL_ORDER["name"]


def _timestamp_value(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _candidate_role_key(doc: Dict[str, Any]) -> Tuple[str, str]:
    normalized_server = doc.get("normalized_server")
    normalized_name = doc.get("normalized_name")
    server = normalized_server if normalized_server else doc.get("server")
    name = normalized_name if normalized_name else doc.get("name")
    return (
        _normalize(str(server or "")),
        _normalize(str(name or "")),
    )


def _ranking_ids(doc: Dict[str, Any]) -> Dict[str, set]:
    """Keep external ID namespaces separate, including legacy key-only identities."""
    values = {field: {str(doc[field]).strip()} if doc.get(field) is not None
              and str(doc[field]).strip() else set()
              for field in ("global_id", "global_role_id", "zone", "person_id")}
    values["game_role_id"] = {str(doc[field]).strip() for field in ("game_role_id", "role_id")
                              if doc.get(field) is not None and str(doc[field]).strip()}
    key = str(doc.get("identity_key") or "")
    for prefix, field in (("global_id:", "global_id"), ("global:", "global_role_id")):
        if key.startswith(prefix) and key[len(prefix):]:
            values[field].add(key[len(prefix):])
    if key.startswith("game:"):
        parts = key.split(":", 2)
        if len(parts) == 3:
            values["zone"].add(parts[1])
            values["game_role_id"].add(parts[2])
    return values


def _ranking_tokens(doc: Dict[str, Any]) -> List[Tuple[str, ...]]:
    ids = _ranking_ids(doc)
    tokens = [(field, value) for field in ("global_id", "global_role_id") for value in ids[field]]
    tokens.extend(("game", zone, game_id) for zone in ids["zone"] for game_id in ids["game_role_id"])
    server, name = _candidate_role_key(doc)
    if server and name:
        tokens.append(("name", server, name))
    return tokens


def _ranking_compatible(player: Dict[str, Any], doc: Dict[str, Any]) -> bool:
    incoming, existing = _ranking_ids(player), _ranking_ids(doc)
    return all(not incoming[field] or not existing[field] or incoming[field] == existing[field]
               for field in incoming)


def _needs_query_locator(player: Dict[str, Any], identity: Dict[str, Any]) -> bool:
    """Only fill query fields supplied by this ranking and missing on the role."""
    zone = str(player.get("zone") or "").strip()
    role_id = str(player.get("game_role_id") or "").strip() or str(player.get("role_id") or "").strip()
    return bool(
        (zone and not identity.get("zone"))
        or (role_id and not (identity.get("game_role_id") or identity.get("role_id")))
    )


def _is_valid_enriched_identity(
    player: Dict[str, Any], original: Dict[str, Any],
    enriched: Optional[Dict[str, Any]],
) -> bool:
    """Safe upsert must preserve the role and cache, and complete known locators."""
    return bool(
        enriched
        and enriched.get("_id") == original["_id"]
        and _ranking_compatible(player, enriched)
        and not _needs_query_locator(player, enriched)
        and ("jjc_kungfu" not in original or "jjc_kungfu" in enriched)
    )


def build_identity_key(
    global_role_id: Optional[str] = None,
    zone: Optional[str] = None,
    game_role_id: Optional[str] = None,
    server: Optional[str] = None,
    name: Optional[str] = None,
    global_id: Optional[str] = None,
) -> Tuple[str, str]:
    """根据可用外部 ID 生成 (identity_key, identity_level)。

    优先级：global_id > global_role_id > zone + game_role_id > server + name。
    """
    return _build_role_identity_key(
        global_role_id=global_role_id,
        zone=zone,
        game_role_id=game_role_id,
        server=server,
        name=name,
        global_id=global_id,
    )


@dataclass(frozen=True)
class RoleIdentityRepo:
    """游戏角色身份统一模型仓储。

    维护身份关联及当前画像；jjc_kungfu为独立时钟的主打心法成功结果。
    """

    db: Optional[AsyncIOMotorDatabase] = None

    def _col(self):
        db = self.db if self.db is not None else _get_db()
        return db.role_identities

    def _history_col(self):
        db = self.db if self.db is not None else _get_db()
        return db.role_identities_history

    @staticmethod
    def _strip_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if doc:
            doc.pop("_id", None)
        return doc

    # ---- 查询 ----

    async def find_by_global_role_id(self, global_role_id: str) -> Optional[Dict[str, Any]]:
        doc = await self._col().find_one({
            "$or": [
                {"global_role_id": global_role_id},
                {"identity_key": f"global:{global_role_id}"},
            ]
        })
        return self._strip_id(doc)

    async def find_by_global_id(self, global_id: str) -> Optional[Dict[str, Any]]:
        doc = await self._col().find_one({
            "$or": [
                {"global_id": _global_id_string_filter(global_id)},
                {"identity_key": f"global_id:{global_id}"},
            ]
        })
        return self._strip_id(doc)

    async def find_by_game_role_id(self, zone: str, game_role_id: str) -> Optional[Dict[str, Any]]:
        doc = await self._col().find_one({
            "$or": [
                {"zone": zone, "game_role_id": game_role_id},
                {"zone": zone, "role_id": game_role_id},
                {"identity_key": f"game:{zone}:{game_role_id}"},
            ]
        })
        return self._strip_id(doc)

    async def find_by_name(self, server: str, name: str) -> List[Dict[str, Any]]:
        """按规范化服务器+角色名查询，可能返回多条（同名同服不同来源的旧记录）。"""
        ns = _normalize(server)
        nn = _normalize(name)
        cursor = self._col().find({"normalized_server": ns, "normalized_name": nn})
        docs = await cursor.to_list(None)
        for doc in docs:
            doc.pop("_id", None)
        return docs

    async def find_best_by_name_with_id(
        self,
        server: str,
        name: str,
        projection: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        """按名称查询最佳身份，返回保留原生 _id 的文档。

        同服同名多身份时，优先身份强度 global_id > global > game_role > name，
        再按 last_seen_at/updated_at 最新记录排序。
        """
        ns = _normalize(server)
        nn = _normalize(name)
        query = {"normalized_server": ns, "normalized_name": nn}
        cursor = self._col().find(query, projection) if projection is not None else self._col().find(query)
        docs = await cursor.to_list(None)
        if not docs:
            return None
        docs.sort(
            key=lambda doc: (
                _identity_strength(doc),
                max(
                    _timestamp_value(doc.get("last_seen_at")),
                    _timestamp_value(doc.get("updated_at")),
                ),
            ),
            reverse=True,
        )
        return docs[0]

    async def find_synced_match_page_candidates(
        self,
        server: str,
        name: str,
        *,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """Find local role identities that can help users recover from an exact miss.

        Candidates are limited to local identities: same normalized name on other
        servers, or names with an @ suffix based on the queried base name.
        """
        ns = _normalize(server)
        nn = _normalize(name)
        if not nn:
            return []

        suffix_pattern = "^" + re.escape(nn + "@")
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        pipeline = [
            {
                "$match": {
                    "$or": [
                        {"normalized_name": nn, "normalized_server": {"$ne": ns}},
                        {"normalized_name": {"$regex": suffix_pattern}},
                    ]
                }
            },
            {
                "$addFields": {
                    "_candidate_same_server": {
                        "$cond": [{"$eq": ["$normalized_server", ns]}, 1, 0]
                    },
                    "_candidate_identity_strength": {
                        "$switch": {
                            "branches": [
                                {"case": {"$eq": ["$identity_level", "global_id"]}, "then": 3},
                                {"case": {"$eq": ["$identity_level", "global"]}, "then": 2},
                                {"case": {"$eq": ["$identity_level", "game_role"]}, "then": 1},
                                {"case": {"$ne": ["$global_id", None]}, "then": 3},
                                {"case": {"$ne": ["$global_role_id", None]}, "then": 2},
                                {"case": {"$ne": ["$game_role_id", None]}, "then": 1},
                                {"case": {"$ne": ["$role_id", None]}, "then": 1},
                            ],
                            "default": 0,
                        }
                    },
                    "_candidate_freshness": {
                        "$max": [
                            {"$ifNull": ["$last_seen_at", epoch]},
                            {"$ifNull": ["$updated_at", epoch]},
                        ]
                    },
                    "_candidate_role_server": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$normalized_server", None]},
                                    {"$ne": ["$normalized_server", ""]},
                                ]
                            },
                            "$normalized_server",
                            {
                                "$toLower": {
                                    "$trim": {"input": {"$ifNull": ["$server", ""]}}
                                }
                            },
                        ]
                    },
                    "_candidate_role_name": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$normalized_name", None]},
                                    {"$ne": ["$normalized_name", ""]},
                                ]
                            },
                            "$normalized_name",
                            {
                                "$toLower": {
                                    "$trim": {"input": {"$ifNull": ["$name", ""]}}
                                }
                            },
                        ]
                    },
                }
            },
            {
                "$sort": {
                    "_candidate_same_server": -1,
                    "_candidate_identity_strength": -1,
                    "_candidate_freshness": -1,
                    "_candidate_role_server": 1,
                    "_candidate_role_name": 1,
                    "_id": 1,
                }
            },
            {
                "$group": {
                    "_id": {
                        "server": "$_candidate_role_server",
                        "name": "$_candidate_role_name",
                    },
                    "doc": {"$first": "$$ROOT"},
                }
            },
            {"$replaceRoot": {"newRoot": "$doc"}},
            {
                "$sort": {
                    "_candidate_same_server": -1,
                    "_candidate_identity_strength": -1,
                    "_candidate_freshness": -1,
                    "_candidate_role_server": 1,
                    "_candidate_role_name": 1,
                    "_id": 1,
                }
            },
            {"$limit": limit},
            {
                "$project": {
                    "_candidate_same_server": 0,
                    "_candidate_identity_strength": 0,
                    "_candidate_freshness": 0,
                    "_candidate_role_server": 0,
                    "_candidate_role_name": 0,
                }
            },
        ]
        cursor = self._col().aggregate(pipeline)
        docs = await cursor.to_list(limit)
        deduped: List[Dict[str, Any]] = []
        seen_role_keys = set()
        for doc in docs:
            candidate_server, candidate_name = _candidate_role_key(doc)
            if candidate_server == ns and candidate_name == nn:
                continue
            dedupe_key = (candidate_server, candidate_name)
            if dedupe_key in seen_role_keys:
                continue
            seen_role_keys.add(dedupe_key)
            deduped.append(doc)
            if len(deduped) >= limit:
                break
        return deduped

    async def get_by_id(self, identity_id: Any) -> Optional[Dict[str, Any]]:
        """按 role_identities._id 查询身份，返回保留原生 _id 的文档。"""
        object_id = _coerce_object_id(identity_id)
        if object_id is None:
            return None
        return await self._col().find_one({"_id": object_id})

    async def resolve_ranking_identities_batch(
        self, players: List[Dict[str, Any]],
    ) -> List[Optional[Dict[str, Any]]]:
        """Resolve in batches; upsert only missing roles or incomplete query locators.

        A conflicting or ambiguous candidate is unknown, never a name-based merge.
        Full documents retain the native identity ID and cached kungfu result.
        """
        if not players:
            return []
        candidates: Dict[Any, Dict[str, Any]] = {}
        for start in range(0, len(players), 250):
            clauses = []
            for player in players[start:start + 250]:
                for token in _ranking_tokens(player):
                    kind, *parts = token
                    if kind in ("global_id", "global_role_id"):
                        prefix = "global_id" if kind == "global_id" else "global"
                        value = _global_id_string_filter(parts[0]) if kind == "global_id" else parts[0]
                        clauses.extend([{kind: value}, {"identity_key": f"{prefix}:{parts[0]}"}])
                    elif kind == "game":
                        zone, game_id = parts
                        clauses.extend([{"zone": zone, "game_role_id": game_id},
                                        {"zone": zone, "role_id": game_id},
                                        {"identity_key": f"game:{zone}:{game_id}"}])
                    else:
                        server, name = parts
                        clauses.extend([{"normalized_server": server, "normalized_name": name},
                                        {"server": str(player.get("server") or "").strip(),
                                         "name": str(player.get("name") or "").strip()}])
            if clauses:
                for doc in await self._col().find({"$or": clauses}).to_list(None):
                    if isinstance(doc.get("_id"), ObjectId):
                        candidates[doc["_id"]] = doc

        by_token: Dict[Tuple[str, ...], Dict[Any, Dict[str, Any]]] = {}

        def index(doc):
            for token in _ranking_tokens(doc):
                by_token.setdefault(token, {})[doc["_id"]] = doc

        for doc in candidates.values():
            index(doc)
        resolved: Dict[Tuple[Any, ...], Optional[Dict[str, Any]]] = {}
        result: List[Optional[Dict[str, Any]]] = []
        fields = ("server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id", "person_id")
        for player in players:
            values = {field: str(player.get(field) or "").strip() for field in fields}
            signature = tuple(values[field] for field in fields)
            if signature not in resolved:
                found = None
                ids = _ranking_ids(player)
                valid = values["server"] and values["name"] and all(len(v) <= 1 for v in ids.values())
                tokens = _ranking_tokens(player)
                groups = [by_token[token] for token in tokens if by_token.get(token)]
                # Token order is global ID, SK01, game ID, then name. A higher
                # strength collision must not fall through to a weaker match.
                if valid and groups:
                    group = groups[0]
                    if len(group) == 1:
                        candidate = next(iter(group.values()))
                        consistent_ids = all(candidate["_id"] in by_token[token]
                                             for token in tokens
                                             if token[0] != "name" and by_token.get(token))
                        if consistent_ids and _ranking_compatible(player, candidate):
                            found = candidate
                            if _needs_query_locator(values, candidate):
                                enriched = await self.upsert_from_ranking_with_id(
                                    **{field: value or None for field, value in values.items()})
                                found = enriched if _is_valid_enriched_identity(player, candidate, enriched) else None
                                if found is not None:
                                    index(found)
                elif valid:
                    candidate = await self.upsert_from_ranking_with_id(
                        **{field: value or None for field, value in values.items()})
                    if (candidate and isinstance(candidate.get("_id"), ObjectId)
                            and _ranking_compatible(player, candidate)
                            and set(tokens).intersection(_ranking_tokens(candidate))):
                        found = candidate
                        index(candidate)
                resolved[signature] = found
            result.append(resolved[signature])
        return result

    async def publish_jjc_kungfu_if_newer(
        self, identity_id: Any, expected_locator: Dict[str, Any],
        generation: int, result: Dict[str, Any],
        *, display_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Publish one newer result without touching the identity observation clock."""
        object_id = _coerce_object_id(identity_id)
        if (object_id is None or not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0
                or not isinstance(result.get("publish_version"), int) or isinstance(result.get("publish_version"), bool)
                or result["publish_version"] != generation):
            raise ValueError("invalid role kungfu publication version")
        query: Dict[str, Any] = {"_id": object_id}
        fields = {"jjc_kungfu": dict(result)}
        if display_update is not None:
            fields.update(_display_publication_fields(object_id, expected_locator, generation, result, display_update))
        for field in ("server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id"):
            if field not in expected_locator:
                query[field] = {"$exists": False}
            else:
                query[field] = {"$eq": expected_locator[field], "$exists": True}
        # Query $type also matches array elements. Expression $type checks the
        # scalar value within the same atomic update, including concurrent changes.
        query["$or"] = [
            {"jjc_kungfu": {"$exists": False}},
            {"$expr": {"$and": [
                {"$eq": [{"$type": "$jjc_kungfu"}, "object"]},
                {"$in": [{"$type": "$jjc_kungfu.publish_version"}, ["int", "long"]]},
                {"$gt": ["$jjc_kungfu.publish_version", 0]},
                {"$lt": ["$jjc_kungfu.publish_version", generation]},
            ]}},
        ]
        updated = await self._col().update_one(query, {"$set": fields})
        return updated.matched_count == 1

    async def resolve_best_identity(
        self,
        server: str,
        name: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        global_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按优先级查找最佳匹配身份：global_id > global_role_id > zone+game_role_id > server+name。"""
        replay_gid = (global_id or "").strip()
        if replay_gid:
            doc = await self.find_by_global_id(replay_gid)
            if doc:
                return doc

        gid = (global_role_id or "").strip()
        if gid:
            doc = await self.find_by_global_role_id(gid)
            if doc:
                return doc

        z = (zone or "").strip()
        grid = (game_role_id or "").strip()
        if z and grid:
            doc = await self.find_by_game_role_id(z, grid)
            if doc:
                return doc

        docs = await self.find_by_name(server, name)
        return docs[0] if docs else None

    async def resolve_best_identity_with_id(
        self,
        server: str,
        name: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        global_id: Optional[str] = None,
        projection: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        """按优先级查找最佳匹配身份，返回保留原生 _id 的文档。"""
        replay_gid = (global_id or "").strip()
        if replay_gid:
            doc = await self._col().find_one({
                "$or": [
                    {"global_id": _global_id_string_filter(replay_gid)},
                    {"identity_key": f"global_id:{replay_gid}"},
                ]
            }, projection)
            if doc:
                return doc

        gid = (global_role_id or "").strip()
        if gid:
            doc = await self._col().find_one({
                "$or": [
                    {"global_role_id": gid},
                    {"identity_key": f"global:{gid}"},
                ]
            }, projection)
            if doc:
                return doc

        z = (zone or "").strip()
        grid = (game_role_id or "").strip()
        if z and grid:
            doc = await self._col().find_one({
                "$or": [
                    {"zone": z, "game_role_id": grid},
                    {"zone": z, "role_id": grid},
                    {"identity_key": f"game:{z}:{grid}"},
                ]
            }, projection)
            if doc:
                return doc

        ns = _normalize(server)
        nn = _normalize(name)
        return await self.find_best_by_name_with_id(ns, nn, projection)

    # ---- upsert 入口 ----

    async def upsert_from_ranking(
        self,
        server: str,
        name: str,
        zone: str,
        game_role_id: str,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        global_id: Optional[str] = None,
        cache_repo: Any = None,
    ) -> Dict[str, Any]:
        """从排行榜数据写入或升级身份。"""
        return await self._upsert_identity(
            server=server, name=name, zone=zone, game_role_id=game_role_id,
            global_role_id=global_role_id, role_id=role_id, person_id=person_id,
            global_id=global_id, source="ranking", cache_repo=cache_repo,
        )

    async def upsert_from_ranking_with_id(
        self,
        server: str,
        name: str,
        zone: str,
        game_role_id: str,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        global_id: Optional[str] = None,
        cache_repo: Any = None,
    ) -> Dict[str, Any]:
        """从排行榜数据写入或升级身份，返回保留原生 _id 的文档。"""
        return await self._upsert_identity(
            server=server, name=name, zone=zone, game_role_id=game_role_id,
            global_role_id=global_role_id, role_id=role_id, person_id=person_id,
            global_id=global_id, source="ranking", cache_repo=cache_repo,
            preserve_id=True,
        )

    async def upsert_from_indicator(
        self,
        server: str,
        name: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        global_id: Optional[str] = None,
        cache_repo: Any = None,
        preserve_id: bool = False,
    ) -> Dict[str, Any]:
        """从 indicator 接口数据写入或升级身份。"""
        return await self._upsert_identity(
            server=server, name=name, zone=zone, game_role_id=game_role_id,
            global_role_id=global_role_id, role_id=role_id, person_id=person_id,
            global_id=global_id,
            source="indicator", cache_repo=cache_repo,
            **({"preserve_id": True} if preserve_id else {}),
        )

    async def upsert_from_match_detail(
        self,
        server: str,
        name: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        observed_match_time: Optional[int] = None,
        global_id: Optional[str] = None,
        cache_repo: Any = None,
    ) -> Dict[str, Any]:
        """从对局详情数据写入或升级身份。"""
        return await self._upsert_identity(
            server=server, name=name, zone=zone, game_role_id=game_role_id,
            global_role_id=global_role_id, role_id=role_id, person_id=person_id,
            global_id=global_id, source="match_detail", observed_at=observed_at,
            observed_match_time=observed_match_time, cache_repo=cache_repo,
        )

    async def upsert_from_match_detail_with_id(
        self,
        server: str,
        name: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        observed_match_time: Optional[int] = None,
        global_id: Optional[str] = None,
        cache_repo: Any = None,
    ) -> Dict[str, Any]:
        """从对局详情数据写入或升级身份，返回保留原生 _id 的文档。"""
        return await self._upsert_identity(
            server=server, name=name, zone=zone, game_role_id=game_role_id,
            global_role_id=global_role_id, role_id=role_id, person_id=person_id,
            global_id=global_id, source="match_detail", observed_at=observed_at,
            observed_match_time=observed_match_time, cache_repo=cache_repo,
            preserve_id=True,
        )

    async def refresh_indicator_fields_by_id(
        self,
        identity_id: Any,
        global_role_id: str,
        refresh_source: str = "indicator",
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        server: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按 _id 刷新 indicator 解析出的身份字段，返回保留原生 _id 的文档。"""
        object_id = _coerce_object_id(identity_id)
        if object_id is None:
            return None

        existing = await self.get_by_id(object_id)
        if not existing:
            return None
        now = datetime.now(timezone.utc)
        optional_fields = {
            "global_role_id": global_role_id,
            "zone": zone,
            "game_role_id": game_role_id,
            "role_id": role_id,
            "person_id": person_id,
            "server": server,
            "name": name,
        }
        if server is not None:
            optional_fields["normalized_server"] = _normalize(server)
        if name is not None:
            optional_fields["normalized_name"] = _normalize(name)
        set_fields = build_guarded_profile_set_fields(existing, optional_fields)
        set_fields["updated_at"] = now
        if not has_verified_query_ids(existing):
            for field, value in optional_fields.items():
                if value is not None and value != "" and value == existing.get(field):
                    set_fields[field] = value
            # Caller labels are not evidence, even if they copy the strict label.
            set_fields["global_role_id_refreshed_at"] = now
            set_fields["global_role_id_refresh_source"] = (
                "indicator" if refresh_source == STRICT_QUERY_ID_REFRESH_SOURCE else refresh_source
            )

        await self._col().update_one(_snapshot_filter(existing), {"$set": set_fields})
        return await self.get_by_id(object_id)

    async def replace_query_ids_if_unchanged(
        self,
        expected: Dict[str, Any],
        *,
        game_role_id: str,
        role_id: str,
        observed_at: datetime,
    ) -> bool:
        """Internal verified-evidence boundary; callers must validate raw responses first."""
        if (_coerce_object_id(expected.get("_id")) is None
                or not role_id or role_id != game_role_id
                or not expected.get("identity_key") or not expected.get("global_id")
                or not str(expected.get("global_role_id") or "").startswith("SK01-")
                or not isinstance(observed_at, datetime)):
            return False
        previous_observation = expected.get("global_role_id_refreshed_at")
        if has_verified_query_ids(expected) and isinstance(previous_observation, datetime):
            previous_utc = (previous_observation.replace(tzinfo=timezone.utc)
                            if previous_observation.tzinfo is None else previous_observation)
            observed_utc = (observed_at.replace(tzinfo=timezone.utc)
                            if observed_at.tzinfo is None else observed_at)
            if observed_utc < previous_utc:
                return False
        archived = await self._archive_profile_snapshot(
            expected, replaced_by_identity_key=expected["identity_key"],
            archive_reason="role_query_ids_recovered", source=STRICT_QUERY_ID_REFRESH_SOURCE,
            observed_match_time=None, archived_at=observed_at,
        )
        if not archived:
            return False
        try:
            result = await self._col().update_one(_snapshot_filter(expected), {"$set": {
                "game_role_id": game_role_id, "role_id": role_id,
                "global_role_id_refreshed_at": observed_at,
                "global_role_id_refresh_source": STRICT_QUERY_ID_REFRESH_SOURCE,
                "updated_at": observed_at,
            }})
            return result.matched_count == 1
        except DuplicateKeyError:
            return False

    async def rollback_query_ids_if_unchanged(
        self, expected_after: Dict[str, Any], before: Dict[str, Any],
    ) -> bool:
        """Conditionally restore only this operation's fields and their missing state."""
        if (_coerce_object_id(expected_after.get("_id")) is None
                or expected_after.get("_id") != before.get("_id")
                or not has_verified_query_ids(expected_after)):
            return False
        update: Dict[str, Any] = {}
        restore = {field: before[field] for field in _QUERY_ID_UPDATE_FIELDS if field in before}
        remove = {field: "" for field in _QUERY_ID_UPDATE_FIELDS if field not in before}
        if restore:
            update["$set"] = restore
        if remove:
            update["$unset"] = remove
        try:
            result = await self._col().update_one(_snapshot_filter(expected_after), update)
            return result.matched_count == 1
        except DuplicateKeyError:
            return False

    # ---- 显式升级 ----

    async def upgrade_identity(
        self,
        current_identity_key: str,
        global_role_id: Optional[str] = None,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_id: Optional[str] = None,
        cache_repo: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """显式升级身份：当获得更高级别外部 ID 时调用。

        若新 key 与当前 key 同级或更低，不做任何操作并返回现有文档。
        升级后将旧 identity_key 推入 aliases。
        若提供 cache_repo，升级成功时同步迁移 role_jjc_cache 中的 identity_key。
        """
        existing = await self._col().find_one({"identity_key": current_identity_key})
        if not existing:
            return None
        if has_verified_query_ids(existing):
            existing.pop("_id", None)
            return existing

        new_key, new_level = build_identity_key(
            global_id=global_id,
            global_role_id=global_role_id,
            zone=zone or existing.get("zone"),
            game_role_id=game_role_id or existing.get("game_role_id"),
            server=existing.get("server", ""),
            name=existing.get("name", ""),
        )

        current_level = existing.get("identity_level", "name")
        if _LEVEL_ORDER.get(new_level, 0) <= _LEVEL_ORDER.get(current_level, 0):
            existing.pop("_id", None)
            return existing

        now = datetime.now(timezone.utc)
        set_fields: Dict[str, Any] = {
            "identity_key": new_key,
            "identity_level": new_level,
            "updated_at": now,
        }
        if global_role_id:
            set_fields["global_role_id"] = global_role_id
        if global_id:
            set_fields["global_id"] = global_id
        if zone:
            set_fields["zone"] = zone
        if game_role_id:
            set_fields["game_role_id"] = game_role_id

        try:
            result = await self._col().update_one(
                _snapshot_filter(existing),
                {
                    "$set": set_fields,
                    "$push": {"aliases": current_identity_key},
                },
            )
        except DuplicateKeyError:
            logger.warning(
                "身份升级冲突: old_key={} new_key={} 目标已存在，跳过升级",
                current_identity_key, new_key,
            )
            existing.pop("_id", None)
            return existing

        if result.matched_count != 1:
            latest = await self._col().find_one({"_id": existing["_id"]} if "_id" in existing
                                                else {"identity_key": current_identity_key})
            return self._strip_id(latest or existing)

        if cache_repo is not None:
            await cache_repo.migrate_identity_key(current_identity_key, new_key)

        doc = await self._col().find_one({"identity_key": new_key})
        if doc:
            doc.pop("_id", None)
        return doc

    # ---- 内部实现 ----

    async def _upsert_identity(
        self,
        server: str,
        name: str,
        source: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        global_id: Optional[str] = None,
        role_id: Optional[str] = None,
        person_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        observed_match_time: Optional[int] = None,
        cache_repo: Any = None,
        preserve_id: bool = False,
    ) -> Dict[str, Any]:
        """通用 upsert：查找已有身份 → 可能升级 → 新建或更新。"""
        now = datetime.now(timezone.utc)
        profile_observed_at = observed_at or now
        profile_observed_match_time = (
            coerce_match_time(observed_match_time)
            if observed_match_time is not None
            else coerce_match_time(observed_at)
        )
        effective_game_role_id = game_role_id or role_id
        ns = _normalize(server)
        nn = _normalize(name)

        if preserve_id:
            projection = MATCH_DETAIL_IDENTITY_PROJECTION if source == "match_detail" else None
            existing = await self.resolve_best_identity_with_id(
                server=server, name=name,
                zone=zone, game_role_id=effective_game_role_id,
                global_role_id=global_role_id, global_id=global_id,
                projection=projection,
            )
        else:
            existing = await self.resolve_best_identity(
                server=server, name=name,
                zone=zone, game_role_id=effective_game_role_id,
                global_role_id=global_role_id, global_id=global_id,
            )

        if existing:
            result = await self._update_existing(
                existing, server, name, ns, nn,
                zone, effective_game_role_id, global_role_id, global_id, role_id, person_id,
                source, now, profile_observed_at, profile_observed_match_time,
                cache_repo=cache_repo, preserve_id=preserve_id,
            )
            return result

        # 无已有身份 → 新建
        identity_key, identity_level = build_identity_key(
            global_role_id=global_role_id, zone=zone, game_role_id=effective_game_role_id,
            server=server, name=name, global_id=global_id,
        )
        doc = {
            "identity_key": identity_key,
            "identity_level": identity_level,
            "server": server,
            "normalized_server": ns,
            "name": name,
            "normalized_name": nn,
            "role_id": role_id or None,
            "person_id": person_id or None,
            "aliases": [],
            "sources": [source],
            "profile_observed_at": profile_observed_at,
            "first_seen_at": now,
            "last_seen_at": now,
            "updated_at": now,
            "schema_version": SCHEMA_VERSION,
        }
        if profile_observed_match_time is not None:
            doc["role_info_observed_match_time"] = profile_observed_match_time
            doc["role_info_source"] = source
            doc["role_info_updated_at"] = now.timestamp()
        # 只写入有实际值的字段，避免 null 参与 partial unique 索引导致冲突
        if zone:
            doc["zone"] = zone
        if effective_game_role_id:
            doc["game_role_id"] = effective_game_role_id
        if global_role_id:
            doc["global_role_id"] = global_role_id
        if global_id:
            doc["global_id"] = global_id

        try:
            insert_result = await self._col().insert_one(doc)
            if preserve_id and "_id" not in doc:
                doc["_id"] = insert_result.inserted_id
        except DuplicateKeyError:
            logger.warning("插入身份冲突（并发写入），重新查找: key={}", identity_key)
            existing = await self._col().find_one({"identity_key": identity_key})
            if existing:
                if not preserve_id:
                    existing.pop("_id", None)
                return await self._update_existing(
                    existing, server, name, ns, nn,
                    zone, effective_game_role_id, global_role_id, global_id, role_id, person_id,
                    source, now, profile_observed_at, profile_observed_match_time,
                    cache_repo=cache_repo, preserve_id=preserve_id,
                )
            raise

        if not preserve_id:
            doc.pop("_id", None)
        return doc

    async def _update_existing(
        self,
        existing: Dict[str, Any],
        server: str,
        name: str,
        ns: str,
        nn: str,
        zone: Optional[str],
        game_role_id: Optional[str],
        global_role_id: Optional[str],
        global_id: Optional[str],
        role_id: Optional[str],
        person_id: Optional[str],
        source: str,
        now: datetime,
        profile_observed_at: datetime,
        observed_match_time: Optional[int],
        cache_repo: Any = None,
        preserve_id: bool = False,
    ) -> Dict[str, Any]:
        """更新已有身份记录，必要时执行身份升级。"""
        current_key: str = existing["identity_key"]
        current_level: str = existing.get("identity_level", "name")
        existing_global_id = (existing.get("global_id") or "").strip()
        incoming_global_id = (global_id or "").strip()
        if existing_global_id and incoming_global_id and existing_global_id != incoming_global_id:
            logger.warning(
                "身份 global_id 冲突，跳过更新: identity_key={} existing_global_id={} incoming_global_id={}",
                current_key,
                existing_global_id,
                incoming_global_id,
            )
            if not preserve_id:
                existing.pop("_id", None)
            return existing

        new_key, new_level = build_identity_key(
            global_id=global_id, global_role_id=global_role_id, zone=zone, game_role_id=game_role_id,
            server=server, name=name,
        )

        needs_upgrade = (not has_verified_query_ids(existing)
                         and _LEVEL_ORDER.get(new_level, 0) > _LEVEL_ORDER.get(current_level, 0))

        set_fields: Dict[str, Any] = {
            "last_seen_at": now,
            "updated_at": now,
        }
        should_update_profile = should_overwrite_profile_fields(
            existing,
            observed_match_time=observed_match_time,
            force_profile_update=False,
        )
        incoming_profile_fields = {
            "server": server,
            "normalized_server": ns,
            "name": name,
            "normalized_name": nn,
            "zone": zone,
            "game_role_id": game_role_id,
            "global_role_id": global_role_id,
            "role_id": role_id,
            "person_id": person_id,
        }
        set_fields.update(build_guarded_profile_set_fields(
            existing,
            incoming_profile_fields,
            observed_match_time=observed_match_time,
            force_profile_update=False,
        ))
        if should_update_profile:
            set_fields["profile_observed_at"] = profile_observed_at
            if observed_match_time is not None:
                set_fields["role_info_observed_match_time"] = observed_match_time
                set_fields["role_info_source"] = source
                set_fields["role_info_updated_at"] = now.timestamp()
        if global_id and not existing_global_id:
            set_fields["global_id"] = global_id

        if needs_upgrade:
            set_fields["identity_key"] = new_key
            set_fields["identity_level"] = new_level
            if global_id:
                set_fields["global_id"] = global_id

        update_op: Dict[str, Any] = {
            "$set": set_fields,
            "$addToSet": {
                "sources": source,
            },
        }
        if needs_upgrade:
            aliases = [current_key]
            aliases.extend(legacy_identity_keys(
                global_role_id=global_role_id,
                zone=zone,
                game_role_id=game_role_id,
                server=server,
                name=name,
            ))
            update_op["$addToSet"]["aliases"] = {"$each": sorted(set(aliases))}

        should_archive = self._should_archive_profile_change(existing, set_fields)
        if should_archive:
            archived = await self._archive_profile_snapshot(
                existing,
                replaced_by_identity_key=new_key if needs_upgrade else current_key,
                archive_reason="role_identity_profile_updated",
                source=source,
                observed_match_time=observed_match_time,
                archived_at=now,
            )
            if not archived:
                if not preserve_id:
                    existing.pop("_id", None)
                return existing

        try:
            result = await self._col().update_one(
                _snapshot_filter(existing),
                update_op,
            )
        except DuplicateKeyError:
            logger.warning(
                "身份升级冲突: old_key={} new_key={} 目标已存在，保留当前记录",
                current_key, new_key,
            )
            if not preserve_id:
                existing.pop("_id", None)
            return existing

        if result.matched_count != 1:
            latest = await self._col().find_one({"_id": existing["_id"]} if "_id" in existing
                                                else {"identity_key": current_key})
            latest = latest or existing
            if not preserve_id:
                latest.pop("_id", None)
            return latest

        if needs_upgrade and cache_repo is not None:
            await cache_repo.migrate_identity_key(current_key, new_key)

        lookup_key = new_key if needs_upgrade else current_key
        projection = MATCH_DETAIL_IDENTITY_PROJECTION if source == "match_detail" and preserve_id else None
        doc = await self._col().find_one({"identity_key": lookup_key}, projection)
        if doc and not preserve_id:
            doc.pop("_id", None)
        return doc or existing

    def _should_archive_profile_change(
        self,
        existing: Dict[str, Any],
        set_fields: Dict[str, Any],
    ) -> bool:
        for field_name in ("server", "name", "global_role_id"):
            if field_name not in set_fields:
                continue
            old_value = str(existing.get(field_name) or "").strip()
            new_value = str(set_fields.get(field_name) or "").strip()
            if old_value and new_value and old_value != new_value:
                return True
        return False

    async def _archive_profile_snapshot(
        self,
        existing: Dict[str, Any],
        replaced_by_identity_key: str,
        archive_reason: str,
        source: str,
        observed_match_time: Optional[int],
        archived_at: datetime,
    ) -> bool:
        archive_doc = dict(existing)
        archive_doc.pop("_id", None)
        archive_doc["archived_at"] = archived_at
        archive_doc["archive_reason"] = archive_reason
        archive_doc["replaced_by_identity_key"] = replaced_by_identity_key
        archive_doc["archive_source"] = source
        if observed_match_time is not None:
            archive_doc["replaced_by_observed_match_time"] = observed_match_time
        try:
            await self._history_col().insert_one(archive_doc)
            return True
        except Exception as exc:
            logger.warning(
                "归档角色身份旧画像失败: identity_key={} replaced_by={} error={}".format(
                    existing.get("identity_key"),
                    replaced_by_identity_key,
                    exc,
                )
            )
            return False

    async def get_equipment_bindings_by_ids(self, ids: List[Any]) -> List[Dict[str, Any]]:
        """Strict identity-only batch for equipment reads; never fetch display packets."""
        object_ids = list(dict.fromkeys(object_id for object_id in map(_coerce_object_id, ids)
                                        if object_id is not None))
        if not object_ids:
            return []
        projection = {"_id": 1, **{key: 1 for key in (
            "server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")}}
        return await self._col().find({"_id": {"$in": object_ids}}, projection).max_time_ms(1500).to_list(None)

    async def get_display_by_ids(self, ids: List[Any]) -> List[Dict[str, Any]]:
        """Read only display bindings and packets in one _id batch; never hydrate."""
        object_ids = list(dict.fromkeys(object_id for object_id in map(_coerce_object_id, ids)
                                        if object_id is not None))
        if not object_ids:
            return []
        projection = {"_id": 1, "jjc_display": 1, **{key: 1 for key in (
            "server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")}}
        return await self._col().find({"_id": {"$in": object_ids}}, projection).to_list(None)
