# 架构说明

更新时间：2026-03-09

本文描述仓库当前已经落地的分层方式，以及新增功能时必须遵守的边界。它不是未来设想，而是面向当前代码的维护说明。

## 系统入口

### `bot.py`

- 调用 `nonebot.init()`
- 注册 OneBot V11 适配器
- 通过 `src.api.register_api()` 挂载 HTTP API
- 通过 `nonebot.load_from_toml("pyproject.toml")` 自动加载 `src/plugins/`

### 插件加载

当前插件目录由 `pyproject.toml` 中的 `plugin_dirs = ["src/plugins"]` 指定。也就是说，只要插件模块位于该目录并符合 NoneBot 约定，就会被自动发现。

## 分层与职责

### `src/plugins/`

职责:

- 定义命令匹配器
- 解析 QQ 消息参数
- 调用 service
- 组织回复文本、图片、消息段

约束:

- 不在这里直接实现复杂业务规则
- 不在这里散写外部接口访问

### `src/plugins/jx3bot_handlers/`

职责:

- 按功能拆分消息处理注册逻辑
- 把 `src/plugins/jx3bot.py` 的历史巨型入口拆成多个小 handler

约束:

- handler 仍属于表现层
- 可以调用 renderer
- 可以依赖 NoneBot 事件对象
- 不要把缓存策略、聚合计算、数据校验沉进这里

### `src/services/jx3/`

职责:

- 查询流程编排
- 缓存策略
- 多数据源聚合
- 业务结构化输出

约束:

- 不依赖 `nonebot.adapters.*`
- 不直接发送消息
- 不持有图片渲染细节

典型模块:

- `singletons.py`: 组装共享对象
- `jjc_ranking.py`: 竞技排名业务流程
- `arena_recent.py`: 竞技近期记录查询
- `group_config_repo.py`: 群配置访问门面

### `src/infra/`

职责:

- 外部世界适配
- HTTP / 文件 / 截图 / 第三方接口封装

约束:

- 不写领域规则
- 不写命令分发逻辑

典型模块:

- `http_client.py`
- `jx3api_get.py`
- `screenshot.py`
- `browser_storage.py`

### `src/storage/`

职责:

- 统一存储抽象
- 封装当前仓库使用的存储访问边界

当前 `factory.py` 可装配:

- `group_binding_storage`
- `subscription_storage`
- `server_alias_cache_storage`

### `src/renderers/`

职责:

- Jinja 模板渲染
- 图片生成
- 发送前的表现层拼装

约束:

- 不新增业务判断分支
- 输入尽量保持结构化数据

### `src/api/routers/`

职责:

- 通过 FastAPI 暴露对外 HTTP 接口
- 返回统一响应结构

当前路由:

- `GET /api/arena/recent`
- `GET /api/jjc/ranking-stats`

## 当前调用链

典型消息路径:

1. NoneBot 收到消息
2. `src/plugins/jx3bot.py` 或其他插件模块匹配命令
3. 对应 handler 解析参数
4. handler 调用 `src/services/jx3/` 内服务
5. service 通过 `infra` / `storage` 拉取数据
6. handler 或 renderer 渲染文本、图片并回发

典型 HTTP API 路径:

1. FastAPI 路由接收请求
2. 路由校验参数
3. 路由调用 `services`
4. 使用 `src/api/response.py` 返回统一格式

## 必须保持的工程约束

### 依赖方向

固定依赖方向:

```text
plugins -> services -> infra/storage
renderers -> templates/assets
utils 作为底层工具被各层依赖
```

禁止出现:

- `services` 反向依赖 `plugins`
- `infra` 依赖 `services`
- `services` 直接引用 `MessageSegment`、`Bot`、事件对象

### 全局对象管理

`src/services/jx3/singletons.py` 只负责组装共享实例，例如:

- Jinja `Environment`
- `GroupConfigRepo`
- `JjcRankingService`
- `MatchDetailClient`

不要继续把新的业务逻辑塞进 `singletons.py`。

### 配置与敏感信息

配置优先来源:

1. 环境变量
2. `config.py`
3. 明确记录过的兼容默认值

严禁把以下内容写死在业务代码里:

- token
- ticket
- 邮箱账号
- 内网地址
- Cookie

## 现阶段技术现实

- 代码仍有历史兼容层，尚未完全去除旧工具函数与旧调用习惯
- 测试覆盖有限，很多能力仍依赖在线接口与手工回归
- `docs/exec-plans/active/refactor-plan.md` 中列出的约束，是当前做增量重构时必须继续遵守的边界

如果新增模块，请优先让结构更清晰，而不是继续扩大历史入口文件和兼容层。


JJC排名心法缓存新增边界：`jjc_kungfu_classifier.py` / `jjc_kungfu_policy.py` 承载纯选择和时效；`jjc_kungfu_refresh.py` 编排条件发布和重试；`jjc_kungfu_refresh_runtime.py` 管理单本地循环，bot挂接生命周期。`infra/tuilan_kungfu_client.py` 仅返回两类原始证据，身份与队列repo分别持久化属性/任务；`singletons.py` 只装配，策略按需校验。排名服务批量读画像并登记任务，展示消费冻结coverage/成员证据。设计与验证记录为 `openspec/changes/jjc-ranking-kungfu-async-refresh/`，无新通用队列或分布式leader层。


## 排行榜装备视图

`jjc_ranking_equipment` 负责最新胜场业务验证，`jjc_ranking_equipment_view` 负责整榜统一视图与缓存，`jjc_equipment_read_runtime` 管理专用只读连接生命周期。API/两个网页入口共用同一视图版本的装备、橙武计数和紫武筛选。`jjc_ranking_display` 和 foreground runtime 提供已发布的查询展示依赖；本次提交不包含未发布 prefetch。装备规则和索引表达式置于 utils，仓储负责完整读取与严格异常传播。
