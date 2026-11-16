"""Pure locator rules for interactive role queries.

A middle dot can be part of a real name. Only the resolver, after an exact
lookup misses, may interpret a matching trailing server as a display suffix.
"""
from typing import Any, Dict, List, Tuple
from src.utils.jjc_query_observability import query_flight


def role_name_candidates(server: str, name: str) -> Tuple[str, ...]:
    server, name = str(server or "").strip(), str(name or "").strip()
    suffix = "·" + server
    if server and name.endswith(suffix) and len(name) > len(suffix):
        return name, name[:-len(suffix)]
    return (name,)


def identity_hints_conflict(identity: Dict[str, Any], server: str,
                            hints: Dict[str, Any]) -> bool:
    """Hints cannot relabel an existing identity or replace its nonempty IDs."""
    actual_server = str(identity.get("server") or "").strip()
    if actual_server and actual_server != server:
        return True
    for field in ("global_id", "global_role_id", "game_role_id", "role_id", "zone"):
        wanted = str(hints.get(field) or "").strip()
        actual = identity.get(field)
        if field in ("game_role_id", "role_id") and not actual:
            actual = identity.get("role_id" if field == "game_role_id" else "game_role_id")
        actual = str(actual or "").strip()
        if wanted and actual and wanted != actual:
            return True
    return False


def local_lookup_hints(hints: Dict[str, Any]) -> Dict[str, Any]:
    """Map the compatibility role_id to the repository's zone/game locator."""
    result = {
        "zone": hints.get("zone"),
        "game_role_id": hints.get("game_role_id") or hints.get("role_id"),
        "global_id": hints.get("global_id"),
    }
    if hints.get("global_role_id"):
        result["global_role_id"] = hints["global_role_id"]
    return result


def ranking_name_matches(server: str, name: str, players: List[Any], *,
                         allowed_names: Tuple[str, ...]) -> List[Tuple[str, Dict[str, Any]]]:
    """Rank source-known display names by exact *actual* name, then alias.

    Do not intersect two sets of possible names: an earlier short-name player
    must not hide a later player whose actual name ends with a server suffix.
    """
    requested = allowed_names
    matches = []
    for position, player in enumerate(players):
        if not isinstance(player, dict):
            continue
        person = player.get("personInfo") or {}
        if not isinstance(person, dict) or str(person.get("server") or "").strip() != server:
            continue
        actual = role_name_candidates(server, person.get("roleName") or "")[-1]
        if actual in requested:
            matches.append((requested.index(actual), position, actual, person))
    return [(actual, person) for _, _, actual, person in sorted(matches)]


IDENTITY_BINDING_FIELDS = (
    "identity_id", "identity_key", "global_id", "global_role_id", "server",
    "name", "zone", "game_role_id", "role_id",
)


def identity_binding(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot identity facts without interpreting namespaces as ID evidence."""
    values = {field: str(identity[field]) if identity.get(field) is not None else None
              for field in IDENTITY_BINDING_FIELDS}
    source_id = identity.get("_id") or identity.get("identity_id")
    values["identity_id"] = str(source_id) if source_id is not None else None
    return values


def indicator_binding_matches(identity: Dict[str, Any], cached: Dict[str, Any]) -> bool:
    expected = identity_binding(identity)
    required = set(IDENTITY_BINDING_FIELDS) - {"global_id"}
    if any(not expected[field] for field in required):
        return False
    for field, value in expected.items():
        actual = str(cached[field]) if cached.get(field) is not None else None
        if actual != value or (field in required and field not in cached):
            return False
    return True


class ForegroundQueryError(Exception):
    """A fixed public failure code, with no upstream payload or identity."""
    code = "indicator_unavailable"


class ForegroundQueryTimeout(ForegroundQueryError):
    code = "indicator_query_timeout"


class ForegroundQueryBusy(ForegroundQueryError):
    code = "indicator_query_busy"


class ForegroundQueryContext:
    """Request-local monotonic policy. HTTP/Mongo mechanics stay in adapters."""
    def __init__(self, *, timeout: float, queue_wait: float, cleanup: float,
                 clock=None) -> None:
        import time
        self.clock = clock or time.monotonic
        self.deadline = self.clock() + max(0.0, timeout)
        self.queue_wait = max(0.0, queue_wait)
        self.cleanup = max(0.0, cleanup)
        self.cancelled = False
        self.failure = None
        self.mongo = None
        self.recovery_physical_owners = set()

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def check(self) -> None:
        if self.cancelled or self.remaining() <= 0:
            raise ForegroundQueryTimeout()
        if self.failure is not None:
            raise self.failure


class ForegroundFlights:
    """Process admission and ephemeral, loop/inspect/semantics-scoped interests."""
    def __init__(self):
        import threading
        self.guard = threading.Lock()
        self.active = 0
        self.entries = {}
        from weakref import WeakKeyDictionary, WeakSet
        self._stopped_loops = WeakSet()
        self._active_by_loop = WeakKeyDictionary()

    def admit(self):
        import asyncio
        loop = asyncio.get_running_loop()
        with self.guard:
            if loop in self._stopped_loops or self.active >= 64:
                raise ForegroundQueryBusy()
            self.active += 1
            self._active_by_loop[loop] = self._active_by_loop.get(loop, 0) + 1

    def leave(self):
        import asyncio
        loop = asyncio.get_running_loop()
        with self.guard:
            self.active -= 1
            count = self._active_by_loop.get(loop, 0) - 1
            if count:
                self._active_by_loop[loop] = count
            else:
                self._active_by_loop.pop(loop, None)

    def snapshot(self):
        with self.guard:
            entries = {id(entry): entry for entry in self.entries.values()}.values()
            waits = [entry['waiters'] for entry in entries]
            return dict(active_requests=self.active, flights=len(waits), max_waiters=max(waits, default=0))

    def _finished(self, entry, task):
        with self.guard:
            for key in entry['keys']:
                if self.entries.get(key) is entry:
                    self.entries.pop(key)
        if not task.cancelled():
            task.exception()

    async def wait(self, entry, context):
        import asyncio
        import copy
        with self.guard:
            if asyncio.get_running_loop() in self._stopped_loops or entry.get('abandoned') or entry['waiters'] >= 64:
                raise ForegroundQueryBusy()
            entry['waiters'] += 1
            waiters = entry['waiters']
        query_flight(waiters)
        try:
            context.check()
            result = await asyncio.wait_for(asyncio.shield(entry['task']), context.remaining())
            context.check()
            return copy.deepcopy(result)
        finally:
            with self.guard:
                entry['waiters'] -= 1
                abandoned = entry['waiters'] == 0 and not entry['task'].done()
                if abandoned:
                    entry['abandoned'] = True
            if abandoned:
                entry['task'].cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(entry['task']), min(.25, context.cleanup))
                except (asyncio.CancelledError, asyncio.TimeoutError, ForegroundQueryError):
                    pass

    async def run(self, key, context, factory):
        import asyncio
        with self.guard:
            if key[0] in self._stopped_loops:
                raise ForegroundQueryBusy()
            entry = self.entries.get(key)
            if entry is not None and entry.get('abandoned'):
                raise ForegroundQueryBusy()
            if entry is None:
                if len({id(item) for item in self.entries.values()}) >= 64:
                    raise ForegroundQueryBusy()
                owner_context = fork_foreground_context(context)
                entry = dict(keys={key}, waiters=0, task=None, abandoned=False)
                owner_context.flight_entry = entry
                entry['task'] = asyncio.create_task(factory(owner_context))
                self.entries[key] = entry
                entry['task'].add_done_callback(lambda task: self._finished(entry, task))
        return await self.wait(entry, context)

    def promote(self, key, context):
        """Only proven canonical identities can alias an original discovery key."""
        with self.guard:
            if key[0] in self._stopped_loops:
                raise ForegroundQueryBusy()
            own = context.flight_entry
            other = self.entries.get(key)
            if own.get('abandoned') or (other is not None and other.get('abandoned')):
                raise ForegroundQueryBusy()
            if other is not None and other is not own:
                return other
            self.entries[key] = own
            own['keys'].add(key)
        return None

    def stop_admission(self):
        import asyncio
        with self.guard:
            self._stopped_loops.add(asyncio.get_running_loop())

    def start_admission(self):
        import asyncio
        loop = asyncio.get_running_loop()
        with self.guard:
            if loop not in self._stopped_loops:
                return
            if self._active_by_loop.get(loop, 0) or any(key[0] is loop for key in self.entries):
                raise ForegroundQueryBusy()
            self._stopped_loops.discard(loop)

    def snapshot_loop(self):
        import asyncio
        loop = asyncio.get_running_loop()
        with self.guard:
            entries = {id(entry): entry for key, entry in self.entries.items() if key[0] is loop}.values()
            waits = [entry['waiters'] for entry in entries]
            return dict(active_requests=self._active_by_loop.get(loop, 0), flights=len(waits),
                        max_waiters=max(waits, default=0), accepting=loop not in self._stopped_loops)

    async def shutdown(self, timeout=.25):
        import asyncio
        import time
        loop = asyncio.get_running_loop()
        self.stop_admission()
        with self.guard:
            entries = {id(entry): entry for key, entry in self.entries.items() if key[0] is loop}.values()
            pending = []
            for entry in entries:
                if not entry['task'].done():
                    entry['abandoned'] = True
                    pending.append(entry['task'])
        for task in pending:
            task.cancel()
        until = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < until:
            snapshot = self.snapshot_loop()
            if not snapshot['active_requests'] and not snapshot['flights']:
                return True
            await asyncio.sleep(min(.005, max(0.0, until - time.monotonic())))
        snapshot = self.snapshot_loop()
        return not snapshot['active_requests'] and not snapshot['flights']


def fork_foreground_context(context):
    owner = ForegroundQueryContext(timeout=context.remaining(), queue_wait=context.queue_wait,
                                   cleanup=context.cleanup, clock=context.clock)
    owner.deadline = context.deadline
    return owner


_FOREGROUND_FLIGHTS = ForegroundFlights()


def foreground_flights():
    return _FOREGROUND_FLIGHTS


def release_after_owners(release, owners):
    """Transfer an existing lease until every retained physical owner finishes."""
    pending = {owner for owner in owners if not owner.done()}
    if not pending:
        release()
        return
    def finished(owner):
        pending.discard(owner)
        if not pending:
            release()
    for owner in tuple(pending):
        owner.add_done_callback(finished)
