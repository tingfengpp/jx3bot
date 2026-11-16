# JX3Bot

JX3Bot 是一个基于 NoneBot2 的剑网 3 QQ 机器人，运行在 OneBot V11 协议之上，同时暴露少量 HTTP API。仓库当前处于持续重构阶段，核心目标是把历史上集中在单插件中的逻辑，逐步收敛为更清晰的 `plugins / services / infra / storage / renderers` 分层。

## 当前能力

- JJC 对阵统计页面：`/jx3/jjc-matchups.html`；接口 `GET /api/jjc/matchups/meta` 与 `POST /api/jjc/matchups/search` 支持最长 14 天、双方 0～3 个心法和平均游戏分区间，并分页展示客观对局数据。点击 `match_id` 复用既有详情接口查看装备与奇穴。

- QQ 竞技排名支持 `竞技排名 tag=40`、`竞技排名统计 tag=40 缓存心法`、`竞排名 拆分 tag=40`；tag 为接口原始正整数，可与现有选项任意排序。指定时跳过时间标签接口和默认排行榜缓存，省略时沿用默认查询。历史排行榜的心法仍按现有统计逻辑获取。
- QQ 群消息命令处理
- 剑网 3 常见查询能力，如公告、竞技、名片、资历、交易行、百战、骗子查询等
- 万宝楼检索与订阅能力
- 定时推送与状态监控
- HTTP API:
  - 生产代理路径通常带 `/jx3bot` 前缀，例如 `https://qike.rickchen.cn/jx3bot/api/...`；仓库内路由定义仍按 `/api/...` 记录。
  - `GET /api/arena/recent?server=<服务器>&name=<角色>`
  - `GET /api/jjc/ranking-stats?action=list`
  - `GET /api/jjc/ranking-stats?action=list&page=1&page_size=20`
  - `GET /api/jjc/ranking-stats?action=list&page=1&page_size=100&with_meta=1`
  - `GET /api/jjc/ranking-stats?action=seasons`：列出所有有排名快照的赛季，并返回最新快照所属的默认赛季
  - `GET /api/jjc/ranking-stats?action=season-history&season=<赛季>`：返回指定赛季的全部排名快照元数据
  - `GET /api/jjc/ranking-stats?action=read&timestamp=<时间戳>`
  - `GET /api/jjc/ranking-stats/details?timestamp=<时间戳>&range=<范围>&lane=<healer|dps>&kungfu=<心法>`
  - `GET /api/jjc/ranking-stats/flat-members?timestamp=<时间戳>&range=<top_1000|top_200|top_100|top_50>`：按原始名次返回当前推栏排名列表，不按心法分组
  - `GET /api/jjc/ranking-stats/peak-score?timestamp=<时间戳>&score_type=<tuilan|game>&range=<top_1000|top_200|top_100|top_50>`：读取 14 天历史最高分排名；当前赛季按快照所属赛季开赛时间截断，响应包含 `aggregation_scope/season_id/season_start_time/window_start/window_end`。缓存缺失时按相同窗口只读聚合；未知历史边界不猜测重算。
  - `GET /api/jjc/ranking-stats/role-recent?server=<服务器>&name=<角色>`
  - `POST /api/jjc/ranking-stats/synced-role-refresh`：提交受保护的最新对局轻量刷新；GET 列表返回 `latest_matches` 与 `refresh` 状态
  - `GET /api/jjc/ranking-stats/synced-role?server=<服务器>&name=<角色>`：查询本地已收录角色身份和同步状态；`server` 为空时按角色名返回本地候选
  - `GET /api/jjc/ranking-stats/synced-role-matches?server=<服务器>&name=<角色>&page=1&page_size=20`：按角色 `global_id` 分页读取其参与过的本地已同步 3v3 对局
  - `POST /api/jjc/ranking-stats/synced-role-sync`：将已收录角色加入 JJC 同步队列
  - `GET /api/jjc/ranking-stats/match-detail?match_id=<对局ID>`
  - `GET /api/jjc/sync/status`：同步队列汇总，包含 `worker_running/background_running`
  - `GET /api/jjc/sync/queue?status=queued&mode=full&server=梦江南&name=角色&page=1&page_size=50`：角色队列分页，支持队列状态、同步类型、服务器/角色名搜索，包含 `has_more`
  - `GET /api/jjc/sync/workers`
  - `GET /api/jx3/servers`：读取当前区服列表，用于前端服务器下拉选择
- 静态页面:
  - 生产静态页面通常由站点映射到 `/jx3/<page>.html`，不要加 `/jx3bot` API 前缀。
  - `GET /jx3/jjc-ranking-stats.html`: JJC 心法分布、当前推栏排名列表、7 天推栏/游戏最高分列表
  - `GET /jx3/jjc-sync-queue.html`: JJC 同步队列与 worker 状态页
  - `GET /jx3/jjc-synced-matches.html`: 按角色查询其参与过的本地已同步 JJC 3v3 对局
  - 本地直接由 bot 暴露静态目录时，可按实际挂载访问 `/public/<page>.html`。

统一响应格式:

```json
{"status_code":0,"status_msg":"success","data":{}}
```

## 仓库结构

```text
bot.py                       NoneBot 启动入口
src/plugins/                 QQ 命令入口与插件注册
src/plugins/jx3bot_handlers/ 各类命令 handler
src/plugins/status_monitor/  状态监控与定时任务
src/plugins/wanbaolou/       万宝楼相关逻辑
src/services/jx3/            业务编排与缓存策略
src/infra/                   外部 API / HTTP / 截图适配
src/storage/                 存储适配
src/renderers/               模板渲染与图片生成
src/api/routers/             HTTP API 路由
templates/                   Jinja 模板
data/                        缓存、统计与运行数据
mpimg/                       名片等图片缓存
docs/                        补充架构与运行文档
```

更具体的边界说明见 `project-architecture.md`。

## 运行前提

- Python 3.9+
- 已安装项目依赖
- 准备好 `config.py`
- 准备好 MongoDB 连接（通过环境变量、部署配置或本地 `runtime_config.json` 中的 `MONGO_URI` 配置）
- NapCat 或其他 OneBot V11 实现已配置反向 WebSocket

默认运行时会同时启用消息插件和 HTTP API，监听地址由 `HOST`/`PORT` 控制。
`runtime_config.json` 存放本地凭证和运行时覆盖项，不应提交到 Git；可从 `runtime_config.example.json` 复制后填写本地值。

## 本地启动

1. 创建虚拟环境

```bash
python -m venv .venv
source .venv/bin/activate
```

2. 安装依赖

```bash
pip install -r requirements.txt
```

3. 启动机器人

```bash
python bot.py
```

如果要模拟容器入口并同时暴露 `mpimg/` 静态目录，可运行:

```bash
bash start.sh
```

## JJC 同步 Worker

JJC 对局同步默认只入队，不会在 bot 进程内自动处理队列。需要单独扩容或排障时仍可启动独立 worker:

```bash
python scripts/jjc_sync.py worker
```

入队默认按 full 处理；需要限制本次入队同步窗口时使用 `days` 或 `until`，例如 `/jjc同步开始 full days=14` 或 `python scripts/jjc_sync.py enqueue --days=14`。worker 只消费队列，不决定同步窗口。

单进程部署也可以启用 bot 内置 worker 和自动补队列 dispatcher。最小配置示例:

```json
{
  "JJC_SYNC_WORKER_COUNT": 1,
  "JJC_SYNC_DISPATCHER_ENABLED": 1
}
```

`JJC_SYNC_WORKER_COUNT=0` 表示关闭内置 worker；大于 0 时，bot 在 Mongo 初始化完成后创建对应数量的后台 worker。`JJC_SYNC_DISPATCHER_ENABLED=1` 时，bot 还会启动一个自动补队列 dispatcher，在 `queued` 深度不足时把到期角色按最近 14 天窗口补入队列；它不直接同步对局，也不会替代页面 full 或手工 full 入队。也可以通过环境变量或管理员命令 `/修改配置 ...` 设置。QQ 修改配置会退出当前进程，自动拉起依赖 Docker、systemd、supervisor 等外部守护。

内置 worker 使用稳定槽位名 `bot:{host}:{index}`，例如 `bot:my-host:0`。同一部署实例重启后会复用同一条 worker 心跳记录，不会因为 pid 或启动时间变化持续新增离线 worker。

## 存储说明

JJC 转服后的查询 ID 恢复由 `JJC_ROLE_IDENTITY_RECOVERY_ENABLED` 控制，默认 `0`，可通过环境变量或 `runtime_config.json` 配置，修改后需重启进程。开启后仅对原始 indicator 与独立 replay 数字身份一致的本地角色执行有界修复；不新增角色、不自动迁移服务器/名字。关闭开关不会撤销已修复资料或解除仓储的历史防回退保护。

存量维护脚本为 `scripts/recover_jjc_role_identities.py`，默认只读，显式 `apply` 需要固定清单哈希、私有备份目录、目标确认及 `--yes`。发布、回滚和未解决范围见 `docs/references/runbook.md` 与 `openspec/changes/jjc-role-identity-recovery/tasks.md`。

当前分支只记录现有文件型运行数据和存储边界。

## 手工验证

仓库当前主要依赖手工回归和在线接口验证。最小检查集:

```bash
nb plugin list --json
python test_tuilan_match_history.py
```

更完整的回归路径见 `docs/references/runbook.md`。

## Docker

容器部署说明见 `README-Docker.md`。如果改动了 `Dockerfile`、`docker-compose.yml` 或 `start.sh`，请同步更新该文档与 `docs/references/runbook.md`。

## 文档索引

- `AGENTS.md`: agent 的最小入口和协作规则
- `project-architecture.md`: 当前架构、依赖方向与模块职责
- `project-roadmap.md`: 中期目标、优先级与阶段方向
- `project-history.md`: 已完成的重要演进与文档调整记录
- `docs/design-docs/development-guide.md`: 新需求落层、改造路径与提交前检查
- `docs/references/runbook.md`: 启动、验证、故障排查、部署注意事项
- `docs/exec-plans/active/refactor-plan.md`: 现阶段重构边界和未完成项
- `docs/tasks/all-tasks.md`: 当前任务清单与任务文档索引
- `openspec/`: 中大型需求的唯一变更流程入口；`specs/` 保存生效规格，`changes/` 保存进行中变更，`changes/archive/` 保存完成档案


### 排名心法缓存与后台刷新

功能默认关闭。`JJC_RANKING_KUNGFU_CACHE_ENABLED=1` 使当前默认榜单优先批量读取 `role_identities.jjc_kungfu`；命中直接复用，缺失或不可用时按排名逐个等待直接查询，成功写回画像并计入本轮，真实失败才保留未知。显式 `tag=` 查询保持原路径，不登记为当前榜单。`JJC_KUNGFU_REFRESH_ENABLED=1` 独立启动 bot 内的串行消费者，也允许先保持统计开关关闭进行预热。Mongo初始化后启动、关闭时取消，不依赖QQ登录；不改变完整历史同步水位。

时效参数：`JJC_KUNGFU_REFRESH_SECONDS=21600`、`JJC_KUNGFU_MAX_STALE_SECONDS=86400`、`JJC_KUNGFU_TRACK_SECONDS=172800`。均支持环境变量和 `runtime_config.json`，必须为正整数且max_stale>=refresh，改动后重启。关闭时不校验可选新功能时效；启用后非法参数会阻止消费者启动，统计保留兼容路径并记录告警。周期为目标，积压时页面覆盖率与fresh/stale/unknown为实际快照值，装备不完整不展示橙武/紫武统计。

部署须只启用一个心法消费者；进程内启停幂等和Mongo领取不能替代跨进程全局限速。与排行榜心法查询一致，每次实际请求前异步随机等待1～3秒（含首次及跨角色查询），单请求10秒、角色轮次25秒（包含等待）；仅indicator/history。401/403冷却至少5分钟，429按Retry-After有界冷却；任务延时持久化，进程冷却重启后不保留。上线步骤为双开关关闭发布→仅一个消费者预热→检查覆盖/积压→启用缓存统计；本次生产发布与初始预热验收见 `openspec/changes/jjc-ranking-kungfu-async-refresh/deployment.md`。

更换赛季或算法前停止旧消费者，统一配置重启后登记迁移任务。回滚先停消费者，再关缓存开关，保留角色结果、任务generation及旧快照；不清队列、不降版本。出现 `QueueIntegrityError`（角色有值但队列丢失、版本回退、索引冲突）先停所有旧写入进程，核对备份及角色publish_version，人工恢复任务generation不低于角色版本后再开，不允许在线从零重建。

离线验证：`JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/jjc_kungfu_test .venv/bin/python -m unittest tests.test_jjc_kungfu_refresh_repo tests.test_jjc_kungfu_refresh_mongo tests.test_role_identity_ranking_batch`。仅使用隔离本地Mongo测试库，其他单测使用fake，不连接生产上游。手工回归：在隔离环境统计冷榜单→确认逐个直接查询、画像落库及本轮coverage→再次统计确认缓存复用且零心法外呼→更换fixture心法并到期刷新→确认旧快照不变→停止时取消在途HTTP→租约到期后恢复。详见 `openspec/changes/jjc-ranking-kungfu-async-refresh/test-plan.md`。


### 排行榜本地最新胜场装备

`JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=1` 后，按稳定角色 ID 读取本地当前赛季最新一场 3v3 胜场，统一提供装备、队友、橙武统计和紫武筛选。取最新胜场后装备缺失时保留未知，不向更旧胜场回退；心法分类保留原有来源。默认值为 0，支持环境变量与私有 runtime_config.json，修改后重启。`JJC_RANKING_DISPLAY_QUERY_ENABLED` 与 `JJC_RANKING_DISPLAY_OVERLAY_ENABLED` 是既有资料查询和展示覆盖开关，各自默认 0。

读服务使用专用 Mongo 连接和 singleflight，在原 1.5 秒 Mongo、2 秒 socket、8 秒构建、10 秒请求预算内完成整榜；超时或证据不一致时明确失败，不截断成员或丢弃异常证据。`JJC_EQUIPMENT_MONGO_HOST` 默认为空，仅在有明确部署需要时覆盖装备读连接的主机，凭据仍取部署配置。

开启前必须完成主表派生字段回填、索引校验与全榜只读预检。字段、索引及回滚限制见 `docs/design-docs/database-design.md` 和 `docs/references/runbook.md`。方案与验证记录见 `openspec/changes/jjc-ranking-local-latest-match/`；独立提交包验证见其中 `submission.md`。
