"""Small request-local diagnostics; never retain identity or upstream payloads."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import functools
import json
import logging
import re
import threading
import time
from uuid import uuid4

try:
    from nonebot import logger as _base_logger
except ImportError:
    import logging
    _base_logger = logging.getLogger(__name__)

_CURRENT = ContextVar("jjc_foreground_diagnostics", default=None)
STAGES = frozenset(("identity", "cache", "repository", "queue", "ranking", "http", "recovery", "cleanup"))
FAILURES = frozenset(("timeout", "cancelled", "upstream_rejected", "transport", "invalid_response",
                      "storage", "unknown_write", "busy", "dependency_error"))
CODES = frozenset(("invalid_params", "role_identity_not_found", "indicator_identity_conflict",
    "indicator_identity_changed", "identity_key_missing", "indicator_unavailable",
    "indicator_query_timeout", "indicator_query_busy", "indicator_storage_unavailable",
    "indicator_cache_write_failed", "indicator_cache_read_failed", "indicator_identity_write_failed",
    "role_identity_unresolved", "identity_unresolved", "global_role_id_missing", "invalid_response",
    "role_not_found", "identity_recovery_failed", "identity_recovery_cooldown", "unknown_error"))
COMPONENTS = frozenset(("inspect", "ranking", "cache", "identity_repo", "profile_repo", "inspect_repo",
                        "http", "signer", "mongo"))


def _emit(packet, level="INFO"):
    # Only this local logger is patched. Existing contextual extras cannot carry
    # a bound identity or exception into a foreground diagnostic record.
    text = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if hasattr(_base_logger, "patch"):
        def sanitize(record):
            record["extra"] = {}
        _base_logger.patch(sanitize).log(level, text)
    else:
        import logging
        _base_logger.log(getattr(logging, level, logging.INFO), text)


class QueryTrace:
    def __init__(self, kind):
        self.request_id = uuid4().hex
        self.kind = kind if kind in ("indicator", "recent") else "indicator"
        self.started = time.monotonic()
        self.guard = threading.RLock()
        self.stage_ms = {}
        self.spans = {}
        self.next_span = 0
        self.queue_ms = 0.0
        self.cache_key_type = "input"
        self.cache_format = "none"
        self.outbound_count = 0
        self.failure_types = set()
        self.exception_types = set()
        self.flight_waiters = 0
        self.cancelled = False
        self.finished = False

    def failure(self, category, error=None):
        if category not in FAILURES:
            category = "dependency_error"
        with self.guard:
            self.failure_types.add(category)
            if error is not None:
                name = type(error).__name__
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                    self.exception_types.add(name)

    def finish(self, result, counts):
        with self.guard:
            if self.finished:
                return
            self.finished = True
            now = time.monotonic()
            code = ""
            if isinstance(result, dict) and result.get("error"):
                candidate = result.get("message")
                code = candidate if isinstance(candidate, str) and candidate in CODES else "indicator_unavailable"
            deadline_expired = code == "indicator_query_timeout" and not self.cancelled
            if self.cancelled:
                self.failure("cancelled")
            elif deadline_expired:
                self.failure("timeout")
            elif code == "indicator_query_busy":
                self.failure("busy")
            elif code:
                self.failure("storage" if "storage" in code or "cache_write" in code or "cache_read" in code else "dependency_error")
            stages = dict(self.stage_ms)
            # A shared physical owner can outlive one caller. Report elapsed
            # portions of its still-open spans without pretending it finished.
            for stage, started in self.spans.values():
                stages[stage] = stages.get(stage, 0.0) + max(0.0, now - started) * 1000
            cache = result.get("cache") if isinstance(result, dict) else None
            packet = {"event": "jjc_role_query", "request_id": self.request_id, "kind": self.kind,
                "duration_ms": round(max(0.0, now - self.started) * 1000, 3),
                "stage_ms": {key: round(value, 3) for key, value in stages.items()},
                "queue_ms": round(self.queue_ms, 3), "cache_key_type": self.cache_key_type,
                "cache_format": self.cache_format, "stale": bool(isinstance(cache, dict) and cache.get("stale")),
                "outbound_count": self.outbound_count, "failure_types": sorted(self.failure_types),
                "exception_types": sorted(self.exception_types), "flight_waiters": self.flight_waiters,
                "flights": int(counts.get("flights", 0)), "active_requests": int(counts.get("active_requests", 0)),
                "deadline_expired": deadline_expired, "cancelled": self.cancelled,
                "expired_tasks": int(deadline_expired), "cancelled_tasks": int(self.cancelled), "error_code": code}
        _emit(packet)


def begin_query_trace(kind):
    trace = QueryTrace(kind)
    return trace, _CURRENT.set(trace)


def end_query_trace(token):
    _CURRENT.reset(token)


@contextmanager
def query_stage(stage):
    trace = _CURRENT.get()
    if trace is None or stage not in STAGES:
        yield
        return
    with trace.guard:
        trace.next_span += 1
        span = trace.next_span
        trace.spans[span] = (stage, time.monotonic())
    try:
        yield
    finally:
        with trace.guard:
            _, started = trace.spans.pop(span)
            trace.stage_ms[stage] = trace.stage_ms.get(stage, 0.0) + max(0.0, time.monotonic() - started) * 1000


def observed_query_stage(stage):
    def decorate(function):
        @functools.wraps(function)
        async def observed(*args, **kwargs):
            with query_stage(stage):
                return await function(*args, **kwargs)
        return observed
    return decorate


def query_failure(category, error=None):
    trace = _CURRENT.get()
    if trace is not None:
        trace.failure(category, error)



def query_exception(error, *, fallback="dependency_error"):
    code = getattr(error, "code", None)
    if code == "indicator_query_busy":
        category = "busy"
    elif code == "indicator_query_timeout" or isinstance(error, asyncio.TimeoutError) or bool(getattr(error, "timeout", False)):
        category = "timeout"
    else:
        category = fallback
    query_failure(category, error)


def query_outbound():
    trace = _CURRENT.get()
    if trace is not None:
        with trace.guard:
            trace.outbound_count += 1


def query_queue(seconds):
    trace = _CURRENT.get()
    if trace is not None:
        duration = max(0.0, seconds) * 1000
        with trace.guard:
            trace.queue_ms += duration
            trace.stage_ms["queue"] = trace.stage_ms.get("queue", 0.0) + duration


def query_cache(*, key_type=None, format=None):
    trace = _CURRENT.get()
    if trace is not None:
        with trace.guard:
            if key_type in ("canonical", "legacy", "input", "none"):
                trace.cache_key_type = key_type
            if format in ("scoped", "legacy", "miss", "invalid", "none"):
                trace.cache_format = format


def query_flight(waiters):
    trace = _CURRENT.get()
    if trace is not None:
        with trace.guard:
            trace.flight_waiters = max(trace.flight_waiters, waiters)


class _ForegroundLogger:
    def __init__(self, base, component):
        self.base = base
        self.component = component if component in COMPONENTS else "inspect"

    def bind(self, *args, **kwargs):
        if _CURRENT.get() is not None:
            return self
        return _ForegroundLogger(self.base.bind(*args, **kwargs), self.component)

    def opt(self, *args, **kwargs):
        if _CURRENT.get() is not None:
            return self
        return _ForegroundLogger(self.base.opt(*args, **kwargs), self.component)

    def __getattr__(self, name):
        if name not in ("trace", "debug", "info", "success", "warning", "error", "critical", "exception"):
            return getattr(self.base, name)
        def write(message, *args, **kwargs):
            trace = _CURRENT.get()
            if trace is None:
                target = self.base.opt(depth=1) if hasattr(self.base, "opt") else self.base
                return getattr(target, name)(message, *args, **kwargs)
            if name not in ("warning", "error", "critical", "exception"):
                return None
            error = next((value for value in args if isinstance(value, BaseException)), None)
            reason = "dependency_warning"
            if message == "JJC foreground write_outcome_unknown":
                reason = "unknown_write"
            elif message == "JJC foreground Mongo native work did not converge; admissions stopped":
                reason = "storage"
            if reason in FAILURES or error is not None:
                trace.failure(reason if reason in FAILURES else "dependency_error", error)
            _emit({"event": "jjc_role_query_dependency", "request_id": trace.request_id,
                   "component": self.component,
                   "reason": "write_outcome_unknown" if reason == "unknown_write" else reason,
                   "exception_type": type(error).__name__ if error is not None and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", type(error).__name__) else ""},
                  "WARNING" if name == "warning" else "ERROR")
        return write


def foreground_safe_logger(base, *, component):
    return _ForegroundLogger(base, component)


_ACCESS_FILTER_LOCK = threading.Lock()


class _RoleQueryAccessFilter(logging.Filter):
    """Keep Uvicorn route/status diagnostics without the two role query strings."""
    _jjc_role_query_privacy = True

    def filter(self, record):
        if record.name != "uvicorn.access" or not isinstance(record.args, tuple) or len(record.args) < 5:
            return True
        target = record.args[2]
        if not isinstance(target, str):
            return True
        path = target.split("?", 1)[0]
        endpoint = path.rstrip("/")
        if endpoint.endswith(("/api/jjc/ranking-stats/role-indicator", "/api/jjc/ranking-stats/role-recent")):
            args = list(record.args)
            args[2] = path
            record.args = tuple(args)
            record.__dict__.pop("message", None)
        return True


def install_role_query_access_privacy():
    """Idempotent process logging setup; no event loop or client is retained."""
    access = logging.getLogger("uvicorn.access")
    with _ACCESS_FILTER_LOCK:
        if not any(getattr(item, "_jjc_role_query_privacy", False) for item in access.filters):
            access.addFilter(_RoleQueryAccessFilter())
