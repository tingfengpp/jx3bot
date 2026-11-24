import asyncio
import unittest
from unittest.mock import AsyncMock
from src.infra.group_delivery import send_group_msg, GroupDeliveryBlocked


class GroupDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_member_can_send(self):
        bot = AsyncMock()
        bot.get_group_list.return_value = [{"group_id": 10}]
        bot.send_group_msg.return_value = {"message_id": 1}
        self.assertEqual(await send_group_msg(bot, group_id=10, message="x"), {"message_id": 1})

    async def test_removed_between_sends(self):
        bot = AsyncMock()
        bot.get_group_list.side_effect = [[{"group_id": 10}], []]
        await send_group_msg(bot, group_id=10, message="x")
        with self.assertRaises(GroupDeliveryBlocked):
            await send_group_msg(bot, group_id=10, message="x")
        self.assertEqual(bot.send_group_msg.call_count, 1)

    async def test_failure_and_malformed_fail_closed(self):
        for response in [None, [{"wrong": 1}], asyncio.TimeoutError()]:
            bot = AsyncMock()
            if isinstance(response, Exception):
                bot.get_group_list.side_effect = response
            else:
                bot.get_group_list.return_value = response
            with self.assertRaises(GroupDeliveryBlocked):
                await send_group_msg(bot, group_id=10, message="x")
            bot.send_group_msg.assert_not_called()
