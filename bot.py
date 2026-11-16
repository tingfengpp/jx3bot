#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import nonebot
from nonebot.adapters.onebot.v11 import Adapter

from src.api import register_api

# You can pass some keyword args config to init function
nonebot.init()
app = nonebot.get_asgi()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)

register_api(app)


@driver.on_startup
async def _startup_mongo():
    from config import MONGO_URI
    from src.infra.mongo import init_mongo
    from src.services.jx3.singletons import jjc_foreground_client
    from src.services.jx3.jjc_foreground_runtime import start_jjc_foreground
    from src.services.jx3.jjc_sync_worker_runtime import start_jjc_sync_dispatcher, start_jjc_sync_workers
    from src.services.jx3.jjc_live_refresh_runtime import start_live_refresh
    from src.services.jx3.jjc_kungfu_refresh_runtime import start_kungfu_refresh

    try:
        await start_jjc_foreground(jjc_foreground_client)
        await init_mongo(MONGO_URI)
        await start_jjc_sync_workers()
        await start_jjc_sync_dispatcher()
        await start_live_refresh()
        await start_kungfu_refresh()
    except BaseException:
        await _shutdown_jjc_resources()
        raise


async def _shutdown_jjc_resources():
    from src.services.jx3.singletons import jjc_foreground_client
    from src.services.jx3.jjc_foreground_runtime import stop_jjc_foreground
    from src.services.jx3.jjc_sync_worker_runtime import stop_jjc_sync_dispatcher, stop_jjc_sync_workers
    from src.services.jx3.jjc_kungfu_refresh_runtime import stop_kungfu_refresh
    from src.services.jx3.jjc_live_refresh_runtime import stop_live_refresh
    from src.infra.mongo import close_mongo

    async def stop_equipment():
        import sys
        assembly = sys.modules.get("src.services.jx3.singletons")
        service = getattr(assembly, "jjc_ranking_equipment_service", None)
        runtime = getattr(assembly, "jjc_equipment_read_runtime", None)
        try:
            if service is not None:
                await service.close()
        finally:
            if runtime is not None:
                await runtime.close()

    stops = (("equipment", stop_equipment), ("foreground", lambda: stop_jjc_foreground(jjc_foreground_client)),
             ("kungfu", stop_kungfu_refresh), ("dispatcher", stop_jjc_sync_dispatcher),
             ("workers", stop_jjc_sync_workers), ("live", stop_live_refresh), ("mongo", close_mongo))
    for component, stop in stops:
        try:
            complete = await stop()
            if complete is False:
                nonebot.logger.warning("JJC lifecycle cleanup incomplete: component={}", component)
        except Exception as error:
            # Component names and exception classes are fixed diagnostic data.
            nonebot.logger.warning("JJC lifecycle cleanup failed: component={} exception_type={}",
                                   component, type(error).__name__)


@driver.on_shutdown
async def _shutdown_jjc_sync_workers():
    await _shutdown_jjc_resources()


# Please DO NOT modify this file unless you know what you are doing!
# As an alternative, you should use command `nb` or modify `pyproject.toml` to load plugins
nonebot.load_from_toml("pyproject.toml")

# Modify some config / config depends on loaded configs
#
# config = driver.config
# do something...


if __name__ == "__main__":
    nonebot.logger.warning("Always use `nb run` to start the bot instead of manually running!")
    nonebot.run(app="__mp_main__:app")
