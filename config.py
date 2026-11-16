"""
剑三机器人配置文件
包含API Token、URL等
"""
import json
import logging
import os
ADMIN_QQ = [595910443]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _local_equipment_flag(value, *, source):
    """Strict opt-in; diagnostics deliberately contain no supplied value."""
    if type(value) is int and value in (0, 1):
        return value
    if type(value) is str and value.strip() in ("0", "1"):
        return int(value.strip())
    logging.getLogger(__name__).warning(
        "Invalid configuration key=JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED source=%s; disabled", source)
    return 0

# API认证凭证
TOKEN = ""
TICKET = ""

# API接口地址
API_URLS = {
    "烟花查询": "https://www.jx3api.com/firework/records",
    "奇遇查询": "https://www.jx3api.com/event/records",
    "装备查询": "",
    "竞技查询": "https://www.jx3api.com/arena/recent",
    "副本查询": "",
    "名片查询": "https://www.jx3api.com/card/record",
    "百战查询": "https://www.jx3api.com/monster/records",
    "资历查询": "https://www.jx3api.com/tuilan/achievement",
    "竞技场时间查询": "https://m.pvp.xoyo.com/3c/mine/arena/time-tag",
    "竞技场排行榜查询": "https://m.pvp.xoyo.com/3c/mine/arena/top200",
    "竞技场战局历史": "https://m.pvp.xoyo.com/3c/mine/match/history",
    "竞技场个人战局历史": "https://m.pvp.xoyo.com/mine/match/person-history",
    "竞技场战局详情": "https://m.pvp.xoyo.com/3c/mine/match/detail",
    "竞技场战局回放": "https://m.pvp.xoyo.com/3c/mine/match/replay",
    "推栏角色指标": "https://m.pvp.xoyo.com/role/indicator",
}

# 默认服务器
DEFAULT_SERVER = "梦江南"

# 用户会话配置
SESSION_TIMEOUT = 45  # 用户会话超时时间（秒）

# GET请求缓存时间
SESSION_data = 720

# 名片缓存目录
IMAGE_CACHE_DIR = "mpimg"

# 命令正则表达式模式
REGEX_PATTERNS = {
    "烟花查询": r"^烟花 (?P<value1>[\S]+)$|^烟花 (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "奇遇查询": r"^奇遇 (?P<value1>[\S]+)$|^奇遇 (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "装备查询": r"^(?:属性|装分) (?P<value1>[\S]+)$|^(?:属性|装分) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "竞技查询": r"^(?:战绩|竞技) (?P<value1>[\S]+)$|^(?:战绩|竞技) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "副本查询": r"^(?:副本|秘境) (?P<value1>[\S]+)$|^(?:副本|秘境) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "骗子查询": r"^(?:骗子|查人) (?P<value1>[\S]+)$|^(?:骗子|查人) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "科举答题": r"^(?:答题|~！) (?P<value1>[\S]+)$|^(?:答题|~！) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "技改": r"^\s*技改\s*$",
    "更新": r"^\s*更新\s*$",
    "活动": r"^\s*活动\s*$",
    "百战查询": r"^\s*百战(?:\s+[\S]+(?:\s+[\S]+)?)?\s*$",
    "交易行查询": r"^(?:交易行|交易行) (?P<value1>[\S]+)$|^(?:交易行|交易行) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "名片查询": r"^名片 (?P<value1>[\S]+)$|^名片 (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "资历查询": r"^(?:资历|资历分布) (?P<value1>[\S]+)$|^(?:资历|资历分布) (?P<server>[\S]+) (?P<value2>[\S]+)$",
    "资历选择": r"^(\d+)$",  # 用于匹配用户回复的数字序号
    "竞技排名": r"^\s*(?:竞技排名(?:统计)?|竞排名)(?:\s+(?:拆分|橙武占比|缓存心法|(?i:debug)|tag=\S*))*\s*$",
    "提醒所有人": r"^\s*提醒所有人\s+(\d{12})\s+(.+?)\s*$",
    "提醒": r"^\s*提醒\s+(\d{12})\s+(.+?)\s*$",
    "提醒列表": r"^\s*提醒列表\s*$",
    "取消提醒": r"^\s*取消提醒\s*$",
}

# 定义一个包含文本的列表
texts = ["你是我的土豆，又土又逗。",
         "我喜欢你，像风走了八百里，不问归期。",
         "昔有朝歌夜弦之高楼，上有倾城倾国之舞袖。",
         "若有知音见采，不辞遍唱阳春。",
         "人间最美，不过鲸落，一念百草生，一念山河成。",
         "星星在天上，你在我心里。",
         "你笑起来真像好天气。",
         "我在找一匹马。什么马？你的微信号码。",
         "你是不是喜欢我找出这句话中重复的字。",
         "见什么世面，见见你就好啦。",
         "我给你备注为一行，因为干一行，爱一行。"
         ]


wanbaolou = 50  # 分钟检查价格
# 开服监控API配置
STATUS_check_time = 3   # 分钟检查一次服务器状态
NEWS_records_time = 30  # 分钟检查一次 新闻技改
calendar_time = 9  # 每天9点推送日常
mail = "用于qq掉线提醒 邮箱的tk"
STATUS_check_API = "https://www.jx3api.com/server/status/check"
# 新闻技改监控配置
NEWS_API_URL = "https://www.jx3api.com/news/records?limit=3"  # 新闻API地址
SKILL_records_URL = "https://www.jx3api.com/skill/rework"  # 技改API地址
calendar_URL = "https://www.jx3api.com/active/calendar"   #活动日常
jx3box_URL = "https://cms.jx3box.com/api/cms/config/banner?client=std&type=code"   #福利

# status_monitor 邮件通知配置（避免在代码中硬编码）
# 说明：默认沿用 mail 作为 SMTP 授权码/密码；如需更清晰可填写 STATUS_MONITOR_SMTP_PASSWORD
STATUS_MONITOR_SMTP_SERVER = "smtp.163.com"
STATUS_MONITOR_SMTP_PORT = 465
STATUS_MONITOR_SMTP_USERNAME = ""
STATUS_MONITOR_SMTP_PASSWORD = ""
STATUS_MONITOR_EMAIL_FROM = ""
STATUS_MONITOR_EMAIL_TO = ""
STATUS_MONITOR_EMAIL_SUBJECT = "服务器状态提醒"

# status_monitor 其他配置（可用环境变量覆盖）
# NapCat 地址（必须包含 http/https，容器内访问宿主机端口映射的常见写法）：
# - http://172.17.0.1:3001
STATUS_MONITOR_NAPCAT_ADDR = "http://172.17.0.1:3001"
STATUS_MONITOR_BASE_URL = ""
STATUS_MONITOR_ADMIN_USERNAME = ""
STATUS_MONITOR_ADMIN_PASSWORD = ""
STATUS_MONITOR_CLOUD_MUSIC_SERVER = ""

# 推栏（资历查询等）接口配置：沿用已提交版本的密钥和设备 ID，允许环境变量覆盖
TUILAN_SECRET_KEY = os.getenv("TUILAN_SECRET_KEY", 'MaYoaMQ3zpWJFWtN9mqJqKpHrkdFwLd9DDlFWk2NnVR1mChVRI6THVe6KsCnhpoR')
TUILAN_COOKIE = os.getenv("TUILAN_COOKIE", "")
TUILAN_DEVICE_ID = os.getenv("TUILAN_DEVICE_ID", 'lWrrIG5QpALPiSZ7txB//A==')
TUILAN_USER_AGENT = os.getenv("TUILAN_USER_AGENT", "okhttp/3.12.2")
# MongoDB 连接串
# 示例: mongodb://用户名:密码@地址:端口/数据库名?authSource=admin
# 可通过环境变量 MONGO_URI 或 runtime_config.json 覆盖
MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:password@127.0.0.1:27017/jx3bot?authSource=admin")

# Bot 内置 JJC 同步 worker 数量，0 表示关闭。
JJC_SYNC_WORKER_COUNT = _env_int("JJC_SYNC_WORKER_COUNT", 0)
JJC_SYNC_DISPATCHER_ENABLED = _env_int("JJC_SYNC_DISPATCHER_ENABLED", 1)
JJC_SYNC_DISPATCHER_IDLE_SLEEP = _env_int("JJC_SYNC_DISPATCHER_IDLE_SLEEP", 10)
JJC_SYNC_DISPATCHER_BATCH_SIZE = _env_int("JJC_SYNC_DISPATCHER_BATCH_SIZE", 20)
JJC_SYNC_DISPATCHER_TARGET_PER_WORKER = _env_int("JJC_SYNC_DISPATCHER_TARGET_PER_WORKER", 3)

# 排名心法缓存与后台刷新独立开关；部署中只启用一个后台消费者。
JJC_RANKING_KUNGFU_CACHE_ENABLED = _env_int("JJC_RANKING_KUNGFU_CACHE_ENABLED", 0)
JJC_KUNGFU_REFRESH_ENABLED = _env_int("JJC_KUNGFU_REFRESH_ENABLED", 0)
JJC_KUNGFU_REFRESH_SECONDS = _env_int("JJC_KUNGFU_REFRESH_SECONDS", 21600)
JJC_KUNGFU_MAX_STALE_SECONDS = _env_int("JJC_KUNGFU_MAX_STALE_SECONDS", 86400)
JJC_KUNGFU_TRACK_SECONDS = _env_int("JJC_KUNGFU_TRACK_SECONDS", 172800)

# 排名展示与本地最新胜场装备独立开关，默认关闭，变更后重启。
JJC_RANKING_DISPLAY_QUERY_ENABLED = int(os.getenv("JJC_RANKING_DISPLAY_QUERY_ENABLED", "0").strip() == "1")
JJC_RANKING_DISPLAY_OVERLAY_ENABLED = int(os.getenv("JJC_RANKING_DISPLAY_OVERLAY_ENABLED", "0").strip() == "1")
JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED = _local_equipment_flag(
    os.getenv("JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED", "0"), source="environment")
JJC_EQUIPMENT_MONGO_HOST = os.getenv("JJC_EQUIPMENT_MONGO_HOST", "")

# 最新列表刷新默认关闭；开启前配置有效推栏凭据与来源密钥。
JJC_LIVE_REFRESH_ENABLED = _env_int("JJC_LIVE_REFRESH_ENABLED", 0)
JJC_ROLE_IDENTITY_RECOVERY_ENABLED = _env_int("JJC_ROLE_IDENTITY_RECOVERY_ENABLED", 0)
JJC_LIVE_REFRESH_CAPACITY = _env_int("JJC_LIVE_REFRESH_CAPACITY", 100)
JJC_LIVE_REFRESH_COOLDOWN = _env_int("JJC_LIVE_REFRESH_COOLDOWN", 120)
JJC_LIVE_SOURCE_SECRET = os.getenv("JJC_LIVE_SOURCE_SECRET", "")
JJC_LIVE_TRUSTED_PROXIES = os.getenv("JJC_LIVE_TRUSTED_PROXIES", "")

# JJC 大橙武名称白名单：品质为5且名称命中才识别，无需维护小橙武名单。
# 新50级名称来源：https://news.17173.com/content/10262026/200343877.shtml
JJC_LEGENDARY_WEAPON_NAMES = [
    # 保留旧等级名称，兼容历史排名。
    "钗蝶语双", "七月嘉树", "万象金声", "幽微夜", "蜕骨", "伏魔悲音", "意真",
    "长安", "掠炎", "金乌回首", "栖贤韵", "瀚海引", "镇恶", "仙灵", "暴龙震江",
    "烛微刀", "碧血豪侠", "伏龙阳焰", "天下宏愿", "龙鲤", "银羽雪辉", "子不语",
    "碧岚幽炎", "逾潮声", "静雨", "折枝花满", "昭佛光", "烬灭",
    # 苍生铸世（新50级），2026-11-07核对，共33个名称。
    "烂柯", "白鹭忘言", "冽心", "游光引夜", "万骨回澜", "海印三昧",
    "赤瞳照业", "寒崖听籁", "寒锋凌日", "关山征魂", "霜塞南还", "中州北望",
    "承瞑愿", "觉梦吟", "烛钧", "回霆", "圣火渡", "望明", "青龙映夜",
    "边魂不寐", "孤照", "志介", "不复", "悬翎在握", "孤游万里", "绝翳曜明",
    "葛天八阕", "鉴魄证身", "天仙子", "一隙见瀚", "穹霄待游", "龙影蔷微", "天羊盏",
]

# JJC 赛季：起始日期使用竞技场开放日，不是资料片公测日。
# 2026-11-05 开赛，所在自然周为第 1 周，2026-11-09（周一）进入第 2 周。
CURRENT_SEASON = "苍生铸世"
CURRENT_SEASON_START = "2026-11-05"

# 心法拼音映射及分类
KUNGFU_META = {
    "lijing": {"name": "离经易道", "category": "healer"},
    "zixia": {"name": "紫霞功", "category": "dps"},
    "beiao": {"name": "北傲诀", "category": "dps"},
    "lingsu": {"name": "灵素", "category": "healer"},
    "huajian": {"name": "花间游", "category": "dps"},
    "fenshan": {"name": "分山劲", "category": "dps"},
    "taixu": {"name": "太虚剑意", "category": "dps"},
    "zhoutian": {"name": "周天功", "category": "dps"},
    "butian": {"name": "补天诀", "category": "healer"},
    "bingxin": {"name": "冰心诀", "category": "dps"},
    "xiangzhi": {"name": "相知", "category": "healer"},
    "jingyu": {"name": "惊羽诀", "category": "dps"},
    "fenying": {"name": "焚影圣诀", "category": "dps"},
    "wufang": {"name": "无方", "category": "dps"},
    "yunshang": {"name": "云裳心经", "category": "healer"},
    "taixuan": {"name": "太玄经", "category": "dps"},
    "dujing": {"name": "毒经", "category": "dps"},
    "gufeng": {"name": "孤锋诀", "category": "dps"},
    "tianluo": {"name": "天罗诡道", "category": "dps"},
    "yijin": {"name": "易筋经", "category": "dps"},
    "aoxue": {"name": "傲血战意", "category": "dps"},
    "mowen": {"name": "莫问", "category": "dps"},
    "xiaochen": {"name": "笑尘诀", "category": "dps"},
    "yinlong": {"name": "隐龙诀", "category": "dps"},
    "linghai": {"name": "凌海诀", "category": "dps"},
    "cangjian": {"name": "山居剑意", "category": "dps"},
    "shanhai": {"name": "山海心诀", "category": "dps"},
    "youluo": {"name": "幽罗引", "category": "dps"},
}

# 运行时可覆盖配置（由 /修改配置 写入）
RUNTIME_CONFIG_FILE = "runtime_config.json"
RUNTIME_CONFIG_KEYS = {
    "JJC_RANKING_DISPLAY_QUERY_ENABLED": int,
    "JJC_RANKING_DISPLAY_OVERLAY_ENABLED": int,
    "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED": int,
    "JJC_EQUIPMENT_MONGO_HOST": str,
    "JJC_RANKING_KUNGFU_CACHE_ENABLED": int,
    "JJC_KUNGFU_REFRESH_ENABLED": int,
    "JJC_KUNGFU_REFRESH_SECONDS": int,
    "JJC_KUNGFU_MAX_STALE_SECONDS": int,
    "JJC_KUNGFU_TRACK_SECONDS": int,
    "JJC_ROLE_IDENTITY_RECOVERY_ENABLED": int,
    "TOKEN": str,
    "TICKET": str,
    "SESSION_data": int,
    "calendar_time": int,
    "STATUS_check_time": int,
    "MONGO_URI": str,
    "JJC_SYNC_WORKER_COUNT": int,
    "JJC_SYNC_DISPATCHER_ENABLED": int,
    "JJC_SYNC_DISPATCHER_IDLE_SLEEP": int,
    "JJC_SYNC_DISPATCHER_BATCH_SIZE": int,
    "JJC_SYNC_DISPATCHER_TARGET_PER_WORKER": int,
}

if os.path.exists(RUNTIME_CONFIG_FILE):
    try:
        with open(RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as f:
            _runtime_config = json.load(f)
        for _key, _type in RUNTIME_CONFIG_KEYS.items():
            if _key in _runtime_config:
                try:
                    if _key == "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED":
                        globals()[_key] = _local_equipment_flag(_runtime_config[_key], source="runtime_config.json")
                    elif _key in ("JJC_RANKING_DISPLAY_QUERY_ENABLED", "JJC_RANKING_DISPLAY_OVERLAY_ENABLED"):
                        _flag = _runtime_config[_key]
                        globals()[_key] = int((type(_flag) is int and _flag == 1) or (type(_flag) is str and _flag.strip() == "1"))
                    else:
                        globals()[_key] = _type(_runtime_config[_key])
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
