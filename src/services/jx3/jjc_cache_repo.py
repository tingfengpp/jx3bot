from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.infra.mongo import get_db as _get_db

try:
    from nonebot import logger  # type: ignore
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from src.utils.jjc_query_observability import foreground_safe_logger

logger = foreground_safe_logger(logger, component="cache")


def _normalize_role_name(raw_name: Any, player_server: Any, target_server: str) -> str:
    """对 role_name 做归一化：仅当末尾 ·xxx 等于 own server 或 target server 时去除。"""
    name_str = str(raw_name) if raw_name is not None else ""
    if "·" not in name_str:
        return name_str
    parts = name_str.rsplit("·", 1)
    if len(parts) != 2:
        return name_str
    base, suffix = parts
    if suffix == str(player_server) or suffix == target_server:
        return base
    return name_str


def _match_player_in_detail(
    player: Dict[str, Any],
    *,
    server: str,
    name: str,
    role_id: Optional[str] = None,
    global_id: Optional[str] = None,
) -> bool:
    player_server = player.get("server")
    raw_name = player.get("role_name") or player.get("person_name")
    if not player_server or not raw_name:
        return False

    display_name = _normalize_role_name(raw_name, player_server, server)

    server_match = str(player_server) == server
    name_match = display_name == name

    if server_match and name_match:
        return True

    if role_id:
        player_role_id = player.get("role_id")
        if player_role_id and str(player_role_id) == str(role_id):
            return True

    if global_id:
        player_global_id = player.get("global_id")
        if player_global_id and str(player_global_id) == str(global_id):
            return True

    return False


def _is_replay_global_id(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.upper().startswith("SK")


def _build_match_detail_win_history_query(
    *,
    server: str,
    name: str,
    season_start_ts: Optional[float],
    role_id: Optional[str],
    global_id: Optional[str],
) -> Dict[str, Any]:
    role_names = [name, "{}·{}".format(name, server)]
    player_match_or: List[Dict[str, Any]] = [
        {
            "data.detail.team1.players_info": {
                "$elemMatch": {"server": server, "role_name": {"$in": role_names}}
            }
        },
        {
            "data.detail.team2.players_info": {
                "$elemMatch": {"server": server, "role_name": {"$in": role_names}}
            }
        },
    ]

    stable_match_terms: List[Dict[str, Any]] = []
    if role_id:
        stable_match_terms.append({"role_id": str(role_id)})
        player_match_or.extend([
            {"data.detail.team1.players_info": {"$elemMatch": {"role_id": str(role_id)}}},
            {"data.detail.team2.players_info": {"$elemMatch": {"role_id": str(role_id)}}},
        ])
    if global_id and _is_replay_global_id(global_id):
        stable_match_terms.append({"global_role_id": str(global_id)})
        player_match_or.extend([
            {"data.detail.team1.players_info": {"$elemMatch": {"global_id": str(global_id)}}},
            {"data.detail.team2.players_info": {"$elemMatch": {"global_id": str(global_id)}}},
        ])
    if stable_match_terms:
        player_match_or.append({
            "data.replay.data.players": {"$elemMatch": {"$or": stable_match_terms}}
        })

    query: Dict[str, Any] = {
        "data.detail": {"$ne": None},
        "$or": player_match_or,
    }
    if season_start_ts is not None:
        query["data.detail.match_time"] = {"$gte": season_start_ts}
    return query


@dataclass(frozen=True)
class JjcCacheRepo:
    jjc_ranking_cache_duration: int
    kungfu_cache_duration: int
    db: Optional[AsyncIOMotorDatabase] = None

    # ---- helpers (lazy repo access to avoid circular imports) ----

    async def load_query_id_recovery_candidates(self, identity: Dict[str, Any]) -> list:
        """Read bounded indexed evidence; expired values are locators, never metrics."""
        from src.services.jx3.role_identity_recovery import collect_query_id_candidates
        from src.services.jx3.role_identity_matching import extract_replay_players

        db = self.db if self.db is not None else _get_db()
        sources = []
        identity_key = identity.get("identity_key")
        keys = [identity_key]
        if identity.get("global_role_id"):
            keys.append("global:" + str(identity["global_role_id"]))
        for key in dict.fromkeys(key for key in keys if key):
            cached = await db.jjc_role_indicator.find_one({"cache_key": key})
            if cached:
                sources.append(cached)
        if identity_key:
            cached = await db.role_jjc_cache.find_one({"identity_key": identity_key})
            if cached:
                sources.append(cached)
            async for historical in db.role_identities_history.find({"identity_key": identity_key}).limit(32):
                sources.append(historical)
        numeric_id = str(identity.get("global_id") or "")
        if numeric_id.isdigit():
            cursor = db.jjc_match_detail.find(
                {"data.replay.data.players.global_role_id": numeric_id},
                {"match_id": 1, "data.replay": 1},
            ).sort("cached_at", -1).limit(16)
            async for document in cursor:
                replay = (document.get("data") or {}).get("replay") or {}
                for player in extract_replay_players(replay):
                    if player.get("global_id") == numeric_id:
                        sources.append(dict(player, match_id=document.get("match_id")))
        return collect_query_id_candidates(identity, sources)

    def _get_identity_repo(self):
        from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
        return RoleIdentityRepo(db=self.db if self.db is not None else _get_db())

    def _get_jjc_cache_repo(self):
        from src.storage.mongo_repos.role_jjc_cache_repo import RoleJjcCacheRepo
        return RoleJjcCacheRepo(db=self.db if self.db is not None else _get_db())

    async def resolve_role_identity(
        self,
        *,
        server: str,
        name: str,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        preserve_id: bool = False,
    ) -> Optional[dict[str, Any]]:
        identity_repo = self._get_identity_repo()
        hints = {"global_role_id": global_role_id} if global_role_id else {}
        finder = identity_repo.resolve_best_identity_with_id if preserve_id else identity_repo.resolve_best_identity
        return await finder(
            server=server,
            name=name,
            zone=zone,
            game_role_id=game_role_id,
            global_id=global_id,
            **hints,
        )

    async def upsert_role_identity_from_indicator(
        self,
        *,
        server: str,
        name: str,
        zone: Optional[str],
        game_role_id: Optional[str],
        global_role_id: Optional[str],
        role_id: Optional[str],
        person_id: Optional[str] = None,
        global_id: Optional[str] = None,
        preserve_id: bool = False,
    ) -> dict[str, Any]:
        identity_repo = self._get_identity_repo()
        jjc_repo = self._get_jjc_cache_repo()
        return await identity_repo.upsert_from_indicator(
            server=server,
            name=name,
            zone=zone,
            game_role_id=game_role_id,
            global_role_id=global_role_id,
            role_id=role_id,
            person_id=person_id,
            global_id=global_id,
            cache_repo=jjc_repo,
            **({"preserve_id": True} if preserve_id else {}),
        )

    async def load_new_kungfu_cache_raw(self, server: str, name: str) -> Optional[dict[str, Any]]:
        jjc_repo = self._get_jjc_cache_repo()
        return await jjc_repo.load_by_best_identity(server=server, name=name)

    @staticmethod
    def _new_doc_to_compat(doc: dict[str, Any]) -> dict[str, Any]:
        """将 role_jjc_cache 文档转为当前服务兼容结构，补充 cache_time。"""
        result = dict(doc)
        checked_at = result.get("checked_at")
        if isinstance(checked_at, datetime):
            result["cache_time"] = checked_at.timestamp()
        elif "cache_time" not in result:
            result["cache_time"] = 0.0
        return result

    @staticmethod
    def _ensure_found_flag(cached_data: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not isinstance(cached_data, dict):
            return cached_data
        if "found" not in cached_data:
            cached_data["found"] = cached_data.get("kungfu") not in (None, "")
        return cached_data

    @staticmethod
    def _summarize_cache_doc(cached_data: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(cached_data, dict):
            return {}
        teammates = cached_data.get("teammates")
        teammate_kungfu_missing = 0
        if isinstance(teammates, list):
            teammate_kungfu_missing = sum(
                1
                for item in teammates
                if not isinstance(item, dict) or item.get("kungfu_id") in (None, "")
            )
        return {
            "identity_key": cached_data.get("identity_key"),
            "global_role_id": cached_data.get("global_role_id"),
            "game_role_id": cached_data.get("game_role_id"),
            "role_id": cached_data.get("role_id"),
            "zone": cached_data.get("zone"),
            "kungfu": cached_data.get("kungfu"),
            "kungfu_id": cached_data.get("kungfu_id"),
            "kungfu_indicator": cached_data.get("kungfu_indicator"),
            "kungfu_match_history": cached_data.get("kungfu_match_history"),
            "kungfu_selected_source": cached_data.get("kungfu_selected_source"),
            "weapon_checked": cached_data.get("weapon_checked"),
            "teammates_checked": cached_data.get("teammates_checked"),
            "match_history_checked": cached_data.get("match_history_checked"),
            "cached_match_detail_win_count": cached_data.get("cached_match_detail_win_count"),
            "cached_match_detail_total_count": cached_data.get("cached_match_detail_total_count"),
            "cached_match_detail_latest_win_match_id": cached_data.get(
                "cached_match_detail_latest_win_match_id"
            ),
            "teammates_count": len(teammates) if isinstance(teammates, list) else None,
            "teammates_missing_kungfu_id_count": teammate_kungfu_missing,
            "cache_time": cached_data.get("cache_time"),
            "checked_at": cached_data.get("checked_at"),
        }

    def _check_freshness(
        self,
        cached_data: dict[str, Any],
        server: str,
        name: str,
        source: str = "",
    ) -> Optional[dict[str, Any]]:
        cache_time = cached_data.get("cache_time", 0)
        kungfu_value = cached_data.get("kungfu")
        weapon_checked = cached_data.get("weapon_checked", False)
        teammates_checked = cached_data.get("teammates_checked", False)
        teammates = cached_data.get("teammates")
        teammates_ok = (
            isinstance(teammates, list)
            and len(teammates) > 0
            and all(isinstance(item, dict) and item.get("kungfu_id") not in (None, "") for item in teammates)
        )

        if kungfu_value not in [None, ""]:
            current_time = time.time()
            cache_age = current_time - cache_time if cache_time else None
            cache_fresh = (
                cache_time
                and cache_age is not None
                and cache_age < self.kungfu_cache_duration
                and weapon_checked
                and teammates_checked
                and teammates_ok
            )
            if cache_fresh:
                logger.info(
                    "使用心法缓存{}: server={} name={} cache_time={} cache_age={} summary={}",
                    source,
                    server,
                    name,
                    cache_time,
                    round(cache_age, 1) if cache_age is not None else None,
                    self._summarize_cache_doc(cached_data),
                )
                return cached_data

            reasons = []
            if not cache_time:
                reasons.append("missing_cache_time")
            elif cache_age is not None and cache_age >= self.kungfu_cache_duration:
                reasons.append("cache_time_expired")
            if not weapon_checked:
                reasons.append("weapon_not_checked")
            if not teammates_checked:
                reasons.append("teammates_not_checked")
            if not teammates_ok:
                reasons.append("teammates_kungfu_id_missing")
            cache_dt = datetime.fromtimestamp(cache_time).strftime("%Y-%m-%d %H:%M:%S") if cache_time else "未知"
            reason_text = ",".join(reasons) if reasons else "unknown"
            logger.info(
                "心法缓存不命中{}: server={} name={} cache_time={} cache_age={} reason={} summary={}",
                source,
                server,
                name,
                cache_dt,
                round(cache_age, 1) if cache_age is not None else None,
                reason_text,
                self._summarize_cache_doc(cached_data),
            )
        else:
            logger.info(
                "心法缓存不命中{}: server={} name={} reason=kungfu_empty summary={}",
                source,
                server,
                name,
                self._summarize_cache_doc(cached_data),
            )

        return None

    # ---- 历史胜场心法兜底 ----

    async def get_kungfu_from_cached_match_detail_win_history(
        self,
        *,
        server: str,
        name: str,
        season_start: Optional[str] = None,
        role_id: Optional[str] = None,
        global_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        db = self.db if self.db is not None else _get_db()

        season_start_ts: Optional[float] = None
        if season_start:
            try:
                dt = datetime.strptime(season_start, "%Y-%m-%d")
                season_start_ts = dt.timestamp()
            except ValueError:
                logger.warning(
                    "无法解析赛季开始时间，将不加时间过滤: season_start=%s",
                    season_start,
                )

        query = _build_match_detail_win_history_query(
            server=server,
            name=name,
            season_start_ts=season_start_ts,
            role_id=str(role_id) if role_id else None,
            global_id=str(global_id) if global_id else None,
        )
        projection = {
            "_id": 0,
            "match_id": 1,
            "data.detail.match_time": 1,
            "data.detail.team1.won": 1,
            "data.detail.team1.players_info.server": 1,
            "data.detail.team1.players_info.role_name": 1,
            "data.detail.team1.players_info.person_name": 1,
            "data.detail.team1.players_info.role_id": 1,
            "data.detail.team1.players_info.global_id": 1,
            "data.detail.team1.players_info.kungfu": 1,
            "data.detail.team1.players_info.kungfu_id": 1,
            "data.detail.team2.won": 1,
            "data.detail.team2.players_info.server": 1,
            "data.detail.team2.players_info.role_name": 1,
            "data.detail.team2.players_info.person_name": 1,
            "data.detail.team2.players_info.role_id": 1,
            "data.detail.team2.players_info.global_id": 1,
            "data.detail.team2.players_info.kungfu": 1,
            "data.detail.team2.players_info.kungfu_id": 1,
            "data.replay.data.players.role_id": 1,
            "data.replay.data.players.global_role_id": 1,
            "data.replay.data.players.role_name": 1,
            "data.replay.data.players.person_name": 1,
        }

        kungfu_stats: Dict[str, Dict[str, Any]] = {}

        try:
            cursor = db.jjc_match_detail.find(query, projection)
            async for doc in cursor:
                data_field = doc.get("data")
                if not isinstance(data_field, dict):
                    continue
                detail = data_field.get("detail")
                if not isinstance(detail, dict):
                    continue

                match_id = doc.get("match_id")
                match_time = detail.get("match_time")

                for team_key in ("team1", "team2"):
                    team = detail.get(team_key)
                    if not isinstance(team, dict):
                        continue
                    players = team.get("players_info") or []
                    if not isinstance(players, list):
                        continue

                    target_player: Optional[Dict[str, Any]] = None
                    for player in players:
                        if not isinstance(player, dict):
                            continue
                        if _match_player_in_detail(
                            player,
                            server=server,
                            name=name,
                            role_id=role_id,
                            global_id=global_id,
                        ):
                            target_player = player
                            break

                    if target_player is None and (role_id or global_id):
                        replay = data_field.get("replay")
                        if isinstance(replay, dict):
                            replay_data = replay.get("data")
                            if isinstance(replay_data, dict):
                                replay_players = replay_data.get("players") or []
                                if isinstance(replay_players, list):
                                    replay_role_name = None
                                    for rp in replay_players:
                                        if not isinstance(rp, dict):
                                            continue
                                        rp_role_id = rp.get("role_id")
                                        if role_id and rp_role_id is not None and str(rp_role_id) == str(role_id):
                                            replay_role_name = rp.get("role_name") or rp.get("person_name")
                                            break
                                        rp_global_role_id = rp.get("global_role_id")
                                        if global_id and rp_global_role_id is not None:
                                            rp_gr_str = str(rp_global_role_id)
                                            if _is_replay_global_id(rp_gr_str) and rp_gr_str == str(global_id):
                                                replay_role_name = rp.get("role_name") or rp.get("person_name")
                                                break
                                    if replay_role_name:
                                        replay_name = _normalize_role_name(replay_role_name, server, server)
                                        for player in players:
                                            if not isinstance(player, dict):
                                                continue
                                            p_server = player.get("server")
                                            p_name = player.get("role_name") or player.get("person_name")
                                            if not p_server or not p_name:
                                                continue
                                            norm_name = _normalize_role_name(p_name, p_server, server)
                                            if str(p_server) == server and norm_name == replay_name:
                                                target_player = player
                                                break

                    if target_player is None:
                        continue

                    kungfu = target_player.get("kungfu")
                    if not kungfu or not isinstance(kungfu, str) or not kungfu.strip():
                        continue
                    kungfu = kungfu.strip()

                    if kungfu not in kungfu_stats:
                        kungfu_stats[kungfu] = {
                            "wins": 0,
                            "total": 0,
                            "latest_win_match_id": None,
                            "latest_win_time": None,
                            "win_samples": [],
                            "kungfu_ids": [],
                        }

                    stats = kungfu_stats[kungfu]
                    stats["total"] += 1

                    kungfu_id = target_player.get("kungfu_id")
                    if kungfu_id is not None and kungfu_id not in stats["kungfu_ids"]:
                        stats["kungfu_ids"].append(kungfu_id)

                    won = bool(team.get("won"))
                    if won:
                        stats["wins"] += 1
                        if match_time is not None and (
                            stats["latest_win_time"] is None
                            or match_time > stats["latest_win_time"]
                        ):
                            stats["latest_win_time"] = match_time
                            stats["latest_win_match_id"] = match_id
                        if len(stats["win_samples"]) < 5:
                            stats["win_samples"].append(
                                {
                                    "match_id": match_id,
                                    "match_time": match_time,
                                    "kungfu": kungfu,
                                }
                            )

                    break
        except Exception as exc:
            logger.warning("从 jjc_match_detail 查询历史胜场心法失败: {}", exc)
            return None

        qualified = {
            k: v for k, v in kungfu_stats.items() if v["wins"] >= 3
        }

        if not qualified:
            logger.info(
                "历史胜场心法兜底未命中: server={} name={} reason=no_qualified_kungfu "
                "total_kungfus={}",
                server,
                name,
                len(kungfu_stats),
            )
            return None

        def _sort_key(item: tuple) -> tuple:
            _kungfu, stats = item
            return (
                -stats["wins"],
                -(stats["latest_win_time"] if stats["latest_win_time"] is not None else 0),
                -stats["total"],
            )

        sorted_kungfus = sorted(qualified.items(), key=_sort_key)
        best_kungfu, best_stats = sorted_kungfus[0]

        result: Dict[str, Any] = {
            "server": server,
            "name": name,
            "kungfu": best_kungfu,
            "kungfu_selected_source": "cached_match_detail_win_history",
            "found": True,
            "cached_match_detail_win_count": best_stats["wins"],
            "cached_match_detail_total_count": best_stats["total"],
            "cached_match_detail_latest_win_match_id": best_stats["latest_win_match_id"],
            "cached_match_detail_latest_win_time": best_stats["latest_win_time"],
            "cached_match_detail_win_samples": best_stats["win_samples"],
        }
        kungfu_ids: List[Any] = best_stats.get("kungfu_ids", [])
        if kungfu_ids:
            result["kungfu_id"] = kungfu_ids[0]

        logger.info(
            "历史胜场心法兜底命中: server={} name={} kungfu={} wins={} total={} "
            "latest_win_match_id={}",
            server,
            name,
            best_kungfu,
            best_stats["wins"],
            best_stats["total"],
            best_stats["latest_win_match_id"],
        )

        return result

    # ---- 排行榜缓存 (不变) ----

    async def load_ranking_cache(self) -> Optional[dict[str, Any]]:
        db = self.db if self.db is not None else _get_db()
        try:
            doc = await db.jjc_ranking_cache.find_one({"cache_key": "ranking"})
        except Exception as exc:
            logger.warning("读取竞技场排行榜缓存失败: {}", exc)
            return None
        if doc is None:
            logger.info("竞技场排行榜缓存未命中 (MongoDB)")
            return None
        cache_time = doc.get("cache_time", 0)
        if time.time() - cache_time >= self.jjc_ranking_cache_duration:
            logger.info("竞技场排行榜缓存已过期 (MongoDB)")
            return None
        logger.info("使用 MongoDB 缓存的竞技场排行榜数据")
        return doc.get("data")

    async def save_ranking_cache(self, ranking_result: dict[str, Any]) -> None:
        db = self.db if self.db is not None else _get_db()
        try:
            await db.jjc_ranking_cache.update_one(
                {"cache_key": "ranking"},
                {"$set": {
                    "cache_time": ranking_result.get("cache_time") or time.time(),
                    "data": ranking_result,
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            logger.info("竞技场排行榜数据已保存到 MongoDB 缓存")
        except Exception as exc:
            logger.warning("保存竞技场排行榜缓存失败: {}", exc)

    # ---- 心法/JJC 角色缓存 (灰度接入新集合) ----

    async def load_kungfu_cache_raw(self, server: str, name: str) -> Optional[dict[str, Any]]:
        try:
            doc = await self.load_new_kungfu_cache_raw(server, name)
            if doc is not None:
                compat_doc = self._ensure_found_flag(self._new_doc_to_compat(doc))
                logger.info(
                    "读取心法原始缓存命中(新集合): server={} name={} summary={}",
                    server,
                    name,
                    self._summarize_cache_doc(compat_doc),
                )
                return compat_doc
        except Exception as exc:
            logger.warning("从 role_jjc_cache 读取原始缓存失败: {}", exc)
        logger.info("心法原始缓存未命中新集合: server={} name={}", server, name)
        return None

    async def load_kungfu_cache(self, server: str, name: str) -> Optional[dict[str, Any]]:
        try:
            doc = await self.load_new_kungfu_cache_raw(server, name)
            if doc is not None:
                cached_data = self._ensure_found_flag(self._new_doc_to_compat(doc))
                if "found" not in doc:
                    logger.debug(
                        "新集合缓存缺少 found 字段，已按 kungfu 自动补齐: server={} name={} kungfu={} found={}",
                        server,
                        name,
                        cached_data.get("kungfu"),
                        cached_data.get("found"),
                    )
                result = self._check_freshness(cached_data, server, name, source="(新集合)")
                if result is None:
                    logger.info(
                        "新集合缓存未通过 freshness 校验: server={} name={}",
                        server,
                        name,
                    )
                    return None
                else:
                    return result
        except Exception as exc:
            logger.warning("从 role_jjc_cache 读取缓存失败: {}", exc)
            return None

        logger.info("心法缓存未命中新集合: server={} name={} reason=cache_miss", server, name)
        return None

    async def save_kungfu_cache(self, server: str, name: str, result: dict[str, Any]) -> None:
        try:
            jjc_repo = self._get_jjc_cache_repo()

            zone = result.get("zone")
            game_role_id = result.get("game_role_id")
            global_role_id = result.get("global_role_id")
            global_id = result.get("global_id")
            role_id = result.get("role_id")
            person_id = result.get("person_id")

            identity = await self.upsert_role_identity_from_indicator(
                server=server,
                name=name,
                zone=zone,
                game_role_id=game_role_id,
                global_role_id=global_role_id,
                role_id=role_id,
                person_id=person_id,
                global_id=global_id,
            )
            identity_key = identity["identity_key"]

            cache_data: dict[str, Any] = {
                "server": server,
                "name": name,
                "source": "ranking",
            }
            _copy_if_present(result, cache_data, [
                "zone", "game_role_id", "role_id", "global_id", "global_role_id",
                "kungfu", "kungfu_id", "kungfu_pinyin",
                "kungfu_indicator", "kungfu_match_history",
                "kungfu_selected_source",
                "weapon", "weapon_icon", "weapon_quality",
                "teammates",
                "cached_match_detail_win_count",
                "cached_match_detail_total_count",
                "cached_match_detail_latest_win_match_id",
                "cached_match_detail_latest_win_time",
                "cached_match_detail_win_samples",
            ])
            cache_data["weapon_checked"] = result.get("weapon_checked", False)
            cache_data["teammates_checked"] = result.get("teammates_checked", False)
            cache_data["found"] = result.get("found", result.get("kungfu") not in (None, ""))
            if "match_history_checked" in result:
                cache_data["match_history_checked"] = result["match_history_checked"]
            if "match_history_win_samples" in result:
                cache_data["match_history_win_samples"] = result["match_history_win_samples"]

            await jjc_repo.save(identity_key, cache_data)
            logger.info(
                "心法缓存已写入新集合: server={} name={} identity_key={}",
                server, name, identity_key,
            )
        except Exception as exc:
            logger.warning("写入新集合 (role_identities / role_jjc_cache) 失败: {}", exc)


def _copy_if_present(src: dict[str, Any], dst: dict[str, Any], keys: list) -> None:
    for k in keys:
        v = src.get(k)
        if v is not None:
            dst[k] = v
