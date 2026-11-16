from __future__ import annotations

import re
import inspect

from typing import Any, Awaitable, Callable, Optional

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Event


def register(
    zhanji_ranking_matcher: Any,
    *,
    query_jjc_ranking: Callable[..., Awaitable[dict]],
    calculate_season_week_info: Callable[[int, Optional[float]], str],
    get_ranking_kungfu_data: Callable[..., Awaitable[dict]],
    save_ranking_stats: Callable[..., Any],
    generate_split_ranking_images: Callable[[Bot, Event, dict, str, bool], Awaitable[None]],
    generate_combined_ranking_image: Callable[[Bot, Event, dict, str, bool], Awaitable[None]],
) -> None:
    @zhanji_ranking_matcher.handle()
    async def zhanji_ranking_to_image(bot: Bot, event: Event) -> None:
        try:
            message_text = event.get_plaintext().strip()
            tag_tokens = [token for token in message_text.split()[1:] if token.startswith("tag=")]
            tag = None
            if tag_tokens:
                value = tag_tokens[0][4:]
                if len(tag_tokens) != 1 or not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
                    await bot.send(event, "tag 必须是正整数且只能指定一次，例如：竞技排名 tag=40")
                    return
                tag = int(value)
            message_text_lower = message_text.lower()
            is_split_mode = "拆分" in message_text
            is_debug_mode = "debug" in message_text_lower
            show_legendary = "橙武占比" in message_text
            use_cached_kungfu = "缓存心法" in message_text.split()

            if is_split_mode:
                mode_text = "拆分模式"
                if use_cached_kungfu:
                    mode_text += "，优先使用缓存心法"
                await bot.send(event, f"正在统计竞技场心法排名（{mode_text}），请稍候...")
            elif use_cached_kungfu:
                await bot.send(event, "正在统计竞技场心法排名（优先使用缓存心法），请稍候...")
            else:
                await bot.send(event, "正在统计竞技场心法排名，请稍候...")

            ranking_result = await query_jjc_ranking(tag=tag) if tag is not None else await query_jjc_ranking()
            if ranking_result is None:
                await bot.send(event, "获取竞技场排行榜数据失败：返回数据为空")
                return

            if ranking_result.get("error"):
                await bot.send(
                    event,
                    f"获取竞技场排行榜数据失败：{ranking_result.get('message', '未知错误')}",
                )
                return

            if ranking_result.get("code") != 0:
                await bot.send(
                    event,
                    f"获取竞技场排行榜数据失败：API返回错误码 {ranking_result.get('code')}",
                )
                return

            default_week = ranking_result.get("defaultWeek")
            cache_time = ranking_result.get("cache_time")
            week_info = (
                calculate_season_week_info(default_week, cache_time)
                if default_week
                else "第12周"
            )

            result = await get_ranking_kungfu_data(
                ranking_data=ranking_result,
                use_cached_kungfu=use_cached_kungfu,
            )
            if result is None:
                await bot.send(event, "获取心法分布数据失败：返回数据为空")
                return

            if result.get("error"):
                await bot.send(
                    event,
                    f"获取心法分布数据失败：{result.get('message', '未知错误')}",
                )
                return

            stats = result.get("kungfu_statistics", {})
            if not stats:
                await bot.send(event, "心法统计数据为空，无法生成统计图片")
                return

            if any(key in result for key in ("kungfu_cache_scope", "refresh_degraded", "_sync_request", "equipment_view")):
                save_task = save_ranking_stats(ranking_result, stats, week_info, payload=result)
            else:
                save_task = save_ranking_stats(ranking_result, stats, week_info)
            if result.get("equipment_view") is not None and save_task is None:
                raise RuntimeError("equipment_publication_unavailable")
            if inspect.isawaitable(save_task):
                await save_task

            if is_split_mode:
                await generate_split_ranking_images(bot, event, stats, week_info, show_legendary)
            else:
                await generate_combined_ranking_image(bot, event, stats, week_info, show_legendary)

            ranking_kungfu_lines = result.get("ranking_kungfu_lines", [])
            missing_kungfu_lines = result.get("missing_kungfu_lines", [])

            if is_debug_mode and ranking_kungfu_lines:
                chunk_size = 200
                total_lines = len(ranking_kungfu_lines)
                for start in range(0, total_lines, chunk_size):
                    end = min(start + chunk_size, total_lines)
                    chunk_header = f"竞技场心法排名（第{start + 1}-{end}名）"
                    chunk_message = "\n".join(ranking_kungfu_lines[start:end])
                    await bot.send(event, f"{chunk_header}\n{chunk_message}")
            elif missing_kungfu_lines:
                chunk_size = 100
                total_lines = len(missing_kungfu_lines)
                for start in range(0, total_lines, chunk_size):
                    end = min(start + chunk_size, total_lines)
                    chunk_header = f"未查询到心法的角色（共{total_lines}人，第{start + 1}-{end}名）"
                    chunk_message = "\n".join(missing_kungfu_lines[start:end])
                    await bot.send(event, f"{chunk_header}\n{chunk_message}")

        except Exception as exc:
            logger.exception("战绩排名统计详细错误")
            await bot.send(event, f"战绩排名统计失败：{str(exc)}")
