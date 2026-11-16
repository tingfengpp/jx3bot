# JX3Bot Docker 部署指南

本文只描述当前仓库里已经存在的容器运行方式，不扩展额外编排方案。

## 当前容器行为

- 容器内主进程由 `start.sh` 启动
- `start.sh` 会先确保 `mpimg/` 存在
- 再后台启动 `python -m http.server 8000` 暴露 `mpimg/`
- 最后执行 `python bot.py` 启动 NoneBot 与 HTTP API

因此，容器内会同时开放:

- `5288`: NoneBot / HTTP API
- `8000`: `mpimg/` 静态文件

## docker-compose 启动

```bash
docker compose up --build -d
docker compose logs -f
```

停止与重建:

```bash
docker compose down
docker compose up --build -d
```

## compose 配置摘要

当前 `docker-compose.yml` 的关键设置:

- 容器名: `jx3bot`
- 时区: `Asia/Shanghai`
- 环境变量:
  - `ENVIRONMENT=prod`
  - `DRIVER=~fastapi+~websockets`
  - `HOST=0.0.0.0`
  - `PORT=5288`
  - 可选 `JJC_SYNC_WORKER_COUNT=1`，让 bot 启动后内置 1 个 JJC 同步 worker；默认 `0` 不启动
  - 可选 `JJC_SYNC_DISPATCHER_ENABLED=1`，启用 bot 内置自动补队列 dispatcher；默认 `1`
  - 推栏请求沿用直连实现，无共享预算桶；签名密钥和设备 ID 有兼容默认值，可通过环境变量覆盖，`TICKET` 通过 `runtime_config.json` 配置
  - `JJC_LIVE_REFRESH_ENABLED=1` 才开启最新对局轻量刷新，并必须配置 `JJC_LIVE_SOURCE_SECRET`
- 端口映射:
  - `5288:5288`
  - `8000:8000`

## 数据挂载

当前 compose 文件挂载了以下路径:

- `/mnt/sata6-1/jx3bot:/app`
- `./mpimg:/app/mpimg`
- `./log.txt:/app/log.txt`
- `./server_data.json:/app/server_data.json`
- `./groups.json:/app/groups.json`

这意味着:

- 应用代码目录会整体挂载到容器内 `/app`
- 名片图片缓存、日志、服务器数据和群配置都直接保存在宿主机

如果调整这些挂载，请同步更新:

- `docker-compose.yml`
- `start.sh`
- `README.md`
- `docs/references/runbook.md`
- `docs/tasks/all-tasks.md`（如影响部署任务或回归路径）

## 部署前检查

角色查询 ID 恢复开关 `JJC_ROLE_IDENTITY_RECOVERY_ENABLED` 默认 `0`，支持既有运行配置白名单；修改后必须实际重启加载配置的进程。首次发布保持关闭，完成显式 CLI 单角色试点后再设为 `1` 并受控重启；仅修改该键，不用本地配置文件覆盖生产配置。开关不影响普通查询或仓储安全保护，不等于数据回滚。

发布前暂停并等待相关同步静默，记录全部写进程。独立 worker 必须升级并实际重载，或保持停止，不能仅凭磁盘文件已更新就恢复。代码挂载采用受审文件 allowlist 与生产基线组合验证，先备份，再停止容器、替换、启动，不需要重启宿主机或清库。操作与回滚见 `docs/references/runbook.md`。

1. 宿主机已安装 Docker 与 Docker Compose
2. `config.py` 已准备完成
3. `groups.json`、`server_data.json` 的读写权限正确
4. OneBot 反向 WebSocket 已指向容器可访问地址
5. 容器环境变量只维护现有运行必需项
6. `runtime_config.json` 由本地从 `runtime_config.example.json` 复制后填写，不提交真实凭证

## JJC 同步 Worker

默认容器只启动 bot 和 `mpimg/` 静态服务，JJC 同步 worker 不自动启动。启用方式任选其一:

- 在 compose 环境变量中增加 `JJC_SYNC_WORKER_COUNT=1`
- 在本地 `runtime_config.json` 中写入 `"JJC_SYNC_WORKER_COUNT": 1`
- 通过 QQ 管理命令 `/修改配置 JJC_SYNC_WORKER_COUNT=1` 写入运行时配置

内置 dispatcher 默认会和 bot 一起启动；它只负责在 `queued` 深度不足时把到期角色按最近 14 天窗口补入队列，不直接同步对局。可通过 `JJC_SYNC_DISPATCHER_ENABLED=0` 关闭。内置 worker 与 `python scripts/jjc_sync.py worker` 独立 worker 可以共存，总并发数等于所有 bot 实例和独立 worker 数量之和。通过 QQ 修改配置时当前实现会退出进程，容器需要 `restart` 策略或其他外部守护来自动拉起。同步窗口由入队记录决定，不由 worker 参数决定。

内置 worker 使用稳定槽位名 `bot:{host}:{index}`。同一容器或主机重启后会复用同一条 worker 心跳记录，不会因为 pid 或启动时间变化持续新增离线 worker；如果同一 host 上同时部署多个 bot 实例，需要先用不同 hostname 或后续实例名配置区分。

## 常见问题

### 容器启动后图片无法访问

先确认:

- `mpimg/` 在宿主机和容器内都存在
- `8000` 端口已暴露
- `start.sh` 是否正常拉起了 `python -m http.server 8000`

### 机器人启动了但 HTTP API 不通

先确认:

- `HOST=0.0.0.0`
- `PORT=5288`
- `bot.py` 已正常注册 `src/api/routers/`
- 端口没有被宿主机防火墙或其他服务占用

### 容器内依赖不完整

`start.sh` 会补装少量运行依赖，但不应依赖它来解决全部环境问题。基础依赖仍应通过镜像构建阶段和 `requirements.txt` 保证一致性。


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
