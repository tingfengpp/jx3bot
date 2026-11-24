from src.infra.group_delivery import send_group_msg
import os
import sys
import json
import time
import subprocess
import asyncio
import config as app_config
from config import ADMIN_QQ  # 从config.py导入管理员QQ列表
from nonebot import on_command, get_driver, logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Bot
from nonebot.params import CommandArg
import src.utils.shared_data


# 运行时配置文件路径
CONFIG_FILE = "runtime_config.json"
RESTART_FLAG_FILE = "restart_info.json"


CONFIG_SCHEMA = {
    "TOKEN": {
        "type": str,
        "sensitive": True,
        "allow_modify": True,
        "validate": None,
    },
    "TICKET": {
        "type": str,
        "sensitive": True,
        "allow_modify": True,
        "validate": None,
    },
    "MONGO_URI": {
        "type": str,
        "sensitive": True,
        "allow_modify": False,
        "validate": None,
    },
    "SESSION_data": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": None,
    },
    "calendar_time": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": None,
    },
    "STATUS_check_time": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": None,
    },
    "JJC_SYNC_WORKER_COUNT": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": lambda value: value >= 0,
    },
    "JJC_SYNC_DISPATCHER_ENABLED": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": lambda value: value in (0, 1),
    },
    "JJC_SYNC_DISPATCHER_IDLE_SLEEP": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": lambda value: value >= 1,
    },
    "JJC_SYNC_DISPATCHER_BATCH_SIZE": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": lambda value: value >= 1,
    },
    "JJC_SYNC_DISPATCHER_TARGET_PER_WORKER": {
        "type": int,
        "sensitive": False,
        "allow_modify": True,
        "validate": lambda value: value >= 1,
    },
}


def _modifiable_config_keys():
    return [
        key
        for key, schema in CONFIG_SCHEMA.items()
        if schema.get("allow_modify", True)
    ]


def _parse_config_value(key, raw_value):
    schema = CONFIG_SCHEMA[key]
    value_type = schema["type"]
    if value_type is int:
        try:
            value = int(raw_value)
        except ValueError:
            return None, "数值类型配置项需要整数"
    elif value_type is str:
        value = str(raw_value)
    else:
        try:
            value = value_type(raw_value)
        except (TypeError, ValueError):
            return None, "配置项类型转换失败"

    validate = schema.get("validate")
    if validate is not None and not validate(value):
        return None, "配置项值不合法"
    return value, None


def _mask_sensitive_value(value):
    if value is None or value == "":
        return "未配置"
    text = str(value)
    if len(text) <= 3:
        return "已配置"
    return f"{text[:3]}***"


def _get_effective_config_value(runtime_config, key):
    if isinstance(runtime_config, dict) and key in runtime_config:
        return runtime_config[key]
    return getattr(app_config, key, None)


def _format_config_value(value, sensitive):
    if sensitive:
        return _mask_sensitive_value(value)
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _build_view_config_text(runtime_config):
    config_lines = []
    for key, schema in CONFIG_SCHEMA.items():
        value = _get_effective_config_value(runtime_config, key)
        display_value = _format_config_value(value, schema.get("sensitive", False))
        config_lines.append(f"{key} = {display_value}")
    return "\n".join(config_lines)

# 存储待发送的重启通知
pending_restart_info = None

# 读取运行时配置文件
def read_config_file():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return f"读取配置文件失败: {e}"

# 写入运行时配置文件
def write_config_file(content: dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        return f"写入配置文件失败: {e}"

# 记录重启信息并执行重启
async def restart_bot(group_id=None, user_id=None, reason="手动重启"):
    try:
        # 记录重启信息
        restart_info = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason
        }
        
        if group_id:
            restart_info["group_id"] = group_id
        if user_id:
            restart_info["user_id"] = user_id
        
        # 写入重启信息到标记文件
        with open(RESTART_FLAG_FILE, "w", encoding="utf-8") as f:
            json.dump(restart_info, f, ensure_ascii=False, indent=2)
        
        logger.info(f"已记录重启信息: {restart_info}")

        # 使用子进程启动新实例
        logger.info("准备重启机器人...")
        
        # 获取当前脚本路径
        current_path = os.path.abspath(sys.argv[0])
        
        # 等待2秒，确保消息发送完成
        await asyncio.sleep(2)
        
        # 创建一个新的进程来启动机器人
        if sys.platform == 'win32':
            # Windows平台
            subprocess.Popen(f'python "{current_path}"', shell=True)
        else:
            # Linux/MacOS平台
            subprocess.Popen(f'python3 "{current_path}" &', shell=True)
        
        # 退出当前进程
        logger.info("即将退出当前进程...")
        await asyncio.sleep(1)
        os._exit(0)
        
    except Exception as e:
        logger.exception(f"重启失败: {e}")
        return f"重启失败: {e}"

# 启动事件处理
driver = get_driver()

@driver.on_startup
async def startup_handler():
    """机器人启动时检查是否需要发送重启通知"""
    logger.info("机器人启动中，检查重启标记...")
    
    # 检查重启标记文件
    if os.path.exists(RESTART_FLAG_FILE):
        logger.info(f"找到重启标记文件: {RESTART_FLAG_FILE}")
        try:
            # 读取重启信息
            with open(RESTART_FLAG_FILE, "r", encoding="utf-8") as f:
                restart_info = json.load(f)
            
            logger.info(f"重启信息: {restart_info}")
            
            # 保存待发送的重启信息，等待连接建立后回复
            global pending_restart_info
            pending_restart_info = restart_info
            logger.info("已记录待发送的重启通知，等待 WebSocket 连接完成后发送")
        except Exception as e:
            logger.exception(f"处理重启标记失败: {e}")

@driver.on_bot_connect
async def handle_bot_connect(bot: Bot):
    """WebSocket 连接建立后发送重启完成通知"""
    global pending_restart_info

    try:
        # 如果内存中暂无信息但仍存在标记文件，则尝试加载一次
        if pending_restart_info is None and os.path.exists(RESTART_FLAG_FILE):
            with open(RESTART_FLAG_FILE, "r", encoding="utf-8") as f:
                pending_restart_info = json.load(f)
            logger.info(f"从文件补充待发送的重启信息: {pending_restart_info}")

        if not pending_restart_info:
            return

        restart_info = pending_restart_info
        pending_restart_info = None

        restart_reason = restart_info.get("reason", "未知原因")
        group_id = restart_info.get("group_id")
        user_id = restart_info.get("user_id")

        message = f"启动完成，重启原因: {restart_reason}"

        if group_id:
            logger.info(f"准备向群{group_id}发送重启完成通知")
            await send_group_msg(bot, group_id=int(group_id), message=message)
            logger.info(f"已向群{group_id}发送重启完成通知")
        elif user_id:
            logger.info(f"准备向用户{user_id}发送重启完成通知")
            await bot.send_private_msg(user_id=int(user_id), message=message)
            logger.info(f"已向用户{user_id}发送重启完成通知")
        else:
            logger.warning("未找到群组或用户 ID，跳过重启通知发送")
    except Exception as e:
        logger.exception(f"发送重启通知时出错: {e}")
    finally:
        if os.path.exists(RESTART_FLAG_FILE):
            os.remove(RESTART_FLAG_FILE)
            logger.info(f"已删除重启标记文件: {RESTART_FLAG_FILE}")

# 管理帮助命令
admin_help_cmd = on_command("管理帮助", priority=5)
@admin_help_cmd.handle()
async def handle_admin_help(event):
    user_id = event.get_user_id()
    if int(user_id) not in ADMIN_QQ:
        await admin_help_cmd.finish("您没有权限查看管理帮助")

    help_text = "\n".join([
        "管理员命令帮助",
        "",
        "配置与重启:",
        "/查看配置",
        "/修改配置 配置项=值",
        "/重启",
        "/重启 原因",
        "",
        "公告管理:",
        "/公告添加 标题 | 内容",
        "/公告列表",
        "/公告删除 <公告ID>",
        "",
        "JJC 对局同步:",
        "/jjc同步状态",
        "/jjc同步开始 limit=50  （只入队，worker 处理）",
        "/jjc同步开始 full",
        "/jjc同步开始 full days=14",
        "/jjc同步开始 full until=2026-06-08",
        "/jjc同步暂停 [原因]",
        "/jjc同步恢复",
        "/jjc同步重置 <服务器> <角色名>",
        "/jjc同步添加 <服务器> <角色名> [priority=100] [queued=1] [global_role_id=...] [role_id=...] [zone=...]",
        "/jjc同步优先级 <服务器> <角色名> <priority>",
        "",
        "普通功能帮助:",
        "帮助",
    ])
    await admin_help_cmd.finish(help_text)


# 查看配置命令
view_config_cmd = on_command("查看配置", priority=5)
@view_config_cmd.handle()
async def handle_view_config(event):
    # 检查权限
    user_id = event.get_user_id()
    if int(user_id) not in ADMIN_QQ:
        await view_config_cmd.finish("您没有权限查看配置")
    
    # 读取配置文件
    config_content = read_config_file()
    if isinstance(config_content, str) and config_content.startswith("配置文件"):
        await view_config_cmd.finish(config_content)
    
    # 解析配置内容，提取所有可查看配置项的当前有效值
    config_info = _build_view_config_text(config_content)
    
    token_limit = src.utils.shared_data.tokendata
    token_limit_text = token_limit if token_limit is not None else "未知"
    await view_config_cmd.finish(f"当前配置:\n token剩余:{token_limit_text}\n{config_info}")

# 修改配置命令
config_cmd = on_command("修改配置", priority=5)
@config_cmd.handle()
async def handle_config(event, args=CommandArg()):
    # 检查权限
    user_id = event.get_user_id()
    if int(user_id) not in ADMIN_QQ:
        await config_cmd.finish("您没有权限修改配置")
    
    arg_text = args.extract_plain_text().strip()
    if not arg_text:
        await config_cmd.finish(
            "用法: /修改配置 配置项=值\n"
            f"可修改的配置项: {', '.join(_modifiable_config_keys())}"
        )
    
    # 解析配置项和值
    try:
        key, value = arg_text.split('=', 1)
        key = key.strip()
        value = value.strip()
    except:
        await config_cmd.finish("格式错误，正确格式: 配置项=值")
    
    # 检查配置项是否允许修改
    if key not in CONFIG_SCHEMA or not CONFIG_SCHEMA[key].get("allow_modify", True):
        await config_cmd.finish(
            f"不允许修改该配置项，允许的配置项: {', '.join(_modifiable_config_keys())}"
        )
    
    # 读取当前配置
    config_content = read_config_file()
    if isinstance(config_content, str) and config_content.startswith("配置文件"):
        await config_cmd.finish(config_content)
    
    # 修改配置
    if not isinstance(config_content, dict):
        await config_cmd.finish("配置文件内容异常，无法修改")

    parsed_value, parse_error = _parse_config_value(key, value)
    if parse_error:
        await config_cmd.finish(parse_error)
    config_content[key] = parsed_value
    
    # 写入配置文件
    result = write_config_file(config_content)
    if result is not True:
        await config_cmd.finish(result)
    
    # 获取群组ID
    group_id = None
    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id
    
    # 告知用户配置已更新，并自动重启
    display_value = _mask_sensitive_value(parsed_value) if CONFIG_SCHEMA[key].get("sensitive") else parsed_value
    await config_cmd.send(f"配置项 {key} 已更新为 {display_value}，正在重启机器人...")
    await restart_bot(group_id=group_id, user_id=user_id, reason=f"修改配置 {key}")

# 添加重启命令
restart_cmd = on_command("重启", priority=5)
@restart_cmd.handle()
async def handle_restart(event, args=CommandArg()):
    # 检查权限
    user_id = event.get_user_id()
    if int(user_id) not in ADMIN_QQ:
        await restart_cmd.finish("您没有权限重启机器人")
    
    # 获取重启原因
    reason = args.extract_plain_text().strip()
    if not reason:
        reason = "手动重启"
    
    # 获取群组ID
    group_id = None
    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id
    
    # 告知用户正在重启
    await restart_cmd.send(f"正在重启机器人，原因: {reason}")
    await restart_bot(group_id=group_id, user_id=user_id, reason=reason)
