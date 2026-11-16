"""Single-consumer orchestration with monotonic role publication then guarded ack."""
import asyncio
from copy import deepcopy
import math
import time
from typing import Any, Callable, Dict, Optional, Union

from nonebot import logger

from src.infra.tuilan_kungfu_client import KungfuQueryError
from src.services.jx3.jjc_kungfu_classifier import select_kungfu
from src.services.jx3.jjc_kungfu_meta import get_jjc_kungfu_catalog
from src.services.jx3.jjc_kungfu_policy import KungfuPolicy, positive_integer, retry_delay, valid_timestamp
from src.services.jx3.jjc_ranking_display import build_display_update, display_context
from src.storage.mongo_repos.jjc_kungfu_refresh_repo import QueueIntegrityError

_LOCATOR_FIELDS = ("server", "name", "zone", "game_role_id", "role_id", "global_id", "global_role_id")
_LEASE_SECONDS = 90


def _same_recovery_value(value: Any, expected: Any, field: Optional[str] = None) -> bool:
    """Compare persisted metadata without Python's bool/int coercion.

    BSON integer subclasses remain legal. Stored times may round-trip as an
    integer or float; counters and identity locators retain their value types.
    """
    if isinstance(expected, dict):
        return (isinstance(value, dict) and value.keys() == expected.keys()
                and all(_same_recovery_value(value[key], item, key)
                        for key, item in expected.items()))
    if field in ("next_run_at", "cooldown_until", "requested_at", "verified_at", "lease_expires_at") and expected is not None:
        return (type(value) in (int, float) and math.isfinite(value)
                and type(expected) in (int, float) and math.isfinite(expected) and value == expected)
    if isinstance(expected, int) and not isinstance(expected, bool):
        return isinstance(value, int) and not isinstance(value, bool) and value == expected
    return type(value) is type(expected) and value == expected


def _locator(identity: Dict[str, Any]) -> Dict[str, Any]:
    # Missing and explicitly null are distinct parts of the publication guard.
    return {key: identity[key] for key in _LOCATOR_FIELDS if key in identity}


def _same_publication(value: Any, generation: int, scope: Dict[str, Any]) -> bool:
    return (isinstance(value, dict) and positive_integer(value.get("publish_version"))
            and value["publish_version"] == generation
            and all(type(value.get(key)) is type(expected) and value.get(key) == expected
                    for key, expected in scope.items()))


class JjcKungfuRefreshService:
    def __init__(
        self, identity_repo: Any, queue_repo: Any, client: Any,
        policy: Union[KungfuPolicy, Callable[[], KungfuPolicy]],
        mapping: Dict[str, str], clock: Callable[[], float] = time.time,
    ) -> None:
        self.identity_repo, self.queue_repo, self.client = identity_repo, queue_repo, client
        self.policy, self.mapping, self.clock = policy, mapping, clock
        self.cooldown_until = 0.0
        self._execution_lock: Optional[asyncio.Lock] = None
        self._canonical_ids = {entry["name"]: entry["id"] for entry in get_jjc_kungfu_catalog()}

    def _policy(self) -> KungfuPolicy:
        policy = self.policy() if callable(self.policy) else self.policy
        if not isinstance(policy, KungfuPolicy):
            raise ValueError("invalid kungfu refresh policy")
        return policy

    async def initialize(self) -> None:
        self._policy()
        # Leave a fixed margin for the two Mongo writes and uncertain-write readback.
        if not 0 < self.client.attempt_timeout < _LEASE_SECONDS - 10:
            raise ValueError("kungfu attempt budget must leave lease finalization time")
        await self.queue_repo.ensure_indexes()

    async def _complete(self, task: Dict[str, Any], next_run_at: float,
                        error_code: Optional[str] = None) -> None:
        if self._remaining(task) <= 0:
            return
        try:
            accepted = await self.queue_repo.complete(task, self.clock(), next_run_at,
                                                      error_code=error_code)
        except Exception as exc:
            if task.get("_raise_integrity_errors") and isinstance(exc, QueueIntegrityError):
                raise
            if self._remaining(task) <= 0:
                raise
            # A lease recovery also produces pending; verify the exact decision,
            # not just status, before resolving an unknown acknowledgement.
            current = await self.queue_repo.get_by_id(task["_id"])
            expected = {"_id": task["_id"], "generation": task["generation"], "scope": task["scope"],
                        "status": "pending", "lease_token": None, "lease_expires_at": None,
                        "next_run_at": next_run_at, "last_error_code": error_code,
                        "consecutive_failures": task.get("consecutive_failures", 0) + 1 if error_code else 0}
            if current and all(key in current and _same_recovery_value(current[key], value, key)
                               for key, value in expected.items()):
                return
            raise
        if not accepted:
            logger.warning("JJC kungfu refresh lost task lease: identity_id={}", task["_id"])

    async def _failure(self, task: Dict[str, Any], code: str,
                       minimum_delay: float = 0) -> None:
        delay = max(retry_delay(task.get("consecutive_failures", 0) + 1), minimum_delay,
                    self.cooldown_until - self.clock())
        logger.warning("JJC kungfu refresh failed: identity_id={} code={}", task["_id"], code)
        await self._complete(task, self.clock() + delay, code)

    async def _still_owned(self, task: Dict[str, Any], policy: KungfuPolicy) -> bool:
        if self._remaining(task) <= 0 or self._policy().scope != policy.scope:
            return False
        current = await self.queue_repo.get_by_id(task["_id"])
        return bool(current and current.get("status") == "running"
                    and current.get("generation") == task["generation"]
                    and current.get("lease_token") == task["lease_token"]
                    and current.get("scope") == task["scope"]
                    and current.get("lease_expires_at", 0) > self.clock())

    async def _publish(
        self, task: Dict[str, Any], locator: Dict[str, Any], result: Dict[str, Any],
        display_update: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resolve uncertain writes by reading; a failed read leaves the lease alone."""
        uncertain = False
        if self._remaining(task) <= 0:
            return None
        task["_publication_started"] = True
        task["_publication_locator"] = deepcopy(locator)
        try:
            if display_update is None:
                published = await self.identity_repo.publish_jjc_kungfu_if_newer(
                    task["_id"], locator, task["generation"], result)
            else:
                published = await self.identity_repo.publish_jjc_kungfu_if_newer(
                    task["_id"], locator, task["generation"], result, display_update=display_update)
        except Exception as exc:
            if task.get("_raise_integrity_errors") and isinstance(exc, QueueIntegrityError):
                raise
            # CancelledError is a BaseException on supported Python versions.
            # Do not confirm an attempt whose caller is cancelling.
            uncertain = True
            published = False
            logger.warning("JJC kungfu publication response unknown: identity_id={} exception_type={}",
                           task["_id"], type(exc).__name__)
        if published:
            return self._outcome({"_id": task["_id"], **locator, "jjc_kungfu": result,
                                  "jjc_display": display_update}, task)
        if self._remaining(task) <= 0:
            return None
        # This await must propagate database errors: no safe conclusion was reached.
        current = await self.identity_repo.get_by_id(task["_id"])
        saved = current.get("jjc_kungfu") if current else None
        outcome = self._outcome(current, task)
        if outcome is not None:
            return outcome
        if not current:
            code = "identity_missing"
        elif _locator(current) != locator:
            code = "identity_changed"
        elif (isinstance(saved, dict) and positive_integer(saved.get("publish_version"))
              and saved["publish_version"] > task["generation"]):
            code = "superseded"
        else:
            code = "publication_unconfirmed" if uncertain else "publication_rejected"
        await self._failure(task, code)
        return None

    def _remaining(self, task: Dict[str, Any]) -> float:
        remaining = task["lease_expires_at"] - self.clock()
        if "_execution_deadline" in task:
            remaining = min(remaining, task["_execution_deadline"] - asyncio.get_running_loop().time())
        return remaining

    def _outcome(self, current: Optional[Dict[str, Any]], task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        saved = current.get("jjc_kungfu") if current else None
        if not _same_publication(saved, task["generation"], task["scope"]):
            return None
        if not task.get("_display_expected"):
            return saved
        locator = task.get("_publication_locator")
        if not _same_recovery_value(_locator(current), locator):
            return None
        display = current.get("jjc_display")
        if not isinstance(display, dict):
            return None
        context = display_context(current, task["scope"])

        def matches(item: Any) -> bool:
            return (isinstance(item, dict) and positive_integer(item.get("publish_version"))
                    and item["publish_version"] == task["generation"]
                    and all(key in item and _same_recovery_value(item[key], value, key)
                            for key, value in context.items())
                    and all(_same_recovery_value(item.get(key), saved.get(key), key)
                            for key in ("requested_at", "verified_at")))

        attempt = display.get("attempt")
        if (not matches(attempt) or not _same_recovery_value(attempt.get("schedule"), task.get("_display_schedule"))
                or attempt.get("state") not in ("full", "partial", "failed", "no_win")):
            return None
        packet = None
        if attempt["state"] in ("full", "partial"):
            packet = display.get("current")
            if not matches(packet):
                return None
        return {**saved, "display_packet": deepcopy(packet), "display_attempt": deepcopy(attempt)}

    def _apply_cooldown(self, error: Dict[str, Any]) -> None:
        if error.get("auth") or error.get("error_code") == "rate_limited":
            delay = error.get("retry_after")
            delay = delay if valid_timestamp(delay) else 300
            delay = min(3600, max(300 if error.get("auth") else 1, delay))
            self.cooldown_until = max(self.cooldown_until, self.clock() + delay)

    def _execution_position(self) -> asyncio.Lock:
        # Python 3.9 binds locks to a loop at construction: create it at runtime.
        if self._execution_lock is None:
            self._execution_lock = asyncio.Lock()
        return self._execution_lock

    async def run_once(self) -> bool:
        async with self._execution_position():
            if self.clock() < self.cooldown_until:
                return False
            policy = self._policy()
            await self.queue_repo.recover_expired(self.clock())
            task = await self.queue_repo.claim(policy.scope, self.clock(), lease_seconds=_LEASE_SECONDS)
            if task is None:
                return False
            await self._execute(task, policy)
            return True

    async def query_now(self, identity: Dict[str, Any], *, raise_integrity_errors: bool = False) -> Optional[Dict[str, Any]]:
        """Query and persist this role in the current call, never wait for a worker.

        Without a durable generation reservation, no safe publication is possible.
        Database or upstream failures return unknown; cancellation still propagates.
        """
        async with self._execution_position():
            identity_id = identity.get("_id")
            if raise_integrity_errors and "jjc_kungfu" in identity:
                old = identity["jjc_kungfu"]
                if not isinstance(old, dict) or not positive_integer(old.get("publish_version")):
                    raise QueueIntegrityError("invalid preexisting publication schema")
            if self.clock() < self.cooldown_until:
                logger.warning("JJC direct kungfu query skipped: identity_id={} code=upstream_cooldown", identity_id)
                return None
            try:
                policy = self._policy()
                await self.queue_repo.register([identity], policy.scope, self.clock(),
                                               track_seconds=policy.track_seconds)
                task = await self.queue_repo.claim_identity(identity_id, policy.scope, self.clock(),
                                                            lease_seconds=_LEASE_SECONDS)
            except Exception as exc:
                if raise_integrity_errors and isinstance(exc, QueueIntegrityError):
                    raise
                logger.error("JJC direct kungfu query failed: identity_id={} code=reservation_failed exception_type={}",
                             identity_id, type(exc).__name__)
                return None
            if task is None:
                logger.warning("JJC direct kungfu query failed: identity_id={} code=reservation_unavailable", identity_id)
                return None
            task["_raise_integrity_errors"] = raise_integrity_errors
            try:
                return await self._execute(task, policy, expected_locator=_locator(identity))
            except QueueIntegrityError:
                if raise_integrity_errors:
                    raise
                logger.error("JJC direct kungfu query failed: identity_id={} code=queue_integrity_error", identity_id)
                return None
            except Exception as exc:
                logger.error("JJC direct kungfu query failed: identity_id={} code=persistence_failed exception_type={}",
                             identity_id, type(exc).__name__)
                # Publication may have succeeded before acknowledgement failed. Use
                # only this exact durable result, leaving task recovery to its lease.
                if not task.get("_publication_started") or self._remaining(task) <= 0:
                    return None
                try:
                    current = await asyncio.wait_for(self.identity_repo.get_by_id(identity_id), self._remaining(task))
                except Exception as read_error:
                    if raise_integrity_errors and isinstance(read_error, QueueIntegrityError):
                        raise
                    logger.error("JJC direct kungfu readback failed: identity_id={} code=readback_failed exception_type={}",
                                 identity_id, type(read_error).__name__)
                    return None
                return self._outcome(current, task)

    async def _execute(
        self, task: Dict[str, Any], policy: KungfuPolicy,
        expected_locator: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        remaining = self._remaining(task)
        if remaining <= 0:
            return None
        task["_execution_deadline"] = asyncio.get_running_loop().time() + remaining
        try:
            return await asyncio.wait_for(self._execute_attempt(task, policy, expected_locator), remaining)
        except asyncio.TimeoutError:
            logger.warning("JJC kungfu refresh lease budget exhausted: identity_id={}", task["_id"])
            return None

    async def _execute_attempt(
        self, task: Dict[str, Any], policy: KungfuPolicy,
        expected_locator: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        identity = await self.identity_repo.get_by_id(task["_id"])
        if identity is None:
            await self._failure(task, "identity_missing")
            return None
        if "jjc_kungfu" in identity:
            old = identity["jjc_kungfu"]
            if (not isinstance(old, dict) or not positive_integer(old.get("publish_version"))):
                if task.get("_raise_integrity_errors"):
                    raise QueueIntegrityError("invalid preexisting publication schema")
                await self._failure(task, "invalid_publication_schema")
                return None
            if old["publish_version"] >= task["generation"]:
                if not await self._still_owned(task, policy):
                    logger.warning("JJC kungfu query discarded: identity_id={} code=execution_lost", task["_id"])
                    return None
                logger.error("JJC kungfu queue version regressed: identity_id={} claimed={} published={}",
                             task["_id"], task["generation"], old["publish_version"])
                # A fresh claim must be above every preexisting publication.
                # Retrying would silently rebuild a corrupted counter online.
                raise QueueIntegrityError("claimed generation does not exceed role publication")
        locator = _locator(identity)
        if expected_locator is not None and locator != expected_locator:
            await self._failure(task, "identity_changed")
            return None
        try:
            evidence = await self.client.query(identity, task["scope"])
        except KungfuQueryError as exc:
            delay = exc.retry_after or 0
            if exc.auth or exc.code == "rate_limited":
                delay = min(3600, max(300 if exc.auth or not delay else 1, delay))
                self.cooldown_until = max(self.cooldown_until, self.clock() + delay)
                logger.error("JJC kungfu upstream cooling down: code={} seconds={}", exc.code, delay)
            await self._failure(task, exc.code, delay)
            return None
        except Exception as exc:
            logger.warning("JJC kungfu query failed: exception_type={}", type(exc).__name__)
            await self._failure(task, "query_failed")
            return None
        if isinstance(evidence, dict) and "display_evidence" in evidence:
            task["_display_expected"] = True
            raw_display = evidence["display_evidence"]
            if isinstance(raw_display, dict):
                self._apply_cooldown(raw_display)
        try:
            selected = select_kungfu(evidence["indicator_data"], evidence["matches"], self.mapping)
            name = selected["kungfu"]
            requested_at = evidence["requested_at"]
        except (KeyError, TypeError, AttributeError, ValueError):
            await self._failure(task, "invalid_query_response")
            return None
        if name not in self._canonical_ids:
            await self._failure(task, "unknown_kungfu")
            return None
        verified_at = self.clock()
        if not valid_timestamp(requested_at) or requested_at > verified_at:
            await self._failure(task, "invalid_query_time")
            return None
        if not await self._still_owned(task, policy):
            logger.warning("JJC kungfu query discarded: identity_id={} code=execution_lost", task["_id"])
            return None
        result = {**task["scope"], "name": name, "kungfu_id": self._canonical_ids[name],
                  "source": selected["kungfu_selected_source"], "requested_at": requested_at,
                  "verified_at": verified_at,
                  "win_sample_count": len(selected["match_history_win_samples"]),
                  "publish_version": task["generation"]}
        display_update = None
        if task.get("_display_expected"):
            display_update = build_display_update(identity, task["scope"], task["generation"], evidence, verified_at)
            attempt = display_update["attempt"]
            normal = attempt["state"] in ("full", "no_win") and not attempt["error_code"]
            failures = 0 if normal else task.get("consecutive_failures", 0) + 1
            schedule = {"next_run_at": max(verified_at + (policy.refresh_seconds if normal else retry_delay(failures)), self.cooldown_until),
                        "error_code": None if normal else attempt["error_code"],
                        "consecutive_failures": failures, "cooldown_until": self.cooldown_until}
            attempt["schedule"] = schedule
            task["_display_schedule"] = deepcopy(schedule)
        saved = await self._publish(task, locator, result, display_update)
        if saved is not None:
            if task.get("_display_expected"):
                schedule = saved["display_attempt"]["schedule"]
                await self._complete(task, schedule["next_run_at"], schedule["error_code"])
            else:
                await self._complete(task, saved["verified_at"] + policy.refresh_seconds)
        return saved
