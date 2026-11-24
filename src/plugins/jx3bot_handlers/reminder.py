from __future__ import annotations
from src.infra.group_delivery import send_group_msg

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any

from nonebot import get_driver, on_regex, require
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot.rule import Rule

from src.infra.mongo import get_db as _get_db
from src.storage.mongo_repos.reminder_repo import ReminderRepo

CANCEL_TIMEOUT_SECONDS = 45
JOB_ID_PREFIX = "group_reminder:"

CANCEL_SESSIONS: dict[str, dict[str, Any]] = {}


def _repo() -> ReminderRepo:
    return ReminderRepo(db=_get_db())

scheduler = require("nonebot_plugin_apscheduler").scheduler
_driver = get_driver()
_bot: Bot | None = None


@_driver.on_bot_connect
async def _on_bot_connect(bot: Bot) -> None:
    global _bot
    _bot = bot


@_driver.on_startup
async def _restore_reminder_jobs() -> None:
    reminders_by_group = await load_reminders()
    now = time.time()
    restored = 0

    for reminders in reminders_by_group.values():
        for reminder in reminders:
            if reminder.get("status") != "pending":
                continue

            remind_at_str = reminder.get("remind_at", "")
            remind_at_dt = _parse_remind_at(remind_at_str)
            if remind_at_dt is None:
                logger.warning(f"提醒时间格式非法，跳过恢复: reminder_id={reminder.get('id')}")
                continue

            run_date = remind_at_dt if remind_at_dt.timestamp() > now else datetime.now()
            _schedule_reminder_job(reminder["reminder_id"], run_date)
            restored += 1

    if restored:
        logger.info(f"提醒任务恢复完成，共恢复 {restored} 条")


def _parse_remind_at(remind_at: str) -> datetime | None:
    try:
        normalized = remind_at.strip()
        if len(normalized) == 12:
            normalized = f"{normalized}00"
        return datetime.strptime(normalized, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _job_id(reminder_id: str) -> str:
    return f"{JOB_ID_PREFIX}{reminder_id}"


def _session_key(group_id: int, user_id: int) -> str:
    return f"{group_id}:{user_id}"


def _render_time(remind_at: str) -> str:
    parsed = _parse_remind_at(remind_at)
    if parsed is None:
        return remind_at
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


async def load_reminders() -> dict[str, list[dict[str, Any]]]:
    return await _repo().load_all_pending()


async def create_reminder(
    *,
    group_id: int,
    user_id: int,
    remind_at: str,
    message: str,
    mention_type: str,
) -> dict[str, Any]:
    reminder = {
        "reminder_id": uuid.uuid4().hex,
        "group_id": str(group_id),
        "creator_user_id": str(user_id),
        "mention_type": mention_type,
        "message": message,
        "remind_at": remind_at,
        "created_at": int(time.time()),
        "status": "pending",
    }
    await _repo().insert(reminder)
    return reminder


async def get_group_pending_reminders(group_id: int) -> list[dict[str, Any]]:
    all_reminders = await _repo().load_by_group(group_id)
    pending = [item for item in all_reminders if item.get("status") == "pending"]
    pending.sort(key=lambda x: x.get("remind_at", ""))
    return pending


async def get_user_pending_reminders(group_id: int, user_id: int) -> list[dict[str, Any]]:
    pending = await get_group_pending_reminders(group_id)
    return [item for item in pending if item.get("creator_user_id") == str(user_id)]


async def mark_reminder_done(reminder_id: str) -> bool:
    return await _repo().update_status(reminder_id, "done", "done_at")


async def cancel_reminder(reminder_id: str, group_id: int, user_id: int) -> dict[str, Any] | None:
    return await _repo().cancel(reminder_id, group_id, user_id)


async def find_pending_reminder(reminder_id: str) -> dict[str, Any] | None:
    doc = await _repo().find_by_id(reminder_id)
    if doc and doc.get("status") == "pending":
        return doc
    return None


def _schedule_reminder_job(reminder_id: str, run_date: datetime) -> None:
    scheduler.add_job(
        _send_reminder,
        "date",
        run_date=run_date,
        args=[reminder_id],
        id=_job_id(reminder_id),
        replace_existing=True,
        misfire_grace_time=300,
    )


def _remove_reminder_job(reminder_id: str) -> None:
    try:
        scheduler.remove_job(_job_id(reminder_id))
    except Exception:
        pass


async def _send_reminder(reminder_id: str) -> None:
    reminder = await find_pending_reminder(reminder_id)
    if reminder is None:
        return

    if _bot is None:
        logger.warning(f"机器人未就绪，提醒延后1分钟重试: reminder_id={reminder_id}")
        _schedule_reminder_job(reminder_id, datetime.fromtimestamp(time.time() + 60))
        return

    group_id = int(reminder["group_id"])
    user_id = int(reminder["creator_user_id"])
    message_text = reminder.get("message", "")
    mention_type = reminder.get("mention_type", "user")

    if mention_type == "all":
        message = MessageSegment.at("all") + Message(f" {message_text}")
    else:
        message = MessageSegment.at(user_id) + Message(f" {message_text}")

    try:
        await send_group_msg(_bot, group_id=group_id, message=message)
        await mark_reminder_done(reminder_id)
        logger.info(
            "提醒发送成功: "
            f"group_id={group_id}, user_id={user_id}, reminder_id={reminder_id}, mention_type={mention_type}"
        )
    except Exception as exc:
        logger.error(
            "提醒发送失败，1分钟后重试: "
            f"group_id={group_id}, user_id={user_id}, reminder_id={reminder_id}, error={exc}"
        )
        _schedule_reminder_job(reminder_id, datetime.fromtimestamp(time.time() + 60))


def _build_list_message(reminders: list[dict[str, Any]]) -> str:
    lines = [f"当前群共有 {len(reminders)} 条待执行提醒："]
    for i, reminder in enumerate(reminders, 1):
        mention_label = "@all" if reminder.get("mention_type") == "all" else f"@{reminder.get('creator_user_id')}"
        lines.append(
            f"{i}. [{_render_time(reminder.get('remind_at', ''))}] {mention_label} {reminder.get('message', '')}"
        )
    return "\n".join(lines)


async def _in_cancel_session(event: Event) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False

    key = _session_key(event.group_id, event.user_id)
    session = CANCEL_SESSIONS.get(key)
    if session is None:
        return False

    if time.time() > session["expires_at"]:
        CANCEL_SESSIONS.pop(key, None)
        return False

    return event.get_plaintext().strip().isdigit()


def register(
    reminder_matcher: Any,
    reminder_all_matcher: Any,
    reminder_list_matcher: Any,
    cancel_reminder_matcher: Any,
) -> None:
    # 使用捕获分组，确保 RegexGroup() 能取到用户输入的序号
    cancel_choice_matcher = on_regex(r"^(\d+)$", rule=Rule(_in_cancel_session), priority=4, block=True)
    logger.warning("提醒模块已注册: reminder/reminder_all/reminder_list/cancel_reminder")

    async def _handle_create(
        event: Event,
        mention_type: str,
        matcher: Matcher,
        matched: tuple[str, ...],
    ) -> None:
        try:
            plain_text = event.get_plaintext().strip()
            logger.warning(
                f"收到提醒命令: event_type={event.get_type()}, mention_type={mention_type}, "
                f"matched={matched}, plain_text={plain_text!r}"
            )

            if not isinstance(event, GroupMessageEvent):
                logger.warning("提醒命令被拒绝: 非群消息")
                await matcher.finish("该命令仅支持群聊使用")
                return

            if len(matched) < 2:
                logger.error(f"提醒命令匹配参数异常: matched={matched}")
                await matcher.finish(MessageSegment.at(event.user_id) + Message(" 提醒参数解析失败，请重试"))
                return

            remind_at = f"{matched[0].strip()}00"
            message_text = matched[1].strip()

            remind_at_dt = _parse_remind_at(remind_at)
            if remind_at_dt is None:
                logger.warning(f"提醒命令时间格式错误: remind_at={remind_at!r}")
                await matcher.finish(MessageSegment.at(event.user_id) + Message(" 时间格式错误，请使用 YYYYMMDDHHMM"))
                return

            if remind_at_dt.timestamp() <= time.time():
                logger.warning(f"提醒命令时间过期: remind_at={remind_at!r}")
                await matcher.finish(MessageSegment.at(event.user_id) + Message(" 提醒时间必须晚于当前时间"))
                return

            reminder = await create_reminder(
                group_id=event.group_id,
                user_id=event.user_id,
                remind_at=remind_at,
                message=message_text,
                mention_type=mention_type,
            )
            _schedule_reminder_job(reminder["reminder_id"], remind_at_dt)

            logger.info(
                "创建提醒成功: "
                f"group_id={event.group_id}, user_id={event.user_id}, reminder_id={reminder['reminder_id']}, "
                f"remind_at={remind_at}, mention_type={mention_type}"
            )
            await matcher.finish(
                MessageSegment.at(event.user_id)
                + Message(f" 提醒已创建：{_render_time(remind_at)} {message_text}")
            )
        except FinishedException:
            raise
        except Exception as exc:
            logger.exception(f"处理提醒命令异常: mention_type={mention_type}, matched={matched}, error={exc}")
            if isinstance(event, GroupMessageEvent):
                await matcher.finish(MessageSegment.at(event.user_id) + Message(" 提醒处理失败，请稍后重试"))
            else:
                await matcher.finish("提醒处理失败，请稍后重试")

    @reminder_matcher.handle()
    async def _handle_reminder(
        event: Event,
        matcher: Matcher,
        matched: tuple = RegexGroup(),
    ) -> None:
        logger.warning(f"命中提醒 matcher: matched={matched}")
        await _handle_create(event, "user", matcher, matched)

    @reminder_all_matcher.handle()
    async def _handle_reminder_all(
        event: Event,
        matcher: Matcher,
        matched: tuple = RegexGroup(),
    ) -> None:
        logger.warning(f"命中提醒所有人 matcher: matched={matched}")
        await _handle_create(event, "all", matcher, matched)

    @reminder_list_matcher.handle()
    async def _handle_reminder_list(event: Event, matcher: Matcher) -> None:
        if not isinstance(event, GroupMessageEvent):
            await matcher.finish("该命令仅支持群聊使用")
            return

        reminders = await get_group_pending_reminders(event.group_id)
        if not reminders:
            await matcher.finish("当前群没有待执行提醒")
            return

        await matcher.finish(_build_list_message(reminders))

    @cancel_reminder_matcher.handle()
    async def _handle_cancel_reminder(event: Event, matcher: Matcher) -> None:
        if not isinstance(event, GroupMessageEvent):
            await matcher.finish("该命令仅支持群聊使用")
            return

        reminders = await get_user_pending_reminders(event.group_id, event.user_id)
        if not reminders:
            await matcher.finish(MessageSegment.at(event.user_id) + Message(" 您当前没有可取消的提醒"))
            return

        key = _session_key(event.group_id, event.user_id)
        CANCEL_SESSIONS[key] = {
            "group_id": event.group_id,
            "user_id": event.user_id,
            "reminder_ids": [item["id"] for item in reminders],
            "expires_at": time.time() + CANCEL_TIMEOUT_SECONDS,
        }

        lines = [f"{len(reminders)}条提醒可取消，请在{CANCEL_TIMEOUT_SECONDS}秒内回复序号："]
        for i, reminder in enumerate(reminders, 1):
            lines.append(f"{i}. [{_render_time(reminder['remind_at'])}] {reminder['message']}")
        await matcher.finish(MessageSegment.at(event.user_id) + Message("\n" + "\n".join(lines)))

    @cancel_choice_matcher.handle()
    async def _handle_cancel_choice(
        event: Event,
        matcher: Matcher,
        matched: tuple = RegexGroup(),
    ) -> None:
        if not isinstance(event, GroupMessageEvent):
            return

        key = _session_key(event.group_id, event.user_id)
        session = CANCEL_SESSIONS.get(key)
        if session is None:
            return

        if time.time() > session["expires_at"]:
            CANCEL_SESSIONS.pop(key, None)
            await matcher.finish(MessageSegment.at(event.user_id) + Message(" 取消提醒会话已超时，请重新输入“取消提醒”"))
            return

        if not matched:
            await matcher.finish(MessageSegment.at(event.user_id) + Message(" 序号无效，请重新输入"))
            return

        index = int(matched[0].strip())
        reminder_ids = session["reminder_ids"]
        if index <= 0 or index > len(reminder_ids):
            await matcher.finish(MessageSegment.at(event.user_id) + Message(" 序号无效，请重新输入"))
            return

        reminder_id = reminder_ids[index - 1]
        canceled = await cancel_reminder(reminder_id, event.group_id, event.user_id)
        if canceled is None:
            CANCEL_SESSIONS.pop(key, None)
            await matcher.finish(MessageSegment.at(event.user_id) + Message(" 取消失败，提醒可能已执行或已被取消"))
            return

        _remove_reminder_job(reminder_id)
        CANCEL_SESSIONS.pop(key, None)

        logger.info(
            "取消提醒成功: "
            f"group_id={event.group_id}, user_id={event.user_id}, reminder_id={reminder_id}"
        )
        await matcher.finish(
            MessageSegment.at(event.user_id)
            + Message(f" 已取消提醒：{_render_time(canceled['remind_at'])} {canceled['message']}")
        )
