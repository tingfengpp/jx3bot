# 重构计划

更新时间：2026-05-23

本文记录当前仓库的重构边界、已经落地的成果、仍然存在的遗留问题，以及后续增量改造时的优先级。它描述的是“当前真实状态”，不是一次性大重写方案。

## 目标

- 固定 `plugins / services / infra / storage / renderers` 的分层边界
- 收敛历史大文件和兼容层
- 统一日志、外部请求和缓存读写口径
- 让文档、代码结构和实际运行方式保持一致

## 必须遵守的约束

### 依赖方向

- 固定依赖方向：`plugins -> services -> infra/storage`
- `utils` 只作为底层工具模块被依赖
- `services` 禁止依赖 `nonebot.adapters.*`、`Bot`、`Event`、`MessageSegment`
- `renderers` 只负责渲染和表现层拼装，不新增业务规则

### 共享对象边界

- `src/services/jx3/singletons.py` 只做实例组装，不继续变成“大杂烩”
- 新的外部系统接入优先进入 `src/infra/`
- 新的存储访问优先进入 `src/storage/`

### 配置与安全

- token、ticket、邮箱、Cookie、内网地址不得硬编码
- 配置优先来自环境变量和 `config.py`
- 新发现的敏感信息优先做“提取配置 + 文档补充”，不要继续扩散

### 文档联动

- 改动运行方式时，同时更新 `README.md`、`README-Docker.md`、`docs/references/runbook.md`
- 改动分层边界时，同时更新 `project-architecture.md`
- 改动重构目标或完成状态时，同时更新本文

## 当前状态总览

### 已基本落地

- `src/plugins/jx3bot.py` 已变为总入口 + handler 注册器，不再承载全部业务实现
- `src/plugins/jx3bot_handlers/` 已承担大部分消息命令的薄表现层逻辑
- `src/services/jx3/` 已承接竞技、公告、名片、群配置等主要业务编排
- `src/infra/http_client.py` 已引入，HTTP 能力开始收口
- `src/storage/` 已承担统一存储边界
- `src/plugins/status_monitor.py` 已拆为模块包
- 文档体系已拆分为 `AGENTS.md`、`project-architecture.md`、`project-roadmap.md`、`project-history.md`、`docs/design-docs/`、`docs/references/`、`docs/exec-plans/`、`docs/tasks/`

### 仍是遗留热点

- `print()` 仍散落在多个模块，日志口径没有完全统一
- 仍存在直接 `open(...)` 读写缓存和运行文件的实现
- `src/utils/defget.py` 仍在部分 handler 中作为兼容入口存在
- `wanbaolou/` 内仍有大量调试式输出和历史写法
- `status_monitor` 虽已拆包，但缓存、通知、调度边界仍可继续收敛

## 分项进展

### 1. `services` 共享依赖与 singletons 边界

状态：部分完成，需继续守边界

已完成：

- `jjc_ranking_service` 已在 `src/services/jx3/singletons.py` 统一装配
- `group_config_repo` 已在 `src/services/jx3/singletons.py` 暴露
- `src/plugins/jx3bot.py` 的帮助命令已通过 `group_config_repo.load` 获取群配置

后续要求：

- 不再向 `singletons.py` 塞入新业务函数
- 如需兼容旧接口，放到原模块或单独 `compat.py`
- 新共享对象先落到 `infra` 或 `storage`，再由 `singletons` 组装

### 2. `status_monitor` 拆包与收口

状态：拆包完成，收口未完成

已完成：

- `src/plugins/status_monitor/` 已拆分为 `__init__`、`jobs.py`、`commands.py`、`notify.py`、`storage.py`
- 定时任务与命令入口不再堆在单文件里

仍需处理：

- `storage.py` 里仍有直接文件读写和 `print()`
- `notify.py`、`jobs.py`、`commands.py` 之间仍有继续下沉到 `services/infra` 的空间
- 若后续继续维护该模块，优先抽离“缓存访问”和“告警发送”边界

### 3. `groups.json` 读写链路统一

状态：主体完成，仍有兼容层残留

已完成：

- 群配置主路径已经收敛到 `GroupConfigRepo`
- `status_monitor` 已通过存储单例接入统一存储而不是私有 `load_groups/save_groups`

遗留项：

- `src/services/jx3/group_binding.py` 仍保留兼容门面
- 需要避免新增新的裸 `open("groups.json")`

### 4. 共享 HTTP Client 收口

状态：初步完成，尚未全量替换

已完成：

- `src/infra/http_client.py` 已存在
- 新分层设计已明确外部请求应优先走 `infra`

遗留项：

- 仍有部分历史请求链路通过旧工具函数或模块内自实现发出
- 后续新增网络请求不得绕过 `infra`

### 5. `defget.py` 瘦身

状态：进行中

已完成：

- 一部分纯函数已拆到 `src/utils/`
- 一部分 IO / 截图 / 请求逻辑已拆到 `src/infra/`

遗留项：

- `src/plugins/jx3bot.py`
- `src/plugins/jx3bot_handlers/queries.py`
- `src/plugins/jx3bot_handlers/mingpian.py`
- `src/plugins/jx3bot_handlers/trade.py`
- `src/plugins/jx3bot_handlers/baizhan.py`

这些模块仍直接从 `src.utils.defget` 导入能力，后续应按类型迁移到 `infra` 或更小的 `utils` 模块。

### 6. `jjc_ranking` 职责拆分

状态：主体完成，仍需清理余波

已完成：

- 已有 `JjcRankingService`
- 已有 `JjcCacheRepo`
- 渲染逻辑已位于 `src/renderers/jx3/jjc_ranking.py`
- handler 与 service 已基本分开
- 2026-05-22 已清理 handler 与 renderer 中当前可见的调试式 `print()`

遗留项：

- 统计文件读写仍由文件系统直接承载，后续如要增强可观测性，可考虑单独抽 repo

### 7. 手工回归清单

状态：已建立基础版本，需随改动维护

已完成：

- 手工回归路径已迁移到 `docs/references/runbook.md`

后续要求：

- 每次影响命令词、缓存路径、API、启动方式时，同步更新 `docs/references/runbook.md`
- 不再把回归清单重复维护在多个文档中

## 当前明确遗留问题

以下问题不是抽象风险，而是仓库中当前可见的实际遗留点：

- `src/utils/defget.py` 仍有调试式输出
- `src/plugins/wanbaolou/` 内仍大量使用 `print()`

这些位置在继续重构时应优先替换为 `nonebot.logger` 或 `loguru`，并补上下文信息。

## 2026-05-22 执行批次：P1 日志收口

状态：已完成。

本批次目标：

- 将当前仍可见的调试式 `print()` 替换为结构化日志，优先覆盖 `config_manager`、`jjc_ranking`、`infra/image_fetch` 与 `services/jx3/kungfu`。
- 保持现有命令行为、用户可见回复、缓存读写路径和外部请求参数不变。
- 不扩大 `src.utils.defget` 依赖面；本批次不主动迁移 `defget` 调用，只做日志收口和同文件低风险兼容修正。
- 同步修正触碰文件中不符合 Python 3.9 基线的 `| None` 注解。

文件范围：

- `src/plugins/config_manager.py`
- `src/plugins/jx3bot_handlers/jjc_ranking.py`
- `src/renderers/jx3/jjc_ranking.py`
- `src/infra/image_fetch.py`
- `src/services/jx3/kungfu.py`
- `docs/exec-plans/active/refactor-plan.md`
- `docs/tasks/in-progress/refactor-plan.md`

实施切片：

1. `config_manager`：引入 `nonebot.logger`，把重启标记、重启通知、异常处理和清理动作中的 `print()` 替换为带上下文的日志；不调整命令权限、配置文件格式和重启流程。
2. `jjc_ranking`：handler 异常改为 `logger.exception`，renderer 单图失败改为日志记录；保持消息发送与分片逻辑不变；顺手将触碰签名中的 `float | None` 改为 `Optional[float]`。
3. `infra/image_fetch` 与 `services/jx3/kungfu`：补日志入口，将缓存读取、缺失 URL、下载失败、下载成功和推栏历史异常改为日志；不调整请求超时、缓存目录和返回值。

验证方式：

```bash
rg -n "\bprint\(" src/plugins/config_manager.py src/plugins/jx3bot_handlers/jjc_ranking.py src/renderers/jx3/jjc_ranking.py src/infra/image_fetch.py src/services/jx3/kungfu.py
python -m py_compile src/plugins/config_manager.py src/plugins/jx3bot_handlers/jjc_ranking.py src/renderers/jx3/jjc_ranking.py src/infra/image_fetch.py src/services/jx3/kungfu.py
```

回归关注：

- `/重启`、启动后的重启完成通知、`/查看配置`、`/修改配置` 行为不变。
- `竞技排名`、`竞技排名 拆分` 的失败提示仍发送给用户，详细错误进入日志。
- `名片` 图片缓存命中、URL 缺失、下载失败时仍返回原有 `None` 语义。
- 推栏 `match/history` 请求异常时仍返回 `None`，由上层保持原有降级逻辑。

验证结果：

- `rg -n "\bprint\(" src/plugins/config_manager.py src/plugins/jx3bot_handlers/jjc_ranking.py src/renderers/jx3/jjc_ranking.py src/infra/image_fetch.py src/services/jx3/kungfu.py`：无匹配。
- `python -m py_compile src/plugins/config_manager.py src/plugins/jx3bot_handlers/jjc_ranking.py src/renderers/jx3/jjc_ranking.py src/infra/image_fetch.py src/services/jx3/kungfu.py`：通过。
- 2026-05-23 用户确认验证正常，本批次标记完成。

## 下一步优先级

### P1

- 统一替换遗留 `print()`，后续优先覆盖 `src/plugins/wanbaolou/` 与 `src/utils/defget.py`
- 收敛 `defget` 导入面，减少新的兼容依赖

### P2

- 为缓存文件访问建立更清晰的 repo/adapter 边界
- 把 `status_monitor` 的通知与缓存访问继续下沉

### P3

- 为高频手工回归路径补自动化最小集
- 视后续需要再评估持久化能力扩展方向

## 决策原则

- 优先做“让结构更清晰的小步改动”，不要为了重构而重构
- 新代码必须服从分层，不接受“先塞进去以后再说”
- 文档若不能指导 agent 和维护者完成真实任务，就说明文档还不够好


JJC排名心法缓存新增边界：`jjc_kungfu_classifier.py` / `jjc_kungfu_policy.py` 承载纯选择和时效；`jjc_kungfu_refresh.py` 编排条件发布和重试；`jjc_kungfu_refresh_runtime.py` 管理单本地循环，bot挂接生命周期。`infra/tuilan_kungfu_client.py` 仅返回两类原始证据，身份与队列repo分别持久化属性/任务；`singletons.py` 只装配，策略按需校验。排名服务批量读画像并登记任务，展示消费冻结coverage/成员证据。设计与验证记录为 `openspec/changes/jjc-ranking-kungfu-async-refresh/`，无新通用队列或分布式leader层。


## 排行榜装备视图

`jjc_ranking_equipment` 负责最新胜场业务验证，`jjc_ranking_equipment_view` 负责整榜统一视图与缓存，`jjc_equipment_read_runtime` 管理专用只读连接生命周期。API/两个网页入口共用同一视图版本的装备、橙武计数和紫武筛选。`jjc_ranking_display` 和 foreground runtime 提供已发布的查询展示依赖；本次提交不包含未发布 prefetch。装备规则和索引表达式置于 utils，仓储负责完整读取与严格异常传播。
