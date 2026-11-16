from __future__ import annotations

import asyncio
import copy
import inspect
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.services.jx3.match_replay import MatchReplayClient
from src.services.jx3.match_detail_persistence import MatchDetailPersistenceService
from src.services.jx3.role_indicator import RoleIndicatorClient
from src.services.jx3.tuilan_rate_limit import random_sleep
from src.services.jx3.role_identity_matching import has_verified_query_ids
from src.services.jx3.role_identity_recovery import recover_query_ids
import config as cfg
from src.storage.mongo_repos.jjc_sync_repo import JjcSyncRepo

from nonebot import logger


_AUTH_ERROR_KEYWORDS = (
    "ticket",
    "token",
    "unauthorized",
    "forbidden",
    "permission",
    "auth",
    "login",
    "无权限",
    "未授权",
    "认证",
    "鉴权",
    "登录",
    "过期",
    "失效",
)
_WORKER_HEARTBEAT_TTL_SECONDS = 300
_IDENTITY_INDICATOR_REFRESH_SECONDS = 86400
_QUEUE_MODE_FULL = "full"
_SUPPORTED_QUEUE_MODES = (_QUEUE_MODE_FULL,)
_WORKER_COMPAT_MODES = ("incremental_or_full", "full", "incremental")
_AUTO_DISPATCHER_SOURCE = "auto_dispatcher"
_AUTO_DISPATCHER_SYNC_WINDOW_DAYS = 14


class JjcSyncGlobalPauseError(RuntimeError):
    """同步应全局暂停的错误，例如推栏 ticket 失效或无权限。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JjcSyncStaleLeaseError(RuntimeError):
    """当前 worker 已失去角色租约，应停止写回角色状态。"""


class JjcSyncStaleRoleLeaseError(JjcSyncStaleLeaseError):
    """当前 worker 已失去角色租约。"""


class JjcSyncStaleMatchDetailLeaseError(JjcSyncStaleLeaseError):
    """当前 worker 已失去对局详情租约。"""


class JjcSyncMatchDetailClaimError(RuntimeError):
    """对局详情领取失败，应中断当前角色并等待后续重试。"""


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


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _stringify_error_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        parts: List[str] = []
        for key in ("error", "message", "msg", "status_msg", "code", "status_code"):
            value = payload.get(key)
            if value is not None:
                parts.append(str(value))
        return " ".join(parts)
    return str(payload or "")


def is_tuilan_auth_error(payload: Any) -> bool:
    """判断推栏响应是否属于需要全局暂停的鉴权类错误。"""
    if isinstance(payload, dict):
        code = _coerce_int(payload.get("code"))
        status_code = _coerce_int(payload.get("status_code"))
        if code in (401, 403) or status_code in (401, 403):
            return True
    text = _stringify_error_payload(payload).lower()
    if not text:
        return False
    return any(keyword in text for keyword in _AUTH_ERROR_KEYWORDS)


def build_tuilan_auth_pause_reason(payload: Any, context: str = "") -> str:
    message = _stringify_error_payload(payload).strip() or "unknown_auth_error"
    if len(message) > 180:
        message = message[:180] + "..."
    return "推栏鉴权失败{}: {}".format(
        "（{}）".format(context) if context else "",
        message,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def is_supported_queue_mode(mode: str) -> bool:
    return mode in _SUPPORTED_QUEUE_MODES


def parse_queue_sync_until_time(value: Any) -> Optional[int]:
    """Parse a one-time queue sync cutoff timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_queue_sync_until_time")
    if isinstance(value, (int, float)):
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("invalid_queue_sync_until_time")
        return parsed
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        parsed = int(text)
        if parsed <= 0:
            raise ValueError("invalid_queue_sync_until_time")
        return parsed
    normalized = text.replace("T", " ")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed_dt = datetime.strptime(normalized, fmt)
            return int(parsed_dt.timestamp())
        except ValueError:
            pass
    try:
        parsed_dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("invalid_queue_sync_until_time") from exc
    return int(parsed_dt.timestamp())


def resolve_queue_sync_until_time(
    *,
    days: Optional[int] = None,
    until: Any = None,
    now: Optional[float] = None,
) -> Optional[int]:
    """Resolve one-time queue sync window from days or an explicit cutoff."""
    if days is not None and until not in (None, ""):
        raise ValueError("queue_sync_window_conflict")
    if days is not None:
        if days < 1:
            raise ValueError("invalid_queue_sync_days")
        base = time.time() if now is None else now
        return int(base - days * 86400)
    return parse_queue_sync_until_time(until)


def build_identity_key(
    *,
    global_id: Optional[str] = None,
    global_role_id: Optional[str] = None,
    zone: Optional[str] = None,
    role_id: Optional[str] = None,
    server: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    """构建角色身份键。

    优先级:
      1. global_id:{global_id}
      2. global:{global_role_id}
      3. game:{zone}:{role_id}
      4. name:{server}:{name}

    Raises:
        ValueError: 无法从提供的参数构建任何有效 key。
    """
    if global_id:
        return f"global_id:{global_id}"
    if global_role_id:
        return f"global:{global_role_id}"
    if zone and role_id:
        return f"game:{zone}:{role_id}"
    if server and name:
        return f"name:{server}:{name}"
    raise ValueError(
        "无法构建 identity_key：至少需要 global_id、global_role_id，或 zone+role_id，或 server+name"
    )


def extract_match_id_from_history(item: dict) -> Optional[int]:
    """从历史 item 中提取 match_id。

    依次尝试键名: match_id, matchId, matchID, id。
    """
    for key in ("match_id", "matchId", "matchID", "id"):
        value = _coerce_int(item.get(key))
        if value is not None:
            return value
    return None


def extract_match_time_from_history(item: dict) -> Optional[int]:
    """从历史 item 中提取 match_time（Unix 秒）。

    依次尝试键名: match_time, matchTime, start_time, startTime, time。
    """
    return _coerce_int(
        item.get("match_time")
        or item.get("matchTime")
        or item.get("start_time")
        or item.get("startTime")
        or item.get("time")
    )


def extract_pvp_type_from_history(item: dict) -> Optional[int]:
    """从历史 item 中提取 pvp_type。

    依次尝试键名: pvpType, pvp_type, type。
    3 表示 3v3。
    """
    return _coerce_int(
        item.get("pvpType")
        or item.get("pvp_type")
        or item.get("type")
    )


def normalize_match_detail_role_name(role_name: Any, server: Any) -> str:
    """去掉角色名中末尾的服务器后缀。

    规则：先 strip。如果不含 · 则直接返回；如果包含则只按最后一个 · 分割。
    若分割后右侧 trim 后等于 server trim 后且左侧非空，返回 trim 后的左侧；
    否则保持原 trim 后的角色名。
    """
    name = str(role_name or "").strip()
    server_name = str(server or "").strip()
    if not name or "·" not in name:
        return name

    left, right = name.rsplit("·", 1)
    left = left.strip()
    right = right.strip()
    if left and server_name and right == server_name:
        return left
    return name


def normalize_role_name(role_name: Any, server: Any) -> str:
    """兼容别名，统一复用对局详情角色名规范化逻辑。"""
    return normalize_match_detail_role_name(role_name, server)


def split_replay_role_name(role_name: Any, server: Any = "") -> Tuple[str, str]:
    """从 replay 玩家名中解析 (server, name)。

    replay 实测通常把服务器拼在 role_name 里，格式为 `角色名·服务器`，且没有独立
    server 字段。若调用方传入 server，则优先使用传入值。
    """
    raw_name = str(role_name or "").strip()
    raw_server = str(server or "").strip()
    if raw_server:
        return raw_server, normalize_role_name(raw_name, raw_server)
    if "·" not in raw_name:
        return "", raw_name
    name_part, server_part = raw_name.rsplit("·", 1)
    parsed_server = server_part.strip()
    parsed_name = normalize_role_name(name_part.strip(), parsed_server)
    return parsed_server, parsed_name


def build_player_match_key(role_name: Any, server: Any) -> Optional[Tuple[str, str]]:
    parsed_server, parsed_name = split_replay_role_name(role_name, server)
    if not parsed_server or not parsed_name:
        return None
    return parsed_server.lower(), parsed_name.lower()


def extract_players_from_detail(detail_data: dict) -> list[dict]:
    """从对局详情 payload 提取双方所有角色。

    从 team1.players_info 和 team2.players_info 提取，每个玩家返回包含以下字段的 dict：
      - role_name
      - global_id
      - global_role_id
      - role_id
      - person_id
      - zone
      - server

    按 global_id、global_role_id、zone+role_id、server+role_name 逐级构建去重键。
    """
    seen: set[str] = set()
    players: list[dict] = []

    for team_key in ("team1", "team2"):
        team = detail_data.get(team_key)
        if not isinstance(team, dict):
            continue
        players_info = team.get("players_info")
        if not isinstance(players_info, list):
            continue
        for player in players_info:
            if not isinstance(player, dict):
                continue
            global_id = str(player.get("global_id") or "").strip()
            global_role_id = str(player.get("global_role_id") or "").strip()
            role_id = str(player.get("role_id") or "").strip()
            person_id = str(player.get("person_id") or "").strip()
            zone = str(player.get("zone") or "").strip()
            server = str(player.get("server") or "").strip()
            role_name = normalize_role_name(player.get("role_name"), server)
            if global_id:
                dedupe_key = f"global_id:{global_id}"
            elif global_role_id:
                dedupe_key = f"global:{global_role_id}"
            elif zone and role_id:
                dedupe_key = f"game:{zone}:{role_id}"
            elif server and role_name:
                dedupe_key = f"name:{server}:{role_name}"
            else:
                continue
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            players.append({
                "role_name": role_name,
                "global_id": global_id,
                "global_role_id": global_role_id,
                "role_id": role_id,
                "person_id": person_id,
                "zone": zone,
                "server": server,
            })

    return players


def is_beyond_stop_time(match_time: Optional[int], stop_time: int) -> bool:
    """判断 match_time 是否已达到或超过停止水位。

    返回 match_time <= stop_time。
    match_time 为 None 时视为不安全，返回 False。
    """
    if match_time is None:
        return False
    return match_time <= stop_time


def is_before_season_start(match_time: Optional[int], season_start_time: int) -> bool:
    """判断 match_time 是否早于赛季开始时间。

    返回 match_time < season_start_time。
    """
    if match_time is None:
        return False
    return match_time < season_start_time


def compute_page_fingerprint(matches: list[dict]) -> str:
    """对一页对局列表生成指纹。

    将所有 match_id 提取、排序后用逗号连接。用于安全阀检测连续相同页面。
    """
    match_ids: list[int] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        match_id = extract_match_id_from_history(item)
        if match_id is not None:
            match_ids.append(match_id)
    match_ids.sort()
    return ",".join(str(mid) for mid in match_ids)


def filter_3v3_matches(history_items: list[dict]) -> list[dict]:
    """只保留 pvp_type == 3 的对局。

    当 pvp_type 为 None 时保留——兼容旧数据可能缺少此字段。
    """
    result: list[dict] = []
    for item in history_items:
        if not isinstance(item, dict):
            continue
        pvp_type = extract_pvp_type_from_history(item)
        if pvp_type is None or pvp_type == 3:
            result.append(item)
    return result


def extract_history_items(payload: dict) -> List[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "matches", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def parse_season_start_timestamp(season_start_str: str) -> int:
    """将 "2026-04-24" 格式的日期字符串转为 Unix 时间戳（秒）。

    使用北京时间（UTC+8）。
    """
    beijing_tz = timezone(timedelta(hours=8))
    dt = datetime.strptime(season_start_str, "%Y-%m-%d").replace(tzinfo=beijing_tz)
    return int(dt.timestamp())


class JjcMatchDataSyncService:
    """JJC 对局数据同步管理服务。

    提供角色管理、单轮同步、暂停/恢复、状态查询等管理用例。
    """

    def __init__(
        self,
        repo: JjcSyncRepo,
        current_season: str,
        current_season_start: str,
        match_history_client: Optional[Any] = None,
        inspect_service: Optional[Any] = None,
        identity_repo: Optional[Any] = None,
        person_match_history_client: Optional[Any] = None,
        match_replay_client: Optional[MatchReplayClient] = None,
        role_indicator_client: Optional[RoleIndicatorClient] = None,
        match_detail_projection_service: Optional[Any] = None,
        match_detail_participant_projection_service: Optional[Any] = None,
        match_detail_persistence_service: Optional[Any] = None,
        sleep_func: Callable[[], Awaitable[None]] = random_sleep,
        page_size: int = 20,
        max_pages_per_role: int = 300,
        lease_seconds: int = 1800,
        dispatcher_idle_sleep: int = 10,
        dispatcher_batch_size: int = 20,
        dispatcher_target_per_worker: int = 3,
    ) -> None:
        self._repo = repo
        self._current_season = current_season
        self._season_start_time = parse_season_start_timestamp(current_season_start)
        self._match_history_client = match_history_client
        self._inspect_service = inspect_service
        self._identity_repo = identity_repo
        self._person_match_history_client = person_match_history_client
        self._match_replay_client = match_replay_client
        self._role_indicator_client = role_indicator_client
        self._identity_indicator_checked_at: Dict[Any, float] = {}
        self._match_detail_projection_service = match_detail_projection_service
        self._match_detail_participant_projection_service = match_detail_participant_projection_service
        self._match_detail_persistence_service = match_detail_persistence_service
        if self._match_detail_persistence_service is None:
            inspect_repo = getattr(self._inspect_service, "cache_repo", None)
            if inspect_repo is not None:
                self._match_detail_persistence_service = MatchDetailPersistenceService(
                    inspect_repo=inspect_repo,
                    identity_projector=self._match_detail_projection_service,
                    participant_projector=self._match_detail_participant_projection_service,
                    fact_projector=None,
                )
        self._sleep_func = sleep_func
        self._page_size = page_size
        self._max_pages_per_role = max_pages_per_role
        self._lease_seconds = lease_seconds
        self._dispatcher_idle_sleep = max(1, dispatcher_idle_sleep)
        self._dispatcher_batch_size = max(1, dispatcher_batch_size)
        self._dispatcher_target_per_worker = max(1, dispatcher_target_per_worker)
        self._background_task: Optional[asyncio.Task] = None
        self._last_background_summary: Optional[Dict[str, Any]] = None
        self._last_dispatcher_summary: Optional[Dict[str, Any]] = None

    async def run_once(
        self,
        mode: str = "full",
        limit: int = 3,
    ) -> Dict[str, Any]:
        """执行一轮同步，由管理员命令显式触发。"""
        if limit < 1:
            return {"error": True, "message": "invalid_limit"}
        result = await self.run_worker(
            mode=mode,
            idle_sleep=1,
            max_seconds=0,
            max_roles=limit,
            stop_when_idle=True,
        )
        result["limit"] = limit
        return result

    @staticmethod
    def _finish_summary(summary: Dict[str, Any], started_at: float) -> Dict[str, Any]:
        summary["elapsed_seconds"] = round(time.time() - started_at, 3)
        return summary

    async def enqueue_roles(
        self,
        mode: str = "full",
        limit: int = 10,
        source: str = "manual",
        batch_id: Optional[str] = None,
        queue_sync_until_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """把可执行角色放入 queued 队列，不在当前调用里同步。"""
        started_at = time.time()
        if not is_supported_queue_mode(mode):
            return {"error": True, "message": "invalid_mode"}
        if limit < 1:
            return {"error": True, "message": "invalid_limit"}
        if self._match_history_client is None or self._inspect_service is None:
            return {"error": True, "message": "sync_dependencies_not_configured"}

        pause_state = await self._get_pause_state()
        paused = bool(pause_state.get("paused"))
        recovered = await self._repo.recover_expired_leases()
        batch = batch_id or f"jjc-sync-enqueue:{uuid.uuid4()}"
        items = await self._repo.enqueue_next_roles(
            limit=limit,
            mode=mode,
            source=source,
            batch_id=batch,
            queue_sync_until_time=queue_sync_until_time,
        )
        enqueued_count = len(items)
        counts = await self._repo.count_by_status()
        workers = await self._list_workers_from_repo(limit=20)
        worker_running = self._has_running_worker(workers)
        return self._finish_summary({
            "error": False,
            "paused": paused,
            "pause_reason": pause_state.get("reason") or "",
            "mode": mode,
            "queue_sync_until_time": queue_sync_until_time,
            "limit": limit,
            "batch_id": batch,
            "enqueued_roles": enqueued_count,
            "enqueued_items": _jsonable(items),
            "recovered_leases": recovered,
            "counts": counts,
            "workers": workers,
            "worker_running": worker_running,
            "background_running": (
                worker_running
                or (self._background_task is not None and not self._background_task.done())
            ),
        }, started_at)

    async def run_worker(
        self,
        mode: str = "full",
        worker_id: Optional[str] = None,
        idle_sleep: int = 10,
        max_seconds: int = 0,
        max_roles: Optional[int] = None,
        stop_when_idle: bool = False,
    ) -> Dict[str, Any]:
        """常驻 worker 循环：每次从 queued 领取一个角色处理。"""
        started_at = time.time()
        if mode not in _WORKER_COMPAT_MODES:
            return {"error": True, "message": "invalid_mode"}
        if idle_sleep < 1:
            return {"error": True, "message": "invalid_idle_sleep"}
        if max_seconds < 0:
            return {"error": True, "message": "invalid_max_seconds"}
        if max_roles == 0:
            max_roles = None
        if max_roles is not None and max_roles < 1:
            return {"error": True, "message": "invalid_max_roles"}
        if self._match_history_client is None or self._inspect_service is None:
            return {"error": True, "message": "sync_dependencies_not_configured"}

        wid = worker_id or self._build_worker_id()
        summary: Dict[str, Any] = {
            "error": False,
            "worker_id": wid,
            "mode": mode,
            "processed_roles": 0,
            "discovered_matches": 0,
            "saved_details": 0,
            "skipped_details": 0,
            "failed_details": 0,
            "unavailable_details": 0,
            "failed_roles": 0,
            "interrupted_ticks": 0,
            "idle_ticks": 0,
            "paused_ticks": 0,
            "recovered_leases": 0,
            "stopped_reason": "",
            "errors": [],
            "elapsed_seconds": 0.0,
        }

        await self._register_worker(wid, mode)
        try:
            while True:
                if max_seconds and time.time() - started_at >= max_seconds:
                    summary["stopped_reason"] = "max_seconds_reached"
                    break
                if max_roles is not None and summary["processed_roles"] >= max_roles:
                    summary["stopped_reason"] = "max_roles_reached"
                    break

                tick = await self.worker_tick(mode=mode, worker_id=wid)
                summary["recovered_leases"] += tick.get("recovered_leases", 0)

                if tick.get("processed"):
                    summary["processed_roles"] += 1
                    result = tick.get("result") or {}
                    for key in ("discovered_matches", "saved_details", "skipped_details", "failed_details", "unavailable_details"):
                        summary[key] = summary.get(key, 0) + result.get(key, 0)
                    if result.get("error"):
                        summary["errors"].append(result.get("message", "unknown_error"))
                        if not self._role_result_was_interrupted(result):
                            summary["failed_roles"] += 1
                    continue

                if tick.get("paused"):
                    summary["paused_ticks"] += 1
                    summary["paused"] = True
                    summary["pause_reason"] = tick.get("pause_reason") or ""
                    if tick.get("auto_paused"):
                        summary["stopped_reason"] = "auto_paused"
                        result = tick.get("result") or {}
                        if result:
                            summary["pause_reason"] = result.get("message", summary["pause_reason"])
                        break
                    if stop_when_idle:
                        summary["stopped_reason"] = "paused"
                        break
                    await asyncio.sleep(idle_sleep)
                    continue

                result = tick.get("result") or {}
                if result.get("error"):
                    for key in ("discovered_matches", "saved_details", "skipped_details", "failed_details", "unavailable_details"):
                        summary[key] = summary.get(key, 0) + result.get(key, 0)
                    summary["errors"].append(result.get("message", "unknown_error"))
                    interrupted = self._role_result_was_interrupted(result)
                    if interrupted:
                        summary["interrupted_ticks"] += 1
                    else:
                        summary["failed_roles"] += 1
                    if interrupted:
                        if stop_when_idle:
                            summary["stopped_reason"] = "interrupted"
                            break
                        await asyncio.sleep(idle_sleep)
                        continue
                    if stop_when_idle:
                        summary["stopped_reason"] = "error"
                        break
                    await asyncio.sleep(idle_sleep)
                    continue

                if tick.get("idle"):
                    summary["idle_ticks"] += 1
                    if stop_when_idle:
                        summary["stopped_reason"] = "idle"
                        break
                    await asyncio.sleep(idle_sleep)
                    continue

                if tick.get("error"):
                    summary["error"] = True
                    summary["message"] = tick.get("message", "worker_tick_error")
                    summary["stopped_reason"] = "error"
                    summary["errors"].append(summary["message"])
                    break
        except asyncio.CancelledError:
            summary["stopped_reason"] = "cancelled"
            await self._stop_worker(wid, "cancelled")
            raise
        except KeyboardInterrupt:
            summary["stopped_reason"] = "keyboard_interrupt"
        except Exception as exc:
            summary["error"] = True
            summary["message"] = str(exc)
            summary["stopped_reason"] = "exception"
            summary["errors"].append(str(exc))
        finally:
            if not summary.get("stopped_reason"):
                summary["stopped_reason"] = "stopped"
            await self._stop_worker(wid, str(summary.get("stopped_reason") or "stopped"))

        return self._finish_summary(summary, started_at)

    async def worker_tick(self, mode: str, worker_id: str) -> Dict[str, Any]:
        """执行一个 worker tick，便于常驻循环和测试复用。"""
        recovered = await self._repo.recover_expired_leases()
        pause_state = await self._get_pause_state()
        if pause_state.get("paused"):
            await self._heartbeat_worker(worker_id, "paused", last_error=pause_state.get("reason") or "")
            return {
                "error": False,
                "paused": True,
                "pause_reason": pause_state.get("reason") or "",
                "recovered_leases": recovered,
            }

        role = await self._repo.claim_queued_role(
            lease_owner=worker_id,
            lease_seconds=self._lease_seconds,
        )
        if role is None:
            await self._heartbeat_worker(worker_id, "idle")
            return {"error": False, "idle": True, "recovered_leases": recovered}

        await self._heartbeat_worker(
            worker_id,
            "syncing",
            current_role=role,
        )
        role_mode = str(role.get("queue_mode") or mode or "full")
        result = await self._sync_one_role(
            role=role,
            mode=role_mode,
            lease_owner=worker_id,
        )
        if result.get("global_paused"):
            await self._heartbeat_worker(
                worker_id,
                "paused",
                current_role=None,
                last_error=result.get("message", ""),
            )
            return {
                "error": False,
                "paused": True,
                "auto_paused": True,
                "processed": False,
                "result": result,
                "recovered_leases": recovered,
            }

        await self._heartbeat_worker(
            worker_id,
            "idle",
            current_role=None,
            last_result=result,
            last_error=result.get("message", "") if result.get("error") else "",
        )
        return {
            "error": False,
            "processed": not self._role_result_was_interrupted(result),
            "result": result,
            "recovered_leases": recovered,
        }

    async def set_role_priority(
        self,
        server: str,
        name: str,
        priority: int,
        updated_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_server = server.strip()
        normalized_name = name.strip()
        if not normalized_server or not normalized_name:
            return {"error": True, "message": "服务器和角色名不能为空"}
        role = await self._repo.get_role_by_name(normalized_server, normalized_name)
        if role is None:
            return {"error": True, "message": f"未找到角色 {server}/{name}"}
        identity_key = str(role.get("identity_key") or "")
        identity_id = self._extract_identity_id(role)
        if identity_id is not None and hasattr(self._repo, "update_identity_priority"):
            success = await self._repo.update_identity_priority(
                identity_id=identity_id,
                priority=priority,
                updated_by=updated_by,
            )
        else:
            success = await self._repo.update_role_priority(
                identity_key=identity_key,
                priority=priority,
                updated_by=updated_by,
            )
        if not success:
            return {"error": True, "message": "更新优先级失败"}
        return {
            "error": False,
            "message": f"角色 {server}/{name} 优先级已调整为 {priority}",
            "identity_key": identity_key,
            "identity_id": str(identity_id or "") if identity_id is not None else None,
            "priority": priority,
        }

    async def queue_status(self) -> Dict[str, Any]:
        try:
            return await self._build_status_summary()
        except Exception as exc:
            logger.warning(f"JJC 队列状态查询失败: error={exc}")
            return {"error": True, "message": f"查询队列状态失败: {exc}"}

    async def list_queue(
        self,
        status: Optional[str] = None,
        mode: Optional[str] = None,
        server: Optional[str] = None,
        name: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        if page < 1:
            return {"error": True, "message": "invalid_page"}
        if page_size < 1 or page_size > 200:
            return {"error": True, "message": "invalid_page_size"}
        result = await self._repo.list_queue(
            status=status,
            mode=mode,
            server=server,
            name=name,
            page=page,
            page_size=page_size,
        )
        result.setdefault("error", False)
        result["items"] = _jsonable(result.get("items") or [])
        return result

    async def list_workers(self) -> Dict[str, Any]:
        workers = await self._list_workers_from_repo(limit=100)
        return {"error": False, "items": workers}

    async def _build_status_summary(self) -> Dict[str, Any]:
        pause_state = await self._get_pause_state()
        counts = await self._repo.count_by_status()
        workers = await self._list_workers_from_repo(limit=20)
        worker_running = self._has_running_worker(workers)
        return {
            "error": False,
            "paused": bool(pause_state.get("paused")),
            "pause_reason": pause_state.get("reason") or "",
            "pause_updated_at": pause_state.get("updated_at"),
            "counts": counts,
            "recent_errors": _jsonable(await self._repo.get_recent_errors(limit=5)),
            "workers": workers,
            "worker_running": worker_running,
            "background_running": (
                worker_running
                or (self._background_task is not None and not self._background_task.done())
            ),
            "last_background_summary": self._last_background_summary,
            "last_dispatcher_summary": self._last_dispatcher_summary,
        }

    @staticmethod
    def _role_result_was_interrupted(result: Dict[str, Any]) -> bool:
        return bool(
            result.get("stale_lease")
            or result.get("stale_detail_lease")
            or result.get("interrupted")
        )

    @staticmethod
    def _build_worker_id() -> str:
        host = socket.gethostname() or "unknown-host"
        return "jjc-sync-worker:{}:{}:{}".format(host, os.getpid(), uuid.uuid4().hex[:8])

    @staticmethod
    def _has_running_worker(workers: List[Dict[str, Any]]) -> bool:
        return any(bool(worker.get("online")) for worker in workers)

    async def _get_pause_state(self) -> Dict[str, Any]:
        return await self._repo.get_pause_state()

    async def _register_worker(self, worker_id: str, mode: str) -> None:
        success = await self._repo.register_worker(
            worker_id=worker_id,
            mode=mode,
            pid=os.getpid(),
            host=socket.gethostname(),
        )
        if not success:
            raise RuntimeError("worker_register_failed")

    async def _heartbeat_worker(
        self,
        worker_id: str,
        status: str,
        current_role: Optional[Dict[str, Any]] = None,
        last_result: Optional[Dict[str, Any]] = None,
        last_error: str = "",
    ) -> None:
        current_identity_key = None
        current_identity_id = None
        current_server = None
        current_name = None
        if current_role:
            current_identity_key = str(current_role.get("identity_key") or "") or None
            current_identity_id = self._extract_identity_id(current_role)
            current_server = str(current_role.get("server") or "") or None
            current_name = str(current_role.get("name") or "") or None
        await self._repo.heartbeat_worker(
            worker_id=worker_id,
            status=status,
            current_identity_id=current_identity_id,
            current_identity_key=current_identity_key,
            current_server=current_server,
            current_name=current_name,
            last_result=last_result,
            last_error=last_error,
        )

    def _worker_heartbeat_interval(self) -> int:
        return max(30, min(120, int(_WORKER_HEARTBEAT_TTL_SECONDS / 2)))

    @staticmethod
    def _count_active_workers(workers: List[Dict[str, Any]]) -> int:
        return sum(1 for worker in workers if bool(worker.get("online")))

    def _resolve_auto_dispatcher_queue_sync_until_time(self, now: Optional[int] = None) -> int:
        ref = int(time.time()) if now is None else int(now)
        return ref - (_AUTO_DISPATCHER_SYNC_WINDOW_DAYS * 86400)

    async def dispatch_queue_once(self) -> Dict[str, Any]:
        pause_state = await self._get_pause_state()
        counts = await self._repo.count_by_status()
        workers = await self._list_workers_from_repo(limit=100)
        active_workers = self._count_active_workers(workers)
        queued_count = int(counts.get("queued", 0) or 0)
        target_queued = active_workers * self._dispatcher_target_per_worker
        summary: Dict[str, Any] = {
            "error": False,
            "source": _AUTO_DISPATCHER_SOURCE,
            "paused": bool(pause_state.get("paused")),
            "pause_reason": pause_state.get("reason") or "",
            "counts": counts,
            "active_workers": active_workers,
            "queued_count": queued_count,
            "target_queued": target_queued,
            "dispatch_batch_size": self._dispatcher_batch_size,
            "dispatcher_target_per_worker": self._dispatcher_target_per_worker,
            "enqueued_roles": 0,
            "queue_sync_until_time": None,
            "action": "skipped",
            "reason": "",
        }

        if summary["paused"]:
            summary["reason"] = "paused"
            self._last_dispatcher_summary = summary
            return summary

        if active_workers <= 0:
            summary["reason"] = "no_active_workers"
            self._last_dispatcher_summary = summary
            return summary

        if queued_count >= target_queued:
            summary["reason"] = "queue_sufficient"
            self._last_dispatcher_summary = summary
            return summary

        dispatch_limit = min(self._dispatcher_batch_size, max(1, target_queued - queued_count))
        queue_sync_until_time = self._resolve_auto_dispatcher_queue_sync_until_time()
        result = await self.enqueue_roles(
            mode="full",
            limit=dispatch_limit,
            source=_AUTO_DISPATCHER_SOURCE,
            queue_sync_until_time=queue_sync_until_time,
        )
        summary.update({
            "action": "enqueued",
            "reason": "queue_refilled",
            "dispatch_limit": dispatch_limit,
            "queue_sync_until_time": queue_sync_until_time,
            "enqueue_result": result,
            "enqueued_roles": int(result.get("enqueued_roles", 0) or 0),
            "counts": result.get("counts", counts),
        })
        self._last_dispatcher_summary = summary
        return summary

    async def run_dispatcher(
        self,
        idle_sleep: Optional[int] = None,
        max_seconds: int = 0,
    ) -> Dict[str, Any]:
        sleep_seconds = self._dispatcher_idle_sleep if idle_sleep is None else max(1, idle_sleep)
        started_at = time.time()
        summary: Dict[str, Any] = {
            "error": False,
            "source": _AUTO_DISPATCHER_SOURCE,
            "idle_sleep": sleep_seconds,
            "max_seconds": max_seconds,
            "rounds": 0,
            "enqueued_roles": 0,
            "stopped_reason": "",
            "last_result": None,
            "elapsed_seconds": 0.0,
        }
        try:
            while True:
                if max_seconds > 0 and (time.time() - started_at) >= max_seconds:
                    summary["stopped_reason"] = "max_seconds_reached"
                    break
                result = await self.dispatch_queue_once()
                summary["rounds"] += 1
                summary["last_result"] = result
                summary["enqueued_roles"] += int(result.get("enqueued_roles", 0) or 0)
                if result.get("error"):
                    summary["error"] = True
                    summary["message"] = result.get("message", "dispatcher_error")
                    summary["stopped_reason"] = "error"
                    break
                await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            summary["stopped_reason"] = "cancelled"
            raise
        finally:
            if not summary.get("stopped_reason"):
                summary["stopped_reason"] = "stopped"
            summary = self._finish_summary(summary, started_at)
            self._last_dispatcher_summary = summary
        return summary

    async def _heartbeat_worker_if_due(
        self,
        worker_id: str,
        current_role: Dict[str, Any],
        last_heartbeat_at: float,
        force: bool = False,
    ) -> float:
        now = time.time()
        if not force and now - last_heartbeat_at < self._worker_heartbeat_interval():
            return last_heartbeat_at
        await self._heartbeat_worker(worker_id, "syncing", current_role=current_role)
        return now

    async def _stop_worker(self, worker_id: str, reason: str) -> None:
        await self._repo.stop_worker(worker_id=worker_id, reason=reason)

    async def _list_workers_from_repo(self, limit: int = 20) -> List[Dict[str, Any]]:
        workers = await self._repo.list_workers(limit=limit)
        return self._annotate_workers(_jsonable(workers))

    @classmethod
    def _annotate_workers(cls, workers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = time.time()
        annotated: List[Dict[str, Any]] = []
        for worker in workers:
            item = dict(worker)
            online = cls._worker_is_active(item, now=now)
            item["online"] = online
            if online:
                item["effective_status"] = str(item.get("status") or "running")
            elif str(item.get("status") or "") == "stopped":
                item["effective_status"] = "stopped"
            else:
                item["effective_status"] = "offline"
            annotated.append(item)
        return annotated

    @staticmethod
    def _worker_is_active(worker: Dict[str, Any], now: Optional[float] = None) -> bool:
        running_statuses = {"starting", "running", "idle", "syncing", "paused"}
        if str(worker.get("status") or "") not in running_statuses:
            return False
        heartbeat_at = _coerce_float(worker.get("heartbeat_at"))
        if heartbeat_at is None:
            return False
        ref = time.time() if now is None else now
        return ref - heartbeat_at <= _WORKER_HEARTBEAT_TTL_SECONDS

    async def _pause_for_global_auth_error(
        self,
        identity_key: str,
        reason: str,
        lease_owner: Optional[str] = None,
        identity_id: Optional[Any] = None,
    ) -> None:
        await self._repo.set_paused(True, reason)
        await self._release_role_interrupted(
            identity_key=identity_key,
            identity_id=identity_id,
            reason=reason,
            requeue=True,
            lease_owner=lease_owner,
        )

    def _role_lease_renew_interval(self) -> int:
        return max(30, min(300, int(self._lease_seconds / 3)))

    async def _renew_role_lease_if_due(
        self,
        identity_key: str,
        lease_owner: str,
        last_renewed_at: float,
        force: bool = False,
        identity_id: Optional[Any] = None,
    ) -> float:
        now = time.time()
        if not force and now - last_renewed_at < self._role_lease_renew_interval():
            return last_renewed_at

        if identity_id is not None and hasattr(self._repo, "renew_identity_lease"):
            renewed = await self._repo.renew_identity_lease(
                identity_id=identity_id,
                lease_owner=lease_owner,
                lease_seconds=self._lease_seconds,
            )
            lease_ref = "identity_id={}".format(identity_id)
        else:
            renewed = await self._repo.renew_role_lease(
                identity_key=identity_key,
                lease_owner=lease_owner,
                lease_seconds=self._lease_seconds,
            )
            lease_ref = "identity_key={}".format(identity_key)
        if not renewed:
            raise JjcSyncStaleRoleLeaseError(
                "stale_role_lease: {} owner={}".format(lease_ref, lease_owner)
            )
        return now

    async def _renew_match_detail_lease_if_due(
        self,
        match_id: int,
        lease_owner: str,
        last_renewed_at: float,
        force: bool = False,
    ) -> float:
        now = time.time()
        if not force and now - last_renewed_at < self._role_lease_renew_interval():
            return last_renewed_at

        renewed = await self._repo.renew_match_detail_lease(
            match_id=match_id,
            lease_owner=lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if not renewed:
            raise JjcSyncStaleMatchDetailLeaseError(
                "stale_match_detail_lease: match_id={} owner={}".format(match_id, lease_owner)
            )
        return now

    @staticmethod
    def _extract_identity_id(doc: Dict[str, Any]) -> Optional[Any]:
        for key in ("identity_id", "_id"):
            value = doc.get(key)
            if value is not None and str(value).strip():
                return value
        return None

    @staticmethod
    def _merge_identity_snapshot(role: Dict[str, Any], identity: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(role)
        if identity.get("_id") is not None and not merged.get("identity_id"):
            merged["identity_id"] = identity.get("_id")
        for identity_key, role_key in (
            ("identity_key", "identity_key"),
            ("server", "server"),
            ("name", "name"),
            ("global_id", "global_id"),
            ("global_role_id", "global_role_id"),
            ("role_id", "role_id"),
            ("game_role_id", "role_id"),
            ("person_id", "person_id"),
            ("zone", "zone"),
            ("full_synced_until_time", "full_synced_until_time"),
            ("history_exhausted", "history_exhausted"),
        ):
            value = identity.get(identity_key)
            if value is not None and str(value).strip():
                merged[role_key] = value
        return merged

    @staticmethod
    def _timestamp_age_seconds(value: Any) -> Optional[float]:
        if value is None:
            return None
        now = time.time()
        if isinstance(value, datetime):
            ref = value.timestamp()
            return max(0.0, now - ref)
        numeric = _coerce_float(value)
        if numeric is not None:
            return max(0.0, now - numeric)
        return None

    def _identity_indicator_refresh_needed(self, identity: Dict[str, Any]) -> bool:
        checked = self._identity_indicator_checked_at.get(identity.get("_id"))
        if checked is not None and time.monotonic() - checked < _IDENTITY_INDICATOR_REFRESH_SECONDS:
            return False
        global_role_id = str(identity.get("global_role_id") or "").strip()
        refreshed_age = self._timestamp_age_seconds(identity.get("global_role_id_refreshed_at"))
        if not global_role_id.startswith("SK01-"):
            return True
        if refreshed_age is None:
            return True
        return refreshed_age > _IDENTITY_INDICATOR_REFRESH_SECONDS

    async def _load_identity_for_sync(self, role: Dict[str, Any]) -> Dict[str, Any]:
        identity_id = self._extract_identity_id(role)
        if identity_id is None or self._identity_repo is None:
            return role
        getter = getattr(self._identity_repo, "get_by_id", None)
        if not callable(getter):
            return role
        try:
            identity = await getter(identity_id)
        except Exception as exc:
            logger.warning("JJC 同步读取角色身份失败: identity_id={} error={}".format(identity_id, exc))
            return role
        if not isinstance(identity, dict) or not identity:
            return role
        merged = self._merge_identity_snapshot(role, identity)
        refreshed = await self._refresh_identity_indicator_for_sync(merged, identity)
        if refreshed is not None:
            merged = self._merge_identity_snapshot(merged, refreshed)
        return merged

    async def _refresh_identity_indicator_for_sync(
        self,
        role: Dict[str, Any],
        identity: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        identity_id = self._extract_identity_id(identity) or self._extract_identity_id(role)
        if identity_id is None or self._identity_repo is None or self._role_indicator_client is None:
            return None
        if not self._identity_indicator_refresh_needed(identity):
            return None
        refresher = getattr(self._identity_repo, "refresh_indicator_fields_by_id", None)
        if not callable(refresher):
            return None
        role_id = str(
            identity.get("game_role_id")
            or identity.get("role_id")
            or role.get("game_role_id")
            or role.get("role_id")
            or ""
        ).strip()
        zone = str(identity.get("zone") or role.get("zone") or "").strip()
        server = str(identity.get("server") or role.get("server") or "").strip()
        name = str(identity.get("name") or role.get("name") or "").strip()
        if not (role_id and zone and server):
            return None
        try:
            await self._sleep_func()
            request_started_at = datetime.now(timezone.utc)
            indicator_data = await asyncio.to_thread(
                self._role_indicator_client.get_role_indicator,
                role_id=role_id,
                zone=zone,
                server=server,
            )
        except Exception as exc:
            if is_tuilan_auth_error(str(exc)):
                raise JjcSyncGlobalPauseError(
                    build_tuilan_auth_pause_reason(str(exc), context="role-indicator")
                )
            logger.warning(
                "JJC 同步刷新角色 indicator 失败: identity_id={} server={} role_id={} error={}".format(
                    identity_id, server, role_id, exc
                )
            )
            return None
        if is_tuilan_auth_error(indicator_data):
            raise JjcSyncGlobalPauseError(
                build_tuilan_auth_pause_reason(indicator_data, context="role-indicator")
            )
        if not isinstance(indicator_data, dict) or indicator_data.get("error"):
            return None
        payload = indicator_data.get("data")
        if not isinstance(payload, dict):
            payload = indicator_data
        role_info = payload.get("role_info") if isinstance(payload, dict) else None
        if (getattr(cfg, "JJC_ROLE_IDENTITY_RECOVERY_ENABLED", 0)
                and self._inspect_service is not None):
            cache_repo = getattr(self._inspect_service, "kungfu_cache_repo", None)
            loader = getattr(cache_repo, "load_query_id_recovery_candidates", None)
            if callable(loader):
                recovery = await recover_query_ids(
                    identity, identity_repo=self._identity_repo,
                    candidates=await loader(identity),
                    indicator_fetcher=self._role_indicator_client.get_role_indicator,
                    replay_fetcher=(self._match_replay_client.get_match_replay if self._match_replay_client else lambda **_: {}),
                    history_fetcher=(self._match_history_client.get_mine_match_history if self._match_history_client else None),
                    current_indicator=indicator_data,
                    current_indicator_request={"role_id": role_id, "zone": zone, "server": server,
                                               "observed_at": request_started_at},
                    max_logical_calls=4, publish=True,
                )
                if recovery.get("status") == "recovered":
                    return recovery["identity"]
                if recovery.get("status") == "upstream_error":
                    failure = recovery.get("upstream_error_data")
                    failure_code = None
                    if isinstance(failure, dict):
                        failure_code = _coerce_int(failure.get("code")) or _coerce_int(failure.get("status_code"))
                    if failure_code != 429 and is_tuilan_auth_error(failure):
                        safe_failure = {"code": failure_code} if failure_code in (401, 403) else {"error": "upstream authentication rejected"}
                        raise JjcSyncGlobalPauseError(build_tuilan_auth_pause_reason(
                            safe_failure, context="role-recovery-" + str(recovery.get("upstream_endpoint") or "request"),
                        ))
                    return None
        if not isinstance(role_info, dict):
            return None
        if has_verified_query_ids(identity):
            # A normal refresh is not a new replay-backed observation.
            self._identity_indicator_checked_at[identity_id] = time.monotonic()
            while len(self._identity_indicator_checked_at) > 1024:
                self._identity_indicator_checked_at.pop(next(iter(self._identity_indicator_checked_at)))
            if any(role_info.get(key) and str(role_info[key]) != str(identity.get(key) or "")
                   for key in ("server", "zone", "role_id", "global_role_id")):
                logger.warning("JJC verified identity migration unresolved: identity_id={}", identity_id)
            return None
        sk01_global = str(role_info.get("global_role_id") or "").strip()
        if not sk01_global.startswith("SK01-"):
            return None
        person_info = payload.get("person_info") if isinstance(payload, dict) else None
        person_id = None
        if isinstance(person_info, dict):
            person_id = str(person_info.get("person_id") or "").strip() or None
        refreshed = await refresher(
            identity_id=identity_id,
            global_role_id=sk01_global,
            refresh_source="indicator",
            zone=str(role_info.get("zone") or zone).strip() or zone,
            game_role_id=str(role_info.get("role_id") or role_id).strip() or role_id,
            role_id=str(role_info.get("role_id") or role_id).strip() or role_id,
            person_id=person_id,
            server=str(role_info.get("server") or server).strip() or server,
            name=name or None,
        )
        if isinstance(refreshed, dict):
            await self._sync_identity_snapshot_if_available(refreshed)
            return refreshed
        return None

    async def _sync_identity_snapshot_if_available(self, identity: Dict[str, Any]) -> None:
        identity_id = self._extract_identity_id(identity)
        if identity_id is None:
            return
        sync_snapshot = getattr(self._repo, "sync_identity_snapshot", None)
        if callable(sync_snapshot):
            try:
                await sync_snapshot(identity=identity)
            except TypeError:
                await sync_snapshot(identity_id=identity_id, identity=identity)
            except Exception as exc:
                logger.warning("JJC 同步 identity 队列快照失败: identity_id={} error={}".format(identity_id, exc))
            return
        upsert_candidate = getattr(self._repo, "upsert_identity_queue_candidate", None)
        if not callable(upsert_candidate):
            return
        try:
            await upsert_candidate(
                identity_id=identity_id,
                identity_key=identity.get("identity_key"),
                server=identity.get("server"),
                name=identity.get("name"),
                normalized_server=identity.get("normalized_server") or identity.get("server"),
                normalized_name=identity.get("normalized_name") or identity.get("name"),
                global_id=identity.get("global_id"),
                global_role_id=identity.get("global_role_id"),
                role_id=identity.get("role_id") or identity.get("game_role_id"),
                game_role_id=identity.get("game_role_id") or identity.get("role_id"),
                person_id=identity.get("person_id"),
                zone=identity.get("zone"),
                source="identity_refresh",
                priority=0,
            )
        except Exception as exc:
            logger.warning("JJC 更新 identity 队列候选快照失败: identity_id={} error={}".format(identity_id, exc))

    async def _release_role_interrupted(
        self,
        identity_key: str,
        reason: str,
        requeue: bool = True,
        lease_owner: Optional[str] = None,
        identity_id: Optional[Any] = None,
    ) -> bool:
        if identity_id is not None and hasattr(self._repo, "release_identity_interrupted"):
            return await self._repo.release_identity_interrupted(
                identity_id=identity_id,
                reason=reason,
                requeue=requeue,
                lease_owner=lease_owner,
            )
        return await self._repo.release_role_interrupted(
            identity_key=identity_key,
            reason=reason,
            requeue=requeue,
            lease_owner=lease_owner,
        )

    async def _release_role_success(
        self,
        identity_key: str,
        identity_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> bool:
        if identity_id is not None and hasattr(self._repo, "release_identity_success"):
            return await self._repo.release_identity_success(identity_id=identity_id, **kwargs)
        return await self._repo.release_role_success(identity_key=identity_key, **kwargs)

    async def _release_role_failure(
        self,
        identity_key: str,
        error_message: str = "",
        lease_owner: Optional[str] = None,
        identity_id: Optional[Any] = None,
    ) -> bool:
        if identity_id is not None and hasattr(self._repo, "release_identity_failure"):
            return await self._repo.release_identity_failure(
                identity_id=identity_id,
                error_message=error_message,
                lease_owner=lease_owner,
            )
        return await self._repo.release_role_failure(
            identity_key,
            error_message,
            lease_owner=lease_owner,
        )

    async def run_until_idle(
        self,
        mode: str = "full",
        limit: int = 20,
        max_rounds: Optional[int] = None,
        max_seconds: int = 3600,
    ) -> Dict[str, Any]:
        """连续执行多轮同步，直到队列暂时无可执行角色或达到保护上限。"""
        started_at = time.time()
        rounds_limit = max_rounds if max_rounds is not None else 1000000
        summary: Dict[str, Any] = {
            "error": False,
            "mode": mode,
            "limit": limit,
            "max_rounds": max_rounds,
            "max_seconds": max_seconds,
            "rounds": 0,
            "processed_roles": 0,
            "discovered_matches": 0,
            "saved_details": 0,
            "skipped_details": 0,
            "failed_details": 0,
            "unavailable_details": 0,
            "failed_roles": 0,
            "recovered_leases": 0,
            "errors": [],
            "stopped_reason": "",
            "elapsed_seconds": 0.0,
        }

        if limit < 1:
            return {"error": True, "message": "invalid_limit"}
        if rounds_limit < 1:
            return {"error": True, "message": "invalid_rounds"}
        if max_seconds < 1:
            return {"error": True, "message": "invalid_max_seconds"}

        while summary["rounds"] < rounds_limit:
            elapsed = time.time() - started_at
            if elapsed >= max_seconds:
                summary["stopped_reason"] = "max_seconds_reached"
                break

            result = await self.run_once(mode=mode, limit=limit)
            summary["rounds"] += 1

            for key in (
                "processed_roles",
                "discovered_matches",
                "saved_details",
                "skipped_details",
                "failed_details",
                "unavailable_details",
                "failed_roles",
                "recovered_leases",
            ):
                summary[key] += result.get(key, 0)

            errors = result.get("errors") or []
            if errors:
                summary["errors"].extend(errors)

            if result.get("error"):
                summary["error"] = True
                summary["message"] = result.get("message", "unknown_error")
                summary["stopped_reason"] = "error"
                break
            if result.get("paused"):
                summary["paused"] = True
                summary["stopped_reason"] = "paused"
                break
            if result.get("processed_roles", 0) <= 0:
                summary["stopped_reason"] = "idle"
                break

        if not summary["stopped_reason"]:
            summary["stopped_reason"] = "max_rounds_reached"
        return self._finish_summary(summary, started_at)

    async def start_background_run(
        self,
        mode: str = "full",
        limit: int = 20,
        max_rounds: Optional[int] = None,
        max_seconds: int = 3600,
    ) -> Dict[str, Any]:
        """启动后台批量同步任务。"""
        if self._background_task is not None and not self._background_task.done():
            return {"error": True, "message": "background_sync_already_running"}
        if limit < 1:
            return {"error": True, "message": "invalid_limit"}
        if max_rounds is not None and max_rounds < 1:
            return {"error": True, "message": "invalid_rounds"}
        if max_seconds < 1:
            return {"error": True, "message": "invalid_max_seconds"}

        self._background_task = asyncio.create_task(
            self._run_background(
                mode=mode,
                limit=limit,
                max_rounds=max_rounds,
                max_seconds=max_seconds,
            )
        )
        return {
            "error": False,
            "message": "background_sync_started",
            "mode": mode,
            "limit": limit,
            "max_rounds": max_rounds,
            "max_seconds": max_seconds,
        }

    async def _run_background(
        self,
        mode: str,
        limit: int,
        max_rounds: Optional[int],
        max_seconds: int,
    ) -> None:
        try:
            self._last_background_summary = await self.run_until_idle(
                mode=mode,
                limit=limit,
                max_rounds=max_rounds,
                max_seconds=max_seconds,
            )
        except Exception as exc:
            logger.warning(f"JJC 后台批量同步异常: error={exc}")
            self._last_background_summary = {
                "error": True,
                "message": str(exc),
                "mode": mode,
                "limit": limit,
                "max_rounds": max_rounds,
                "max_seconds": max_seconds,
                "stopped_reason": "exception",
                "elapsed_seconds": 0.0,
            }

    async def _sync_one_role(
        self,
        role: Dict[str, Any],
        mode: str,
        lease_owner: str,
    ) -> Dict[str, Any]:
        try:
            role = await self._load_identity_for_sync(role)
        except JjcSyncGlobalPauseError as exc:
            await self._pause_for_global_auth_error(
                str(role.get("identity_key") or ""), exc.reason,
                lease_owner=lease_owner, identity_id=self._extract_identity_id(role),
            )
            return {"error": True, "global_paused": True, "message": exc.reason}
        identity_key = str(role.get("identity_key") or "")
        identity_id = self._extract_identity_id(role)
        server = str(role.get("server") or "")
        name = str(role.get("name") or "")
        global_role_id = str(role.get("global_role_id") or "").strip()
        if not identity_key:
            if identity_id is None:
                return {"error": True, "message": "role_missing_identity_key"}
            identity_key = "identity_id:{}".format(identity_id)
        last_role_lease_renewed_at = 0.0
        last_worker_heartbeat_at = 0.0
        current_role = dict(role)
        try:
            last_role_lease_renewed_at = await self._renew_role_lease_if_due(
                identity_key,
                lease_owner,
                last_role_lease_renewed_at,
                force=True,
                identity_id=identity_id,
            )
        except JjcSyncStaleLeaseError as exc:
            return {"error": True, "stale_lease": True, "message": str(exc)}

        async def renew_role_context(force: bool = False) -> None:
            nonlocal last_role_lease_renewed_at
            last_role_lease_renewed_at = await self._renew_role_lease_if_due(
                identity_key,
                lease_owner,
                last_role_lease_renewed_at,
                force=force,
                identity_id=identity_id,
            )

        if not global_role_id:
            try:
                identity = await self._resolve_role_identity_for_sync(role)
            except JjcSyncGlobalPauseError as exc:
                reason = exc.reason
                logger.warning(
                    f"JJC 同步身份补全触发全局暂停: server={server} name={name} reason={reason}"
                )
                await self._pause_for_global_auth_error(
                    identity_key,
                    reason,
                    lease_owner=lease_owner,
                    identity_id=identity_id,
                )
                return {"error": True, "global_paused": True, "message": reason}
            global_role_id = str(identity.get("global_role_id") or "").strip()
            if not global_role_id:
                message = f"{server}/{name} 缺少 global_role_id，无法同步推栏战局历史"
                released = await self._release_role_failure(
                    identity_key,
                    message,
                    lease_owner=lease_owner,
                    identity_id=identity_id,
                )
                if released is False:
                    return {"error": True, "stale_lease": True, "message": "stale_role_lease"}
                return {"error": True, "message": message}
            if identity_id is None:
                migrated_identity_key = await self._repo.update_role_identity_fields_and_key(
                    identity_key=identity_key,
                    global_id=str(identity.get("global_id") or role.get("global_id") or "").strip() or None,
                    global_role_id=global_role_id,
                    role_id=str(identity.get("role_id") or identity.get("game_role_id") or "").strip() or None,
                    person_id=str(identity.get("person_id") or role.get("person_id") or "").strip() or None,
                    zone=str(identity.get("zone") or "").strip() or None,
                    identity_source=str(identity.get("source") or "").strip() or None,
                    lease_owner=lease_owner,
                )
                if not migrated_identity_key:
                    return {
                        "error": True,
                        "stale_lease": True,
                        "message": "stale_role_lease: identity_key={} owner={} during_identity_migration".format(
                            identity_key,
                            lease_owner,
                        ),
                    }
                if migrated_identity_key:
                    identity_key = migrated_identity_key
                    current_role["identity_key"] = identity_key
                    try:
                        last_role_lease_renewed_at = await self._renew_role_lease_if_due(
                            identity_key,
                            lease_owner,
                            last_role_lease_renewed_at,
                            force=True,
                        )
                    except JjcSyncStaleLeaseError as exc:
                        return {"error": True, "stale_lease": True, "message": str(exc)}
            await self._upsert_role_identity_from_resolved(server, name, identity)

        run_upper_time = int(time.time())
        stop_time = self._resolve_stop_time(role, mode)
        cursor = 0
        page_index = 0
        previous_fingerprint = ""
        repeated_fingerprint_count = 0
        oldest_synced_match_time: Optional[int] = None
        latest_seen_match_time: Optional[int] = None
        reached_boundary = False
        reached_season_start = False
        result: Dict[str, Any] = {
            "error": False,
            "discovered_matches": 0,
            "saved_details": 0,
            "skipped_details": 0,
            "failed_details": 0,
            "unavailable_details": 0,
        }

        try:
            async def renew_worker_heartbeat(force: bool = False) -> None:
                nonlocal last_worker_heartbeat_at
                last_worker_heartbeat_at = await self._heartbeat_worker_if_due(
                    lease_owner,
                    current_role,
                    last_worker_heartbeat_at,
                    force=force,
                )

            while page_index < self._max_pages_per_role:
                await renew_worker_heartbeat(force=page_index == 0)
                last_role_lease_renewed_at = await self._renew_role_lease_if_due(
                    identity_key,
                    lease_owner,
                    last_role_lease_renewed_at,
                    identity_id=identity_id,
                )
                await self._sleep_func()
                payload = await asyncio.to_thread(
                    self._match_history_client.get_mine_match_history,
                    global_role_id=global_role_id,
                    size=self._page_size,
                    cursor=cursor,
                )
                if is_tuilan_auth_error(payload):
                    raise JjcSyncGlobalPauseError(
                        build_tuilan_auth_pause_reason(payload, context="战局历史")
                    )
                if not isinstance(payload, dict) or payload.get("error"):
                    raise RuntimeError(str(payload.get("error") if isinstance(payload, dict) else "invalid_history_response"))

                history_items = extract_history_items(payload)
                if not history_items:
                    reached_boundary = True
                    break

                fingerprint = compute_page_fingerprint(history_items)
                if fingerprint and fingerprint == previous_fingerprint:
                    repeated_fingerprint_count += 1
                else:
                    repeated_fingerprint_count = 0
                previous_fingerprint = fingerprint
                if repeated_fingerprint_count >= 2:
                    raise RuntimeError("history_repeated_page_safety_limit")

                should_stop_after_page = False
                for item in filter_3v3_matches(history_items):
                    match_id = extract_match_id_from_history(item)
                    match_time = extract_match_time_from_history(item)
                    if match_time is not None:
                        if latest_seen_match_time is None or match_time > latest_seen_match_time:
                            latest_seen_match_time = match_time
                        if oldest_synced_match_time is None or match_time < oldest_synced_match_time:
                            oldest_synced_match_time = match_time

                    if stop_time is not None and is_beyond_stop_time(match_time, stop_time):
                        reached_boundary = True
                        should_stop_after_page = True
                        break
                    if is_before_season_start(match_time, self._season_start_time):
                        reached_boundary = True
                        reached_season_start = True
                        should_stop_after_page = True
                        break
                    if match_id is None:
                        continue

                    await renew_worker_heartbeat()
                    marked = await self._repo.mark_match_discovered(
                        match_id=match_id,
                        match_time=match_time,
                        source_identity_id=identity_id,
                        source_identity_key=identity_key,
                        source_server=server,
                        source_role_name=name,
                    )
                    if marked:
                        result["discovered_matches"] += 1
                    detail_result = await self._sync_match_detail(
                        match_id=match_id,
                        match_time=match_time,
                        lease_owner=lease_owner,
                        role_identity_key=identity_key,
                        role_identity_id=identity_id,
                        server=server,
                        name=name,
                        worker_heartbeat_renewer=renew_worker_heartbeat,
                    )
                    if detail_result == "saved":
                        result["saved_details"] += 1
                    elif detail_result == "skipped":
                        result["skipped_details"] += 1
                    elif detail_result == "failed":
                        result["failed_details"] += 1
                    elif detail_result == "unavailable":
                        result["unavailable_details"] += 1
                    await renew_worker_heartbeat()

                if should_stop_after_page:
                    break
                if len(history_items) < self._page_size:
                    reached_boundary = True
                    break
                cursor += self._page_size
                page_index += 1

            if page_index >= self._max_pages_per_role and not reached_boundary:
                raise RuntimeError("history_max_pages_safety_limit")

            history_exhausted = bool(role.get("history_exhausted")) or reached_season_start
            released = await self._release_role_success(
                identity_key=identity_key,
                identity_id=identity_id,
                full_synced_until_time=run_upper_time,
                oldest_synced_match_time=oldest_synced_match_time,
                latest_seen_match_time=latest_seen_match_time,
                history_exhausted=history_exhausted,
                season_id=self._current_season,
                last_cursor=cursor,
                lease_owner=lease_owner,
            )
            if released is False:
                result["error"] = True
                result["stale_lease"] = True
                result["message"] = "stale_role_lease"
            return result
        except JjcSyncGlobalPauseError as exc:
            reason = exc.reason
            logger.warning(
                "JJC 同步触发全局暂停: server={} name={} reason={}".format(
                    server,
                    name,
                    reason,
                )
            )
            await self._pause_for_global_auth_error(
                identity_key,
                reason,
                lease_owner=lease_owner,
                identity_id=identity_id,
            )
            result["error"] = True
            result["global_paused"] = True
            result["message"] = reason
            return result
        except JjcSyncStaleMatchDetailLeaseError as exc:
            logger.warning(
                "JJC 对局详情租约已失效，释放当前角色回队列: server={} name={} reason={}".format(
                    server,
                    name,
                    exc,
                )
            )
            released = await self._release_role_interrupted(
                identity_key=identity_key,
                identity_id=identity_id,
                reason=str(exc),
                requeue=True,
                lease_owner=lease_owner,
            )
            result["error"] = True
            result["stale_detail_lease"] = True
            result["message"] = str(exc)
            if not released:
                result["stale_lease"] = True
            return result
        except JjcSyncMatchDetailClaimError as exc:
            logger.warning(
                "JJC 对局详情领取失败，释放当前角色回队列: server={} name={} reason={}".format(
                    server,
                    name,
                    exc,
                )
            )
            released = await self._release_role_interrupted(
                identity_key=identity_key,
                identity_id=identity_id,
                reason=str(exc),
                requeue=True,
                lease_owner=lease_owner,
            )
            result["error"] = True
            result["interrupted"] = True
            result["error_type"] = "match_detail_claim_failed"
            result["message"] = str(exc)
            if not released:
                result["stale_lease"] = True
            return result
        except JjcSyncStaleRoleLeaseError as exc:
            logger.warning(
                f"JJC 同步角色租约已失效，停止写回角色状态: server={server} name={name} reason={exc}"
            )
            result["error"] = True
            result["stale_lease"] = True
            result["message"] = str(exc)
            return result
        except JjcSyncStaleLeaseError as exc:
            logger.warning(
                f"JJC 同步租约已失效，停止写回角色状态: server={server} name={name} reason={exc}"
            )
            result["error"] = True
            result["stale_lease"] = True
            result["message"] = str(exc)
            return result
        except Exception as exc:
            message = f"{server}/{name} 同步失败: {exc}"
            logger.warning(message)
            released = await self._release_role_failure(
                identity_key,
                message,
                lease_owner=lease_owner,
                identity_id=identity_id,
            )
            result["error"] = True
            result["message"] = message
            if released is False:
                result["stale_lease"] = True
            return result

    async def _resolve_role_identity_for_sync(self, role: Dict[str, Any]) -> Dict[str, Any]:
        server = str(role.get("server") or "").strip()
        name = str(role.get("name") or "").strip()
        if not server or not name or self._inspect_service is None:
            return {}

        resolver = getattr(self._inspect_service, "_resolve_role_identity", None)
        if not callable(resolver):
            return {}

        role_id = str(role.get("role_id") or "").strip()
        zone = str(role.get("zone") or "").strip()
        hints: Dict[str, Any] = {
            "global_id": str(role.get("global_id") or "").strip(),
            "global_role_id": str(role.get("global_role_id") or "").strip(),
            "role_id": role_id,
            "game_role_id": str(role.get("game_role_id") or role_id).strip(),
            "zone": zone,
        }

        try:
            await self._sleep_func()
            identity = await resolver(
                server=server,
                name=name,
                identity_hints=hints,
            )
        except Exception as exc:
            if is_tuilan_auth_error(str(exc)):
                raise JjcSyncGlobalPauseError(
                    build_tuilan_auth_pause_reason(str(exc), context="角色身份补全")
                )
            logger.warning(
                f"JJC 同步角色身份补全失败: server={server} name={name} error={exc}"
            )
            return {}

        if is_tuilan_auth_error(identity):
            raise JjcSyncGlobalPauseError(
                build_tuilan_auth_pause_reason(identity, context="角色身份补全")
            )
        if not isinstance(identity, dict) or identity.get("error"):
            return {}
        if "identity" in identity:
            # Inspect's observation/request metadata stays inside its resolution
            # packet. Sync continues to consume only identity facts and source.
            resolution = identity
            identity = resolution.get("identity")
            if is_tuilan_auth_error(identity):
                raise JjcSyncGlobalPauseError(
                    build_tuilan_auth_pause_reason(identity, context="角色身份补全")
                )
            if not isinstance(identity, dict) or identity.get("error"):
                return {}
            identity = dict(identity)
            source = str(resolution.get("identity_source") or "").strip()
            if source and not identity.get("source"):
                identity["source"] = source
        return identity

    async def _resolve_player_identity_from_local_repo(self, player: Dict[str, Any]) -> Dict[str, Any]:
        if self._identity_repo is None:
            return {}
        server = str(player.get("server") or "").strip()
        name = str(player.get("role_name") or "").strip()
        zone = str(player.get("zone") or "").strip()
        role_id = str(player.get("role_id") or "").strip()
        global_id = str(player.get("global_id") or "").strip()
        global_role_id = str(player.get("global_role_id") or "").strip()
        if not ((server and name) or (zone and role_id) or global_id or global_role_id):
            return {}
        try:
            doc = await self._identity_repo.resolve_best_identity(
                server=server,
                name=name,
                zone=zone or None,
                game_role_id=role_id or None,
                global_id=global_id or None,
                global_role_id=global_role_id or None,
            )
        except Exception as exc:
            logger.warning(
                "JJC 同步通过本地身份库补全玩家身份失败: server={} name={} error={}".format(
                    server, name, exc
                )
            )
            return {}
        if not doc:
            return {}
        if not (server and name) and zone and role_id:
            doc_zone = str(doc.get("zone") or "").strip()
            doc_role_id = str(doc.get("role_id") or doc.get("game_role_id") or "").strip()
            if doc_zone != zone or doc_role_id != role_id:
                return {}
        return {
            "global_id": str(doc.get("global_id") or "").strip(),
            "global_role_id": str(doc.get("global_role_id") or "").strip(),
            "role_id": str(doc.get("role_id") or doc.get("game_role_id") or "").strip(),
            "game_role_id": str(doc.get("game_role_id") or doc.get("role_id") or "").strip(),
            "person_id": str(doc.get("person_id") or "").strip(),
            "zone": str(doc.get("zone") or "").strip(),
            "server": str(doc.get("server") or "").strip(),
            "role_name": str(doc.get("role_name") or doc.get("name") or "").strip(),
            "role_info_observed_match_time": _coerce_int(doc.get("role_info_observed_match_time")),
            "source": "local_identity",
        }

    @staticmethod
    def _backfill_player_from_identity(player: Dict[str, Any], identity: Dict[str, Any]) -> None:
        for source_key, target_key in (
            ("global_id", "global_id"),
            ("global_role_id", "global_role_id"),
            ("role_id", "role_id"),
            ("game_role_id", "role_id"),
            ("zone", "zone"),
            ("person_id", "person_id"),
        ):
            value = str(identity.get(source_key) or "").strip()
            if value and not str(player.get(target_key) or "").strip():
                player[target_key] = value
        server_value = str(identity.get("server") or "").strip()
        if server_value and not str(player.get("server") or "").strip():
            player["server"] = server_value
        normalized_server = str(player.get("server") or identity.get("server") or "").strip()
        name_value = normalize_role_name(
            identity.get("role_name") or identity.get("name"),
            normalized_server,
        )
        if name_value and not str(player.get("role_name") or "").strip():
            player["role_name"] = name_value
        # 回填后统一做一次规范化，确保不会残留带服务器后缀的角色名
        current_name = str(player.get("role_name") or "").strip()
        current_server = str(player.get("server") or "").strip()
        if current_name and current_server:
            player["role_name"] = normalize_role_name(current_name, current_server)

    async def _upsert_role_identity_from_resolved(
        self,
        server: str,
        name: str,
        identity: Dict[str, Any],
        observed_match_time: Optional[int] = None,
    ) -> None:
        if self._identity_repo is None:
            return

        global_id = str(identity.get("global_id") or "").strip() or None
        global_role_id = str(identity.get("global_role_id") or "").strip() or None
        role_id = str(identity.get("role_id") or identity.get("game_role_id") or "").strip() or None
        person_id = str(identity.get("person_id") or "").strip() or None
        zone = str(identity.get("zone") or "").strip() or None
        if not global_id and not global_role_id and not role_id and not zone and not person_id:
            return
        observed_at = (
            datetime.fromtimestamp(observed_match_time, tz=timezone.utc)
            if observed_match_time is not None
            else None
        )

        try:
            await self._identity_repo.upsert_from_match_detail(
                server=server,
                name=name,
                zone=zone,
                game_role_id=role_id,
                global_id=global_id,
                global_role_id=global_role_id,
                role_id=role_id,
                person_id=person_id,
                observed_at=observed_at,
                observed_match_time=observed_match_time,
            )
        except Exception as exc:
            logger.warning(
                f"JJC 同步写入角色身份表失败: server={server} name={name} error={exc}"
            )

    def _resolve_stop_time(self, role: Dict[str, Any], mode: str) -> Optional[int]:
        queue_sync_until_time = _coerce_int(role.get("queue_sync_until_time"))
        if queue_sync_until_time is not None:
            return queue_sync_until_time
        if mode == "full":
            return None
        full_synced_until_time = _coerce_int(role.get("full_synced_until_time"))
        if mode == "incremental":
            return full_synced_until_time
        return full_synced_until_time

    async def _sync_match_detail(
        self,
        match_id: int,
        match_time: Optional[int],
        lease_owner: str,
        role_identity_key: str = "",
        role_identity_id: Optional[Any] = None,
        server: str = "",
        name: str = "",
        max_attempts: int = 3,
        worker_heartbeat_renewer: Optional[Callable[[bool], Awaitable[None]]] = None,
    ) -> str:
        last_role_lease_renewed_at = 0.0
        last_detail_lease_renewed_at = 0.0

        if role_identity_key:
            last_role_lease_renewed_at = await self._renew_role_lease_if_due(
                role_identity_key,
                lease_owner,
                last_role_lease_renewed_at,
                force=True,
                identity_id=role_identity_id,
            )

        try:
            claim = await self._repo.claim_match_detail(
                match_id=match_id,
                lease_owner=lease_owner,
                lease_seconds=self._lease_seconds,
            )
        except Exception as exc:
            logger.warning(
                "JJC 对局详情领取失败，中断当前角色等待后续重试: match_id={} owner={} error={}".format(
                    match_id,
                    lease_owner,
                    exc,
                )
            )
            raise JjcSyncMatchDetailClaimError(
                "match_detail_claim_failed: match_id={} owner={} error={}".format(
                    match_id,
                    lease_owner,
                    exc,
                )
            )
        if claim is None:
            try:
                detail_state = await self._repo.get_match_detail_sync_state(match_id)
            except Exception as exc:
                logger.warning(
                    "JJC 对局详情状态查询失败，中断当前角色等待后续重试: match_id={} owner={} error={}".format(
                        match_id,
                        lease_owner,
                        exc,
                    )
                )
                raise JjcSyncMatchDetailClaimError(
                    "match_detail_state_check_failed: match_id={} owner={} error={}".format(
                        match_id,
                        lease_owner,
                        exc,
                    )
                )
            status = str(detail_state.get("status") or "unknown")
            action = str(detail_state.get("action") or "")
            if action == "skip":
                return "skipped"
            raise JjcSyncMatchDetailClaimError(
                "match_detail_not_claimable: match_id={} owner={} status={} action={}".format(
                    match_id,
                    lease_owner,
                    status,
                    action or "unknown",
                )
            )

        last_error = ""

        async def renew_role_context(force: bool = False) -> None:
            nonlocal last_role_lease_renewed_at
            if not role_identity_key:
                return
            last_role_lease_renewed_at = await self._renew_role_lease_if_due(
                role_identity_key,
                lease_owner,
                last_role_lease_renewed_at,
                force=force,
                identity_id=role_identity_id,
            )

        async def renew_detail_context(force: bool = False) -> None:
            nonlocal last_detail_lease_renewed_at
            last_detail_lease_renewed_at = await self._renew_match_detail_lease_if_due(
                match_id,
                lease_owner,
                last_detail_lease_renewed_at,
                force=force,
            )

        async def renew_processing_context(force: bool = False) -> None:
            await renew_role_context(force=force)
            await renew_detail_context(force=force)
            if worker_heartbeat_renewer is not None:
                await worker_heartbeat_renewer(force)

        for attempt in range(1, max_attempts + 1):
            try:
                await renew_processing_context(force=attempt == 1)
                await self._sleep_func()
                payload = await self._inspect_service.get_match_detail(match_id=match_id)
                if is_tuilan_auth_error(payload):
                    raise JjcSyncGlobalPauseError(
                        build_tuilan_auth_pause_reason(payload, context="对局详情")
                    )
                if not isinstance(payload, dict) or payload.get("error"):
                    message = payload.get("message") if isinstance(payload, dict) else "invalid_detail_response"
                    last_error = str(message)
                else:
                    detail = payload.get("detail")
                    if detail is None:
                        reason = str(payload.get("message") or "no data found")
                        code = payload.get("code") if payload.get("code") is not None else -1
                        await self._persist_sync_match_detail(
                            match_id=match_id,
                            payload=self._build_sync_persistence_payload(payload, source_changed=False),
                            unavailable=True,
                        )
                        await renew_processing_context(force=True)
                        marked = await self._repo.mark_match_detail_unavailable(
                            match_id,
                            reason=reason,
                            code=code,
                            lease_owner=lease_owner,
                        )
                        if not marked:
                            raise JjcSyncStaleMatchDetailLeaseError(
                                "stale_match_detail_lease: match_id={} owner={}".format(match_id, lease_owner)
                            )
                        return "unavailable"
                    if isinstance(detail, dict):
                        detail_payload = dict(payload)
                        detail_payload["detail"] = detail
                        source_changed = False
                        if match_time is not None and _coerce_int(detail_payload.get("match_time")) is None:
                            detail_payload["match_time"] = match_time
                            source_changed = True
                        await renew_processing_context(force=True)
                        await self._enrich_detail_with_replay(
                            detail,
                            match_id,
                            replay_data=payload.get("replay"),
                            lease_renewer=renew_processing_context,
                        )
                        await renew_processing_context(force=True)
                        await self._persist_sync_match_detail(
                            match_id=match_id,
                            payload=self._build_sync_persistence_payload(
                                detail_payload,
                                source_changed=source_changed,
                            ),
                            unavailable=False,
                        )
                    else:
                        last_error = "invalid_detail_response"
                        continue
                    await renew_processing_context(force=True)
                    marked = await self._repo.mark_match_detail_saved(match_id, lease_owner=lease_owner)
                    if not marked:
                        raise JjcSyncStaleMatchDetailLeaseError(
                            "stale_match_detail_lease: match_id={} owner={}".format(match_id, lease_owner)
                        )
                    await self._refresh_match_detail_participant_sync_status_if_configured(match_id=match_id)
                    return "saved"
            except JjcSyncGlobalPauseError as exc:
                await self._repo.release_match_detail_interrupted(
                    match_id,
                    reason=exc.reason,
                    lease_owner=lease_owner,
                )
                raise
            except JjcSyncStaleLeaseError:
                raise
            except Exception as exc:
                if is_tuilan_auth_error(str(exc)):
                    raise JjcSyncGlobalPauseError(
                        build_tuilan_auth_pause_reason(str(exc), context="对局详情")
                    )
                last_error = str(exc)

        marked = await self._repo.mark_match_detail_failed(
            match_id,
            last_error,
            lease_owner=lease_owner,
        )
        if not marked:
            raise JjcSyncStaleMatchDetailLeaseError(
                "stale_match_detail_lease: match_id={} owner={}".format(match_id, lease_owner)
            )
        return "failed"

    def _build_sync_persistence_payload(
        self,
        payload: Dict[str, Any],
        *,
        source_changed: bool,
    ) -> Dict[str, Any]:
        data = {
            key: copy.deepcopy(value)
            for key, value in payload.items()
            if key not in ("cache", "projection_error", "projection_message")
        }
        cached_at = payload.get("cached_at")
        cache = payload.get("cache")
        if isinstance(cache, dict) and cache.get("cached_at") is not None:
            cached_at = cache.get("cached_at")
        try:
            old_cached_at = float(cached_at)
        except (TypeError, ValueError):
            old_cached_at = 0.0
        if source_changed:
            cached_at = max(time.time(), old_cached_at + 0.000001)
        elif cached_at is None:
            cached_at = time.time()
        return {"cached_at": cached_at, "data": data}

    async def _persist_sync_match_detail(
        self,
        *,
        match_id: int,
        payload: Dict[str, Any],
        unavailable: bool,
    ) -> None:
        service = self._match_detail_persistence_service
        if service is not None:
            result = await service.persist(match_id, payload, "sync_worker")
            if getattr(result, "main_saved", None) is not True:
                raise RuntimeError(
                    "match_detail_persistence_main_save_failed: match_id={}".format(match_id)
                )
            return

        raise RuntimeError(
            "match_detail_persistence_service is required when inspect_service.cache_repo is unavailable"
        )

    async def _project_match_detail_if_configured(
        self,
        match_id: int,
        payload: Dict[str, Any],
    ) -> None:
        if self._match_detail_projection_service is None:
            return
        projector = getattr(self._match_detail_projection_service, "project_payload", None)
        if not callable(projector):
            return
        try:
            result = projector(
                match_id=match_id,
                payload=payload,
                source="match_detail_sync",
                priority=-10,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("JJC 对局详情身份投影失败: match_id={} error={}".format(match_id, exc))

    async def _project_match_detail_participants_if_configured(
        self,
        match_id: int,
        payload: Dict[str, Any],
    ) -> None:
        service = self._match_detail_participant_projection_service
        if service is None:
            return
        projector = getattr(service, "project_payload", None)
        if not callable(projector):
            return
        try:
            result = projector(
                match_id=match_id,
                payload=payload,
                source="sync_worker",
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("JJC 对局详情参与者投影失败: match_id={} error={}".format(match_id, exc))

    async def _clear_match_detail_participants_if_configured(self, match_id: int) -> None:
        service = self._match_detail_participant_projection_service
        if service is None:
            return
        clearer = getattr(service, "clear_match", None)
        if not callable(clearer):
            return
        try:
            result = clearer(match_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("JJC 对局详情参与者投影清理失败: match_id={} error={}".format(match_id, exc))

    async def _refresh_match_detail_participant_sync_status_if_configured(self, match_id: int) -> None:
        service = self._match_detail_participant_projection_service
        if service is None:
            return
        refresher = getattr(service, "refresh_sync_status", None)
        if not callable(refresher):
            return
        try:
            result = refresher(match_id=match_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("JJC 对局详情参与者同步状态刷新失败: match_id={} error={}".format(match_id, exc))

    async def _enrich_detail_with_replay(
        self,
        detail: dict,
        match_id: int,
        replay_data: Optional[dict] = None,
        lease_renewer: Optional[Callable[[bool], Awaitable[None]]] = None,
    ) -> None:
        """从 replay 接口获取回放数据，按 global_id / role_id / normalized name 合并到 detail players。

        replay players[].global_role_id 是数字字符串，只写入 detail player.global_id，
        绝不写入 SK01 字段 global_role_id。
        """
        if replay_data is None and self._match_replay_client is None:
            return
        if replay_data is None:
            try:
                if lease_renewer is not None:
                    await lease_renewer(False)
                replay_data = await asyncio.to_thread(
                    self._match_replay_client.get_match_replay,
                    match_id=match_id,
                )
                if lease_renewer is not None:
                    await lease_renewer(False)
            except Exception as exc:
                if is_tuilan_auth_error(str(exc)):
                    raise JjcSyncGlobalPauseError(
                        build_tuilan_auth_pause_reason(str(exc), context="match-replay")
                    )
                logger.warning(f"JJC replay 请求异常: match_id={match_id}")
                return

        if is_tuilan_auth_error(replay_data):
            raise JjcSyncGlobalPauseError(
                build_tuilan_auth_pause_reason(replay_data, context="match-replay")
            )
        if not isinstance(replay_data, dict) or replay_data.get("error"):
            return

        data = replay_data.get("data")
        replay_players: list[dict] = []
        if isinstance(data, dict):
            players_raw = data.get("players")
            if isinstance(players_raw, list):
                replay_players = [p for p in players_raw if isinstance(p, dict)]

        if not replay_players:
            return

        replay_by_global_id: Dict[str, Optional[Dict[str, Any]]] = {}
        replay_by_role_id: Dict[str, Optional[Dict[str, Any]]] = {}
        replay_by_name: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}

        def _put_unique(
            lookup: Dict[Any, Optional[Dict[str, Any]]],
            key: Any,
            replay_player: Dict[str, Any],
        ) -> None:
            if key in lookup:
                existing = lookup.get(key)
                if existing is not None and existing is not replay_player:
                    lookup[key] = None
                return
            lookup[key] = replay_player

        for rp in replay_players:
            replay_global_id = str(rp.get("global_role_id") or "").strip()
            replay_role_id = str(rp.get("role_id") or "").strip()
            replay_name_key = build_player_match_key(rp.get("role_name"), rp.get("server"))
            if replay_global_id:
                _put_unique(replay_by_global_id, replay_global_id, rp)
            if replay_role_id:
                _put_unique(replay_by_role_id, replay_role_id, rp)
            if replay_name_key is not None:
                _put_unique(replay_by_name, replay_name_key, rp)

        for team_key in ("team1", "team2"):
            team = detail.get(team_key)
            if not isinstance(team, dict):
                continue
            players_info = team.get("players_info")
            if not isinstance(players_info, list):
                continue
            for player in players_info:
                if not isinstance(player, dict):
                    continue
                player_global_id = str(player.get("global_id") or "").strip()
                player_role_id = str(player.get("role_id") or "").strip()
                key = build_player_match_key(player.get("role_name"), player.get("server"))
                rp: Optional[Dict[str, Any]] = None
                if player_global_id:
                    rp = replay_by_global_id.get(player_global_id)
                if rp is None and player_role_id:
                    candidate = replay_by_role_id.get(player_role_id)
                    if candidate is not None and self._replay_candidate_matches_detail_player(candidate, player):
                        rp = candidate
                if rp is None and key is not None:
                    rp = replay_by_name.get(key)
                if rp is None:
                    continue
                # 从 replay 回填 global_id / role_id / zone / server，绝不回填 SK01 global_role_id
                replay_server, _ = split_replay_role_name(rp.get("role_name"), rp.get("server"))
                replay_global_id = str(rp.get("global_role_id") or "").strip()
                if replay_global_id and not str(player.get("global_id") or "").strip():
                    player["global_id"] = replay_global_id
                for field_name in ("role_id", "zone"):
                    rp_val = str(rp.get(field_name) or "").strip()
                    if rp_val and not str(player.get(field_name) or "").strip():
                        player[field_name] = rp_val
                if replay_server and not str(player.get("server") or "").strip():
                    player["server"] = replay_server

    @staticmethod
    def _replay_candidate_matches_detail_player(
        replay_player: Dict[str, Any],
        detail_player: Dict[str, Any],
    ) -> bool:
        """Role ID 命中时，若两边都有可比对的 server/name，也必须一致。"""
        replay_server, replay_name = split_replay_role_name(
            replay_player.get("role_name"),
            replay_player.get("server"),
        )
        detail_server, detail_name = split_replay_role_name(
            detail_player.get("role_name"),
            detail_player.get("server"),
        )
        if replay_name and detail_name and replay_name.lower() != detail_name.lower():
            return False
        if replay_server and detail_server and replay_server.lower() != detail_server.lower():
            return False
        return True

    async def _enrich_detail_with_indicator(
        self,
        detail: dict,
        match_time: Optional[int] = None,
        lease_renewer: Optional[Callable[[bool], Awaitable[None]]] = None,
    ) -> None:
        """对 detail 中有 role_id + zone + server 且缺少 SK01 global_role_id 的玩家
        请求 indicator 接口，回填 SK01 global_role_id 和 person_id。

        若本地身份表已有不早于当前对局时间的 SK01 global_role_id，则直接复用本地身份，
        避免同一批玩家在多场对局中反复请求 indicator。

        若 detail 已有 person_id 且与 indicator 返回的不一致，保留 detail 的并 log warning。
        """
        detail_match_time = _coerce_int(match_time)

        for team_key in ("team1", "team2"):
            team = detail.get(team_key)
            if not isinstance(team, dict):
                continue
            players_info = team.get("players_info")
            if not isinstance(players_info, list):
                continue
            for player in players_info:
                if not isinstance(player, dict):
                    continue
                role_id = str(player.get("role_id") or "").strip()
                zone = str(player.get("zone") or "").strip()
                server = str(player.get("server") or "").strip()
                existing_global = str(player.get("global_role_id") or "").strip()

                if existing_global:
                    continue
                if not (role_id and zone and server):
                    continue

                local_identity = await self._resolve_player_identity_from_local_repo(player)
                if self._local_identity_can_skip_indicator(local_identity, detail_match_time):
                    self._backfill_player_from_identity(player, local_identity)
                    continue
                if self._role_indicator_client is None:
                    continue

                if lease_renewer is not None:
                    await lease_renewer(False)
                await self._sleep_func()
                if lease_renewer is not None:
                    await lease_renewer(False)
                try:
                    indicator_data = await asyncio.to_thread(
                        self._role_indicator_client.get_role_indicator,
                        role_id=role_id,
                        zone=zone,
                        server=server,
                    )
                    if lease_renewer is not None:
                        await lease_renewer(False)
                except Exception as exc:
                    if is_tuilan_auth_error(str(exc)):
                        raise JjcSyncGlobalPauseError(
                            build_tuilan_auth_pause_reason(str(exc), context="role-indicator")
                        )
                    continue

                if is_tuilan_auth_error(indicator_data):
                    raise JjcSyncGlobalPauseError(
                        build_tuilan_auth_pause_reason(indicator_data, context="role-indicator")
                    )
                if not isinstance(indicator_data, dict) or indicator_data.get("error"):
                    continue

                indicator_payload = indicator_data.get("data")
                if not isinstance(indicator_payload, dict):
                    indicator_payload = indicator_data

                role_info = indicator_payload.get("role_info")
                if isinstance(role_info, dict):
                    sk01_global = str(role_info.get("global_role_id") or "").strip()
                    ind_role_id = str(role_info.get("role_id") or "").strip()
                    if ind_role_id and ind_role_id != role_id:
                        logger.warning(
                            "JJC indicator role_id 冲突: requested={} indicator={} server={}".format(
                                role_id,
                                ind_role_id,
                                server,
                            )
                        )
                        continue
                    if sk01_global.startswith("SK01-"):
                        player["global_role_id"] = sk01_global
                    if ind_role_id and not str(player.get("role_id") or "").strip():
                        player["role_id"] = ind_role_id
                    ind_zone = str(role_info.get("zone") or "").strip()
                    if ind_zone and not str(player.get("zone") or "").strip():
                        player["zone"] = ind_zone
                    ind_server = str(role_info.get("server") or "").strip()
                    if ind_server and not str(player.get("server") or "").strip():
                        player["server"] = ind_server

                person_info = indicator_payload.get("person_info")
                if isinstance(person_info, dict):
                    ind_person_id = str(person_info.get("person_id") or "").strip()
                    existing_person_id = str(player.get("person_id") or "").strip()
                    if ind_person_id:
                        if not existing_person_id:
                            player["person_id"] = ind_person_id
                        elif existing_person_id != ind_person_id:
                            logger.warning(
                                "JJC indicator person_id 冲突: detail={} indicator={} role_id={} server={}".format(
                                    existing_person_id,
                                    ind_person_id,
                                    role_id,
                                    server,
                                )
                            )

    @staticmethod
    def _local_identity_can_skip_indicator(
        identity: Dict[str, Any],
        match_time: Optional[int],
    ) -> bool:
        global_role_id = str(identity.get("global_role_id") or "").strip()
        if not global_role_id.startswith("SK01-"):
            return False
        observed_match_time = _coerce_int(identity.get("role_info_observed_match_time"))
        return (
            match_time is None
            or observed_match_time is None
            or match_time <= observed_match_time
        )

    async def _enqueue_players_from_detail(
        self,
        detail: Dict[str, Any],
        fallback_match_time: Optional[int] = None,
        lease_renewer: Optional[Callable[[bool], Awaitable[None]]] = None,
    ) -> None:
        detail_match_time = _coerce_int(detail.get("match_time")) or fallback_match_time
        for player in extract_players_from_detail(detail):
            if lease_renewer is not None:
                await lease_renewer(False)
            if not str(player.get("global_role_id") or "").strip():
                if lease_renewer is not None:
                    await lease_renewer(False)
                identity = await self._resolve_player_identity_from_local_repo(player)
                if lease_renewer is not None:
                    await lease_renewer(False)
                if identity:
                    self._backfill_player_from_identity(player, identity)
            server = str(player.get("server") or "").strip()
            name = normalize_role_name(
                str(player.get("role_name") or "").strip(), server
            )
            if not server or not name:
                continue
            if lease_renewer is not None:
                await lease_renewer(False)
            await self._upsert_role_identity_from_resolved(
                server,
                name,
                {
                    "global_role_id": str(player.get("global_role_id") or "").strip(),
                    "global_id": str(player.get("global_id") or "").strip(),
                    "role_id": str(player.get("role_id") or "").strip(),
                    "game_role_id": str(player.get("role_id") or "").strip(),
                    "person_id": str(player.get("person_id") or "").strip(),
                    "zone": str(player.get("zone") or "").strip(),
                },
                observed_match_time=detail_match_time,
            )
            global_role_id = str(player.get("global_role_id") or "").strip()
            if not global_role_id:
                logger.warning(
                    "JJC 对局玩家缺少 SK01 global_role_id，跳过写入可执行同步队列: server={} name={}".format(
                        server,
                        name,
                    )
                )
                continue
            if lease_renewer is not None:
                await lease_renewer(False)
            await self._repo.upsert_role(
                server=server,
                name=name,
                normalized_server=server,
                normalized_name=name,
                global_id=str(player.get("global_id") or "").strip() or None,
                global_role_id=global_role_id,
                role_id=str(player.get("role_id") or "").strip() or None,
                person_id=str(player.get("person_id") or "").strip() or None,
                zone=str(player.get("zone") or "").strip() or None,
                source="match_detail",
                priority=-10,
                season_id=self._current_season,
                season_start_time=self._season_start_time,
                observed_match_time=detail_match_time,
            )
            if lease_renewer is not None:
                await lease_renewer(False)

    async def _upsert_identity_queue_candidate_for_role(
        self,
        server: str,
        name: str,
        normalized_server: str,
        normalized_name: str,
        global_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        zone: Optional[str] = None,
        source: str = "manual",
        priority: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Create/update role identity and queue candidate, preferring identity-id queue APIs."""
        if self._identity_repo is not None and hasattr(self._identity_repo, "upsert_from_match_detail_with_id"):
            identity = await self._identity_repo.upsert_from_match_detail_with_id(
                server=normalized_server,
                name=normalized_name,
                zone=(zone or "").strip() or None,
                game_role_id=(role_id or "").strip() or None,
                global_id=(global_id or "").strip() or None,
                global_role_id=(global_role_id or "").strip() or None,
                role_id=(role_id or "").strip() or None,
                observed_match_time=None,
            )
            if isinstance(identity, dict):
                identity_id = self._extract_identity_id(identity)
                if identity_id is not None and hasattr(self._repo, "upsert_identity_queue_candidate"):
                    queued = await self._repo.upsert_identity_queue_candidate(
                        identity_id=identity_id,
                        identity_key=identity.get("identity_key"),
                        server=identity.get("server") or server,
                        name=identity.get("name") or name,
                        normalized_server=identity.get("normalized_server") or normalized_server,
                        normalized_name=identity.get("normalized_name") or normalized_name,
                        global_id=identity.get("global_id") or global_id,
                        global_role_id=identity.get("global_role_id") or global_role_id,
                        role_id=identity.get("role_id") or role_id,
                        game_role_id=identity.get("game_role_id") or role_id,
                        person_id=identity.get("person_id"),
                        zone=identity.get("zone") or zone,
                        source=source,
                        priority=priority,
                        season_id=self._current_season,
                        season_start_time=self._season_start_time,
                    )
                    return identity if queued else None

        await self._upsert_role_identity_from_resolved(
            normalized_server,
            normalized_name,
            {
                "global_id": global_id or "",
                "global_role_id": global_role_id or "",
                "role_id": role_id or "",
                "game_role_id": role_id or "",
                "zone": zone or "",
            },
        )
        identity_key = await self._repo.upsert_role(
            server=server,
            name=name,
            normalized_server=normalized_server,
            normalized_name=normalized_name,
            global_id=global_id,
            global_role_id=global_role_id,
            role_id=role_id,
            zone=zone,
            source=source,
            priority=priority,
            season_id=self._current_season,
            season_start_time=self._season_start_time,
        )
        return {"identity_key": identity_key} if identity_key else None

    async def add_role(
        self,
        server: str,
        name: str,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        zone: Optional[str] = None,
        source: str = 'manual',
        global_id: Optional[str] = None,
        priority: int = 100,
        queue: bool = False,
        mode: str = "full",
        queue_sync_until_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """添加角色到同步队列候选池，可选择立即排队。"""
        normalized_server = server.strip()
        normalized_name = name.strip()
        if not normalized_server or not normalized_name:
            return {"error": True, "message": "服务器和角色名不能为空"}
        if not is_supported_queue_mode(mode):
            return {"error": True, "message": "invalid_mode"}

        try:
            identity_doc = await self._upsert_identity_queue_candidate_for_role(
                server=server,
                name=name,
                normalized_server=normalized_server,
                normalized_name=normalized_name,
                global_id=global_id,
                global_role_id=global_role_id,
                role_id=role_id,
                zone=zone,
                source=source,
                priority=priority,
            )
            identity_key = str(identity_doc.get("identity_key") or "") if identity_doc else ""
            identity_id = self._extract_identity_id(identity_doc or {})
            if not identity_key:
                return {"error": True, "message": "添加角色失败"}
            queued = False
            if queue:
                if identity_id is not None and hasattr(self._repo, "enqueue_identity"):
                    queued_doc = await self._repo.enqueue_identity(
                        identity_id=identity_id,
                        mode=mode,
                        source=source,
                        batch_id=f"jjc-sync-manual:{uuid.uuid4()}",
                        queue_sync_until_time=queue_sync_until_time,
                    )
                else:
                    queued_doc = await self._repo.enqueue_role(
                        identity_key=identity_key,
                        mode=mode,
                        source=source,
                        batch_id=f"jjc-sync-manual:{uuid.uuid4()}",
                        queue_sync_until_time=queue_sync_until_time,
                    )
                queued = bool(queued_doc)
            return {
                "error": False,
                "message": (
                    f"角色 {server}/{name} 已加入排队队列"
                    if queued
                    else f"角色 {server}/{name} 已加入同步队列"
                ),
                "identity_key": identity_key,
                "identity_id": str(identity_id or "") if identity_id is not None else None,
                "priority": priority,
                "queued": queued,
            }
        except Exception as exc:
            logger.warning(f"add_role 失败: server={server} name={name} error={exc}")
            return {"error": True, "message": f"添加角色失败: {exc}"}

    async def sync_single_role(
        self,
        server: str,
        name: str,
        global_role_id: Optional[str] = None,
        role_id: Optional[str] = None,
        zone: Optional[str] = None,
        global_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """直接同步单个角色的对局数据，不入队列排队，立即执行。"""
        normalized_server = server.strip()
        normalized_name = name.strip()
        if not normalized_server or not normalized_name:
            return {"error": True, "message": "服务器和角色名不能为空"}

        if self._match_history_client is None or self._inspect_service is None:
            return {"error": True, "message": "sync_dependencies_not_configured"}

        try:
            identity_doc = await self._upsert_identity_queue_candidate_for_role(
                server=server,
                name=name,
                normalized_server=normalized_server,
                normalized_name=normalized_name,
                global_id=global_id,
                global_role_id=global_role_id,
                role_id=role_id,
                zone=zone,
                source="manual",
                priority=100,
            )
            identity_key = str(identity_doc.get("identity_key") or "") if identity_doc else ""
            identity_id = self._extract_identity_id(identity_doc or {})
            if not identity_key:
                return {"error": True, "message": "添加角色失败"}

            lease_owner = f"jjc-sync-single:{uuid.uuid4()}"
            if identity_id is not None and hasattr(self._repo, "claim_specific_identity"):
                claimed = await self._repo.claim_specific_identity(
                    identity_id=identity_id,
                    lease_owner=lease_owner,
                    lease_seconds=self._lease_seconds,
                )
            else:
                claimed = await self._repo.claim_specific_role(
                    identity_key=identity_key,
                    lease_owner=lease_owner,
                    lease_seconds=self._lease_seconds,
                )
            if claimed is None:
                return {"error": True, "message": f"角色 {server}/{name} 正在冷却中或被其他任务同步，请稍后再试"}

            role: Dict[str, Any] = {
                "identity_id": identity_id,
                "identity_key": identity_key,
                "server": normalized_server,
                "name": normalized_name,
                "global_id": global_id or "",
                "global_role_id": global_role_id or "",
                "role_id": role_id or "",
                "zone": zone or "",
            }
            result = await self._sync_one_role(
                role=role,
                mode="full",
                lease_owner=lease_owner,
            )
            return result
        except Exception as exc:
            logger.warning(f"sync_single_role 失败: server={server} name={name} error={exc}")
            return {"error": True, "message": f"单人同步失败: {exc}"}

    async def pause(self, reason: str = '') -> Dict[str, Any]:
        """暂停全局同步。"""
        success = await self._repo.set_paused(True, reason)
        if success:
            return {"error": False, "message": "同步已暂停" + (f"（{reason}）" if reason else "")}
        return {"error": True, "message": "暂停同步失败"}

    async def resume(self) -> Dict[str, Any]:
        """恢复全局同步。"""
        success = await self._repo.set_paused(False)
        if success:
            return {"error": False, "message": "同步已恢复"}
        return {"error": True, "message": "恢复同步失败"}

    async def status(self) -> Dict[str, Any]:
        """查询同步状态。"""
        try:
            return await self._build_status_summary()
        except Exception as exc:
            logger.warning(f"status 查询失败: error={exc}")
            return {"error": True, "message": f"查询状态失败: {exc}"}

    async def reset_role(self, server: str, name: str) -> Dict[str, Any]:
        """重置角色同步进度。"""
        normalized_server = server.strip()
        normalized_name = name.strip()
        if not normalized_server or not normalized_name:
            return {"error": True, "message": "服务器和角色名不能为空"}

        role = await self._repo.get_role_by_name(normalized_server, normalized_name)
        if role is None:
            return {"error": True, "message": f"未找到角色 {server}/{name}"}

        identity_key = role.get("identity_key", "")
        identity_id = self._extract_identity_id(role)
        if identity_id is not None and hasattr(self._repo, "reset_identity_progress"):
            success = await self._repo.reset_identity_progress(identity_id)
        else:
            success = await self._repo.reset_role_progress(identity_key)
        if success:
            return {
                "error": False,
                "message": f"角色 {server}/{name} 同步进度已重置",
                "identity_key": identity_key,
                "identity_id": str(identity_id or "") if identity_id is not None else None,
            }
        return {"error": True, "message": f"重置角色 {server}/{name} 同步进度失败"}
