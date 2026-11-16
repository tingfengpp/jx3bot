# 运行与回归手册


## JJC 最新对局刷新

上线前确认有效签名密钥、设备 ID、`TICKET` 与 `JJC_LIVE_SOURCE_SECRET`，再设置 `JJC_LIVE_REFRESH_ENABLED=1`。推栏请求不再使用共享预算桶。

灰度观察请求失败、队列拒绝和成功检查年龄。异常先设置 `JJC_LIVE_REFRESH_ENABLED=0`；鉴权失败时更新有效票据。独立 Mongo 测试使用 `JJC_TEST_MONGO_URI`，禁止使用生产连接串。

离线回归覆盖已知角色提交、关闭开关读取旧摘要、后页失败保留和启动降级；前端需检查刷新与加载更多同时操作、切换角色后的迟到响应和十次轮询上限。可复现脚本与分组测试命令见 `openspec/changes/jjc-safe-live-refresh/test-plan.md`；所有浏览器接口使用 fixture，不作为真实官方响应验收。

更新时间：2026-06-03

本文面向维护者和 agent，记录当前仓库可执行的启动方式、验证命令和常见排查路径。

## 运行方式

### JJC 查询 ID 恢复与存量操作

`JJC_ROLE_IDENTITY_RECOVERY_ENABLED` 默认 `0`，支持环境变量及运行配置。公开 API/排名/同步新增恢复入口只有开启才探测，CLI 显式维护不依赖该开关。关闭后仓储保护仍生效，不会还原数据。首次部署保持关闭，单角色试点通过后仅修改这一配置键并第二次重启。

先运行 `python scripts/recover_jjc_role_identities.py --help`。`inventory --output <绝对私有目录>` 只读盘点所有本地身份及保留缓存/历史/排名的不同 locator；额外 replay 扫描须显式 `--replay-scan-limit N`，最大 1000000，结果报告扫描范围和截断可能。候选不是故障名单，无候选与未扫描来源均是未知，不是健康。操作目录须当前用户所有、0700、无符号链接。

`verify` 和 `apply` 都需 `--manifest`、`--manifest-sha256` 及同一 `--run-dir`；apply 重新实时验证，还需 `--backup-dir`、`--confirm-target host:port/database`、`--yes`。同一 run 的 manifest/目标/版本绑定不能替换。每次最多 20 角色、单并发、上游逻辑调用间隔至少 1 秒；实现保守将整个 run 压至 100 次（包含 verify、apply、复验/API预留），不能靠切命令重置。上限耗尽保存进度停止，不连续创建新 run 绕限制。

`--resume-from <原run目录>` 跳过该命令已处理终态；prepared 崩溃项先回读，不盲重放。显式 `--name/--server` 可受控复验；无证据、认证/429、冲突或预算不足如实留待处理。CLI 不写战绩缓存，保留关联缓存 before-image；成功 ID 更新先标 `applied_unverified`，由后续公开 API 写成功新缓存并验收，不能把旧缓存重新标新鲜。

`verify-applied --run-dir ...` 只读数据库，不调用会写缓存/自愈的公开 API；主动 API 验收前用同命令 `--reserve-api-calls 5` 为每个无法证明缓存命中的请求保守预留 5 次，同 run 共享额度。公开 API 与真实页面验收另列为允许写缓存/自愈的操作。API/页面通过者才在验收证据记 recovered；脚本不伪造该状态。

回滚默认 `rollback --run-dir ...` 只预览，显式 `--apply --yes --confirm-target ...` 才按 after-image CAS 还原本次字段/缺失状态，后续合法更新跳过。CLI 不写缓存，因此不删除后来合法缓存。代码、数据、启用配置分别回退；代码仅恢复本次 allowlist 并验证启动，配置只还原该键原值。旧代码可能重新允许历史回退，风险解除前同步保持暂停。

生产发布须先确认容器挂载/原暂停状态、无旧独立写者、磁盘与备份校验、生产基线加实际补丁组合测试；受控停止容器后替换文件再启动。验证 Running/StartedAt/文件哈希、日志、业务 API、真实桌面及手机宽度页面、未修角色自动自愈和健康对照；按原状态恢复同步后至少观察 15 分钟。串号、越权修改、回退、认证/429或重启循环立即停止，分别判断代码/数据回滚。完整门禁和当前执行证据位于 `openspec/changes/jjc-role-identity-recovery/`。

### 本地 Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

前提:

- `config.py` 已配置
- MongoDB 已启动且 `MONGO_URI` 配置正确
- OneBot 反向 WebSocket 已准备完成

### 模拟容器入口

```bash
bash start.sh
```

该脚本会:

1. 创建 `/app/mpimg`
2. 检查并补装少量运行依赖
3. 启动 `mpimg/` 的静态 HTTP 服务
4. 启动 `python bot.py`

### Docker Compose

```bash
docker compose up --build -d
docker compose logs -f
```

## 关键配置

### 必备文件

- `config.py`
- `runtime_config.json`：本地运行时覆盖文件，可从 `runtime_config.example.json` 复制创建；包含凭证和连接串时不得提交到 Git

### 必备服务

- MongoDB（连接串通过环境变量、部署配置或本地 `runtime_config.json` 中的 `MONGO_URI` 配置）

### 常用环境变量

- `HOST`
- `PORT`
- `TZ`
- `JJC_SYNC_WORKER_COUNT`：可选，bot 内置 JJC 同步 worker 数量，默认 `0` 不启动
- `JJC_SYNC_DISPATCHER_ENABLED`：可选，是否启用 bot 内置自动补队列 dispatcher，默认 `1`
- `JJC_SYNC_DISPATCHER_IDLE_SLEEP`：可选，dispatcher 空转 sleep 秒数，默认 `10`
- `JJC_SYNC_DISPATCHER_BATCH_SIZE`：可选，dispatcher 单轮最多补队列角色数，默认 `20`
- `JJC_SYNC_DISPATCHER_TARGET_PER_WORKER`：可选，每个活跃 worker 期望保有的 queued 深度，默认 `3`

## 最小验证集

这些命令不覆盖全部功能，但能快速发现明显损坏:

```bash
nb plugin list --json
python test_tuilan_match_history.py
```

说明:

- `nb plugin list --json` 用于确认插件仍能被 NoneBot 发现
- `test_tuilan_match_history.py` 依赖在线接口，失败时要先区分是代码回归还是外部服务异常

## 手工回归清单

### 1. 启动与初始化

- 启动后确认没有明显导入错误
- 观察缓存初始化日志，确认 `server_data.json`、竞技缓存、token 缓存、MongoDB 连接路径正常
- 若启用定时任务，观察 APScheduler 是否频繁 misfire

### 2. 基础命令

- `帮助`
- `更新`
- `活动`
- `技改`
- 管理员帮助：`/管理帮助`

预期:

- 能返回文本或渲染结果
- 无明显堆栈报错
- `/管理帮助` 只有 `config.py` 中 `ADMIN_QQ` 管理员可查看，返回 `/重启`、`/查看配置`、`/修改配置`、`/公告添加`、`/公告列表`、`/公告删除`、JJC 同步等管理命令用法

### 3. 交易行与万宝楼

- `交易行 <物品>`
- 相关万宝楼查询/订阅命令

预期:

- 外部接口成功时能返回价格信息
- 失败时返回可理解错误，而不是直接抛异常

### 4. 名片

- `名片 <角色>`
- `名片 <服务器> <角色>`

预期:

- 首次可生成或下载图片
- 再次查询应能复用缓存
- `mpimg/` 路径和图片访问 URL 正常

### 4.1 JX3API 查询回归

- `烟花 <角色>`
- `烟花 <服务器> <角色>`
- `奇遇 <角色>`
- `奇遇 <服务器> <角色>`
- `战绩 <服务器> <角色>`
- `名片 <服务器> <角色>`
- `百战 <服务器> <角色>`
- `百战`
- `日常`
- `答题 <题目关键词>`
- `骗子 <QQ号>`
- `开服`
- `属性 <角色>` / `装分 <角色>`
- `副本 <角色>` / `秘境 <角色>`
- 管理员执行 `/查看配置`

预期:

- 烟花、奇遇、战绩、名片、角色百战、日常、答题、骗子、开服命令应访问当前 JX3API OpenAPI 地址，不再请求旧 `/data/...` 404 地址
- `百战`（无角色参数）应提示官方接口暂未提供百战总览；`百战 <角色>` 与 `百战 <服务器> <角色>`应继续展示角色百战信息
- 输入不在本地区服目录中的旧服名/别名时，应提示输入当前正式区服名，不再请求已下线的主服映射接口
- `属性/装分` 返回装备查询接口暂不可用提示，不继续请求旧装备属性接口
- `副本/秘境` 返回副本查询接口暂不可用提示，不继续请求旧团队 CD 接口
- `/查看配置` 中 `token剩余` 无接口数据时显示 `未知`，不影响启动缓存初始化

### 5. 竞技相关

#### JJC 对阵统计

- 页面：`/jx3/jjc-matchups.html`；查询最长 14 天，支持双方 0～3 个心法、重复心法、空边和平均游戏分区间。
- 回填先执行 `python scripts/backfill_jjc_match_facts.py --batch-size 500`（dry-run），确认容量后才允许 `--apply --yes`；完成后执行 `--verify-only`，异常必须为 0 或逐条解释。
- 性能门禁：运行 `python scripts/check_jjc_matchup_performance.py`，7 天常见查询 ≤2 秒，14 天及双方空 ≤5 秒，确认无阻塞 SORT/facet 内存异常。
- 回滚为非破坏性：隐藏页面/路由并停止事实 projector，保留 `jjc_match_facts` 供核验。

- `竞技查询`
- `竞技排名`
- `竞技排名 拆分`
- `竞技排名 tag=40`（替换为接口可用 tag）
- `竞技排名统计 tag=40 缓存心法`
- `竞排名 拆分 tag=40 橙武占比 debug`
- `竞技排名 tag=abc`、`竞技排名 tag=0`、`竞技排名 tag=40 tag=41`：应提示用法且不调用查询

手动 tag 回归时核对日志：不请求时间标签接口，不读写默认排行榜缓存；图片周次及统计存档采用实际 tag。不带 tag 的命令继续使用默认缓存，未命中时取时间标签接口的 `data.defaultWeek`。tag 是接口原始值，历史排行榜的心法不保证为历史快照。

- 添加并默认排队指定角色：`/jjc同步添加 <服务器> <角色名> [priority=100] [queued=1] [global_role_id=...] [role_id=...] [zone=...]`
- 查看状态：`/jjc同步状态`
- 批量入队：`/jjc同步开始 [default|full] [limit=10] [days=14|until=YYYY-MM-DD]`
- 调整优先级：`/jjc同步优先级 <服务器> <角色名> <priority>`
- 暂停后续同步：`/jjc同步暂停 [原因]`
- 恢复同步：`/jjc同步恢复`
- 重置角色水位：`/jjc同步重置 <服务器> <角色名>`
- 启动独立常驻 worker：`python scripts/jjc_sync.py worker` 或 `python scripts/jjc_sync.py start --limit=10`；`start --limit` 只限制最多处理数量，队列暂空时仍会继续等待；worker 不决定同步窗口
- 启用 bot 内置 worker：设置 `JJC_SYNC_WORKER_COUNT=1` 后启动 `python bot.py`，或执行 `/修改配置 JJC_SYNC_WORKER_COUNT=1` 写入本地运行时配置并重启；`0` 表示关闭
- bot 内置 dispatcher 默认跟随 bot 一起启动；它只负责自动把到期角色补入 `queued`，不直接同步对局。dispatcher 路径固定使用最近 14 天窗口，不会替代页面 full 或手工 full 入队。
- 内置 worker 名称使用稳定槽位 `bot:{host}:{index}`，同一部署实例重启后会复用同一条 worker 心跳记录；升级前已经产生的旧 `bot:{host}:{启动时间}:{pid}:{index}` 离线记录可按需人工清理。
- 批量入队脚本：`python scripts/jjc_sync.py enqueue --limit=10`，本次只同步最近 14 天可用 `python scripts/jjc_sync.py enqueue --limit=10 --days=14`
- 页面路径与接口路径分开维护：生产静态页面使用 `/jx3/<page>.html`，例如 `https://qike.rickchen.cn/jx3/jjc-sync-queue.html`；API 使用 `/jx3bot/api/...`，例如 `https://qike.rickchen.cn/jx3bot/api/jjc/sync/status`。HTML 页面链接不要加 `/jx3bot` 前缀。
- 队列页面：生产环境访问 `https://qike.rickchen.cn/jx3/jjc-sync-queue.html`；本地若直接由 bot 暴露静态目录，可按实际挂载访问 `http://<bot-host>:<port>/public/jjc-sync-queue.html`。页面支持按服务器、角色名搜索，状态以中文展示；页面依赖同源 `/jx3bot/api/jjc/sync/...` 或本地 `/api/jjc/sync/...` 接口，不能直接用本地文件方式打开。
- 已同步对局页面：生产环境访问 `https://qike.rickchen.cn/jx3/jjc-synced-matches.html`；输入服务器和角色名后，页面先查询 `role_identities`，再只按该身份的 `global_id` 匹配 `jjc_match_detail` 中 6 名参赛玩家的 `global_id`，展示该角色参与过的本地已同步 3v3 对局。页面不使用服务器名、昵称、`global_role_id` 或 `role_id` 兜底匹配对局，避免转服、改名造成串号。页面依赖同源 `/jx3bot/api/jjc/ranking-stats/synced-role*`、`role-indicator`、`match-detail` 接口，不能直接用本地文件方式打开。

预期:

- service 能返回结构化数据
- renderer 能生成图片
- JJC 排名统计快照写入 MongoDB `jjc_ranking_stat_summaries` 与 `jjc_ranking_stat_details`；HTTP API 仅读 Mongo，未命中时返回 `not_found`。
- 历史 `data/jjc_ranking_stats/` 文件仅作为一次性迁移输入，不再作为运行时 fallback。
- JJC 同步命令只有 `config.py` 中 `ADMIN_QQ` 管理员可执行
- `/jjc同步开始` 只负责把角色放入 `queued` 队列；即使全局暂停也允许继续入队，实际处理由独立 worker 或 bot 内置 worker 在恢复后领取。
- 多个 worker 可以同时运行；每个 worker 一次只处理一个角色，通过 Mongo `lease_owner/lease_expires_at` 避免重复领取。
- 内置 worker 与独立 worker 可以共存，总 worker 数等于所有 bot 实例配置数量加独立 worker 数量；不要无意超配。
- 同一 host 上如果同时运行多个 bot 实例并都启用内置 worker，稳定槽位名可能冲突；当前线上按单 bot 实例部署验证，多实例需要先用不同 hostname 或新增实例名配置区分。
- 状态命令和队列页面只把 5 分钟内有心跳的 `starting/running/idle/syncing/paused` worker 视为活跃 worker；进程被 kill 后旧心跳不会长期显示为运行中。
- worker、脚本或容器中断后，已领取的 `syncing/detail_syncing` 记录会在租约过期后由新 worker 恢复，角色水位只在完整成功后推进，重复扫描依赖 match/detail 幂等跳过。
- 修改推栏 ticket 的推荐流程：先 `/jjc同步暂停 更换ticket`，等待 `/jjc同步状态` 显示无 `syncing` 或 worker 已 paused，重启 bot/worker 加载新配置，再 `/jjc同步恢复`；暂停期间可以继续添加角色或批量入队，worker 暂不领取，但 tick 仍会恢复过期租约。
- `/修改配置 JJC_SYNC_WORKER_COUNT=<数量>` 会调用当前重启流程退出进程，自动拉起依赖 Docker restart、systemd、supervisor 等外部守护；没有外部守护时进程只会退出。
- 当前 `/修改配置`、`/重启` 使用 `os._exit(0)`，不会保证 NoneBot shutdown 回调执行。旧内置 worker 可能不立刻写入 `stopped/cancelled` 心跳，状态会依赖约 5 分钟在线判定 TTL 自然失效，角色租约按现有过期恢复机制处理。
- 推栏返回明确 ticket 过期、无权限、鉴权失败时，worker 会自动全局暂停，并把当前角色释放回队列且不增加角色 `fail_count`。
- JJC 最终身份主键为 replay 数字 ID：`global_id:{global_id}`；`global_id` 来自 `/3c/mine/match/replay` 的 `players[].global_role_id`，不要与 SK01 `global_role_id` 混用。
- `global_role_id` 专指 `/role/indicator` 返回的 `SK01-...`，用于请求 `match/history`；新队列入队不要求 SK01，worker 领取 `identity_id` 后会用 `role_identities` 中的区服、`game_role_id`、服务器先刷新 indicator。
- 同步详情应写入现有 `jjc_match_detail`，并通过统一对局身份投影把详情玩家写入 `role_identities` 与 `jjc_sync_identity_queue`；投影阶段只使用详情/replay 已知字段，不因缺少 SK01 跳过入队。
- 旧 `jjc_sync_role_queue` 迁移到 `jjc_sync_identity_queue` 前，先暂停 worker 并确认无活跃 `syncing`；使用 `python scripts/migrate_jjc_sync_identity_queue.py forward --dry-run` 预检查，确认 `unresolvable/failed` 后使用 `forward --execute` 正式迁移，再恢复 worker。分批迁移时配合 `--limit` 和上批最后一个源 `_id` 的 `--after-id` 推进游标。代码版本回滚前使用同脚本 `reverse-sync --dry-run` 和 `reverse-sync --execute` 把新队列水位尽力写回旧队列。
- 单条详情临时失败不会中断当前角色同步，`/jjc同步开始` 输出中的 `详情失败` 表示对局详情已写入失败状态并等待 `detail_retry_after` 后重试
- 若 Mongo 领取对局详情租约失败，worker 会释放当前角色回 `queued`，不推进角色水位，等待后续 tick 重试，避免跳过该角色后续对局。
- 推栏返回 `code=-1`、`msg=no data found`、`data=null` 时，对局写入 `detail_unavailable` 终态；`/jjc同步开始` 输出中的 `详情不可用` 表示后续不会重复请求该对局详情
- 若状态中最近错误出现 `role_identity_not_found`，优先检查目标角色是否已能通过最近对局 replay 补到 `global_id`；仅缺 SK01 `global_role_id` 时再用带 `global_role_id=...` 的添加命令补充 history 请求字段。
- 若服务中断后状态长期存在 `syncing` 或 `detail_syncing`，再次执行 `/jjc同步开始` 或启动 worker 会先恢复过期租约；角色恢复为 `queued`，详情恢复为可重试状态
- 已同步对局页面输入不存在于 `role_identities` 的角色时，应显示未收录身份空态；已收录但缺少 `global_id`，或详情玩家尚未回填 `global_id` 时，对局列表为空。
- 已同步对局列表只显示 `config.CURRENT_SEASON` 对应的 `jjc_match_participants.season_id`。新赛季确定后先同步更新 `CURRENT_SEASON` 与 `CURRENT_SEASON_START`，重启 bot 使配置生效；新赛季暂无对局时列表为空，不应显示旧赛季记录。
- 排名页角色弹窗的 `role-recent` 实时查询、缓存命中与排名预热也只展示 `CURRENT_SEASON_START` 北京时间零点及之后的记录；缺少对局时间的记录不展示。换季后无标记或赛季名称/起点不符的近期缓存自动视为未命中，不需要清空 Mongo；第一轮请求可能重新访问推栏。
- 赛季切换回归：用仅有上赛季对局的角色打开排名弹窗及对局查询页，应显示空列表；混合历史只出现本赛季记录，到达上一赛季后不再显示加载更多。确认当天零点记录保留、零点前一秒记录隐藏。心法对阵选择跨赛季的 14 天范围时，列表、总数与胜率只统计本赛季部分；仅查询上赛季时结果为空。历史排名快照的赛季选择器继续保留。
- 首次启用或切换赛季后，按顺序回填玩家投影；默认 dry-run 不写库，确认输出后才执行 apply：
  ```bash
  python scripts/backfill_jjc_match_participant_season.py --dry-run
  python scripts/backfill_jjc_match_participant_season.py --apply --yes
  python scripts/backfill_jjc_match_participant_season.py --verify-only
  ```
- 已同步对局页面点击“加入同步队列”后，应调用 `POST /jx3bot/api/jjc/ranking-stats/synced-role-sync`（本地无代理时为 `/api/jjc/ranking-stats/synced-role-sync`），POST body 为 `{"server":"...","name":"..."}`；返回后先展示入队接口返回的 `sync_status`，再刷新同步状态和本地对局列表；列表文案使用“同步状态/已同步对局”，不使用对局缓存刷新语义。
- 已同步对局页面点击单场对局时，应调用 `GET /api/jjc/ranking-stats/match-detail?match_id=<对局ID>` 打开详情弹窗，焦点角色在队伍列表中高亮。
- JJC 14 天历史最高分排名每天 09:00 独立生成，读取最近两个 `jjc_ranking_stat_summaries.timestamp` 作为锚点，从 `jjc_match_participants` 中统计各锚点往前 14 天内所有本地已同步 3v3 对局参与者；统计对象不是排行榜成员，因此可以覆盖洗车后掉出榜单但历史对局已同步的玩家。
- 最高分统计使用 `jjc_match_participants.match_time` 作为对局发生时间；该字段来自推栏接口返回的 `match_time/start_time`，不要用 `cached_at/detail_saved_at/updated_at` 排查统计窗口。
- 最高分结果写入 `jjc_peak_score_rankings`，同一 `anchor_timestamp + score_type + version` 幂等唯一。`score_type=tuilan` 使用推栏分数 `mmr`，`score_type=game` 使用游戏分数 `total_score`，缺失时兜底 `score`；每条排名项记录最高分对应的 `match_id` 和 `match_time`。
- 赛季隔离口径：最高分实际起点为 `max(anchor_timestamp-14天, 快照所属赛季开赛时间)`。定时和即时聚合、分布与详情统一使用该窗口；不得通过删除旧 items 代替从原始参与者重新排名。新结果必须有 `aggregation_scope=season_bounded_v1`、`season_id` 和 `season_start_time`，当前赛季旧 done 结果缺少元数据时失效。历史 legacy 结果继续可读，未知历史边界不重新聚合。
- 定向重建：先备份本赛季最高分文档和待部署源码；运行 `python scripts/rebuild_jjc_peak_score_rankings.py --mode dry-run --season 苍生铸世` 查看两类分数的旧/新人数、开赛前来源及成员变化，确认窗口后用 `--mode apply` 重建，再用 `--mode verify` 检查每项时间、连续排名、角色唯一性和元数据。可重复传入 `--anchor <timestamp>` 先处理最新/结算锚点。默认模式只读，dry-run/verify 不初始化索引，不改原始对局、参与者、队列或历史赛季；任一失败退出非零。正式执行前重枚举赛季锚点，完成后再枚举以补齐期间新增项。
- 发布应更新最高分 service、repo、router 和两个排名 HTML 入口，重启 bot 使旧统计进程退出。不要用本地配置覆盖生产；候选先在生产 Python 3.9 下验证。异常恢复源码/静态页并重启，正确的新结果优先保留，仅确认数据写坏时才按备份恢复目标文档；回退旧代码会再次暴露跨季问题。
- 新增投影字段上线后，历史对局可通过参与者投影回填脚本补齐分数字段：
  ```bash
  # dry-run，小批量观察
  python scripts/backfill_jjc_match_participants.py --limit 100

  # 小批量写入验证
  python scripts/backfill_jjc_match_participants.py --apply --yes --limit 100

  # 全量写入，必要时配合 --resume-after-match-id 断点续跑
  python scripts/backfill_jjc_match_participants.py --apply --yes --batch-size 200
  ```

离线自动验证：

```bash
python -m unittest tests.test_jjc_match_data_sync_handler tests.test_jjc_match_data_sync tests.test_jjc_sync_repo tests.test_jjc_sync_worker_queue tests.test_jjc_sync_router tests.test_migrate_jjc_sync_identity_queue
python -m unittest tests.test_jjc_match_detail_snapshots tests.test_jjc_snapshot_repo tests.test_jjc_match_detail_hydration tests.test_scripts_jjc_snapshot
python -m py_compile src/services/jx3/jjc_match_data_sync.py src/storage/mongo_repos/jjc_sync_repo.py src/plugins/jx3bot_handlers/jjc_match_data_sync.py src/api/routers/jjc_sync.py scripts/jjc_sync.py src/infra/mongo.py
```

在线手工回归需要真实 QQ/推栏环境：先启动一个 `python scripts/jjc_sync.py worker`，再 `/jjc同步添加 <服务器> <角色名>`、`/jjc同步状态`、打开 `http://<bot-host>:<port>/public/jjc-sync-queue.html`，确认角色进入 queued 后被 worker 领取、对局详情写入、单条详情失败时仍继续处理后续对局和后续页。

JJC replay 角色 ID / global_id 回填：

```bash
# 默认 dry-run，只处理最近 20 场 detail_saved 对局
python scripts/backfill_jjc_role_id_from_match_replay.py --limit 20 --dry-run

# 按当前排序处理第 21 到 40 条，1-based 闭区间
python scripts/backfill_jjc_role_id_from_match_replay.py --start 21 --end 40 --dry-run

# 指定单场 dry-run
python scripts/backfill_jjc_role_id_from_match_replay.py --match-id <对局ID> --dry-run

# 确认写库
python scripts/backfill_jjc_role_id_from_match_replay.py --limit 20 --apply --yes
```

预期:

- 脚本请求推栏 `/3c/mine/match/replay`，从 `players[].role_id` 补齐 `role_id`，从 `players[].global_role_id` 补齐 replay 数字 `global_id`。
- 脚本默认每场对局后随机 sleep 1 到 3 秒；可用 `--sleep-min` / `--sleep-max` 调整，或用 `--sleep` 指定固定间隔。
- 当目标文档已有 `zone` 时，脚本会继续请求 `/role/indicator`，用 `role_id + zone + server` 补齐 `SK01-... global_role_id` 和 `person_id`。
- 脚本按 `role_info_observed_match_time` 判断是否更新：旧对局只补缺失字段，不覆盖已有完整字段。
- 若已有 `role_id` 与 replay 不一致、已有 `global_id` 与 replay 数字 ID 不一致，或已有 SK01 `global_role_id` 与 indicator 返回不一致，脚本跳过并记录 conflict。

### 6. 资历 / 百战 / 骗子查询

- `资历 <角色>`
- `百战 <角色>`
- `骗子 <角色或关键字>`

预期:

- 参数解析正常
- 外部接口失败时有降级提示

### 7. HTTP API

请求:

```bash
curl "http://127.0.0.1:5288/api/arena/recent?server=梦江南&name=示例角色"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats?action=list"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats?action=list&page=1&page_size=20"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats?action=list&page=1&page_size=100&with_meta=1"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats?action=read&timestamp=<时间戳>"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats/details?timestamp=<时间戳>&range=top_50&lane=healer&kungfu=云裳心经"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats/role-recent?server=梦江南&name=示例角色"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats/role-indicator?server=梦江南&name=示例角色&force_refresh=true"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats/synced-role?server=梦江南&name=示例角色"
curl "http://127.0.0.1:5288/api/jjc/ranking-stats/synced-role-matches?server=梦江南&name=示例角色&page=1&page_size=20"
curl -X POST "http://127.0.0.1:5288/api/jjc/ranking-stats/synced-role-sync" -H "Content-Type: application/json" -d '{"server":"梦江南","name":"示例角色"}'
curl "http://127.0.0.1:5288/api/jjc/ranking-stats/match-detail?match_id=<对局ID>"
```

生产代理路径示例：

```bash
curl "https://qike.rickchen.cn/jx3bot/api/jjc/ranking-stats/synced-role?server=梦江南&name=示例角色"
curl -X POST "https://qike.rickchen.cn/jx3bot/api/jjc/ranking-stats/synced-role-sync" -H "Content-Type: application/json" -d '{"server":"梦江南","name":"示例角色"}'
```

预期:

- 返回统一结构
- 参数非法时返回错误响应，而不是 500
- `action=list&with_meta=1` 返回带赛季、周次、结算状态的分页快照列表；旧 `action=list` 仍返回时间戳数组
- `action=read` 首屏摘要不再返回全量 `members`
- `details` 接口可按需返回单个心法成员明细
- 统计页点击角色时可按需返回最近 3v3 胜负和最近对局列表
- 统计页角色指标默认使用 1 天内 `jjc_role_indicator` 缓存；页面刷新按钮或 `force_refresh=true` 会绕过缓存并写回最新结果
- 已同步对局接口只查询本地已收录身份和本地已保存对局详情；对局列表只按 `identity.global_id == detail.team*.players_info.global_id` 匹配，不使用昵称/服务器兜底，不创建新身份
- 已同步对局页面的“加入同步队列”按钮应只对已命中身份展示，并将角色加入 JJC 同步队列后刷新页面同步状态
- 统计页点击对局时可按需返回单局详情
- 统计页历史选择器可按赛季、周次、全部/结算/日常快照切换；切换后详情懒加载仍使用当前快照 timestamp
- 不可用对局详情返回 `unavailable=true`、`code=-1`、`message=no data found`、`detail=null`，统计页应显示“该对局查询不到数据”
- 每日/手动 JJC 排名统计后，若统计过程已请求到 `role/indicator` 或最近胜场 `match/detail`，Mongo 中应可看到对应 `jjc_role_indicator`、`jjc_match_detail` 预热缓存；页面再次查看对应角色或对局时应命中缓存

## 故障排查

### 插件没有加载

检查:

- `pyproject.toml` 中 `plugin_dirs = ["src/plugins"]`
- 插件目录是否存在导入错误
- `nb plugin list --json` 输出是否异常

### API 能启动但消息命令失效

检查:

- OneBot 反向 WebSocket 是否连通
- NapCat 配置是否仍指向当前 `HOST` / `PORT`
- NoneBot 启动日志是否显示适配器注册成功

### 图片渲染失败

检查:

- `templates/` 是否缺文件
- `mpimg/` 是否可写
- 截图依赖与浏览器环境是否完整
- 相关字体或静态资源路径是否仍然存在

### 8. JJC 对局详情快照缓存

`jjc_match_detail` 只保存对局详情主体和玩家节点中的 `equipment_snapshot_hash` / `talent_snapshot_hash`。完整 `armors` / `talents` 分别保存在 `jjc_equipment_snapshot` / `jjc_talent_snapshot`，读取时由 `JjcInspectRepo` 按 hash 拼回 API 响应。

历史详情缓存不迁移；临时清理和核验脚本已删除。需要验证时优先通过角色近期/详情下钻接口触发读取，确认返回结构能由 `JjcInspectRepo` 拼回完整装备和奇穴。

### 存储或缓存表现异常

检查:

- MongoDB 连接是否正常（`GET /api/mongo/health` 返回 `connected: true`）
- 相关集合索引是否已创建
- `src/storage/` 的装配逻辑是否仍与代码实现一致
- 最近是否改动了缓存路径、运行目录或部署挂载

## 文档联动规则

以下变更必须同步更新本文:

- 启动命令变更
- 环境变量变更
- 新增或删除 HTTP API
- 手工回归路径变更
- 存储后端切换方式变更


### 排名心法缓存与后台刷新

功能默认关闭。`JJC_RANKING_KUNGFU_CACHE_ENABLED=1` 使当前默认榜单优先批量读取 `role_identities.jjc_kungfu`；命中直接复用，缺失或不可用时按排名逐个等待直接查询，成功写回画像并计入本轮，真实失败才保留未知。显式 `tag=` 查询保持原路径，不登记为当前榜单。`JJC_KUNGFU_REFRESH_ENABLED=1` 独立启动 bot 内的串行消费者，也允许先保持统计开关关闭进行预热。Mongo初始化后启动、关闭时取消，不依赖QQ登录；不改变完整历史同步水位。

时效参数：`JJC_KUNGFU_REFRESH_SECONDS=21600`、`JJC_KUNGFU_MAX_STALE_SECONDS=86400`、`JJC_KUNGFU_TRACK_SECONDS=172800`。均支持环境变量和 `runtime_config.json`，必须为正整数且max_stale>=refresh，改动后重启。关闭时不校验可选新功能时效；启用后非法参数会阻止消费者启动，统计保留兼容路径并记录告警。周期为目标，积压时页面覆盖率与fresh/stale/unknown为实际快照值，装备不完整不展示橙武/紫武统计。

部署须只启用一个心法消费者；进程内启停幂等和Mongo领取不能替代跨进程全局限速。请求开始间隔至少2秒、单请求10秒、角色轮次25秒；仅indicator/history。401/403冷却至少5分钟，429按Retry-After有界冷却；任务延时持久化，进程冷却重启后不保留。上线步骤为双开关关闭发布→仅一个消费者预热→检查覆盖/积压→启用缓存统计；本次生产发布与初始预热验收见 `openspec/changes/jjc-ranking-kungfu-async-refresh/deployment.md`。

更换赛季或算法前停止旧消费者，统一配置重启后登记迁移任务。回滚先停消费者，再关缓存开关，保留角色结果、任务generation及旧快照；不清队列、不降版本。出现 `QueueIntegrityError`（角色有值但队列丢失、版本回退、索引冲突）先停所有旧写入进程，核对备份及角色publish_version，人工恢复任务generation不低于角色版本后再开，不允许在线从零重建。

离线验证：`JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/jjc_kungfu_test .venv/bin/python -m unittest tests.test_jjc_kungfu_refresh_repo tests.test_jjc_kungfu_refresh_mongo tests.test_role_identity_ranking_batch`。仅使用隔离本地Mongo测试库，其他单测使用fake，不连接生产上游。手工回归：在隔离环境统计冷榜单→确认逐个直接查询、画像落库及本轮coverage→再次统计确认缓存复用且零心法外呼→更换fixture心法并到期刷新→确认旧快照不变→停止时取消在途HTTP→租约到期后恢复。详见 `openspec/changes/jjc-ranking-kungfu-async-refresh/test-plan.md`。

### 心法缓存缺失直接查询（2026-11-06修订）

当前统计开关开启后，缓存命中沿用角色画像，缺失逐个等待直接查询并计入本次统计。后台关闭不阻止该查询；真实鉴权/限流冷却和数据失败仍可留下未知。直接查询先预留角色发布代次，不依赖后台轮转，队列确认异常按租约恢复。可在隔离榜单验证冷查询成功写回role_identities.jjc_kungfu，第二次查询零心法外呼；无安全身份不强行合并。旧“冷热均不外呼”验收条件已被替代。装备资料不完整时紫武列表仍隐藏，不据此判断心法全空。

## 排名统计成员重新从队首同步

实现与发布记录见 `openspec/changes/archive/2026-11-07-jjc-ranking-force-sync-front/`，2026-11-07 已发布验收。只有实际执行排名统计会触发；查看已保存网页不会触发。每轮有效角色（包括心法未知）在 cooldown/exhausted/failed/pending 时立即入队，queued 时更新到队首，syncing 时等待本轮释放后合并补排。最新排名批次先于普通高 priority 任务，不抢占正在执行的 worker，不绕过全局暂停或 disabled。查询历史仍使用最近14天及赛季边界，复用已有详情。

离线回归：`.venv/bin/python -m unittest tests.test_jjc_ranking_force_sync tests.test_jjc_sync_worker_queue tests.test_jjc_match_data_sync tests.test_jjc_ranking_stats_repo tests.test_jjc_ranking_command_tag`。真实并发验证须显式 `JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/` 运行 `tests.test_jjc_ranking_force_sync_mongo`；测试仅删除自己创建的随机 `jjc_sync_front_test_` 库，不得指向生产。

后续再次升级时：先停止所有 bot 及独立同步 worker（含统计生产者），统一代码与配置，初始化两队列新增 `idx_status_ranking_front`，确认排序定义与 database-design.md 一致，再恢复进程。两轮实际统计观察同 cache_time 的不同 queue_batch_id；验证冷却/失败立即 queued、最新批次排位、syncing 当前租约不变、释放后只补排一次；全局暂停时确认没有上游调用。页面筛选 queued 后应与 worker 领取顺序一致。

回滚准备须在停止所有相关进程后执行：逐条读取队列，有 ranking_pending 的记录将其待执行窗口/赛季/source/batch 等字段合并到当前记录，置 queued，清 next_sync_after 和租约；将 queue_front_order、queue_front_rank、ranking_pending 统一清为 null，保留 ranking_request_order、比赛、水位及失败审计。先核对 pending 数与转换成功数一致、剩余 pending 为零，再切回旧版本恢复进程。没有 pending 的运行中任务沿用既有租约过期恢复。保留新增索引，不删除身份、队列、对局或旧索引。该转换已在隔离真实Mongo两队列演练通过，未执行生产回滚。

本次私有发布目录为 `/mnt/sata6-1/jx3bot-releases/ranking-front-20261107-e1ee55ef4405/`。回滚须停止全部bot、独立worker、dispatcher、统计生产者和其他队列写者；临时容器只执行转换器，使用核对过的同镜像、网络、原环境及只读原配置，工作目录/PYTHONPATH为/app，核对目标Mongo配置指纹。preimage使用canonical Extended JSON逐条flush/fsync，写入宿主持久化可写挂载，限制权限并使用唯一新文件名。disabled不复活，保留较高人工priority。两队列pending/front残留为零、转换数与备份条数一致、退出码为0后才恢复旧allowlist源码并启动；异常/CAS失配保持停止，保留部分备份，重试换新文件名。脚本、日志、转换计数和哈希保留；就绪/API/QQ/worker检查后，仅撤销本次暂停，保留期间其他原因的暂停。详见归档change的deployment.md。


### 排行榜本地最新胜场装备

`JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=1` 后，按稳定角色 ID 读取本地当前赛季最新一场 3v3 胜场，统一提供装备、队友、橙武统计和紫武筛选。取最新胜场后装备缺失时保留未知，不向更旧胜场回退；心法分类保留原有来源。默认值为 0，支持环境变量与私有 runtime_config.json，修改后重启。`JJC_RANKING_DISPLAY_QUERY_ENABLED` 与 `JJC_RANKING_DISPLAY_OVERLAY_ENABLED` 是既有资料查询和展示覆盖开关，各自默认 0。

读服务使用专用 Mongo 连接和 singleflight，在原 1.5 秒 Mongo、2 秒 socket、8 秒构建、10 秒请求预算内完成整榜；超时或证据不一致时明确失败，不截断成员或丢弃异常证据。`JJC_EQUIPMENT_MONGO_HOST` 默认为空，仅在有明确部署需要时覆盖装备读连接的主机，凭据仍取部署配置。

开启前必须完成主表派生字段回填、索引校验与全榜只读预检。字段、索引及回滚限制见 `docs/design-docs/database-design.md` 和 `docs/references/runbook.md`。方案与验证记录见 `openspec/changes/jjc-ranking-local-latest-match/`；独立提交包验证见其中 `submission.md`。
