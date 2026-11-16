from __future__ import annotations

from typing import Any, Awaitable, Callable

from jinja2 import Environment
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment

from src.services.jx3.weapon_quality import extract_member_weapon_name, is_jjc_legendary_weapon


def _prepare_template_data(
    rank_data: dict[str, Any], rank_type: str
) -> list[tuple[Any, Any, str, Any, float]]:
    if not rank_data or rank_type not in rank_data:
        return []
    sorted_list = rank_data[rank_type].get("list", [])
    if not sorted_list:
        return []
    valid_count = rank_data[rank_type].get("valid_count", 0)
    min_score = rank_data[rank_type].get("min_score")
    members_map = rank_data[rank_type].get("members", {}) or {}
    return [
        (
            k,
            v,
            f"{v / valid_count * 100:.1f}%" if valid_count > 0 else "0%",
            min_score,
            ((rank_data[rank_type].get("equipment_metrics", {}).get(k, {}).get("legendary_ratio") or 0) * 100
             if rank_data.get("equipment_source") == "local_win_v2"
             else _calculate_legendary_percent(members_map.get(k, []))),
        )
        for k, v in sorted_list
    ]


def _calculate_legendary_percent(members: list[dict[str, Any]]) -> float:
    if not members:
        return 0.0
    legendary_count = sum(
        1
        for item in members
        if is_jjc_legendary_weapon(
            item.get("weapon_quality"),
            extract_member_weapon_name(item),
        )
    )
    return legendary_count / len(members) * 100


def _coverage_label(rank_data: dict[str, Any]) -> str:
    cache = rank_data.get("kungfu_cache")
    label = (f"心法覆盖 {(cache.get('coverage') or 0) * 100:.1f}% · "
             f"未知 {cache.get('unknown', 0)}") if cache else "心法覆盖率未记录"
    if rank_data.get("equipment_source") == "local_win_v2":
        metrics = rank_data.get("equipment_metrics") or {}
        coverage = metrics.get("equipment_coverage")
        label += f" · 装备覆盖 {coverage*100:.1f}%" if coverage is not None else " · 装备覆盖 --"
        label += f" · 装备未知 {metrics.get('U', 0)}"
    return label


def _equipment_incomplete(rank_data: dict[str, Any]) -> bool:
    if rank_data.get("equipment_source") == "local_win_v2" and isinstance(rank_data.get("equipment_metrics"), dict):
        return False
    return any((rank_data.get(lane) or {}).get("equipment_incomplete", False)
               for lane in ("healer", "dps"))


async def render_combined_ranking_image(
    *,
    env: Environment,
    render_template_image: Callable[..., Awaitable[bytes]],
    current_season: Any,
    stats: dict[str, Any],
    week_info: str,
    show_legendary: bool,
) -> dict[str, Any]:
    has_top_1000 = "top_1000" in stats
    scope_desc = "前200、前100、前50"
    if has_top_1000:
        scope_desc = "前1000、前200、前100、前50"

    image_bytes = await render_template_image(
        env,
        "竞技场心法排名统计.html",
        {
            "current_season": current_season,
            "week_info": week_info,
            "scope_desc": scope_desc,
            "top_1000_healer": _prepare_template_data(stats.get("top_1000", {}), "healer") if has_top_1000 else [],
            "top_1000_dps": _prepare_template_data(stats.get("top_1000", {}), "dps") if has_top_1000 else [],
            "top_200_healer": _prepare_template_data(stats.get("top_200", {}), "healer"),
            "top_200_dps": _prepare_template_data(stats.get("top_200", {}), "dps"),
            "top_100_healer": _prepare_template_data(stats.get("top_100", {}), "healer"),
            "top_100_dps": _prepare_template_data(stats.get("top_100", {}), "dps"),
            "top_50_healer": _prepare_template_data(stats.get("top_50", {}), "healer"),
            "top_50_dps": _prepare_template_data(stats.get("top_50", {}), "dps"),
            "has_top_1000": has_top_1000,
            "show_legendary": show_legendary and not any(_equipment_incomplete(v) for v in stats.values() if isinstance(v, dict)),
            "coverage_labels": {k: _coverage_label(v) for k, v in stats.items() if isinstance(v, dict)},
            "equipment_incomplete": any(_equipment_incomplete(v) for v in stats.values() if isinstance(v, dict)),
        },
        width=1120,
        height="ck",
    )

    processed_key = "top_1000" if has_top_1000 else "top_200"
    total_valid_data = (stats.get(processed_key, {}) or {}).get("total_valid_count", 0) or 0
    processed_label = "前1000名" if has_top_1000 else "前200名"

    return {
        "image_bytes": image_bytes,
        "total_valid_data": total_valid_data,
        "processed_label": processed_label,
        "scope_desc": scope_desc,
        "has_top_1000": has_top_1000,
    }


async def send_combined_ranking_image(
    bot: Bot,
    event: Event,
    *,
    env: Environment,
    render_template_image: Callable[..., Awaitable[bytes]],
    current_season: Any,
    stats: dict[str, Any],
    week_info: str,
    show_legendary: bool,
) -> None:
    payload = await render_combined_ranking_image(
        env=env,
        render_template_image=render_template_image,
        current_season=current_season,
        stats=stats,
        week_info=week_info,
        show_legendary=show_legendary,
    )
    await bot.send(event, MessageSegment.image(payload["image_bytes"]))
    await bot.send(
        event,
        f"统计完成！共处理 {payload['total_valid_data']} 条有效数据（{payload['processed_label']}），统计范围：{payload['scope_desc']}",
    )


async def send_split_ranking_images(
    bot: Bot,
    event: Event,
    *,
    env: Environment,
    render_template_image: Callable[..., Awaitable[bytes]],
    current_season: Any,
    stats: dict[str, Any],
    week_info: str,
    show_legendary: bool,
) -> None:
    has_top_1000 = "top_1000" in stats
    ranking_configs = []
    if has_top_1000:
        ranking_configs.extend(
            [
                {
                    "name": "前1000奶妈",
                    "template": "竞技场心法排名_前1000奶妈.html",
                    "data_key": "top_1000_healer",
                    "data": _prepare_template_data(stats.get("top_1000", {}), "healer"),
                },
                {
                    "name": "前1000DPS",
                    "template": "竞技场心法排名_前1000DPS.html",
                    "data_key": "top_1000_dps",
                    "data": _prepare_template_data(stats.get("top_1000", {}), "dps"),
                },
            ]
        )
    ranking_configs.extend(
        [
            {
                "name": "前200奶妈",
                "template": "竞技场心法排名_前200奶妈.html",
                "data_key": "top_200_healer",
                "data": _prepare_template_data(stats.get("top_200", {}), "healer"),
            },
            {
                "name": "前200DPS",
                "template": "竞技场心法排名_前200DPS.html",
                "data_key": "top_200_dps",
                "data": _prepare_template_data(stats.get("top_200", {}), "dps"),
            },
            {
                "name": "前100奶妈",
                "template": "竞技场心法排名_前100奶妈.html",
                "data_key": "top_100_healer",
                "data": _prepare_template_data(stats.get("top_100", {}), "healer"),
            },
            {
                "name": "前100DPS",
                "template": "竞技场心法排名_前100DPS.html",
                "data_key": "top_100_dps",
                "data": _prepare_template_data(stats.get("top_100", {}), "dps"),
            },
            {
                "name": "前50奶妈",
                "template": "竞技场心法排名_前50奶妈.html",
                "data_key": "top_50_healer",
                "data": _prepare_template_data(stats.get("top_50", {}), "healer"),
            },
            {
                "name": "前50DPS",
                "template": "竞技场心法排名_前50DPS.html",
                "data_key": "top_50_dps",
                "data": _prepare_template_data(stats.get("top_50", {}), "dps"),
            },
        ]
    )

    images_sent = 0
    for i, config in enumerate(ranking_configs, 1):
        try:
            image_bytes = await render_template_image(
                env,
                config["template"],
                {
                    "current_season": current_season,
                    "week_info": week_info,
                    config["data_key"]: config["data"],
                    "show_legendary": show_legendary and not _equipment_incomplete(stats.get(config["data_key"].rsplit("_", 1)[0], {})),
                    "coverage_label": _coverage_label(stats.get(config["data_key"].rsplit("_", 1)[0], {})),
                    "equipment_incomplete": _equipment_incomplete(stats.get(config["data_key"].rsplit("_", 1)[0], {})),
                },
                width=800,
                height="ck",
            )
            await bot.send(event, MessageSegment.image(image_bytes))
            images_sent += 1
            if i < len(ranking_configs):
                import asyncio

                await asyncio.sleep(1)
        except Exception as exc:
            logger.exception(f"生成{config['name']}图片失败")
            await bot.send(event, f"生成{config['name']}图片失败: {str(exc)}")

    processed_key = "top_1000" if has_top_1000 else "top_200"
    total_valid_data = (stats.get(processed_key, {}) or {}).get("total_valid_count", 0) or 0
    processed_label = "前1000名" if has_top_1000 else "前200名"

    await bot.send(
        event,
        f"拆分统计完成！共处理 {total_valid_data} 条有效数据（{processed_label}），已生成{images_sent}张详细排名图",
    )

