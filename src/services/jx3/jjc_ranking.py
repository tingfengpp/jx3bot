from __future__ import annotations

import asyncio
import copy
import time
from uuid import uuid4
from weakref import WeakKeyDictionary
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from nonebot import logger
from src.utils.jjc_query_observability import foreground_safe_logger, observed_query_stage

logger = foreground_safe_logger(logger, component="ranking")
import config as cfg

from src.services.jx3.indicator_utils import parse_3v3_indicator
from src.services.jx3.role_identity_recovery import indicator_matches_request, recover_query_ids
from src.services.jx3.kungfu import get_role_indicator, get_kungfu_detail_by_role_info, merge_replay_global_ids_into_match_detail
from src.services.jx3.weapon_quality import extract_member_weapon_name, extract_weapon_name, is_jjc_legendary_weapon
from src.infra.mongo import get_db
from src.services.jx3.jjc_api_client import JjcApiClient
from src.services.jx3.jjc_role_query_context import ForegroundQueryError, ForegroundQueryTimeout
from src.infra.jjc_foreground_mongo import ForegroundMongoFailure
from src.services.jx3.jjc_cache_repo import JjcCacheRepo
from src.services.jx3.jjc_kungfu_policy import KungfuPolicy, make_scope
from src.services.jx3.jjc_kungfu_meta import get_jjc_kungfu_catalog
from src.services.jx3.jjc_ranking_display import display_context
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from src.storage.mongo_repos.jjc_kungfu_refresh_repo import JjcKungfuRefreshRepo
from src.services.jx3.tuilan_rate_limit import fixed_sleep, random_sleep
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo, EquipmentSnapshotError
from src.services.jx3.jjc_ranking_equipment import equipment_view_statistics
from src.storage.mongo_repos.jjc_sync_repo import JjcSyncRepo
from src.services.jx3.match_detail_persistence import MatchDetailPersistenceService


CACHED_MATCH_DETAIL_WIN_KUNGFU_FIELDS = (
    "cached_match_detail_win_count",
    "cached_match_detail_total_count",
    "cached_match_detail_latest_win_match_id",
    "cached_match_detail_latest_win_time",
    "cached_match_detail_win_samples",
    "kungfu_selected_source",
    "kungfu_id",
)

_RANKING_KUNGFU_FIELDS = (
    "kungfu_verified_at", "kungfu_source", "kungfu_freshness",
    "kungfu_unknown_reason", "kungfu_publish_version", "kungfu_acquisition",
    "display_identity_id", "display_locator", "display_scope",
    "display_packet", "display_attempt", "self_kungfu_id",
    "weapon_checked", "teammates_checked",
)


_EQUIPMENT_PUBLICATION_LOCKS = WeakKeyDictionary()


def _equipment_publication_lock():
    loop = asyncio.get_running_loop()
    if loop not in _EQUIPMENT_PUBLICATION_LOCKS:
        _EQUIPMENT_PUBLICATION_LOCKS[loop] = asyncio.Lock()
    return _EQUIPMENT_PUBLICATION_LOCKS[loop]


@dataclass(frozen=True)
class JjcRankingService:
    token: str
    ticket: str
    jjc_query_url: str
    arena_time_tag_url: str
    arena_ranking_url: str
    match_detail_url: str
    jjc_ranking_cache_duration: int
    kungfu_cache_duration: int
    current_season: Any
    current_season_start: str
    kungfu_healer_list: list[str]
    kungfu_dps_list: list[str]
    kungfu_pinyin_to_chinese: dict[str, str]
    tuilan_request: Callable[[str, dict[str, Any]], Any]
    defget_get: Callable[..., Awaitable[dict[str, Any]]]
    match_replay_url: Optional[str] = None
    match_detail_projection_service: Any = None
    match_detail_participant_projection_service: Any = None
    match_detail_persistence_service: Any = None
    inspect_repo: Optional[JjcInspectRepo] = None
    identity_repo: Any = None
    kungfu_refresh_repo: Any = None
    kungfu_query_service: Any = None
    kungfu_policy: Optional[Union[KungfuPolicy, Callable[[], KungfuPolicy]]] = None

    equipment_service: Any = None

    foreground_client: Any = None
    query_context: Any = None

    def _ranking_kungfu_policy(self) -> KungfuPolicy:
        if self.kungfu_policy is not None:
            return self.kungfu_policy() if callable(self.kungfu_policy) else self.kungfu_policy
        return KungfuPolicy(
            make_scope(str(self.current_season), self._coerce_season_start_time()),
            refresh_seconds=getattr(cfg, "JJC_KUNGFU_REFRESH_SECONDS", 21600),
            max_stale_seconds=getattr(cfg, "JJC_KUNGFU_MAX_STALE_SECONDS", 86400),
            track_seconds=getattr(cfg, "JJC_KUNGFU_TRACK_SECONDS", 172800),
        )

    @staticmethod
    def _ranking_identity_inputs(players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        identities = []
        for player in players:
            person = player.get("personInfo") or {}
            server = person.get("server", "")
            name = person.get("roleName", "")
            if name and str(name).endswith("·" + str(server)):
                name = name.rsplit("·", 1)[0]
            identities.append({
                "server": server, "name": name, "zone": person.get("zone"),
                "game_role_id": person.get("gameRoleId"),
                "role_id": person.get("roleId") or person.get("gameRoleId"),
                "global_role_id": person.get("globalRoleId"),
                "global_id": person.get("globalId"), "person_id": person.get("personId"),
            })
        return identities

    async def _prewarm_ranking_kungfus(self, ranking_data: Dict[str, Any]) -> Dict[str, Any]:
        """Successful default boards feed refresh even when statistics are not requested."""
        if not getattr(cfg, "JJC_KUNGFU_REFRESH_ENABLED", 0):
            return ranking_data
        try:
            resolved = await self.resolve_ranking_kungfus(ranking_data)
            degraded = resolved["refresh_degraded"]
        except Exception as exc:
            degraded = True
            logger.warning("排行榜心法预热登记失败，保留已获取榜单: {}", exc)
        # Metadata belongs to this request, never mutate the shared ranking cache.
        return {**ranking_data, "refresh_degraded": degraded}

    async def resolve_ranking_kungfus(
        self, ranking_data: Dict[str, Any], *, policy: Optional[KungfuPolicy] = None,
    ) -> Dict[str, Any]:
        """Read cached values; register usable results for future refresh only."""
        policy = policy if policy is not None else self._ranking_kungfu_policy()
        scope = dict(policy.scope)
        cutoff = time.time()
        players = ranking_data.get("data") or []
        repo = self.identity_repo if self.identity_repo is not None else RoleIdentityRepo()
        identities = await repo.resolve_ranking_identities_batch(self._ranking_identity_inputs(players))
        if len(identities) != len(players):
            raise ValueError("ranking identity batch must preserve every position")
        valid_names = set(self.kungfu_healer_list + self.kungfu_dps_list)
        canonical_ids = {item["name"]: item["id"] for item in get_jjc_kungfu_catalog()}
        members = []
        for player, identity in zip(players, identities):
            person = player.get("personInfo") or {}
            server = person.get("server", "未知")
            name = person.get("roleName", "未知")
            if name and str(name).endswith("·" + str(server)):
                name = name.rsplit("·", 1)[0]
            result = copy.deepcopy(identity.get("jjc_kungfu")) if identity else None
            status = policy.evaluate(result, cutoff, valid_names=valid_names)
            if status["freshness"] != "unknown" and canonical_ids.get(result["name"]) != result["kungfu_id"]:
                status = {"freshness": "unknown", "reason": "unknown_kungfu"}
            found = status["freshness"] != "unknown"
            member = {
                "server": server, "name": name, "found": found,
                "kungfu": result["name"] if found else None,
                "kungfu_id": result["kungfu_id"] if found else None,
                "game_role_id": person.get("gameRoleId"),
                "role_id": (identity or {}).get("role_id") or person.get("gameRoleId"),
                "zone": person.get("zone"),
                "global_role_id": (identity or {}).get("global_role_id") or person.get("globalRoleId"),
                "global_id": (identity or {}).get("global_id"),
                "kungfu_freshness": status["freshness"],
                "kungfu_unknown_reason": status["reason"] if identity else "identity_unresolved",
                "kungfu_verified_at": result.get("verified_at") if isinstance(result, dict) else None,
                "kungfu_source": result.get("source") if isinstance(result, dict) else None,
                "kungfu_publish_version": result.get("publish_version") if isinstance(result, dict) else None,
                "kungfu_selected_source": result.get("source") if found else None,
                "weapon_checked": False, "teammates_checked": False,
                "kungfu_acquisition": "cache",
            }
            if identity is not None:
                # Freeze before registration yields: a later refresh must not
                # redefine this snapshot's identity or publication baseline.
                context = copy.deepcopy(display_context(identity, scope))
                member.update({"display_" + key: value for key, value in context.items()})
            members.append(member)
        degraded = bool(ranking_data.get("refresh_degraded"))
        if (getattr(cfg, "JJC_KUNGFU_REFRESH_ENABLED", 0)
                or getattr(cfg, "JJC_RANKING_KUNGFU_CACHE_ENABLED", 0)):
            try:
                queue = self.kungfu_refresh_repo if self.kungfu_refresh_repo is not None else JjcKungfuRefreshRepo()
                await queue.register([item for item, member in zip(identities, members)
                                      if item is not None and (member["found"] or not
                                          getattr(cfg, "JJC_RANKING_KUNGFU_CACHE_ENABLED", 0))],
                                     dict(scope), cutoff,
                                     track_seconds=policy.track_seconds)
            except Exception as exc:
                degraded = True
                logger.warning("排行榜心法刷新登记失败，保留已读取结果: {}", exc)
        return {"members": members, "identities": identities, "refresh_degraded": degraded,
                "kungfu_cache_scope": scope}

    async def _query_missing_ranking_kungfu(
        self, member: Dict[str, Any], identity: Optional[Dict[str, Any]],
        ranking_data: Dict[str, Any], rank: int, policy: KungfuPolicy,
    ) -> Dict[str, Any]:
        """A cache miss is resolved in this statistics call, never deferred."""
        try:
            if identity is not None and self.kungfu_query_service is not None:
                saved = await self.kungfu_query_service.query_now(identity)
                status = policy.evaluate(saved, time.time(),
                    valid_names=set(self.kungfu_healer_list + self.kungfu_dps_list))
                canonical = {item["name"]: item["id"] for item in get_jjc_kungfu_catalog()}
                if (status["freshness"] == "unknown" or
                        canonical.get(saved["name"]) != saved["kungfu_id"]):
                    return member
                packet = copy.deepcopy(saved.get("display_packet"))
                attempt = copy.deepcopy(saved.get("display_attempt"))
                return {**member, "found": True, "kungfu": saved["name"],
                        "kungfu_id": saved["kungfu_id"],
                        "kungfu_freshness": status["freshness"], "kungfu_unknown_reason": None,
                        "kungfu_verified_at": saved["verified_at"], "kungfu_source": saved["source"],
                        "kungfu_selected_source": saved["source"],
                        "kungfu_publish_version": saved["publish_version"],
                        "kungfu_acquisition": "live", "display_packet": packet,
                        "display_attempt": attempt,
                        "self_kungfu_id": packet.get("self_kungfu_id") if packet else None,
                        "weapon_checked": bool(packet and packet.get("weapon_ready")),
                        "teammates_checked": bool(packet and packet.get("team_ready")),
                        "weapon_name": packet.get("weapon_name") if packet else None,
                        "weapon_icon": packet.get("weapon_icon") if packet else None,
                        "weapon_quality": packet.get("weapon_quality") if packet else None,
                        "teammates": copy.deepcopy(packet.get("teammates", [])) if packet else []}
            # No safe persistent identity: query using the ranking locator, but
            # never publish the answer onto a conflicting same-name identity.
            queried = await self.get_user_kungfu(member["server"], member["name"],
                ranking_data=ranking_data, rank=rank, prefer_cache=False)
            canonical = {item["name"]: item["id"] for item in get_jjc_kungfu_catalog()}
            name = queried.get("kungfu")
            if (not queried.get("found") or name not in canonical or
                    queried.get("kungfu_selected_source") == "cached_match_detail_win_history"):
                return member
            return {**member, **queried, "kungfu_id": canonical[name],
                    "kungfu_freshness": "fresh", "kungfu_unknown_reason": None,
                    "kungfu_verified_at": time.time(), "kungfu_source": "direct_legacy",
                    "kungfu_publish_version": None, "kungfu_acquisition": "live"}
        except Exception as exc:
            logger.warning("排行榜缺失心法直接查询失败: rank={} exception_type={}", rank, type(exc).__name__)
            return member

    def _api(self) -> JjcApiClient:
        return JjcApiClient(
            arena_time_tag_url=self.arena_time_tag_url,
            arena_ranking_url=self.arena_ranking_url,
            tuilan_request=self.tuilan_request,
        )

    async def _foreground_sync_request(self, func, *args):
        context = self.query_context
        context.check()
        def invoke():
            # Executor queueing must not start new HTTP after the caller left.
            context.check()
            return func(*args)
        owner = asyncio.create_task(asyncio.to_thread(invoke))
        owners = context.recovery_physical_owners
        owners.add(owner)
        def finished(task):
            owners.discard(task)
            if not task.cancelled():
                task.exception()
        owner.add_done_callback(finished)
        return await asyncio.shield(owner)

    async def _foreground_request(self, endpoint, params):
        context = self.query_context
        context.check()
        def retain(owner):
            context.recovery_physical_owners.add(owner)
            owner.add_done_callback(context.recovery_physical_owners.discard)
        tracked_request = getattr(self.foreground_client, 'request_with_owner', None)
        if callable(tracked_request):
            return await tracked_request(endpoint, params, deadline=context.deadline, on_owner=retain)
        return await self.foreground_client.request(endpoint, params, deadline=context.deadline)

    def _cache(self) -> JjcCacheRepo:
        try:
            repo = JjcCacheRepo(
                jjc_ranking_cache_duration=self.jjc_ranking_cache_duration,
                kungfu_cache_duration=self.kungfu_cache_duration,
                db=get_db(),
            )
        except Exception:
            if self.query_context is not None:
                raise ForegroundMongoFailure("indicator_storage_unavailable") from None
            raise
        if self.query_context is not None and self.query_context.mongo is not None:
            return self.query_context.mongo.bind_repo(repo)
        return repo

    def _inspect_cache(self) -> JjcInspectRepo:
        return self.inspect_repo or JjcInspectRepo()

    def _translate_detail_kungfu_names(self, detail: dict[str, Any]) -> None:
        for team_key in ("team1", "team2"):
            team = detail.get(team_key)
            if not isinstance(team, dict):
                continue
            players = team.get("players_info") or []
            if not isinstance(players, list):
                continue
            for player in players:
                if not isinstance(player, dict):
                    continue
                kungfu = player.get("kungfu")
                if isinstance(kungfu, str) and kungfu:
                    player["kungfu"] = self.kungfu_pinyin_to_chinese.get(kungfu, kungfu)

    async def _enrich_warmup_match_detail_with_replay(self, match_id: int, payload: Dict[str, Any]) -> None:
        if not self.match_replay_url:
            return
        detail = payload.get("detail")
        if not isinstance(detail, dict):
            return
        try:
            replay = await asyncio.to_thread(
                self.tuilan_request,
                self.match_replay_url,
                {"match_id": match_id},
            )
        except Exception as exc:
            logger.warning("定时统计预热 match_detail replay 请求失败: match_id={} error={}", match_id, exc)
            return
        if not isinstance(replay, dict) or replay.get("error"):
            logger.warning("定时统计预热 match_detail replay 响应无效: match_id={}", match_id)
            return
        payload["replay"] = replay
        merge_replay_global_ids_into_match_detail({"data": detail}, replay)

    async def _warmup_inspect_cache_from_kungfu_detail(
        self,
        *,
        server: str,
        name: str,
        kungfu_detail: Optional[dict[str, Any]],
    ) -> None:
        if not isinstance(kungfu_detail, dict):
            return
        cache_warmup = kungfu_detail.get("_cache_warmup")
        if not isinstance(cache_warmup, dict):
            return

        repo = self._inspect_cache()
        cached_at = time.time()

        role_indicator = cache_warmup.get("role_indicator")
        if isinstance(role_indicator, dict):
            raw = role_indicator.get("raw")
            if isinstance(raw, dict):
                parsed = parse_3v3_indicator(raw)
                if parsed.get("error") or not indicator_matches_request(
                    role_indicator, raw, role_id=str(role_indicator.get("game_role_id") or role_indicator.get("role_id") or ""),
                    zone=str(role_indicator.get("zone") or ""), server=server,
                ):
                    logger.warning(
                        "定时统计预热 role_indicator 缓存失败: server={} name={} reason={}",
                        server,
                        name,
                        parsed.get("error"),
                    )
                else:
                    role_info = (raw.get("data") or {}).get("role_info") or {}
                    global_role_id = (
                        role_indicator.get("global_role_id")
                        or role_info.get("global_role_id")
                        or role_info.get("globalRoleId")
                    )
                    global_id = role_indicator.get("global_id")
                    identity_key = (
                        f"global_id:{global_id}"
                        if global_id
                        else (f"global:{global_role_id}" if global_role_id else None)
                    )
                    if getattr(cfg, "JJC_ROLE_IDENTITY_RECOVERY_ENABLED", 0) and global_id:
                        if not await self._recover_ranking_query_ids(
                            server=server, name=name, role_indicator=role_indicator,
                            cache_warmup=cache_warmup,
                        ):
                            identity_key = None
                    if identity_key:
                        await repo.save_role_indicator(
                            identity_key,
                            {
                                "identity_key": identity_key,
                                "server": server,
                                "name": name,
                                "game_role_id": role_indicator.get("game_role_id"),
                                "global_role_id": global_role_id,
                                "global_id": global_id,
                                "zone": role_indicator.get("zone"),
                                "indicator": parsed,
                                "raw": raw,
                                "cached_at": cached_at,
                            },
                        )

                    else:
                        logger.warning(
                            "定时统计预热 role_indicator 缓存失败: server={} name={} reason=global_role_id_missing",
                            server,
                            name,
                        )

        match_detail = cache_warmup.get("match_detail")
        if isinstance(match_detail, dict):
            match_id = match_detail.get("match_id")
            raw = match_detail.get("raw")
            if isinstance(raw, dict) and match_id is not None:
                try:
                    normalized_match_id = int(match_id)
                except (TypeError, ValueError):
                    normalized_match_id = None
                if normalized_match_id is not None:
                    payload = None
                    if raw.get("code") == -1 and str(raw.get("msg") or "").strip() == "no data found" and raw.get("data") is None:
                        payload = {
                            "match_id": normalized_match_id,
                            "unavailable": True,
                            "code": -1,
                            "message": "no data found",
                            "detail": None,
                        }
                    elif raw.get("code") == 0 and isinstance(raw.get("data"), dict):
                        detail = copy.deepcopy(raw.get("data") or {})
                        self._translate_detail_kungfu_names(detail)
                        payload = {
                            "match_id": normalized_match_id,
                            "detail": detail,
                        }
                    if payload is not None:
                        await self._enrich_warmup_match_detail_with_replay(normalized_match_id, payload)
                        persistence_result = await self._persist_match_detail(
                            repo=repo,
                            match_id=normalized_match_id,
                            payload={"cached_at": cached_at, "data": payload},
                            source="ranking_warmup",
                        )
                        if getattr(persistence_result, "main_saved", True) is not True:
                            logger.warning(
                                "定时统计预热 match_detail 主保存未成功: match_id={}",
                                normalized_match_id,
                            )

        role_recent = cache_warmup.get("role_recent")
        if isinstance(role_recent, dict):
            raw_matches = role_recent.get("raw_matches") or []
            if raw_matches:
                from src.services.jx3.jjc_ranking_inspect import normalize_recent_matches, recent_matches_pagination

                indicator_data = role_indicator if isinstance(role_indicator, dict) else {}
                game_role_id = indicator_data.get("game_role_id")
                role_id_val = indicator_data.get("role_id")
                zone_val = indicator_data.get("zone")
                global_role_id_val = indicator_data.get("global_role_id")
                global_id_val = indicator_data.get("global_id")

                identity_key = (
                    f"global_id:{global_id_val}"
                    if global_id_val
                    else (f"global:{global_role_id_val}" if global_role_id_val else None)
                )

                identity_hints: dict[str, Any] = {}
                for key, value in [
                    ("game_role_id", game_role_id),
                    ("role_id", role_id_val),
                    ("zone", zone_val),
                    ("global_role_id", global_role_id_val),
                    ("global_id", global_id_val),
                ]:
                    if value:
                        identity_hints[key] = value

                identity = {
                    "server": server,
                    "name": name,
                    "game_role_id": game_role_id,
                    "role_id": role_id_val,
                    "zone": zone_val,
                    "global_role_id": global_role_id_val,
                    "global_id": global_id_val,
                    "source": "ranking_warmup",
                    "identity_hints": identity_hints,
                }

                max_recent = 20
                season_start_time = self._coerce_season_start_time()
                first_page = raw_matches[:max_recent]
                recent_matches = normalize_recent_matches(
                    first_page,
                    kungfu_pinyin_to_chinese=self.kungfu_pinyin_to_chinese,
                    max_recent_matches=max_recent,
                    season_start_time=season_start_time,
                )

                payload: dict[str, Any] = {
                    "current_season": self.current_season,
                    "season_start_time": season_start_time,
                    "player": {"server": server, "name": name},
                    "identity": identity,
                    "identity_key": identity_key,
                    "pagination": recent_matches_pagination(
                        first_page, cursor=0, page_size=max_recent,
                        season_start_time=season_start_time,
                    ),
                    "recent_matches": recent_matches,
                }

                try:
                    await repo.save_role_recent(server, name, {"cached_at": cached_at, "data": payload})
                except Exception as exc:
                    logger.warning(
                        "定时统计预热 role_recent 缓存失败: server={} name={} error={}",
                        server,
                        name,
                        exc,
                    )

    async def _recover_ranking_query_ids(self, *, server: str, name: str,
                                        role_indicator: Dict[str, Any],
                                        cache_warmup: Dict[str, Any]) -> bool:
        cache = self._cache()
        identity_repo = cache._get_identity_repo()
        existing = await identity_repo.resolve_best_identity_with_id(
            server=server, name=name, global_id=str(role_indicator.get("global_id")),
        )
        if not existing or str(existing.get("global_id")) != str(role_indicator.get("global_id")):
            return False
        raw = role_indicator.get("raw") or {}
        role_id = str(role_indicator.get("game_role_id") or role_indicator.get("role_id") or "")
        if role_id == str(existing.get("game_role_id") or existing.get("role_id") or ""):
            return indicator_matches_request(existing, raw, role_id=role_id,
                                             zone=str(existing.get("zone") or ""), server=server)
        if not self.match_replay_url:
            return False
        detail = cache_warmup.get("match_detail") or {}
        locator = dict(role_indicator, role_id=role_id, server=server,
                       match_id=detail.get("match_id"))
        candidates = [locator] + await cache.load_query_id_recovery_candidates(existing)

        def indicator_fetcher(**query: Any) -> Any:
            return get_role_indicator(query["role_id"], query["zone"], query["server"],
                                      tuilan_request=self.tuilan_request, name=name)

        recovery = await recover_query_ids(
            existing, identity_repo=identity_repo, candidates=candidates,
            indicator_fetcher=indicator_fetcher,
            replay_fetcher=lambda **query: self.tuilan_request(self.match_replay_url, query),
            history_fetcher=lambda **query: self.tuilan_request(cfg.API_URLS["竞技场战局历史"], query),
            current_indicator=raw,
            current_indicator_request=dict(locator, observed_at=role_indicator.get("request_started_at")),
            max_logical_calls=4, publish=True,
        )
        # A healthy original ID does not authorize caching the held alternative.
        return recovery.get("status") == "recovered"

    async def _persist_match_detail(
        self,
        *,
        repo: JjcInspectRepo,
        match_id: int,
        payload: Dict[str, Any],
        source: str,
    ) -> Any:
        service = self.match_detail_persistence_service
        if service is None:
            logger.warning("JJC match persistence service unavailable; fact projection skipped: match_id={} source={}", match_id, source)
            service = MatchDetailPersistenceService(
                inspect_repo=repo,
                identity_projector=self.match_detail_projection_service,
                participant_projector=self.match_detail_participant_projection_service,
                fact_projector=None,
            )
        return await service.persist(match_id, payload, source)

    async def _get_cached_match_detail_win_kungfu(
        self,
        *,
        server: str,
        name: str,
        role_id: Optional[Any] = None,
    ) -> Optional[dict[str, Any]]:
        return await self._cache().get_kungfu_from_cached_match_detail_win_history(
            server=server,
            name=name,
            season_start=self.current_season_start,
            # Ranking gameRoleId matches replay players[].role_id; replay numeric global_id is optional.
            role_id=role_id,
        )

    @staticmethod
    def _copy_cached_match_detail_win_kungfu_fields(
        result: dict[str, Any],
        history_win_result: dict[str, Any],
    ) -> None:
        for key in CACHED_MATCH_DETAIL_WIN_KUNGFU_FIELDS:
            if key in history_win_result:
                result[key] = history_win_result[key]

    async def _save_cached_match_detail_win_kungfu_result(
        self,
        *,
        server: str,
        name: str,
        history_win_result: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "server": server,
            "name": name,
            "kungfu": history_win_result.get("kungfu"),
            "found": True,
            "cache_time": time.time(),
        }
        self._copy_cached_match_detail_win_kungfu_fields(result, history_win_result)
        result.setdefault("weapon_checked", True)
        await self._cache().save_kungfu_cache(server, name, result)
        return result

    @staticmethod
    def _coerce_score(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                return int(float(value))
            except ValueError:
                return None
        return None

    @classmethod
    def _extract_score(cls, player: dict[str, Any], person_info: dict[str, Any]) -> Optional[int]:
        candidates = [
            person_info.get("score"),
            player.get("score"),
            person_info.get("rating"),
            player.get("rating"),
            person_info.get("pvpScore"),
            player.get("pvpScore"),
            person_info.get("totalScore"),
            player.get("totalScore"),
            person_info.get("avgGrade"),
            player.get("avgGrade"),
            person_info.get("grade"),
            player.get("grade"),
        ]
        for item in candidates:
            score = cls._coerce_score(item)
            if score is not None:
                return score
        return None

    @staticmethod
    def _build_invalid_kungfu_reason(player_item: dict[str, Any]) -> str:
        reasons: list[str] = []
        if not player_item.get("found"):
            reasons.append("found_false")
        if not player_item.get("kungfu"):
            reasons.append("kungfu_empty")
        if not player_item.get("kungfu_selected_source"):
            reasons.append("source_missing")
        if not player_item.get("weapon_checked"):
            reasons.append("weapon_not_checked")
        if not player_item.get("teammates_checked"):
            reasons.append("teammates_not_checked")
        teammates = player_item.get("teammates")
        if not isinstance(teammates, list) or not teammates:
            reasons.append("teammates_empty")
        else:
            missing_count = sum(
                1
                for item in teammates
                if not isinstance(item, dict) or item.get("kungfu_id") in (None, "")
            )
            if missing_count:
                reasons.append("teammates_kungfu_id_missing:%s" % missing_count)
        if not player_item.get("match_history_checked"):
            reasons.append("match_history_not_checked")
        return ",".join(reasons) if reasons else "unknown"

    async def query_jjc_ranking(self, tag: Optional[int] = None) -> dict[str, Any]:
        """Read a board and preserve normal default-board refresh registration."""
        result = await self.query_jjc_ranking_raw(tag=tag)
        if tag is None and not result.get("error") and result.get("code") == 0:
            return await self._prewarm_ranking_kungfus(result)
        return result

    @observed_query_stage("ranking")
    async def query_jjc_ranking_raw(self, tag: Optional[int] = None) -> dict[str, Any]:
        """Read only the board, without resolving or queueing its role identities.

        Caller results, upstream responses and the saved raw board own separate
        copies so request-specific metadata or edits cannot leak between them.
        """
        logger.info("开始查询竞技场排行榜数据")

        explicit_tag = tag is not None
        if explicit_tag and (isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0):
            return {"error": True, "message": "tag 必须是正整数"}

        if not explicit_tag:
            cached = await self._cache().load_ranking_cache()
            if cached:
                return copy.deepcopy(cached)

        try:
            if explicit_tag:
                default_week = tag
                logger.info("使用指定竞技场时间标签: tag={}", tag)
            else:
                logger.info("获取竞技场时间标签")
                if self.query_context is not None and self.foreground_client is not None:
                    self.query_context.check()
                    time_tag_result = await self._foreground_request('arena_time_tag', {'type': 'role'})
                elif self.query_context is not None:
                    time_tag_result = await self._foreground_sync_request(self._api().get_arena_time_tag)
                else:
                    time_tag_result = await asyncio.to_thread(self._api().get_arena_time_tag)

                if time_tag_result.get("error"):
                    logger.warning(f"获取竞技场时间标签失败: {time_tag_result}")
                    return {
                        "error": True,
                        "message": f"获取时间标签失败: {time_tag_result.get('error', '未知错误')}",
                    }

                if time_tag_result.get("code") != 0:
                    logger.warning(f"获取竞技场时间标签失败: {time_tag_result}")
                    return {
                        "error": True,
                        "message": f"获取时间标签失败: {time_tag_result.get('msg', '未知错误')}",
                    }

                if self.query_context is not None:
                    self.query_context.check()
                    if self.query_context.remaining() <= 5.45:
                        raise ForegroundQueryTimeout()
                await fixed_sleep(5.45)
                if self.query_context is not None:
                    self.query_context.check()

                data = time_tag_result.get("data", {})
                default_week = int(data.get("defaultWeek") or 0)
                if default_week <= 0:
                    return {
                        "error": True,
                        "message": f"获取时间标签失败: 缺少 defaultWeek 参数: {data}",
                    }

                tag = default_week

                logger.info(f"获取竞技场时间标签成功: defaultWeek={default_week} tag={tag}")
            logger.info("获取竞技场排行榜")
            if self.query_context is not None and self.foreground_client is not None:
                self.query_context.check()
                ranking_result = await self._foreground_request('arena_ranking',
                    {'typeName': 'week', 'heiMaBang': False, 'tag': tag})
            elif self.query_context is not None:
                ranking_result = await self._foreground_sync_request(self._api().get_arena_ranking, tag)
            else:
                ranking_result = await asyncio.to_thread(self._api().get_arena_ranking, tag)

            if ranking_result.get("error"):
                return {"error": True, "message": f"获取竞技场排行榜失败: {ranking_result.get('error')}"}

            if ranking_result.get("code") != 0:
                return {
                    "error": True,
                    "message": f"获取竞技场排行榜失败: {ranking_result.get('msg', '未知错误')}",
                }

            ranking_result = copy.deepcopy(ranking_result)
            if explicit_tag:
                ranking_result["explicit_tag"] = True
            ranking_result["defaultWeek"] = default_week
            ranking_result["cache_time"] = time.time()

            if not explicit_tag:
                await self._cache().save_ranking_cache(copy.deepcopy(ranking_result))

            return ranking_result
        except (ForegroundQueryError, ForegroundMongoFailure, asyncio.TimeoutError):
            raise
        except Exception as exc:
            logger.exception(f"查询竞技场排行榜失败: {exc}")
            return {"error": True, "message": f"查询竞技场排行榜失败: {exc}"}

    async def update_kungfu_cache(self, server: str, name: str, jjc_data: dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"优先使用心法查询接口更新心法信息: server={server} name={name}")

        kungfu_info = None
        kungfu_detail: Optional[dict[str, Any]] = None
        captured_game_role_id = None
        captured_zone = None
        try:
            ranking_result = await self.query_jjc_ranking()
            if ranking_result and not ranking_result.get("error") and ranking_result.get("code") == 0:
                ranking_data = ranking_result.get("data", [])
                for player in ranking_data:
                    person_info = player.get("personInfo", {})
                    player_server = person_info.get("server")
                    player_name = person_info.get("roleName")

                    if player_name and "·" in player_name:
                        parts = player_name.rsplit("·", 1)
                        if len(parts) == 2 and parts[1] == str(player_server):
                            player_name = parts[0]

                    if player_server == server and player_name == name:
                        game_role_id = person_info.get("gameRoleId")
                        zone = person_info.get("zone")
                        captured_game_role_id = game_role_id
                        captured_zone = zone

                        if game_role_id and zone:
                            logger.info(
                                f"在排行榜中找到角色: server={server} name={name} role_id={game_role_id} zone={zone}"
                            )
                            kungfu_detail = await asyncio.to_thread(
                                get_kungfu_detail_by_role_info,
                                game_role_id,
                                zone,
                                server,
                                tuilan_request=self.tuilan_request,
                                kungfu_pinyin_to_chinese=self.kungfu_pinyin_to_chinese,
                                match_detail_url=self.match_detail_url,
                                match_replay_url=self.match_replay_url,
                                role_name=name,
                                rank=None,
                            )
                            kungfu_name = (kungfu_detail or {}).get("kungfu")
                            if kungfu_name:
                                logger.info(f"心法查询成功: server={server} name={name} kungfu={kungfu_name}")
                                kungfu_info = kungfu_name
                                await self._warmup_inspect_cache_from_kungfu_detail(
                                    server=server,
                                    name=name,
                                    kungfu_detail=kungfu_detail,
                                )
                                break
                            logger.info("心法查询失败: 未找到心法信息")
                            await self._warmup_inspect_cache_from_kungfu_detail(
                                server=server,
                                name=name,
                                kungfu_detail=kungfu_detail,
                            )
                            break

                if not kungfu_info:
                    logger.info(f"在排行榜中未找到匹配的角色: server={server} name={name}")
            else:
                logger.warning("获取排行榜数据失败，无法进行心法查询")
        except Exception as exc:
            logger.warning(f"心法查询过程中出错: {exc}")

        if not kungfu_info:
            logger.info("心法查询失败，从竞技场数据中提取心法信息作为备选方案")
            history_data = jjc_data.get("data", {}).get("history", [])
            if history_data:
                for match in history_data:
                    if match.get("won"):
                        kungfu_info = match.get("kungfu")
                        break

        result: dict[str, Any] = {
            "server": server,
            "name": name,
            "kungfu": kungfu_info,
            "found": kungfu_info is not None,
            "cache_time": time.time(),
        }
        if kungfu_detail:
            result.update(kungfu_detail)
        if captured_game_role_id and "game_role_id" not in result:
            result["game_role_id"] = captured_game_role_id
        if captured_zone and "zone" not in result:
            result["zone"] = captured_zone
        result.setdefault("weapon_checked", True)

        await self._cache().save_kungfu_cache(server, name, result)
        return result

    def save_ranking_stats(
        self,
        ranking_result: dict[str, Any],
        stats: dict[str, Any],
        week_info: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Optional[asyncio.Task]:
        equipment_view = (payload or {}).get("equipment_view")
        ranking_timestamp = int(equipment_view["timestamp"] if equipment_view else ranking_result.get("cache_time") or time.time())
        try:
            stats_payload = {
                "generated_at": time.time(),
                "ranking_cache_time": ranking_result.get("cache_time"),
                "default_week": ranking_result.get("defaultWeek"),
                "current_season": self.current_season,
                "week_info": week_info,
                "kungfu_statistics": stats,
                "ranking_display_kind": "historical" if ranking_result.get("explicit_tag") is True else "current",
            }
            for key in ("kungfu_cache_scope", "refresh_degraded"):
                if payload is not None and key in payload:
                    stats_payload[key] = copy.deepcopy(payload[key])
            if equipment_view is not None:
                if ranking_result.get("explicit_tag") is True:
                    raise EquipmentSnapshotError("historical")
                stats_payload.update(schema_version=2, equipment_source="local_win_v2", equipment_complete=True,
                    equipment_view=copy.deepcopy(equipment_view), member_revision=equipment_view["member_revision"],
                    equipment_view_revision=equipment_view["equipment_view_revision"],
                    kungfu_cache_scope=copy.deepcopy(equipment_view["scope"]))
            summary_payload = self._build_summary_payload(stats_payload)
            detail_payloads = self._build_detail_payloads(stats)
            mongo_task = self._save_ranking_stats_to_mongo(
                timestamp=ranking_timestamp,
                summary_payload=summary_payload,
                detail_payloads=detail_payloads,
                sync_request=(payload or {}).get("_sync_request"),
                equipment_expected=(payload or {}).get("_equipment_expected"),
            )

            logger.info("保存竞技场统计到 Mongo: timestamp={} details={}", ranking_timestamp, len(detail_payloads))
            return mongo_task
        except Exception as exc:
            logger.warning("保存竞技场统计结果失败: {}", exc)
            if equipment_view is not None:
                raise
            return None

    def _build_summary_payload(self, stats_payload: dict[str, Any]) -> dict[str, Any]:
        summary_payload = {
            key: value
            for key, value in stats_payload.items()
            if key != "kungfu_statistics"
        }
        summary_payload["kungfu_statistics"] = {}

        kungfu_statistics = stats_payload.get("kungfu_statistics") or {}
        for range_key, range_stats in kungfu_statistics.items():
            if not isinstance(range_stats, dict):
                summary_payload["kungfu_statistics"][range_key] = range_stats
                continue

            summary_range: dict[str, Any] = {
                key: value
                for key, value in range_stats.items()
                if key not in {"healer", "dps", "unknown_members"}
            }
            for lane_name in ("healer", "dps"):
                lane = range_stats.get(lane_name) or {}
                if not isinstance(lane, dict):
                    summary_range[lane_name] = lane
                    continue

                if range_stats.get("equipment_source") == "local_win_v2":
                    summary_range[lane_name] = {key: copy.deepcopy(value) for key, value in lane.items() if key != "members"}
                    continue
                members_map = lane.get("members") or {}
                legendary_count_map: dict[str, int] = {}
                for kungfu, members in members_map.items():
                    legendary_count_map[kungfu] = sum(
                        1
                        for member in (members or [])
                        if is_jjc_legendary_weapon(
                            (member or {}).get("weapon_quality"),
                            extract_member_weapon_name(member),
                        )
                    )

                summary_lane = {
                    key: value
                    for key, value in lane.items()
                    if key != "members"
                }
                summary_lane["legendary_count_map"] = legendary_count_map
                summary_range[lane_name] = summary_lane

            summary_payload["kungfu_statistics"][range_key] = summary_range

        return summary_payload

    def _build_detail_payloads(self, stats: Dict[str, Any]) -> List[Dict[str, Any]]:
        detail_payloads: List[Dict[str, Any]] = []
        for range_key, range_stats in (stats or {}).items():
            if not isinstance(range_stats, dict):
                continue
            # Same-anchor recomputes (including rollback) must clear old unknown rows.
            detail_payloads.append({"range": range_key, "lane": "unknown", "kungfu": "unknown",
                                    "members": copy.deepcopy(range_stats.get("unknown_members") or [])})
            for lane_name in ("healer", "dps"):
                lane = range_stats.get(lane_name) or {}
                members_map = lane.get("members") or {}
                if not isinstance(members_map, dict):
                    continue
                for kungfu, members in members_map.items():
                    detail_payloads.append({
                        "range": range_key,
                        "lane": lane_name,
                        "kungfu": kungfu,
                        "members": members or [],
                    })
        return detail_payloads

    @staticmethod
    def _new_sync_request() -> Dict[str, Any]:
        order = "{:020d}:{}".format(time.time_ns(), uuid4().hex)
        return {"order": order, "batch_id": "ranking_stats:" + order,
                "until": int(time.time()) - 14 * 86400}

    def _save_ranking_stats_to_mongo(
        self,
        *,
        timestamp: int,
        summary_payload: Dict[str, Any],
        detail_payloads: List[Dict[str, Any]],
        sync_request: Optional[Dict[str, Any]] = None,
        equipment_expected: Optional[Dict[str, Any]] = None,
    ) -> Optional[asyncio.Task]:
        sync_request = sync_request or self._new_sync_request()
        async def _save() -> None:
            try:
                repo = JjcRankingStatsRepo()
                async with _equipment_publication_lock():
                    if summary_payload.get("equipment_source") == "local_win_v2":
                        latest = await repo.get_latest_current_summary(summary_payload["kungfu_cache_scope"])
                        if latest and latest.get("timestamp", 0) > timestamp:
                            raise EquipmentSnapshotError("historical")
                        expected = equipment_expected or {}
                        await repo.publish_equipment_snapshot(timestamp, summary_payload,
                            expected_revision=expected.get("expected_revision"), expected_legacy=expected.get("expected_legacy"))
                    else:
                        await repo.save_snapshot(timestamp=timestamp, summary_payload=summary_payload,
                            detail_payloads=detail_payloads, source="ranking_job")
                await self._enqueue_ranking_members_for_sync(
                    detail_payloads=detail_payloads,
                    timestamp=timestamp,
                    sync_request=sync_request,
                )
            except Exception as exc:
                logger.warning("保存竞技场统计到 Mongo 失败: timestamp={} error={}", timestamp, exc)
                if summary_payload.get("equipment_source") == "local_win_v2" or isinstance(exc, EquipmentSnapshotError):
                    raise

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            logger.warning("保存竞技场统计到 Mongo 失败: 无可用事件循环 timestamp={} error={}", timestamp, exc)
            return None
        return loop.create_task(_save())

    async def _enqueue_ranking_members_for_sync(
        self,
        *,
        detail_payloads: List[Dict[str, Any]],
        timestamp: int,
        sync_request: Optional[Dict[str, Any]] = None,
    ) -> None:
        repo = JjcSyncRepo()
        seen: set[str] = set()
        enqueued_count = 0
        sync_request = sync_request or self._new_sync_request()
        batch_id = sync_request["batch_id"]
        queue_sync_until_time = sync_request["until"]
        for detail in detail_payloads:
            for member in detail.get("members") or []:
                if not isinstance(member, dict):
                    continue
                dedupe_key = "{}|{}|{}|{}|{}".format(
                    member.get("global_id") or "",
                    member.get("global_role_id") or "",
                    member.get("zone") or "",
                    member.get("role_id") or member.get("game_role_id") or "",
                    "{}:{}".format(member.get("server") or "", member.get("name") or member.get("role_name") or ""),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                queued = await self._enqueue_ranking_member_for_sync(
                    member=member,
                    repo=repo,
                    batch_id=batch_id,
                    queue_sync_until_time=queue_sync_until_time,
                    request_order=sync_request["order"],
                )
                if queued:
                    enqueued_count += 1
        logger.info(
            "竞技排名统计成员加入同步队列完成: timestamp={} members={} enqueued={}",
            timestamp,
            len(seen),
            enqueued_count,
        )

    async def _enqueue_ranking_member_for_sync(
        self,
        *,
        member: Dict[str, Any],
        repo: JjcSyncRepo,
        batch_id: str,
        queue_sync_until_time: int,
        request_order: Optional[str] = None,
    ) -> bool:
        try:
            queued = await repo.enqueue_ranking_member(
                member,
                season_id=str(self.current_season) if self.current_season is not None else None,
                season_start_time=self._coerce_season_start_time(),
                priority=1,
                mode="full",
                source="ranking_stats",
                batch_id=batch_id,
                queue_sync_until_time=queue_sync_until_time,
                request_order=request_order,
                rank=int(member.get("rank") or 0),
            )
            return queued is not None
        except Exception as exc:
            logger.warning(
                "竞技排名统计成员加入同步队列失败: server={} name={} error={}",
                member.get("server"),
                member.get("name") or member.get("role_name"),
                exc,
            )
            return False

    def _should_enqueue_ranking_member_immediately(
        self,
        *,
        member: Dict[str, Any],
        rank: int,
        total_players: int,
    ) -> bool:
        if rank <= 0:
            return False
        tracked_rank_limit = 1000 if total_players >= 1000 else 200
        if rank > tracked_rank_limit:
            return False
        return True

    def _coerce_season_start_time(self) -> int:
        try:
            return int(datetime.strptime(str(self.current_season_start), "%Y-%m-%d").replace(
                tzinfo=timezone(timedelta(hours=8)),
            ).timestamp())
        except (TypeError, ValueError):
            return 0

    async def get_user_kungfu(
        self,
        server: str,
        name: str,
        ranking_data: Optional[dict[str, Any]] = None,
        rank: Optional[int] = None,
        prefer_cache: bool = False,
    ) -> dict[str, Any]:
        if prefer_cache:
            cached_result = await self._load_cached_kungfu_for_ranking(server, name)
            if cached_result is not None:
                return cached_result

        await random_sleep(1, 3)

        logger.info(f"优先使用心法查询接口查询心法信息: server={server} name={name}")

        captured_game_role_id: Optional[Any] = None

        try:
            ranking_result = ranking_data
            if not ranking_result:
                ranking_result = await self.query_jjc_ranking()
            if ranking_result and not ranking_result.get("error") and ranking_result.get("code") == 0:
                ranking_list = ranking_result.get("data", [])

                for player in ranking_list:
                    person_info = player.get("personInfo", {})
                    player_server = person_info.get("server")
                    player_name = person_info.get("roleName")

                    if player_name and "·" in player_name:
                        parts = player_name.rsplit("·", 1)
                        if len(parts) == 2 and parts[1] == str(player_server):
                            player_name = parts[0]

                    if player_server == server and player_name == name:
                        game_role_id = person_info.get("gameRoleId")
                        captured_game_role_id = game_role_id
                        zone = person_info.get("zone")

                        if game_role_id and zone:
                            logger.info(
                                f"在排行榜中找到角色: server={server} name={name} role_id={game_role_id} zone={zone}"
                            )

                            kungfu_detail = await asyncio.to_thread(
                                get_kungfu_detail_by_role_info,
                                game_role_id,
                                zone,
                                server,
                                tuilan_request=self.tuilan_request,
                                kungfu_pinyin_to_chinese=self.kungfu_pinyin_to_chinese,
                                match_detail_url=self.match_detail_url,
                                match_replay_url=self.match_replay_url,
                                role_name=name,
                                rank=rank,
                            )
                            kungfu_name = (kungfu_detail or {}).get("kungfu")

                            if kungfu_name:
                                logger.info(f"心法查询成功: server={server} name={name} kungfu={kungfu_name}")

                            result = {
                                "server": server,
                                "name": name,
                                "cache_time": time.time(),
                                **(kungfu_detail or {}),
                            }
                            if game_role_id:
                                result.setdefault("game_role_id", game_role_id)
                            if zone:
                                result.setdefault("zone", zone)
                            result["found"] = result.get("kungfu") is not None

                            await self._cache().save_kungfu_cache(server, name, result)
                            if result["found"]:
                                await self._warmup_inspect_cache_from_kungfu_detail(
                                    server=server,
                                    name=name,
                                    kungfu_detail=kungfu_detail,
                                )
                                return result

                            logger.info("心法查询失败: 未找到心法信息")
                            await self._warmup_inspect_cache_from_kungfu_detail(
                                server=server,
                                name=name,
                                kungfu_detail=kungfu_detail,
                            )
                            break

                logger.info(f"在排行榜中未找到匹配的角色: server={server} name={name}")
            else:
                logger.warning("获取排行榜数据失败，无法进行心法查询")
        except Exception as exc:
            logger.warning(f"心法查询过程中出错: {exc}")

        logger.info("心法查询失败，使用竞技场数据查询作为备选方案")
        logger.info(f"正在查询竞技场数据: server={server} name={name}")
        jjc_data = await self.defget_get(
            url=self.jjc_query_url,
            server=server,
            name=name,
            token=self.token,
            ticket=self.ticket,
        )

        if jjc_data.get("error") or jjc_data.get("msg") != "success":
            logger.warning(f"获取竞技场数据失败: {jjc_data}")
            logger.info(
                "defget 竞技场数据失败，尝试缓存对局详情胜场心法兜底: server={} name={} role_id={}",
                server,
                name,
                captured_game_role_id,
            )
            history_win_result = await self._get_cached_match_detail_win_kungfu(
                server=server,
                name=name,
                role_id=captured_game_role_id,
            )
            if history_win_result and history_win_result.get("found"):
                logger.info(
                    "defget 失败后缓存对局详情胜场心法兜底命中: server={} name={} kungfu={} win_count={} latest_match_id={}",
                    server,
                    name,
                    history_win_result.get("kungfu"),
                    history_win_result.get("cached_match_detail_win_count"),
                    history_win_result.get("cached_match_detail_latest_win_match_id"),
                )
                return await self._save_cached_match_detail_win_kungfu_result(
                    server=server,
                    name=name,
                    history_win_result=history_win_result,
                )
            logger.info(
                "defget 失败且缓存对局详情胜场心法兜底未命中，返回原错误: server={} name={}",
                server,
                name,
            )
            return {
                "error": True,
                "message": f"获取竞技场数据失败: {jjc_data.get('message', '未知错误')}",
                "server": server,
                "name": name,
            }

        refreshed_result = await self.update_kungfu_cache(server, name, jjc_data)
        if refreshed_result.get("found") and refreshed_result.get("kungfu"):
            return refreshed_result

        kungfu_info = None
        history_data = jjc_data.get("data", {}).get("history", [])
        if history_data:
            for match in history_data:
                if match.get("won") is True:
                    kungfu_info = match.get("kungfu")
                    break

        history_win_result: Optional[dict[str, Any]] = None
        if not kungfu_info:
            logger.info(
                "历史胜场心法兜底: defget 历史未找到心法，尝试从缓存对局详情推断 "
                "server={} name={}",
                server,
                name,
            )
            history_win_result = await self._get_cached_match_detail_win_kungfu(
                server=server,
                name=name,
                role_id=captured_game_role_id,
            )
            if history_win_result and history_win_result.get("found"):
                kungfu_info = history_win_result.get("kungfu")

        result = {
            "server": server,
            "name": name,
            "kungfu": kungfu_info,
            "found": kungfu_info is not None,
            "cache_time": time.time(),
        }
        if history_win_result:
            self._copy_cached_match_detail_win_kungfu_fields(result, history_win_result)
        result.setdefault("weapon_checked", True)
        await self._cache().save_kungfu_cache(server, name, result)
        return result

    async def _load_cached_kungfu_for_ranking(self, server: str, name: str) -> Optional[dict[str, Any]]:
        cache = self._cache()
        cached = await cache.load_kungfu_cache(server, name)
        if cached is not None and cached.get("kungfu") not in (None, ""):
            result = dict(cached)
            result.setdefault("server", server)
            result.setdefault("name", name)
            result["found"] = True
            logger.info(
                "排名统计使用缓存心法(新鲜命中): server={} name={} kungfu={}",
                server,
                name,
                result.get("kungfu"),
            )
            return result

        cached = await cache.load_kungfu_cache_raw(server, name)

        if not isinstance(cached, dict) or cached.get("kungfu") in (None, ""):
            logger.info("排名统计缓存心法未命中: server={} name={}", server, name)
            return None

        result = dict(cached)
        result.setdefault("server", server)
        result.setdefault("name", name)
        result["found"] = True
        logger.info(
            "排名统计使用缓存心法(原始缓存命中): server={} name={} kungfu={}",
            server,
            name,
            result.get("kungfu"),
        )
        return result

    async def get_ranking_kungfu_data(
        self,
        ranking_data: dict[str, Any],
        use_cached_kungfu: bool = False,
    ) -> dict[str, Any]:
        try:
            data_list = ranking_data.get("data", [])
            if not data_list:
                return {"error": True, "message": "排行榜数据为空"}

            sync_request = self._new_sync_request()
            historical = ranking_data.get("explicit_tag") is True
            use_local = bool(getattr(cfg, "JJC_RANKING_KUNGFU_CACHE_ENABLED", 0)) and not historical
            refresh_enabled = bool(getattr(cfg, "JJC_KUNGFU_REFRESH_ENABLED", 0)) and not historical
            resolved = None
            if use_local or refresh_enabled:
                try:
                    # Invalid configuration closes only the new feature, retaining the old path.
                    policy = self._ranking_kungfu_policy()
                except ValueError as exc:
                    logger.warning("排行榜心法配置无效，关闭新路径: {}", exc)
                    use_local = refresh_enabled = False
                if use_local:
                    resolved = await self.resolve_ranking_kungfus(ranking_data, policy=policy)
                elif refresh_enabled:
                    try:
                        resolved = await self.resolve_ranking_kungfus(ranking_data, policy=policy)
                    except Exception as exc:
                        logger.warning("排行榜心法预热登记失败，保留旧统计流程: {}", exc)
                        resolved = {"refresh_degraded": True}

            kungfu_results: list[dict[str, Any]] = []
            ranking_kungfu_lines: list[str] = []
            missing_kungfu_lines: list[str] = []

            total_players = len(data_list)
            logger.info(f"竞技场排行榜总人数: {total_players}")
            sync_repo = JjcSyncRepo()
            sync_batch_id = sync_request["batch_id"]
            sync_until_time = sync_request["until"]

            for i, player in enumerate(data_list):
                person_info = player.get("personInfo", {})
                server = person_info.get("server", "未知")
                name = person_info.get("roleName", "未知")
                score = self._extract_score(player, person_info)

                if name and "·" in name:
                    parts = name.rsplit("·", 1)
                    if len(parts) == 2 and parts[1] == str(server):
                        name = parts[0]

                if use_local:
                    kungfu_info = resolved["members"][i]
                    if not kungfu_info["found"]:
                        kungfu_info = await self._query_missing_ranking_kungfu(
                            kungfu_info, resolved["identities"][i], ranking_data, i + 1, policy)
                else:
                    kungfu_info = await self.get_user_kungfu(
                        server, name, ranking_data=ranking_data, rank=i + 1,
                        prefer_cache=use_cached_kungfu,
                    )
                indicator_kungfu = kungfu_info.get("kungfu_indicator")
                match_history_kungfu = kungfu_info.get("kungfu_match_history")
                if (
                    indicator_kungfu
                    and match_history_kungfu
                    and indicator_kungfu != match_history_kungfu
                ):
                    logger.warning(
                        "心法不一致(排名): rank={} score={} server={} name={} role_id={} global_role_id={} "
                        "indicator={} match_history={} selected={} source={} checked={} win_samples={}",
                        i + 1,
                        score,
                        server,
                        name,
                        kungfu_info.get("role_id"),
                        kungfu_info.get("global_role_id"),
                        indicator_kungfu,
                        match_history_kungfu,
                        kungfu_info.get("kungfu"),
                        kungfu_info.get("kungfu_selected_source"),
                        kungfu_info.get("match_history_checked"),
                        kungfu_info.get("match_history_win_samples"),
                    )
                ranking_member = {
                    "rank": i + 1,
                    "server": server,
                    "name": name,
                    "score": score,
                    "kungfu": kungfu_info.get("kungfu"),
                    "found": kungfu_info.get("found", False),
                    "kungfu_id": kungfu_info.get("kungfu_id"),
                    "game_role_id": person_info.get("gameRoleId"),
                    "global_role_id": (
                        kungfu_info.get("global_role_id")
                        or person_info.get("globalRoleId")
                    ),
                    "global_id": kungfu_info.get("global_id"),
                    "role_id": kungfu_info.get("role_id") or person_info.get("gameRoleId"),
                    "zone": person_info.get("zone"),
                    "teammates": kungfu_info.get("teammates"),
                    "weapon_icon": kungfu_info.get("weapon_icon"),
                    "weapon_quality": kungfu_info.get("weapon_quality"),
                    "weapon_name": extract_weapon_name(kungfu_info.get("weapon")) or kungfu_info.get("weapon_name"),
                    "weapon_checked": kungfu_info.get("weapon_checked"),
                    "teammates_checked": kungfu_info.get("teammates_checked"),
                    "match_history_checked": kungfu_info.get("match_history_checked"),
                    "kungfu_selected_source": kungfu_info.get("kungfu_selected_source"),
                    "kungfu_indicator": kungfu_info.get("kungfu_indicator"),
                    "kungfu_match_history": kungfu_info.get("kungfu_match_history"),
                    "cached_match_detail_win_count": kungfu_info.get("cached_match_detail_win_count"),
                    "cached_match_detail_total_count": kungfu_info.get("cached_match_detail_total_count"),
                    "cached_match_detail_latest_win_match_id": kungfu_info.get("cached_match_detail_latest_win_match_id"),
                    "cached_match_detail_latest_win_time": kungfu_info.get("cached_match_detail_latest_win_time"),
                    "cached_match_detail_win_samples": kungfu_info.get("cached_match_detail_win_samples"),
                }
                if use_local:
                    for field in _RANKING_KUNGFU_FIELDS:
                        ranking_member[field] = copy.deepcopy(kungfu_info.get(field))
                kungfu_results.append(ranking_member)
                if self._should_enqueue_ranking_member_immediately(
                    member=ranking_member,
                    rank=i + 1,
                    total_players=total_players,
                ):
                    await self._enqueue_ranking_member_for_sync(
                        member=ranking_member,
                        repo=sync_repo,
                        batch_id=sync_batch_id,
                        queue_sync_until_time=sync_until_time,
                        request_order=sync_request["order"],
                    )

                if kungfu_info.get("found") and kungfu_info.get("kungfu"):
                    ranking_kungfu_lines.append(f"{i + 1}. {server} {name} - {kungfu_info['kungfu']}")
                else:
                    role_id = (
                        person_info.get("gameRoleId")
                        or person_info.get("globalRoleId")
                        or kungfu_info.get("role_id")
                        or kungfu_info.get("global_role_id")
                        or "未知"
                    )
                    missing_kungfu_lines.append(f"{i + 1}. {server} {name} - id:{role_id}")

            def count_kungfu_by_rank(player_data: list[dict[str, Any]], max_rank: int) -> dict[str, Any]:
                healer_kungfu = self.kungfu_healer_list
                dps_kungfu = self.kungfu_dps_list

                healer_count: dict[str, int] = {}
                dps_count: dict[str, int] = {}

                healer_valid_count = 0
                dps_valid_count = 0
                invalid_count = 0
                invalid_details: list[str] = []

                healer_first_rank: dict[str, int] = {}
                dps_first_rank: dict[str, int] = {}
                healer_members: dict[str, list[dict[str, Any]]] = {}
                dps_members: dict[str, list[dict[str, Any]]] = {}

                overall_min_score = None

                for kungfu in healer_kungfu:
                    healer_count[kungfu] = 0
                    healer_members[kungfu] = []
                for kungfu in dps_kungfu:
                    dps_count[kungfu] = 0
                    dps_members[kungfu] = []

                for player_item in player_data[:max_rank]:
                    score = self._coerce_score(player_item.get("score"))
                    if score is not None and (overall_min_score is None or score < overall_min_score):
                        overall_min_score = score
                if overall_min_score is None:
                    logger.warning(f"前{max_rank}名范围内未找到可用分数字段，最低分将无法展示")

                for i, player_item in enumerate(player_data[:max_rank]):
                    if player_item.get("found") and player_item.get("kungfu"):
                        kungfu = player_item["kungfu"]
                        score = self._coerce_score(player_item.get("score"))

                        if kungfu in healer_kungfu:
                            healer_count[kungfu] = healer_count.get(kungfu, 0) + 1
                            healer_valid_count += 1
                            if kungfu not in healer_first_rank:
                                healer_first_rank[kungfu] = i + 1
                            healer_members[kungfu].append(
                                {
                                    "rank": i + 1,
                                    "server": player_item.get("server", "未知"),
                                    "name": player_item.get("name", "未知"),
                                    "score": score,
                                    "kungfu_id": player_item.get("kungfu_id"),
                                    "game_role_id": player_item.get("game_role_id"),
                                    "global_role_id": player_item.get("global_role_id"),
                                    "global_id": player_item.get("global_id"),
                                    "role_id": player_item.get("role_id"),
                                    "zone": player_item.get("zone"),
                                    "teammates": player_item.get("teammates"),
                                    "weapon_icon": player_item.get("weapon_icon"),
                                    "weapon_quality": player_item.get("weapon_quality"),
                                    "weapon_name": player_item.get("weapon_name"),
                                    **({field: copy.deepcopy(player_item.get(field)) for field in
                                        _RANKING_KUNGFU_FIELDS} if use_local else {}),
                                }
                            )
                        elif kungfu in dps_kungfu:
                            dps_count[kungfu] = dps_count.get(kungfu, 0) + 1
                            dps_valid_count += 1
                            if kungfu not in dps_first_rank:
                                dps_first_rank[kungfu] = i + 1
                            dps_members[kungfu].append(
                                {
                                    "rank": i + 1,
                                    "server": player_item.get("server", "未知"),
                                    "name": player_item.get("name", "未知"),
                                    "score": score,
                                    "kungfu_id": player_item.get("kungfu_id"),
                                    "game_role_id": player_item.get("game_role_id"),
                                    "global_role_id": player_item.get("global_role_id"),
                                    "global_id": player_item.get("global_id"),
                                    "role_id": player_item.get("role_id"),
                                    "zone": player_item.get("zone"),
                                    "teammates": player_item.get("teammates"),
                                    "weapon_icon": player_item.get("weapon_icon"),
                                    "weapon_quality": player_item.get("weapon_quality"),
                                    "weapon_name": player_item.get("weapon_name"),
                                    **({field: copy.deepcopy(player_item.get(field)) for field in
                                        _RANKING_KUNGFU_FIELDS} if use_local else {}),
                                }
                            )
                        else:
                            logger.info(
                                f"⚠️ 发现未分类心法：第{i+1}名 {player_item.get('server', '未知')} "
                                f"{player_item.get('name', '未知')} - {kungfu}"
                            )
                    else:
                        invalid_count += 1
                        invalid_details.append(
                            "第{}名：{} {} score={} role_id={} global_role_id={} zone={} "
                            "source={} indicator={} match_history={} reason={}".format(
                                i + 1,
                                player_item.get("server", "未知"),
                                player_item.get("name", "未知"),
                                player_item.get("score"),
                                player_item.get("role_id"),
                                player_item.get("global_role_id"),
                                player_item.get("zone"),
                                player_item.get("kungfu_selected_source"),
                                player_item.get("kungfu_indicator"),
                                player_item.get("kungfu_match_history"),
                                self._build_invalid_kungfu_reason(player_item),
                            )
                        )

                for i, kungfu in enumerate(healer_kungfu):
                    if kungfu not in healer_first_rank:
                        healer_first_rank[kungfu] = 9999 + i
                for i, kungfu in enumerate(dps_kungfu):
                    if kungfu not in dps_first_rank:
                        dps_first_rank[kungfu] = 9999 + i

                sorted_healer = sorted(
                    healer_count.items(),
                    key=lambda x: (x[1], -healer_first_rank[x[0]]),
                    reverse=True,
                )
                sorted_dps = sorted(
                    dps_count.items(),
                    key=lambda x: (x[1], -dps_first_rank[x[0]]),
                    reverse=True,
                )

                if invalid_details:
                    logger.info(f"前{max_rank}名中无效数据角色（共{len(invalid_details)}个）")
                    for detail in invalid_details:
                        logger.info(f"invalid: {detail}")

                actual_count = len(player_data[:max_rank])
                cache_metadata = {"unknown_members": [dict(item, rank=index + 1)
                    for index, item in enumerate(player_data[:max_rank]) if not item.get("found")
                    or item.get("kungfu") not in healer_kungfu + dps_kungfu]}
                if use_local:
                    counts = {key: sum(item.get("kungfu_freshness") == key for item in player_data[:max_rank])
                              for key in ("fresh", "stale", "unknown")}
                    cache_metadata = {
                        "kungfu_cache": dict(counts, total=actual_count,
                            coverage=(counts["fresh"] + counts["stale"]) / actual_count if actual_count else None),
                        "unknown_members": cache_metadata["unknown_members"],
                    }
                return {
                    **cache_metadata,
                    "total_players": actual_count if use_local else max_rank,
                    "healer": {
                        **({"equipment_incomplete": True} if use_local else {}),
                        "valid_count": healer_valid_count,
                        "distribution": dict(sorted_healer),
                        "list": sorted_healer,
                        "min_score": overall_min_score,
                        "members": healer_members,
                    },
                    "dps": {
                        **({"equipment_incomplete": True} if use_local else {}),
                        "valid_count": dps_valid_count,
                        "distribution": dict(sorted_dps),
                        "list": sorted_dps,
                        "min_score": overall_min_score,
                        "members": dps_members,
                    },
                    "total_valid_count": healer_valid_count + dps_valid_count,
                    "invalid_count": invalid_count,
                    "invalid_details": invalid_details,
                    "unclassified_count": (actual_count if use_local else max_rank) - (healer_valid_count + dps_valid_count + invalid_count),
                }

            logger.info("正在统计kungfu分布")
            kungfu_stats: dict[str, Any] = {}
            if total_players >= 1000:
                logger.info("检测到排行榜包含1000条数据，开始统计前1000心法分布")
                kungfu_stats["top_1000"] = count_kungfu_by_rank(kungfu_results, 1000)
            kungfu_stats["top_200"] = count_kungfu_by_rank(kungfu_results, 200)
            kungfu_stats["top_100"] = count_kungfu_by_rank(kungfu_results, 100)
            kungfu_stats["top_50"] = count_kungfu_by_rank(kungfu_results, 50)

            equipment_view = None
            equipment_expected = None
            if getattr(cfg, "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED", 0) == 1 and not historical:
                if self.equipment_service is None:
                    raise RuntimeError("equipment_service_unavailable")
                frozen = copy.deepcopy(kungfu_results)
                for member in frozen:
                    member["lane"] = ("healer" if member.get("kungfu") in self.kungfu_healer_list else
                                      "dps" if member.get("kungfu") in self.kungfu_dps_list else "unknown")
                equipment_view = await self.equipment_service.build_frozen_view(
                    timestamp=int(ranking_data.get("cache_time") or time.time()), members=frozen)
                equipment_expected = equipment_view.pop("_publication_expected", None)
                previous_stats = kungfu_stats
                kungfu_stats = equipment_view_statistics(equipment_view)
                for range_key, previous in previous_stats.items():
                    for field in ("kungfu_cache",):
                        if field in previous:
                            kungfu_stats[range_key][field] = copy.deepcopy(previous[field])

            result: dict[str, Any] = {
                "_sync_request": sync_request,
                "kungfu_statistics": kungfu_stats,
                "ranking_kungfu_lines": ranking_kungfu_lines,
                "missing_kungfu_lines": missing_kungfu_lines,
                "ranking_display_kind": "historical" if historical else "current",
            }

            if equipment_view is not None:
                result["equipment_view"] = equipment_view
                result["_equipment_expected"] = equipment_expected

            if "refresh_degraded" in ranking_data:
                result["refresh_degraded"] = bool(ranking_data["refresh_degraded"])
            if resolved is not None:
                result["refresh_degraded"] = resolved["refresh_degraded"]
                if use_local:
                    result["kungfu_cache_scope"] = copy.deepcopy(resolved["kungfu_cache_scope"])

            logger.info("=" * 80)
            logger.info("KUNGFU统计结果 (奶妈/DPS分类)")
            logger.info("=" * 80)

            for rank_range, stats in kungfu_stats.items():
                logger.info(
                    f"\n{rank_range.upper()} ({stats['total_players']}人，有效数据{stats['total_valid_count']}人，"
                    f"无效数据{stats['invalid_count']}人):"
                )
                logger.info("=" * 60)

                logger.info(f"奶妈排名（{stats['healer']['valid_count']}人）")
                logger.info("-" * 40)
                if stats["healer"]["list"]:
                    for kungfu, count in stats["healer"]["list"]:
                        percentage = (
                            (count / stats["healer"]["valid_count"] * 100)
                            if stats["healer"]["valid_count"] > 0
                            else 0
                        )
                        logger.info(f"{kungfu}: {count}人 ({percentage:.1f}%)")
                else:
                    logger.info("无奶妈数据")

                logger.info(f"DPS排名（{stats['dps']['valid_count']}人）")
                logger.info("-" * 40)
                if stats["dps"]["list"]:
                    for kungfu, count in stats["dps"]["list"]:
                        percentage = (
                            (count / stats["dps"]["valid_count"] * 100)
                            if stats["dps"]["valid_count"] > 0
                            else 0
                        )
                        logger.info(f"{kungfu}: {count}人 ({percentage:.1f}%)")
                else:
                    logger.info("无DPS数据")

            logger.info("=" * 80)

            return result
        except Exception as exc:
            logger.exception(f"获取心法分布数据失败: {exc}")
            return {"error": True, "message": f"获取心法分布数据失败: {exc}"}

    def calculate_season_week_info(self, default_week: int, cache_time: Optional[float] = None) -> str:
        try:
            now = datetime.fromtimestamp(cache_time) if cache_time else datetime.now()
            season_start = datetime.strptime(self.current_season_start, "%Y-%m-%d")

            def week_monday(dt: datetime) -> datetime:
                monday = dt - timedelta(days=dt.weekday())
                return monday.replace(hour=0, minute=0, second=0, microsecond=0)

            season_anchor_monday = week_monday(season_start)
            current_monday = week_monday(now)
            season_week_now = max(1, ((current_monday - season_anchor_monday).days // 7) + 1)
            api_week = max(1, int(default_week))

            now_iso_year, now_iso_week, _ = now.isocalendar()
            api_year = now_iso_year
            week_gap = api_week - now_iso_week
            if week_gap > 26:
                api_year -= 1
            elif week_gap < -26:
                api_year += 1

            try:
                target_monday = datetime.fromisocalendar(api_year, api_week, 1)
            except ValueError:
                logger.info(f"calculate_season_week_info: defaultWeek={default_week} 生成ISO日期失败，使用当前周")
                target_monday = current_monday

            season_week_from_api = max(
                1, ((target_monday - season_anchor_monday).days // 7) + 1
            )
            weekday_names = ["周1", "周2", "周3", "周4", "周5", "周6", "周7"]
            weekday_str = weekday_names[now.weekday()]
            time_str = now.strftime("%H:%M")

            logger.info(
                f"defaultWeek={default_week} season_anchor_monday={season_anchor_monday} current_monday={current_monday} "
                f"season_week_now={season_week_now} api_week={api_week} target_monday={target_monday} "
                f"weekday_str={weekday_str} time_str={time_str}"
            )
            if target_monday < current_monday:
                return f"第{season_week_from_api}周 结算"

            if target_monday == current_monday:
                return f"第{season_week_now}周 {weekday_str} {time_str}"

            logger.info(f"calculate_season_week_info: defaultWeek={api_week} 指向未来周，锚定赛季周 {season_week_from_api}")
            return f"第{season_week_from_api}周 {weekday_str} {time_str}"

        except Exception as exc:
            logger.warning(f"计算赛季周信息失败: {exc}")
            return f"第{default_week}周"
