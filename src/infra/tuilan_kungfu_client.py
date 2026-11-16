"""Thin cancellable adapter for the two requests needed by kungfu refresh."""
import asyncio
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from src.infra.http_client import HttpClient
from src.utils.tuilan_request import build_tuilan_request
from src.utils.jjc_display_identity import indicator_identity, needs_replay


class KungfuQueryError(Exception):
    def __init__(self, code: str, retry_after: Optional[float] = None, auth: bool = False):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.auth = auth


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


class TuilanKungfuClient:
    """Return raw evidence; the service owns the kungfu selection policy."""

    def __init__(
        self, *, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        request_delay: Tuple[float, float] = (1, 3), request_timeout: float = 10,
        attempt_timeout: Optional[float] = None, http_client: Optional[Any] = None,
        display_enabled: bool = False,
    ) -> None:
        attempt_timeout = attempt_timeout if attempt_timeout is not None else (60 if display_enabled else 25)
        if (len(request_delay) != 2 or not all(math.isfinite(value) for value in
                    (*request_delay, request_timeout, attempt_timeout))
                or not 0 <= request_delay[0] <= request_delay[1]
                or request_timeout <= 0 or attempt_timeout <= 0):
            raise ValueError("invalid kungfu request budget")
        self.clock, self.sleep = clock, sleep
        self.request_delay = request_delay
        self.request_timeout, self.attempt_timeout = request_timeout, attempt_timeout
        self.http_client = http_client if http_client is not None else HttpClient(
            timeout=request_timeout, retries=0, verify=False)
        self._query_lock: Optional[asyncio.Lock] = None
        self.display_enabled = display_enabled
        self._deadline: Optional[float] = None

    async def query(self, identity: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
        # Foreground misses and background refresh share the same request path.
        # The attempt budget starts once this role owns it, not while waiting
        # for another role to finish its bounded attempt.
        if self._query_lock is None:
            self._query_lock = asyncio.Lock()
        async with self._query_lock:
            try:
                if self.display_enabled:
                    self._deadline = asyncio.get_running_loop().time() + self.attempt_timeout
                    return await self._query(identity, scope)
                return await asyncio.wait_for(self._query(identity, scope), self.attempt_timeout)
            except asyncio.TimeoutError as exc:
                raise KungfuQueryError("attempt_timeout") from exc
            finally:
                self._deadline = None

    async def _request(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._deadline is not None:
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise KungfuQueryError("attempt_timeout")
            try:
                return await asyncio.wait_for(self._request_once(url, params), remaining)
            except asyncio.TimeoutError as exc:
                raise KungfuQueryError("attempt_timeout") from exc
        return await self._request_once(url, params)

    async def _request_once(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        # Match ranking lookups: wait a random 1–3 seconds before each request,
        # including the first request and requests for the next queued role.
        delay = random.uniform(*self.request_delay)
        if delay > 0:
            await self.sleep(delay)
        headers, body = build_tuilan_request(params)
        try:
            result = await asyncio.wait_for(
                self.http_client.arequest_json(
                    "POST", url, headers=headers, content=body.encode("utf-8"),
                    timeout=self.request_timeout, verify=False),
                self.request_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise KungfuQueryError("request_timeout") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The queue logs error codes, never raw upstream bodies or headers.
            raise KungfuQueryError("request_failed") from exc
        return result

    def _retry_after(self, value: Any) -> float:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(str(value))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - self.clock()
            except (TypeError, ValueError, OverflowError):
                seconds = 300
        if not math.isfinite(seconds):
            seconds = 300
        return min(3600, max(1, seconds))

    def _error(self, response: Any) -> Optional[KungfuQueryError]:
        if not isinstance(response, dict):
            return KungfuQueryError("invalid_response")
        status = response.get("status_code")
        code = response.get("code")
        if status == 429 or code == 429:
            return KungfuQueryError("rate_limited", self._retry_after(response.get("retry_after")))
        failed = bool(response.get("error")) or status not in (None, 0, 200) or code != 0
        message = _text(response.get("msg") or response.get("message")).lower()
        if (status in (401, 403) or code in (401, 403)
                or (failed and any(marker in message for marker in (
                    "请重新登录", "请先登录", "未登录", "登录失效", "登录过期", "票据失效", "票据错误",
                    "ticket invalid", "ticket expired", "invalid ticket", "expired ticket",
                    "token invalid", "token expired", "not logged in")))):
            return KungfuQueryError("authentication_failed", 300, auth=True)
        if failed:
            error = response.get("error")
            # Never propagate arbitrary upstream text into structured error logs.
            if error in ("request_failed", "invalid_json"):
                return KungfuQueryError(error)
            if isinstance(status, int) and 400 <= status <= 599:
                return KungfuQueryError("http_status_{}".format(status))
            return KungfuQueryError("upstream_error")
        return None

    @staticmethod
    def _check_scope(data: Dict[str, Any], scope: Dict[str, Any]) -> None:
        # Only explicit declarations are checked. The configured scope is not
        # evidence that every match in an undeclared response is in that season.
        for key in ("season_id", "season_start", "pvp_type"):
            if data.get(key) is not None and _text(data[key]) != _text(scope.get(key)):
                raise KungfuQueryError("scope_mismatch")

    @staticmethod
    def _check_identity(info: Any, identity: Dict[str, Any], role_id: str) -> None:
        if not isinstance(info, dict) or not info:
            raise KungfuQueryError("invalid_indicator")
        for aliases, wanted in (
            (("role_id", "roleId"), role_id),
            (("zone",), identity.get("zone")),
            (("server",), identity.get("server")),
        ):
            actual = next((info[key] for key in aliases if info.get(key) is not None), None)
            if actual is not None and _text(actual) != _text(wanted):
                raise KungfuQueryError("identity_mismatch")
        expected = _text(identity.get("global_role_id"))
        actual = _text(info.get("global_role_id") or info.get("globalRoleId"))
        if expected.startswith("SK01-") and actual and expected != actual:
            raise KungfuQueryError("identity_mismatch")

    async def _query(self, identity: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
        role_id = _text(identity.get("game_role_id") or identity.get("role_id"))
        zone, server = _text(identity.get("zone")), _text(identity.get("server"))
        if not role_id or not zone or not server or "未知" in (role_id, zone, server):
            raise KungfuQueryError("identity_unresolved")
        requested_at = self.clock()
        response = await self._request("https://m.pvp.xoyo.com/role/indicator", {
            "role_id": role_id, "zone": zone, "server": server,
        })
        error = self._error(response)
        if error:
            raise error
        data = response.get("data")
        if not isinstance(data, dict):
            raise KungfuQueryError("invalid_indicator")
        self._check_scope(data, scope)
        info = data.get("role_info")
        self._check_identity(info, identity, role_id)
        matches, history_error = [], None
        sk01 = _text(info.get("global_role_id") or info.get("globalRoleId"))
        if sk01.startswith("SK01-"):
            try:
                history = await self._request("https://m.pvp.xoyo.com/3c/mine/match/history", {
                    "global_role_id": sk01, "size": 40, "cursor": 0,
                })
                error = self._error(history)
                if error:
                    raise error
                self._check_scope(history, scope)
                rows = history.get("data")
                if not isinstance(rows, list):
                    raise KungfuQueryError("invalid_history")
                matches = [row for row in rows[:40] if isinstance(row, dict)]
            except KungfuQueryError as exc:
                if not self.display_enabled and (exc.auth or exc.code in ("rate_limited", "scope_mismatch")):
                    raise
                # Legacy selection falls back to indicator when history fails;
                # auth/rate limits remain control signals and cannot be bypassed.
                history_error = exc.code
                history_failure = exc
        result = {"indicator_data": data, "matches": matches,
                  "requested_at": requested_at, "verified_at": self.clock()}
        if history_error:
            result["history_error_code"] = history_error
        if self.display_enabled:
            if history_error:
                result["display_evidence"] = self._display_failure(history_failure)
            elif not sk01.startswith("SK01-"):
                result["display_evidence"] = self._display_failure(KungfuQueryError("missing_sk01"))
            else:
                result["display_evidence"] = await self._display(indicator_identity(identity, data), scope, matches)
            result["verified_at"] = self.clock()
        return result

    @staticmethod
    def _display_failure(error: KungfuQueryError) -> Dict[str, Any]:
        return {"status": "failed", "error_code": error.code,
                "retry_after": error.retry_after, "auth": error.auth}

    async def _display(self, identity: Dict[str, Any], scope: Dict[str, Any],
                       matches: list) -> Dict[str, Any]:
        candidates = []
        missing_time = False
        for row in matches:
            if row.get("won") is not True:
                continue
            if any(row.get(key) is not None and str(row[key]) != str(scope.get(key))
                   for key in ("season_id", "pvp_type")):
                continue
            try:
                stamp = int(row.get("match_time") or row.get("matchTime") or
                            row.get("start_time") or row.get("startTime") or row.get("time"))
                match_id = int(row.get("match_id") or row.get("matchId") or row.get("id"))
            except (TypeError, ValueError, OverflowError):
                missing_time = True
                continue
            if stamp <= 0 or (stamp >= scope["season_start"] and match_id <= 0):
                missing_time = True
                continue
            if stamp >= scope["season_start"] and match_id > 0:
                candidates.append((stamp, match_id))
        if not candidates:
            return (self._display_failure(KungfuQueryError("invalid_match_locator")) if missing_time
                    else {"status": "no_win"})
        stamp, match_id = max(candidates)
        evidence: Dict[str, Any] = {"status": "ready", "match_id": match_id, "match_time": stamp}
        try:
            detail = await self._request("https://m.pvp.xoyo.com/3c/mine/match/detail", {"match_id": match_id})
            error = self._error(detail)
            if error:
                raise error
            data = detail.get("data")
            if not isinstance(data, dict):
                raise KungfuQueryError("invalid_detail")
            self._check_scope(data, scope)
            evidence["detail_data"] = data
            if needs_replay(data, identity):
                replay = await self._request("https://m.pvp.xoyo.com/3c/mine/match/replay", {"match_id": match_id})
                error = self._error(replay)
                if error:
                    raise error
                if not isinstance(replay.get("data"), dict):
                    raise KungfuQueryError("invalid_replay")
                self._check_scope(replay["data"], scope)
                evidence["replay_data"] = replay["data"]
        except KungfuQueryError as exc:
            evidence.update(self._display_failure(exc))
        return evidence
