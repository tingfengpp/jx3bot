"""Cancellable foreground transport; URLs are selected by a trusted endpoint map."""
import asyncio
import json
import time
from typing import Any, Callable, Dict, Optional
from weakref import WeakKeyDictionary, WeakSet

import httpx
import anyio

from src.utils.tuilan_request import build_tuilan_request
from src.utils.jjc_query_observability import observed_query_stage, query_outbound, query_failure


FOREGROUND_ENDPOINTS = frozenset(("role_indicator", "match_history", "arena_time_tag",
                                  "arena_ranking", "match_detail", "match_replay"))


class TuilanForegroundClient:
    def __init__(self, *, endpoint_urls: Dict[str, str], signer: Optional[Callable] = None,
                 client_factory: Optional[Callable] = None) -> None:
        if not set(endpoint_urls).issubset(FOREGROUND_ENDPOINTS):
            raise ValueError("unsupported_foreground_endpoint")
        self.endpoint_urls = dict(endpoint_urls)
        self.signer = signer or build_tuilan_request
        self.client_factory = client_factory
        self._clients = WeakKeyDictionary()
        self._closed_loops = WeakSet()
        self._close_failed_loops = WeakSet()
        self._owners = WeakKeyDictionary()
        self._closing = WeakKeyDictionary()

    def _client(self):
        loop = asyncio.get_running_loop()
        if loop in self._closed_loops:
            raise RuntimeError("foreground_client_closed")
        client = self._clients.get(loop)
        if client is None:
            client = self.client_factory() if self.client_factory else httpx.AsyncClient(
                verify=False, follow_redirects=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            )
            self._clients[loop] = client
        return client

    async def request(self, endpoint: str, params: Dict[str, Any], *, deadline: float) -> Dict[str, Any]:
        return await self._request(endpoint, params, deadline=deadline)

    async def request_with_owner(self, endpoint, params, *, deadline, on_owner):
        """Expose completion so the caller can retain its existing physical lease."""
        return await self._request(endpoint, params, deadline=deadline, on_owner=on_owner)

    @observed_query_stage("http")
    async def _request(self, endpoint, params, *, deadline, on_owner=None):
        if endpoint not in FOREGROUND_ENDPOINTS or endpoint not in self.endpoint_urls:
            raise ValueError("unsupported_foreground_endpoint")
        if asyncio.get_running_loop() in self._closed_loops:
            raise RuntimeError("foreground_client_closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError()

        async def perform():
            client = self._client()
            headers, raw_json = self.signer(dict(params))
            content = raw_json.encode("utf-8") if isinstance(raw_json, str) else raw_json
            left = deadline - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError()
            timeout = httpx.Timeout(left, connect=min(2.0, left))
            # Response ownership includes the whole body; trickle reads cannot
            # extend the outer absolute deadline. No physical retry loop exists.
            request = client.build_request('POST', self.endpoint_urls[endpoint], headers=headers,
                                           content=content, timeout=timeout)
            query_outbound()
            response = await client.send(request, stream=True)
            try:
                if response.status_code >= 400:
                    query_failure("upstream_rejected")
                    return {"error": "upstream_rejected", "status_code": response.status_code}
                body = await response.aread()
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    query_failure("invalid_response", exc)
                    return {"error": "invalid_response"}
                if not isinstance(result, dict):
                    query_failure("invalid_response")
                    return {"error": "invalid_response"}
                return result
            finally:
                # Scope cancellation closes IO once. Repeated cancellation of
                # the public waiter cannot interrupt this response ownership.
                with anyio.CancelScope(shield=True):
                    await response.aclose()

        state = {}
        async def physical_owner():
            with anyio.CancelScope() as scope:
                state['scope'] = scope
                result = await perform()
            if scope.cancel_called:
                raise asyncio.TimeoutError()
            return result

        loop = asyncio.get_running_loop()
        owner = asyncio.create_task(physical_owner())
        owners = self._owners.setdefault(loop, {})
        owners[owner] = state
        def completed(done):
            owners.pop(done, None)
            if not done.cancelled():
                done.exception()
        owner.add_done_callback(completed)
        if on_owner is not None:
            on_owner(owner)
        try:
            return await asyncio.wait_for(asyncio.shield(owner), timeout=remaining)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            query_failure("timeout", exc)
            raise asyncio.TimeoutError() from None
        except httpx.TransportError as exc:
            query_failure("transport", exc)
            return {"error": "upstream_transport_unavailable"}
        except asyncio.CancelledError:
            query_failure("cancelled")
            raise
        finally:
            if not owner.done():
                scope = state.get('scope')
                if scope is None:
                    owner.cancel()  # no IO was submitted before its first step
                else:
                    scope.cancel()
                until = time.monotonic() + .25
                while not owner.done() and time.monotonic() < until:
                    try:
                        await asyncio.wait_for(asyncio.shield(owner), max(0, until - time.monotonic()))
                    except asyncio.CancelledError:
                        # A second caller cancellation must not reach the owner.
                        continue
                    except Exception:
                        break

    async def start(self) -> None:
        """Explicitly start; an unconfirmed close requires a new process lifecycle."""
        loop = asyncio.get_running_loop()
        if loop in self._close_failed_loops:
            raise RuntimeError("foreground_client_close_failed")
        closing = self._closing.get(loop)
        if closing is not None and not closing.done():
            raise RuntimeError("foreground_client_closing")
        if loop in self._closed_loops and any(not task.done() for task in self._owners.get(loop, {})):
            raise RuntimeError("foreground_client_closing")
        self._closed_loops.discard(loop)
        try:
            self._client()
        except BaseException:
            self._closed_loops.add(loop)
            raise

    def snapshot(self):
        loop = asyncio.get_running_loop()
        closing = self._closing.get(loop)
        return {"accepting": loop not in self._closed_loops,
                "clients": int(loop in self._clients),
                "owners": sum(not task.done() for task in self._owners.get(loop, {})),
                "closing": bool(closing is not None and not closing.done()),
                "close_failed": loop in self._close_failed_loops}

    async def aclose(self, *, timeout: float = .25) -> bool:
        """Stop this loop first; a bounded waiter retains actual cleanup ownership."""
        loop = asyncio.get_running_loop()
        self._closed_loops.add(loop)
        if loop in self._close_failed_loops:
            # HTTPX marks CLOSED before awaiting its transport; a later no-op
            # aclose cannot prove that the original physical close succeeded.
            return False
        closer = self._closing.get(loop)
        if closer is None:
            async def close_resources():
                pending = []
                for owner, state in tuple(self._owners.get(loop, {}).items()):
                    if owner.done():
                        continue
                    pending.append(owner)
                    scope = state.get('scope')
                    if scope is None:
                        owner.cancel()  # No HTTP has started before its first step.
                    else:
                        scope.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=.25)
                client = self._clients.get(loop)
                if client is not None:
                    try:
                        await client.aclose()
                    except BaseException:
                        # Retain the actual client and keep this loop closed.
                        # Never discard unknown resources and open another pool.
                        self._close_failed_loops.add(loop)
                        raise
                    if self._clients.get(loop) is client:
                        self._clients.pop(loop, None)

            closer = asyncio.create_task(close_resources())
            self._closing[loop] = closer
            def finished(done):
                if self._closing.get(loop) is done:
                    self._closing.pop(loop, None)
                if not done.cancelled():
                    done.exception()
            closer.add_done_callback(finished)
        try:
            await asyncio.wait_for(asyncio.shield(closer), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return False
        return not self.snapshot()["owners"]
