# 本地最新胜场统一装备：详细执行计划

This execution plan is a living document and must be kept up to date according to `docs/PLANS.md`.

日期：2026-11-13，Asia/Shanghai。用户已确认 V2 方案、执行/测试计划，明确授权编码和本地测试；测试先由子 agent 编写，完成后由未参与实现的子 agent 审核。禁止部署、访问生产或发送真实消息。Windows 副本仅供阅读；本目录为权威。


## Purpose / Big Picture

原排行榜的卡片装备和队友、橙武统计及紫武筛选分属不同数据链路。实施后，按冻结角色稳定身份读取本地当前赛季最新3v3胜场，得到一份整榜effective view；原卡片、四范围橙武数量/比例、紫武成员/分布、QQ和导出都从同一版本派生，不新增栏目。紫武页不得显示已确认的大橙武，未知装备不得默认紫武。排名/分数/分类心法仍是该榜冻结口径。

N=L+P+U；L/N保持原橙武比例分母，覆盖为(L+P)/N。N=10、L=2、P=5、U=3时，各消费者必须给出橙武20%、装备覆盖70%、紫武5人。N=0比例为null，不伪造0%完整覆盖。

## Progress

- [x] 用户确认V2方案及计划编写范围。
- [x] 阅读当前项目约束、数据/路由/保存/渲染与已有测试入口，形成计划初稿。
- [x] 计划与测试计划经两位子agent初审、修订及闭环复核；报告见plan-review.md。
- [x] 2026-11-13用户确认执行/测试计划并明确开始实施；禁止部署。
- [x] E0 固定实施基线和隔离环境，运行授权后的现状基线。
- [x] E1 纯规则/数据契约TDD。
- [x] E2 专用本地主表/装备strict批读与索引TDD。
- [x] E3 单文档完整发布、并发CAS和schema读者TDD。
- [x] E4 排名生成、保存和QQ同源整合TDD。
- [x] E5 整榜读取、缓存/资格/撤销和API协商TDD。
- [x] E6 两入口整份采用、筛选、交互与导出TDD。
- [x] E7 真实Mongo→ASGI→浏览器、双Python与性能验收。
- [x] E8 实现审查、文档联动及可审阅发布候选；真实发布另行授权。

## Surprises & Discoveries

工作区已有大量未提交实现，HEAD不是完整候选。PROJECT_CONTEXT.md现已记录2026-11-12心法ID全目录校正并上线；design中10626被拒绝的证据是此前时点诊断，不能在本次实施中重复覆盖目录修正。V2复用当前已校验来源/图标目录，新增测试证明10626及真实来源不被分类ID覆盖。

当前stat repo先保存summary再逐项details，异常可能被吞；ranking.save_ranking_stats及后台_save同样有捕获返回None/只日志的分支，必须明确传播V2失败，不能让QQ发出未成功保存的新版本。相同ranking cache_time会重用timestamp；Mongo4.4.30兼容性以当前项目记录为准，禁止使用Mongo5+算子或未经验证的整$$ROOT字面量CAS。

现有rank并发是既有能力，本计划只串行化本实例“资格校验+最终发布”短临界段，不将查询/心法采集/整榜构建整个串行化。普通sync每轮重新入队契约仍保留；“新装备读取零外呼/写事实/入队”不等于禁止原排名生成链的正常上游或正常入队。

## Decision Log

2026-11-12：V1仅展示且装备统计冻结的口径已失效；design-v1-superseded/review-v1-superseded仅历史。V2整份视图同时驱动三处及QQ/导出，不改变心法分类。

主表为选场权威，不依赖参与者投影覆盖；先选最近有效胜场再读装备。多批读取只保证视图内部来自本次一次装配，不声称跨集合事务快照。历史冻结、最高分隔离、单实例发布者边界保留。

V2采用现有summary集合单文档完整视图，不新增事实集合/队列/持久撤销writer。整文档实际BSON≤8MiB；候选失败不截断、不降为“无胜场”、不公开半版本。schema2读者从完整equipment_view派生，不读旧details混装。

单实例发布短锁覆盖所有ranking发布分支；同timestamp靠CAS，不同timestamp按最新资格校验。实施/发布检查确认只有一个V2排名写者；不宣称Mongo单文档CAS提供跨实例、跨timestamp全局序列化。若实际部署存在第二排名写者，先停止启用并设计同等明确的协调方案，不能混跑旧写者。

## Outcomes & Retrospective

已实施 E0–E6 首版并取得分组 RED/GREEN；独立实现审核发现 B01–B07、U01–U04、W01–W03，共14项，均已修复并独立复审关闭。真实双 Python、Mongo4.4、ASGI、Node、Chromium、QQ/网页真实 PNG 初版已运行。最终主执行者双Python各629项通过；正式热读/并发、PF03故障恢复收尾及PF05真实发布链已验证，细节和证据继承边界见acceptance。所有结果见 evidence/2026-11-13-implementation；没有生产或部署结果。

## Context and Orientation

入口为src/plugins/status_monitor/jobs.py的push_daily_jjc_ranking，以及现有手动排名handler；它们调用JjcRankingService.get_ranking_kungfu_data、save_ranking_stats、等待保存后渲染。ranking.py现有_build_summary_payload会按旧weapon_*重算橙武；V2必须分支使用统一聚合。

数据在jjc_match_detail、jjc_equipment_snapshot、role_identities；jjc_match_participants只是best-effort投影。JjcInspectRepo完整load_match_detail会校验奇穴旧schema，不可用于本次只需装备的路径。正常sync和prefetch保持原owner及水位。

src/storage/mongo_repos/jjc_ranking_stats_repo.py负责jjc_ranking_stat_summaries/details；src/api/routers/jjc_ranking_stats.py暴露read/list/seasons/details/flat/current-displays；两个HTML为public/index.html和public/jjc-ranking-stats.html；QQ renderer在src/renderers/jx3/jjc_ranking.py及竞技场排名系列模板。依赖仍为plugins→services→infra/storage，router不直接访问Mongo，renderer不做领域判定。

### 具体数据与接口契约

拟新增src/services/jx3/jjc_ranking_equipment.py承载纯函数validate_local_win、classify_equipment、aggregate_equipment_view、public_equipment_view、apply_revocations；新增src/services/jx3/jjc_ranking_equipment_view.py承载build_frozen_view、read_dynamic_view、资格/缓存/singleflight编排。singletons只注入repo/policy/clock，不在其中写规则。上述文件已创建，职责按本计划实现。

冻结成员：rank、姓名/服务器、冻结score、分类kungfu/分类ID、lane、found及分类覆盖元数据；内部可靠identity/global_id/locator绑定以member_revision和binding_revision固定。member_revision对完整有序成员、scope和分类元数据作规范化摘要，不用“心法版本号”代表身份。公开binding_revision是不具授权作用的opaque摘要，身份决定仍由服务端冻结成员派生，API不接受任意global_id集合。

每成员local_win_packet包含match_id/match_time、self来源心法/icon、equipment_state=legendary|purple|unknown、weapon_name/quality/icon及可独立缺失原因、两名已确认队友（未知心法保留名字）、source=local_win_v2。状态包含no_local_win、binding_invalid、data_inconsistent、partial；read_failed是整次取数失败，不编码成每人unknown。

分类：完整有效名称+品质调用现有is_jjc_legendary_weapon；品质5命中白名单为L，信息齐全且明确符合项目既有非大橙武口径为P，否则U。布尔quality、空/占位名称、非法品质不视为合法装备。品质/名称可判定而仅缺图标时仍L/P，使用缺图提示；不能以图标缺失借用旧装备。白名单/算法版本参与视图摘要，真实来源心法不因分类目录变化覆盖。

view包含timestamp、schema_version=2、source、scope、member_revision、equipment_view_revision、checked_at/completed_at、至多1000唯一成员、四范围统计及紫武rank集合。四范围按原始rank<=50/100/200/1000取成员，人数不足用实际N；每分类/unknown及全范围均核对N=L+P+U、P集合与紫武显示完全对应。view_revision摘要不含检查时间/耗时/自己，只含成员绑定和有效内容、scope及算法/白名单摘要；不比较哈希大小。

动态响应保留统一success/error envelope；有效数据有mode=frozen|dynamic、overlay_enabled、retired、reload_required、reason、latest_timestamp、current_member_revision、current_frozen_source、view（完整或null）。GET /ranking-stats/equipment-view?timestamp=…&equipment_contract=2&expected_member_revision=…&expected_frozen_source=…只允许服务端确认的榜单；expected_member_revision和expected_frozen_source来自已加载完整冻包的bootstrap manifest，仅作客户端版本一致性约束、不提供身份授权。服务端与当前完整冻结manifest比较，不依赖旧内容缓存或单实例记忆；不符即reload_required及当前manifest版本，view=null，不把新成员装备套给旧分类集合。动态不是对任意角色的查询。OVERLAY=0优先返回disabled、view=null、零数据库访问；前端从已加载冻结视图恢复。LOCAL=0不进行新主表/装备聚合，但schema2冻结读取必须继续可用。

现有action=read声明equipment_contract=2后读取完整冻结视图，并返回bootstrap manifest的member_revision和frozen_source（schema1规范为legacy_v1，schema2为local_win_v2）。schema1最新若允许V2转换，先由冻结完整成员/scope计算可靠member_revision再发manifest；不可证明完整则ineligible_legacy，不伪造版本。expected_frozen_source比较冻结来源，不与动态view.source比较：legacy冻包按批准方式转换成local_win_v2动态值本身不触发重载。新页面最新榜在LOCAL/OVERLAY允许时再整体采用equipment-view动态结果，不能混冻结统计与动态卡片。schema1历史走完整legacy；schema1最新转V2须完整成员/强scope，缺身份的成员置U；完整成员集合/作用域不可确证则明确ineligible_legacy，不做局部V2。

details/flat V2是冻结视图的可选派生API，必须equipment_contract=2及expected_view_revision；该参数仅定位当前完整冻结版本，不凭动态摘要去旧details或猜缓存版本。失配HTTP409/version_mismatch，客户端重新取完整视图；新页面展开、筛选和导出只用本次已取完整view，不再次取分接口拼接。list/seasons/history只返回通过schema1或schema2完整性检查的元数据，附来源/版本；未声明能力的旧read/details/flat读V2/已启用V2最新榜时HTTP409/equipment_contract_upgrade_required。旧current-displays服务器端对上述榜单返回禁用，不允许旧HTML继续单卡更新。

### strict读取与预算初值

E2先用两队global_id现有索引按100身份一块约束候选；Mongo4.4使用match/project/必要normalize/sort/group，不用setWindowFields。目标存在与won=true绑定同队，验证当前赛季及有限合法时间/ID、本人唯一、两队各三人与作用域；不能先过滤装备齐全、detail_available或目录ID后选场。字段时间优先级对照当前持久化规范和真实本地fixture固定，冲突/无法证明且影响最新判定为data_inconsistent，非3v3或明确旧赛季跳过。全游标必须完整消费且在预算内；不以任意limit当全量选场。

新批读的inspect repo与equipment snapshot repo必须注入同一专用读db/隔离目标，不能让snapshot_repo默认回到全局数据库；工厂/真链验证所有实际namespace一致。新批读只取必要字段，按match_id及装备hash去重；内联armors优先使用同场有效数据，有hash则加载对应快照，不读talents。缺文档是实际缺项，driver异常必须strict传播。不存在第三方HTTPclient、sync_repo、prefetch消费依赖。

初始可验收预算（目标，不是实测）：整次动态请求10秒、构建8秒；每个Mongo命令maxTimeMS=min(1500ms,剩余预算)，驱动connect/serverSelection各≤1500ms、socket≤2000ms、waitQueue≤500ms，pool≤4，禁止应用超时后无限后台继续读。若现有共享client不能提供这些driver上限，在src/infra/jjc_equipment_read_runtime.py增加可选lazy client生命周期适配，仅LOCAL启用才创建，业务规则仍在service，凭据仅从原配置注入，bot关闭正常close；schema2冻结读取不依赖此可选client。该适配必须有关闭零初始化测试，不得擅改现有sync共用连接池。

build_frozen_view与read_dynamic_view必须注入同一个实例级构建配额/预算管理对象（singletons只装配一次），两入口合计最多1个重构任务，不得各自一个信号量。冻结生成不借动态缓存或未发布候选作成功值；同key单flight仅合并同用途动态请求，用途purpose参与任务键。资格检查与内容缓存分离；缓存TTL≤15秒、完整视图槽≤2、等待同key任务计入10秒，动态每key等待者上限32；其他key或冻结生成等待共享配额都计入自身预算，无法及时服务返回busy/read_failed/生成失败，不能无限积压。取消最后动态等待者只取消它所属的动态共享构建/排队，不能取消另一个独立冻结生成任务；冻结生成也有独立owner/预算。保留有效旧视图，不发布不完整候选；shared任务由service持有并可靠收尾，不丢fire-and-forget任务。driver层超时与任务计数共同验收。

## Plan of Work

| 阶段 | 变更/目标文件与函数 | 依赖与退出门槛 |
|---|---|---|
| E0 | 固定dirty基线/目录/运行时/局部现有回归，不改业务 | 计划获用户确认后才执行；证据可复现且与其他改动分开 |
| E1 | 新equipment纯领域函数、契约fixtures和分类/聚合测试 | 独立测试agent先写有效RED；主实现GREEN，N/L/P/U与同场规则成立 |
| E2 | JjcInspectRepo新批量候选读、装备repo strict批读、现有索引核对，必要时infra可选读client | E1完成；真实Mongo4.4主表漏投影/装备缺项/故障/查询预算均通过 |
| E3 | stats_repo新增publish_equipment_snapshot/load_complete_view/list完整过滤；新schema2及CAS | E1；单文档、8MiB、争用、写结果未知、legacy保护，真实Mongo通过 |
| E4 | ranking.get_ranking_kungfu_data/save_ranking_stats/_save_ranking_stats_to_mongo及QQ renderer/templates，daily/manual handler | E1–E3；原入队不变、保存先于QQ、只有同版有效值、离线仍存数据 |
| E5 | 新equipment_view service、singletons、router、config、条件性bot.py/runtime启停；旧端点来源保护 | E1–E3；零副作用、旧客户端升级、资格/缓存/撤销/预算正确 |
| E6 | 两HTML共同版本的bootstrap/整份状态/DOM/紫武/导出/生命周期 | E5协议固定；旧模式保留，新模式不用旧筛选与单卡poller，桌面/手机通过 |
| E7 | 新Mongo/ASGI/Chromium真链、性能、兼容与既有回归 | E4–E6；硬门槛通过且关键组skip=0，无新失败 |
| E8 | 更新运行/结构/数据库/API文档，独立实现review，候选/回滚清单 | 不触发部署。仅形成可审阅发布包，等待发布明确授权 |

测试责任：每阶段授权实施后，测试子agent先独占相应新test文件写用例，主agent独立复现RED再实现，再独立复跑GREEN；ImportError/缺依赖不计有效RED。E1契约稳定后，可由不同agent在互不重叠新文件中并行处理E2仓储测试和E3发布测试。共享ranking.py、router、两个HTML、config由主agent串行整合；不得多个agent同时修改同一文件。每阶段先做规格符合性审查，再做质量/边界审查，确认后才进入依赖阶段。

### E0：保护已有工作区和确认前提

授权后保存git status、HEAD、目标文件hash和原有diff到本change独立证据目录（不保存env/票据）。先读当前生产发布记录但不将旧截图诊断当最新事实；确需生产核对时按未来发布授权只读取必要代码hash/schema/拓扑，不打印配置值。使用隔离工作树需将实际已验证依赖变更纳入候选，不以HEAD checkout覆盖dirty源码；不reset、clean或恢复无关文件。

本地.venv当前存在3.13解释器，Python3.9临时路径不保证存在；实施开始重新核对可用解释器、Node、Chromium、Mongo版本。正式验收必须真实3.9执行/ASGI导入与Mongo4.4语义，不能用py_compile代替或借旧日志通过。缺环境先补齐隔离环境，不接生产库、不默认升级依赖。

### E1–E2：规则和读取

测试fixtures以人工可审小集合为主：败场新于胜场、同时间ID排序、橙紫未知与图标缺失、本人在另一获胜队的反例、队友未知ID。角色名/局部role_id不作为跨服唯一标识。复用当前规范心法目录；加入分类山居与来源问水不同、10626、唐门两心法规范已修复的保持测试，不重复修目录。

查询前后复验冻结global_id与当前identity绑定，不调用会upsert的resolver。若有数据不一致明确影响某角色最新胜场，返回该角色U/原因并按整榜聚合；数据库异常则整份read_failed。纯读用mock外呼/队列/事实写spy和Mongo命令监视双层证明，不只凭函数名推定纯读。索引先explain现有两支global_id索引；若未达预算，再提出具体复合索引到src/infra/mongo.py和database-design，同步隔离迁移/关闭行为测试；没有证据不盲加索引。

### E3：完整发布和写结果未知

新增纯builder生成最终schema2文档，完整equipment_view中只保存一次完整成员数组，四范围P集合用rank引用；外层兼容字段若存在也由同一视图派生且计入BSON整文档8MiB。repository用strict方法原子写，不复用吞异常的save_snapshot旧路径。读取验证schema/source/complete、revision、rank唯一/范围、统计不变量，不能仅见complete=true就信任损坏包。

首次timestamp创建insert-only，重复键视为竞争，读回检查结果；schema1升级只允许最新当前榜且具备完整冻结成员/scope，读取原文档作完整CAS守卫（Mongo4.4必须准确比较完整顶层键集合，每个原键都存在、BSON类型及whole-value/literal表达式值一致，含null/数组/嵌套对象；不能把原值直接拼成普通query等值条件，不能仅凭键数，不用整$$ROOT与literal相等。可在聚合$expr内比较objectToArray导出的键集合并逐字段比较，但具体算子组合须先经真实4.4反例测试验证）；schema2更新按_id+schema2+expected_equipment_view_revision+member_revision守卫，覆盖完整文档，matched_count=0为conflict，不重试同一个陈旧候选。记录published_at和内容摘要。

网络中断写结果未知时，只读回核对候选revision及完整内容；相同完整候选表示本次已发布，不重复新写。不同/无法读回返回publish_unknown/失败，不报告成功也不发本次QQ；保留可重试状态，下一次生成完整重查/CAS，不重置代次或改历史。短publication lock包住最新资格检查与最终写，各排名入口都必须经过；不持有该锁做上游或整榜构建。更新完成再重新确认当前latest供后续处理，不声称跨实例原子保证。

### E4：生成与所有消费者

生成入口先检查explicit_tag：explicit_tag=True时维持原历史生成/已有冻结来源，不调用当前赛季build_frozen_view或本地主表/装备新聚合；首次历史tag也不得取今天胜场。schema1历史禁止升级，schema2历史禁止普通重跑覆盖，最终发布资格再次拒绝历史新写。无tag的当前榜才进入V2装备阶段。先形成原冻结排名/分类成员，保持原_sync_request、每轮有效角色入队、心法缓存命中/现场查询语义；新装备步骤使用只读依赖。V2成功后让stats中的成员、legendary_count_map、purple集合和覆盖全部派生于同一equipment_view。V2分支的_build_summary_payload不能再根据legacy字段二次重算；_build_detail_payloads若用于原sync补偿去重，应从同一冻结成员生成且不改变调度，不能把“不写V2旧details”误解为取消保存后的同步补偿。

save_ranking_stats与_save_ranking_stats_to_mongo对V2返回可await且失败能传到调用方的结果；不要只logger后None。daily任务payload白名单加入V2 view，否则现有仅kungfu_scope/_sync_request白名单可能丢失新数据。手动handler、daily定时都await同一发布结果；发布失败不得发本次新ranking图，原失败通知策略保持，不新增外部消息。QQ未连接时仍完成采集/保存；渲染或发送失败不回滚已保存文档、不补发旧群消息。

QQ _prepare_template_data/_calculate_legendary_percent/_equipment_incomplete在V2改为纯消费聚合，不再看旧weapon或全栏隐藏；各combined/split模板显示同一L/N及覆盖/U，不新增业务栏目。只改内部传值，不在产品页面显示revision/CAS等实现信息。

### E5：动态service、API、配置与撤销

config新增JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED默认0，仅0/1，非法安全关闭并记录不含值的告警，启动生效。读取V2冻包不受该开关阻断；只有新生成和主表动态聚合受控。QUERY不成为本地前提，OVERLAY=0仅对equipment-view动态调用窗口在相关repo/新可选client初始化与访问前返回disabled；不是全应用零初始化，不阻止LOCAL=1时冻结生成读取/发布，也不阻止schema2冻结读取。

read_dynamic_view先验榜单完整性/latest/member_revision/当前scope/身份，再读/构建缓存，返回前再验资格与开关；同key复用任务，失败不污染缓存。A被新timestamp B取代/已跨当前赛季资格时返回retired；同timestamp当前完整member_revision或冻结来源变化必须单独返回reload_required=true、reason=member_revision_changed/source_changed、current_member_revision/current_frozen_source，不能冒充历史退役/停止后永用旧冻包。客户端同时比较响应manifest与已加载冻包；不符使旧epoch和该成员版本的缓存失效，记录member_reload_pending，重新action=read&equipment_contract=2加载当前完整冻包后再申请动态。重载失败显示整体重载失败并退避重取bootstrap，不以旧成员发新动态请求；pending期间disabled/retired也不能恢复已知被替代的旧冻包，直到加载当前完整冻包。新旧member_revision的掩码不得串用。资格查询失败只read_failed，不能无理由恢复旧卡。API参数以ASCII正整数（timestamp在BSON int64正值范围）、contract精确版本和既有range/白名单约束，1000人数来自冻结成员而非客户端参数。409能力/版本错、400参数错、503故障/busy、200有效/disabled/retired均采用既有统一JSON envelope，不将嵌套dict放入可公开标量字段；不暴露数据库定位/配置/异常原文。

撤销键为timestamp+scope+member_revision+binding_revision+rank，缓存保存确认时刻/reason。已确认binding_invalid/data_inconsistent不能被read_failed、disabled或retired回退复活；对当前已有完整视图/冻结视图先应用掩码，再用同一聚合公式重算三处。若构建中已确认撤销但其他查询失败，响应只携带经过验证的撤销指令（不携带部分新装备），客户端在旧完整view上原子应用掩码并重算；若无法安全应用则整体转不可用，不能保留已证伪图标。客户端会话掩码和服务端有界内存不是持久历史修复；关页+缓存失效后历史重开可无此上下文。只有确认时刻之后对相同绑定成功重验的有效新视图可解掩码，A的rank不能影响B。

若E2需要新增可选读取runtime，E5明确修改bot.py实际startup/shutdown注册（当前_startup_mongo/_shutdown_jjc_sync_workers），启动只登记lazy factory、LOCAL=0不建新client；关闭顺序为service停止接收新构建→取消/有界收尾动态singleflight、冻结owner和等待者→关闭可选client，之后再沿原顺序关闭既有sync/live/kungfu/主Mongo。创建失败/部分初始化也必须finally关闭已创建资源；close幂等，不改变既有worker职责或停机顺序。新增tests.test_jjc_ranking_equipment_runtime验证LOCAL=1首次连接、在途查询shutdown、创建异常、重复close及关闭态零可选初始化，真实3.9也执行。若不需要新client，应记录共享client已满足driver上限的证据，仍测试service构建/等待者shutdown，不能跳过任务生命周期。

### E6：前端与导出

两HTML保持一致。新增V2完整视图状态机及node稳定键；bootstrap用equipment_contract=2识别frozen来源，最新允许动态时等待完整equipment-view后采用；legacy/ineligible使用完整旧来源并提示，不混。V2 mode禁用createRankingDisplayPoller对旧current-displays，不使用N-L/!isLegendary推紫武；所有range/lane/列表/分布都从同一个state对象读取。

15秒一次无重叠轮询，错误30/60秒退避；成功同revision仍清失败提示并处理状态。真正失去最新/关闭叠加整体恢复对应冻包（带适用掩码），不是只恢复数字。同timestamp reload_required分支必须重新加载当前完整冻包，保留UI选择后再启动动态；不能用旧member_revision恢复并永久停止poll。切tab/日期/赛季、隐藏、离开及导出增加epoch并取消旧请求；计时器回调不能累积重叠。DOM提交前构建新state，按稳定键更新或恢复展开、滚动、焦点；P→L移除当前紫武成员和合理移焦必须同一提交。

start/finish export冻结整份view及UI状态，单卡、当前范围、整体导出共用；导出期间不取live混版，导出结束/失败均finally恢复轮询和原视图/交互。既有role弹窗仍独立，不借它更新卡片或统计。

## Concrete Steps

所有命令在仓库<repository>执行，以下仅计划，**本轮未执行**。E0记录基线，E1—E7每个任务按test-plan的命令组先RED、实现GREEN、主执行者独立复跑，再静态复核diff。新模块在对应阶段创建前不能运行下面的未来命令，并不能把ModuleNotFoundError当业务RED。

领域/仓储：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_policy tests.test_jjc_ranking_equipment_repo tests.test_jjc_ranking_equipment_publish`。

生成/渲染：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_generation tests.test_jjc_ranking_equipment_render`。

service与API分开进程：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_view tests.test_jjc_ranking_equipment_runtime`；`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_router`，避免现有router测试stub污染service。

Node由Python测试启动真实Node：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_frontend`；Chromium：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_browser`。

隔离Mongo与真实链：`JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/jjc_kungfu_test .venv/bin/python -m unittest tests.test_jjc_ranking_equipment_mongo`；另一独立进程运行同变量的`tests.test_jjc_ranking_equipment_chain`。只允许checked_test_uri确认的loopback27029和本次随机测试库；不运行drop生产集合的脚本，不借dotenv默认URI。

性能：`.venv/bin/python -m tests.benchmark_jjc_ranking_equipment --output <本次证据目录>/equipment-benchmark.json`（拟新增测试工具，接口须在E7实现并与本计划一致；显式同隔离Mongo变量）。真实3.9使用实施时验证的绝对解释器路径重复E1—E5与Mongo/ASGI必要组，不复用已过期/tmp路径。

E7真链在装配边界允许注入连接checked_test_uri随机db的真实repo/service/可选runtime及clock，允许替换router默认repo构造器/singletons引用；禁止替换选场、分类、聚合、发布函数或返回预制equipment-view响应。连接前断言所有app/主/可选client同为loopback27029和登记随机db，命令监视核对实际namespace；不得import/启动bot.py触发真实worker或读取默认config.MONGO_URI。bot挂接通过独立受控hook测试验证，真链仅构建实际router的受控ASGI app，不执行生产启动器。

E8将通过的具体命令/版本/退出码/skip数填入acceptance；更新README、README-Docker、PROJECT_CONTEXT、database-design、runbook及架构说明引用，差异只涉及本次事实；OpenSpec delta实施完成后再合入specs并归档，当前不归档。

## Validation and Acceptance

测试计划TC01—TC50及PF01—PF05形成需求→阶段→测试模块→证据映射。主执行者独立复跑新增关键组，Mongo/ASGI/Node/Chromium/3.9必需组不得用skip替代。既有回归覆盖排名缓存/显式历史tag、强制同步、赛季/最高分、心法目录、两HTML导航、状态任务离线保存、旧display及prefetch边界；发现已有失败须对照固定dirty基线证明同因且不受本次影响，不能笼统以“存量”豁免。

业务硬门槛：任一V2紫武成员都在同revision的P集合，卡片永不显示L；橙武数量等于该范围/分类L成员数；所有N=L+P+U；unknown不默认为紫；QQ/网页/导出同版公式；正常sync请求数量/优先级/水位与原基线契约一致；动态新路径对上游/事实写/队列均0。

性能目标（待测）：1000角色动态冷构建P95≤5秒、P99≤8秒，缓存命中含资格校验P95≤1秒、P99≤2秒；超时完整响应≤10秒+小于0.5秒收尾误差，超时底层读资源≤2秒驱动上限后收束；20个并发同key客户端只触发1次构建，无额外排队/外呼，缓存槽≤2、BSON≤8MiB。候选发布前30轮冷构建+100轮缓存/并发样本，记录场次数量、查询计划/扫描量、CPU/RSS、响应大小和真实driver超时。当前未证明这些指标；不达标先修索引/批处理/预算，不默默放宽门槛或降为局部图标刷新。

release-plan仍只形成未来操作/观察清单。2026-11-13已获本地编码/测试授权，生产发布和实际消息仍禁止。本轮阶段结果持续记录evidence与acceptance，未完成项不冒称通过。

## 2026-11-13 实施记录（持续更新）

- 三位测试子 agent 分别先写 policy/repo/publication、generation/QQ/frontend/browser、service/runtime/router/assembly 的测试，主执行者取得 RED 后实现。源代码原始备份及脏工作区清单已保存，未覆盖已有前台/身份改动。
- 隔离 Mongo4.4.30 仅 loopback27029、自有随机库；Python3.13.2 与真实3.9.25；浏览器 Chromium134。没有启动 bot/worker、访问生产或发真实消息。
- 安全旧时间裁剪优化前，1000×200 主表候选导致8秒预算失败；优化只排除所有已出现时间别名一致合法且在开赛前的记录。冲突/非法/未来/未知证据保留，不按装备齐全/胜负或任意 limit 筛选。
- 实际浏览器发现展开详情会永久停整榜轮询，已修复并通过双入口及真实链；实际QQ链发现无心法cache时提前return漏装备覆盖，已修复。
- 独立后端审核 B01–B05 已补回归并修复：原始完整CAS基线、时间别名冲突优先、冻结分类状态及policy指纹、异常roster交领域判定、明确非3v3跳过。B06 metadata增加预算与清理；B07公开match_id十进制字符串保留int64精度。
- U01–U04前端异常恢复/撤销及W01–W03关闭生命周期均已测试先行修复并复审；LOCAL保持默认0，未部署。

## 最终本地交付

- 主执行者最终回归Python3.13.2/3.9.25各629项通过、skip0；实际3.9编译及改动空白检查通过。
- 1000×200(40当前+160历史)30冷P95/P99=4.233/4.271s；最终100真热P95/P99=.858/.964s；20动态+1冻结全部成功且真实quota峰1。PF03独立收尾27次真实Mongo50，28/28恢复，活跃资源归零；PF05实际发布锁/CAS+跨实例manifest测试通过。
- 热压测发现资格IO重复放大，补完整dynamic read singleflight，仍每轮前后校验、不将资格缓存15秒；单waiter取消不影响同flight其他请求，32cap在首次IO前，最后waiter/close分层drain。该改进属于原计划同key动态singleflight范围。
- 原失败、测试夹具修正、阶段指纹及统计继承范围均保留；不把过期cold混hot，不把构建等待任务数当配额持有者，不凭外层Future取消宣称普通driver线程终止。
- 不部署、不提交/推送、不改生产配置/事实/同步水位、不发真实消息。release-plan仍仅未来附录，change保持active。


## 2026-11-13 性能发布闭环补充

用户已授权解决全榜性能后继续部署。DEP06常见旧时间短路及sort前窄投影已由子agent先RED/双版GREEN，但真实r15仍超时。DEP07已按performance-plan.md采用两队覆盖索引枚举/全榜物理ID去重/256源分片，全1000targets重验、每片_id_存在性校验；子agent先补RED，双版51项通过，新的完整回归/独立复审与生产索引维护/全榜shadow/R1-R3待完成。主表不新增数据字段，不迁移业务事实，新增两普通索引属显式维护步骤，原1.5/2/8/10秒读预算保持。


## 2026-11-16 生产续做（实时状态）

- [x] 恢复生产专用22文件候选；双版373基线、维护各11场景与独立审核通过。
- [x] 真实182文件私有备份、逐成员SHA及apply/rollback演练；R1 LOCAL0受控发布、四workers、900秒观察通过。
- [x] 原子维护四技术字段后完成captured ObjectId回填及三索引，只改派生信息，原业务事实保留。
- [x] 首次R2公开503执行LOCAL0止损，保留原失败/回滚记录，不计R2完成。
- [x] winner批量查询测试先RED、实现、双版384全绿+边界Mongo各3项、独立静态闭环、1000真实shadow通过。修正计划DEP06与测试标签DEP11为同一改动。
- [x] R1b仅仓储单文件发布并就绪，LOCAL0，备份/prior/manifest分开保存。
- [ ] R1b完整900秒观察后开启R2b。
- [ ] R2b正式双入口真实页面/四范围/心法L/P/U/紫武全部列表一致性与900秒原生轮询、资源观察。
- [ ] R3自然生成、冻结/CAS/退役生命周期生产事件实际验收；不强制生成。


## 2026-11-16 R2d收尾

- [x] DEP12/DEP13测试先RED、最小实现、双版各414全绿、独立子agent审核。
- [x] R1d LOCAL0及R2d LOCAL1分阶段发布、各900秒观察，原门槛和完整1000人不变。
- [x] 两正式入口列表/紫武/分布/队友/网络证明、首入口900秒自然刷新通过。
- [x] 部署后目标最新本地胜场与260原始历史对照；目标不在最新榜，未称DOM通过。
- [x] 备份/真实回滚check及最终源配置/四worker复核，旧失败保留。
- [ ] R3自然榜生成、冻结/CAS和退役事件验收，不强制生成。


## R2d终验纠正与保守恢复（2026-11-16 14:20后）

独立终验发现live-r2d-browser-ranking/acceptance.json虽passed=true，却没有把request_failures纳入结论：id12为HTTP200后正文未完整读取、ERR_ABORTED，发生在切紫武附近，原工具缺主动取消的因果证据。因此上文“双入口最终验收通过/LOCAL1当前状态”已过时，R2d不能计完整终验。其画面和网络整包一致性、900秒资源与38次自然200仍是各自局部真实结果，不能替代全请求审计。

root最初由10.10092s误推客户端10秒timer；静态review纠正equipment fetch没有该timer，仅切tab/view的poller.suspend会abort。该请求不是页面close，但现有记录不足证明是主动切tab还是其他网络取消，根因标未归因，不能宣称后端超时。未改产品或放宽预算；先补验收工具负控及真实abort/action证据，再复验。

已按保守止损执行独立rollback-r2d.py：新--check通过后--apply实际完成，LOCAL=0（恢复原键缺省），ready=true，保留新兼容源码、原事实与队列。原结果/哈希、失败和前述误判全部保留。当前不是LOCAL1上线完成态；R3仍待验。


## R2e真实503止损（2026-11-16 14:47–14:48）

严格工具在首入口自然驻留期间锁存request215 HTTP503（06:47:41.675579Z），此后成功不清除；request216 HTTP502发生在稍后的受控恢复窗口，单列而不当作503根因。R2e完整900秒和双入口验收未通过，不执行第二入口，不将局部画面/目标对照当作完整发布成功。

已用新独立rollback-r2e.py实际--apply继承发布锁完成LOCAL0恢复，14:48:13+08启动，初始化/4fresh workers/源配置守卫正常，保留兼容源码、原事实、队列与水位。当前LOCAL=0。继续用当前线上源码在自有受限只读容器中作8次完整1000人诊断，完成后间隔30秒，保留原1500/2000ms与8/10s门槛，抓取白名单阶段/类型/code/frames；不改生产超时、不截断、不把诊断成功代替发布通过。


## DEP14与R1f实际进度（2026-11-16）

DEP14以测试子agent先RED后实现：只加载已通过完整对局校验的胜方且无非空inline armors的装备快照；完整候选/双方6人/最新胜场判定、同场队友、未知规则、资格和身份复查保持。新增专用snapshot reader maxTimeMS1500、batch512、完整消费和100ms close；旧strict/普通loader保持。两个context find_one明确原生1500，完整summary CAS不减。安全失败日志只固定stage/exception_kind/数字code/单调时钟elapsed，不含异常消息、URI或身份，成功/取消不记故障，日志异常不改变业务结果。

最终生产组合af8436fa263040503e795e59e167febae5f591dfa39c476da749c42aafdedaeb，双版各438/0失败/0错误/0skip，358文件前后和跨版本一致，测试安全nonce及U02实际事件等待由独立review附录B关闭。初轮3.9 U02固定等待失败原件保留，未改产品等待/预算；旧414、629、工具15+2/32和回滚23不累加。

受限候选8次1000人诊断全过：请求2.374–7.844秒、build1.272–6.665秒，snapshot实际1592份（原2294），不声明P95或把随机样本称固定提速。真实router两flow24请求全过、build1.969/2.900秒，ordinary/upstream/writes/pending0。两probe无OOM且移除、生产源与启动在探针阶段未改；上述诊断不等于历史R2e503已定位，也不代替公开页面15分钟。

R1f于2026-11-16 15:34:13+08启动，只应用四修改/一新增源文件，LOCAL=0、同容器镜像、restart0、always，初始化和4fresh workers/源配置守卫通过；阶段before-backup-r1f/prior/manifest独立保留，原182文件全备份不覆盖。真实公开seasons、season-history、legacy read三GET均HTTP200/status_code0。900秒观察于epoch1794814600.851开始，正在执行，R2f未开启。R3自然生成未验，不强制。


## R2f实际收尾

- [x] DEP14测试先RED/独立审查/最终双版438同指纹。
- [x] R1f LOCAL0及R2f LOCAL1原门槛完整观察、备份恢复准备。
- [x] 双正式入口全请求审计及整榜/紫武/橙武/队友同来源，首入口900s自然刷新。
- [x] R2f目标独立3vs260对照；目标不在最新榜，不冒称目标DOM。
- [ ] R3自然生成及冻结/CAS/退役生产生命周期，禁止强制生成。
