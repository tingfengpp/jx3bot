# 数据库设计

更新时间：2026-05-11

本文是当前项目数据库结构的维护文档。以后新增、删除、重命名集合或字段，调整索引、TTL、迁移脚本、存储归属时，必须同步更新本文。

## 总览

- 当前主存储是 MongoDB，连接串来自 `config.MONGO_URI`，可由环境变量 `MONGO_URI` 或 `runtime_config.json` 覆盖。
- Mongo 初始化入口是 `src/infra/mongo.py:init_mongo()`，启动时会连接数据库、执行 `ping`，并通过 `_ensure_indexes()` 幂等创建索引。
- 存储访问边界优先放在 `src/storage/mongo_repos/`。历史上仍有 `src/services/jx3/jjc_cache_repo.py` 直接封装 JJC 缓存访问，修改时应保持调用方兼容，避免在 handler 中直接读写 Mongo。
- 集合采用 MongoDB 默认 `_id`，业务唯一性由下表索引保证。文档字段没有强 schema 校验，字段契约以本文、repo 写入逻辑和迁移脚本共同约束。

## 维护规则

- 数据库相关改动必须同步修改本文，包括集合、字段、索引、TTL、数据类型、迁移脚本和读写 repo 的变化。
- 新增持久化能力优先新增或扩展 `src/storage/mongo_repos/`，不要在 `plugins`、handler、API router 中散写 `db.collection`。
- 新增索引默认写入 `src/infra/mongo.py:_ensure_indexes()`，并在本文的集合章节记录索引名、字段和唯一性。默认关闭、需要先验证跨进程配置的 JJC 受控刷新是明确例外：其五个集合由 `JjcLiveRefreshRepo.initialize()` 负责初始化，仅在独立刷新 worker 启动时执行；失败只停用新刷新能力，不阻断既有 bot/API。
- 有历史 JSON 或文件缓存需要迁移时，迁移脚本放在 `scripts/`，脚本说明要写明源路径、目标集合和幂等键。
- 时间字段应优先使用 Unix 秒级时间戳；若使用 Mongo `datetime` 用于 TTL，必须在字段说明中明确类型和原因。
- **TTL 字段约束**：MongoDB TTL 索引仅对 `Date` 类型字段生效，对 `float`/`int` 类型字段创建 TTL 索引实际不会触发自动删除。新建集合的 TTL 字段必须使用 `datetime` 类型；旧集合若使用非 `Date` 字段声明了 TTL 索引，应在对应集合描述中标注"TTL 实际不生效"，并在合理时机补 `_dt` 后缀的 Date 冗余字段或由 repo 业务逻辑自行判断过期。
- 群号、QQ 号等外部 ID 当前多数以字符串落库，新增字段应沿用对应集合已有类型，避免同一字段混用数字和字符串。

## 集合设计

### JJC 受控刷新

共享官方出口预算桶已回退；应用不再读写 `tuilan_request_budget`。已有集合保留为历史数据，本次不执行线上数据删除。

`jjc_live_refresh_slots` 维护固定数量数字槽位；字符串 `task_key` partial unique 索引防止同角色重复任务。`jjc_live_refresh_state` 以规范身份+赛季+3v3 的 SHA256 为 `_id`，保存 generation、租约、冷却、失败与当前摘要 snapshot 指针，独立于历史同步水位。

`jjc_live_match_summaries` 保存不可变摘要快照，唯一索引 `(task_key,snapshot_id,match_id)`，`expires_at_dt` 使用 Date 类型 TTL 0 默认保留 7 天。发布先写快照，再以有效 generation CAS 更新 state 指针，迟到 worker 不能覆盖当前结果。`jjc_live_workers` 保存心跳有效期，`jjc_live_source_limits` 保存服务端 HMAC 来源的 CAS 滚动窗口。

刷新 repo 在 worker 启动时幂等建立上述索引；配置不一致或索引失败时功能不启动。最新摘要最多 40 条，不影响完整历史分页与统计集合。

| 集合 | 索引名 | 字段与约束 |
|---|---|---|
| `jjc_live_refresh_slots` | `task_key_1` | `task_key` 唯一，仅包含字符串 task_key；数字 `_id` 为任务槽，`configuration` 文档保存一致性参数 |
| `jjc_live_match_summaries` | `task_key_1_snapshot_id_1_match_id_1` | 三字段复合唯一 |
| `jjc_live_match_summaries` | `match_id_1` | 普通索引 |
| `jjc_live_match_summaries` | `expires_at_dt_1` | Date 类型，TTL 0；到期时间为快照发布后 7 天，读取也检查有效期 |
| `jjc_live_source_limits` | `expires_at_dt_1` | Date 类型，TTL 0；窗口 60 秒，闲置来源在最后接收后 120 秒到期 |

上述集合均有默认 `_id_` 索引。`jjc_live_refresh_state` 的冷却和活动期限、`jjc_live_workers.until` 心跳期限为 Unix 秒时间戳，由业务判断有效期，不设 TTL；任务槽位固定保留，以便跨进程复用。来源集合的 `_id` 仅保存 HMAC 散列，不记录原始 IP。

### `jjc_match_facts`

用途：每场 3v3 对局一条的统计读模型，由 `jjc_match_detail` 派生；`active=false` 为保留源水位的 tombstone，不参与查询。

读写归属：`src/storage/mongo_repos/jjc_match_fact_repo.py`；构建与投影由 `src/services/jx3/jjc_match_fact.py`、`match_detail_fact_projection.py` 负责。

核心字段：`match_id`（int）、`match_time`（int Unix 秒）、`team1/team2`（三人心法 ID、组合键、分数和）、胜负字段、`game_score`/`score_avg`、`source_hash`、`source_cached_at`、`schema_version`、`active`、`invalid_reason`。

索引：`idx_match_id`（`match_id` unique）、`idx_active_combo_time`（`any_team_combo_keys, match_time, match_id`，partial `active=true`）、`idx_active_time`（`match_time, match_id`，partial `active=true`）。历史回填使用 `scripts/backfill_jjc_match_facts.py`，repo 以源水位/hash/schema CAS 防止旧来源复活 tombstone。

### `group_configs`

用途：群配置持久化，替代旧 `groups.json`。用于开服监控、日常推送、群默认服务器等按群配置。

读写归属：

- `src/storage/mongo_repos/group_config_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `group_id` | string | 群号，业务唯一键 |
| 其他配置字段 | any | 从旧 `{group_id: config}` 结构展开保存，例如 `servers`、开服推送相关开关等 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_group_id` | `group_id` | unique |

### `reminders`

用途：群提醒任务持久化，支持启动后恢复 pending 任务、取消提醒和完成状态记录。

读写归属：

- `src/storage/mongo_repos/reminder_repo.py`
- 业务入口：`src/plugins/jx3bot_handlers/reminder.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `reminder_id` | string | 提醒业务 ID，当前由 `uuid.uuid4().hex` 生成 |
| `group_id` | string | 群号 |
| `creator_user_id` | string | 创建提醒的 QQ 号 |
| `mention_type` | string | `user` 或 `all` |
| `message` | string | 提醒内容 |
| `remind_at` | string | 执行时间，格式为 `YYYYMMDDHHMMSS` |
| `created_at` | int | 创建时间 Unix 秒 |
| `status` | string | `pending`、`done`、`canceled` |
| `done_at` | int | 完成时间 Unix 秒，仅完成后写入 |
| `canceled_at` | int | 取消时间 Unix 秒，仅取消后写入 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_reminder_id` | `reminder_id` | unique |
| `idx_group_status` | `group_id`, `status` | 普通复合索引 |
| `idx_status_remind_at` | `status`, `remind_at` | 普通复合索引 |

### `wanbaolou_subscriptions`

用途：万宝楼价格订阅，替代旧 `data/wanbaolou_subscriptions.json`。

读写归属：

- `src/storage/mongo_repos/wanbaolou_sub_repo.py`
- 业务入口：`src/plugins/wanbaolou/`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `user_id` | string | 订阅用户 QQ 号 |
| `item_name` | string | 订阅商品名 |
| `price_threshold` | int | 价格阈值 |
| `group_id` | string/null | 订阅所在群，私聊或未知时为 `null` |
| `created_at` | float | 创建或重新启用时间 Unix 秒 |
| `active` | bool | 是否有效；删除订阅时置为 `False` |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_user_id` | `user_id` | 普通索引 |
| `idx_user_item` | `user_id`, `item_name` | unique |

### `status_cache`

用途：状态监控插件的通用缓存集合，存储新闻、技改、福利码、服务器状态和状态历史等小型状态。

读写归属：

- `src/storage/mongo_repos/status_cache_repo.py`
- 业务入口：`src/plugins/status_monitor/storage.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `cache_name` | string | 缓存名，例如 `records_ids`、`news_ids`、`event_codes_ids`、`server_status`、`status_history` |
| `data` | any | 缓存内容 |
| `updated_at` | int | 更新时间 Unix 秒 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_cache_name` | `cache_name` | unique |

### `server_master_cache`

用途：区服主服查询缓存，减少重复请求 `jx3api` 主服接口。

读写归属：

- `src/storage/mongo_repos/server_master_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `key` | string | 查询 key |
| `name` | string | 主服名称 |
| `zone` | string | 大区 |
| `server_id` | string | 区服 ID |
| `cached_at` | int | 缓存写入时间 Unix 秒（⚠ int 类型，TTL 索引不生效） |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_key` | `key` | unique |
| `idx_cached_at` | `cached_at` | TTL 604800 秒（⚠ int 类型，TTL 实际不生效） |

说明：repo 内部也会按 `ttl_seconds` 主动判断过期并删除；TTL 索引因字段类型为 int 不可靠，只作兜底意图声明。若长期保留本集合，应补 `cached_at_dt`（Date 类型）用于真实 TTL。

### `kungfu_cache` **[LEGACY — 已删除]**

> **状态**：本集合已被 `role_identities` + `role_jjc_cache` 替代。运行时代码已移除对本集合的依赖，且历史 `kungfu_cache` 集合已于 2026-04-30 从 MongoDB 删除（删除前文档数 11474）。新代码不应再直接依赖本集合。

用途：JJC 排名中角色心法、武器和队友心法判断缓存，替代旧 `data/cache/kungfu/*.json`。以 `server + name` 为唯一键，但该键不是永久身份主键——同一角色改名/转服后会产生新记录，同一角色也可能因来源不同存在多条记录。

历史归属：

- 历史心法解析逻辑：`src/services/jx3/kungfu.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `server` | string | 服务器名 |
| `name` | string | 角色名 |
| `kungfu` | string/null | 心法中文名 |
| `kungfu_id` | string/int/null | 推栏返回的心法 ID |
| `weapon` | object/null | 武器详情原始结构 |
| `weapon_checked` | bool | 是否完成武器检查 |
| `teammates` | array | 队友结构列表，包含 `role_name`、`server`、`role_id`、`global_role_id`、`kungfu_id` 等 |
| `teammates_checked` | bool | 是否完成队友心法检查 |
| `cache_time` | float | 缓存时间 Unix 秒（⚠ float 类型不可用于 MongoDB TTL 索引；TTL 依赖 `idx_cache_time` 实际不生效，仅由 repo 业务逻辑判断过期） |
| 其他结果字段 | any | 历史缓存文件和心法解析结果可能携带的附加字段 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_server_name` | `server`, `name` | unique |
| `idx_cache_time` | `cache_time` | TTL 604800 秒（⚠ float 类型，TTL 实际不生效） |

迁移到新集合的说明：

- 运行时业务已不再回退本集合；新集合 miss 时直接查外部接口并回写 `role_identities` / `role_jjc_cache`。
- 本集合已完成历史清理；旧迁移脚本已删除，后续不再从该集合迁移数据。

### `role_identities`

查询 ID 恢复复用 `game_role_id` / `role_id`，不增加集合、字段或索引。仅原始成功 indicator 的请求/响应 ID、服务器、区服、SK01、名字与独立 replay 精确数字 `global_id` 映射一致时，内部 `replace_query_ids_if_unchanged` 才能以全画像快照 CAS 替换两个 ID。CAS 区分缺失和 null，冲突不强写；不改变稳定主键、画像对局水位或同步进度。

复用的 `global_role_id_refreshed_at` 为本次严格请求开始的 UTC Date（毫秒精度），`global_role_id_refresh_source=indicator_replay_verified` 表示严格证据；普通调用方不能仅复制标签获得权限。严格标记后，历史/未验证写入不能部分回退服务器、名字、规范化字段、区服和 ID 组合，普通刷新不推进或擦除该标记。再次转服/改名超出本次 ID-only 范围，保守列为未解决。

此严格 ID 覆盖前也归档旧画像到既有 `role_identities_history`，`archive_reason=role_query_ids_recovered`；归档失败零更新，归档成功但 CAS 失配不计修复。条件回滚仅还原本次 ID/观测字段及其原缺失状态，保留后续合法更新。一次性 CLI 的私有操作文件承担 before/after-image 和调用预算，不作为运行时缓存或 Mongo 恢复平台。

用途：游戏角色身份与当前画像统一模型，维护不同来源的身份关联，并通过 `jjc_kungfu` 保存当前3v3最近一次成功心法结果。替代 `kungfu_cache` 中以 `server + name` 作为唯一键的弱身份模型。

代码新增画像字段与独立刷新队列（2026-11-06已部署并开启生产刷新，仍在初始预热）。`jjc_kungfu` 是新统计路径的唯一成功值；旧 `role_jjc_cache` 仍供兼容查询及辅助数据使用，新路径不双写心法。字段如下，缺失表示尚无可用成功结果：

| `jjc_kungfu` 子字段 | 类型 | 说明 |
|---|---|---|
| `name` / `kungfu_id` | string / int | 规范心法名与目录ID |
| `season_id` / `season_start` | string / int | 当前配置赛季与北京时间开赛零点Unix秒 |
| `pvp_type` / `algorithm_version` | int / int | 首版固定3v3及判定版本1 |
| `source` | string | `indicator` 或 `match_history` |
| `requested_at` / `verified_at` | float | 本轮尝试开始（含请求间隔等待）和验证完成UTC Unix秒，独立于身份时钟 |
| `win_sample_count` | int | 最近40场中用于十胜判定的胜场样本数 |
| `publish_version` | int | 对应队列角色级generation，正整数，跨作用域递增 |

发布只 `$set jjc_kungfu`，不修改顶层 `updated_at`、画像对局水位或历史同步进度。按角色 `_id`、完整查询定位快照（区分缺失与null）及严格较旧整数版本条件更新，不upsert身份；非法存量版本拒绝覆盖。失败保留上次成功值和验证时间。统计校验作用域、版本、时间及规范心法ID，默认6小时内fresh、24小时内stale可用，超过24小时unknown。批量榜单解析每250人一批查询既有身份，冲突不按同名猜测。 兼容身份缺少榜单已提供的查询定位时，仅该候选走原安全upsert补齐，并验证同一_id、ID兼容、必要字段齐全与已有心法保留；定位判断与补全结果校验为独立纯函数，完整身份保持批量热路径。此整理不改变集合字段、索引或条件发布协议。


读写归属：

- `src/storage/mongo_repos/role_identity_repo.py`
- 辅助脚本：`scripts/backfill_jjc_role_id_from_match_replay.py` 可从已同步 match/replay 补充 `global_id`、`role_id`、SK01 `global_role_id` 等身份字段。

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `identity_key` | string | 内部主键，按 `identity_key` 生成规则计算，业务唯一；JJC 新数据优先使用 `global_id:{global_id}` |
| `identity_level` | string | 身份级别：新 JJC 身份固定优先 `global_id`；历史兼容值包括 `global`（旧 SK01 `global_role_id` 主键）、`game_role`（`zone + game_role_id`）、`name`（仅 `server + name`） |
| `server` | string | 当前服务器名 |
| `normalized_server` | string | 规范化服务器名（去除空格、统一大小写等） |
| `name` | string | 当前角色名，只保存纯角色名；推栏对局详情里的 `角色名·服务器` 展示名在派生写入时必须先拆掉服务器后缀 |
| `normalized_name` | string | 规范化角色名 |
| `zone` | string/null | 大区，来源于排行榜或 indicator |
| `game_role_id` | string/null | 排行榜常见的角色 ID（`gameRoleId`） |
| `role_id` | string/null | 角色详情或对局详情中的角色 ID |
| `global_id` | string/null | JJC 稳定角色身份 ID，来自 `match/replay.players[].global_role_id`；不得与 SK01 `global_role_id` 混用 |
| `global_role_id` | string/null | SK01 全局角色 ID，来自 `role/indicator.role_info.global_role_id`，用于请求推栏战局历史 |
| `person_id` | string/null | 推栏个人 ID；对局详情或 indicator `person_info.person_id` 可提供 |
| `role_info_observed_match_time` | int/null | 当前角色画像字段所依据的最近对局时间 Unix 秒；对局来源只有晚于该值才允许覆盖已有非空画像字段 |
| `role_info_source` | string/null | 最近一次角色画像补充来源，如 `match_detail`、`match_replay_indicator_backfill`、`match_replay_backfill`、`match_replay_indicator` |
| `role_info_updated_at` | float/null | 最近一次角色信息补充写入时间 Unix 秒 |
| `aliases` | array | 历史名称、历史服务器名、旧 `identity_key` 等，支持改名/转服后的回溯查询 |
| `sources` | array | 数据来源列表，取值：`ranking`、`indicator`、`match_detail`、`migrated_kungfu_cache` |
| `profile_observed_at` | datetime/null | 当前 `server` / `name` 所依据的资料观测时间；排行榜/indicator 用查询时间，对局详情用 `match_time` |
| `first_seen_at` | datetime | 首次记录时间 |
| `last_seen_at` | datetime | 最近一次出现时间 |
| `updated_at` | datetime | 最近一次字段更新（如升级 identity_level）时间 |
| `schema_version` | int | schema 版本号 |

`created_at` 不是 `role_identities` 标准必需字段；历史脚本写入的旧字段可存在，线上规范化脚本只做兼容转换，新写入逻辑不依赖它。

`profile_history` 已废弃：历史文档可能仍残留该数组字段，但运行时写入和补数脚本不再维护它。需要清理存量大文档时，使用 `scripts/cleanup_role_identity_profile_history.py` 默认 dry-run 核对规模，再通过 `--execute` 对 `role_identities` 执行 `$unset profile_history`。当前画像保留在 `role_identities` 主文档；重要画像覆盖前的旧快照由 `role_identities_history` 承担回溯。

`identity_key` 生成规则：

| 优先级 | 条件 | key 格式 | identity_level |
|---|---|---|---|
| 1 | 有 replay `global_id` | `global_id:{global_id}` | `global_id` |
| 2 | 无 `global_id`，有 SK01 `global_role_id` | `global:{global_role_id}` | `global`（历史兼容，待迁移） |
| 3 | 无 global，有 `zone + game_role_id` | `game:{zone}:{game_role_id}` | `game_role`（弱身份兼容） |
| 4 | 只有名称入口 | `name:{normalized_server}:{normalized_name}` | `name`（弱身份兼容） |

**身份升级规则**：当一条较低级别身份（如 `name` 或 `game_role`）后来获得了更高级别的外部 ID 时，执行升级：

- `name` → `game_role`：从排行榜拿到 `zone + game_role_id` 后，重新计算 `identity_key` 为 `game:{zone}:{game_role_id}`，将旧 `identity_key` 推入 `aliases`。
- 任意弱身份 → `global_id`：通过 `match/history + match/replay` 或同步详情 replay 拿到数字 `global_id` 后，重新计算 `identity_key` 为 `global_id:{global_id}`，将旧 `identity_key` 推入 `aliases`。
- `game_role` → `global`：仅作为历史兼容；通过 indicator 拿到 SK01 `global_role_id` 后可补字段，但后续仍应继续补 replay `global_id`。
- 升级后旧 `identity_key` 记录不删除，但标记或合并到新记录。
- 同一 `global_id` 不得出现多条 `global_id:{global_id}` 记录；同一 SK01 `global_role_id` 或同一 `zone + game_role_id` 对应多个 `global_id` 时只输出冲突样本，不按 SK01 或旧 role_id 自动合并。
- `match_detail` / replay 属于历史对局来源，只有当对局 `match_time` 晚于现有 `role_info_observed_match_time` 时才覆盖当前画像字段（`server`、`name`、`zone`、`role_id`、`game_role_id`、SK01 `global_role_id`、`person_id`）；旧对局或无对局时间来源默认只补齐缺失字段，避免用旧对局覆盖转服/改名后的当前资料。
- 当更新对局覆盖 `server`、`name` 或 SK01 `global_role_id` 时，覆盖前的旧 `role_identities` 文档快照写入 `role_identities_history`；仅 `role_id` / `zone` 等普通画像变化不单独归档。
- `ranking` / `indicator` 等无对局时间来源默认只补齐缺失画像字段，不覆盖已有非空画像字段；若入口已通过最近对局 replay 补到 `match_time`，则按同一时间水位规则处理。
- 对于 `match_detail` 来源，原始详情里的 `players_info[].role_name` 可以保留推栏展示值；但派生写入 `role_identities.name` 时必须按“仅当最后一个 `·` 右侧等于当前 `server` 才拆分”的规则规范化为纯角色名。

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_identity_key` | `identity_key` | unique |
| `idx_global_id` | `global_id` | unique, partial（仅 `global_id` 不为 null 时） |
| `idx_global_role_id` | `global_role_id` | 普通索引；SK01 不再作为最终唯一身份 |
| `idx_zone_game_role_id` | `zone`, `game_role_id` | 普通复合索引；用于弱身份 fallback 与冲突审计 |
| `idx_zone_role_id` | `zone`, `role_id` | 普通复合索引；用于兼容旧 `role_id` 字段的弱身份 fallback |
| `idx_normalized_server_name` | `normalized_server`, `normalized_name` | 普通索引（用于按名称查询入口） |
| `idx_server_name` | `server`, `name` | 普通非unique复合索引；支持legacy raw名称分支，使榜单批量OR各分支均可使用索引 |
| `idx_normalized_name` | `normalized_name` | 普通索引；用于 JJC 对局查询未命中时按同名跨服和 `@` 后缀前缀匹配候选 |
| `idx_last_seen_at` | `last_seen_at` | 普通索引 |

### `role_identities_history`

用途：归档被合并或被新画像替换的 `role_identities` 历史文档。当前运行时代码可在角色画像更新时写入历史快照；旧审计归档脚本已删除。

读写归属：

- `src/storage/mongo_repos/role_identity_repo.py`
- 只读查询按需使用 `identity_key` 或 `global_role_id`

字段：继承 `role_identities` 全部字段，外加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `archived_at` | datetime | 归档时间 |
| `archive_reason` | string | 归档原因，包括 `"duplicate_global_role_id_merged"`、`"role_identity_profile_updated"`、严格查询 ID 修复的 `"role_query_ids_recovered"` |
| `replaced_by_identity_key` | string | 保留文档的 `identity_key` |
| `archive_source` | string/null | 触发归档的写入来源，如 `match_detail` |
| `replaced_by_observed_match_time` | int/null | 触发覆盖的新对局时间 Unix 秒 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_identity_key` | `identity_key` | 普通索引 |
| `idx_global_role_id` | `global_role_id` | 普通索引 |
| `idx_archived_at` | `archived_at` | 普通索引 |

### `role_jjc_cache`

用途：JJC 角色画像缓存，关联 `identity_key`。从 `kungfu_cache` 中拆分出心法、武器、队友等可变缓存信息，与角色身份模型解耦。

读写归属：

- `src/storage/mongo_repos/role_jjc_cache_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `identity_key` | string | 关联 `role_identities.identity_key`，业务唯一 |
| `server` | string | 查询与展示冗余字段 |
| `name` | string | 查询与展示冗余字段 |
| `normalized_server` | string | 规范化服务器名，与 `role_identities` 对齐用于关联查询 |
| `normalized_name` | string | 规范化角色名，与 `role_identities` 对齐用于关联查询 |
| `zone` | string/null | 查询与展示冗余字段 |
| `game_role_id` | string/null | 外部 ID 冗余，便于调试和查询 |
| `role_id` | string/null | 外部 ID 冗余 |
| `global_id` | string/null | JJC 稳定角色身份 ID 冗余，缓存查询优先使用 |
| `global_role_id` | string/null | SK01 全局角色 ID 冗余，用于请求战局历史 |
| `kungfu` | string/null | 最终判定的心法中文名 |
| `kungfu_id` | string/int/null | 最终判定的心法 ID |
| `kungfu_pinyin` | string/null | 心法拼音 |
| `kungfu_indicator` | string/null | 通过 indicator 接口获取的心法名 |
| `kungfu_match_history` | string/null | 通过战局历史统计获取的心法名 |
| `kungfu_selected_source` | string/null | 最终采用的心法来源：`indicator`、`match_history`、`cached_match_detail_win_history` 等 |
| `cached_match_detail_win_count` | int/null | 历史胜场心法兜底的胜场数 |
| `cached_match_detail_total_count` | int/null | 历史胜场心法兜底的总出场次数 |
| `cached_match_detail_latest_win_match_id` | int/null | 历史胜场心法兜底最近胜场对局 ID |
| `cached_match_detail_latest_win_time` | int/null | 历史胜场心法兜底最近胜场时间 Unix 秒 |
| `cached_match_detail_win_samples` | array/null | 历史胜场心法兜底胜场采样（最多 5 条） |
| `weapon` | object/null | 武器详情原始结构 |
| `weapon_icon` | string/null | 武器图标 |
| `weapon_quality` | string/int/null | 武器品质 |
| `weapon_checked` | bool | 是否完成武器检查 |
| `teammates` | array | 队友信息列表 |
| `teammates_checked` | bool | 是否完成队友心法检查 |
| `match_history_checked` | bool | 是否完成战局历史检查 |
| `match_history_win_samples` | int | 战局历史胜利采样数 |
| `source` | string | 写入来源：`ranking`、`inspect`、`migrated` |
| `checked_at` | datetime | 检查/更新时间（用于 TTL 过期判断） |
| `expires_at` | datetime | 过期时间，仅供 repo 业务清理使用；不参与 MongoDB TTL 索引（TTL 仅由 `checked_at` 驱动） |
| `schema_version` | int | schema 版本号 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_identity_key` | `identity_key` | unique |
| `idx_global_id` | `global_id` | 普通索引 |
| `idx_global_role_id` | `global_role_id` | 普通索引 |
| `idx_zone_game_role_id` | `zone`, `game_role_id` | 普通索引 |
| `idx_normalized_server_name` | `normalized_server`, `normalized_name` | 普通索引 |
| `idx_checked_at` | `checked_at` | TTL 604800 秒（Date 类型，7 天） |

### `jjc_ranking_cache`

用途：JJC 排行榜完整结果缓存，避免频繁请求排行榜接口和重复计算。

读写归属：

- `src/services/jx3/jjc_cache_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `cache_key` | string | 当前固定为 `ranking` |
| `cache_time` | float | 业务缓存时间 Unix 秒 |
| `data` | object | 排行榜结果结构 |
| `created_at` | datetime | Mongo TTL 使用的写入时间（Date 类型，TTL 正常生效） |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_cache_key` | `cache_key` | unique |
| `idx_created_at` | `created_at` | TTL 7200 秒（Date 类型，TTL 正常生效） |

### `jjc_role_recent`

用途：JJC 角色近期战绩检查缓存，替代旧 `data/cache/jjc_ranking_inspect/role_recent/`。

读写归属：

- `src/storage/mongo_repos/jjc_inspect_repo.py`
- 业务逻辑：`src/services/jx3/jjc_ranking_inspect.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `server` | string | 服务器名 |
| `name` | string | 角色名 |
| `cached_at` | float | 缓存时间 Unix 秒（⚠ float 类型，TTL 索引不生效） |
| `data` | object | 角色近期战绩结果 |
| `data.current_season` | string | 生成缓存时的赛季名称，须与当前配置一致才复用 |
| `data.season_start_time` | int | 生成缓存时的竞技场开赛边界，按北京时间零点计算，须与当前配置一致才复用 |

角色近期缓存由实时查询及排名预热写入相同的赛季标记和时间过滤结果。旧文档无标记或与配置不符时按缓存未命中处理，由正常请求覆盖，无需迁移或清库；即使标记一致，响应前仍过滤赛季边界前或缺少比赛时间的记录。分页在历史时间倒序到达上一赛季时停止，其他情况使用上游原始记录数推进游标。

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_server_name` | `server`, `name` | unique |
| `idx_cached_at` | `cached_at` | TTL 86400 秒（⚠ float 类型，TTL 实际不生效；实际过期判断由业务逻辑完成） |

### `jjc_role_indicator`

用途：JJC 角色推栏 indicator 指标缓存，用于排行榜角色弹窗顶部指标展示。

读写归属：

- `src/storage/mongo_repos/jjc_inspect_repo.py`
- 业务逻辑：`src/services/jx3/jjc_ranking_inspect.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `cache_key` | string | 缓存键，使用 `identity_key`（新数据如 `global_id:<global_id>`，历史兼容 `global:<global_role_id>`），业务唯一 |
| `identity_key` | string | 关联 `role_identities.identity_key` |
| `server` | string | 服务器名 |
| `name` | string | 角色名 |
| `game_role_id` | string/null | 游戏角色 ID |
| `global_id` | string/null | JJC 稳定角色身份 ID，来自 replay 数字 ID |
| `global_role_id` | string/null | SK01 全局角色 ID，用于请求战局历史 |
| `zone` | string/null | 区服分区 |
| `indicator` | object | 规整后的 3v3 指标：`source`、`type`、`total_matches`、`win_rate`、`score`、`mvp_count`、`grade`；其中 `mvp_count` 表示赛季最佳/MVP 次数 |
| `raw` | object | `role/indicator` 原始响应 |
| `cached_at` | float | 缓存时间 Unix 秒（⚠ float 类型，TTL 索引不生效；过期由 repo 业务 TTL=86400 秒判断；页面主动刷新可绕过缓存） |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_cache_key` | `cache_key` | unique |
| `idx_cached_at` | `cached_at` | 普通索引（用于排查和后续清理；过期判断由业务逻辑完成，不依赖 TTL 索引） |

写入来源：

- 排行榜页面角色 indicator 接口按需读取推栏并写入。
- 每日/手动 JJC 排名统计在心法判定过程中已请求到 `role/indicator` 时，会同步预热写入本集合。

### `jjc_match_detail`

用途：JJC 对局详情缓存，替代旧 `data/cache/jjc_ranking_inspect/match_detail/`。

读写归属：

- `src/storage/mongo_repos/jjc_inspect_repo.py`
- 业务逻辑：`src/services/jx3/jjc_ranking_inspect.py`、`src/services/jx3/jjc_ranking.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `match_id` | int | 对局 ID，业务唯一键 |
| `cached_at` | float | 缓存时间 Unix 秒 |
| `data` | object | 对局详情原始或规整后的结果；包含 `detail` 与可选完整 `replay` 原始响应 |

`data` 顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `detail` | object/null | 推栏 `match/detail` 详情数据；不可用终态为 null |
| `replay` | object/null | 推栏 `match/replay` 完整原始响应，用于后续页面点击和身份补齐复用，避免重复请求 replay |

`data` 内部玩家节点字段（拆表后）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `players_info[].equipment_snapshot_hash` | string | 关联 `jjc_equipment_snapshot.snapshot_hash`，替代原 `armors` 数组 |
| `players_info[].talent_snapshot_hash` | string | 关联 `jjc_talent_snapshot.snapshot_hash`，替代原 `talents` 数组 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_match_id` | `match_id` | unique |
| `idx_detail_match_time` | `data.detail.match_time` | 普通索引 |
| `idx_detail_team1_server_role_name` | `data.detail.team1.players_info.server`, `data.detail.team1.players_info.role_name` | 普通复合索引 |
| `idx_detail_team2_server_role_name` | `data.detail.team2.players_info.server`, `data.detail.team2.players_info.role_name` | 普通复合索引 |
| `idx_detail_team1_player_global_id` | `data.detail.team1.players_info.global_id` | 普通索引 |
| `idx_detail_team2_player_global_id` | `data.detail.team2.players_info.global_id` | 普通索引 |
| `idx_replay_player_role_id` | `data.replay.data.players.role_id` | 普通索引 |
| `idx_replay_player_global_role_id` | `data.replay.data.players.global_role_id` | 普通索引 |

写入来源：

- 排行榜页面对局详情接口按需读取推栏并写入。
- JJC 对局同步通过排行榜 inspect service 写入。
- 每日/手动 JJC 排名统计在心法判定过程中已请求到最近胜场 `match/detail` 时，会同步预热写入本集合，并复用装备/奇穴快照拆分逻辑。

当前存储形态：

- `data` 字段是 MongoDB 嵌套对象（BSON document），不是 JSON 字符串。代码通过 Motor/PyMongo 写入 Python `dict`，Mongo 底层以 BSON 保存；这是推荐形态，支持嵌套字段查询、局部更新和后续迁移。
- 不应把 `data` 改成 JSON 字符串。JSON 字符串会让 Mongo 只能按普通字符串处理，无法直接索引或查询 `data.detail.*` 子字段。
- 现有 API 返回结构依赖 `data.detail.basic_info`、`team1`、`team2`、`players_info[].armors`、`players_info[].talents` 等字段。前端 `public/jjc-ranking-stats.html` 当前展示基础对局信息、双方玩家、分数/血量/装分、装备图标和奇穴图标。
- 推栏明确返回 `code=-1`、`msg=no data found`、`data=null` 时，`data` 保存轻量不可用终态：`{"match_id": <int>, "unavailable": true, "code": -1, "message": "no data found", "detail": null}`。这类文档不进入装备/奇穴快照拆表，读取时原样返回并带缓存命中信息。
- 可用详情会尽量保存 `data.replay` 完整原始响应。读取缓存时若已存在 `data.replay`，直接用它回填 `players_info[].global_id`；仅历史缓存缺失 replay 时才补请求并写回本集合。

2026-04-30 只读抽样观察：

- 当前集合文档数约 341，逻辑大小约 20.45 MB，平均每局约 60 KB；WiredTiger 压缩后集合存储约 7.0 MB，索引约 80 KB。
- 顺序采样 80 局显示单局 BSON 大小约 51-61 KB，3v3 对局固定 6 名玩家。
- 主要体积来自玩家装备与奇穴：`armors` 平均约 4.1 KB/玩家，`talents` 平均约 3.1 KB/玩家，`metrics` 平均约 1.1 KB/玩家，`body_qualities` 平均约 1.0 KB/玩家。
- 因用户希望保留字段完整性，本集合后续优化优先考虑拆表与去重，不以字段裁剪作为默认方向。

后续演进约束：

- 历史对局详情是“对局当时快照”，不要直接改成从角色当前画像表读取装备、奇穴、分数等动态状态，否则会把历史对局展示成当前角色状态。
- 可以把角色身份字段（如 `global_role_id`、`role_id`、`person_id`、服务器、角色名）与已有 `role_identities` 关联，但对局时的 `kungfu`、`mmr`、`score`、`total_score`、`equip_score`、`max_hp`、`mvp`、`fight_seconds` 等仍属于对局快照。
- `players_info[].role_name` 可继续保留推栏原始展示名，例如 `角色名·服务器`；运行时若要派生写入 `role_identities` 或 `jjc_sync_role_queue`，必须先规范化为纯角色名，不直接回写快照源数据。
- 第一阶段拆表已完成：`jjc_equipment_snapshot` 保存完整 `armors` 数组，`jjc_talent_snapshot` 保存完整 `talents` 数组，`jjc_match_detail` 的玩家节点保存对应 `*_snapshot_hash`。读取详情时由 `JjcInspectRepo` 批量查快照并拼回原 API 结构。历史详情缓存不做兼容迁移，相关一次性清理脚本已删除。
- `snapshot_hash` 必须基于规范化后的完整数组生成：装备按稳定字段（优先 `pos`，再 `ui_id`/`name`）排序，奇穴按稳定字段（优先 `level`，再 `id`/`name`）排序，JSON 序列化使用固定 key 顺序和固定分隔符。
- 后续若需要“点了某个奇穴的对局数”“有 CW 的对局数”等统计，可在 snapshot 表补充可查询索引字段（如 `talent_ids`、`talent_names`、`item_ui_ids`、`item_names`、`has_cw`），或再建 `jjc_match_player_fact` 事实表。事实表应视为可重建的查询索引，不应替代完整详情与快照源数据。
- 若大量爬取导致热表继续增长，应考虑冷热分层：热集合保留近期可快速查询结构，归档集合或对象存储保留完整历史详情。无论是否归档，`match_id` 仍是幂等主键。

计划文档：`docs/exec-plans/active/jjc-match-detail-storage-plan.md`。

### `jjc_match_participants`

用途：JJC 本地已收录对局的玩家级轻量投影表。每场 3v3 对局最多 6 条，用于按 `global_id` 快速分页查询某个角色参与过的本地可展示对局列表。`jjc_match_detail` 仍是详情内容主事实；本集合是可重建读取模型。

读写归属：

- `src/storage/mongo_repos/jjc_match_participant_repo.py`
- 业务编排：`src/services/jx3/match_detail_participant_projection.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `match_id` | int | 对局 ID |
| `global_id` | string | replay 稳定角色 ID，只写非空值 |
| `identity_id` | ObjectId/null | 可选角色身份 ID，第一版通常为 null |
| `identity_key` | string/null | 可选角色身份 key |
| `source_identity_id` | ObjectId/null | `jjc_sync_match_seen.source_identity_id` 冗余 |
| `source_identity_key` | string/null | `jjc_sync_match_seen.source_identity_key` 冗余 |
| `sync_status` | string | 同步状态冗余；无 seen 文档时固定为 `not_synced` |
| `detail_available` | bool | 当前 `jjc_match_detail` 是否存在可展示详情 |
| `detail_source` | string | 详情写入来源，如 `sync_worker`、`ranking_detail`、`warmup`、`manual`、`unknown` |
| `server` | string/null | 对局详情中的服务器 |
| `role_name` | string/null | 对局详情中的原始展示名或规范化名 |
| `team_key` | string | `team1` 或 `team2` |
| `won` | bool | 所在队伍是否获胜 |
| `kungfu` | string/null | 当前场心法 |
| `kungfu_id` | string/null | 当前场心法 ID，来自对局详情玩家节点 `kungfu_id` |
| `match_type` | int/null | 对局类型，3 表示 3v3 |
| `match_type_inferred` | bool | `match_type` 是否由双方玩家数量推断 |
| `match_time` | int/null | 对局时间 |
| `season_id` | string/null | 对局所属赛季。仅当 `match_time >= config.CURRENT_SEASON_START` 时写入当前 `config.CURRENT_SEASON`；赛季前或缺少对局时间时为 `null`，不会出现在当前赛季已同步对局列表中。 |
| `start_time` | int/null | 开始时间 |
| `duration` | int/null | 时长 |
| `avg_grade` | int/null | 平均段位/分段 |
| `tuilan_score` | int/null | 推栏分数，来自对局详情玩家节点 `mmr` |
| `game_score` | int/null | 游戏分数，优先来自 `total_score`，缺失时兜底 `score` |
| `game_score_source` | string/null | `game_score` 来源字段，如 `total_score` 或 `score` |
| `raw_mmr` | int/null | 对局详情玩家节点原始 `mmr` |
| `raw_score` | int/null | 对局详情玩家节点原始 `score` |
| `raw_total_score` | int/null | 对局详情玩家节点原始 `total_score` |
| `total_mmr` | int/null | 当前场总评分 |
| `mmr_delta` | int/null | 当前场评分变化 |
| `mvp` | bool | 是否 MVP |
| `detail_saved_at` | float/null | 同步详情保存时间 |
| `cached_at` | float/null | `jjc_match_detail` 缓存时间 |
| `updated_at` | float | 投影更新时间 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_match_global_id` | `match_id`, `global_id` | unique |
| `idx_global_available_time` | `global_id`, `match_type`, `detail_available`, `match_time`, `match_id` | 普通复合索引，`match_time` 与 `match_id` 降序 |
| `idx_global_season_available_time` | `global_id`, `season_id`, `match_type`, `detail_available`, `match_time`, `match_id` | 普通复合索引，供当前赛季角色已同步对局分页查询使用；`match_time` 与 `match_id` 降序 |
| `idx_available_time_tuilan_score` | `match_type`, `detail_available`, `match_time`, `tuilan_score` | 普通复合索引，`tuilan_score` 降序 |
| `idx_available_time_game_score` | `match_type`, `detail_available`, `match_time`, `game_score` | 普通复合索引，`game_score` 降序 |
| `idx_match_id` | `match_id` | 普通索引 |

说明：

- 本集合由 `jjc_match_detail` 中的可用详情派生，不以 `jjc_sync_match_seen.status='detail_saved'` 作为展示门槛。
- `jjc_sync_match_seen` 只补充同步状态展示字段；无 seen 文档的本地详情也可以生成投影。
- 投影可删除后通过 `scripts/backfill_jjc_match_participants.py` 重建。
- 赛季字段通过 `scripts/backfill_jjc_match_participant_season.py` 补齐；先执行默认 dry-run，确认统计后才可使用 `--apply --yes` 写入，并用 `--verify-only` 核验。
- 历史最高分统计使用 `match_time` 作为对局发生时间；该字段来自接口返回的 `match_time` 或 `start_time`，不要使用 `cached_at`、`detail_saved_at`、`updated_at` 作为统计窗口。

### `jjc_equipment_snapshot`

用途：保存完整装备快照，按内容 hash 去重。与 `jjc_match_detail` 解耦，避免每场对局重复存储相同装备数组。

读写归属：

- `src/storage/mongo_repos/jjc_match_snapshot_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `snapshot_hash` | string | 规范化 `armors` 数组后的 SHA-256 内容 hash，业务唯一 |
| `armors` | array | 完整装备数组，不裁剪字段 |
| `created_at` | datetime | 首次写入时间 |
| `last_seen_at` | datetime | 最近一次被对局引用的时间 |
| `schema_version` | int | schema 版本，初始为 1 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_snapshot_hash` | `snapshot_hash` | unique |
| `idx_last_seen_at` | `last_seen_at` | 普通索引 |

### `jjc_talent_snapshot`

用途：保存完整奇穴快照，按内容 hash 去重。与 `jjc_match_detail` 解耦，避免每场对局重复存储相同奇穴数组。

读写归属：

- `src/storage/mongo_repos/jjc_match_snapshot_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `snapshot_hash` | string | 规范化 `talents` 数组后的 SHA-256 内容 hash，业务唯一 |
| `talents` | array | 完整奇穴数组，不裁剪字段 |
| `created_at` | datetime | 首次写入时间 |
| `last_seen_at` | datetime | 最近一次被对局引用的时间 |
| `schema_version` | int | schema 版本，初始为 1 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_snapshot_hash` | `snapshot_hash` | unique |
| `idx_last_seen_at` | `last_seen_at` | 普通索引 |

### `jjc_sync_identity_queue`

用途：JJC 对局数据同步的当前角色队列，保存每个待同步身份的本赛季同步进度与执行租约。队列以 `role_identities._id` 为业务关联主键，队列内的角色名、服务器、SK01 等字段只作为展示和诊断冗余；worker 领取后会重新读取 `role_identities` 并刷新 indicator。

读写归属：

- `src/storage/mongo_repos/jjc_sync_repo.py`
- 业务入口：`src/plugins/jx3bot_handlers/jjc_match_data_sync.py`
- 同步编排：`src/services/jx3/jjc_match_data_sync.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `identity_id` | ObjectId | 关联 `role_identities._id`，业务唯一 |
| `identity_key` | string/null | 当前身份键，仅作展示、诊断和迁移期兼容，不再作为队列主键 |
| `server` | string | 服务器名 |
| `name` | string | 角色名，只保存纯角色名；对局详情自动发现链路写入前必须先去掉可判定的服务器展示后缀 |
| `normalized_server` | string | 规范化服务器名 |
| `normalized_name` | string | 规范化角色名 |
| `global_id` | string/null | JJC 稳定角色身份 ID，来自 `match/replay.players[].global_role_id` |
| `global_role_id` | string/null | SK01 全局角色 ID，来自 `role/indicator.role_info.global_role_id`，用于请求推栏战局历史 |
| `role_id` | string/null | 角色 ID |
| `person_id` | string/null | 推栏个人 ID；对局详情或 indicator 可提供，入口兜底时仍可用于 `person-history` 兼容补全 |
| `zone` | string/null | 区服分区 |
| `identity_source` | string/null | 同步前身份补全来源，如 `role_identity_name_match`、`live_ranking_global_role_id`、`person_history` |
| `role_info_observed_match_time` | int/null | 当前角色画像字段所依据的最近对局时间 Unix 秒；对局来源只有晚于该值才允许覆盖已有非空画像字段 |
| `role_info_source` | string/null | 最近一次角色画像补充来源，如 `match_detail`、`match_replay_indicator_backfill`、`match_replay_backfill`、`match_replay_indicator` |
| `role_info_updated_at` | float/null | 最近一次角色信息补充写入时间 Unix 秒 |
| `source` | string | 来源：`manual`、`ranking`、`match_detail` |
| `priority` | int | 调度优先级，手动添加高于自动发现 |
| `status` | string | 状态：`pending`、`queued`、`syncing`、`exhausted`、`cooldown`、`failed`、`disabled` |
| `queued_at` | float/null | 最近一次进入 worker 排队队列的时间 Unix 秒 |
| `queue_batch_id` | string/null | 最近一次批量入队批次 ID |
| `queue_mode` | string/null | 入队时指定的同步模式；新入队统一写 `full`，历史值 `incremental_or_full`、`incremental` 仅兼容读取 |
| `queue_sync_until_time` | int/null | 本次入队任务的一次性同步窗口截止时间 Unix 秒；为空表示 full；每次重新入队应覆盖或清空 |
| `queue_source` | string/null | 入队来源，如 `qq_start`、`manual_add`、`cli`、`interrupted`；中断回队列时写为 `interrupted` |
| `queue_window_migrated_at` | float/null | 历史队列窗口迁移脚本最近写入时间 Unix 秒 |
| `queue_window_migrated_by` | string/null | 历史队列窗口迁移脚本标识 |
| `priority_updated_at` | float/null | 最近一次优先级调整时间 Unix 秒 |
| `priority_updated_by` | string/null | 最近一次优先级调整来源 |
| `interrupted_reason` | string/null | worker 中断、暂停或鉴权失败释放回队列的原因 |
| `interrupted_at` | float/null | 最近一次中断释放时间 Unix 秒 |
| `season_id` | string/null | 当前赛季标识 |
| `season_start_time` | int | 当前赛季开始时间 Unix 秒 |
| `full_synced_until_time` | int/null | 已完整覆盖到的最新时间点 Unix 秒 |
| `oldest_synced_match_time` | int/null | 已同步到的最早对局时间 Unix 秒 |
| `latest_seen_match_time` | int/null | 最近看到的最新对局时间 Unix 秒 |
| `history_exhausted` | bool/null | 本赛季是否已经回溯到赛季开始 |
| `last_cursor` | int | 最近处理 cursor |
| `lease_owner` | string/null | 当前执行实例标识 |
| `lease_expires_at` | float/null | 执行租约过期时间 Unix 秒；过期的 `syncing` 角色会恢复为 `queued` 继续由 worker 处理 |
| `last_synced_at` | float/null | 最近同步时间 Unix 秒 |
| `next_sync_after` | float/null | 下一次允许同步时间 Unix 秒 |
| `fail_count` | int | 连续失败次数 |
| `last_error` | string/null | 最近错误 |
| `created_at` | float | 创建时间 Unix 秒 |
| `updated_at` | float | 更新时间 Unix 秒 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_identity_id` | `identity_id` | unique |
| `idx_identity_key` | `identity_key` | 普通索引 |
| `idx_status_priority_next_sync_after` | `status`, `priority`, `next_sync_after` | 普通复合索引 |
| `idx_status_priority_queued_at` | `status`, `priority`, `queued_at` | 普通复合索引 |
| `idx_normalized_server_name` | `normalized_server`, `normalized_name` | 普通复合索引 |
| `idx_global_id` | `global_id` | 普通索引 |
| `idx_global_role_id` | `global_role_id` | 普通索引 |
| `idx_lease_expires_at` | `lease_expires_at` | 普通索引 |

### `jjc_sync_role_queue`

用途：JJC 对局数据同步旧角色队列。当前运行时代码不再以该集合作为主队列；集合保留为 `scripts/migrate_jjc_sync_identity_queue.py` 的迁移源和代码版本回滚前反向同步目标。

业务主键：`identity_key`。旧集合没有 `identity_id`，不能被当前 identity-id 队列代码直接作为运行时集合使用。

### `jjc_sync_match_seen`

用途：保存已发现对局与详情同步状态，避免重复请求详情。

读写归属：

- `src/storage/mongo_repos/jjc_sync_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `match_id` | int | 对局 ID，业务唯一 |
| `source_identity_id` | ObjectId/null | 首次发现该对局的 `role_identities._id` |
| `source_identity_key` | string/null | 首次发现该对局的角色身份键 |
| `source_server` | string/null | 首次发现来源服务器 |
| `source_role_name` | string/null | 首次发现来源角色名 |
| `status` | string | 状态：`discovered`、`detail_syncing`、`detail_saved`、`failed`、`detail_unavailable` |
| `match_time` | int/null | 对局时间 Unix 秒 |
| `lease_owner` | string/null | 当前执行实例标识 |
| `lease_expires_at` | float/null | 执行租约过期时间 Unix 秒 |
| `discovered_at` | float | 首次发现时间 Unix 秒 |
| `detail_saved_at` | float/null | 详情保存时间 Unix 秒 |
| `detail_retry_after` | float/null | 失败后下次重试时间 Unix 秒 |
| `detail_unavailable_reason` | string/null | 详情不可用原因 |
| `detail_unavailable_code` | int/null | 详情不可用代码 |
| `detail_unavailable_at` | float/null | 标记不可用时间 Unix 秒 |
| `fail_count` | int | 连续失败次数 |
| `last_error` | string/null | 最近错误 |
| `updated_at` | float | 更新时间 Unix 秒 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_match_id` | `match_id` | unique |
| `idx_status_match_time` | `status`, `match_time` | 普通复合索引 |
| `idx_source_identity_id` | `source_identity_id` | 普通索引 |
| `idx_source_identity_key` | `source_identity_key` | 普通索引 |
| `idx_lease_expires_at` | `lease_expires_at` | 普通索引 |

### `jjc_sync_state`

用途：保存同步全局状态，如暂停/恢复开关。

读写归属：

- `src/storage/mongo_repos/jjc_sync_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `key` | string | 状态键，当前固定为 `global`，业务唯一 |
| `paused` | bool | 是否全局暂停 |
| `reason` | string | 暂停原因 |
| `updated_at` | float | 更新时间 Unix 秒 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_key` | `key` | unique |

### `jjc_sync_workers`

用途：记录 JJC 对局同步 worker 进程心跳、当前处理角色和最近结果，供状态命令与队列页面展示。

读写归属：

- `src/storage/mongo_repos/jjc_sync_repo.py`
- 同步编排：`src/services/jx3/jjc_match_data_sync.py`
- API 入口：`src/api/routers/jjc_sync.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `worker_id` | string | worker 实例 ID，业务唯一；bot 内置 worker 使用稳定槽位 `bot:{host}:{index}`，独立 CLI worker 可使用进程级 ID |
| `mode` | string | worker 同步模式 |
| `status` | string | 状态：`starting`、`running`、`idle`、`syncing`、`paused`、`stopped`；状态页只把 5 分钟内有心跳的非 stopped worker 视为活跃 |
| `pid` | int/null | 进程 ID |
| `host` | string/null | 主机名 |
| `current_identity_id` | ObjectId/string/null | 当前处理角色的 `role_identities._id`；API 输出时转字符串 |
| `current_identity_key` | string/null | 当前处理角色 identity_key |
| `current_server` | string/null | 当前处理角色服务器 |
| `current_name` | string/null | 当前处理角色名 |
| `started_at` | float | worker 本轮启动注册时间 Unix 秒；同一稳定 `worker_id` 重新注册时会刷新 |
| `heartbeat_at` | float | 最近心跳时间 Unix 秒 |
| `last_result` | object/null | 最近一次角色同步摘要 |
| `last_error` | string/null | 最近 worker 或角色错误 |
| `stop_reason` | string/null | worker 停止原因 |
| `updated_at` | float | 更新时间 Unix 秒 |

状态 API 会派生返回 `online` 与 `effective_status`：`online` 表示 `heartbeat_at` 距当前 5 分钟内且 `status` 属于 `starting/running/idle/syncing/paused`；过期心跳展示为 `effective_status=offline`，该派生字段不写入 MongoDB。同一稳定 `worker_id` 重新注册时会清空 `current_identity_id/current_identity_key/current_server/current_name/last_error`，避免重启后展示上一轮残留处理对象。

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_worker_id` | `worker_id` | unique |
| `idx_heartbeat_at` | `heartbeat_at` | 普通索引 |
| `idx_status_heartbeat_at` | `status`, `heartbeat_at` | 普通复合索引 |

### `announcements`

用途：系统公告与更新说明，通过前端 SPA 展示并支持 QQ 机器人管理员命令维护。

读写归属：

- `src/storage/mongo_repos/announcement_repo.py`
- API 入口：`src/api/routers/announcements.py`
- QQ 管理命令：`src/plugins/announcement_admin.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `announcement_id` | string | UUID 十六进制字符串，业务唯一键 |
| `title` | string | 公告标题 |
| `content` | string | 公告正文 |
| `date` | string | 日期键如 `2026-05-11`，用于分组和最新日期比较 |
| `created_at` | float | 创建时间 Unix 秒 |
| `created_by` | string | 创建者 QQ 号 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_announcement_id` | `announcement_id` | unique |
| `idx_date_created_at` | `date` (降序), `created_at` (降序) | 普通复合索引 |

### `jjc_ranking_stat_summaries`

用途：保存 JJC 排名统计快照的首屏摘要与历史列表元数据。新生成统计只写入本集合；`data/jjc_ranking_stats/` 历史文件仅作为一次性迁移输入，不再作为运行时 fallback。

读写归属：

- `src/storage/mongo_repos/jjc_ranking_stats_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `timestamp` | int | 统计快照时间戳，沿用原文件目录名 |
| `generated_at` | float | 统计生成时间 Unix 秒 |
| `ranking_cache_time` | float/int/null | 排行榜缓存时间 |
| `default_week` | int/string/null | 默认周次 |
| `current_season` | string/int/null | 当前赛季标识 |
| `week_info` | string | 周次展示文案 |
| `kungfu_statistics` | object | 去除 members 明细后的 summary 统计结构 |
| `source` | string | 写入来源，如 `ranking_job` 或 `migration` |
| `schema_version` | int | 当前为 1 |
| `created_at` | datetime | 首次写入时间 |
| `updated_at` | datetime | 最近更新时间 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_timestamp` | `timestamp` | unique |
| `idx_generated_at` | `generated_at` (降序) | 普通索引 |
| `idx_ranking_cache_time` | `ranking_cache_time` (降序) | 普通索引 |
| `idx_current_season_default_week` | `current_season`, `default_week` | 普通复合索引 |

### `jjc_ranking_stat_details`

用途：保存 JJC 排名统计快照的按需明细。每条文档对应一个 `timestamp + range + lane + kungfu`，避免单个 summary 文档过大。

读写归属：

- `src/storage/mongo_repos/jjc_ranking_stats_repo.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `timestamp` | int | 统计快照时间戳 |
| `range` | string | 排名范围，如 `top_200` |
| `lane` | string | `healer` 或 `dps` |
| `kungfu` | string | 心法名称 |
| `members` | array | 该范围、分组、心法下的成员明细 |
| `source` | string | 写入来源，如 `ranking_job` 或 `migration` |
| `schema_version` | int | 当前为 1 |
| `created_at` | datetime | 首次写入时间 |
| `updated_at` | datetime | 最近更新时间 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_timestamp_range_lane_kungfu` | `timestamp`, `range`, `lane`, `kungfu` | unique |
| `idx_timestamp` | `timestamp` | 普通索引 |
| `idx_timestamp_range_lane` | `timestamp`, `range`, `lane` | 普通复合索引 |

### `jjc_peak_score_rankings`

用途：保存 JJC 14 天历史最高分排名结果。每天 09:00 读取最近两个排名统计快照的 `timestamp` 作为统计锚点，分别按推栏分数和游戏分数统计本地已同步 3v3 对局参与者的最高分排名。新口径窗口取锚点前 14 天与快照所属赛季的交集；已存 legacy 历史结果保留原口径。本集合独立于心法排名快照，不以排名快照成员作为统计对象。

读写归属：

- `src/storage/mongo_repos/jjc_peak_score_ranking_repo.py`
- `src/services/jx3/jjc_peak_score_ranking.py`
- 定时任务入口：`src/plugins/status_monitor/jobs.py`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | MongoDB 自动主键 |
| `anchor_timestamp` | int | 统计锚点，来自 `jjc_ranking_stat_summaries.timestamp` |
| `window_start` | int | 修正口径为 `max(anchor_timestamp - 14天, season_start_time)`；legacy 历史保留原窗口 |
| `window_end` | int | 统计窗口终点，等于 `anchor_timestamp` |
| `score_type` | string | `tuilan` 或 `game` |
| `window_days` | int | 当前固定为 14 |
| `version` | int | 统计口径版本，当前为 1 |
| `aggregation_scope` | string/null | 新结果为 `season_bounded_v1`；当前赛季缺少标记的旧结果不再复用 |
| `season_id` | string/null | 统计锚点摘要所属赛季；legacy 历史可缺失 |
| `season_start_time` | int/null | 所属赛季北京时间零点开赛边界，切季后仍保留供历史读取 |
| `status` | string | `processing`、`done`、`failed` |
| `items` | array | 排名项；当前只保存前 1000 名，页面的前 200/100/50 从该数组切片展示 |
| `item_count` | int | 排名项数量 |
| `source_match_count` | int | 参与统计的去重对局数 |
| `source_participant_count` | int | 参与统计的玩家投影行数 |
| `generated_at` | float | 统计完成时间 Unix 秒 |
| `processing_at` | float/null | 开始处理时间 Unix 秒 |
| `failed_at` | float/null | 失败时间 Unix 秒 |
| `error` | string/null | 失败原因 |
| `created_at` | datetime | 首次写入时间 |
| `updated_at` | datetime | 最近更新时间 |

`items[]` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `rank` | int | 排名 |
| `score` | int/null | 最高分 |
| `match_id` | int/null | 最高分对应对局 ID |
| `match_time` | int/null | 最高分对应对局发生时间 |
| `score_source` | string/null | 来源字段；推栏分数为 `mmr`，游戏分数为 `total_score` 或 `score` |
| `global_id` | string/null | replay 稳定角色 ID |
| `role_name` | string/null | 对局详情中的角色名 |
| `server` | string/null | 服务器 |
| `zone` | string/null | 大区 |
| `kungfu` | string/null | 心法 |
| `kungfu_id` | string/null | 心法 ID |
| `match_count` | int | 窗口内该角色参与统计的对局数 |

索引：

| 索引名 | 字段 | 约束 |
|---|---|---|
| `idx_anchor_score_type_version` | `anchor_timestamp`, `score_type`, `version` | unique |
| `idx_anchor_score_type_status` | `anchor_timestamp`, `score_type`, `status` | 普通复合索引，`anchor_timestamp` 降序 |

### `jjc_kungfu_refresh_queue`

读写归属：`src/storage/mongo_repos/jjc_kungfu_refresh_repo.py`；运行编排为 `src/services/jx3/jjc_kungfu_refresh.py`。每角色一个长期任务，`_id` 与 `role_identities._id` 为同一BSON ObjectId，不按赛季另建行。无TTL、不在线删除重建、不共用完整历史同步队列。

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId | 角色唯一主键 |
| `scope` | object | season_id/season_start/pvp_type/algorithm_version |
| `status` | string | `pending` / `running` |
| `next_run_at` | float | 周期刷新及失败重试共用的到期时间 |
| `generation` | int | 初始0，每次领取与scope切换递增，永不归零 |
| `lease_token` / `lease_expires_at` | string/null / float/null | 每次随机token与90秒租约 |
| `last_seen_in_ranking_at` / `active_until` | float | 最近上榜与48小时追踪期，重复登记只取最大值 |
| `consecutive_failures` / `last_error_code` | int / string/null | 连续失败次数及脱敏错误码 |

索引仅内建 `_id_` 与 `idx_kungfu_next_run(status,next_run_at,_id)`、`idx_kungfu_expired_lease(status,lease_expires_at)`，升序。启动校验同名索引键/选项，冲突停止本消费者，不删除索引。到期领取原子递增generation；回收只使过期running回到pending，不重置代次。追踪过期不领取，保留文档供再次上榜。

成功先条件发布角色属性，再以 `_id/status/token/generation/scope/未到期租约` 确认队列，按已保存verified_at+刷新周期安排下一次。失败只确认退避 `min(60*2^(failures-1),3600)`；认证失败任务与本进程至少冷却5分钟，429遵守1秒至1小时的Retry-After。进程冷却不跨重启持久化。

发布结果不确定时重读同代次同scope角色值确认；读回失败或确认异常保留running，等待租约恢复。允许过期A在B尚未发布时写入n，但B已成功n+1后A不能覆盖。scope变更先停旧消费者、更新配置重启再登记，CAS撤销旧token且递增generation，不支持新旧scope长期混跑。任务丢失但角色已有publish_version或队列代次低于角色版本时报完整性错误，停止所有旧写入实例后方可人工恢复到不低于已有版本，禁止在线归零。

排名快照 `jjc_ranking_stat_summaries` 可选新增 `kungfu_cache_scope`、`refresh_degraded`，每范围 `kungfu_cache(total,fresh,stale,unknown,coverage)`；lane `equipment_incomplete` 防止未知装备被误算。明细冻结 `kungfu_verified_at/kungfu_source/kungfu_freshness/kungfu_unknown_reason/kungfu_publish_version`，未知角色单独以lane=`unknown`、kungfu=`unknown`保留原名次。后台不会改历史快照，旧快照不补造新鲜度。

## 文件型持久化与非 Mongo 数据

以下数据当前仍不是 MongoDB schema，但会影响运行状态，修改时也要确认是否需要纳入本文：

- `runtime_config.json`: `/修改配置` 等运行时配置写入文件，包含可覆盖的 `MONGO_URI` 等配置。
- `data/jjc_ranking_stats/<timestamp>/summary.json` 与 `details/`: 历史 JJC 统计产物；运行时主存储为 `jjc_ranking_stat_summaries` 与 `jjc_ranking_stat_details`。
- `data/baizhan_images/baizhan_data.json`、图片缓存和 `mpimg/`: 静态或缓存资源。

## 已清理的未使用 Mongo 集合

2026-04-30 按当前代码引用、`src/infra/mongo.py:_ensure_indexes()` 和本文集合设计核对，以下 MongoDB 集合不属于运行时代码访问边界，已从配置库 `jx3bot` 删除：

| 集合 | 删除前文档数 | 清理原因 |
|---|---:|---|
| `cache_entries` | 9 | 未在当前代码、迁移脚本或数据库设计中引用 |
| `group_reminders` | 0 | 历史提醒集合名；当前使用 `reminders` |
| `jjc_kungfu_cache` | 0 | 历史/临时集合名；当前 JJC 角色缓存使用 `role_identities` 与 `role_jjc_cache` |
| `jjc_match_detail_snapshot_migration_backup` | 0 | 临时备份集合，当前代码无引用 |
| `jjc_ranking_stats` | 1 | Mongo 集合无运行时代码引用；当前 JJC 统计快照写入 `jjc_ranking_stat_summaries` 与 `jjc_ranking_stat_details` |

## 数据维护脚本

| 脚本 | 处理范围 | 写入集合 | 幂等键 |
|---|---|---|---|
| `scripts/backfill_jjc_role_id_from_match_replay.py` | 已同步 JJC match/replay 身份补充 | `role_identities`, `jjc_sync_role_queue`, `jjc_match_detail` | `global_id`, `identity_key`, `match_id` |
| `scripts/migrate_jjc_sync_identity_queue.py` | 旧 JJC 同步队列迁移到 identity-id 队列，或回滚前反向同步水位 | `role_identities`, `jjc_sync_identity_queue`, `jjc_sync_role_queue` | `identity_id`, `identity_key` |

### 心法缓存缺失直接查询的存储约定（2026-11-06修正）

不新增集合或字段。`jjc_kungfu_refresh_queue.claim_identity` 供统计直接查询按角色_id/scope预留新的generation和token，无视next_run_at/已有running；当前调用立即执行，旧token不能确认新任务。与后台共享角色条件发布和先发布后确认协议。直接查询确认异常时可读回同generation/scope的角色成功值用于本次统计，队列保持租约恢复。冷角色不再只交后台后以unknown完成统计；已有缓存仍批量登记周期刷新。

### JJC 排名统计触发的队首同步（2026-11-07）

`jjc_sync_identity_queue`（主路径）与 `jjc_sync_role_queue`（兼容路径）新增以下非 TTL 字段；读写仍归 `JjcSyncRepo`，不新增集合或迁移删除。

| 字段 | 类型 | 说明 |
|---|---|---|
| `ranking_request_order` | string/null | 本角色最后接受的排名请求；固定宽度 Unix 纳秒加随机运行 ID，同轮保留以去重，较旧请求不能覆盖 |
| `ranking_pending` | object/null | 仅 syncing 时保存最新待重排参数；含 queue_mode/source/batch_id/until、season、priority 及 front；不改变当前执行参数 |
| `queue_front_order` | string/null | 未消费排名批次的顺序；最新优先，普通任务缺失或为 null |
| `queue_front_rank` | int/null | 同批次按原始榜单名次升序；普通任务缺失或为 null |

两集合新增 `idx_status_ranking_front`：`status ASC, queue_front_order DESC, queue_front_rank ASC, priority DESC, queued_at ASC, identity_key ASC, _id ASC`。旧索引保留。worker 领取、队列分页的状态分组、位置计算共用上述顺序；普通记录缺新字段时继续按 priority/FIFO。Mongo 只在状态 queued 时领取，角色 next_sync_after 不阻止显式排名请求。

每轮排名统计产生独立请求上下文，在 QQ/定时入口传给保存补偿；不依赖 cache_time 唯一性。同一角色非 disabled/syncing 时立即 queued，清角色冷却，保留失败审计与既有水位。syncing 只写 pending；有效租约释放 CAS 比较 request_order，有 pending 则发布本轮结果并 queued，否则按原冷却/失败规则释放并清 front。过期恢复逐文档执行同样 CAS，保留最新请求。ranking_request_order 不随成功清除，避免同轮保存补偿重复执行。

回滚必须先停所有生产者和消费者，物化 pending 为普通 queued 并清 front/pending；不能仅回退旧 worker，否则 pending 无人处理。完整步骤见 runbook 与 change tasks.md。顺序使用部署主机时间，要求参与实例系统时钟同步；不提供跨主机独立时钟的全局顺序服务。


## 排行榜本地最新胜场读索引（v1）

`jjc_match_detail` 新增根字段 `equipment_read_time_v1`、`equipment_read_valid_v1`、`equipment_winner_gids_v1`、`equipment_match_id_v1`。`src/utils/jjc_equipment_time.py` 的 `equipment_source_index` 在主表保存时与原文原子写入；未知/冲突形状保持无有效证书。`build_equipment_source_update_pipeline` 提供 Mongo 4.4 保守回填表达式。它们只派生读索引元数据，不改对局事实、参与者投影或同步水位。

必须预先准备以下非唯一索引（开启新开关前备份、受控回填并核对执行计划）：

- `idx_equipment_winner_latest_v1`: `equipment_winner_gids_v1:1, equipment_read_time_v1:-1, equipment_match_id_v1:-1, _id:1`。
- `idx_equipment_team1_state_time_source_v1`: `data.detail.team1.players_info.global_id:1, equipment_read_valid_v1:1, equipment_read_time_v1:1, _id:1`。
- `idx_equipment_team2_state_time_source_v1`: 同上一索引，将 team1 换成 team2。

查询先按角色取有证书的最新胜场源 ID，同时完整收集未认证、当前异常及未来时间证据，再分块按物理 `_id` 读取原文并验证原文与元数据。每角色 limit 1 仅用于已证明合法胜场的最大值计算；异常证据不截断，最终全榜仍完整。源消失或版本变化使本次视图失败。

新快照使用装备 schema 2；回滚优先关闭 LOCAL 开关并保留兼容 reader，不把新快照交给不识别 schema 2 的旧二进制，不清事实、队列和水位。
