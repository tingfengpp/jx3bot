import hashlib
import hmac
import json
import datetime
import warnings
from collections import OrderedDict
import config as cfg

from src.infra.http_client import HttpClient

# 忽略SSL警告
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

try:
    from nonebot import logger  # type: ignore
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from src.utils.jjc_query_observability import foreground_safe_logger

logger = foreground_safe_logger(logger, component="signer")


def calculate_xsk(data):
    """
    计算X-Sk签名

    Args:
        data (dict): 请求参数字典

    Returns:
        tuple: (签名, JSON字符串)
    """
    secret_key = getattr(cfg, "TUILAN_SECRET_KEY", "") or ""
    if not secret_key:
        raise ValueError("缺少推栏签名密钥：请在 config.py 配置 TUILAN_SECRET_KEY")

    # 按字母顺序排序参数
    ordered_data = OrderedDict()
    for key in sorted(data.keys()):
        ordered_data[key] = data[key]

    # 确保JSON字符串正确编码
    try:
        json_str = json.dumps(ordered_data, separators=(',', ':'), ensure_ascii=False)
    except UnicodeEncodeError as e:
        logger.warning(f"tuilan_request JSON 编码错误: {e}")
        # 如果包含无法编码的字符，使用ensure_ascii=True
        json_str = json.dumps(ordered_data, separators=(',', ':'), ensure_ascii=True)

    # 推栏签名算法：JSON字符串 + 固定后缀
    input_data = f"{json_str}@#?.#@"

    # 使用HMAC-SHA256计算签名
    signature = hmac.new(
        secret_key.encode('utf-8'),
        input_data.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return signature, json_str


def build_tuilan_request(params=None):
    """Share the existing signed headers/body between sync and async adapters."""
    # 构造请求数据
    data = OrderedDict()

    # 添加用户传入的参数
    if params:
        for key, value in params.items():
            data[key] = value

    # 自动添加时间戳 - 推栏API要求的时间戳格式
    # 格式：年月日时分秒毫秒（17位数字）
    data["ts"] = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]

    # 计算X-Sk签名
    x_sk, raw_json = calculate_xsk(data)

    # 构造请求头 - 推栏API标准请求头
    tuilan_cookie = getattr(cfg, "TUILAN_COOKIE", "") or ""
    tuilan_device_id = getattr(cfg, "TUILAN_DEVICE_ID", "") or "lWrrIG5QpALPiSZ7txB//A=="
    tuilan_user_agent = getattr(cfg, "TUILAN_USER_AGENT", "") or "okhttp/3.12.2"

    headers = {
        "accept": "application/json",
        "deviceid": tuilan_device_id,
        "platform": "android",
        "gamename": "jx3",
        "fromsys": "APP",
        "clientkey": "1",
        "cache-control": "no-cache",
        "apiversion": "3",
        "sign": "true",
        "token": cfg.TICKET,
        "Content-Type": "application/json",
        "Host": "m.pvp.xoyo.com",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "User-Agent": tuilan_user_agent,
        "X-Sk": x_sk
    }
    if tuilan_cookie:
        headers["Cookie"] = tuilan_cookie

    return headers, raw_json


def tuilan_request(url, params=None):
    """
    推栏API请求方法

    Args:
        url (str): 请求地址
        params (dict, optional): 请求参数，不包含ts

    Returns:
        dict: 响应结果
    """
    headers, raw_json = build_tuilan_request(params)

    # 发送请求
    http_client = HttpClient(timeout=30.0, retries=2, backoff_seconds=0.5, verify=False)
    try:
        # 确保数据是UTF-8编码的字节串
        if isinstance(raw_json, str):
            data_bytes = raw_json.encode('utf-8')
        else:
            data_bytes = raw_json

        return http_client.request_json("POST", url, headers=headers, content=data_bytes, verify=False)
    except UnicodeEncodeError as e:
        logger.warning(f"tuilan_request 编码错误: {e}")
        # 尝试使用不同的编码方式
        try:
            data_bytes = raw_json.encode('utf-8', errors='ignore')
            return http_client.request_json("POST", url, headers=headers, content=data_bytes, verify=False)
        except Exception as e2:
            logger.warning(f"tuilan_request 备用编码也失败: {e2}")
            return {"error": f"编码错误: {e}"}


# 导出组件
tuilan_request_module = {
    "calculate_xsk": calculate_xsk,
    "tuilan_request": tuilan_request,
}
