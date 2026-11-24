from src.infra.group_delivery import send_group_msg
from typing import Dict, Any, List, Optional, Annotated
import asyncio
from nonebot import get_driver, on_regex, require,on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.params import RegexGroup
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.rule import Rule
import time
from nonebot.log import logger
from nonebot.matcher import Matcher
from jinja2 import Environment, FileSystemLoader, Template
from src.plugins.wanbaolou.api import api, JX3TradeAPI, search_jx3_appearances
from src.plugins.wanbaolou.config import config
from src.plugins.wanbaolou.utils import format_time_string, save_image_cache
from src.infra.screenshot import jietu
from src.utils.random_text import suijitext
from config import wanbaolou
from src.utils.shared_data import SEARCH_RESULTS,user_sessions
from .alias import setup_alias_refresh_job
from src.storage.mongo_repos.wanbaolou_sub_repo import WanbaolouSubRepo

from src.infra.mongo import get_db as _get_db


def _repo() -> WanbaolouSubRepo:
    return WanbaolouSubRepo(db=_get_db())


# 导出主要功能函数，方便直接导入使用
# 创建全局API实例（禁用SSL验证）
api = JX3TradeAPI(verify_ssl=False)
env = Environment(loader=FileSystemLoader('templates'))


# 导出获取物品列表的方法（带解析功能）
async def get_item_list(item_name=None, sort_type=None,
                        status_filter=None, min_price=None, max_price=None,
                        page=1, page_size=10, follow_sort=None, parse_data=True):
    """
    获取物品列表完整数据（包含分页信息和解析后的物品数据）

    Args:
        item_name: 物品名称
        sort_type: 价格排序方式 (1:从低到高, 0:从高到低)
        status_filter: 状态过滤 (2:在售, 1:公示)
        min_price: 最低价格
        max_price: 最高价格
        page: 页码
        page_size: 每页数量
        follow_sort: 关注排序方式 (0:从低到高, 1:从高到低)
        parse_data: 是否解析数据为易读格式

    Returns:
        Dict: 完整物品数据，包含列表和分页信息
    """
    return await api.get_item_list(
        item_name=item_name,
        sort_type=sort_type,
        status_filter=status_filter,
        min_price=min_price,
        max_price=max_price,
        page=page,
        page_size=page_size,
        follow_sort=follow_sort,
        parse_data=parse_data
    )


# 导出获取物品图片URL的方法
async def get_item_image(item_name):
    """
    获取物品图片URL

    Args:
        item_name: 物品名称

    Returns:
        str: 图片URL，如果未找到则返回None
    """
    return await api.get_item_image(item_name)


# 导入定时任务插件
scheduler = require("nonebot_plugin_apscheduler").scheduler

from .searcher import init_appearance_searcher, search_appearance

# 定义导出的内容
__all__ = [
    "api",
    "JX3TradeAPI",
    "config",
    "search_jx3_appearances",
    "get_item_list",
    "get_item_image",
    "format_time_string",
    "save_image_cache"
]

__plugin_meta__ = PluginMetadata(
    name="外观搜索",
    description="模糊搜索剑网3外观物品",
    usage="外观 <关键词>"
)

driver = get_driver()

# 全局变量存储搜索结果
# 常量定义
SEARCH_TIMEOUT = 30  # 搜索结果有效期5分钟
SESSION_TIMEOUT = SEARCH_TIMEOUT  # 确保两个超时时间一致
NOTIFIED_USERS = set()  # 已通知的用户集合
# 全局变量定义（放在代码顶部，其他全局变量旁边）
# 全局变量定义（放在代码顶部，其他全局变量旁边）
USER_LAST_QUERY = {}  # 用户ID -> 最近查询的外观信息

# 获取全局机器人实例
_bot = None
@driver.on_bot_connect
async def _(bot: Bot):
    global _bot
    _bot = bot


@driver.on_startup
async def _():
    """初始化搜索器"""
    logger.info("初始化外观搜索插件...")
    await init_appearance_searcher("waiguan.json")
    logger.info("外观搜索插件初始化完成")

    # 启动超时检查任务
    scheduler.add_job(
        check_timeouts,
        "interval",
        seconds=5,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
        replace_existing=True,
    )

    # 初始化并定时刷新别名缓存，仅用于物价/外观查询时本地解析别名
    await setup_alias_refresh_job(scheduler)


# 定期检查超时会话的函数
async def check_timeouts():
    if not _bot:
        return

    from nonebot.adapters.onebot.v11 import MessageSegment

    current_time = time.time()

    # 创建SEARCH_RESULTS的副本以避免在迭代时修改字典
    search_results_copy = dict(SEARCH_RESULTS)

    # 检查所有会话
    for user_id, data in search_results_copy.items():
        # 再次检查用户ID是否仍在字典中
        if user_id not in SEARCH_RESULTS:
            continue

        if current_time > data["expiry_time"] and user_id not in NOTIFIED_USERS:
            NOTIFIED_USERS.add(user_id)

            # 自动选择前再次检查用户会话是否存在
            if user_id not in SEARCH_RESULTS:
                logger.info(f"用户 {user_id} 的会话已不存在，跳过自动选择")
                continue

            # 自动选择第一个结果
            try:
                results = data["results"]
                if results and len(results) > 0:
                    # 处理前再次检查会话是否存在
                    if user_id not in SEARCH_RESULTS:
                        logger.info(f"用户 {user_id} 的会话已不存在，跳过自动选择")
                        continue

                    selected = results[0]  # 选择第一项
                    name = selected['name']
                    group_id = data.get("group_id")

                    # 直接查询物品信息并生成图片
                    logger.info(f"已为用户 {user_id} 自动选择第一个搜索结果: {name}")

                    # 直接执行查询逻辑
                    mingcheng = name
                    sale_items = await api.get_item_list(
                        item_name=mingcheng,
                        sort_type=1,  # 1表示价格从低到高
                        status_filter=2  # 公示=1 2表示在售
                    )
                    public_items = await api.get_item_list(
                        item_name=mingcheng,
                        follow_sort=0,  # 关注排序方式 (0:从低到高, 1:从高到低)
                        status_filter=1  # 公示=1 2表示在售
                    )
                    # 检查用户是否已订阅此外观，并获取订阅价格
                    is_subscribed = False
                    subscribed_price = 0
                    user_subscriptions = await get_user_subscriptions(user_id)
                    for sub in user_subscriptions:
                        if sub["item_name"] == mingcheng:
                            is_subscribed = True
                            subscribed_price = sub["price_threshold"]
                            break
                    img = await get_item_image(mingcheng)
                    template = env.get_template('万宝楼查询.html')
                    html_content = template.render(
                        header_image=img,
                        sale_total=sale_items.get('total_items', 0),
                        public_total=public_items.get('total_items', 0),
                        sale_items=sale_items.get('parsed_items', []),
                        public_items=public_items.get('parsed_items', []),
                        is_subscribed=is_subscribed,  # 是否已订阅
                        subscribed_price=subscribed_price,  # 订阅价格阈值
                    )
                    image_bytes = await jietu(html_content, 810, "ck")

                    # 发送消息
                    if group_id:
                        await send_group_msg(_bot,
                            group_id=group_id,
                            message=MessageSegment.at(user_id) + "查询结果\n" + MessageSegment.image(image_bytes)
                        )
                    else:
                        await _bot.send_private_msg(
                            user_id=int(user_id),
                            message="查询结果\n" + MessageSegment.image(image_bytes)
                        )
                else:
                    # 没有结果，仅通知超时
                    group_id = data.get("group_id")
                    if group_id:
                        await send_group_msg(_bot,
                            group_id=group_id,
                            message=MessageSegment.at(user_id) + "外观搜索会话已超时，请重新搜索"
                        )
                    else:
                        await _bot.send_private_msg(
                            user_id=int(user_id),
                            message="外观搜索会话已超时，请重新搜索"
                        )
            except Exception as e:
                logger.error(f"自动选择/超时通知失败: {str(e)}")
                logger.error(f"详细错误信息: {type(e).__name__}: {e}")
            finally:
                # 处理完毕后立即删除该用户会话
                if user_id in SEARCH_RESULTS:
                    del SEARCH_RESULTS[user_id]
                    logger.info(f"已删除用户 {user_id} 的超时会话")


# 外观搜索命令
search_cmd = on_regex(r"^(外观|物价)\s+(.+)$", priority=5, block=True)


@search_cmd.handle()
async def handle_search(bot: Bot, event: Event, matcher: Matcher, matched: Annotated[tuple[str, ...], RegexGroup()]):
    """处理外观搜索命令"""
    # matched[0]是命令类型(外观或物价)，matched[1]是实际关键词
    keyword = matched[1].strip()  # 正确获取关键词
    user_id = str(event.user_id)

    # 清除之前的通知记录
    if user_id in NOTIFIED_USERS:
        NOTIFIED_USERS.remove(user_id)
    if user_id in SEARCH_RESULTS:
        del SEARCH_RESULTS[user_id]
    # 删除资历会话
    if user_id in user_sessions:
        del user_sessions[user_id]
    logger.info(f"用户 {user_id} 搜索关键词: {keyword}")

    if not keyword:
        await matcher.finish("请输入要搜索的关键词，例如：外观 故梦")
        return

    try:
        print(f"[cmd] start search keyword='{keyword}'")
        results = await search_appearance(keyword)
        print(f"[cmd] search done keyword='{keyword}', count={len(results) if results else 0}")

        if not results:
            await matcher.finish(f"未找到与 '{keyword}' 相关的物品")
            return

        # 如果只有一个结果，直接执行查询
        if len(results) == 1:
            selected = results[0]
            await bot.send(event, Message(f"正在为您查询,{selected['name']}"))
            logger.info(f"仅找到一个结果，自动查询: {selected['name']}")
            await process_appearance_query(bot, event, selected, is_auto=True)
            return

        # 保存搜索结果到用户状态
        SEARCH_RESULTS[user_id] = {
            "results": results,
            "expiry_time": time.time() + SESSION_TIMEOUT,
            "group_id": getattr(event, "group_id", None)  # 保存群组ID，如果有的话
        }
        logger.info(f"为用户 {user_id} 保存了 {len(results)} 个搜索结果")

        # 构建回复消息
        reply = f"找到 {len(results)} 个与 '{keyword}' 相关的物品：\n"
        for i, item in enumerate(results, 1):
            name_to_show = item.get('display') or f"{item['name']} ({item.get('category','')})"
            reply += f"{i}. {name_to_show}\n"
        reply += f"--------------------------------"
        reply += f"\n请在 {SESSION_TIMEOUT} 秒内回复数字选择"
        reply += f"超时将自动选择第一项: {results[0]['name']}"

        await matcher.finish(Message(reply.strip()))

    except FinishedException:
        # 由 matcher.finish 主动结束流程，直接抛出交给 NoneBot 处理
        raise
    except Exception as e:
        logger.exception(f"外观/物价搜索异常，关键词: {keyword}")
        await matcher.finish("搜索出现异常，请稍后重试")




# 数字选择正则匹配器
# 数字选择正则匹配器
async def _in_search_session(event: Event) -> bool:
    user_id = str(getattr(event, "user_id", ""))
    if not user_id:
        return False

    session = SEARCH_RESULTS.get(user_id)
    if not session:
        return False

    expiry_time = session.get("expiry_time", 0)
    if time.time() > expiry_time:
        SEARCH_RESULTS.pop(user_id, None)
        return False

    return True


number_choice = on_regex(r"^(\d+)$", rule=Rule(_in_search_session))


@number_choice.handle()
async def handle_number_choice(bot: Bot, event: Event, matcher: Matcher,
                               matched: Annotated[tuple[str, ...], RegexGroup()]):
    """处理数字选择命令"""
    user_id = str(event.user_id)
    msg = matched[0]

    logger.info(f"接收到数字选择: {msg} 来自用户 {user_id}")
    # 删除资历会话
    if user_id in user_sessions:
        del user_sessions[user_id]
    # 检查用户是否有搜索结果
    if user_id not in SEARCH_RESULTS:
        logger.debug(f"用户 {user_id} 没有活跃的搜索会话，忽略数字选择")
        return

    # 清除之前的通知记录
    if user_id in NOTIFIED_USERS:
        NOTIFIED_USERS.remove(user_id)

    # 检查是否超时
    current_time = time.time()
    if current_time > SEARCH_RESULTS[user_id]["expiry_time"]:
        # 超时，删除会话
        results = SEARCH_RESULTS[user_id]["results"]
        del SEARCH_RESULTS[user_id]
        logger.info(f"用户 {user_id} 的会话已超时")

        # 自动选择第一项
        if results and len(results) > 0:
            selected = results[0]
            await process_appearance_query(bot, event, selected, is_auto=True)
        else:
            await bot.send(event, MessageSegment.at(event.user_id) + Message("操作已超时，请重新输入外观关键词进行搜索"))
        return

    try:
        # 获取选择的数字
        number = int(msg)
        results = SEARCH_RESULTS[user_id]["results"]
        # 检查选择是否有效
        if number < 1 or number > len(results):
            await bot.send(event, MessageSegment.at(event.user_id) + Message(f"请选择有效的序号 (1-{len(results)})"))
            return

        # 获取选择的外观
        selected = results[number - 1]
        logger.info(f"用户 {user_id} 选择了外观: {selected['name']}，{selected['category']}")
        if selected['name']:
            await bot.send(event, Message(f"正在为您查询,{selected['name']}"))


        # 处理查询
        await process_appearance_query(bot, event, selected)

        # 使用后删除搜索结果，防止重复处理
        del SEARCH_RESULTS[user_id]

    except ValueError:
        await bot.send(event, MessageSegment.at(event.user_id) + Message("请输入有效的数字"))
    except Exception as e:
        logger.error(f"处理选择时出错: {str(e)}")



async def process_appearance_query(bot: Bot, event: Event, selected: dict, is_auto=False):
    """处理外观查询，生成图片并发送"""
    mingcheng = item_name = selected['name']
    user_id = str(event.user_id)

    # 保存用户最近的查询，以便订阅功能使用
    USER_LAST_QUERY[user_id] = selected

    # 设置查询信息过期时间（比如30分钟后）
    asyncio.create_task(clear_user_query(user_id, 30 * 60))

    # 获取在售物品
    sale_items = await api.get_item_list(
        item_name=mingcheng,
        sort_type=1,  # 1表示价格从低到高
        status_filter=2  # 公示=1 2表示在售
    )
    # 获取公示物品
    public_items = await api.get_item_list(
        item_name=mingcheng,
        follow_sort=0,  # 关注排序方式 (0:从低到高, 1:从高到低)
        status_filter=1  # 公示=1 2表示在售
    )

    # 检查用户是否已订阅此外观，并获取订阅价格
    is_subscribed = False
    subscribed_price = 0
    user_subscriptions = await get_user_subscriptions(user_id)
    for sub in user_subscriptions:
        if sub["item_name"] == mingcheng:
            is_subscribed = True
            subscribed_price = sub["price_threshold"]
            break
    text = suijitext()
    img = await get_item_image(mingcheng)
    template = env.get_template('万宝楼查询.html')
    html_content = template.render(
        header_image=img,
        sale_total=sale_items.get('total_items', 0),
        public_total=public_items.get('total_items', 0),
        sale_items=sale_items.get('parsed_items', []),
        public_items=public_items.get('parsed_items', []),
        is_subscribed=is_subscribed,  # 是否已订阅
        subscribed_price=subscribed_price , # 订阅价格阈值
        text=text
    )
    image_bytes = await jietu(html_content, 820, "ck")

    # 发送结果
    await bot.send(
        event,
        MessageSegment.at(event.user_id) +
        Message("   查询结果") +
        MessageSegment.image(image_bytes)
    )

    # 记录日志
    logger.info(
        f"用户 {user_id} 查询了外观: {selected['name']} ({selected.get('category', '未知类型')}), 自动选择: {is_auto}")


# 自动清理过期的用户查询记录
async def clear_user_query(user_id, delay):
    """在指定延迟后清除用户的查询记录"""
    await asyncio.sleep(delay)
    if user_id in USER_LAST_QUERY:
        del USER_LAST_QUERY[user_id]
        logger.debug(f"已清除用户 {user_id} 的查询记录")

# 在原始搜索函数中添加自动处理单个结果的逻辑
async def search_appearances(bot: Bot, event: Event, keyword: str):
    """搜索外观并处理结果"""
    try:
        # 搜索外观
        results = await search_jx3_appearances(keyword)

        # 处理搜索结果
        if not results or len(results.get('parsed_items', [])) == 0:
            await bot.send(event, MessageSegment.at(event.user_id) + Message(f"未找到与 {keyword} 相关的物品"))
            return

        items = results.get('parsed_items', [])

        # 如果只有一个结果，直接执行查询
        if len(items) == 1:
            selected = {
                'name': items[0]['name'],
                'category': items[0].get('type', '未知类型')
            }
            logger.info(f"仅找到一个结果，自动查询: {selected['name']}")
            await process_appearance_query(bot, event, selected, is_auto=True)
            return

        # 多个结果，保存并让用户选择
        user_id = str(event.user_id)
        # 将结果转换为用于显示的格式
        display_results = []
        for item in items:
            display_results.append({
                'name': item['name'],
                'category': item.get('type', '未知类型')
            })

        # 保存搜索结果
        SEARCH_RESULTS[user_id] = {
            'results': display_results,
            'expiry_time': time.time() + SEARCH_TIMEOUT,
            'group_id': getattr(event, 'group_id', None)
        }

        # 构建显示消息
        reply = f"找到 {len(display_results)} 个相关物品:\n"
        for i, item in enumerate(display_results, 1):
            reply += f"{i}. {item['name']} ({item['category']})\n"
        reply += "\n请回复数字选择要查询的外观"

        await bot.send(event, MessageSegment.at(event.user_id) + Message(reply))

    except Exception as e:
        print(e)




#订阅功能开始
# 添加价格订阅
async def add_price_subscription(user_id: str, item_name: str, price_threshold: int,
                                 group_id: Optional[int] = None) -> bool:
    logger.info("用户 {} 订阅了 {} 的价格提醒，阈值: {}", user_id, item_name, price_threshold)
    return await _repo().add(user_id, item_name, price_threshold, group_id)


# 获取用户的所有订阅
async def get_user_subscriptions(user_id: str) -> List[Dict[str, Any]]:
    return await _repo().find_by_user(user_id)


# 删除订阅
async def remove_subscription(user_id: str, index: int) -> Optional[Dict[str, Any]]:
    subs = await _repo().find_by_user(user_id)
    if not subs or index < 1 or index > len(subs):
        return None
    removed = subs[index - 1]
    item_name = removed["item_name"]
    success = await _repo().remove(user_id, item_name)
    if success:
        logger.info("用户 {} 删除了对 {} 的价格订阅", user_id, item_name)
        return removed
    return None


# 获取所有订阅
suoyou_config_cmd = on_command("所有订阅", priority=5)


@suoyou_config_cmd.handle()
async def get_all_subscriptions(bot: Bot, event: Event):
    """显示所有用户的所有订阅"""
    all_subs = await _repo().all()

    if not all_subs:
        await suoyou_config_cmd.finish("当前没有任何用户订阅")
        return

    # 按 user_id 分组
    grouped: dict[str, list[dict]] = {}
    for sub in all_subs:
        uid = sub.get("user_id", "未知")
        if uid not in grouped:
            grouped[uid] = []
        grouped[uid].append(sub)

    message = "所有用户订阅信息：\n"
    total_subs = len(all_subs)

    for user_id, subscriptions in grouped.items():
        user_info = f"用户 {user_id} 的订阅({len(subscriptions)}个):\n"
        for sub in subscriptions:
            item_name = sub.get("item_name", "未知物品")
            price = sub.get("price_threshold", 0)
            user_info += f"  - {item_name}: {price}元\n"
        message += user_info + "\n"

    message += f"\n共有 {len(grouped)} 个用户，{total_subs} 个订阅"

    if len(message) > 1000:
        html_content = f"<pre>{message}</pre>"
        image_bytes = await jietu(html_content, 600, "ck")
        await suoyou_config_cmd.finish(MessageSegment.image(image_bytes))
    else:
        await suoyou_config_cmd.finish(message)


# 订阅外观价格变动的命令
subscribe_cmd = on_regex(r"^订阅\s+(\d+)$", priority=5, block=True)


@subscribe_cmd.handle()
async def handle_subscribe(bot: Bot, event: Event, matcher: Matcher, matched: Annotated[tuple[str, ...], RegexGroup()]):
    """处理外观价格订阅命令"""
    price_threshold = matched[0].strip()
    user_id = str(event.user_id)

    logger.info(f"用户 {user_id} 尝试订阅价格提醒，阈值: {price_threshold}")

    # 检查用户是否最近查询过外观
    if user_id not in USER_LAST_QUERY:
        await matcher.finish(MessageSegment.at(event.user_id) + Message("请先搜索并查询一个外观，然后再设置价格提醒"))
        return

    try:
        # 获取用户最近查询的外观信息
        last_query = USER_LAST_QUERY[user_id]
        item_name = last_query.get('name')
        category = last_query.get('category')
        price = int(price_threshold)

        # 设置价格提醒，保存到JSON文件
        success = await add_price_subscription(
            user_id,
            item_name,
            price,
            event.group_id if hasattr(event, 'group_id') else None
        )

        if success:
            # 响应用户
            await matcher.finish(MessageSegment.at(event.user_id) + Message(
                f"\n已为您订阅【{item_name}】的价格提醒\n"
                f"当价格低于 {price} 元时，将通知您\n"
                f"您可以通过「我的订阅」查看所有订阅"
            ))
        else:
            await matcher.finish(MessageSegment.at(event.user_id) + Message("订阅设置失败，请稍后重试"))

    except ValueError:
        await matcher.finish(MessageSegment.at(event.user_id) + Message("价格必须是有效的数字"))
    except Exception as e:
        logger.error(f"设置价格提醒时出错: {str(e)}")



# 查看我的订阅命令
my_subscriptions = on_regex(r"^我的订阅$", priority=5, block=True)


@my_subscriptions.handle()
async def handle_my_subscriptions(bot: Bot, event: Event, matcher: Matcher):
    """处理查看我的订阅命令"""
    user_id = str(event.user_id)

    # 从JSON文件获取用户订阅
    subscriptions = await get_user_subscriptions(user_id)

    if not subscriptions:
        await matcher.finish(MessageSegment.at(event.user_id) + Message("您当前没有任何订阅"))
        return

    # 构建回复消息
    reply = f"\n您当前有 {len(subscriptions)} 个价格订阅：\n"
    for i, alert in enumerate(subscriptions, 1):
        item_name = alert["item_name"]
        threshold = alert["price_threshold"]
        created_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(alert["created_at"]))
        reply += f"{i}. 【{item_name}】价格低于 {threshold} 元 (创建于 {created_at})\n"

    reply += "\n回复「取消订阅 序号」可以取消对应的订阅"

    await matcher.finish(MessageSegment.at(event.user_id) + Message(reply))


# 查看订阅价格
my_xsubscriptions = on_regex(r"^订阅价格$", priority=5, block=True)


@my_xsubscriptions.handle()
async def handle_my_subscriptions(bot: Bot, event: Event, matcher: Matcher):
    """处理查看我的订阅命令"""
    user_id = str(event.user_id)
    await bot.send(event, Message(f"正在为您查询,订阅价格！"))

    # 从JSON文件获取用户订阅
    subscriptions = await get_user_subscriptions(user_id)

    if not subscriptions:
        await matcher.finish(MessageSegment.at(event.user_id) + Message("您当前没有任何订阅"))
        return

    # 构建回复消息
    reply = f"\n您当前有 {len(subscriptions)} 个价格订阅：\n"

    # 1. 收集所有唯一的物品名称
    unique_items = set(alert["item_name"] for alert in subscriptions)

    # 2. 为每个唯一物品只查询一次价格
    price_cache = {}  # 存储已查询的价格信息

    for item_name in unique_items:
        try:
            # 获取在售物品
            sale_items = await api.get_item_list(
                item_name=item_name,
                sort_type=1,  # 1表示价格从低到高
                status_filter=2,  # 公示=1 2表示在售
                page_size=1,  # 仅获取1条记录
                page=1  # 第一页
            )

            # 检查在售物品是否有价格
            if sale_items and 'parsed_items' in sale_items and sale_items['parsed_items']:
                price_cache[item_name] = {
                    'price': sale_items['parsed_items'][0].get('price', "未知"),
                    'source': '在售'
                }
                continue  # 找到价格后跳过公示查询

            # 如果没有在售价格，查询公示价格
            public_items = await api.get_item_list(
                item_name=item_name,
                follow_sort=0,  # 从低到高
                status_filter=1,  # 公示状态
                page_size=1,  # 仅获取1条记录
                page=1  # 第一页
            )

            if public_items and 'parsed_items' in public_items and public_items['parsed_items']:
                price_cache[item_name] = {
                    'price': public_items['parsed_items'][0].get('price', "未知"),
                    'source': '公示'
                }
            else:
                price_cache[item_name] = {'price': "未知", 'source': '无'}

        except Exception as e:
            logger.error(f"获取物品 {item_name} 价格异常: {str(e)}")
            price_cache[item_name] = {'price': "获取失败", 'source': '错误'}

    # 3. 遍历每个订阅，使用缓存的价格信息
    for i, alert in enumerate(subscriptions, 1):
        item_name = alert["item_name"]
        threshold = alert["price_threshold"]

        # 使用缓存的价格信息
        price_info = price_cache.get(item_name, {'price': "未知", 'source': '无'})
        current_price = price_info['price']

        # 格式化价格显示
        price_display = current_price if current_price == "未知" or current_price == "获取失败" else f"{current_price} 元"

        # 判断是否低于阈值
        threshold_info = ""
        if isinstance(current_price, (int, float)) and current_price < threshold:
            threshold_info = f"【低于阈值!】"

        # 显示价格来源
        source_display = f"({price_info['source']})" if price_info['source'] != '无' and price_info[
            'source'] != '错误' else ""

        reply += f"{i}. 【{item_name}】价格阈值: {threshold} 元，当前价格: {price_display} {source_display} {threshold_info}\n"

    reply += "\n回复「取消订阅 序号」可以取消对应的订阅"

    await matcher.finish(MessageSegment.at(event.user_id) + Message(reply))


# 取消订阅命令
cancel_subscription = on_regex(r"^取消订阅\s+(\d+)$", priority=5, block=True)


@cancel_subscription.handle()
async def handle_cancel_subscription(bot: Bot, event: Event, matcher: Matcher,
                                     matched: Annotated[tuple[str, ...], RegexGroup()]):
    """处理取消订阅命令"""
    index_str = matched[0].strip()
    user_id = str(event.user_id)

    try:
        index = int(index_str)

        removed = await remove_subscription(user_id, index)
        if removed is None:
            await matcher.finish(MessageSegment.at(event.user_id) + Message("\n未找到指定的订阅或序号无效"))
            return

        item_name = removed["item_name"]
        remaining = len(await get_user_subscriptions(user_id))

        if remaining > 0:
            await matcher.finish(MessageSegment.at(event.user_id) + Message(
                f"\n已取消对【{item_name}】的价格订阅，剩余 {remaining} 个订阅"))
        else:
            await matcher.finish(
                MessageSegment.at(event.user_id) + Message(f"\n已取消对【{item_name}】的价格订阅\n您当前没有任何订阅"))

    except ValueError:
        await matcher.finish(MessageSegment.at(event.user_id) + Message("\n序号必须是有效的数字"))
    except Exception as e:
        logger.error(f"\n取消订阅时出错: {str(e)}")





# 定期检查价格的任务
async def check_price_alerts():
    """定期检查所有价格提醒"""
    if not _bot:
        return

    all_subs = await _repo().all()

    if not all_subs:
        logger.info("没有有效的价格订阅")
        return

    logger.info("开始检查价格提醒 ({} 个订阅)", len(all_subs))

    for sub in all_subs:
        try:
            item_name = sub["item_name"]
            threshold = sub["price_threshold"]
            user_id = sub["user_id"]

            sale_items = await api.get_item_list(
                item_name=item_name,
                sort_type=1,
                status_filter=2,
                page=1,
                page_size=1
            )

            if sale_items and sale_items.get('parsed_items') and len(sale_items['parsed_items']) > 0:
                lowest_price = sale_items['parsed_items'][0]['price']

                if lowest_price <= threshold:
                    message = (
                        f"\n价格提醒：【{item_name}】\n"
                        f"当前最低价: {lowest_price} 元\n"
                        f"您设置的阈值: {threshold} 元\n"
                        f"请及时查看，此条订阅已完成！"
                    )

                    if sub.get("group_id"):
                        await send_group_msg(_bot,
                            group_id=int(sub["group_id"]),
                            message=f"[CQ:at,qq={user_id}] {message}"
                        )
                    else:
                        await _bot.send_private_msg(
                            user_id=int(user_id),
                            message=message
                        )

                    await _repo().remove(user_id, item_name)
                    logger.info("已通知用户 {} 关于 {} 的价格提醒 ({} <= {})，该提醒已删除",
                                user_id, item_name, lowest_price, threshold)

        except Exception as e:
            logger.error(f"检查价格提醒时出错: {str(e)}")

    logger.info("价格提醒检查完成")


# 确保启动时加载订阅数据，并启动定期检查任务
@driver.on_startup
async def init_subscription_system():
    """初始化订阅系统"""
    logger.info("初始化价格订阅系统...")
    # 启动定期检查任务
    scheduler.add_job(check_price_alerts, "interval", minutes=wanbaolou)
    logger.info("价格订阅系统初始化完成")
