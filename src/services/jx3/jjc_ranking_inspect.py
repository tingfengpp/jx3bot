from __future__ import annotations

import asyncio
import copy
import inspect
import threading
import time
from weakref import WeakKeyDictionary
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Union

from nonebot import logger
from src.utils.jjc_query_observability import (foreground_safe_logger, begin_query_trace,
    end_query_trace, query_stage, observed_query_stage, query_queue, query_cache, query_failure)

logger = foreground_safe_logger(logger, component="inspect")
import config as cfg

from src.services.jx3.jjc_cache_repo import JjcCacheRepo
from src.infra.jjc_foreground_mongo import (ForegroundMongoFailure, ForegroundMongoSession, default_controller)
from src.utils.jjc_indicator_cache import indicator_cache_age, normalize_indicator_scope
from src.services.jx3.jjc_ranking import JjcRankingService
from src.services.jx3.indicator_utils import parse_3v3_indicator
from src.services.jx3.kungfu import (
    extract_global_id_from_match_replay,
    merge_replay_global_ids_into_match_detail,
)
from src.services.jx3.match_history import MatchHistoryClient
from src.services.jx3.match_detail import MatchDetailClient, MatchDetailResponse
from src.services.jx3.match_detail_persistence import MatchDetailPersistenceService
from src.services.jx3.match_replay import MatchReplayClient
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_sync_repo import JjcSyncRepo
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from src.services.jx3.role_identity_recovery import indicator_matches_request, recover_query_ids
from src.services.jx3.jjc_role_query_context import (
    ForegroundQueryContext, ForegroundQueryError, ForegroundQueryTimeout, ForegroundQueryBusy,
    identity_binding, indicator_binding_matches, identity_hints_conflict, local_lookup_hints,
    ranking_name_matches, role_name_candidates, foreground_flights, release_after_owners,
)


SYNCED_MATCH_PAGE_SYNC_PRIORITY = 2


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _extract_match_id(match: dict[str, Any]) -> Optional[int]:
    for key in ("match_id", "matchId", "matchID", "id"):
        value = _coerce_int(match.get(key))
        if value is not None:
            return value
    return None


def _extract_id_like_fields(match: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in match.items():
        if "id" not in str(key).lower():
            continue
        result[str(key)] = value
    return result


def _extract_grade_like_fields(match: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in match.items():
        key_text = str(key).lower()
        if "grade" in key_text or "rank" in key_text or "segment" in key_text:
            result[str(key)] = value
    return result


def _normalize_name(name: Optional[str]) -> str:
    # This is also used on already confirmed names. Never strip a second time.
    return str(name or "").strip()


def _pick_str(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_match_time(match: dict[str, Any]) -> Optional[int]:
    return _coerce_int(
        match.get("match_time")
        or match.get("matchTime")
        or match.get("start_time")
        or match.get("startTime")
        or match.get("time")
    )


def _extract_duration(match: dict[str, Any]) -> Optional[int]:
    duration = _coerce_int(match.get("duration") or match.get("fight_time") or match.get("fight_seconds"))
    if duration is not None:
        return duration
    start_time = _coerce_int(match.get("start_time") or match.get("startTime"))
    end_time = _coerce_int(match.get("end_time") or match.get("endTime"))
    if start_time and end_time and end_time >= start_time:
        return end_time - start_time
    return None


def _extract_avg_grade(match: dict[str, Any]) -> Optional[int]:
    return _coerce_int(
        match.get("avgGrade")
        or match.get("avg_grade")
        or match.get("grade")
        or match.get("segment")
        or match.get("level")
    )


def _extract_total_mmr(match: dict[str, Any]) -> Optional[int]:
    return _coerce_int(
        match.get("totalMmr")
        or match.get("total_mmr")
        or match.get("score")
        or match.get("mmr_total")
    )


def _parse_3v3_indicator(raw: dict[str, Any]) -> dict[str, Any]:
    return parse_3v3_indicator(raw)


def _extract_person_id_from_indicator(raw: dict[str, Any]) -> Optional[str]:
    data = raw.get("data") if isinstance(raw, dict) else None
    payload = data if isinstance(data, dict) else raw
    person_info = payload.get("person_info") if isinstance(payload, dict) else None
    if not isinstance(person_info, dict):
        return None
    return _pick_str(person_info.get("person_id"), person_info.get("personId"))


def filter_current_season_matches(raw_matches: list, *, season_start_time: int = 0) -> list:
    """Keep only matches with evidence of occurring in the configured season."""
    return [
        item for item in raw_matches
        if isinstance(item, dict) and (
            season_start_time <= 0
            or (_extract_match_time(item) or 0) >= season_start_time
        )
    ]


def _has_previous_season_matches(raw_matches: list, season_start_time: int) -> bool:
    return season_start_time > 0 and any(
        0 < (_extract_match_time(item) or 0) < season_start_time
        for item in raw_matches if isinstance(item, dict)
    )


def recent_matches_pagination(
    raw_matches: list, *, cursor: int, page_size: int, season_start_time: int = 0,
) -> dict[str, Any]:
    """Advance by upstream rows; its descending history ends at the season boundary."""
    has_more = len(raw_matches) >= page_size and not _has_previous_season_matches(
        raw_matches, season_start_time,
    )
    return {
        "cursor": cursor,
        "has_more": has_more,
        "next_cursor": cursor + len(raw_matches) if has_more else None,
    }


def normalize_recent_matches(
    raw_matches: list,
    *,
    kungfu_pinyin_to_chinese: dict[str, str],
    max_recent_matches: int = 20,
    season_start_time: int = 0,
) -> list:
    """Convert raw match history items to the recent_matches shape used by the live path.

    Pure function: accepts kungfu_pinyin_to_chinese and max_recent_matches explicitly.
    Handles pvpType/pvp_type/type filtering, field aliases, Chinese kungfu translation,
    sort descending, truncation, and missing fields gracefully.
    """
    history_3v3: list = []
    for item in filter_current_season_matches(raw_matches, season_start_time=season_start_time):
        if not isinstance(item, dict):
            continue
        type_raw = None
        type_present = False
        for key in ("pvpType", "pvp_type", "type"):
            if key in item:
                type_raw = item[key]
                type_present = True
                break
        if type_present:
            pvp_type = _coerce_int(type_raw)
            if pvp_type != 3:
                continue
        match_id = _extract_match_id(item)
        match_time = _extract_match_time(item)
        kungfu_raw = item.get("kungfu") or item.get("kungfu_name")
        kungfu_cn = ""
        if kungfu_raw:
            kungfu_cn = kungfu_pinyin_to_chinese.get(str(kungfu_raw), str(kungfu_raw))
        history_3v3.append(
            {
                "match_id": match_id,
                "won": bool(item.get("won")),
                "kungfu": kungfu_cn,
                "avg_grade": _extract_avg_grade(item),
                "total_mmr": _extract_total_mmr(item),
                "mmr_delta": _coerce_int(item.get("mmr")),
                "mvp": bool(item.get("mvp")),
                "match_time": match_time,
                "start_time": _coerce_int(item.get("startTime") or item.get("start_time")) or match_time,
                "end_time": _coerce_int(item.get("endTime") or item.get("end_time")),
                "duration": _extract_duration(item),
            }
        )

    history_3v3.sort(key=lambda item: item.get("match_time") or item.get("start_time") or 0, reverse=True)
    return history_3v3[:max_recent_matches]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class JjcRankingInspectService:
    ranking_service: JjcRankingService
    kungfu_cache_repo: JjcCacheRepo
    match_history_client: MatchHistoryClient
    match_detail_client: MatchDetailClient
    cache_repo: JjcInspectRepo
    tuilan_request: Callable[[str, dict[str, Any]], Any]
    role_indicator_fetcher: Callable[..., Optional[dict[str, Any]]]
    kungfu_pinyin_to_chinese: dict[str, str]
    match_replay_client: Optional[MatchReplayClient] = None
    match_detail_projection_service: Any = None
    match_detail_participant_projection_service: Any = None
    match_detail_persistence_service: Any = None
    role_identity_repo: Any = None
    sync_repo: Any = None
    role_recent_ttl_seconds: int = 86400
    role_indicator_ttl_seconds: int = 86400
    role_indicator_stale_grace_seconds: int = 3600
    # A completion observation must fit the foreground freshness window.
    resolution_indicator_max_age_seconds: float = 10.0
    current_season: str = ""
    season_start_time: int = 0
    max_recent_matches: int = 20
    foreground_client: Any = None
    foreground_mongo_controller: Any = None
    foreground_timeout_seconds: float = 10.0
    foreground_queue_wait_seconds: float = 0.3
    foreground_cleanup_seconds: float = 0.25
    query_context: Any = field(default=None, repr=False)
    _foreground_token: Any = field(default_factory=object, init=False, repr=False)
    cache_hit_replay_enrich_timeout_seconds: float = 5.0
    _tuilan_query_locks: WeakKeyDictionary = field(default_factory=WeakKeyDictionary, init=False, repr=False)
    _tuilan_query_locks_guard: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _identity_recovery_tasks: dict = field(default_factory=dict, init=False, repr=False)
    _identity_recovery_cooldown: dict = field(default_factory=dict, init=False, repr=False)

    def _get_tuilan_query_lock(self, endpoint_key: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._tuilan_query_locks_guard:
            locks = self._tuilan_query_locks.get(loop)
            if locks is None:
                locks = {}
                self._tuilan_query_locks[loop] = locks
            lock = locks.get(endpoint_key)
            if lock is None:
                lock = asyncio.Lock()
                locks[endpoint_key] = lock
        return lock

    def _translate_kungfu_name(self, value: Any) -> str:
        text = _pick_str(value) or ""
        if not text:
            return ""
        return self.kungfu_pinyin_to_chinese.get(text, text)

    def _role_identity_repo(self) -> Any:
        return self.role_identity_repo or RoleIdentityRepo()

    def _sync_repo(self) -> Any:
        return self.sync_repo or JjcSyncRepo()

    @staticmethod
    def _serialize_identity(identity: dict[str, Any]) -> dict[str, Any]:
        identity_id = identity.get("_id") or identity.get("identity_id")
        result: dict[str, Any] = {}
        if identity_id is not None:
            result["identity_id"] = str(identity_id)
        for key in (
            "identity_key",
            "server",
            "name",
            "zone",
            "game_role_id",
            "global_id",
            "global_role_id",
            "role_id",
            "person_id",
            "identity_level",
            "source",
        ):
            value = identity.get(key)
            if value is not None:
                result[key] = str(value) if key.endswith("_id") else value
        return result
    @staticmethod
    def _serialize_sync_status(doc: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not doc:
            return {"exists": False, "status": "not_queued"}
        result = {
            "exists": True,
            "identity_id": str(doc.get("identity_id")) if doc.get("identity_id") is not None else None,
            "identity_key": doc.get("identity_key"),
            "status": doc.get("status"),
            "queued_at": doc.get("queued_at"),
            "queue_mode": doc.get("queue_mode"),
            "queue_source": doc.get("queue_source"),
            "queue_sync_until_time": doc.get("queue_sync_until_time"),
            "priority": doc.get("priority"),
            "last_synced_at": doc.get("last_synced_at"),
            "latest_seen_match_time": doc.get("latest_seen_match_time"),
            "history_exhausted": doc.get("history_exhausted"),
            "last_error": doc.get("last_error"),
            "lease_owner": doc.get("lease_owner"),
            "lease_expires_at": doc.get("lease_expires_at"),
        }
        return result

    async def _serialize_sync_status_with_position(self, doc: Optional[dict[str, Any]]) -> dict[str, Any]:
        result = self._serialize_sync_status(doc)
        if doc and str(doc.get("status") or "") == "queued":
            sync_repo = self._sync_repo()
            if hasattr(sync_repo, "get_queue_position"):
                position = await sync_repo.get_queue_position(doc)
                if position is not None:
                    result["queue_position"] = position
        return result

    async def _resolve_synced_identity_only(
        self, *, server: str, name: str, identity_hints: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        repo = self._role_identity_repo()
        hints = identity_hints or {}
        strong = hints.get("global_id") or hints.get("global_role_id") or (
            hints.get("zone") and (hints.get("game_role_id") or hints.get("role_id"))
        )
        finder = getattr(repo, "resolve_best_identity_with_id", None)
        if strong and callable(finder):
            identity = await _maybe_await(finder(server=server, name=name, **local_lookup_hints(hints)))
            if isinstance(identity, dict) and identity:
                return identity
        for candidate in role_name_candidates(server, name):
            if hasattr(repo, "find_best_by_name_with_id"):
                identity = await repo.find_best_by_name_with_id(server, candidate)
            else:
                identity = await repo.resolve_best_identity_with_id(server=server, name=candidate)
            if isinstance(identity, dict) and identity:
                return identity
        return None

    async def _find_synced_role_candidates(self, *, server: str, name: str, limit: int = 8) -> List[Dict[str, Any]]:
        repo = self._role_identity_repo()
        finder = getattr(repo, "find_synced_match_page_candidates", None)
        if not callable(finder):
            return []
        try:
            docs = await finder(server, name, limit=limit)
        except Exception as exc:
            logger.warning("JJC 已同步角色候选查询失败: server={} name={} error={}", server, name, exc)
            return []

        candidates: List[Dict[str, Any]] = []
        for doc in docs or []:
            if not isinstance(doc, dict):
                continue
            identity = self._serialize_identity(doc)
            candidate_server = _pick_str(doc.get("server"), identity.get("server")) or ""
            candidate_name = _normalize_name(_pick_str(doc.get("name"), identity.get("name")) or "")
            if not candidate_server or not candidate_name:
                continue
            reason = "same_name_other_server"
            if "@" in candidate_name:
                reason = "name_with_suffix"
            sync_doc = await self._sync_repo().get_queue_state_by_identity_id(doc.get("_id"))
            candidates.append({
                "player": {"server": candidate_server, "name": candidate_name},
                "identity": identity,
                "sync_status": await self._serialize_sync_status_with_position(sync_doc),
                "reason": reason,
            })
        return candidates

    async def resolve_synced_role(self, *, server: str, name: str) -> dict[str, Any]:
        """Resolve a role for the synced-match page using role_identities only."""
        identity = None
        if str(server or "").strip():
            identity = await self._resolve_synced_identity_only(server=server, name=name)
        if not identity:
            candidates = await self._find_synced_role_candidates(server=server, name=name)
            return {
                "error": True,
                "message": "role_identity_not_found",
                "player": {"server": server, "name": _normalize_name(name)},
                "candidates": candidates,
                "guidance": (
                    "如果目标角色暂未收录，可以搜索和他打过 3v3 的角色，"
                    "更新该角色对局后，系统会从对局详情里同步目标角色信息。"
                ),
            }
        sync_doc = await self._sync_repo().get_queue_state_by_identity_id(identity.get("_id"))
        sync_status = await self._serialize_sync_status_with_position(sync_doc)
        return {
            "player": {
                "server": identity.get("server") or server,
                "name": _normalize_name(_pick_str(identity.get("name")) or name),
            },
            "identity": self._serialize_identity(identity),
            "sync_status": sync_status,
        }

    async def get_synced_role_matches(
        self,
        *,
        server: str,
        name: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        resolved = await self.resolve_synced_role(server=server, name=name)
        if resolved.get("error"):
            return resolved
        identity = resolved.get("identity") or {}
        try:
            matches = await self.cache_repo.list_saved_local_3v3_matches_for_identity(
                identity_id=identity.get("identity_id"),
                identity_key=identity.get("identity_key"),
                season_id=self.current_season,
                season_start_time=self.season_start_time,
                server=identity.get("server") or server,
                name=identity.get("name") or name,
                global_id=identity.get("global_id"),
                global_role_id=identity.get("global_role_id"),
                role_id=identity.get("role_id"),
                game_role_id=identity.get("game_role_id"),
                page=page,
                page_size=page_size,
            )
        except Exception as exc:
            logger.warning(
                "JJC 已同步对局参与者投影查询失败: server={} name={} page={} page_size={} "
                "identity_id={} global_id={} error={}".format(
                    server,
                    name,
                    page,
                    page_size,
                    identity.get("identity_id"),
                    identity.get("global_id"),
                    exc,
                )
            )
            return {
                "error": True,
                "message": "match_participants_query_failed",
                "player": resolved.get("player"),
                "identity": identity,
                "sync_status": resolved.get("sync_status"),
            }
        return {
            "player": resolved.get("player"),
            "identity": identity,
            "sync_status": resolved.get("sync_status"),
            "pagination": {
                "page": matches.get("page", page),
                "page_size": matches.get("page_size", page_size),
                "total": matches.get("total", 0),
                "has_more": matches.get("has_more", False),
            },
            "recent_matches": matches.get("items") or [],
            "cache": {},
        }

    async def enqueue_synced_role(self, *, server: str, name: str) -> dict[str, Any]:
        identity = await self._resolve_synced_identity_only(server=server, name=name)
        if not identity:
            return {
                "error": True,
                "message": "role_identity_not_found",
                "player": {"server": server, "name": _normalize_name(name)},
            }
        sync_repo = self._sync_repo()
        existing_sync_doc = await sync_repo.get_queue_state_by_identity_id(identity.get("_id"))
        if (
            existing_sync_doc
            and str(existing_sync_doc.get("status") or "") == "queued"
            and _coerce_int(existing_sync_doc.get("priority")) == SYNCED_MATCH_PAGE_SYNC_PRIORITY
        ):
            sync_status = await self._serialize_sync_status_with_position(existing_sync_doc)
            return {
                "queued": True,
                "already_queued": True,
                "player": {
                    "server": identity.get("server") or server,
                    "name": _normalize_name(_pick_str(identity.get("name")) or name),
                },
                "identity": self._serialize_identity(identity),
                "sync_status": sync_status,
            }
        sync_doc = await sync_repo.enqueue_existing_identity(
            identity,
            priority=SYNCED_MATCH_PAGE_SYNC_PRIORITY,
            source="synced_match_page",
            mode="full",
            queue_sync_until_time=None,
        )
        if sync_doc is None:
            sync_doc = existing_sync_doc or await sync_repo.get_queue_state_by_identity_id(identity.get("_id"))
            if not sync_doc or str(sync_doc.get("status") or "") != "syncing":
                return {
                    "error": True,
                    "message": "enqueue_failed",
                    "player": {
                        "server": identity.get("server") or server,
                        "name": _normalize_name(_pick_str(identity.get("name")) or name),
                    },
                    "identity": self._serialize_identity(identity),
                    "sync_status": await self._serialize_sync_status_with_position(sync_doc),
                }
        sync_status = await self._serialize_sync_status_with_position(sync_doc)
        return {
            "queued": bool(sync_doc and sync_doc.get("status") == "queued"),
            "player": {
                "server": identity.get("server") or server,
                "name": _normalize_name(_pick_str(identity.get("name")) or name),
            },
            "identity": self._serialize_identity(identity),
            "sync_status": sync_status,
        }

    async def _fetch_match_replay(self, match_id: int) -> Optional[dict[str, Any]]:
        if self.match_replay_client is None:
            return None
        replay = await self._run_serialized_tuilan_query(
            "match_replay",
            f"match_replay:{match_id}",
            self.match_replay_client.get_match_replay,
            match_id=match_id,
        )
        if not isinstance(replay, dict) or replay.get("error"):
            return None
        return replay

    async def _resolve_global_id_from_match(
        self,
        *,
        match_id: int,
        identity: dict[str, Any],
        server: str,
        name: str,
    ) -> Optional[str]:
        if _pick_str(identity.get("global_id")):
            return _pick_str(identity.get("global_id"))
        replay = await self._fetch_match_replay(match_id)
        if not replay:
            return None
        return extract_global_id_from_match_replay(
            replay,
            role_id=_pick_str(identity.get("role_id"), identity.get("game_role_id")),
            role_name=_normalize_name(name),
            server=server,
        )

    async def _enrich_detail_payload_with_replay(self, payload: dict[str, Any]) -> bool:
        match_id = _coerce_int(payload.get("match_id"))
        detail = payload.get("detail")
        if match_id is None or not isinstance(detail, dict):
            return False
        replay = payload.get("replay")
        fetched_replay = False
        if not isinstance(replay, dict) or replay.get("error"):
            replay = await self._fetch_match_replay(match_id)
            fetched_replay = True
            if replay:
                payload["replay"] = replay
        if not replay:
            return False
        merge_replay_global_ids_into_match_detail({"data": detail}, replay)
        return True

    async def _enrich_cached_match_detail_replay_and_project(
        self,
        *,
        match_id: int,
        cached_at: Any,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        before = repr(data)
        await self._enrich_detail_payload_with_replay(data)
        if repr(data) == before:
            return {"data": data}

        effective_cached_at = cached_at or time.time()
        await self._persist_match_detail(
            match_id=match_id,
            payload={"cached_at": effective_cached_at, "data": data},
            source="inspect_cache_hit_replay_enrich",
        )
        return {"data": data}

    @staticmethod
    def _log_background_task_exception(task: "asyncio.Task[Any]") -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("JJC 对局详情 replay 后台补全任务被取消")
        except Exception as exc:
            logger.warning("JJC 对局详情 replay 后台补全任务失败: error={}".format(exc))

    @staticmethod
    def _consume_foreground_task(task) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def foreground_snapshot():
        return foreground_flights().snapshot()

    def _foreground_key(self, kind, kwargs, identity=None):
        hints = kwargs.get('identity_hints') or kwargs
        locator = (str(kwargs.get('server') or '').strip(), _normalize_name(kwargs.get('name')))
        if identity:
            query_cache(key_type="canonical")
            locator = (str(identity.get('identity_key') or ''), str(identity.get('server') or ''),
                       str(identity.get('name') or ''))
        return (asyncio.get_running_loop(), self._foreground_token, kind, locator,
                bool(kwargs.get('identity_only')), bool(kwargs.get('force_refresh')),
                *(str(hints.get(field) or '') for field in
                  ('global_id', 'global_role_id', 'game_role_id', 'role_id', 'zone')),
                kwargs.get('cursor', 0) if kind == 'recent' else None)

    async def _foreground_request(self, kind: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        trace, token = begin_query_trace(kind)
        context = ForegroundQueryContext(
            timeout=self.foreground_timeout_seconds, queue_wait=self.foreground_queue_wait_seconds,
            cleanup=self.foreground_cleanup_seconds,
        )
        registry = foreground_flights()
        admitted = False
        result = None
        try:
            registry.admit()
            admitted = True
            result = await registry.run(self._foreground_key(kind, kwargs), context,
                lambda owner: self._execute_foreground(kind, kwargs, owner))
            return result
        except asyncio.CancelledError:
            trace.cancelled = True
            raise
        except (asyncio.TimeoutError, ForegroundQueryTimeout):
            result = {'error': True, 'message': 'indicator_query_timeout'}
            return result
        except (ForegroundQueryError, ForegroundMongoFailure) as exc:
            result = {'error': True, 'message': exc.code}
            return result
        except Exception as exc:
            query_failure('dependency_error', exc)
            result = {'error': True, 'message': 'indicator_unavailable'}
            return result
        finally:
            try:
                if admitted:
                    registry.leave()
                trace.finish(result, registry.snapshot())
            finally:
                end_query_trace(token)

    async def _execute_foreground(self, kind, kwargs, context, *, prepare=True):
        mongo = ForegroundMongoSession(context, self.foreground_mongo_controller or default_controller())
        context.mongo = mongo
        context.force_refresh = bool(kwargs.get('force_refresh', getattr(context, 'force_refresh', False)))
        service = replace(self, query_context=context,
                          kungfu_cache_repo=mongo.bind_repo(self.kungfu_cache_repo),
                          cache_repo=mongo.bind_repo(self.cache_repo),
                          role_identity_repo=mongo.bind_repo(self._role_identity_repo()))
        for name in ('_tuilan_query_locks', '_tuilan_query_locks_guard',
                     '_identity_recovery_tasks', '_identity_recovery_cooldown', '_foreground_token'):
            object.__setattr__(service, name, getattr(self, name))
        ranking = self.ranking_service
        if is_dataclass(ranking) and 'query_context' in {item.name for item in fields(ranking)}:
            ranking = replace(ranking, query_context=context, foreground_client=self.foreground_client)
            object.__setattr__(service, 'ranking_service', ranking)

        async def business():
            if prepare:
                hints = kwargs.get('identity_hints') or kwargs
                server, name = str(kwargs.get('server') or '').strip(), _normalize_name(kwargs.get('name'))
                if not server or not name:
                    return {'error': True, 'message': 'invalid_params'}
                with query_stage("identity"):
                    local = None
                    if kwargs.get('identity_only'):
                        local = await service._resolve_synced_identity_only(server=server, name=name, identity_hints=hints)
                    else:
                        for candidate in role_name_candidates(server, name):
                            local = await _maybe_await(service.kungfu_cache_repo.resolve_role_identity(
                                server=server, name=candidate, **local_lookup_hints(hints), preserve_id=True))
                            if isinstance(local, dict) and local:
                                break
                            local = None
                context.local_identity = local
                if local:
                    if identity_hints_conflict(local, server, hints):
                        return {'error': True, 'message': 'indicator_identity_conflict'}
                    other = foreground_flights().promote(self._foreground_key(kind, kwargs, local), context)
                    if other is not None:
                        return await foreground_flights().wait(other, context)
                method = service._get_role_indicator_entry if kind == 'indicator' else service._get_role_recent_once
            else:
                local = getattr(context, 'local_identity', None)
                if local and not local.get('global_role_id'):
                    # Different force semantics must never share raw. Serialize
                    # partial completion, then re-read facts before issuing this
                    # owner's HTTP; another verified completion may now exist.
                    lock = await service._acquire_query_lock('identity_completion')
                    try:
                        latest = await _maybe_await(service.kungfu_cache_repo.resolve_role_identity(
                            server=local.get('server'), name=local.get('name'),
                            **local_lookup_hints(kwargs.get('identity_hints') or {}), preserve_id=True))
                        context.local_identity = latest if isinstance(latest, dict) and latest else None
                        return await service._resolve_role_identity_once(**kwargs)
                    finally:
                        lock.release()
                method = service._resolve_role_identity_once
            effective = dict(kwargs)
            if prepare and local:
                effective['name'] = local.get('name') or effective.get('name')
            return await method(**effective)

        owner = asyncio.create_task(business())
        owner.add_done_callback(self._consume_foreground_task)
        try:
            context.check()
            result = await asyncio.wait_for(asyncio.shield(owner), timeout=context.remaining())
            context.check()
            return result
        except (asyncio.TimeoutError, ForegroundQueryTimeout):
            return {'error': True, 'message': 'indicator_query_timeout'}
        except (ForegroundQueryError, ForegroundMongoFailure) as exc:
            return {'error': True, 'message': exc.code}
        finally:
            with query_stage("cleanup"):
                cleanup_deadline = time.monotonic() + min(.25, context.cleanup)
                context.cancelled = True
                if not owner.done():
                    owner.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(owner), timeout=max(0.0, cleanup_deadline - time.monotonic()))
                    except (asyncio.TimeoutError, asyncio.CancelledError, ForegroundQueryError, ForegroundMongoFailure):
                        pass
                await mongo.settle(max(0.0, cleanup_deadline - time.monotonic()))

    async def _acquire_query_lock(self, endpoint_key):
        lock = self._get_tuilan_query_lock(endpoint_key)
        context = self.query_context
        if context is None:
            await lock.acquire()
            return lock
        context.check()
        queued_at = time.monotonic()
        try:
            await asyncio.wait_for(lock.acquire(), min(context.queue_wait, context.remaining()))
        except asyncio.TimeoutError:
            context.check()
            raise ForegroundQueryBusy() from None
        finally:
            query_queue(time.monotonic() - queued_at)
        try:
            context.check()
        except BaseException:
            lock.release()
            raise
        return lock

    async def _run_serialized_tuilan_query(
        self, endpoint_key: str, label: str, func: Callable[..., Any],
        *args: Any, **kwargs: Any,
    ) -> Any:
        context = self.query_context
        lock = self._get_tuilan_query_lock(endpoint_key)

        async def acquire():
            await self._acquire_query_lock(endpoint_key)

        if context is not None and self.foreground_client is not None:
            await acquire()
            physical_owners = set()
            def retain(owner):
                physical_owners.add(owner)
                owner.add_done_callback(physical_owners.discard)
                recovery_owners = context.recovery_physical_owners
                recovery_owners.add(owner)
                owner.add_done_callback(recovery_owners.discard)
            try:
                if endpoint_key == "role_indicator":
                    params = {key: kwargs.get(key, args[index] if len(args) > index else None)
                              for index, key in enumerate(("role_id", "zone", "server"))}
                else:
                    params = {key: value for key, value in kwargs.items()
                              if key in {"global_role_id", "size", "cursor", "match_id"}}
                    if args and "match_id" not in params:
                        params["match_id"] = args[0]
                tracked_request = getattr(self.foreground_client, 'request_with_owner', None)
                if callable(tracked_request):
                    result = await tracked_request(endpoint_key, params, deadline=context.deadline, on_owner=retain)
                else:
                    result = await self.foreground_client.request(endpoint_key, params, deadline=context.deadline)
                context.check()
                return result
            finally:
                # The original lease survives frontend cleanup until IO ends.
                release_after_owners(lock.release, physical_owners)

        submitted = asyncio.Event()
        physical_owner = getattr(context, 'recovery_physical_owners', None) if context else None
        async def synchronous_owner():
            await acquire()
            try:
                submitted.set()
                return await asyncio.to_thread(func, *args, **kwargs)
            finally:
                lock.release()
        owner = asyncio.create_task(synchronous_owner())
        if physical_owner is not None:
            physical_owner.add(owner)
            owner.add_done_callback(physical_owner.discard)
        owner.add_done_callback(self._consume_foreground_task)
        try:
            result = await asyncio.shield(owner)
            if context is not None:
                context.check()
            return result
        except asyncio.CancelledError:
            if not submitted.is_set():
                owner.cancel()
            # Submitted synchronous work owns the lock until its real return.
            raise

    async def get_role_recent(self, **kwargs: Any) -> dict[str, Any]:
        return await self._foreground_request("recent", kwargs)

    async def _get_role_recent_once(
        self,
        *,
        server: str,
        name: str,
        identity_hints: Optional[dict[str, Any]] = None,
        cursor: int = 0,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        server, name = str(server or "").strip(), _normalize_name(name)
        if not server or not name:
            return {"error": True, "message": "invalid_params"}
        resolution = await self._resolve_role_identity(
            server=server, name=name, identity_hints=identity_hints or {},
        )
        if resolution.get("error"):
            return resolution
        identity = self._serialize_identity(resolution["identity"])
        name = _pick_str(identity.get("name")) or name
        is_first_page = cursor <= 0
        if is_first_page and not force_refresh:
            cached = await self.cache_repo.load_role_recent(
                server,
                name,
                ttl_seconds=self.role_recent_ttl_seconds,
            )
            cached_data = (cached or {}).get("data") or {}
            same_season = self.season_start_time <= 0 or (
                cached_data.get("current_season") == self.current_season
                and cached_data.get("season_start_time") == self.season_start_time
            )
            cached_identity = cached_data.get("identity")
            same_identity = bool(cached_identity) and isinstance(cached_identity, dict) and self._cached_indicator_matches_identity(
                identity, cached_identity,
            )
            if cached and same_season and same_identity:
                data = copy.deepcopy(cached_data)
                data["identity"] = self._serialize_identity(identity)
                data["player"] = {"server": server, "name": name}
                raw_matches = data.get("recent_matches") or []
                recent_matches = filter_current_season_matches(
                    raw_matches, season_start_time=self.season_start_time,
                )
                # Keep the upstream cursor for valid pages, but stop before old history.
                if _has_previous_season_matches(raw_matches, self.season_start_time):
                    data["pagination"] = {
                        **(data.get("pagination") or {}),
                        "has_more": False, "next_cursor": None,
                    }
                await self._hydrate_recent_matches_with_cached_details(recent_matches)
                data["recent_matches"] = recent_matches
                data["cache"] = {
                    "hit": True,
                    "cached_at": cached.get("cached_at"),
                    "ttl_seconds": self.role_recent_ttl_seconds,
                    "force_refresh": False,
                }
                return data

        payload = await self._build_role_recent_payload(server=server, name=name, identity=identity, cursor=cursor)
        if payload.get("error"):
            return payload
        if is_first_page:
            cached_at = time.time()
            await self.cache_repo.save_role_recent(server, name, {"cached_at": cached_at, "data": copy.deepcopy(payload)})
            await self._hydrate_recent_matches_with_cached_details(
                payload.get("recent_matches") or []
            )
            payload["cache"] = {
                "hit": False,
                "cached_at": cached_at,
                "ttl_seconds": self.role_recent_ttl_seconds,
                "force_refresh": bool(force_refresh),
            }
        else:
            await self._hydrate_recent_matches_with_cached_details(
                payload.get("recent_matches") or []
            )
            payload["cache"] = {"hit": False, "cached_at": time.time(), "force_refresh": bool(force_refresh)}
        return payload

    @staticmethod
    def _cached_indicator_matches_identity(identity: dict[str, Any], cached: dict[str, Any]) -> bool:
        expected = dict(identity)
        query_id = _pick_str(identity.get("game_role_id"), identity.get("role_id"))
        expected["game_role_id"] = query_id
        expected["role_id"] = query_id
        raw = cached.get("raw")
        data = raw.get("data") if isinstance(raw, dict) else None
        info = data.get("role_info") if isinstance(data, dict) else None
        for source in (cached, info if isinstance(info, dict) else {}):
            for key in ("identity_key", "game_role_id", "role_id", "zone", "server", "global_id", "global_role_id"):
                actual = _pick_str(source.get(key))
                wanted = _pick_str(expected.get(key))
                if actual and wanted and actual != wanted:
                    return False
        return True

    async def get_role_indicator(self, **kwargs: Any) -> dict[str, Any]:
        return await self._foreground_request("indicator", kwargs)

    async def _get_role_indicator_entry(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["server"] = str(kwargs.get("server") or "").strip()
        kwargs["name"] = _normalize_name(kwargs.get("name"))
        if not kwargs["server"] or not kwargs["name"]:
            return {"error": True, "message": "invalid_params"}
        if not getattr(cfg, "JJC_ROLE_IDENTITY_RECOVERY_ENABLED", 0):
            return await self._get_role_indicator_once(**kwargs)
        key = (kwargs.get("server"), _normalize_name(kwargs.get("name") or ""),
               bool(kwargs.get("identity_only")),
               *(str(kwargs.get(field) or "") for field in
                 ("game_role_id", "global_role_id", "global_id", "role_id", "zone")))
        cooldown = self._identity_recovery_cooldown.get(key)
        if cooldown and cooldown[0] > time.monotonic():
            return copy.deepcopy(cooldown[1])
        result = await self._get_role_indicator_once(**kwargs)
        genuine = bool(getattr(self.query_context, 'identity_recovery_failed', False))
        if genuine:
            self._identity_recovery_cooldown.pop(key, None)
            self._identity_recovery_cooldown[key] = (time.monotonic() + 180, copy.deepcopy(result))
            while len(self._identity_recovery_cooldown) > 1024:
                self._identity_recovery_cooldown.pop(next(iter(self._identity_recovery_cooldown)))
        return result

    @staticmethod
    def _resolution_packet(result: dict[str, Any], allowed_names: tuple[str, ...], *,
                           snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if result.get("error"):
            return result
        if isinstance(result.get("identity"), dict):
            packet = copy.deepcopy(result)
        else:
            packet = {"identity": copy.deepcopy(result), "identity_source": result.get("source")}
        if snapshot is not None:
            packet["identity_snapshot"] = copy.deepcopy(snapshot)
        packet["allowed_names"] = allowed_names
        return packet

    async def _identity_snapshot_is_current(self, snapshot: dict[str, Any]) -> bool:
        source_id = snapshot.get("_id") or snapshot.get("identity_id")
        if source_id is None:
            return False
        latest = await self._role_identity_repo().get_by_id(source_id)
        return isinstance(latest, dict) and identity_binding(latest) == identity_binding(snapshot)

    @staticmethod
    def _indicator_matches_identity(identity: dict[str, Any], raw: Any, *, role_id: str,
                                    zone: str, server: str, allowed_names: tuple[str, ...],
                                    request_hints: Optional[dict[str, Any]] = None) -> bool:
        if not indicator_matches_request(identity, raw, role_id=role_id, zone=zone, server=server):
            return False
        info = raw["data"]["role_info"]
        actual = {**info, "global_role_id": info.get("global_role_id") or info.get("globalRoleId")}
        query_alias = info.get("role_id") if info.get("role_id") is not None else info.get("roleId")
        if query_alias is not None:
            actual["role_id"] = query_alias
        stable_constraints = {field: (request_hints or {}).get(field)
                              for field in ("global_id", "global_role_id")}
        if (identity_hints_conflict(actual, server, identity)
                or identity_hints_conflict(actual, server, stable_constraints)):
            return False
        actual_name = _pick_str(info.get("name"))
        return not actual_name or actual_name in allowed_names

    def _role_indicator_scope(self) -> Optional[dict[str, Any]]:
        return normalize_indicator_scope({"season_id": self.current_season,
                                          "season_start": self.season_start_time, "pvp_type": 3})

    def _indicator_cache_state(self, cached: dict[str, Any], *, now: Optional[float] = None) -> Optional[str]:
        age = indicator_cache_age(cached.get("cached_at"), time.time() if now is None else now)
        if age is None or not isinstance(cached.get("indicator"), dict) or not cached["indicator"]:
            return None
        expected = self._role_indicator_scope()
        if "scope" in cached:
            scope = normalize_indicator_scope(cached["scope"])
            if scope is None or expected is None or scope != expected:
                return None
        else:
            # Unknown season configuration retains old callers' fresh behavior;
            # legacy records never gain stale eligibility.
            if self.season_start_time > 0 and cached["cached_at"] <= self.season_start_time:
                return None
            return "fresh" if age < self.role_indicator_ttl_seconds else None
        if age < self.role_indicator_ttl_seconds:
            return "fresh"
        if age < self.role_indicator_ttl_seconds + self.role_indicator_stale_grace_seconds:
            return "stale"
        return None

    def _indicator_cache_response(self, identity: dict[str, Any], cached: dict[str, Any], *,
                                  stale: bool, now: float, error_code: Optional[str] = None) -> dict[str, Any]:
        query_cache(format="scoped" if "scope" in cached else "legacy")
        result = {"player": {"server": identity.get("server"), "name": identity.get("name")},
                  "identity": copy.deepcopy(identity), "indicator": copy.deepcopy(cached["indicator"]),
                  "raw": copy.deepcopy(cached["raw"]),
                  "cache": {"hit": True, "cached_at": cached["cached_at"],
                            "ttl_seconds": self.role_indicator_ttl_seconds, "stale": stale,
                            "age_seconds": indicator_cache_age(cached["cached_at"], now)}}
        if stale:
            result["refresh"] = {"state": "failed", "error_code": error_code, "retryable": True}
        return result

    async def _get_role_indicator_once(
        self, *, server: str, name: str, game_role_id: Optional[str] = None,
        global_role_id: Optional[str] = None, global_id: Optional[str] = None,
        role_id: Optional[str] = None, zone: Optional[str] = None,
        force_refresh: bool = False, identity_only: bool = False,
    ) -> dict[str, Any]:
        hints = {"game_role_id": game_role_id, "global_role_id": global_role_id,
                 "global_id": global_id, "role_id": role_id, "zone": zone}
        if identity_only:
            if self.query_context is not None and hasattr(self.query_context, 'local_identity'):
                stored = self.query_context.local_identity
            else:
                stored = await self._resolve_synced_identity_only(server=server, name=name, identity_hints=hints)
            if not stored:
                return {"error": True, "message": "role_identity_not_found",
                        "player": {"server": server, "name": name}}
            if identity_hints_conflict(stored, server, hints):
                return {"error": True, "message": "indicator_identity_conflict"}
            effective = copy.deepcopy(stored)
            query_id = _pick_str(stored.get("game_role_id"), stored.get("role_id"), game_role_id, role_id)
            effective["game_role_id"] = _pick_str(stored.get("game_role_id"), query_id)
            effective["role_id"] = _pick_str(stored.get("role_id"), query_id)
            effective["zone"] = _pick_str(stored.get("zone"), zone)
            canonical_name = _pick_str(stored.get("name")) or name
            resolution = self._resolution_packet(effective, (canonical_name,), snapshot=stored)
        else:
            resolution = await self._resolve_role_identity(server=server, name=name, identity_hints=hints)
        if resolution.get("error"):
            return resolution
        resolved = resolution["identity"]
        snapshot = resolution.get("identity_snapshot")
        # Legacy locators are not canonical identity facts. Recheck the actual
        # repository before cache reuse; never derive a numeric ID from a key.
        if not isinstance(snapshot, dict):
            canonical = await _maybe_await(self.kungfu_cache_repo.resolve_role_identity(
                server=server, name=resolved.get("name") or name,
                **local_lookup_hints(resolved), preserve_id=True,
            ))
            if isinstance(canonical, dict) and canonical:
                if identity_hints_conflict(canonical, server, resolved):
                    return {"error": True, "message": "indicator_identity_conflict"}
                snapshot = copy.deepcopy(canonical)
                resolved = {**resolved, **canonical}
        identity = self._serialize_identity(resolved)
        if resolution.get("identity_source"):
            identity["source"] = resolution["identity_source"]
        name = _pick_str(identity.get("name")) or name
        allowed_names = tuple(resolution.get("allowed_names") or (name,))
        if identity_hints_conflict(identity, server, hints):
            return {"error": True, "message": "indicator_identity_conflict", "identity": identity}
        identity_key = _pick_str(identity.get("identity_key"))
        if not identity_key:
            return {"error": True, "message": "identity_key_missing", "identity": identity}
        query_id, query_zone = _pick_str(identity.get("game_role_id"), identity.get("role_id")), _pick_str(identity.get("zone"))
        stale_cache = None
        stale_cache_key_type = None
        if not force_refresh and isinstance(snapshot, dict):
            keys = [identity_key]
            sk01_key = "global:" + str(snapshot.get("global_role_id") or "")
            if snapshot.get("global_role_id") and sk01_key != identity_key:
                keys.append(sk01_key)
            for key in keys:
                query_cache(key_type="canonical" if key == identity_key else "legacy")
                read_options = {"ttl_seconds": self.role_indicator_ttl_seconds}
                if self._role_indicator_scope() is not None:
                    read_options["max_age_seconds"] = self.role_indicator_ttl_seconds + self.role_indicator_stale_grace_seconds
                cached = await self.cache_repo.load_role_indicator(key, **read_options)
                if not cached or not indicator_binding_matches(snapshot, cached):
                    continue
                if not self._indicator_matches_identity(snapshot, cached.get("raw"), role_id=query_id or "",
                        zone=query_zone or "", server=server, allowed_names=allowed_names, request_hints=hints):
                    continue
                state = self._indicator_cache_state(cached)
                if state == "fresh":
                    if not await self._identity_snapshot_is_current(snapshot):
                        return {"error": True, "message": "indicator_identity_changed", "identity": identity}
                    response_time = time.time()
                    state = self._indicator_cache_state(cached, now=response_time)
                    if state == "fresh":
                        return self._indicator_cache_response(identity, cached, stale=False, now=response_time)
                if state == "stale" and stale_cache is None:
                    stale_cache = copy.deepcopy(cached)
                    stale_cache_key_type = "canonical" if key == identity_key else "legacy"

        async def refresh_failed(error_code: str) -> dict[str, Any]:
            # Re-evaluate age and current binding after the attempted refresh;
            # stale eligibility must not survive a query-ID/season boundary.
            if (stale_cache is not None and self._indicator_cache_state(stale_cache) == "stale"
                    and indicator_binding_matches(snapshot, stale_cache)):
                if not await self._identity_snapshot_is_current(snapshot):
                    return {"error": True, "message": "indicator_identity_changed", "identity": identity}
                response_time = time.time()
                if self._indicator_cache_state(stale_cache, now=response_time) == "stale":
                    query_cache(key_type=stale_cache_key_type)
                    return self._indicator_cache_response(identity, stale_cache, stale=True,
                                                          now=response_time, error_code=error_code)
            return {"error": True, "message": error_code, "identity": identity}

        raw = None
        completed_request = resolution.get("indicator_request") or {}
        completion_age = time.monotonic() - completed_request.get("requested_at", float("-inf"))
        if (0 <= completion_age < self.resolution_indicator_max_age_seconds and completed_request.get("role_id") == query_id
                and completed_request.get("zone") == query_zone and completed_request.get("server") == server
                and self._indicator_matches_identity(identity, resolution.get("indicator_raw"), role_id=query_id,
                    zone=query_zone, server=server, allowed_names=allowed_names, request_hints=hints)):
            raw = copy.deepcopy(resolution["indicator_raw"])
        recovery_enabled = bool(getattr(cfg, "JJC_ROLE_IDENTITY_RECOVERY_ENABLED", 0) and identity_only and snapshot)
        if raw is None and query_id and query_zone and not recovery_enabled:
            try:
                raw = await self._run_serialized_tuilan_query(
                    "role_indicator", f"role_indicator:{server}:{name}", self.role_indicator_fetcher,
                    query_id, query_zone, server, tuilan_request=self.tuilan_request, rank=None, name=name,
                )
            except ForegroundQueryBusy:
                return await refresh_failed('indicator_query_busy')
            except (ForegroundQueryError, ForegroundMongoFailure, asyncio.TimeoutError):
                raise
            except Exception as exc:
                logger.warning("JJC indicator transport failed: {}", type(exc).__name__)
                return await refresh_failed("indicator_unavailable")
        if recovery_enabled and raw is None:
            try:
                loader = getattr(self.kungfu_cache_repo, "load_query_id_recovery_candidates", None)
                try:
                    candidates = await loader(snapshot) if callable(loader) else []
                except Exception as exc:
                    logger.warning("JJC recovery locators unavailable: {}", exc)
                    candidates = []
                constraint_mismatches = []
                async def fetch_indicator(**query: Any) -> Any:
                    fetched = await self._run_serialized_tuilan_query(
                        'role_indicator', 'foreground_recovery', self.role_indicator_fetcher,
                        query['role_id'], query['zone'], query['server'],
                        tuilan_request=self.tuilan_request, rank=None, name=name)
                    # A missing/invalid current locator must still reach the
                    # existing bounded recovery alternatives. Only contradictory
                    # original stable hints stop the recovery before publication.
                    data = fetched.get("data") if isinstance(fetched, dict) else None
                    info = data.get("role_info") if isinstance(data, dict) else None
                    if isinstance(info, dict):
                        actual = {**info, "global_role_id": info.get("global_role_id") or info.get("globalRoleId")}
                        constraints = {field: hints.get(field) for field in ("global_id", "global_role_id")}
                        if identity_hints_conflict(actual, query["server"], constraints):
                            constraint_mismatches.append(True)
                            # This locator supplies no usable identity evidence;
                            # it is not a transport failure. The existing bounded
                            # candidate/replay loop must still be allowed to run.
                            return {"code": 0, "data": {}}
                        alias = info.get("roleId")
                        if (info.get("role_id") is None and alias is not None
                                and str(alias) != str(query["role_id"])):
                            # Expose a contradictory alias to the legacy guard so
                            # it rejects this locator before any CAS. Valid raw
                            # responses keep their original representation.
                            fetched = copy.deepcopy(fetched)
                            fetched["data"]["role_info"]["role_id"] = alias
                    return fetched
                async def fetch_history(**query):
                    return await self._run_serialized_tuilan_query(
                        'match_history', 'foreground_recovery', self.match_history_client.get_mine_match_history,
                        **query, size=20, cursor=0)

                async def fetch_replay(**query):
                    if self.match_replay_client is None:
                        return {}
                    return await self._run_serialized_tuilan_query(
                        'match_replay', 'foreground_recovery', self.match_replay_client.get_match_replay, **query)

                # A sync fallback owner retains both its endpoint lock and the
                # recovery permit until the physical thread returns.
                for callback in (fetch_indicator, fetch_history, fetch_replay):
                    callback.retain_physical_owner = self.foreground_client is None
                    callback.physical_owners = (self.query_context.recovery_physical_owners
                                                if self.query_context is not None else set())
                with query_stage("recovery"):
                    recovery = await recover_query_ids(
                        snapshot, identity_repo=self._role_identity_repo(), candidates=candidates,
                        indicator_fetcher=fetch_indicator,
                        replay_fetcher=fetch_replay, history_fetcher=fetch_history, publish=True,
                        deadline_seconds=min(15.0, self.query_context.remaining()) if self.query_context else 15.0,
                    )
                if self.query_context is not None:
                    self.query_context.check()
                if recovery.get('error_code'):
                    if recovery['error_code'] == 'indicator_query_busy':
                        return await refresh_failed('indicator_query_busy')
                    raise ForegroundQueryTimeout()
                if recovery.get('status') in {'unresolved', 'conflict'}:
                    if self.query_context is not None:
                        self.query_context.identity_recovery_failed = True
                    return {'error': True, 'message': 'indicator_identity_conflict', 'identity': identity}
                if recovery.get("status") == "busy":
                    return await refresh_failed('indicator_query_busy')
                if recovery.get("status") in {"healthy", "recovered"}:
                    snapshot = copy.deepcopy(recovery["identity"])
                    identity = self._serialize_identity(snapshot)
                    query_id, query_zone = identity.get("game_role_id"), identity.get("zone")
                    raw = recovery["raw"]
            except (ForegroundQueryError, ForegroundMongoFailure):
                raise
            except Exception as exc:
                logger.warning('JJC identity recovery failed: {}', type(exc).__name__)
        if not query_id or not query_zone:
            return {"error": True, "message": "indicator_params_missing", "identity": identity}
        if not isinstance(raw, dict) or raw.get("error"):
            return await refresh_failed("indicator_unavailable")
        parsed = _parse_3v3_indicator(raw)
        if parsed.get("error"):
            failure = await refresh_failed(parsed.get("error") or "indicator_parse_failed")
            if failure.get("error"):
                failure["raw"] = raw
            return failure
        try:
            lock = await self._acquire_query_lock('indicator_publication')
        except ForegroundQueryBusy:
            return await refresh_failed('indicator_query_busy')
        try:
            if not self._indicator_matches_identity(identity, raw, role_id=query_id, zone=query_zone,
                                                    server=server, allowed_names=allowed_names, request_hints=hints):
                return {"error": True, "message": "indicator_identity_conflict", "identity": identity}
            # Check before the ordinary upsert: it must not overwrite query IDs that
            # a separately verified recovery changed while this HTTP call was in flight.
            if snapshot and not await self._identity_snapshot_is_current(snapshot):
                return {"error": True, "message": "indicator_identity_changed", "identity": identity}
            info = raw["data"]["role_info"]
            actual_name = _pick_str(info.get("name")) or name
            person_id = _extract_person_id_from_indicator(raw)
            if not resolution.get("identity_published"):
                try:
                    updated = await _maybe_await(self.kungfu_cache_repo.upsert_role_identity_from_indicator(
                        server=server, name=actual_name, zone=query_zone, game_role_id=query_id,
                        global_role_id=_pick_str(info.get("global_role_id"), info.get("globalRoleId"), identity.get("global_role_id")),
                        role_id=_pick_str(info.get("role_id"), info.get("roleId"), query_id),
                        person_id=person_id, global_id=_pick_str((snapshot or {}).get("global_id")), preserve_id=True,
                    ))
                except Exception as exc:
                    logger.warning("JJC indicator identity write failed: {}", exc)
                    return {"error": True, "message": "indicator_storage_unavailable", "identity": identity}
                if not isinstance(updated, dict) or not updated:
                    return {"error": True, "message": "indicator_storage_unavailable", "identity": identity}
                snapshot = copy.deepcopy(updated)
            if not snapshot or not self._indicator_matches_identity(snapshot, raw, role_id=query_id,
                        zone=query_zone, server=server,
                        allowed_names=(_pick_str(snapshot.get("name")) or name,), request_hints=hints):
                return {"error": True, "message": "indicator_identity_changed", "identity": identity}
            identity = self._serialize_identity(snapshot)
            identity_key = _pick_str(snapshot.get("identity_key"))
            name = _pick_str(snapshot.get("name")) or actual_name
            cached_at = time.time()
            payload = {**identity_binding(snapshot), "person_id": person_id,
                       "indicator": parsed, "raw": copy.deepcopy(raw), "cached_at": cached_at}
            query_cache(key_type="canonical")
            scope = self._role_indicator_scope()
            if scope is not None:
                payload["scope"] = scope
            if not identity_key or not indicator_binding_matches(snapshot, payload):
                return {"error": True, "message": "indicator_identity_changed", "identity": identity}
            if not await self._identity_snapshot_is_current(snapshot):
                return {"error": True, "message": "indicator_identity_changed", "identity": identity}
            try:
                await self.cache_repo.save_role_indicator(identity_key, payload, strict=True)
                saved = await self.cache_repo.load_role_indicator(identity_key,
                    ttl_seconds=self.role_indicator_ttl_seconds, strict=True)
                if not await self._identity_snapshot_is_current(snapshot):
                    return {"error": True, "message": "indicator_identity_changed", "identity": identity}
                if (not saved or saved.get("cached_at") != cached_at or saved.get("raw") != raw
                        or not indicator_binding_matches(snapshot, saved)):
                    return {"error": True, "message": "indicator_cache_write_failed", "identity": identity}
            except Exception as exc:
                logger.warning("JJC indicator cache publication failed: {}", exc)
                return {"error": True, "message": "indicator_cache_write_failed", "identity": identity}
            return {"player": {"server": server, "name": name}, "identity": identity,
                    "indicator": parsed, "raw": raw,
                    "cache": {"hit": False, "cached_at": cached_at, "ttl_seconds": self.role_indicator_ttl_seconds,
                              "force_refresh": bool(force_refresh), "stale": False, "age_seconds": indicator_cache_age(cached_at, time.time())}}

        finally:
            lock.release()

    @observed_query_stage("identity")
    async def _resolve_role_identity(self, *, server, name, identity_hints):
        if self.query_context is None:
            return await self._resolve_role_identity_once(server=server, name=name, identity_hints=identity_hints)
        kwargs = dict(server=server, name=name, identity_hints=identity_hints,
                      force_refresh=getattr(self.query_context, 'force_refresh', False))
        local = getattr(self.query_context, 'local_identity', None)
        key = self._foreground_key('identity', kwargs, local)
        async def resolve(context):
            context.force_refresh = getattr(self.query_context, 'force_refresh', False)
            context.local_identity = copy.deepcopy(local)
            return await self._execute_foreground('identity',
                dict(server=server, name=name, identity_hints=identity_hints), context, prepare=False)
        return await foreground_flights().run(key, self.query_context, resolve)

    async def _resolve_role_identity_once(
        self,
        *,
        server: str,
        name: str,
        identity_hints: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve an exact stored name before one display-suffix candidate.

        Repository fallback after an unknown hint is not proof that the hint
        belongs to the named role; compare every nonempty hint before reuse.
        """
        server, name = str(server or "").strip(), _normalize_name(name)
        candidates = role_name_candidates(server, name)
        for candidate in candidates:
            if self.query_context is not None and hasattr(self.query_context, 'local_identity'):
                identity = self.query_context.local_identity if candidate == candidates[0] else None
            else:
                identity = await _maybe_await(self.kungfu_cache_repo.resolve_role_identity(
                    server=server, name=candidate,
                    **local_lookup_hints(identity_hints), preserve_id=True,
                ))
            if not isinstance(identity, dict) or not identity:
                continue
            if identity_hints_conflict(identity, server, identity_hints):
                return {"error": True, "message": "indicator_identity_conflict"}
            stored_identity = copy.deepcopy(identity)
            # Hints may fill missing query locators only after known fields
            # agree. Keep this request-local; raw identity validation still
            # precedes publication, and stored strong IDs remain authoritative.
            identity = dict(identity)
            query_id = _pick_str(
                identity.get("game_role_id"), identity.get("role_id"),
                identity_hints.get("game_role_id"), identity_hints.get("role_id"),
            )
            identity["game_role_id"] = _pick_str(identity.get("game_role_id"), query_id)
            identity["role_id"] = _pick_str(identity.get("role_id"), query_id)
            identity["zone"] = _pick_str(identity.get("zone"), identity_hints.get("zone"))
            canonical = _pick_str(identity.get("name")) or candidate
            if _pick_str(identity.get("global_role_id")):
                result = self._serialize_identity(identity)
                result.update(server=server, name=canonical, source="role_identity_name_match")
                return self._resolution_packet(result, (canonical,), snapshot=stored_identity)
            # The name is now confirmed; helpers must not strip it again.
            confirmed_hints = {
                key: value for key, value in identity.items()
                if key in ("global_id", "global_role_id", "game_role_id", "role_id", "zone") and value
            }
            confirmed_hints.update({key: value for key, value in identity_hints.items() if value})
            query_id = _pick_str(identity.get("game_role_id"), identity.get("role_id"))
            query_zone = _pick_str(identity.get("zone"))
            result = None
            if query_id and query_zone:
                result = await self._resolve_identity_from_indicator(
                    server=server, name=canonical, game_role_id=query_id, zone=query_zone,
                    role_id=_pick_str(identity.get("role_id")),
                    global_id=_pick_str(identity.get("global_id")), source="role_identity_indicator",
                    allowed_names=(canonical,), identity_hints=confirmed_hints,
                    identity_snapshot=stored_identity,
                )
            if result is None:
                # A known identity is never replaced by another name after IO fails.
                return {"error": True, "message": "indicator_unavailable",
                        "identity": self._serialize_identity(identity)}
            return self._resolution_packet(result, (canonical,))
        result = await self._resolve_role_identity_fallback(
            server=server, name=name, identity_hints=identity_hints,
            allowed_names=candidates,
        )
        return self._resolution_packet(result, candidates)

    async def _resolve_role_identity_fallback(
        self, *, server: str, name: str, identity_hints: dict[str, Any],
        allowed_names: tuple[str, ...],
    ) -> dict[str, Any]:
        """Discover a missing locator; never repeat local lookup or name inference."""
        hint_global_id = _pick_str(identity_hints.get("global_id"))
        hint_sk01 = _pick_str(identity_hints.get("global_role_id"))
        hint_id = _pick_str(identity_hints.get("game_role_id"), identity_hints.get("role_id"))
        hint_zone = _pick_str(identity_hints.get("zone"))
        if hint_sk01:
            # History can use an explicit SK01 without a query ID. Indicator IO
            # validates the actual role_info before any publication.
            return {"server": server, "name": name, "global_role_id": hint_sk01,
                    "global_id": hint_global_id, "role_id": hint_id,
                    "game_role_id": hint_id, "zone": hint_zone,
                    "source": "detail_hint_global_role_id",
                    "identity_key": f"global_id:{hint_global_id}" if hint_global_id else f"global:{hint_sk01}"}
        if hint_id and hint_zone:
            result = await self._resolve_identity_from_indicator(
                server=server, name=name, game_role_id=hint_id, zone=hint_zone,
                role_id=hint_id, global_id=hint_global_id, source="detail_hint_game_role_id",
                allowed_names=allowed_names, identity_hints=identity_hints,
            )
            if result is not None:
                return result
            # An explicit failed locator is not permission to try another role.
            return {"error": True, "message": "role_identity_not_found"}

        for candidate in allowed_names:
            cached = await _maybe_await(self.kungfu_cache_repo.load_new_kungfu_cache_raw(
                server=server, name=candidate,
            ))
            if not isinstance(cached, dict) or not cached:
                continue
            if identity_hints_conflict(cached, server, identity_hints):
                return {"error": True, "message": "indicator_identity_conflict"}
            cached_name = _pick_str(cached.get("name")) or candidate
            if cached_name not in allowed_names:
                continue
            sk01 = _pick_str(cached.get("global_role_id"))
            query_id = _pick_str(cached.get("game_role_id"), cached.get("role_id"))
            query_zone = _pick_str(cached.get("zone"))
            numeric = hint_global_id  # request constraint, never a cache-derived identity fact
            if sk01:
                return {"server": server, "name": cached_name, "global_role_id": sk01,
                        "global_id": numeric, "role_id": query_id, "game_role_id": query_id,
                        "zone": query_zone, "source": "new_cache_global_role_id",
                        "identity_key": cached.get("identity_key") or f"global:{sk01}"}
            if query_id and query_zone:
                result = await self._resolve_identity_from_indicator(
                    server=server, name=cached_name, game_role_id=query_id, zone=query_zone,
                    role_id=query_id, global_id=numeric, source="new_cache_game_role_id",
                    allowed_names=allowed_names, identity_hints=identity_hints,
                )
                if result is not None:
                    return result

        lock = await self._acquire_query_lock('live_ranking')
        try:
            ranking = await self.ranking_service.query_jjc_ranking_raw()
        finally:
            owners = self.query_context.recovery_physical_owners if self.query_context else ()
            release_after_owners(lock.release, owners)
        if not ranking.get("error") and ranking.get("code") == 0:
            for canonical, person in ranking_name_matches(
                server, name, ranking.get("data", []), allowed_names=allowed_names,
            ):
                sk01 = _pick_str(person.get("globalRoleId"))
                query_id = _pick_str(person.get("gameRoleId"))
                query_zone = _pick_str(person.get("zone"))
                numeric = _pick_str(person.get("globalId"))
                locator = {"server": server, "zone": query_zone, "game_role_id": query_id,
                           "role_id": query_id, "global_role_id": sk01, "global_id": numeric}
                if identity_hints_conflict(locator, server, identity_hints):
                    continue
                if sk01:
                    return {**locator, "name": canonical, "source": "live_ranking_global_role_id",
                            "identity_key": f"global_id:{numeric}" if numeric else f"global:{sk01}"}
                if query_id and query_zone:
                    result = await self._resolve_identity_from_indicator(
                        server=server, name=canonical, game_role_id=query_id, zone=query_zone,
                        role_id=query_id, global_id=numeric, source="live_ranking_game_role_id",
                        allowed_names=(canonical,), identity_hints=identity_hints,
                    )
                    if result is not None:
                        return result
                    break
        return {"error": True, "message": "role_identity_not_found", "server": server, "name": name}

    async def _resolve_identity_from_indicator(
        self,
        *,
        server: str,
        name: str,
        game_role_id: str,
        zone: str,
        role_id: Optional[str],
        global_id: Optional[str],
        source: str,
        allowed_names: Optional[tuple[str, ...]] = None,
        identity_hints: Optional[dict[str, Any]] = None,
        identity_snapshot: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        logger.info(
            "JJC 角色标识解析: 调用 indicator 补全标识 server={} name={} game_role_id={} zone={} source={}",
            server,
            name,
            game_role_id,
            zone,
            source,
        )
        requested_at = time.monotonic()
        result = await self._run_serialized_tuilan_query(
            "role_indicator",
            f"role_indicator:{server}:{name}",
            self.role_indicator_fetcher,
            game_role_id,
            zone,
            server,
            tuilan_request=self.tuilan_request,
            rank=None,
            name=name,
        )
        if not isinstance(result, dict):
            return None
        if result.get("error") or result.get("code") not in (0, None):
            return None
        data = result.get("data")
        role_info = data.get("role_info") if isinstance(data, dict) else None
        if not isinstance(role_info, dict):
            return None
        resolved_name = _pick_str(role_info.get("name")) or name
        if resolved_name not in (allowed_names if allowed_names is not None else (name,)):
            return None
        global_role_id = _pick_str(role_info.get("global_role_id"), role_info.get("globalRoleId"))
        resolved_role_id = _pick_str(role_info.get("role_id"), role_info.get("roleId"), role_id, game_role_id)
        wanted = {**(identity_hints or {}), "game_role_id": game_role_id,
                  "role_id": role_id or game_role_id, "zone": zone}
        actual = {**role_info, "role_id": resolved_role_id, "global_role_id": global_role_id}
        if identity_hints_conflict(actual, server, wanted):
            return None
        person_id = _extract_person_id_from_indicator(result)
        if not global_role_id:
            logger.warning(
                "JJC 角色标识解析: indicator 未返回 global_role_id server={} name={} source={}",
                server,
                name,
                source,
            )
            return None
        if identity_snapshot and not await self._identity_snapshot_is_current(identity_snapshot):
            return {"error": True, "message": "indicator_identity_changed"}
        # Neither an old profile cache nor a caller hint is numeric identity
        # proof. Recheck independently using the physically validated locator/SK01.
        authoritative = await _maybe_await(self.kungfu_cache_repo.resolve_role_identity(
            server=server, name=resolved_name, zone=zone, game_role_id=game_role_id,
            global_role_id=global_role_id, preserve_id=True,
        ))
        established_numeric = None
        if isinstance(authoritative, dict) and authoritative:
            locator = {"zone": zone, "game_role_id": game_role_id,
                       "role_id": resolved_role_id, "global_role_id": global_role_id}
            if (identity_hints_conflict(authoritative, server, locator)
                    or identity_hints_conflict(authoritative, server, identity_hints or {})):
                return {"error": True, "message": "indicator_identity_conflict"}
            established_numeric = _pick_str(authoritative.get("global_id"))
        try:
            updated = await _maybe_await(self.kungfu_cache_repo.upsert_role_identity_from_indicator(
                server=server, name=resolved_name, zone=zone, game_role_id=game_role_id,
                global_role_id=global_role_id, role_id=resolved_role_id, person_id=person_id,
                global_id=established_numeric, preserve_id=True,
            ))
        except Exception as exc:
            logger.warning("JJC identity completion write failed: {}", exc)
            return {"error": True, "message": "indicator_storage_unavailable"}
        if not isinstance(updated, dict) or not updated:
            return {"error": True, "message": "indicator_storage_unavailable"}
        if (identity_hints_conflict(updated, server, wanted)
                or identity_hints_conflict(actual, server, updated)
                or _pick_str(updated.get("name")) != resolved_name):
            return {"error": True, "message": "indicator_identity_changed"}
        return {
            "identity": copy.deepcopy(updated), "identity_snapshot": copy.deepcopy(updated),
            "indicator_raw": copy.deepcopy(result),
            "indicator_request": {"role_id": game_role_id, "zone": zone, "server": server,
                                  "requested_at": requested_at},
            "identity_source": source, "identity_published": True,
        }

    async def _build_role_recent_payload(self, *, server: str, name: str, identity: dict[str, Any], cursor: int = 0) -> dict[str, Any]:
        global_role_id = _pick_str(identity.get("global_role_id"))
        if not global_role_id:
            return {"error": True, "message": "global_role_id_missing", "identity": identity}

        raw = await self._run_serialized_tuilan_query(
            "match_history",
            f"match_history:{server}:{name}",
            self.match_history_client.get_mine_match_history,
            global_role_id=global_role_id,
            size=self.max_recent_matches,
            cursor=cursor,
        )
        if not isinstance(raw, dict):
            return {"error": True, "message": "invalid_response", "identity": identity}
        if raw.get("error"):
            return {"error": True, "message": raw.get("error"), "identity": identity}
        if raw.get("code") != 0 or raw.get("msg") != "success":
            return {"error": True, "message": raw.get("msg") or "unknown_error", "identity": identity, "raw": raw}

        history = raw.get("data") or []
        first_raw_match = next((item for item in history if isinstance(item, dict)), None)
        recent_matches = normalize_recent_matches(
            history,
            kungfu_pinyin_to_chinese=self.kungfu_pinyin_to_chinese,
            max_recent_matches=self.max_recent_matches,
            season_start_time=self.season_start_time,
        )
        missing_match_id_count = sum(1 for item in recent_matches if not item.get("match_id"))
        missing_avg_grade_count = sum(1 for item in recent_matches if item.get("avg_grade") is None)
        if not _pick_str(identity.get("global_id")):
            first_match_id = next(
                (
                    _coerce_int(item.get("match_id"))
                    for item in recent_matches
                    if isinstance(item, dict) and _coerce_int(item.get("match_id")) is not None
                ),
                None,
            )
            if first_match_id is not None:
                global_id = await self._resolve_global_id_from_match(
                    match_id=first_match_id,
                    identity=identity,
                    server=server,
                    name=name,
                )
                if global_id:
                    identity = dict(identity)
                    identity["global_id"] = global_id
                    identity["identity_hints"] = {
                        **(identity.get("identity_hints") or {}),
                        "global_id": global_id,
                        "source_match_id": first_match_id,
                    }

        logger.info(
            "JJC 角色近期构建完成: server={} name={} history_total={} recent_total={} missing_match_id_count={} missing_avg_grade_count={} identity_source={}",
            server,
            name,
            sum(1 for item in history if isinstance(item, dict)),
            len(recent_matches),
            missing_match_id_count,
            missing_avg_grade_count,
            identity.get("source"),
        )
        if recent_matches:
            logger.info(
                "JJC 推栏战局历史样本字段: server={} name={} first_keys={} first_id_like_fields={} first_grade_like_fields={}",
                server,
                name,
                sorted(first_raw_match.keys()) if isinstance(first_raw_match, dict) else [],
                _extract_id_like_fields(first_raw_match) if isinstance(first_raw_match, dict) else {},
                _extract_grade_like_fields(first_raw_match) if isinstance(first_raw_match, dict) else {},
            )
        if missing_avg_grade_count > 0:
            missing_avg_grade_samples = []
            for item in raw.get("data") or []:
                if not isinstance(item, dict):
                    continue
                parsed_avg_grade = _extract_avg_grade(item)
                if parsed_avg_grade is not None:
                    continue
                missing_avg_grade_samples.append(
                    {
                        "keys": sorted(item.keys()),
                        "grade_like_fields": _extract_grade_like_fields(item),
                        "time": item.get("match_time") or item.get("startTime") or item.get("start_time"),
                        "kungfu": item.get("kungfu") or item.get("kungfu_name"),
                        "won": item.get("won"),
                    }
                )
                if len(missing_avg_grade_samples) >= 3:
                    break
            logger.warning(
                "JJC 角色近期存在缺失段位字段样本: server={} name={} samples={}",
                server,
                name,
                missing_avg_grade_samples,
            )
        if missing_match_id_count > 0:
            missing_match_id_samples = []
            for item in raw.get("data") or []:
                if not isinstance(item, dict):
                    continue
                if _extract_match_id(item) is not None:
                    continue
                missing_match_id_samples.append(
                    {
                        "keys": sorted(item.keys()),
                        "id_like_fields": _extract_id_like_fields(item),
                        "time": item.get("match_time") or item.get("startTime") or item.get("start_time"),
                        "kungfu": item.get("kungfu") or item.get("kungfu_name"),
                        "won": item.get("won"),
                    }
                )
                if len(missing_match_id_samples) >= 3:
                    break
            logger.warning(
                "JJC 角色近期存在缺失对局ID样本: server={} name={} samples={}",
                server,
                name,
                missing_match_id_samples,
            )

        return {
            "current_season": self.current_season,
            "season_start_time": self.season_start_time,
            "player": {"server": server, "name": name},
            "identity": identity,
            "identity_key": identity.get("identity_key"),
            "pagination": recent_matches_pagination(
                history, cursor=cursor, page_size=self.max_recent_matches,
                season_start_time=self.season_start_time,
            ),
            "recent_matches": recent_matches,
        }

    async def _hydrate_recent_matches_with_cached_details(
        self,
        recent_matches: list,
    ) -> None:
        if not recent_matches:
            return
        match_ids = []
        for item in recent_matches:
            if not isinstance(item, dict):
                continue
            mid = _coerce_int(item.get("match_id"))
            if mid:
                match_ids.append(mid)
        if not match_ids:
            for item in recent_matches:
                if isinstance(item, dict):
                    item.pop("cached_detail_summary", None)
            return
        summaries = await self.cache_repo.batch_load_cached_detail_summaries(match_ids)
        for item in recent_matches:
            if not isinstance(item, dict):
                continue
            mid = _coerce_int(item.get("match_id"))
            if mid is not None and mid in summaries:
                item["cached_detail_summary"] = summaries[mid]
            else:
                item.pop("cached_detail_summary", None)

    async def get_match_detail(self, *, match_id: Union[int, str]) -> dict[str, Any]:
        normalized_match_id = _coerce_int(match_id)
        if normalized_match_id is None:
            return {"error": True, "message": "invalid_match_id"}

        cached = await self.cache_repo.load_match_detail(normalized_match_id)
        if cached:
            data = dict(cached.get("data") or {})
            enrich_task = asyncio.create_task(
                self._enrich_cached_match_detail_replay_and_project(
                    match_id=normalized_match_id,
                    cached_at=cached.get("cached_at"),
                    data=copy.deepcopy(data),
                )
            )
            try:
                enrich_result = await asyncio.wait_for(
                    asyncio.shield(enrich_task),
                    timeout=self.cache_hit_replay_enrich_timeout_seconds,
                )
                data = enrich_result["data"]
                replay_enrich_pending = False
            except asyncio.TimeoutError:
                enrich_task.add_done_callback(self._log_background_task_exception)
                replay_enrich_pending = True
            data["cache"] = {
                "hit": True,
                "cached_at": cached.get("cached_at"),
                "replay_enrich_pending": replay_enrich_pending,
            }
            return data

        detail = await self._run_serialized_tuilan_query(
            "match_detail",
            f"match_detail:{normalized_match_id}",
            self.match_detail_client.get_match_detail_obj,
            match_id=normalized_match_id,
        )
        if not isinstance(detail, MatchDetailResponse):
            return {"error": True, "message": "invalid_response"}
        if detail.code == -1 and detail.msg.strip() == "no data found" and detail.data is None:
            payload: dict[str, Any] = {
                "match_id": normalized_match_id,
                "unavailable": True,
                "code": -1,
                "message": "no data found",
                "detail": None,
            }
            cached_at = time.time()
            await self._persist_match_detail(
                match_id=normalized_match_id,
                payload={"cached_at": cached_at, "data": payload},
                source="inspect_cache_miss_unavailable",
            )
            payload["cache"] = {"hit": False, "cached_at": cached_at}
            return payload
        if detail.code != 0 or not detail.data:
            return {"error": True, "message": detail.msg or "unknown_error", "code": detail.code}

        payload = {
            "match_id": normalized_match_id,
            "detail": asdict(detail.data),
        }
        for team_key in ("team1", "team2"):
            team = payload["detail"].get(team_key)
            if not isinstance(team, dict):
                continue
            players = team.get("players_info") or []
            if not isinstance(players, list):
                continue
            for player in players:
                if not isinstance(player, dict):
                    continue
                player["kungfu"] = self._translate_kungfu_name(player.get("kungfu"))
        await self._enrich_detail_payload_with_replay(payload)
        cached_at = time.time()
        persistence_result = await self._persist_match_detail(
            match_id=normalized_match_id,
            payload={"cached_at": cached_at, "data": payload},
            source="inspect_cache_miss",
        )
        if getattr(persistence_result, "identity_error", None):
            payload["projection_error"] = True
            payload["projection_message"] = persistence_result.identity_error
        payload["cache"] = {"hit": False, "cached_at": cached_at}
        return payload

    async def _persist_match_detail(
        self,
        *,
        match_id: int,
        payload: Dict[str, Any],
        source: str,
    ) -> Any:
        service = self.match_detail_persistence_service
        if service is None:
            logger.warning("JJC match persistence service unavailable; fact projection skipped: match_id={} source={}", match_id, source)
            service = MatchDetailPersistenceService(
                inspect_repo=self.cache_repo,
                identity_projector=self.match_detail_projection_service,
                participant_projector=self.match_detail_participant_projection_service,
                fact_projector=None,
            )
        result = await service.persist(match_id, copy.deepcopy(payload), source)
        if getattr(result, "main_saved", True) is not True:
            logger.warning("JJC 对局详情主保存未成功: match_id={} source={}", match_id, source)
        return result
