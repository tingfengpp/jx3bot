"""Check current QQ membership before an explicit group delivery."""
import asyncio

from nonebot import logger


class GroupDeliveryBlocked(RuntimeError):
    pass


async def send_group_msg(bot, **data):
    group_id = int(data["group_id"])
    try:
        groups = await asyncio.wait_for(bot.get_group_list(), timeout=5)
        if not isinstance(groups, list):
            raise ValueError("invalid group list")
        current = {int(row["group_id"]) for row in groups}
    except Exception as exc:
        logger.warning("群消息跳过: group_id={} reason=membership_unavailable type={}",
                       group_id, type(exc).__name__)
        raise GroupDeliveryBlocked("membership unavailable") from exc
    if group_id not in current:
        logger.warning("群消息跳过: group_id={} reason=not_in_group", group_id)
        raise GroupDeliveryBlocked("not in group")
    return await bot.send_group_msg(**data)
