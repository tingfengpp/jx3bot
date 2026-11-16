# 本地最新胜场统一装备 V2：详细测试计划

> 2026-11-13 执行状态：用户已授权编码/本地测试，本文矩阵保留批准时的验收目标；文内“拟执行/未执行”描述原计划时点，不表示现在没有运行。实际RED/GREEN、未通过项及复审以 `acceptance.md` 和 `evidence/2026-11-13-implementation/` 为准。禁止部署。


日期：2026-11-12，Asia/Shanghai。design V2已获用户确认；本文件为待确认执行计划配套测试设计，已完成两位子agent静态复核。**本轮只写文档和静态复审，不创建测试源码、不运行代码/构建/测试、不接数据库、不部署或发送消息。** 下列文件、命令、结果门槛均为拟实施内容，尚无实际通过结果。

## 1. 验证目标和证据原则

以用户可观察行为为核心：原卡片位置直接显示同场胜场武器/队友；橙武统计和紫武筛选来自同一有效值，P集合中不得出现L卡片；未知装备不冒充紫武。冻结排名/分类不变，旧赛季/最高分隔离，历史视图完整恢复。仅测试文本是否包含某函数名不能证明这些行为。

每个用例关联TC编号、E阶段、业务输入、可观察断言和证据。测试子agent先写有业务断言的RED，主agent独立复现后实现GREEN，再独立复跑。新函数尚不存在时可在获准编码阶段先建立合法可导入边界，但空实现造成的业务失败才算RED；ImportError、环境缺失或旧fixture提取器未适配不计RED。

单元/spy证明调用边界，真实Mongo证明查询/CAS/原子性，真实ASGI证明路由/状态码，Node证明实际JS函数逻辑，Chromium证明DOM交互，Mongo→真实ASGI→Chromium证明跨层闭环，真实本地渲染检查图片。不用fixture接口完全替换链路后冒称端到端。

## 2. 环境、数据和隔离

实施起点固定当前dirty工作区完整指纹、HEAD、Python路径/版本、Node/Chromium/Motor/PyMongo/FastAPI版本，保存已有失败基线。当前只确认.venv存在3.13路径；Python3.9绝对路径在实施开始验证，不假定/tmp环境仍存在。关键Python规则/仓储/发布/service/API和真链均须在真实3.9与当前解释器验证；py_compile只补语法证据，不代替3.9执行。

真实Mongo必须显式JJC_TEST_MONGO_URI，并通过既有checked_test_uri的loopback:27029白名单。每个测试/并发进程只用本轮jjc_kungfu_test_<uuid>随机库，db名登记在证据。只清理自己创建的库；禁止从config/.env默认MONGO_URI继承，禁止prod连接、修改已运行实例或清空共享库。Mongo4.4兼容实例必须实际运行；若另测新版本，只作补充不能代替4.4CAS/算子语义。

数据分三类：人工可审金样（10人L2/P5/U3，四范围边界，rank446截图角色的去敏复现）、带fixed seed的随机成员/装备集合、性能大样本。真实角色仅保留必要名称/时间/同队事实，测试稳定global_id用合成值；不保存票据、URI密码、私钥、raw全库或运行配置。

所有浏览器/API测试外部网络默认拒绝，上游HTTP函数/队列/事实writer设“调用即失败”spy。Mongo CommandListener在读取窗口过滤随机库记录find/aggregate/getMore及写命令；主表/快照/身份/队列/水位/prefetch窗口前后做数据库端dbHash或规范化内容对照，计数相同不等于未写入。合法TTL本身会改变数据库的集合不作为仅计数证据；测试夹具关闭TTL自然干扰或独立记录TTL变化。

## 3. 拟新增测试模块与职责

| 模块（均拟新增） | 内容 | 实施阶段/执行方式 |
|---|---|---|
| test_jjc_ranking_equipment_policy | 胜场规范/装备三态/来源心法/聚合不变量/公开白名单 | E1；纯Python，固定seed随机行为断言 |
| test_jjc_ranking_equipment_repo | 查询选择、strict异常、内联/快照、时间/身份边界与无副作用 | E2；fake repo + 独立Mongo测试补强 |
| test_jjc_ranking_equipment_publish | schema2完整候选、CAS、尺寸、结果未知、历史/降级保护 | E3；unit，真实竞争在mongo组 |
| test_jjc_ranking_equipment_generation | 排名生成/存储/同步补偿/daily与manual等待保存 | E4；合成事件、mock发送，无真实消息 |
| test_jjc_ranking_equipment_render | QQ合/拆图模板输入及真实本地截图，未知覆盖不隐藏 | E4；capture模板数据 + 真实Chromium渲染补强 |
| test_jjc_ranking_equipment_view | 冻结与动态共享配额/单flight/预算/缓存/资格/撤销/开关 | E5；可注入clock/Events控制竞争，不靠长sleep |
| test_jjc_ranking_equipment_runtime | service/bot受控启停、在途关闭、部分创建失败与幂等close | E5；独立受控hook与真实3.9验证 |
| test_jjc_ranking_equipment_router | 真实FastAPI ASGI参数/envelope/capability/revision | E5；单独进程，不能污染service模块 |
| test_jjc_ranking_equipment_frontend | 两真实HTML提取实际JS，完整state/过滤/轮询/导出生命周期 | E6；Python调用真实Node，不另写一套生产逻辑代测 |
| test_jjc_ranking_equipment_browser | 桌面/手机，DOM统计与成员一致、交互/导出/旧HTML | E6；全部请求走本地fixture；证明UI而非DB |
| test_jjc_ranking_equipment_mongo | Mongo4.4真实数据查询、两进程CAS、写结果未知、schema读者/索引 | E2/E3/E7；显式隔离URI |
| test_jjc_ranking_equipment_chain | 真实Mongo+实际service+实际ASGI+实际两个页面/导出 | E7；允许装配边界注入真实repo/service/可选runtime到同一随机db，不替换算法或equipment-view响应 |
| benchmark_jjc_ranking_equipment | 1000角色、并发/故障预算、查询计划/RSS/响应尺寸 | E7；不是unittest correctness的替代 |

## 4. 业务与边界矩阵

下面TC01—TC50均为建议验证，当前全部未执行。表内P表示紫武集合，L表示已确认大橙武，U为装备未知；“旧值”均是有明确fixture的对照，不代表从线上读取。

| ID | 阶段/主要模块 | 输入/触发 | 必须可观察的结果 |
|---|---|---|---|
| TC01 | E1/E2 policy/repo | 最近败场、更早两场胜场 | 取时间最近胜场，不取败场、不统计最近40条上游采样 |
| TC02 | E1/E2 | 同时间两场胜场ID不同、输入顺序反转 | 正整数match_id倒序确定同一场，与遍历顺序无关 |
| TC03 | E1/E2 | 全败或主表成功空结果 | no_local_win，装备U、不入P/L；文案仅说本地未收录 |
| TC04 | E1/E2 | 旧赛季、开赛边界前1秒/恰好边界、未来/无时间/非法ID | 旧赛季/未来/无效不伪装当前胜场；边界合法；影响判断的矛盾明确data_inconsistent |
| TC05 | E1/E2 | 本人在败队、对手赢；won为字符串false/1/空 | 绝不当作本人胜场，严格布尔验证可见，无宽松bool转换 |
| TC06 | E1/E2 | 2v2/5v5、六人但一队2另一队4、重复/缺稳定ID | 不推断有效3v3；无跨队误取，data_inconsistent有理由 |
| TC07 | E1/E2 | 同名跨服、局部role_id撞号、改名/转服绑定冲突 | 只以global_id与冻结绑定证明本人；不按昵称/裸局部ID串号 |
| TC08 | E1/E2 | 新胜场主表完整但投影缺失，或投影旧胜/主表已修正败 | 主表新场可见；旧投影不能覆盖胜负或完整候选 |
| TC09 | E1/E2 | 最新胜场缺武器、旧胜场完整 | 仍保留最新场与可确认队友，装备U，不借旧武器/退回旧胜场 |
| TC10 | E1/E2 | 本人山居分类但来源问水；队友10626、未知/空心法ID | 分类不变，真实来源保留；未知图标仍保留确认队友姓名/区服 |
| TC11 | E1/E2 | 内联armors、装备hash、旧奇穴schema/缺奇穴 | 两种装备正确恢复，无奇穴依赖、无回源；同场优先级稳定 |
| TC12 | E1 policy | 白名单全部名称、品质5整数/字符串；品质4同名 | L仅在品质5且白名单；前后端沿用当前已发布目录/白名单 |
| TC13 | E1 policy | 空/占位/超长名称、bool/无效品质、完整非大橙武 | 资料不足为U而不是!isLegendary得到P；完整非大橙武按既有口径P |
| TC14 | E1/E6 | 名称/品质可判定但icon空/非法URL | 装备类别不变，缺图占位；无javascript/data注入，不借旧图标武器 |
| TC15 | E1 aggregate | N10/L2/P5/U3、无已知、N0及分类unknown | 全组N=L+P+U，20%/70%/P5；N0为null；unknown分类覆盖一致 |
| TC16 | E1 aggregate | ranks49/50/51/99/100/101/199/200/201/999/1000 | 四范围独立按原rank，实际不足人数正确，P不能重排后再截取 |
| TC17 | E1/E6 | 有最新胜场装备变动、原分数/分类固定 | 只有装备统计/P与卡片改变；原rank、score、分类人数/心法覆盖不变 |
| TC18 | E1/E5 | 成员/队友公开标量内混入dict、私有locator/token字段/XSS姓名 | 白名单拒绝/过滤嵌套穿透，DOM转义，输出不含私有配置/异常原文 |
| TC19 | E3 publish/mongo | 首次完整schema2发布，legacy details不存在/陈旧 | 一次原子可见完整view，summary/details/flat都从该文档派生且revision一致 |
| TC20 | E3 publish/mongo | 实际BSON 8MiB边界、8MiB+1、1001成员/超长字段 | 超限拒绝且不截断，不触碰此前完整版本；计整文档不是仅JSON字节 |
| TC21 | E3 mongo | 同timestamp两个独立连接/进程抢expected_revision | 只有一个匹配成功，失败者不能旧候选直接重试；读者不见半版 |
| TC22 | E3/E4 | A构建慢、B先成为latest；同timestamp成员版本变化 | 短发布锁+重新资格/CAS拒绝历史覆盖，不把内容cache当当前资格 |
| TC23 | E3 publish/mongo | 服务端写成功但客户端响应丢失；读回相同/不同/超时 | 同完整候选读回可确认；不同/未知返回失败，不重复新写，不发本次QQ |
| TC24 | E3/E5 | schema1升级、null→missing+同键数换键/missing→null/数组与嵌套对象/$前缀字符串的并发变化、无scope、LOCAL关闭写V2 | 完整键集合/存在性/类型及whole-value实际4.4CAS拒绝所有原文档变化；只有可证完整最新可升级，无scope不局部启用，禁止降级覆盖schema2 |
| TC25 | E3/E5 | schema2 complete标记但内容损坏/重复rank/错误聚合 | list/seasons/latest/read拒绝损坏版本，不能仅信complete=true |
| TC26 | E3/E5 router | 先read frozen revA，再details/flat时已revB；传动态摘要 | 409/version_mismatch，整份重取；不按timestamp拼旧details/动态缓存猜版本 |
| TC27 | E4 generation | daily/manual、原save返回None/异常，payload原白名单 | V2 view完整传入；等待真实保存结果后才渲染；失败不静默当成功 |
| TC28 | E4/E7 generation | 新装备构建/发布失败，原每轮排名强制sync请求 | 旧入队补偿/去重不被取消或双增；新读用途径不额外外呼/入队/改水位 |
| TC29 | E4 | QQ首次未连接、掉线、无群、渲染失败/发送失败 | 前三者仍存完整视图；后两者不回滚保存、不补发；测试消息全部mock |
| TC30 | E4 render | V2 N10/L2/P5/U3且旧字段相反/旧incomplete=true | QQ合图/拆图及网页20%/70%/P5，不重算旧weapon、不全栏隐藏 |
| TC31 | E5 config | LOCAL 0/1/非法、QUERY0/1、OVERLAY0、LOCAL1首次连接/在途shutdown/创建失败/重复close | 非法安全关闭；equipment-view动态窗口OVERLAY0新用途repo/可选client调用0；LOCAL1+OVERLAY0仍能生成/发布V2并读取冻包；service关闭有界收尾，client关闭幂等，旧worker生命周期不变 |
| TC32 | E5/E7 view | 20同key动态+冻结生成+其他key同时竞争，最后HTTP等待者取消及32人上限 | frozen/dynamic合计峰值构建≤1且共享配额，不以各自1个伪通过；等待计入预算；取消动态不误取消独立生成；超过等待上限busy |
| TC33 | E5 view | 15秒内命中、TTL过期、scope/source改变、同timestamp新member_revision完整发布，旧客户端命中已换新cache或另一实例 | 请求携带expected_member_revision/expected_frozen_source，服务端对当前manifest比较，资格仍验；同timestamp变更返回reload_required而非retired，重载完整冻包后再动态，旧成员掩码不串入新版本，动态source变化不误比冻结source；重载失败/pending后禁用也不能恢复已知旧成员 |
| TC34 | E5/E6 | A缓存存在或构建中B发布；latest资格查询超时 | A明确retired整体恢复；超时read_failed保留而不冒充退役 |
| TC35 | E2/E5 | 身份/候选/装备数据库分别超时、连接池等待、外层取消 | 整次read_failed且无无胜场假值；旧整份保留、driver/任务预算收束 |
| TC36 | E5/E6 | 无旧V2包首次失败、成功空胜场、实际装备missing | 首次整体错误；成功确认时同步置U与调整L/P；不混legacy统计或旧场 |
| TC37 | E5/E6 | 先确认身份无效后其他查询失败/关闭OVERLAY/退役 | 撤销仍作用卡片和L/P/U；不安全时整份不可用，不恢复证伪旧装备 |
| TC38 | E5/E6 | A撤销后切B同rank，故障、关闭开关，再同绑定成功重验 | 掩码不串榜、不被故障解除；只有更新的同绑定有效复验解除；跨会话非持久边界有断言 |
| TC39 | E5 router | 非ASCII数字/负数/超长timestamp、伪scope、任意global_id请求 | 400明确参数错，服务端只按冻结榜单成员构建，不作任意角色查询接口 |
| TC40 | E5/E6 | 旧HTML无contract请求新榜/旧最新V2，伪造contract字符串 | 409升级、无V2数据喂旧N-L算法；current-displays服务器禁单卡；合法contract2可读 |
| TC41 | E6 frontend/browser | P→L/L→P/U→P/同场补齐，在紫武页保持打开 | 同次状态提交改变卡片、L/P集合/分布，无中间帧紫武显示L |
| TC42 | E6 | 顶部统计变化角色不在视口或折叠组、四范围切换 | 整榜统计仍正确，不以可见100人代全榜；展开无需补取另一版本详情 |
| TC43 | E6 | 同revision先故障后恢复、disabled/retired、同timestamp source/member_revision_changed | 状态不因摘要相同跳过；reload_required使旧epoch失效并加载新冻包，不能停留旧成员集 |
| TC44 | E6 | 请求乱序、切日期/赛季/最高分tab、隐藏/离开 | epoch丢旧请求，no overlapping poll；最高分不发新装备请求 |
| TC45 | E6 browser | 桌面1440/手机390，展开、滚动、焦点后更新；焦点角色移出P | 展开/滚动保留，焦点合理迁移，无全页15秒重建/横向溢出 |
| TC46 | E6 browser | 单卡/范围/整体导出，导出中更新/失败 | 全artifact同revision；finally恢复UI/轮询，不能半新半旧或永久停poll |
| TC47 | E5/E6/E7 | 新榜退役A、同timestamp新成员完整发布、赛季切换、历史schema1/schema2、无scope legacy | A退役恢复冻结；同timestamp必须整份重载而非退役；历史不取今天场，掩码只在原成员版本内有效 |
| TC48 | E5/E6/E7 | LOCAL关后已有schema2、旧current-displays、兼容读者回滚 | schema2冻包仍同源可读；不以旧统计配新卡片，不能旧二进制无损假设 |
| TC49 | E7 chain | 金样主表记录→实际service/API→两真实页面→同场装备变更 | 首次和轮询后截图/数值/P集合均同revision；接口不注入预制装备响应 |
| TC50 | E7 regression | 心法目录已修、首次explicit_tag=True历史生成/已有历史schema1/schema2、无tag当前对照、最高分/sync/prefetch | 历史tag的新V2 builder/主表新聚合调用=0，不创建含今天场的历史schema2，不升级/覆盖历史；无tag当前仍可生成；其余旧事实所有权/分类/赛季/调度保持 |

## 5. 性能与资源矩阵

门槛是本次拟定验收目标，不是当前线上SLO，也没有已测通过。所有性能测试同时检查业务正确性/零副作用，不能以返回空视图或U全体规避耗时。

| ID | 数据与操作 | 样本与必须记录 | 初始门槛 |
|---|---|---|---|
| PF01 | 1000角色，各40当前赛季+160旧赛季主表候选；混合L/P/U及六人共享详情；现有两队global_id索引 | 暖机后30次强制冷构建；记录物理文档/索引基数、examined/returned、聚合plan、命令次数、CPU/RSS/BSON/HTTP字节 | 构建P95≤5s/P99≤8s；无COLLSCAN或每人count/find；结果仍与小型oracle一致 |
| PF02 | 同key20并发动态客户端+真实build_frozen_view同时到达，一组冷请求+100组热资格读，随后TTL到期 | frozen/dynamic统一start/finish/failure/active task计数，pool/等待、每key实际构建数、缓存槽和命中率 | 两个入口共享build≤1，HTTP取消不误取消独立生成；冷同key1构建；缓存含资格P95≤1s/P99≤2s；build≤1、完整缓存≤2，无无界等待 |
| PF03 | 候选/装备/资格各阶段故障，外层取消与驱动迟返回 | 控制故障60s+恢复30s，取消任务/活跃driver操作/连接/构建计数；不靠外层wait_for假装已停 | 单请求≤10s+0.5s收尾，后台读≤2s驱动上限后结束；无持续增长/漏任务，恢复能生成完整合法视图 |
| PF04 | 合法字段长度组合逼近8MiB，分别恰达/超限及最大1000成员 | 实际BSON encode整个最终doc，HTTP payload字节与峰值RSS；不以dict字符数估算 | 大文档≤8MiB才发布；超限零写/旧视图完整；缓存上限在持续不同timestamp请求后仍成立 |
| PF05 | A动态构建中由真实生成路径发布B、同timestamp新member_revision完整发布、旧客户端落到新cache/另一实例、20客户端边界轮询 | 新胜场提交时刻、下次完整view可见时刻、统计/P/card共同revision、retired返回 | 健康无退避时新本地事实及同timestamp新成员版本40s内整体可见目标；expected manifest约束在跨实例也触发reload_required、新冻包+三处同版，不保持旧成员，旧掩码不串用；每次成功读取不继续采用已查明退役A；可见与否不改全榜结果 |

E2若现有索引达不到PF01，先出隔离explain证据并在执行计划补具体索引与回滚，不直接全表扫描、降低人数、删掉旧赛季fixture或放宽门槛。PF01/PF02不达标不启用LOCAL；只跑一个100人批次不能代表1000人视图性能。

## 6. 命令与进程分组（拟执行，本轮未运行）

新模块先在对应阶段创建。每条命令保存真实命令、解释器/依赖版本、起止时间、退出码、用例数/skip、代码指纹。不要用一个discover进程混合已有大量sys.modules/router stub测试。测试代码必须显式禁止外呼/默认生产URI；启动测试前检查这一点。

E1–E3：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_policy tests.test_jjc_ranking_equipment_repo tests.test_jjc_ranking_equipment_publish`。

E4：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_generation tests.test_jjc_ranking_equipment_render`。

E5分别执行：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_view tests.test_jjc_ranking_equipment_runtime`；`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_router`。

E6：`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_frontend`；另一进程`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_browser`。Node必须实际执行两入口JS；Chromiumpageerror/console errors=0，网络捕获禁止外部访问。

真实数据库：`JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/jjc_kungfu_test .venv/bin/python -m unittest tests.test_jjc_ranking_equipment_mongo`；另一进程同URI变量运行`.venv/bin/python -m unittest tests.test_jjc_ranking_equipment_chain`。

基准（拟新增工具）：`JJC_TEST_MONGO_URI=mongodb://127.0.0.1:27029/jjc_kungfu_test .venv/bin/python -m tests.benchmark_jjc_ranking_equipment --output <本轮证据绝对目录>/equipment-benchmark.json`。由工具自行生成随机库并注册清理归属，不连接生产。大数据生成时间单列，不混入读取P95；Mongo4.4版本与explain保留。

真实3.9使用E0核实的绝对解释器替换.venv路径，按上述分组复跑policy/repo/publish/generation/view/runtime/router/mongo/chain关键组；无真实3.9环境是未完成，不能用3.13解析成功替代。修改FastAPI解析签名处额外以实际3.9导入app，避免仅py_compile漏掉类型解析问题。

## 7. 既有回归与基线差异

| 组 | 已存在的主要入口（实施时按实际文件重新核对） | 目的 |
|---|---|---|
| R1 | test_jjc_weapon_quality、test_jjc_kungfu_display、test_jjc_data_center_navigation、test_jjc_ranking_stats_frontend | 白名单/来源ID/双入口/导航与现有JS行为 |
| R2 | test_jjc_ranking_kungfu_cache、test_jjc_ranking_stats_repo、test_jjc_ranking_stats_router、test_jjc_ranking_explicit_tag | 心法获取/旧schema1/历史tag/路由兼容；stub与新路由分进程 |
| R3 | test_jjc_ranking_force_sync、test_jjc_ranking_force_sync_mongo、test_jjc_sync_worker_queue、test_jjc_match_data_sync | 本轮正常入队/代次/同步水位及owner不被新装备读改写 |
| R4 | test_jjc_peak_score_ranking、test_rebuild_jjc_peak_score_rankings | 14日最高分/赛季边界不进入动态装备链 |
| R5 | test_status_monitor_jobs_offline_statistics、test_status_monitor_jobs_admin_notification、test_status_monitor_jobs_config | 离线仍保存、保存错误可见、QQ失败不回滚 |
| R6 | test_jjc_ranking_display_read、test_jjc_ranking_display_router、test_jjc_ranking_display_frontend、test_jjc_ranking_display_browser、test_jjc_ranking_display_chain、test_jjc_match_detail_snapshots、test_jjc_snapshot_repo | legacy旧展示与快照路径保持原契约；V2替代不能误改旧链 |
| R7 | test_jjc_match_prefetch_wiring、test_jjc_match_prefetch_runtime_read、test_jjc_match_prefetch_identity_guard、test_jjc_match_prefetch_strict_read | 不消费暂存证据、不新增事实writer，关闭行为不变 |

不宣称既有所有测试在一个大进程可稳定跑；E0先形成各组独立基线。历史报告中的旧失败只有在当前指纹下独立复现且排除影响后才可作为已知限制，新增/加重失败必须修复，不能删测试/更新断言来迎合错误实现。V1测试要求“统计不随动态变化”仅对V2新分支有意替换；legacy对应断言仍保留并显式设置源，不能整体放宽所有旧测试。

## 8. 图片、真链与手工观察

Chromium1440和390宽保存相同view的全榜/紫武/展开截图，检查原位置无新增栏目、队友tooltip、缺图/unknown提示、紫武无L、覆盖文字、滚动/焦点。QQ合图及8类拆图用本地render_template_image路径生成静态产物，核对20%/70%及未知提示；发送函数mock，不调用Bot.send。

导出冻结逻辑可用本地html2canvas stub证明版本/恢复流程，但stub不是实际图片导出证据。E7须用已核实可用的本地真实html2canvas/浏览器依赖生成并查看PNG；若依赖缺失，记录未完成，不能以page.screenshot冒称实际导出通过。额外page截图仅作为UI视觉QA。

真链从合成本地主表写入开始，通过实际service计算/保存、真实ASGI路由及真实两个HTML读取；真链允许在装配边界注入指向同一随机库的真实repo/service及可选runtime、受控clock，并替换router默认repo constructor和singleton引用；upstream替为禁止调用。禁止替换选场/分类/聚合/发布实现，不注入equipment-view响应或页面state。所有app/主/可选client连接前必须断言checked_test_uri(loopback27029)+登记随机db一致，命令监视核对namespace与真实实例类型；不得import或启动bot.py触发真实worker/默认配置Mongo。启停hook用独立受控测试覆盖。更新主表/装备后等待受控clock轮询，比较同revision的统计/集合/DOM，并用命令监视和集合内容指纹证明GET零写入。

未来生产手工验收仅在发布另获授权后进行，步骤见release-plan；本轮不得因写了计划就访问生产、强制生成榜单、修改next_run_at或发测试消息。

## 9. 通过门槛与交付物

所有TC有明确负责模块/断言和执行记录，关键Mongo/ASGI/Node/Chromium/3.9/真图组skip=0；所有新增测试有效RED→GREEN及主执行者独立复跑有证据。TC/PF未完成逐项记录为未执行/阻塞，不算通过。性能不达标不启用；未授权生产不提供“已上线”结论。

证据写入本change独立日期目录：environment.json、baseline-manifest.json、red/green命令日志、mongo-explain.json、mongo-command-summary.json、cas-interleavings.json、equipment-benchmark.json、asgi-responses.json（公开字段）、浏览器/QQ/导出PNG、network-report、截图核查记录、review与acceptance。所有文件经过凭据检查，仅保存必要数据；不覆盖此前诊断和其他agent产物。阶段验收摘要指向具体证据，不能仅写“全部通过”。
