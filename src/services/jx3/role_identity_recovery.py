"""Bounded query-ID recovery using original indicator and replay responses."""

import asyncio
import copy
import inspect
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from src.services.jx3.indicator_utils import parse_3v3_indicator
from src.services.jx3.jjc_role_query_context import release_after_owners
from src.services.jx3.role_identity_matching import validate_query_id_evidence


_HTTP_PERMIT = threading.Semaphore(1)


def collect_query_id_candidates(identity: Dict[str, Any], *sources: Any) -> list:
    """Keep all distinct retained locators, including older alternatives."""
    result = []
    seen = set()

    def add(source: Any) -> None:
        if isinstance(source, (list, tuple)):
            for item in source:
                add(item)
            return
        if not isinstance(source, dict):
            return
        role_id = str(source.get("role_id") or source.get("game_role_id") or "").strip()
        zone = str(source.get("zone") or identity.get("zone") or "").strip()
        server = str(source.get("server") or identity.get("server") or "").strip()
        locator = (zone, server, role_id)
        if all(locator) and locator not in seen:
            seen.add(locator)
            result.append(dict(source, role_id=role_id, zone=zone, server=server))
        elif all(locator):
            for retained in result:
                if (retained["zone"], retained["server"], retained["role_id"]) == locator:
                    if not retained.get("match_id") and source.get("match_id"):
                        retained["match_id"] = source["match_id"]
                    break
        for key in ("profile_history", "data", "raw", "role_info"):
            child = source.get(key)
            if isinstance(child, (dict, list, tuple)):
                add(child)

    add(identity)
    for source in sources:
        add(source)
    return result


def indicator_matches_request(identity: Dict[str, Any], raw: Any, *, role_id: str,
                              zone: str, server: str) -> bool:
    """Compatibility read guard; missing old response fields are not proof to migrate IDs."""
    if not isinstance(raw, dict) or raw.get("code") != 0 or raw.get("error"):
        return False
    data = raw.get("data")
    info = data.get("role_info") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info or parse_3v3_indicator(raw).get("error"):
        return False
    for key, wanted in (("role_id", role_id), ("zone", zone), ("server", server)):
        actual = info.get(key)
        if actual is not None and str(actual) != str(wanted):
            return False
    expected_sk01 = str(identity.get("global_role_id") or "")
    actual_sk01 = str(info.get("global_role_id") or info.get("globalRoleId") or "")
    return not (expected_sk01 and actual_sk01 and expected_sk01 != actual_sk01)


def _upstream_failed(raw: Any) -> bool:
    return (not isinstance(raw, dict) or bool(raw.get("error"))
            or raw.get("code") not in (0, None)
            or raw.get("status_code") not in (0, 200, None)
            or ("data" not in raw and raw.get("code") is None))


async def recover_query_ids(
    identity: Dict[str, Any], *, identity_repo: Any, candidates: list,
    indicator_fetcher: Callable[..., Any], replay_fetcher: Callable[..., Any],
    history_fetcher: Optional[Callable[..., Any]] = None,
    current_indicator: Optional[Dict[str, Any]] = None,
    current_indicator_request: Optional[Dict[str, Any]] = None,
    replay_data: Optional[Dict[str, Any]] = None,
    deadline_seconds: float = 15.0, max_logical_calls: int = 5, publish: bool = False,
) -> Dict[str, Any]:
    """Verify by default. Explicit publication is an archived snapshot CAS, never an upsert."""
    snapshot = copy.deepcopy(identity)
    started = datetime.now(timezone.utc)
    observed_at = started.replace(microsecond=(started.microsecond // 1000) * 1000)
    deadline = time.monotonic() + max(0, deadline_seconds)
    calls = 0
    limit = min(5, max(0, max_logical_calls))

    def outcome(status: str, **extra: Any) -> Dict[str, Any]:
        return dict(status=status, identity=snapshot, logical_calls=calls,
                    observed_at=observed_at, **extra)

    async def call(func: Callable[..., Any], **kwargs: Any) -> Any:
        nonlocal calls
        remaining = deadline - time.monotonic()
        if remaining <= 0 or calls >= limit:
            raise TimeoutError("recovery_deadline_or_budget")
        asynchronous = inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(getattr(func, '__call__', None))
        if asynchronous:
            async def invoke_async():
                nonlocal calls
                # Acquire in the executing owner, immediately before its try.
                # A task cancelled before its first step never owns a permit.
                if time.monotonic() >= deadline:
                    raise TimeoutError('recovery_deadline')
                if not _HTTP_PERMIT.acquire(blocking=False):
                    raise TimeoutError('recovery_http_busy')
                calls += 1
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('recovery_deadline')
                    return await func(**kwargs)
                finally:
                    # Frontend exit cannot relinquish a still-closing socket.
                    release_after_owners(_HTTP_PERMIT.release, getattr(func, 'physical_owners', ()))
            task = asyncio.create_task(invoke_async())
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            if getattr(func, 'retain_physical_owner', False):
                # The service callback itself owns the sync endpoint until real
                # completion; cancellation of its waiter must not free permit.
                return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            return await asyncio.wait_for(task, timeout=remaining)

        if not _HTTP_PERMIT.acquire(blocking=False):
            raise TimeoutError('recovery_http_busy')
        calls += 1

        def invoke() -> Any:
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError('recovery_deadline')
                return func(**kwargs)
            finally:
                _HTTP_PERMIT.release()

        task = asyncio.create_task(asyncio.to_thread(invoke))
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)

    current_id = str(snapshot.get("game_role_id") or snapshot.get("role_id") or "")
    zone = str(snapshot.get("zone") or "")
    server = str(snapshot.get("server") or "")
    held_request = current_indicator_request
    if held_request is not None:
        observed = held_request.get("observed_at")
        if not isinstance(observed, datetime):
            return outcome("unresolved", reason="raw_request_observation_missing")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = (started - observed).total_seconds()
        if age < 0 or age >= min(15.0, deadline_seconds):
            return outcome("unresolved", reason="raw_request_observation_expired")
        observed_at = observed.replace(microsecond=(observed.microsecond // 1000) * 1000)
        deadline -= age
    try:
        current_request_matches = held_request is None or (
            str(held_request.get("role_id")) == current_id
            and held_request.get("zone") == zone and held_request.get("server") == server
        )
        current = current_indicator if current_request_matches else None
        if current is None and current_id and zone and server:
            current = await call(indicator_fetcher, role_id=current_id, zone=zone, server=server)
        if current is not None and _upstream_failed(current):
            return outcome("upstream_error", upstream_endpoint="indicator", upstream_error_data=current)
        if indicator_matches_request(
            snapshot, current, role_id=current_id, zone=zone, server=server,
        ):
            return outcome("healthy", raw=current)

        alternatives = [row for row in collect_query_id_candidates(snapshot, candidates)
                        if row["zone"] == zone and row["server"] == server and row["role_id"] != current_id]
        for locator in alternatives[:2]:
            if held_request is not None and all(
                str(held_request.get(key) or "") == str(locator[key])
                for key in ("role_id", "zone", "server")
            ):
                raw = current_indicator
            else:
                raw = await call(indicator_fetcher, role_id=locator["role_id"], zone=zone, server=server)
            if _upstream_failed(raw):
                return outcome("upstream_error", upstream_endpoint="indicator", upstream_error_data=raw)
            if not indicator_matches_request(snapshot, raw, role_id=locator["role_id"], zone=zone, server=server):
                continue
            replay = replay_data
            match_id = locator.get("match_id")
            if replay is None and not match_id and history_fetcher is not None:
                history = await call(history_fetcher, global_role_id=snapshot.get("global_role_id"))
                if _upstream_failed(history):
                    return outcome("upstream_error", upstream_endpoint="history", upstream_error_data=history)
                items = history.get("data") or []
                if isinstance(items, dict):
                    items = items.get("list") or items.get("items") or []
                if isinstance(items, list) and items:
                    match_id = items[0].get("match_id")
            if replay is None and match_id:
                replay = await call(replay_fetcher, match_id=int(match_id))
                if _upstream_failed(replay):
                    return outcome("upstream_error", upstream_endpoint="replay", upstream_error_data=replay)
            if time.monotonic() >= deadline:
                return outcome("unresolved", reason="deadline")
            if not validate_query_id_evidence(
                snapshot, request_zone=zone, request_server=server,
                request_role_id=locator["role_id"], indicator_data=raw, replay_data=replay or {},
            ):
                continue
            evidence = dict(candidate=locator, raw=raw, replay_data=replay)
            if not publish:
                return outcome("ready", **evidence)
            replaced = await identity_repo.replace_query_ids_if_unchanged(
                snapshot, game_role_id=locator["role_id"], role_id=locator["role_id"],
                observed_at=observed_at,
            )
            if not replaced:
                latest = await identity_repo.get_by_id(snapshot.get("_id"))
                return outcome("conflict", current_identity=latest)
            recovered = dict(snapshot, game_role_id=locator["role_id"], role_id=locator["role_id"])
            result = outcome("recovered", **evidence)
            result["identity"] = recovered
            return result
        return outcome("unresolved")
    except Exception as exc:
        if isinstance(exc, TimeoutError) and str(exc) == "recovery_http_busy":
            return outcome("busy", reason="recovery_http_busy")
        return outcome('upstream_error', reason=type(exc).__name__, error_code=getattr(exc, 'code', None),
                       upstream_error_data={'error': str(exc)})
