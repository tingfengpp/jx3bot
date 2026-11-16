"""Request-local Motor CSOT and bounded ownership of real driver futures.

The original clients, their topology/pool defaults and background repositories
are never mutated. Cancelling a frontend await does not release a driver slot.
"""
import asyncio
from dataclasses import fields, is_dataclass, replace
import functools
import inspect
import math
import threading
import time
from typing import Any

import pymongo
from pymongo.errors import AutoReconnect, DuplicateKeyError
from nonebot import logger
from src.utils.jjc_query_observability import (foreground_safe_logger, query_stage,
    query_cache, query_failure, query_queue, query_exception)

logger = foreground_safe_logger(logger, component="mongo")

from src.infra.mongo import get_db


# Errors are constructed lazily to keep the adapter independent of services.
class ForegroundMongoFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


COLLECTIONS = frozenset(("role_identities", "role_identities_history", "role_jjc_cache",
    "jjc_ranking_cache", "jjc_role_indicator", "jjc_role_recent", "jjc_match_detail", "jjc_match_participants"))
WRITES = frozenset(("insert_one", "insert_many", "update_one", "update_many", "replace_one",
                    "delete_one", "delete_many", "find_one_and_update", "bulk_write"))
READS = frozenset(("find_one", "count_documents", "distinct", "estimated_document_count"))


class ForegroundMongoController:
    def __init__(self, *, operation_timeout_seconds=1.0, settle_seconds=1.0):
        if (not math.isfinite(operation_timeout_seconds) or operation_timeout_seconds <= 0
                or not math.isfinite(settle_seconds) or settle_seconds < 0):
            raise ValueError("invalid_foreground_mongo_policy")
        self.operation_timeout_seconds = min(1.0, operation_timeout_seconds)
        self.settle_seconds = min(1.0, settle_seconds)
        self._guard = threading.Lock()
        self._records = {}
        self._next_id = 0
        self._faulted = False
        self._submitted = self._completed = self._unknown_writes = 0

    def snapshot(self):
        with self._guard:
            return {"outstanding": len(self._records), "faulted": self._faulted,
                    "submitted": self._submitted, "completed": self._completed,
                    "unknown_writes": self._unknown_writes}

    async def acquire(self, context):
        queued_at = time.monotonic()
        until = queued_at + min(context.queue_wait, context.remaining())
        try:
            while True:
                context.check()
                with self._guard:
                    if self._faulted:
                        raise ForegroundMongoFailure("indicator_storage_unavailable")
                    if len(self._records) < 2:
                        self._next_id += 1
                        record = {"id": self._next_id, "native": None, "owner": None,
                                  "watch": None, "unknown": False}
                        self._records[record["id"]] = record
                        return record
                if time.monotonic() >= until:
                    raise ForegroundMongoFailure("indicator_query_busy")
                await asyncio.sleep(min(.005, max(0.0, until - time.monotonic())))
        finally:
            query_queue(time.monotonic() - queued_at)

    def unknown(self, record):
        with self._guard:
            if record["unknown"]:
                return
            record["unknown"] = True
            self._unknown_writes += 1
        logger.warning("JJC foreground write_outcome_unknown")

    def release(self, record, native=None):
        # A cancelled asyncio Future is not evidence that its executor ended.
        with self._guard:
            if native is not None and native.cancelled():
                self._faulted = True
                return
            if self._records.pop(record["id"], None) is not None and native is not None:
                self._completed += 1
        watch = record["watch"]
        if watch is not None and not watch.done():
            watch.cancel()
        if native is not None:
            native.exception()  # retrieve errors even after the caller left

    async def watch(self, record, deadline):
        await asyncio.sleep(max(0.0, deadline + self.settle_seconds - time.monotonic()))
        with self._guard:
            if record["id"] not in self._records:
                return
            self._faulted = True
        logger.warning("JJC foreground Mongo native work did not converge; admissions stopped")

    async def drain(self, timeout=2):
        until = time.monotonic() + max(0.0, timeout)
        while self.snapshot()["outstanding"] and time.monotonic() < until:
            await asyncio.sleep(.005)
        return self.snapshot()["outstanding"] == 0


_DEFAULT_CONTROLLER = ForegroundMongoController()


def default_controller():
    return _DEFAULT_CONTROLLER


def _consume(task):
    if not task.cancelled():
        task.exception()


class ForegroundMongoSession:
    def __init__(self, context, controller):
        self.context, self.controller = context, controller
        self._inflight = set()

    def fail(self, code):
        error = ForegroundMongoFailure(code)
        self.context.failure = error
        return error

    async def execute(self, operation, *, write=False, collection=""):
        self.context.check()
        try:
            record = await self.controller.acquire(self.context)
        except ForegroundMongoFailure as exc:
            self.context.failure = exc
            raise
        self._inflight.add(record["id"])

        def finished(native):
            self.controller.release(record, native)
            if not native.cancelled():
                self._inflight.discard(record["id"])

        async def native_owner():
            try:
                self.context.check()
                cap = min(self.controller.operation_timeout_seconds, self.context.remaining())
                if not callable(getattr(pymongo, "timeout", None)):
                    with self.controller._guard:
                        self.controller._faulted = True
                    raise self.fail("indicator_storage_unavailable")
                # Motor copies this context into its executor. Retain both the
                # owner and the uncancelled actual Future until native completion.
                with pymongo.timeout(cap):
                    native = asyncio.ensure_future(operation())
                    record["native"] = native
                    with self.controller._guard:
                        self.controller._submitted += 1
                    record["watch"] = asyncio.create_task(self.controller.watch(record, time.monotonic() + cap))
                    record["watch"].add_done_callback(_consume)
                    native.add_done_callback(finished)
                    return await asyncio.shield(native)
            except StopAsyncIteration:
                # Exhaustion of a real Motor cursor is normal control flow.
                raise
            except DuplicateKeyError:
                # The identity repository already has bounded conflict handling.
                # Do not turn its known duplicate result into a new repair right.
                if collection == "role_identities":
                    raise
                raise self.fail("indicator_storage_unavailable") from None
            except ForegroundMongoFailure:
                raise
            except asyncio.CancelledError:
                # Loop shutdown may cancel owners; the native Future/slot remains.
                if write and record["native"] is not None:
                    self.controller.unknown(record)
                raise
            except Exception as exc:
                query_exception(exc, fallback="storage")
                timed_out = isinstance(exc, asyncio.TimeoutError) or bool(getattr(exc, "timeout", False))
                if write and record["native"] is not None and (timed_out or isinstance(exc, AutoReconnect)):
                    self.controller.unknown(record)
                code = "indicator_query_timeout" if timed_out else "indicator_storage_unavailable"
                raise self.fail(code) from None
            finally:
                if record["native"] is None:
                    self.controller.release(record)
                    self._inflight.discard(record["id"])

        owner = asyncio.create_task(native_owner())
        record["owner"] = owner
        owner.add_done_callback(_consume)
        try:
            return await asyncio.shield(owner)
        except asyncio.CancelledError:
            if write and record["native"] is not None:
                self.controller.unknown(record)
            raise

    async def settle(self, timeout):
        until = time.monotonic() + max(0.0, timeout)
        while self._inflight and time.monotonic() < until:
            await asyncio.sleep(min(.005, max(0.0, until - time.monotonic())))
        return not self._inflight

    def bind_repo(self, repo):
        if repo is None:
            return None
        if isinstance(repo, _RepoFacade) and repo._session is self:
            return repo
        # A shared identity owner outlives its creating request. Rebinding must
        # remove both the old method guard and its database/session ownership.
        while isinstance(repo, _RepoFacade):
            repo = repo._repo
        if is_dataclass(repo) and any(item.name == "db" and item.init for item in fields(repo)):
            database = repo.db
            while isinstance(database, _Database):
                database = database._database
            repo = replace(repo, db=_Database(database, self))
        return _RepoFacade(repo, self)


class _RepoFacade:
    def __init__(self, repo, session):
        self._repo, self._session = repo, session

    def __getattr__(self, name):
        member = getattr(self._repo, name)
        if not inspect.iscoroutinefunction(member):
            return member

        @functools.wraps(member)
        async def guarded(*args, **kwargs):
            self._session.context.check()
            try:
                cache_method = name in {"load_role_indicator", "save_role_indicator", "load_role_recent",
                    "save_role_recent", "load_ranking_cache", "save_ranking_cache", "load_new_kungfu_cache_raw"}
                with query_stage("repository"), query_stage("cache" if cache_method else None):
                    result = await member(*args, **kwargs)
            except Exception as exc:
                query_exception(exc, fallback="storage")
                self._session.context.check()
                raise
            if name == "load_role_indicator":
                query_cache(format="miss" if result is None else
                            "scoped" if isinstance(result, dict) and "scope" in result else
                            "legacy" if isinstance(result, dict) else "invalid")
            # Existing optional I/O policies may return None on failure. The
            # sticky request-local failure prevents that becoming business miss.
            self._session.context.check()
            return result
        return guarded


class _Database:
    def __init__(self, database, session):
        self._database, self._session = database, session

    def _resolve(self):
        return self._database if self._database is not None else get_db()

    def __getitem__(self, name):
        if name not in COLLECTIONS:
            raise ForegroundMongoFailure("indicator_storage_unavailable")
        return _Collection(self, name)

    def __getattr__(self, name):
        if name == "name":
            return self._resolve().name
        return self[name]


class _Collection:
    def __init__(self, database, name):
        self._database, self.name = database, name
        self._session = database._session

    @property
    def full_name(self):
        return self._database._resolve()[self.name].full_name

    def _cap_ms(self):
        self._session.context.check()
        return max(1, math.ceil(1000 * min(self._session.controller.operation_timeout_seconds,
                                         self._session.context.remaining())))

    def __getattr__(self, name):
        if name in ("find", "aggregate"):
            def cursor(*args, **kwargs):
                parameter = "max_time_ms" if name == "find" else "maxTimeMS"
                kwargs[parameter] = min(kwargs.get(parameter, self._cap_ms()), self._cap_ms())
                target = getattr(self._database._resolve()[self.name], name)(*args, **kwargs)
                return _Cursor(target, self._session, self.name)
            return cursor
        if name not in READS and name not in WRITES:
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            self._session.context.check()
            if name in READS:
                parameter = "max_time_ms" if name == "find_one" else "maxTimeMS"
                kwargs[parameter] = min(kwargs.get(parameter, self._cap_ms()), self._cap_ms())
            return self._session.execute(
                lambda: getattr(self._database._resolve()[self.name], name)(*args, **kwargs),
                write=name in WRITES, collection=self.name,
            )
        return invoke


class _Cursor:
    def __init__(self, cursor, session, collection):
        self._cursor, self._session, self._collection = cursor, session, collection

    def __getattr__(self, name):
        if name in ("sort", "limit", "batch_size", "skip", "max_time_ms"):
            def chain(*args, **kwargs):
                self._session.context.check()
                self._cursor = getattr(self._cursor, name)(*args, **kwargs)
                return self
            return chain
        if name == "to_list":
            def execute(*args, **kwargs):
                return self._session.execute(lambda: self._cursor.to_list(*args, **kwargs), collection=self._collection)
            return execute
        raise AttributeError(name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._session.execute(lambda: self._cursor.__anext__(), collection=self._collection)
