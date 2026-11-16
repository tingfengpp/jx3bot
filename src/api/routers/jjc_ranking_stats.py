from __future__ import annotations

import asyncio
import time
import math
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query
from starlette.requests import Request
from starlette.responses import JSONResponse
from nonebot import logger

import config as cfg
from config import KUNGFU_META
from src.api.response import error_response, success_response
from src.services.jx3.singletons import jjc_ranking_inspect_service
from src.services.jx3.jjc_peak_score_ranking import JjcPeakScoreRankingService
from src.services.jx3.jjc_ranking_display import DisplayRequestError, public_display_member
from src.storage.mongo_repos.jjc_peak_score_ranking_repo import JjcPeakScoreRankingRepo
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo, EquipmentSnapshotError
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_participant_repo import JjcMatchParticipantRepo


router = APIRouter(prefix="/api/jjc", tags=["jjc"])


_RANGE_LIMITS = {
    "top_1000": 1000,
    "top_200": 200,
    "top_100": 100,
    "top_50": 50,
}

_HEALER_KUNGFU_NAMES = {
    str(meta.get("name"))
    for meta in KUNGFU_META.values()
    if isinstance(meta, dict) and meta.get("category") == "healer" and meta.get("name")
}



def _equipment_enabled() -> bool:
    return getattr(cfg, "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED", 0) == 1


def _equipment_overlay_enabled() -> bool:
    return getattr(cfg, "JJC_RANKING_DISPLAY_OVERLAY_ENABLED", 0) == 1


def _equipment_service():
    # Legacy import harnesses and rollback code may not expose this optional
    # assembly. Never import the application launcher or create a service here.
    try:
        from src.services.jx3.singletons import jjc_ranking_equipment_service
    except ImportError:
        return None
    return jjc_ranking_equipment_service


def _equipment_timestamp(value: Any) -> Optional[int]:
    if (not isinstance(value, str) or not value or len(value) > 19
            or not value.isascii() or not value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= 9223372036854775807 else None


def _equipment_contract(value: Any) -> bool:
    return isinstance(value, str) and value == "2"


def _equipment_failure(code: str, *, data: Optional[Dict[str, Any]] = None):
    if code in ("equipment_contract_upgrade_required", "version_mismatch"):
        status = 409
    elif code in ("invalid_params", "invalid_timestamp", "invalid_range", "invalid_lane"):
        status = 400
    elif code == "not_found":
        status = 404
    else:
        code = "busy" if code == "busy" else "read_failed"
        status = 503
    return JSONResponse(status_code=status, content=error_response(code, data=data))


def _public_revocations(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    answer = []
    fields = ("timestamp", "member_revision", "binding_revision", "rank", "confirmed_at", "reason")
    for value in values[:1000]:
        if not isinstance(value, dict) or value.get("reason") not in ("binding_invalid", "data_inconsistent", "no_local_win"):
            continue
        item = {k: value[k] for k in fields if k in value
                and (value[k] is None or type(value[k]) in (str, int, float, bool))
                and (type(value[k]) is not float or math.isfinite(value[k]))}
        scope = value.get("scope")
        item["scope"] = {k: scope[k] for k in ("season_id", "season_start", "pvp_type", "algorithm_version")
                         if isinstance(scope, dict) and k in scope
                         and type(scope[k]) in (str, int, float, bool)
                         and (type(scope[k]) is not float or math.isfinite(scope[k]))}
        answer.append(item)
    return answer


def _snapshot_read_repo():
    if not _equipment_enabled():
        return JjcRankingStatsRepo()
    service = _equipment_service()
    selector = getattr(service, "snapshot_read_repo", None)
    if not callable(selector):
        raise EquipmentSnapshotError("read_failed")
    return selector()


async def _equipment_mode(repo, timestamp: int, summary=None) -> bool:
    """Reject cross-protocol reads, including already persisted V2 when off."""
    if _equipment_enabled():
        reader = _snapshot_read_repo()
    else:
        service = _equipment_service()
        reader = getattr(service, "stats_repo", repo) if service is not None else repo
    if summary is None:
        loader = getattr(reader, "load_summary", None)
        if not callable(loader):
            return False
        summary = await loader(timestamp)
    if not isinstance(summary, dict):
        return False
    if summary.get("schema_version") == 2 or summary.get("equipment_source") == "local_win_v2":
        return True
    if not _equipment_enabled() or summary.get("ranking_display_kind") != "current":
        return False
    scope = summary.get("kungfu_cache_scope")
    latest_reader = getattr(reader, "get_latest_current_summary", None)
    if not isinstance(scope, dict) or not scope.get("season_id") or not callable(latest_reader):
        return False
    latest = await latest_reader(scope)
    return bool(latest and latest.get("timestamp") == timestamp)


async def _equipment_frozen(timestamp: int, expected_view_revision=None):
    service = _equipment_service()
    if service is None:
        return None, _equipment_failure("read_failed")
    try:
        payload = await service.read_frozen_snapshot(timestamp, expected_view_revision=expected_view_revision)
        return payload, None
    except Exception as exc:
        logger.warning("JJC frozen equipment read failed: exception_type={}", type(exc).__name__)
        return None, _equipment_failure(getattr(exc, "code", "read_failed"))


def _build_peak_kungfu_statistics(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    lanes: Dict[str, Dict[str, Any]] = {
        "healer": {"distribution": {}, "valid_count": 0, "min_score": None},
        "dps": {"distribution": {}, "valid_count": 0, "min_score": None},
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        kungfu = str(item.get("kungfu") or "").strip()
        if not kungfu:
            kungfu = "未知心法"
        lane_key = "healer" if kungfu in _HEALER_KUNGFU_NAMES else "dps"
        lane = lanes[lane_key]
        lane["distribution"][kungfu] = int(lane["distribution"].get(kungfu, 0)) + 1
        lane["valid_count"] += 1
        score = item.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            lane["min_score"] = score if lane["min_score"] is None else min(lane["min_score"], score)

    for lane in lanes.values():
        lane["list"] = sorted(lane["distribution"].items(), key=lambda pair: pair[1], reverse=True)
        lane["legendary_count_map"] = {}
        if lane["min_score"] is None:
            lane["min_score"] = "-"
    return lanes


async def _load_peak_score_doc(
    *,
    timestamp: str,
    score_type: str,
    range_key: str,
    version: int,
) -> Optional[Dict[str, Any]]:
    limit = _RANGE_LIMITS.get(range_key)
    if limit is None:
        return None
    try:
        return await JjcPeakScoreRankingService(
            ranking_stats_repo=JjcRankingStatsRepo(),
            participant_repo=JjcMatchParticipantRepo(),
            peak_repo=JjcPeakScoreRankingRepo(suppress_errors=False),
        ).load_for_anchor(
            anchor_timestamp=int(timestamp), score_type=score_type,
            version=version, max_items=limit,
        )
    except Exception as exc:
        logger.warning("JJC peak-score season read failed: anchor={} score_type={} error={}",
                       timestamp, score_type, type(exc).__name__)
        return None


async def _read_snapshot_metadata(action, season, page, page_size, with_meta):
    if not _equipment_enabled():
        return await _read_snapshot_metadata_selected(action, season, page, page_size, with_meta)
    try:
        return await _read_snapshot_metadata_selected(action, season, page, page_size, with_meta)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("JJC optional snapshot metadata read failed: exception_type={}", type(exc).__name__)
        raise EquipmentSnapshotError("read_failed") from None


async def _read_snapshot_metadata_selected(action, season, page, page_size, with_meta):
    """Preserve legacy metadata formats; strict V2 read failures reach HTTP 503."""
    if action == "seasons":
        return success_response(await _snapshot_read_repo().list_seasons())

    if action == "season-history":
        normalized_season = str(season or "").strip()
        if not normalized_season:
            return error_response("invalid_season")
        return success_response(
            await _snapshot_read_repo().list_season_history(normalized_season)
        )

    if action == "list":
        if with_meta:
            normalized_page = page or 1
            normalized_page_size = page_size or 100
            mongo_result = await _snapshot_read_repo().list_timestamps(
                page=normalized_page,
                page_size=normalized_page_size,
                with_meta=True,
            )
            if isinstance(mongo_result, dict):
                logger.info(
                    "JJC ranking stats list with_meta: page={} page_size={} total={}".format(
                        mongo_result.get("page"),
                        mongo_result.get("page_size"),
                        mongo_result.get("total"),
                    )
                )
                return success_response(mongo_result)
            logger.info(
                "JJC ranking stats list with_meta 返回异常，返回空分页"
            )
            return success_response({
                "items": [],
                "page": normalized_page,
                "page_size": normalized_page_size,
                "total": 0,
                "has_more": False,
            })

        if page is not None or page_size is not None:
            normalized_page = page or 1
            normalized_page_size = page_size or 20
            mongo_result = await _snapshot_read_repo().list_timestamps(
                page=normalized_page,
                page_size=normalized_page_size,
            )
            if isinstance(mongo_result, dict):
                logger.info(
                    "JJC ranking stats list 读取 Mongo: page={} page_size={} total={}".format(
                        mongo_result.get("page"),
                        mongo_result.get("page_size"),
                        mongo_result.get("total"),
                    )
                )
                return success_response(mongo_result)
            logger.info(
                "JJC ranking stats list Mongo 读取异常，返回空分页: page={} page_size={}".format(
                    normalized_page,
                    normalized_page_size,
                )
            )
            return success_response({
                "items": [],
                "page": normalized_page,
                "page_size": normalized_page_size,
                "total": 0,
                "has_more": False,
            })

        mongo_timestamps = await _snapshot_read_repo().list_timestamps()
        if not mongo_timestamps:
            logger.info("JJC ranking stats list 未命中数据")
            return error_response("not_found")
        logger.info(
            "JJC ranking stats list 读取 Mongo: mongo_count={}".format(
                len(mongo_timestamps or []),
            )
        )
        return success_response(mongo_timestamps)


@router.get("/ranking-stats")
async def get_ranking_stats(
    action: str = Query("list", description="list、seasons、season-history 或 read"),
    timestamp: Optional[str] = Query(None, description="read 模式下的时间戳"),
    season: Optional[str] = Query(None, description="season-history 模式下的赛季名称"),
    page: Optional[int] = Query(None, ge=1, description="list 模式分页页码"),
    page_size: Optional[int] = Query(None, ge=1, le=100, description="list 模式分页大小"),
    with_meta: bool = Query(False, description="list 模式返回元数据列表"),
    equipment_contract: Optional[str] = Query(None),
) -> Any:
    action = action.strip().lower()
    if action in ("seasons", "season-history", "list"):
        try:
            return await _read_snapshot_metadata(action, season, page, page_size, with_meta)
        except Exception as exc:
            if getattr(exc, "code", None) == "read_failed":
                logger.warning("JJC snapshot metadata read failed: exception_type={}", type(exc).__name__)
                return _equipment_failure("read_failed")
            raise

    if action == "read":
        if not timestamp or not timestamp.isdigit():
            return error_response("invalid_timestamp")

        if _equipment_contract(equipment_contract):
            stamp = _equipment_timestamp(timestamp)
            if stamp is None:
                return _equipment_failure("invalid_timestamp")
            payload, failure = await _equipment_frozen(stamp)
            if failure is not None:
                return failure
            return success_response(payload) if payload is not None else _equipment_failure("not_found")
        try:
            repo = _snapshot_read_repo()
            mongo_payload = await repo.load_summary(int(timestamp))
            if mongo_payload is not None and await _equipment_mode(repo, int(timestamp), mongo_payload):
                return _equipment_failure("equipment_contract_upgrade_required")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("JJC equipment protocol check failed: exception_type={}", type(exc).__name__)
            return _equipment_failure("read_failed")
        if mongo_payload is not None:
            logger.info("JJC ranking stats read 命中 Mongo: timestamp={}".format(timestamp))
            return success_response(mongo_payload)

        logger.info("JJC ranking stats read Mongo 未命中: timestamp={}".format(timestamp))
        return error_response("not_found")

    return error_response("invalid_action")


@router.get("/ranking-stats/details")
async def get_ranking_stats_details(
    timestamp: str = Query(..., description="统计时间戳"),
    range_key: str = Query(..., alias="range", description="排名范围，如 top_200"),
    lane: str = Query(..., description="healer 或 dps"),
    kungfu: str = Query(..., description="心法名称"),
    equipment_contract: Optional[str] = Query(None),
    expected_view_revision: Optional[str] = Query(None),
) -> Any:
    if _equipment_contract(equipment_contract) and _equipment_timestamp(timestamp) is None:
        return _equipment_failure("invalid_timestamp")
    if not timestamp.isdigit():
        return error_response("invalid_timestamp")

    lane = lane.strip().lower()
    if lane not in {"healer", "dps"} and not (_equipment_contract(equipment_contract) and lane == "unknown"):
        return error_response("invalid_lane")

    kungfu = kungfu.strip()
    range_key = range_key.strip()
    if not kungfu or not range_key:
        return error_response("invalid_params")

    repo = JjcRankingStatsRepo()
    try:
        equipment_mode = await _equipment_mode(repo, int(timestamp))
    except Exception as exc:
        logger.warning("JJC equipment detail protocol check failed: exception_type={}", type(exc).__name__)
        return _equipment_failure("read_failed")
    if equipment_mode:
        if not _equipment_contract(equipment_contract):
            return _equipment_failure("equipment_contract_upgrade_required")
        if range_key not in _RANGE_LIMITS:
            return _equipment_failure("invalid_range")
        if not isinstance(expected_view_revision, str) or not expected_view_revision:
            return _equipment_failure("version_mismatch")
        payload, failure = await _equipment_frozen(int(timestamp), expected_view_revision)
        if failure is not None:
            return failure
        view = (payload or {}).get("equipment_view")
        if not isinstance(view, dict):
            return _equipment_failure("version_mismatch")
        members = [m for m in view["members"] if m["rank"] <= _RANGE_LIMITS[range_key]
                   and m.get("lane") == lane
                   and ((m.get("kungfu") or "未知") == kungfu
                        or (lane == "unknown" and kungfu == "unknown"))]
        return success_response({"timestamp": int(timestamp), "range": range_key, "lane": lane,
            "kungfu": kungfu, "equipment_view_revision": view["equipment_view_revision"],
            "members": members})
    mongo_payload = await repo.load_detail(int(timestamp), range_key, lane, kungfu)
    if mongo_payload is not None:
        logger.info(
            "JJC ranking stats detail 命中 Mongo: timestamp={} range={} lane={} kungfu={}".format(
                timestamp,
                range_key,
                lane,
                kungfu,
            )
        )
        return success_response({**mongo_payload, "members": [public_display_member(member)
            for member in mongo_payload.get("members", [])]})

    logger.info(
        "JJC ranking stats detail Mongo 未命中: timestamp={} range={} lane={} kungfu={}".format(
            timestamp,
            range_key,
            lane,
            kungfu,
        )
    )
    return error_response("not_found")


@router.get("/ranking-stats/flat-members")
async def get_ranking_stats_flat_members(
    timestamp: str = Query(..., description="统计时间戳"),
    range_key: str = Query("top_1000", alias="range", description="排名范围"),
    equipment_contract: Optional[str] = Query(None),
    expected_view_revision: Optional[str] = Query(None),
) -> Any:
    started_at = time.perf_counter()
    if _equipment_contract(equipment_contract) and _equipment_timestamp(timestamp) is None:
        return _equipment_failure("invalid_timestamp")
    if not timestamp.isdigit():
        return error_response("invalid_timestamp")
    range_key = range_key.strip()
    limit = _RANGE_LIMITS.get(range_key)
    if limit is None:
        return error_response("invalid_range")

    logger.info(
        "JJC flat-members request start: timestamp={} range={} limit={}".format(
            timestamp,
            range_key,
            limit,
        )
    )
    repo = JjcRankingStatsRepo()
    try:
        equipment_mode = await _equipment_mode(repo, int(timestamp))
    except Exception as exc:
        logger.warning("JJC equipment flat protocol check failed: exception_type={}", type(exc).__name__)
        return _equipment_failure("read_failed")
    if equipment_mode:
        if not _equipment_contract(equipment_contract):
            return _equipment_failure("equipment_contract_upgrade_required")
        if not isinstance(expected_view_revision, str) or not expected_view_revision:
            return _equipment_failure("version_mismatch")
        payload, failure = await _equipment_frozen(int(timestamp), expected_view_revision)
        if failure is not None:
            return failure
        view = (payload or {}).get("equipment_view")
        if not isinstance(view, dict):
            return _equipment_failure("version_mismatch")
        members = [m for m in view["members"] if m["rank"] <= limit]
        return success_response({"timestamp": int(timestamp), "range": range_key,
            "equipment_view_revision": view["equipment_view_revision"], "items": members,
            "total": len(members), "item_count": len(members)})
    result = await repo.list_flat_members(int(timestamp), range_key, limit=limit)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    logger.info(
        "JJC flat-members request done: timestamp={} range={} limit={} elapsed_ms={} item_count={} total={} detail_count={}".format(
            timestamp,
            range_key,
            limit,
            elapsed_ms,
            result.get("item_count"),
            result.get("total"),
            result.get("detail_count"),
        )
    )
    if not result.get("items"):
        return error_response("not_found", data=result)
    return success_response({**result, "items": [public_display_member(member) for member in result["items"]]})


@router.get("/ranking-stats/peak-score")
async def get_jjc_peak_score_ranking(
    timestamp: str = Query(..., description="统计锚点时间戳"),
    score_type: str = Query(..., description="tuilan 或 game"),
    range_key: str = Query("top_1000", alias="range", description="排名范围"),
    version: int = Query(1, ge=1, description="统计口径版本"),
    include_match_summary: bool = Query(True, description="是否补充最高分对局心法摘要"),
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    if not timestamp.isdigit():
        return error_response("invalid_timestamp")
    score_type = score_type.strip().lower()
    if score_type not in {"tuilan", "game"}:
        return error_response("invalid_score_type")
    range_key = range_key.strip()
    limit = _RANGE_LIMITS.get(range_key)
    if limit is None:
        return error_response("invalid_range")

    logger.info(
            "JJC peak-score request start: timestamp={} score_type={} range={} version={} limit={} include_match_summary={}".format(
                timestamp,
                score_type,
                range_key,
                version,
                limit,
                include_match_summary,
            )
    )
    load_started_at = time.perf_counter()
    doc = await _load_peak_score_doc(
        timestamp=timestamp, score_type=score_type, range_key=range_key, version=version,
    )
    load_elapsed_ms = int((time.perf_counter() - load_started_at) * 1000)
    if not doc or doc.get("status") != "done":
        return error_response("not_found")

    items = doc.get("items") or []
    if not isinstance(items, list):
        items = []
    payload = dict(doc)
    payload["range"] = range_key
    payload["items"] = items[:limit]
    payload["item_count"] = len(payload["items"])
    payload["total"] = int(doc.get("item_count") or len(items))
    payload["kungfu_statistics"] = _build_peak_kungfu_statistics(payload["items"])

    match_ids = []
    for item in payload["items"]:
        if isinstance(item, dict):
            match_id = item.get("match_id")
            if isinstance(match_id, int):
                match_ids.append(match_id)
    summaries: Dict[int, Dict[str, Any]] = {}
    if include_match_summary and match_ids:
        summary_started_at = time.perf_counter()
        summary_timed_out = False
        try:
            summaries = await asyncio.wait_for(
                JjcInspectRepo().batch_load_cached_detail_summaries(match_ids),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            summary_timed_out = True
            summaries = {}
        summary_elapsed_ms = int((time.perf_counter() - summary_started_at) * 1000)
        if summary_timed_out:
            logger.info(
                "JJC peak-score summary load timeout: timestamp={} score_type={} range={} match_ids={} elapsed_ms={}".format(
                    timestamp,
                    score_type,
                    range_key,
                    len(match_ids),
                    summary_elapsed_ms,
                )
            )
        for item in payload["items"]:
            if not isinstance(item, dict):
                continue
            match_id = item.get("match_id")
            if isinstance(match_id, int) and match_id in summaries:
                item["cached_detail_summary"] = summaries[match_id]
    else:
        summary_elapsed_ms = 0
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    logger.info(
        "JJC peak-score request done: timestamp={} score_type={} range={} version={} include_match_summary={} elapsed_ms={} load_elapsed_ms={} summary_elapsed_ms={} item_count={} total={} match_ids={} summaries={}".format(
            timestamp,
            score_type,
            range_key,
            version,
            include_match_summary,
            elapsed_ms,
            load_elapsed_ms,
            summary_elapsed_ms,
            payload["item_count"],
            payload["total"],
            len(match_ids),
            len(summaries) if match_ids else 0,
        )
    )
    return success_response(payload)


@router.get("/ranking-stats/peak-score/details")
async def get_jjc_peak_score_details(
    timestamp: str = Query(..., description="统计锚点时间戳"),
    score_type: str = Query(..., description="tuilan 或 game"),
    range_key: str = Query("top_1000", alias="range", description="排名范围"),
    kungfu: str = Query(..., description="心法名称"),
    version: int = Query(1, ge=1, description="统计口径版本"),
) -> Dict[str, Any]:
    if not timestamp.isdigit():
        return error_response("invalid_timestamp")
    score_type = score_type.strip().lower()
    if score_type not in {"tuilan", "game"}:
        return error_response("invalid_score_type")
    range_key = range_key.strip()
    limit = _RANGE_LIMITS.get(range_key)
    if limit is None:
        return error_response("invalid_range")
    kungfu = kungfu.strip()
    if not kungfu:
        return error_response("invalid_params")

    doc = await _load_peak_score_doc(
        timestamp=timestamp,
        score_type=score_type,
        range_key=range_key,
        version=version,
    )
    if not doc or doc.get("status") != "done":
        return error_response("not_found")

    raw_items = doc.get("items") or []
    items = raw_items if isinstance(raw_items, list) else []
    members: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_kungfu = str(item.get("kungfu") or "").strip()
        if item_kungfu != kungfu:
            continue
        member = dict(item)
        if not member.get("name"):
            member["name"] = member.get("role_name")
        members.append(member)

    payload = {
        "timestamp": int(timestamp),
        "range": range_key,
        "lane": "dps",
        "kungfu": kungfu,
        "score_type": score_type,
        "version": version,
        "members": members,
    }
    return success_response(payload)


@router.get("/ranking-stats/role-recent")
async def get_ranking_stats_role_recent(
    server: str = Query(..., description="服务器名"),
    name: str = Query(..., description="角色名"),
    game_role_id: Optional[str] = Query(None, description="榜单角色 ID"),
    global_role_id: Optional[str] = Query(None, description="推栏全局角色 ID"),
    global_id: Optional[str] = Query(None, description="推栏 replay 数字全局角色 ID"),
    role_id: Optional[str] = Query(None, description="推栏角色 ID"),
    zone: Optional[str] = Query(None, description="区服分区"),
    cursor: int = Query(0, ge=0, le=2000, description="分页游标，0 表示第一页"),
    force_refresh: bool = Query(False, description="是否绕过缓存实时刷新第一页"),
) -> dict[str, Any]:
    server = server.strip()
    name = name.strip()
    if not server or not name:
        return error_response("invalid_params")

    result = await jjc_ranking_inspect_service.get_role_recent(
        server=server,
        name=name,
        cursor=cursor,
        identity_hints={
            "game_role_id": (game_role_id or "").strip() or None,
            "global_role_id": (global_role_id or "").strip() or None,
            "global_id": (global_id or "").strip() or None,
            "role_id": (role_id or "").strip() or None,
            "zone": (zone or "").strip() or None,
        },
        force_refresh=force_refresh,
    )
    if result.get("error"):
        return error_response(result.get("message") or result.get("error") or "unknown_error", data=result)
    return success_response(result)


@router.get("/ranking-stats/role-indicator")
async def get_ranking_stats_role_indicator(
    server: str = Query(..., description="服务器名"),
    name: str = Query(..., description="角色名"),
    game_role_id: Optional[str] = Query(None, description="榜单角色 ID"),
    global_role_id: Optional[str] = Query(None, description="推栏全局角色 ID"),
    global_id: Optional[str] = Query(None, description="推栏 replay 数字全局角色 ID"),
    role_id: Optional[str] = Query(None, description="推栏角色 ID"),
    zone: Optional[str] = Query(None, description="区服分区"),
    force_refresh: bool = Query(False, description="是否绕过缓存实时刷新"),
    identity_only: bool = Query(False, description="是否只从本地 role_identities 解析身份"),
) -> dict[str, Any]:
    server = server.strip()
    name = name.strip()
    if not server or not name:
        return error_response("invalid_params", data={"error": True, "message": "invalid_params"})

    result = await jjc_ranking_inspect_service.get_role_indicator(
        server=server,
        name=name,
        game_role_id=(game_role_id or "").strip() or None,
        global_role_id=(global_role_id or "").strip() or None,
        global_id=(global_id or "").strip() or None,
        role_id=(role_id or "").strip() or None,
        zone=(zone or "").strip() or None,
        force_refresh=force_refresh,
        identity_only=identity_only,
    )
    if result.get("error"):
        return error_response(result.get("message") or result.get("error") or "unknown_error", data=result)
    return success_response(result)


@router.get("/ranking-stats/synced-role")
async def get_ranking_stats_synced_role(
    server: str = Query("", description="服务器名；为空时返回全服候选"),
    name: str = Query(..., description="角色名"),
) -> dict[str, Any]:
    server = server.strip()
    name = name.strip()
    if not name:
        return error_response("invalid_params")

    result = await jjc_ranking_inspect_service.resolve_synced_role(server=server, name=name)
    if result.get("error"):
        return error_response(result.get("message") or "unknown_error", data=result)
    return success_response(result)


@router.get("/ranking-stats/synced-role-matches")
async def get_ranking_stats_synced_role_matches(
    server: str = Query(..., description="服务器名"),
    name: str = Query(..., description="角色名"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(20, ge=1, le=100, description="分页大小"),
) -> dict[str, Any]:
    started_at = time.perf_counter()
    server = server.strip()
    name = name.strip()
    if not server or not name:
        return error_response("invalid_params")

    result = await jjc_ranking_inspect_service.get_synced_role_matches(
        server=server,
        name=name,
        page=page,
        page_size=page_size,
    )
    import config as cfg
    result["refresh"] = {"state": "disabled", "enabled": False}
    result["latest_matches"] = []
    identity_id = (result.get("identity") or {}).get("identity_id")
    if identity_id and not result.get("error"):
        from src.services.jx3.singletons import jjc_live_refresh_service
        try:
            result.update(await jjc_live_refresh_service.read(identity_id))
        except Exception:
            enabled = bool(getattr(cfg, "JJC_LIVE_REFRESH_ENABLED", 0))
            result["refresh"] = {"state": "storage_unavailable" if enabled else "disabled", "enabled": enabled}
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    logger.info(
        "JJC 对局查询 API 完成: endpoint=synced-role-matches server={} name={} page={} page_size={} "
        "elapsed_ms={} error={} total={} items={}".format(
            server,
            name,
            page,
            page_size,
            elapsed_ms,
            bool(result.get("error")),
            ((result.get("pagination") or {}).get("total") if isinstance(result.get("pagination"), dict) else None),
            len(result.get("recent_matches") or []) if isinstance(result.get("recent_matches"), list) else None,
        )
    )
    if result.get("error"):
        return error_response(result.get("message") or "unknown_error", data=result)
    return success_response(result)


@router.post("/ranking-stats/synced-role-refresh")
async def post_ranking_stats_synced_role_refresh(
    request: Request,
    payload: Dict[str, Any] = Body(...),
):
    import config as cfg
    from src.services.jx3.singletons import jjc_live_refresh_service
    from src.services.jx3.jjc_public_refresh import submit_refresh
    server, name = payload.get("server"), payload.get("name")
    if not isinstance(server, str) or not isinstance(name, str):
        return JSONResponse(error_response("invalid_params"), status_code=400)
    result, status = await submit_refresh(jjc_live_refresh_service,
        server=server.strip(), name=name.strip(),
        peer=request.client.host if request.client else "",
        forwarded=request.headers.get("x-forwarded-for", ""),
        trusted_proxies=getattr(cfg, "JJC_LIVE_TRUSTED_PROXIES", ""),
        secret=getattr(cfg, "JJC_LIVE_SOURCE_SECRET", ""))
    headers = {}
    if status in (429, 503):
        headers["Retry-After"] = str(result.get("retry_after_seconds", 60))
    return JSONResponse(success_response(result), status_code=status, headers=headers)


@router.post("/ranking-stats/synced-role-sync")
async def post_ranking_stats_synced_role_sync(
    payload: Optional[Dict[str, Any]] = Body(None),
    server: Optional[str] = Query(None, description="服务器名"),
    name: Optional[str] = Query(None, description="角色名"),
) -> dict[str, Any]:
    body = payload or {}
    server_text = (server or body.get("server") or "").strip()
    name_text = (name or body.get("name") or "").strip()
    if not server_text or not name_text:
        return error_response("invalid_params")

    result = await jjc_ranking_inspect_service.enqueue_synced_role(
        server=server_text,
        name=name_text,
    )
    if result.get("error"):
        return error_response(result.get("message") or "unknown_error", data=result)
    return success_response(result)


@router.get("/ranking-stats/match-detail")
async def get_ranking_stats_match_detail(
    match_id: str = Query(..., description="对局 ID"),
) -> dict[str, Any]:
    started_at = time.perf_counter()
    result = await jjc_ranking_inspect_service.get_match_detail(match_id=match_id)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    cache = result.get("cache") if isinstance(result, dict) else None
    logger.info(
        "JJC 对局查询 API 完成: endpoint=match-detail match_id={} elapsed_ms={} error={} cache_hit={}".format(
            match_id,
            elapsed_ms,
            bool(result.get("error")) if isinstance(result, dict) else None,
            cache.get("hit") if isinstance(cache, dict) else None,
        )
    )
    if result.get("error"):
        return error_response(result.get("message") or result.get("error") or "unknown_error", data=result)
    return success_response(result)


@router.get("/ranking-stats/current-displays")
async def get_ranking_stats_current_displays(
    timestamp: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    ranks: Optional[str] = Query(None),
) -> Dict[str, Any]:
    if (not timestamp or len(timestamp) > 20 or not timestamp.isascii() or not timestamp.isdigit() or int(timestamp) <= 0
            or range_key not in _RANGE_LIMITS or not ranks or len(ranks) > 4096):
        return error_response("invalid_params")
    parts = ranks.split(",")
    if any(not part.isascii() or not part.isdigit() or len(part) > 10 or int(part) <= 0 for part in parts):
        return error_response("invalid_ranks")
    requested = list(dict.fromkeys(int(part) for part in parts))
    if len(requested) > 100 or any(rank > _RANGE_LIMITS[range_key] for rank in requested):
        return error_response("invalid_ranks")
    # Lazy assembly keeps route import independent of optional singleton setup.
    from src.services.jx3.singletons import jjc_ranking_display_service
    try:
        service = _equipment_service()
        if service is not None:
            if not _equipment_overlay_enabled():
                return success_response({"timestamp": int(timestamp), "range": range_key,
                    "overlay_enabled": False, "items": [], "reason": "disabled"})
            if await _equipment_mode(JjcRankingStatsRepo(), int(timestamp)):
                return success_response({"timestamp": int(timestamp), "range": range_key,
                    "overlay_enabled": False, "items": [], "reason": "equipment_contract_upgrade_required"})
        return success_response(await jjc_ranking_display_service.read(int(timestamp), range_key, requested))
    except DisplayRequestError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.warning("JJC ranking display read failed: exception_type={}", type(exc).__name__)
        return error_response("read_failed")


@router.get("/ranking-stats/equipment-view")
async def get_ranking_stats_equipment_view(
    request: Request,
    timestamp: Optional[str] = Query(None),
    equipment_contract: Optional[str] = Query(None),
    expected_member_revision: Optional[str] = Query(None),
    expected_frozen_source: Optional[str] = Query(None),
) -> Any:
    allowed = {"timestamp", "equipment_contract", "expected_member_revision", "expected_frozen_source"}
    pairs = list(request.query_params.multi_items())
    stamp = _equipment_timestamp(timestamp)
    if (set(request.query_params) - allowed or len(pairs) != len(set(request.query_params))
            or stamp is None or not _equipment_contract(equipment_contract)
            or not isinstance(expected_member_revision, str) or not 0 < len(expected_member_revision) <= 128
            or not expected_member_revision.isascii()
            or any(not (ch.isalnum() or ch in "._:-") for ch in expected_member_revision)
            or expected_frozen_source not in ("legacy_v1", "local_win_v2")):
        return _equipment_failure("invalid_params")
    # This gate precedes even optional service assembly and all repositories.
    if not _equipment_overlay_enabled() or not _equipment_enabled():
        return success_response({"timestamp": stamp, "overlay_enabled": False,
            "retired": False, "reload_required": False, "reason": "disabled", "view": None})
    service = _equipment_service()
    if service is None:
        return _equipment_failure("read_failed")
    try:
        result = await service.read_dynamic_view(timestamp=stamp,
            expected_member_revision=expected_member_revision, expected_frozen_source=expected_frozen_source)
        return success_response(result)
    except Exception as exc:
        logger.warning("JJC whole-board equipment read failed: exception_type={}", type(exc).__name__)
        instructions = _public_revocations(getattr(exc, "revocations", []))
        return _equipment_failure(getattr(exc, "code", "read_failed"),
                                  data={"revocations": instructions} if instructions else None)
