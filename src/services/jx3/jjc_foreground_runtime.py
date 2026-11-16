"""Bot lifecycle composition for loop-local foreground resources."""
import asyncio
import time

from src.infra.jjc_foreground_mongo import default_controller
from src.services.jx3.jjc_role_query_context import foreground_flights
from src.utils.jjc_query_observability import install_role_query_access_privacy


async def start_jjc_foreground(client):
    install_role_query_access_privacy()
    controller = default_controller()
    if controller.snapshot()["faulted"]:
        raise RuntimeError("foreground_storage_not_ready")
    await client.start()
    foreground_flights().start_admission()


async def stop_jjc_foreground(client, *, timeout=2.5):
    registry = foreground_flights()
    # This happens before the first await and before any unrelated stop hook.
    registry.stop_admission()
    until = time.monotonic() + max(0.0, timeout)
    left = lambda: max(0.0, until - time.monotonic())
    await registry.shutdown(timeout=min(.25, left()))
    await client.aclose(timeout=min(.25, left()))
    await default_controller().drain(timeout=min(2.0, left()))
    while left():
        flights, transport = registry.snapshot_loop(), client.snapshot()
        if not flights["active_requests"] and not flights["flights"] and not transport["owners"] and not transport["closing"]:
            break
        await asyncio.sleep(min(.005, left()))
    flights, transport = registry.snapshot_loop(), client.snapshot()
    return (not flights["active_requests"] and not flights["flights"] and not transport["owners"]
            and not transport["clients"] and not transport["closing"]
            and default_controller().snapshot()["outstanding"] == 0)
