from src.infra.group_delivery import send_group_msg
import asyncio
import json
import re
import time
from datetime import datetime
from functools import wraps

import config as cfg
import httpx
from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot_plugin_apscheduler import scheduler

from src.services.jx3.jjc_peak_score_ranking import JjcPeakScoreRankingService
from src.services.jx3.singletons import jjc_ranking_service
from src.services.jx3.singletons import group_config_repo
from src.services.jx3.singletons import env
from src.renderers.jx3.image import render_template_image
from src.renderers.jx3.jjc_ranking import render_combined_ranking_image

from .notify import get_NapCat_data, send_email_via_163
from .storage import CacheManager, load_id_set, save_id_set
from src.infra.jx3api_compat import normalize_jx3api_response

try:
    from config import (
        NEWS_API_URL,
        NEWS_records_time,
        SKILL_records_URL,
        STATUS_check_API,
        STATUS_check_time,
        calendar_URL,
        calendar_time,
        jx3box_URL,
    )
except Exception:
    NEWS_records_time = 30
    STATUS_check_time = 3
    calendar_time = 9
    STATUS_check_API = ""
    NEWS_API_URL = ""
    SKILL_records_URL = ""
    calendar_URL = ""
    jx3box_URL = ""


BOT_INITIALIZED = False
previous_status = {}
previous_news_ids = set()
previous_records_ids = set()
previous_codes_ids = set()
last_status_ok = True
jjc_peak_score_ranking_service = JjcPeakScoreRankingService()


def set_bot_initialized(value: bool) -> None:
    global BOT_INITIALIZED
    BOT_INITIALIZED = value


async def notify_jjc_ranking_failure(bot, detail: str) -> None:
    message = f"竞技排名定时任务统计失败：{detail}"
    if bot is None:
        bot = next(iter(get_driver().bots.values()), None)
    if bot is None:
        logger.warning("status_monitor QQ 未连接，无法私聊管理员: {}", message)
        return
    for admin_qq in getattr(cfg, "ADMIN_QQ", []):
        try:
            await bot.send_private_msg(user_id=int(admin_qq), message=message)
        except Exception as exc:
            logger.warning(
                "status_monitor 通知竞技排名统计失败给管理员{}时出错: {}",
                admin_qq,
                exc,
            )


def log_startup() -> None:
    logger.info("status_monitor 开服监控已启动，API: {} 延时{}分钟", STATUS_check_API, STATUS_check_time)
    logger.info("status_monitor 新闻监控已启动，API: {} 延时{}分钟", NEWS_API_URL, NEWS_records_time)
    logger.info("status_monitor 技改监控已启动，API: {} 延时{}分钟", SKILL_records_URL, NEWS_records_time)
    logger.info("status_monitor 日常监控已启动，每天{}点推送", calendar_time)


def prevent_duplicate_runs(timeout_seconds=60):
    def decorator(func):
        last_run = 0

        @wraps(func)
        async def wrapper(*args, **kwargs):
            nonlocal last_run
            current_time = time.time()

            if current_time - last_run > timeout_seconds:
                last_run = current_time
                return await func(*args, **kwargs)
            else:
                logger.debug("status_monitor skip duplicate run: {}", func.__name__)

        return wrapper

    return decorator


def format_time(timestamp):
    t = time.localtime(timestamp)
    month = t.tm_mon
    day = t.tm_mday
    hour = t.tm_hour
    minute = t.tm_min
    second = t.tm_sec
    return f"{month}月{day}日 {hour:02d}:{minute:02d}:{second:02d}"


def extract_version(json_data):
    try:
        if not json_data:
            return "未知"

        data = json_data if isinstance(json_data, dict) else json.loads(json_data)

        if "data" in data and len(data["data"]) > 0:
            first_item = data["data"][0]
            title = first_item.get("title", "")

            version_match = re.search(r"(\d+\.\d+\.\d+\.\d+)", title)
            if version_match:
                return version_match.group(1)

        return "未知"
    except Exception as e:
        logger.warning("status_monitor 提取版本号失败: {}", e)
        return "未知"


def format_gte_message(gte_data):
    if not gte_data or "data" not in gte_data:
        return "获取每日GTE数据失败"

    data = gte_data["data"]

    date_str = data.get("date", "")
    week_str = data.get("week", "")

    if date_str:
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            date_str = f"{date_obj.year}年{date_obj.month}月{date_obj.day}日"
        except Exception:
            pass

    message = [f"当前时间：{date_str} 星期{week_str}"]
    message.append(f"秘境大战：{data.get('war', '无')}")
    message.append(f"战场任务：{data.get('battle', '无')}")
    message.append(f"宗门事件：{data.get('school', '无')}")
    message.append(f"驰援任务：{data.get('rescue', '无')}")
    message.append(f"阵营任务：{data.get('orecar', '无')}")
    message.append("帮会跑商：阴山商路(10:00)")

    luck_pets = ";".join(data.get("luck", []))
    message.append(f"福源宠物：{luck_pets}")

    if "draw" in data:
        message.append(f"美人图：{data.get('draw', '无')}")

    message.append("")
    message.append("家园声望·加倍道具")
    card_items = ";".join(data.get("card", []))
    message.append(card_items)

    if "team" in data and len(data["team"]) >= 3:
        message.append("武林通鉴·公共任务")
        message.append(data["team"][0])
        message.append("武林通鉴·秘境任务")
        message.append(data["team"][1])
        message.append("武林通鉴·团队秘境")
        message.append(data["team"][2])

    return "\n".join(message)


async def get_gte_data(url, server):
    try:
        url = f"{url}?server={server}"
        logger.debug("status_monitor 请求日常: {}", url)
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(url)
            if response.status_code != 200:
                logger.warning("status_monitor 日常API返回错误: {}", response.status_code)
                return None
            return normalize_jx3api_response(url, response.json())
    except Exception as e:
        logger.warning("status_monitor 请求日常API失败: {}", e)
        return None


async def get_server_status():
    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(STATUS_check_API)
            if response.status_code != 200:
                logger.warning("status_monitor 开服监测API返回错误: {}", response.status_code)
                return None
            return normalize_jx3api_response(STATUS_check_API, response.json())
    except Exception as e:
        logger.warning("status_monitor 开服监测请求失败: {}", e)
        return None


async def get_server_banben():
    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get("https://www.jx3api.com/news/announce")
            if response.status_code != 200:
                logger.warning("status_monitor 公告API返回错误: {}", response.status_code)
                return None
            return normalize_jx3api_response("https://www.jx3api.com/news/announce", response.json())
    except Exception as e:
        logger.warning("status_monitor 公告请求失败: {}", e)
        return None


async def get_news_data():
    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(NEWS_API_URL)
            if response.status_code != 200:
                logger.warning("status_monitor 新闻API返回错误: {}", response.status_code)
                return None
            return normalize_jx3api_response(NEWS_API_URL, response.json())
    except Exception as e:
        logger.warning("status_monitor 请求新闻API失败: {}", e)
        return None


async def get_records_data():
    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(SKILL_records_URL)
            if response.status_code != 200:
                logger.warning("status_monitor 技改API返回错误: {}", response.status_code)
                return None
            return normalize_jx3api_response(SKILL_records_URL, response.json())
    except Exception as e:
        logger.warning("status_monitor 请求技改API失败: {}", e)
        return None


async def get_jx3box_data():
    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(jx3box_URL)
            if response.status_code != 200:
                logger.warning("status_monitor 福利API返回错误: {}", response.status_code)
                return None
            return response.json()
    except Exception as e:
        logger.warning("status_monitor 请求福利API失败: {}", e)
        return None


@scheduler.scheduled_job("interval", seconds=60)
async def mail_records():
    global last_status_ok

    data = await get_NapCat_data()

    if data is None:
        current_status_ok = False
        status_info = "无法获取服务器状态，API请求失败"
    else:
        current_status_ok = data.get("status", "").lower() == "ok"
        status_info = json.dumps(data, ensure_ascii=False)

    if last_status_ok and not current_status_ok:
        logger.warning("status_monitor 检测到状态异常，发送邮件通知")
        email_content = f"服务器状态异常！\n\n当前状态信息：\n{status_info}"
        send_result = send_email_via_163(email_content)
        if send_result:
            logger.info("status_monitor 邮件发送成功")
        else:
            logger.warning("status_monitor 邮件发送失败")

    elif not last_status_ok and current_status_ok:
        recovery_content = "服务器状态已恢复正常！"
        send_email_via_163(recovery_content)

    last_status_ok = current_status_ok


@scheduler.scheduled_job("interval", minutes=NEWS_records_time)
async def check_records():
    if BOT_INITIALIZED:
        global previous_records_ids
        try:
            if not previous_records_ids:
                previous_records_ids = await load_id_set("records_ids")
                if not previous_records_ids:
                    logger.info("status_monitor 没有发现技改缓存或缓存为空")
                else:
                    logger.info("status_monitor 从缓存加载了{}条技改ID", len(previous_records_ids))

            data = await get_records_data()
            if not data or "data" not in data:
                logger.warning("status_monitor 获取技改数据失败")
                return

            current_records = data["data"]
            current_records_ids = {str(record.get("id", "")) for record in current_records}

            if not previous_records_ids:
                previous_records_ids = current_records_ids
                await save_id_set(previous_records_ids, "records_ids")
                logger.info("status_monitor 首次运行，已记录{}条技改ID", len(current_records_ids))
                return

            new_records_ids = current_records_ids - previous_records_ids
            if new_records_ids:
                logger.info("status_monitor 检测到{}条新增技改", len(new_records_ids))

                new_records = [
                    record
                    for record in current_records
                    if str(record.get("id", "")) in new_records_ids
                ]

                if get_driver().bots:
                    bot = list(get_driver().bots.values())[0]
                    groups_config = await group_config_repo.load()

                    for gid, config in groups_config.items():
                        try:
                            if not isinstance(config, dict) or config.get("技改推送", False) is False:
                                continue

                            for record in new_records:
                                record_class = "官方公告"
                                title = record.get("title", "无标题")
                                url = record.get("url", "")
                                date = record.get("time", "")

                                message = (
                                    f"{record_class}\n"
                                    f"标题：{title}\n"
                                    f"链接：{url}\n"
                                    f"日期：{date}"
                                )

                                await send_group_msg(bot, group_id=int(gid), message=message)
                                logger.info("status_monitor 向群{}推送技改: {}", gid, title)
                        except Exception as e:
                            logger.warning("status_monitor 向群{}推送技改失败: {}", gid, e)

            previous_records_ids = current_records_ids
            await save_id_set(previous_records_ids, "records_ids")
        except Exception as e:
            logger.warning("status_monitor 检查技改出错: {}", e)


@scheduler.scheduled_job("interval", minutes=NEWS_records_time)
async def check_news():
    if BOT_INITIALIZED:
        global previous_news_ids
        try:
            if not previous_news_ids:
                previous_news_ids = await load_id_set("news_ids")
                if not previous_news_ids:
                    logger.info("status_monitor 没有发现新闻缓存或缓存为空")
                else:
                    logger.info("status_monitor 从缓存加载了{}条新闻ID", len(previous_news_ids))

            data = await get_news_data()
            if not data or "data" not in data:
                logger.warning("status_monitor 获取新闻数据失败")
                return

            current_news = data["data"]
            current_news = [
                item
                for item in current_news
                if "武学" not in item.get("title", "")
                and "版本" not in item.get("title", "")
                and "维护" not in item.get("title", "")
            ]
            current_news_ids = {news["id"] for news in current_news}

            if not previous_news_ids:
                previous_news_ids = current_news_ids
                await save_id_set(previous_news_ids, "news_ids")
                logger.info("status_monitor 首次运行，已记录{}条新闻ID", len(current_news_ids))
                return

            new_news_ids = current_news_ids - previous_news_ids
            if new_news_ids:
                logger.info("status_monitor 检测到{}条新增新闻", len(new_news_ids))
                new_news = [news for news in current_news if news["id"] in new_news_ids]

                if get_driver().bots:
                    bot = list(get_driver().bots.values())[0]
                    groups_config = await group_config_repo.load()

                    for gid, config in groups_config.items():
                        try:
                            if not isinstance(config, dict) or config.get("新闻推送", False) is False:
                                continue

                            for news in new_news:
                                news_class = news.get("type", "公告")
                                title = news.get("title", "无标题")
                                url = news.get("url", "")
                                date = news.get("date", "")

                                message = (
                                    f"{news_class}\n"
                                    f"标题：{title}\n"
                                    f"链接：{url}\n"
                                    f"日期：{date}"
                                )

                                await send_group_msg(bot, group_id=int(gid), message=message)
                                logger.info("status_monitor 向群{}推送新闻: {}", gid, title)
                        except Exception as e:
                            logger.warning("status_monitor 向群{}推送新闻失败: {}", gid, e)

            previous_news_ids = current_news_ids
            await save_id_set(previous_news_ids, "news_ids")
        except Exception as e:
            logger.warning("status_monitor 检查新闻出错: {}", e)


@scheduler.scheduled_job("interval", minutes=200)
async def check_event_codes():
    if BOT_INITIALIZED:
        global previous_codes_ids
        current_codes_ids = set()

        try:
            if not previous_codes_ids:
                previous_codes_ids = await load_id_set("event_codes_ids")
                if not previous_codes_ids:
                    logger.info("status_monitor 没有发现活动码缓存或缓存为空")
                else:
                    logger.info("status_monitor 从缓存加载了{}条活动码ID", len(previous_codes_ids))

            response = await get_jx3box_data()
            if not response or "data" not in response or "list" not in response["data"]:
                logger.warning("status_monitor 获取活动码数据失败")
                return

            current_codes = response["data"]["list"]
            current_codes_ids = {code["ID"] for code in current_codes}

            if not previous_codes_ids:
                previous_codes_ids = current_codes_ids
                await save_id_set(previous_codes_ids, "event_codes_ids")
                logger.info("status_monitor 首次运行，已记录{}条活动码ID", len(current_codes_ids))
                return

            new_codes_ids = current_codes_ids - previous_codes_ids
            if new_codes_ids:
                logger.info("status_monitor 检测到{}条新增活动码", len(new_codes_ids))
                new_codes = [code for code in current_codes if code["ID"] in new_codes_ids]

                if get_driver().bots:
                    groups_config = await group_config_repo.load()
                    bot = list(get_driver().bots.values())[0]

                    for code in new_codes:
                        if code["status"] == 0:
                            continue

                        title = code.get("title", "未知活动")
                        desc = code.get("desc", "无描述")
                        created_at = code.get("created_at", "").replace("T", " ").replace("Z", "")
                        message = (
                            f"检测到新活动码:\n奖励: {desc}\n兑换码:{title} \n创建时间: {created_at}\n输入 奖励 查看过往兑换码"
                        )

                        for gid, config in groups_config.items():
                            try:
                                if not isinstance(config, dict) or config.get("福利推送", False) is False:
                                    continue

                                await send_group_msg(bot, group_id=int(gid), message=message)
                                logger.info("status_monitor 向群{}推送活动码: {}", gid, title)
                            except Exception as e:
                                logger.warning("status_monitor 向群{}推送活动码失败: {}", gid, e)

            if current_codes_ids:
                previous_codes_ids = current_codes_ids
                await save_id_set(previous_codes_ids, "event_codes_ids")
        except Exception as e:
            logger.warning("status_monitor 检查活动码出错: {}", e)


@scheduler.scheduled_job("interval", minutes=STATUS_check_time)
@prevent_duplicate_runs(timeout_seconds=3600)
async def check_status():
    global previous_status
    try:
        if not previous_status:
            previous_status = await CacheManager.load_cache("server_status", {})
            if not previous_status:
                logger.info("status_monitor 没有发现服务器状态缓存或缓存为空")
                previous_status = {}
            else:
                logger.info("status_monitor 从缓存加载了{}条服务器状态数据", len(previous_status))

        data = await get_server_status()
        if not data:
            logger.warning("status_monitor 获取服务器状态失败")
            return

        current_details = {s["server"]: s for s in data.get("data", [])}
        current = {server: info.get("legacyStatus", info["status"]) for server, info in current_details.items()}
        current_time = int(time.time())

        status_history = await CacheManager.load_cache("status_history", {})

        if not previous_status:
            for server, status in current.items():
                if server not in status_history:
                    status_history[server] = {"last_maintenance": None, "last_open": None}

                api_time = current_details[server].get("time", current_time)
                if status == 0:
                    status_history[server]["last_maintenance"] = api_time
                else:
                    status_history[server]["last_open"] = api_time

            logger.info("status_monitor 首次运行，已记录{}条服务器数据", len(current))
            previous_status = current

            await CacheManager.save_cache(previous_status, "server_status")
            await CacheManager.save_cache(status_history, "status_history")
            return

        changed_servers = []
        for server, status in current.items():
            if server in previous_status and previous_status[server] != status:
                changed_servers.append(server)

                if server not in status_history:
                    status_history[server] = {"last_maintenance": None, "last_open": None}

                api_time = current_details[server].get("time", current_time)
                if status == 1:
                    status_history[server]["last_open"] = api_time
                else:
                    status_history[server]["last_maintenance"] = api_time

        if changed_servers:
            logger.info("status_monitor 检测到{}个服务器状态变化", len(changed_servers))
            await asyncio.sleep(5)

            if get_driver().bots:
                bot = list(get_driver().bots.values())[0]
                groups_config = await group_config_repo.load()

                for gid, config in groups_config.items():
                    try:
                        if not isinstance(config, dict) or config.get("开服推送", True) is False:
                            continue

                        bound_server = config.get("servers", "")
                        if not bound_server or bound_server not in changed_servers:
                            continue

                        try:
                            banben = await get_server_banben()
                            banben = extract_version(banben)
                        except Exception as e:
                            logger.warning("status_monitor 获取版本号失败: {}", e)
                            banben = "未知"

                        server_info = current_details[bound_server]
                        zone = server_info["zone"]
                        if zone.endswith("区"):
                            zone = zone[:-1] + "大区"
                        status = server_info.get("legacyStatus", server_info["status"])

                        server_history = status_history.get(
                            bound_server, {"last_maintenance": None, "last_open": None}
                        )
                        last_maintenance_time = server_history.get("last_maintenance")
                        last_open_time = server_history.get("last_open")

                        maintenance_time_str = (
                            "无记录" if last_maintenance_time is None else format_time(last_maintenance_time)
                        )
                        open_time_str = "无记录" if last_open_time is None else format_time(last_open_time)

                        if status == 1:
                            message = (
                                f"{zone}：{bound_server}「 已开服 」\n"
                                f"开服时间：{open_time_str}\n"
                                f"维护时间：{maintenance_time_str}"
                            )
                        else:
                            message = (
                                f"{zone}：{bound_server}「 维护中 」\n"
                                f"维护时间：{maintenance_time_str}\n"
                                f"上次开服：{open_time_str}"
                            )

                        if banben != "未知":
                            message += f"\n最新版本：{banben}"

                        await send_group_msg(bot, group_id=int(gid), message=message)
                        logger.info("status_monitor 向群{}推送服务器状态变化: {}", gid, bound_server)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning("status_monitor 向群{}推送服务器状态失败: {}", gid, e)

        previous_status = current
        await CacheManager.save_cache(previous_status, "server_status")
        await CacheManager.save_cache(status_history, "status_history")
    except Exception as e:
        logger.warning("status_monitor 开服检查出错: {}", e)


@scheduler.scheduled_job("cron", hour=9, minute=0)
async def push_daily_gte():
    if BOT_INITIALIZED:
        try:
            if not get_driver().bots:
                return
            bot = list(get_driver().bots.values())[0]
            groups_config = await group_config_repo.load()

            for gid, config in groups_config.items():
                try:
                    if not isinstance(config, dict) or config.get("日常推送", False) is False:
                        continue

                    server = config.get("servers", "")
                    if not server:
                        continue

                    gte_data = await get_gte_data(url=calendar_URL, server=server)
                    if not gte_data:
                        continue
                    message = format_gte_message(gte_data)
                    await send_group_msg(bot, group_id=int(gid), message=message)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning("status_monitor 向群{}推送日常失败: {}", gid, e)
        except Exception as e:
            logger.warning("status_monitor 推送日常出错: {}", e)


@scheduler.scheduled_job("cron", hour=9, minute=0, max_instances=1)
async def push_daily_jjc_peak_score_ranking():
    try:
        result = await jjc_peak_score_ranking_service.run_latest_two_snapshots()
        logger.info(
            "status_monitor JJC 14天最高分统计完成: anchors={} generated={} skipped={} failed={}".format(
                result.get("anchors"),
                result.get("generated"),
                result.get("skipped"),
                result.get("failed"),
            )
        )
    except Exception as e:
        logger.warning("status_monitor JJC 14天最高分统计出错: {}", e)


@scheduler.scheduled_job("cron", hour="3,21", minute=0, max_instances=2)
async def push_daily_jjc_ranking():
    bot = None
    try:
        ranking_result = await jjc_ranking_service.query_jjc_ranking()
        if not ranking_result:
            logger.warning("status_monitor 获取竞技场排行榜数据失败：返回为空")
            await notify_jjc_ranking_failure(bot, "获取竞技场排行榜数据失败：返回为空")
            return

        if ranking_result.get("error"):
            detail = ranking_result.get("message", "未知错误")
            logger.warning(
                "status_monitor 获取竞技场排行榜数据失败：{}",
                detail,
            )
            await notify_jjc_ranking_failure(bot, f"获取竞技场排行榜数据失败：{detail}")
            return

        if ranking_result.get("code") != 0:
            detail = ranking_result.get("code")
            logger.warning(
                "status_monitor 获取竞技场排行榜数据失败：API返回错误码 {}",
                detail,
            )
            await notify_jjc_ranking_failure(bot, f"获取竞技场排行榜数据失败：API返回错误码 {detail}")
            return

        default_week = ranking_result.get("defaultWeek")
        cache_time = ranking_result.get("cache_time")
        week_info = (
            jjc_ranking_service.calculate_season_week_info(default_week, cache_time)
            if default_week
            else "第12周"
        )

        ranking_data = await jjc_ranking_service.get_ranking_kungfu_data(ranking_data=ranking_result)
        if not ranking_data:
            logger.warning("status_monitor 获取心法统计数据失败：返回为空")
            await notify_jjc_ranking_failure(bot, "获取心法统计数据失败：返回为空")
            return

        if ranking_data.get("error"):
            detail = ranking_data.get("message", "未知错误")
            logger.warning(
                "status_monitor 获取心法统计数据失败：{}",
                detail,
            )
            await notify_jjc_ranking_failure(bot, f"获取心法统计数据失败：{detail}")
            return

        stats = ranking_data.get("kungfu_statistics", {})
        if not stats:
            logger.warning("status_monitor 心法统计数据为空")
            await notify_jjc_ranking_failure(bot, "心法统计数据为空")
            return

        # 统计落库不依赖 QQ、群订阅或图片渲染。
        save_task = jjc_ranking_service.save_ranking_stats(
            ranking_result=ranking_result,
            stats=stats,
            week_info=week_info,
            **({"payload": ranking_data} if any(key in ranking_data for key in ("kungfu_cache_scope", "refresh_degraded", "_sync_request", "equipment_view")) else {}),
        )
        if ranking_data.get("equipment_view") is not None and save_task is None:
            raise RuntimeError("equipment_publication_unavailable")
        if save_task is not None:
            await save_task

        # 采集可能耗时较长，发送前重新获取连接状态。
        bot = next(iter(get_driver().bots.values()), None)
        if bot is None:
            logger.info("status_monitor 竞技排名统计流程完成，QQ 未连接，跳过群推送")
            return

        groups_config = await group_config_repo.load()
        target_groups = [
            gid
            for gid, config in groups_config.items()
            if isinstance(config, dict) and config.get("竞技排名推送", False)
        ]
        if not target_groups:
            logger.info("status_monitor 竞技排名统计流程完成，没有开启竞技排名推送的群组")
            return

        payload = await render_combined_ranking_image(
            env=env,
            render_template_image=render_template_image,
            current_season=cfg.CURRENT_SEASON,
            stats=stats,
            week_info=week_info,
            show_legendary=False,
        )
        if not payload:
            logger.warning("status_monitor 渲染竞技场统计图失败")
            await notify_jjc_ranking_failure(bot, "渲染竞技场统计图失败")
            return

        summary_text = (
            f"⏰ 每日03:00/21:00竞技排名推送（{week_info}）\n"
            f"统计范围：{payload['scope_desc']}\n"
            f"统计完成！共处理 {payload['total_valid_data']} 条有效数据（{payload['processed_label']}）"
        )
        missing_kungfu_lines = ranking_data.get("missing_kungfu_lines", [])

        for gid in target_groups:
            try:
                await send_group_msg(bot,
                    group_id=int(gid), message=MessageSegment.image(payload["image_bytes"])
                )
                await send_group_msg(bot, group_id=int(gid), message=summary_text)
                if missing_kungfu_lines:
                    chunk_size = 100
                    total_lines = len(missing_kungfu_lines)
                    for start in range(0, total_lines, chunk_size):
                        end = min(start + chunk_size, total_lines)
                        chunk_header = f"未查询到心法的角色（共{total_lines}人，第{start + 1}-{end}名）"
                        chunk_message = "\n".join(missing_kungfu_lines[start:end])
                        await send_group_msg(bot,
                            group_id=int(gid),
                            message=f"{chunk_header}\n{chunk_message}",
                        )
                        await asyncio.sleep(0.5)
                logger.info("status_monitor 向群{}推送每日竞技排名统计", gid)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning("status_monitor 向群{}推送竞技排名统计失败: {}", gid, e)

    except Exception as e:
        logger.warning("status_monitor 每日竞技排名推送任务出错: {}", e)
        await notify_jjc_ranking_failure(bot, f"每日竞技排名推送任务出错：{e}")
