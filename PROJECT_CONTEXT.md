# PROJECT_CONTEXT.md

本文件是仓库级项目上下文单一事实源。`AGENTS.md` 与 `CLAUDE.md` 不再重复维护项目说明；涉及仓库事实、架构、约束、验证、文档联动时，统一以本文件为准。

## 你在维护什么

- 这是一个基于 NoneBot2 + OneBot V11 的剑网 3 QQ 机器人，同时暴露 FastAPI HTTP API。
- 入口是 `bot.py`，负责启动 NoneBot、注册 OneBot V11 适配器，并挂载 HTTP 路由。
- 插件从 `src/plugins/` 自动加载。
- 当前代码已经从“单文件插件”演进为分层结构，新增功能应优先落入现有层次，而不是继续堆进 `src/plugins/jx3bot.py`。

## 开始之前先读

1. `README.md`
2. `project-architecture.md`
3. `project-roadmap.md`
4. `project-history.md`
5. `docs/design-docs/index.md`
   - 数据库相关改动必须补读 `docs/design-docs/database-design.md`
6. `docs/references/index.md`
7. `docs/PLANS.md`
8. `openspec/specs/` 与相关 active change
9. `docs/exec-plans/index.md`
10. `docs/tasks/all-tasks.md`

如果任务只涉及部署，再补读 `README-Docker.md`。

## 常用命令

```bash
# 启动
python bot.py

# 容器入口（启动 mpimg 静态服务 + bot）
bash start.sh

# Docker
docker compose up --build -d

# 验证插件加载
nb plugin list --json

# 手工测试推栏接口
python test_tuilan_match_history.py

# 类型注解兼容性检查（Python 3.9）
python -m py_compile src/api/routers/<router>.py
python -m py_compile <相关 service/storage 文件>
```

## 当前真实结构

- `bot.py`: NoneBot 启动入口，注册 OneBot 适配器与 HTTP 路由。
- `src/plugins/`: 消息入口层，只做命令匹配、参数提取、调用 service、发送消息。
- `src/plugins/jx3bot_handlers/`: 各命令 handler 注册与薄逻辑。
- `src/services/jx3/`: 业务编排层，负责查询流程、缓存策略、仓储调用。
- `src/services/jx3/singletons.py`: 共享对象装配，只做实例组装（Jinja env、repo、service 实例），不追加新的业务逻辑。
- `src/infra/`: 外部系统适配层，如 HTTP、截图、jx3api 请求封装。
- `src/renderers/`: 模板渲染、图片生成、消息输出辅助。
- `src/storage/`: 存储适配与工厂。
- `src/api/routers/`: 对外 HTTP API。
- `templates/`, `mpimg/`, `data/`: 模板、缓存图片、运行数据。

## 分层架构与依赖方向

依赖方向固定为：`plugins -> services -> infra/storage`，`utils` 只能被依赖，不能反向引用上层。

| 层 | 目录 | 职责 | 禁止 |
|---|---|---|---|
| 入口 | `bot.py` | 启动、注册适配器、挂载 API | — |
| 消息表现 | `src/plugins/`, `src/plugins/jx3bot_handlers/` | 命令匹配、参数提取、调用 service、组织回复 | 写业务逻辑、散写外部请求 |
| 业务编排 | `src/services/jx3/` | 查询流程、缓存策略、多数据源聚合 | 依赖 NoneBot 事件/Message/Bot；发送消息；渲染细节 |
| 外部适配 | `src/infra/` | HTTP、截图、第三方 API 封装 | 写领域规则、命令分发 |
| 存储 | `src/storage/` | 统一存储抽象与访问 | 存储抽象中写业务逻辑 |
| 渲染 | `src/renderers/` | 模板渲染、图片生成 | 写业务判断分支 |
| HTTP API | `src/api/routers/` | FastAPI 路由、参数校验、调用 service | 直接读写文件、调用截图/消息发送 |

## DDD 修改流程

以后修改项目时，默认按 DDD 思路推进：

1. 先设计用例：明确触发入口、参与角色、输入输出、业务规则、异常分支和验收标准。
2. 再修改代码：把领域规则放在 `services` 可编排的业务层或更内聚的领域对象中，入口层只做参数提取和调用，外部系统能力通过 `infra/storage/renderers` 适配。
3. 最后验证用例：针对设计用例逐项执行自动化或手工验证，说明覆盖到的正常路径、异常路径，以及无法离线验证的外部依赖。

如果现有代码尚未完全符合 DDD 分层，优先在本次改动范围内收敛边界，不为无关模块做大规模重构。

## 需求交付流程

以后修改本项目时，默认遵循 `docs/PLANS.md` 定义的需求级交付流程：`需求澄清 -> 方案文档 -> 执行计划 -> 开发实现 -> 测试验证 -> review -> 验收归档`。该流程适用于实现、重构、排查修复、文档联动等仓库任务。

中大型需求默认在 `openspec/changes/<change-id>/` 下建立 change。开始前先读取 `openspec/AGENTS.md`、相关 `openspec/specs/` 基线和 active change。change 内至少按需维护：

- `proposal.md`
- `design.md`
- `tasks.md`
- `specs/<capability>/spec.md` delta
- `test-plan.md`
- `review.md`
- `acceptance.md`

执行规则：

1. 先与用户澄清需求，明确目标、入口、影响范围、非目标、兼容要求和回滚要求。
2. 需求澄清后先输出方案文档，方案确认后再输出执行计划。
3. 执行计划必须细化到具体开发方案设计，覆盖涉及模块、文件和分层边界、数据结构与存储、API/前端/定时任务/缓存/外部接口改动点、验证命令、手工回归路径、风险与回滚方案。
4. 执行计划经用户或项目 owner 确认前，不开始业务代码实现。
5. 实现过程中按计划逐项修改代码或文档，不随意扩大改动范围，并持续更新进度、决策、发现、测试和 review 记录。
6. 中大型改动优先按 `subagent-implementation` skill 拆分可并行任务；测试、冒烟和 review 也可由子 agent 辅助执行，但主 agent 负责最终整合、跑测和结论。
7. 完成后运行计划中定义的自动化验证和最小冒烟；无法离线验证的外部依赖，补充手工回归路径和风险说明。
8. 对照方案、执行计划和 diff 做 code review，确认实现、测试和文档没有偏离方案。

小范围修复、历史计划续做或项目 owner 明确要求时，可以继续使用 `docs/exec-plans/active/` 单文件计划，并同步 `docs/exec-plans/index.md`。纯问答、只读排查、运行验证命令、数据查询、git 操作、以及不改变仓库文件的临时诊断命令可不新增计划文档。

## 强约束

- 运行时基线是 `Python 3.9+`。新增类型注解必须兼容 Python 3.9，不要在 FastAPI/Pydantic 会解析的代码路径里使用 `str | None`、`dict[str, Any] | None` 这类 `|` 联合写法，统一改用 `typing.Optional[...]`、`typing.Union[...]`。其他位置也尽量不用。
- `services` 不得直接依赖 NoneBot 事件对象、`Bot`、`Event`、`MessageSegment` 或发送消息。
- `renderers` 只负责渲染，不写业务决策。
- 新增外部调用时，优先补到 `src/infra/`，不要在 handler 或 service 中直接散写请求。
- 配置优先来自环境变量和 `config.py`，秘钥、票据、Cookie、邮箱、内网地址不得硬编码进代码和文档。
- 群配置、订阅、服务器别名缓存、运行时缓存统一走 `src/storage/`，当前优先使用 MongoDB 相关 repo（如 `src/storage/mongo_repos/`），不新增裸 `open(...)` 或 JSON 文件读写。
- 数据库设计以 `docs/design-docs/database-design.md` 为准。新增、删除、重命名集合或字段，调整索引、TTL、迁移脚本、存储归属时，必须同步更新该文档。
- JJC 统计文件写入 `data/jjc_ranking_stats/<timestamp>/summary.json`，明细按需拆分在 `details/` 子目录。

## API 响应格式

所有 HTTP API 返回统一结构：

```json
{"status_code": 0, "status_msg": "success", "data": {}}
```

路由注册见 `src/api/__init__.py`，响应构造走 `src/api/response.py`。

## 文档更新规则

- `public/index.html` 与 `public/jjc-ranking-stats.html` 是同一个排名页面的两个入口，内容须保持一致；修改任一入口后同步另一个，并运行 `python -m unittest tests.test_jjc_data_center_navigation`。四个 JJC 页面统一由 `public/jjc-site.css` 控制内容最大宽度（1920px）与页面边距，不在单页覆盖 `.page` 宽度。
- 改动启动方式、端口、环境变量时，同时更新 `README.md`、`README-Docker.md`、`docs/references/runbook.md`。
- 改动模块边界、目录职责时，同时更新 `project-architecture.md` 和 `docs/exec-plans/active/refactor-plan.md`。
- 改动 API 路由时，同时更新 `README.md` 中的接口说明。
- 改动数据库集合、字段、索引、TTL、迁移脚本或存储 repo 时，同时更新 `docs/design-docs/database-design.md`。
- 改动手工验证路径时，同时更新 `docs/references/runbook.md` 的回归清单。
- 生成阶段性执行计划时，写入 `docs/exec-plans/active/` 并同步更新 `docs/exec-plans/index.md`；相关代码提交后，才可将计划移入 `docs/exec-plans/completed/` 并更新索引。
- 新需求采用 OpenSpec change 时，方案、执行计划、规格 delta、测试、review、验收文档之间口径必须一致；验收后先同步进 `openspec/specs/`，再归档 change。

## 已知遗留问题

- 多处仍使用 `print()` 而非结构化日志，详见 `docs/exec-plans/active/refactor-plan.md`。新代码使用 `nonebot.logger` 或 `loguru`，不要继续新增 `print()`。
- `src/utils/defget.py` 仍有向 handler 的兼容导入，涉及相关改造时注意依赖方向不要继续恶化。

## 常用验证

### JJC 当前赛季

JJC 大橙武以 `weapon_quality == 5`（接受字符串或整数）且武器名称命中 `config.JJC_LEGENDARY_WEAPON_NAMES` 判断。白名单保留旧等级 28 个名称，并追加苍生铸世新 50 级 33 个名称；无需小橙武排除配置。无名称或未知名称不算大橙武。两个排名页面的 `LEGENDARY_WEAPON_NAMES` 与后端配置同步，名称变更需同时更新三个位置。新生成的统计使用新配置，已保存的 `legendary_count_map` 不自动重算；部署后重启后端以载入配置。2026-11-07 已发布生产白名单和两个排名页面并重启验收，4 个内部 worker 已恢复同步；发布记录见 `docs/exec-plans/completed/jjc-cangsheng-legendary-weapons-plan.md`。

验证：`.venv/bin/python -m unittest tests.test_jjc_weapon_quality tests.test_jjc_data_center_navigation tests.test_jjc_ranking_stats_frontend tests.test_jjc_kungfu_display`。武器测试实际运行 Node 校验前后端全部名称及空值判断，覆盖 summary、图片占比和紫武筛选。

当前 `config.py` 使用“苍生铸世”赛季，`CURRENT_SEASON_START="2026-11-05"` 为竞技场开放日期（北京时间零点边界），用于排名周次、同步窗口、角色对局查询、参与者投影和最新对局刷新。周次沿用周一换周规则：2026-11-02 至 11-08 所在周为第 1 周，11-09 起为第 2 周。资料片于 10-29 公测，不使用该日期计算竞技场周次。修改配置后须重启 bot 与独立 worker；前端赛季名称和周次从统计快照读取，新赛季快照生成后自动展示。

对局历史仅展示当前赛季：排名角色近期查询（含缓存及预热）按开赛时间过滤，无时间记录不展示；近期缓存的赛季名称及起点必须与当前配置一致。上游时间倒序历史进入旧赛季后停止分页。心法对阵查询在仓储统计前截断跨赛季区间，已同步列表按赛季名称和比赛时间双重过滤。旧赛季原始数据和历史排名快照仍保留，无需清库。

14 日最高分使用快照所属赛季：`window_start=max(anchor-14天,season_start_time)`，先过滤参与者再按角色取最高分、排序及截取。定时任务、API 即时聚合与心法详情共用 `JjcPeakScoreRankingService`。结果沿用 version=1 和原唯一键，新增 `aggregation_scope=season_bounded_v1`、`season_id`、`season_start_time`；当前赛季缺少或不匹配元数据的 done 结果失效。生成先计算再原子发布，写失败必须传播，旧有效结果保留。历史 legacy 已存结果保留，边界未知不重算；已修正历史保存其原赛季边界供下次切季读取。`scripts/rebuild_jjc_peak_score_rankings.py --mode dry-run|apply|verify --season <当前赛季>` 定向处理全部锚点/两类分数，默认只读 dry-run，apply 前另行备份。实现与验收见 `openspec/changes/archive/2026-11-09-jjc-peak-score-season-boundary/`。2026-11-09 11:05 已部署，当前赛季 8 个锚点两类分数共 16 份结果完成重建和校验，公网四范围及详情验证通过；旧赛季保留，生产配置哈希未变，4 worker 在线。

### JJC 最新对局受控刷新

`openspec/changes/jjc-safe-live-refresh/` 记录最新摘要方案及预算桶回退决定。最新摘要由独立 worker 拉取，页面仍读 Mongo，不推进完整历史水位或写统计事实。推栏调用恢复为 `src/utils/tuilan_request.py` 的直连实现，不使用共享预算桶、并发许可、小时/日配额或共享暂停。签名密钥和设备 ID 沿用已提交版本默认值，可通过环境变量覆盖；票据可在 `runtime_config.json` 中配置。`JJC_LIVE_REFRESH_ENABLED` 默认 0，启用前须配置有效凭据与 `JJC_LIVE_SOURCE_SECRET`。

前端刷新提交 `POST /api/jjc/ranking-stats/synced-role-refresh`，随后最多轮询本地列表 10 次。最新摘要仍保留任务槽位、角色冷却和来源限频；原有详情与角色查询恢复直连行为。

验证：`.venv/bin/python -m unittest tests.test_tuilan_request tests.test_jjc_live_refresh`；独立 Mongo 测试需显式 `JJC_TEST_MONGO_URI`，禁止指向生产库。

- 本地启动: `python bot.py`
- 插件快速检查: `nb plugin list --json`
- 手工脚本: `python test_tuilan_match_history.py`
- 改过 FastAPI 路由、Pydantic 会解析的函数签名或新增类型注解后，至少执行一次 `python -m py_compile <相关文件>`，重点确认没有 Python 3.9 注解兼容问题。
- JJC 对局详情快照相关单测：
  ```bash
  python -m unittest tests.test_jjc_match_detail_snapshots tests.test_jjc_snapshot_repo tests.test_jjc_match_detail_hydration
  python -m py_compile src/services/jx3/match_detail_snapshots.py src/storage/mongo_repos/jjc_match_snapshot_repo.py src/storage/mongo_repos/jjc_inspect_repo.py
  ```

外部接口较多，很多验证依赖在线服务。无法离线证明正确时，至少补充手工回归路径。


### JJC 排名心法本地统计与后台画像刷新

当前默认关闭的 `JJC_RANKING_KUNGFU_CACHE_ENABLED` 控制缓存优先统计，`JJC_KUNGFU_REFRESH_ENABLED` 独立控制bot内单消费者。结果归 `role_identities.jjc_kungfu`，长期任务归独立 `jjc_kungfu_refresh_queue`；先条件发布角色后确认任务，租约/token/角色级generation提供至少一次恢复，不用事务/control/心跳。缓存未命中按排名逐个直接查询并等待，成功写回角色且计入本轮，真实查询失败保留未知；显式历史tag沿用旧路径。scope切换先停旧消费者、统一配置重启；任务不可TTL/归零重建，发现版本完整性异常先停旧写入实例再恢复。

新client只拉indicator/history，复用共享签名构造与异步HttpClient，service共享纯心法判定，singletons仅装配且按需校验配置。默认6小时刷新、24小时最大可用、48小时上榜追踪，可覆盖的三个环境变量分别为 `JJC_KUNGFU_REFRESH_SECONDS`、`JJC_KUNGFU_MAX_STALE_SECONDS`、`JJC_KUNGFU_TRACK_SECONDS`。仅一个部署实例启用消费者，Mongo原子领取不保证多实例全局请求限速。配置/回滚与测试见README、runbook及change；2026-11-06已按用户授权发布并开启生产两开关，初始预热持续进行；生产时效和覆盖率以实际快照为准，发布证据见change内deployment.md。

### JJC 排名统计强制队首同步

每次实际排名统计为范围内有效角色发起独立同步请求，包含心法未知成员；同轮即时与保存补偿共用运行标识，复用榜单 cache_time 的下一轮仍可重新排队。冷却/完成/失败立即 queued，已排队移动至最新排名批次队首；不因 priority>=1 跳过。禁用角色仍跳过，syncing 仅持久化最新待请求，在有效租约释放或过期恢复时合并补排一次，不抢占当前执行。全局暂停仍生效，查询保存快照不入队。

主/兼容队列增加四个调度字段与 idx_status_ranking_front；最新排名批次、原始名次优先于普通 priority/FIFO，列表/位置/领取一致。同步仍从最新页查最近14天并按当前赛季截断、复用已有详情。实现记录：openspec/changes/archive/2026-11-07-jjc-ranking-force-sync-front；2026-11-07 已按用户授权提交并发布；生产候选382项通过，QQ/API/双队列索引及4个内部worker验收通过；单角色冷却重新插队由真实worker完成。详见该change的deployment.md。需统一更新 bot/独立 worker，停旧消费者后升级；不能混用新生产者与旧 worker。回滚先物化 pending 为普通 queued，详见 runbook。

验证：`.venv/bin/python -m unittest tests.test_jjc_ranking_force_sync tests.test_jjc_sync_worker_queue tests.test_jjc_match_data_sync tests.test_jjc_ranking_stats_repo tests.test_jjc_ranking_command_tag`。真实 Mongo：`JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/ .venv/bin/python -m unittest tests.test_jjc_ranking_force_sync_mongo`，仅允许该显式本地隔离端口与随机测试库。


### 排行榜本地最新胜场装备

`JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=1` 后，按稳定角色 ID 读取本地当前赛季最新一场 3v3 胜场，统一提供装备、队友、橙武统计和紫武筛选。取最新胜场后装备缺失时保留未知，不向更旧胜场回退；心法分类保留原有来源。默认值为 0，支持环境变量与私有 runtime_config.json，修改后重启。`JJC_RANKING_DISPLAY_QUERY_ENABLED` 与 `JJC_RANKING_DISPLAY_OVERLAY_ENABLED` 是既有资料查询和展示覆盖开关，各自默认 0。

读服务使用专用 Mongo 连接和 singleflight，在原 1.5 秒 Mongo、2 秒 socket、8 秒构建、10 秒请求预算内完成整榜；超时或证据不一致时明确失败，不截断成员或丢弃异常证据。`JJC_EQUIPMENT_MONGO_HOST` 默认为空，仅在有明确部署需要时覆盖装备读连接的主机，凭据仍取部署配置。

开启前必须完成主表派生字段回填、索引校验与全榜只读预检。字段、索引及回滚限制见 `docs/design-docs/database-design.md` 和 `docs/references/runbook.md`。方案与验证记录见 `openspec/changes/jjc-ranking-local-latest-match/`；独立提交包验证见其中 `submission.md`。
