"""Bounded, read-only, whole-board local winning-match equipment views."""
import asyncio
from functools import wraps
from collections import OrderedDict
from copy import deepcopy
import time
import math

from src.utils.jjc_equipment_read_diagnostics import report_equipment_read_failure

from src.services.jx3.jjc_ranking_equipment import (aggregate_equipment_view,
    validate_local_win, public_equipment_view, positive_int, content_revision, required_local_win_snapshot_hashes)


class EquipmentViewError(RuntimeError):
    def __init__(self, code, *, revocations=None):
        self.code = code
        self.revocations = deepcopy(revocations or [])
        super().__init__(code)


def _owned_equipment_request(method):
    """Track the entire internal request, including bootstrap/qualification IO."""
    @wraps(method)
    async def owned(self, *args, **kwargs):
        if self._closed:
            raise EquipmentViewError("closed")
        task = asyncio.create_task(method(self, *args, **kwargs))
        self._requests.add(task)
        try:
            return await task
        finally:
            self._requests.discard(task)
    return owned


class JjcRankingEquipmentViewService:
    def __init__(self, *, stats_repo, identity_repo, inspect_repo, snapshot_repo,
                 scope, dynamic_stats_repo=None, enabled=True, overlay_enabled=True, clock=time.time,
                 monotonic=time.monotonic, build_timeout=8, request_timeout=10,
                 ttl=15, max_waiters=32):
        self.stats_repo, self.identity_repo = stats_repo, identity_repo
        self.dynamic_stats_repo = stats_repo if dynamic_stats_repo is None else dynamic_stats_repo
        self.inspect_repo, self.snapshot_repo = inspect_repo, snapshot_repo
        self.scope, self.enabled, self.overlay_enabled = scope, enabled, overlay_enabled
        self.clock, self.monotonic = clock, monotonic
        self.build_timeout, self.request_timeout = min(8, build_timeout), min(10, request_timeout)
        self.ttl, self.max_waiters = min(15, ttl), min(32, max_waiters)
        if min(self.build_timeout, self.request_timeout, self.ttl, self.max_waiters) <= 0:
            raise ValueError("invalid_equipment_budget")
        self._slot = None
        self._closed = False
        self._cache, self._revocations = OrderedDict(), OrderedDict()
        self._pending, self._waiters, self._owners = {}, {}, set()
        self._requests = set()
        self._read_flights, self._read_waiters = {}, {}
        self._close_task = None

    async def _observe(self, stage, awaitable):
        started = time.monotonic()
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            try:
                report_equipment_read_failure(stage, exc, time.monotonic() - started)
            except BaseException:
                pass
            raise

    @staticmethod
    def _flag(value):
        return (value() if callable(value) else value) in (True, 1)

    def _scope(self):
        value = self.scope() if callable(self.scope) else self.scope
        return deepcopy(getattr(value, "scope", value))

    def _key(self, timestamp, scope, member_revision):
        return (timestamp, content_revision(scope), member_revision)

    def _revoke(self, key, scope, member, reason):
        entries = self._revocations.setdefault(key, {})
        token = (member["rank"], member.get("binding_revision"))
        entries[token] = {"timestamp": key[0], "scope": deepcopy(scope), "member_revision": key[2],
                          "binding_revision": token[1], "rank": token[0],
                          "confirmed_at": self.clock(), "reason": reason}
        self._revocations.move_to_end(key)
        while len(self._revocations) > 2:
            self._revocations.popitem(last=False)

    def _revoked(self, key):
        return list(deepcopy(self._revocations.get(key, {})).values())

    @staticmethod
    def _identity_id(member):
        return str(member.get("display_identity_id") or member.get("identity_id") or "")

    def snapshot_read_repo(self):
        """Keep enabled snapshot reads on the bounded pool; off uses compatibility IO."""
        return self.dynamic_stats_repo if self._flag(self.enabled) else self.stats_repo

    async def _bindings(self, members):
        ids = list(dict.fromkeys(self._identity_id(m) for m in members if self._identity_id(m)))
        docs = await self.identity_repo.get_equipment_bindings_by_ids(ids) if ids else []
        by_id = {str(doc["_id"]): doc for doc in docs}
        answer = {}
        for m in members:
            row = by_id.get(self._identity_id(m))
            good = bool(row and positive_int(str(m.get("global_id") or ""))
                        and str(row.get("global_id") or "") == str(m["global_id"]))
            locator = m.get("display_locator") or {}
            if good:
                good = all(row.get(k) == v for k, v in locator.items() if v is not None)
            answer[m["rank"]] = good
        return answer

    async def _construct(self, *, timestamp, members, scope, member_revision):
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        async with self._slot:
            if self._closed:
                raise EquipmentViewError("closed")
            rows = deepcopy(members)
            key = self._key(timestamp, scope, member_revision)
            now = self.clock()
            for m in rows:
                m.setdefault("binding_revision", content_revision({k: m.get(k) for k in
                    ("rank", "global_id", "display_identity_id", "identity_id", "display_locator")}))
            bindings = await self._observe("binding_before", self._bindings(rows))
            ids = []
            for m in rows:
                if not bindings[m["rank"]]:
                    m.update(local_win_packet=None, equipment_status="binding_invalid")
                    self._revoke(key, scope, m, "binding_invalid")
                else:
                    ids.append(str(m["global_id"]))
            candidates = await self._observe("candidates", self.inspect_repo.list_local_win_candidates_batch(
                list(dict.fromkeys(ids)), season_start=scope["season_start"], checked_at=now,
                max_time_ms=1500, season_id=scope["season_id"])) if ids else []
            indexed, by_match = {}, {}
            wanted = set(ids)
            for doc in candidates:
                data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
                detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
                match_id = positive_int(doc.get("match_id"))
                if match_id is not None:
                    by_match[match_id] = doc
                seen = set()
                for side in ("team1", "team2"):
                    team = detail.get(side)
                    players = team.get("players_info") if isinstance(team, dict) else []
                    for player in players if isinstance(players, list) else []:
                        if not isinstance(player, dict):
                            continue
                        gid = str(player.get("global_id") or player.get("globalId") or "")
                        if gid in wanted and gid not in seen:
                            indexed.setdefault(gid, []).append(doc)
                            seen.add(gid)
            selected, hashes = {}, set()
            for m in rows:
                if bindings[m["rank"]]:
                    result = validate_local_win(member=m, candidates=indexed.get(str(m["global_id"]), []), scope=scope, checked_at=now)
                    selected[m["rank"]] = result
                    if result["packet"]:
                        doc = by_match.get(result["packet"]["match_id"], {})
                        hashes.update(required_local_win_snapshot_hashes(m, doc))
            snapshots = await self._observe("snapshots", self.snapshot_repo.load_local_win_equipment_snapshots(sorted(hashes))) if hashes else {}
            for m in rows:
                if not bindings[m["rank"]]:
                    continue
                result = selected[m["rank"]]
                if result["packet"]:
                    result = validate_local_win(member=m, candidates=[by_match[result["packet"]["match_id"]]],
                        scope=scope, checked_at=now, equipment_snapshots=snapshots)
                m.update(local_win_packet=result["packet"], equipment_status=result["state"])
                if result["state"] in ("no_local_win", "data_inconsistent"):
                    self._revoke(key, scope, m, result["state"])
                else:
                    self._revocations.get(key, {}).pop((m["rank"], m["binding_revision"]), None)
            # Recheck identity after match/snapshot awaits; never publish a role that changed in flight.
            fresh = await self._observe("binding_after", self._bindings(rows))
            for m in rows:
                if not fresh[m["rank"]]:
                    m.update(local_win_packet=None, equipment_status="binding_invalid")
                    self._revoke(key, scope, m, "binding_invalid")
            return aggregate_equipment_view(timestamp=timestamp, scope=scope, members=rows,
                checked_at=now, completed_at=self.clock(), member_revision=member_revision)

    async def _bounded_build(self, **kwargs):
        task = asyncio.create_task(self._construct(**kwargs))
        self._owners.add(task)
        key = self._key(kwargs["timestamp"], kwargs["scope"], kwargs["member_revision"])
        try:
            return await self._observe("construct", asyncio.wait_for(task, self.build_timeout))
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except EquipmentViewError:
            raise
        except Exception:
            raise EquipmentViewError("read_failed", revocations=self._revoked(key)) from None
        finally:
            self._owners.discard(task)

    @_owned_equipment_request
    async def build_frozen_view(self, *, timestamp, members):
        if self._closed:
            raise EquipmentViewError("closed")
        if not self._flag(self.enabled):
            raise EquipmentViewError("disabled")
        async def build():
            before = await self.dynamic_stats_repo.load_publication_baseline(timestamp)
            expected = ({"expected_revision": before["equipment_view_revision"]} if before and before.get("schema_version") == 2
                        else {"expected_legacy": deepcopy(before)} if before else {})
            if before and before.get("ranking_display_kind") == "historical":
                raise EquipmentViewError("historical")
            scope = self._scope()
            revision = content_revision({"scope": scope, "members": [{k: m.get(k) for k in
                ("rank", "name", "server", "score", "kungfu", "kungfu_id", "lane", "global_id", "display_identity_id", "identity_id", "display_locator")} for m in members]})
            view = await self._bounded_build(timestamp=timestamp, members=members, scope=scope, member_revision=revision)
            view["_publication_expected"] = expected
            return view
        try:
            return await self._observe("read", asyncio.wait_for(build(), self.request_timeout))
        except asyncio.CancelledError:
            raise
        except EquipmentViewError:
            raise
        except Exception:
            raise EquipmentViewError("read_failed") from None

    async def _qualify(self, timestamp, member_revision, frozen_source):
        context = await self._observe("context", self.dynamic_stats_repo.load_frozen_equipment_context(timestamp))
        scope = self._scope()
        latest = await self._observe("latest", self.dynamic_stats_repo.get_latest_current_summary(scope))
        response = {"timestamp": timestamp, "overlay_enabled": False, "retired": False,
                    "reload_required": False, "reason": None, "latest_timestamp": latest.get("timestamp") if latest else None,
                    "current_member_revision": context.get("member_revision") if context else None,
                    "current_frozen_source": context.get("frozen_source") if context else None, "view": None}
        if not context or not context.get("complete"):
            response["reason"] = "ineligible_legacy"
            return response, None
        if (context.get("ranking_display_kind") != "current" or context.get("scope") != scope
                or not latest or latest.get("timestamp") != timestamp):
            response.update(retired=True, reason="retired")
            return response, None
        if context["member_revision"] != member_revision or context["frozen_source"] != frozen_source:
            response.update(reload_required=True, reason="member_revision_changed" if context["member_revision"] != member_revision else "source_changed")
            return response, None
        return response, context

    async def _read(self, timestamp, expected_member_revision, expected_frozen_source):
        response, context = await self._observe("qualify_before", self._qualify(timestamp, expected_member_revision, expected_frozen_source))
        if context is None:
            return response
        key = self._key(timestamp, context["scope"], context["member_revision"])
        cached = self._cache.get(key)
        if cached and self.monotonic() - cached[0] < self.ttl:
            view = deepcopy(cached[1])
            bindings = await self._observe("binding_cached", self._bindings(context["members"]))
            if any(not good for good in bindings.values()):
                cached = None
        if not cached or self.monotonic() - cached[0] >= self.ttl:
            if self._waiters.get(key, 0) >= self.max_waiters:
                raise EquipmentViewError("busy")
            task = self._pending.get(key)
            if task is None:
                task = asyncio.create_task(self._bounded_build(timestamp=timestamp, members=context["members"],
                    scope=context["scope"], member_revision=context["member_revision"]))
                self._pending[key] = task
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            self._waiters[key] = self._waiters.get(key, 0) + 1
            try:
                view = await asyncio.shield(task)
            finally:
                self._waiters[key] -= 1
                if self._waiters[key] == 0:
                    self._waiters.pop(key, None)
                    self._pending.pop(key, None)
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            if not self._closed:
                self._cache[key] = (self.monotonic(), deepcopy(view))
                self._cache.move_to_end(key)
                while len(self._cache) > 2:
                    self._cache.popitem(last=False)
        response, fresh = await self._observe("qualify_after", self._qualify(timestamp, expected_member_revision, expected_frozen_source))
        if fresh is None or self._closed or not self._flag(self.enabled) or not self._flag(self.overlay_enabled):
            return response
        response.update(overlay_enabled=True, checked_at=self.clock(), view=public_equipment_view(view),
                        revocations=self._revoked(key))
        return response

    @_owned_equipment_request
    async def read_dynamic_view(self, *, timestamp, expected_member_revision, expected_frozen_source):
        if self._closed:
            raise EquipmentViewError("closed")
        response = {"timestamp": timestamp, "overlay_enabled": False, "retired": False,
                    "reload_required": False, "reason": "disabled", "view": None}
        if not self._flag(self.enabled) or not self._flag(self.overlay_enabled):
            return response
        # Share the entire in-flight read, including both qualification checks.
        # A completed flight is never a qualification cache: the next request
        # starts fresh IO even while the separately bounded content cache is hot.
        flight_key = (timestamp, content_revision(self._scope()),
                      expected_member_revision, expected_frozen_source)
        if self._read_waiters.get(flight_key, 0) >= self.max_waiters:
            raise EquipmentViewError("busy")
        task = self._read_flights.get(flight_key)
        if task is None:
            task = asyncio.create_task(asyncio.wait_for(
                self._read(timestamp, expected_member_revision, expected_frozen_source),
                self.request_timeout))
            self._read_flights[flight_key] = task
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self._read_waiters[flight_key] = self._read_waiters.get(flight_key, 0) + 1
        try:
            result = await self._observe("read", asyncio.wait_for(asyncio.shield(task), self.request_timeout))
            return deepcopy(result)
        except asyncio.CancelledError:
            raise
        except EquipmentViewError:
            raise
        except Exception:
            key = self._key(timestamp, self._scope(), expected_member_revision)
            raise EquipmentViewError("read_failed", revocations=self._revoked(key)) from None
        finally:
            remaining = self._read_waiters[flight_key] - 1
            if remaining:
                self._read_waiters[flight_key] = remaining
            else:
                self._read_waiters.pop(flight_key, None)
                self._read_flights.pop(flight_key, None)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    @_owned_equipment_request
    async def read_frozen_snapshot(self, timestamp, expected_view_revision=None):
        """Project one complete frozen context; never fetch summary a second time."""
        if self._closed:
            raise EquipmentViewError("closed")
        from src.services.jx3.jjc_ranking_equipment import (
            apply_revocations, equipment_view_statistics,
        )
        try:
            context = await asyncio.wait_for(
                self.snapshot_read_repo().load_frozen_equipment_context(timestamp), self.request_timeout)
            if context is None:
                return None
            summary = context.get("summary") or {}
            source = context.get("frozen_source", "legacy_v1")
            revision = context.get("member_revision")
            view = context.get("frozen_view")
            if source == "local_win_v2":
                if not context.get("complete") or not isinstance(view, dict):
                    raise EquipmentViewError("read_failed")
                key = self._key(timestamp, context["scope"], revision)
                instructions = self._revoked(key)
                if instructions:
                    view = apply_revocations(view, instructions)
                if (expected_view_revision is not None
                        and expected_view_revision != view.get("equipment_view_revision")):
                    raise EquipmentViewError("version_mismatch")
                public = public_equipment_view(view)
                # Everything exposed as V2 statistics is derived from this exact
                # public view; legacy outer fields are never copied into it.
                payload = {k: deepcopy(summary[k]) for k in (
                    "generated_at", "ranking_cache_time", "default_week", "current_season",
                    "week_info", "ranking_display_kind", "refresh_degraded")
                    if k in summary and (summary[k] is None or type(summary[k]) in (str, int, float, bool))
                    and (type(summary[k]) is not float or math.isfinite(summary[k]))}
                payload.update(timestamp=timestamp, schema_version=2,
                    current_season=public["scope"]["season_id"],
                    ranking_display_kind=context.get("ranking_display_kind", "current"),
                    kungfu_cache_scope=deepcopy(public["scope"]), equipment_source="local_win_v2",
                    equipment_complete=True, member_revision=revision, frozen_source=source,
                    equipment_view_revision=public["equipment_view_revision"], equipment_view=public,
                    kungfu_statistics=equipment_view_statistics(public))
            else:
                if expected_view_revision is not None:
                    raise EquipmentViewError("version_mismatch")
                # Schema1 has its original complete legacy source, not a
                # fabricated local-win packet. The manifest only identifies its
                # frozen roster for a later optional whole-board conversion.
                payload = {k: deepcopy(v) for k, v in summary.items() if k not in (
                    "_id", "_publication_expected", "_sync_request")}
                payload.update(timestamp=timestamp, frozen_source="legacy_v1")
                if context.get("complete"):
                    payload["member_revision"] = revision
                    fields = ("rank", "name", "server", "score", "kungfu", "kungfu_id",
                              "lane", "found", "binding_revision")
                    payload["equipment_members"] = [
                        {k: deepcopy(m[k]) for k in fields if k in m
                         and (m[k] is None or type(m[k]) in (str, int, float, bool))}
                        for m in context.get("members") or []]
                else:
                    payload.update(equipment_ineligible=True, equipment_reason="ineligible_legacy")
            payload["bootstrap_manifest"] = {"timestamp": timestamp,
                "member_revision": revision, "frozen_source": source,
                "complete": bool(context.get("complete"))}
            return payload
        except asyncio.CancelledError:
            raise
        except EquipmentViewError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "version_mismatch":
                raise EquipmentViewError(code) from None
            raise EquipmentViewError("read_failed") from None

    async def _drain_requests(self):
        # Cancel the roots first and let wait_for/singleflight propagate to their
        # children. Cancelling both levels at once can interrupt child finally
        # cleanup with a second cancellation, notably on Python 3.9.
        for remaining in (lambda: self._requests, lambda: self._read_flights.values(),
                          lambda: self._pending.values(), lambda: self._owners):
            tasks = {task for task in remaining() if not task.done()}
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        self._read_flights.clear()
        self._read_waiters.clear()
        self._pending.clear()
        self._cache.clear()

    async def close(self):
        # Exactly one cancellation pass: repeated close must not interrupt
        # asynchronous finally blocks entered by the first cancellation.
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._drain_requests())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # A caller's finally may close the runtime. Delay propagating its
            # cancellation until this shared drain completes (also on 3.9).
            while not self._close_task.done():
                try:
                    await asyncio.shield(self._close_task)
                except asyncio.CancelledError:
                    continue
            self._close_task.result()
            raise
