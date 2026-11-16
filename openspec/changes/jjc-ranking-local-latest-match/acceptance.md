当前最终状态：R2f已启用LOCAL1；双正式入口严格全请求审计、完整1000人/橙紫一致性、完整资源及自然驻留观察通过。目标本地最新胜场数据源对照通过，但目标不在最新榜。R3自然生成待验。以下旧阶段保留历史，以末尾R2f最终记录为准。

# 本地最新胜场统一装备 V2：本地验收记录

当前状态（2026-11-16）：R2d终验发现未归因取消，已保守恢复LOCAL0；双入口全请求审计待补，旧画面与900秒局部结果不代替完整通过。双版各414和目标数据对照保留；R3待验。详见evidence/production-20261116/deployment-report.md，以下旧日期保留历史。

下列2026-11-13实施证据按当时版本保留，旧“未部署/禁止部署”描述属于历史阶段，不能覆盖用户后续授权，也不代表11月16日生产候选状态。原629计数与当前生产组合384计数分属不同固定范围，不相加或互相替代。

## 已实现行为

排行榜原位置的装备/队友、橙武统计和紫武筛选共用整榜本地当前赛季最新3v3胜场；胜负不再取不限。最新胜场缺装备不退回旧场，U不入P/L；保持原rank/score/分类心法。QQ与网页导出消费同一view。schema2在现有summary完整单文档CAS发布，读者不混用旧details；失败不发送新图。

## 本地环境和执行证据

权威证据目录：`evidence/2026-11-13-implementation/`。`environment-prep.md`、`baseline-manifest.json`、各 red/green 日志记录真实环境/版本/退出码。Mongo4.4.30为本轮自有 loopback27029 隔离实例；测试仅使用登记随机库。Python3.13.2及真实3.9.25；实际ASGI、Node、Chromium134、html2canvas1.4.1与Jinja截图渲染。

初版分组测试与实际Mongo→service→publish→ASGI→双HTML已通过两Python；主执行者独立复跑日志为`root-final-*.log`（独立审核修复前版本，不能替代修复后回归）。真实网页导出PNG、QQ合图及8张拆图已生成；PNG只证布局，图标在离线fixture本地替换，不证明远端图标内容。

## 审核与修复

未参与实现的后端/UI审核已发现B01–B07与U01–U02，主执行者核实并逐项修复。源码差异与新增回归记录见review及review-root/red/green日志。B01–B07、U01–U04、W01–W03共14项均已修复并由原独立reviewer复审关闭。本次实现及受影响范围静态PASS；完整Harness/全仓及外部CI等边界见review.md。

## 最终主执行者回归

Python3.13.2 与真实3.9.25 **各629项测试通过，skip=0**，按进程分组，避免旧路由stub污染；重复Mongo组只计最终含PF05的10项版本。完整命令/退出码/数目见 `evidence/2026-11-13-implementation/final-core-commands.json`、`final-ui-commands.json`、`final-test-summary.json`；实际日志为final-core/final-ui前缀。另已完成真实3.9 py_compile及git diff --check。最终42份代码/测试文件SHA与测试后复核完全一致，见final-source-manifest.json。

覆盖新规则、strict候选/快照、Mongo4.4CAS/写未知、生成/保存/QQ、配置、装配/六阶段与并发关闭、整段singleflight、API、Node、双入口桌面/手机、真实导出、历史/最高分、旧展示、原同步/离线任务等。既有prefetch原生驱动故障回归在此前独立3.13组已通过，未混入629双版计数。

## 性能与资源结果

| 项目 | 实际结果/证据 | 判定 |
|---|---|---|
| PF01 1000角色×200主表(40当前/160旧)，30冷 | P95 4.233s、P99 4.271s；e7-formal-equipment-benchmark.json保留原完整失败报告；冷阶段成功，后续原压力fixture不合production形状，明确区分 | 冷门槛通过；查询/选场等价路径继承，最终production小样另4.093s |
| PF02 真实production保存结构，100热 | 100/100真cache hit、0重建，P95 .858s/P99 .964s；完整请求singleflight保留前后资格/绑定校验 | 热门槛通过 |
| PF02 20动态+1独立冻结 | 全成功8.123s，真实quota holder峰1；任务峰2为一个运行、一个等槽，非并行构建 | 配额/请求预算通过 |
| PF03 60秒故障+30秒恢复 | 原failed原件保留；独立e7-pf03-settled-final.json：故障61.497s/恢复31.130s，27次全Mongo50，最长9.357s，28/28恢复成功；回收点driver/request/read flight/build/waiter/quota=0、FD12 | 通过 |
| PF04 大小/完整发布 | production整summary实际BSON1,361,619B；8MiB边界/超限原子零写在publication suite验证 | 通过 |
| PF05 真生成及跨实例 | 实际ranking保存/发布锁+MongoCAS；A构建中B发布后20客户端均retired；同timestamp新member版本在旧/新service均reload_required | 双版真实Mongo10项包含此2例并通过 |

PF01原30冷之后的改动是整段动态read合并、任务关闭和metadata游标生命周期；主表查询/3v3金样选场及配额不变，冷证据仅继承这个明确范围，不冒称原文件是最终全部门槛通过的report。真实production结构及热缓存分类另有修正前失败证据和最终稳定source指纹。没有放宽8s构建/10s请求、人数/历史/冷热门槛，不以U全体或空视图规避耗时。

## 保留边界

metadata最多512候选/32MiB、1.5s读取+最多100ms协程清理预算；超限明确read_failed，不称任意历史规模可用。普通Mongo client已提交的底层thread/socket不能仅凭取消Future认定停止；专用equipment client另有原生超时，实际故障样本以driver监控记录。PNG图标在离线fixture本地替换，布局/数量可验证，真实远端图标及生产自然周期未验证。完整Harness保存/coverage ledger、独立只做安全的门禁及外部CI未代签。压力进程恢复段RSS约1.06GiB（含大数据fixture/监控/Python内存保留），原放大夹具压力峰值更高；不称零内存或生产容量已验证，未来启用前须核对真实容器容量。

## 发布边界

没有部署、提交、推送、重启服务、改变生产配置、迁移生产数据或发送真实消息。LOCAL默认0。性能不达标不得启用；发布需另获授权并按release-plan执行。不将本地fixture验收当作生产修复已完成。

## 收尾与可审阅交付

最终实现review所有14项闭环，TC01–TC50源码追溯见test-coverage.md。主执行者已复核PF03 JSON无failures、候选42文件指纹无变化。测试只访问本轮自有loopback27029及随机库，所有随机库已清理；自有Mongo进程已按ownership校验关闭（cleanup退出0），保留证据/数据文件供追溯。不执行提交、推送、部署、生产迁移或消息发送。change保持active，未来生产授权与验收按release-plan另行处理。


## 2026-11-16 发布验收增补（执行中）

- 生产专用候选基于实际T8源，仅选定22文件，默认LOCAL0兼容schema2。182文件完整备份及22文件apply/rollback演练通过；R1四fresh workers与900秒原门槛观察通过。
- 原子writer开启后完成2386510条captured ObjectId范围的四派生字段回填，不改raw/cached_at/match_id；其他_id类型保留完整legacy读取。三索引仅新增，维护160秒，无旧索引删除。
- R2首次公开GET出现503，第二次自然轮询200，已按止损关闭LOCAL并恢复兼容版本；R2不计通过。此前错误静态路径/jx3bot/返回test占位，单独保留工具失败；正式页面路径为/jx3/。
- 后续winner每25角色union仍逐gid精确argmax，完整uncertain/future读取、同版raw/meta与ABA验证、四worker、1500/2000ms/8/10s预算不变。真实shadow1000人build5.602/4.286秒、request8.566/5.810秒、321命令、max1.042秒，零写与零pending，四范围/心法/adapter L/P/U一致。
- `evidence/2026-11-16-dep11-union-regression/`记录冻结候选Python3.13/3.9各384项全绿，最后唯一Mongo测试module追加混合_id排序/禁COLLSCAN后双版各3项通过；不冒称最后385项全跑。仓储源SHA f894a19e0833e790e93a0b704f7f188222c44ba99257ef93b806d7ca4539bc8c，最终组合指纹39116fde8403b53c0532580918c12aad679b8170a940d23d161570fc2488095c，独立review闭环。
- 目标稳定身份不在最新1794769209榜。当前本地最新胜场已由优化3候选与独立全部260条原始历史相互验证：271587982，2026-11-09 21:49:21+08，紫武靖荒太岁·抱剑轻剑，两队友似小卿·梦江南、溪向晚月·梦江南。此为本地数据核验，不冒充最新榜目标DOM。
- R1b已就绪、四新worker正常，LOCAL0观察900秒进行中。R2b真实浏览器/15分钟自然轮询和R3自然生成仍待实际完成。不提交/push，不发送QQ测试消息，不清理事实/队列/水位。


## R2d 最终实际验收（2026-11-16）

R1d于13:37:27+08启动，LOCAL0完整900秒观察通过后，R2d于13:56:07+08启用LOCAL1。最终生产组合指纹c3bad6a11f3e32657a80d1493c5e394c50202f3ea24d4e43df606ed4f71d093d；双Python3.9/3.13各414项通过、零失败/错误/跳过，独立子agent复审闭环。范围见test-final-summary.md及dep13-review.md，不将历史373/384/397或旧629重复计入。

两正式入口/jx3/index.html与/jx3/jjc-ranking-stats.html真实Chromium均passed：API与DOM整份视图有严格网络及授权撤销掩码证明，四范围、各心法、卡片装备/队友、橙武占比与完整紫武名单一致。当前1000人L/P/U=70/849/81；50人为9/32/9、100人为12/70/18、200人为18/152/30。紫武列表849人，零橙武；未知81人不归紫武。每个截图套件固定同装备revision，不将独立入口的不同checked_at混成一套。

首入口自然驻留900.011秒，38次自然装备响应全部200、零核心HTTP5xx；无角色弹窗/手动刷新/同步或POST。资源独立完整900秒、70样本通过，停止原因空；四worker持续在线，最终RSS348112KiB、峰368168KiB、线程22、FD31、CPU峰65.916%。仅证明本观察窗口，不宣称长期容量或生产P95；角色业务GET样本0另列，不冒充该路径错误率为0。

慕容十三·唯我独尊不在当前最新榜timestamp1794769209，不宣称目标当前榜DOM已验。部署后独立读取其最新本地当前赛季3v3胜场271587982（2026-11-09 21:49:21+08），紫武「靖荒太岁·抱剑轻剑」，队友「似小卿·梦江南」（九幽阎罗·试花扇）与「溪向晚月·梦江南」（星弈云台·烬蕊卷），均quality4；优化3候选与完整原始260场一致，写入/pending0，自有probe已移除。详见r2d-target/。

最终就绪/源配置守卫通过，原容器/镜像不变、restart_count0、policy always；仅LOCAL从0启用1，原QUERY/OVERLAY/CACHE/REFRESH及worker4保持。未放宽1500ms Mongo、2000ms socket、8s构建、10s请求或metadata预算；不截断1000人、任意历史或不确定证据。182文件私有备份完整核对，独立rollback-r2d.py真实线上只读--check通过；未执行standalone --apply，旧阶段实际止损回滚记录保留。回滚优先仅LOCAL0并保留兼容reader/writer，不倒退事实/队列/水位。

R3自然生成尚待验；下一自然21:00/03:00事件及冻结/CAS/退役生命周期保持待验。不强制生成、不发QQ测试消息、不提交/push、不归档change。此前R2/R2b/R2c失败/回滚及验证工具误判原记录保留；局部独立静态PASS不等于完整Harness CLEAR或外部CI/独立安全门禁代签。

证据：r2d-final/、live-r2d-browser-index/、live-r2d-browser-ranking/、r2d-target/、test-final-summary.md。


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


## R2f 最终生产验收（2026-11-16）

R1f于15:34:13+08启动，LOCAL0，五源delta备份/恢复与190文件守卫通过；公开seasons/history/legacy read均200/code0。完整观察901.926s/75样本通过后，R2f于15:52:30+08只启用LOCAL1，原QUERY/OVERLAY/CACHE/REFRESH=1、worker4不变，同容器镜像、restart0/always，最终guard/4freshworker通过。

DEP14最终生产组合af8436fa263040503e795e59e167febae5f591dfa39c476da749c42aafdedaeb，双Python各438/0失败/0错误/0skip，同358文件指纹，独立审核附录B/C通过；最新Harness main实际重新获取仍c6694cdf11063ea5e76a1fd75bd9f8bbf4dc55a3，规则未变。仅加载会被消费的胜方装备快照、补专用native1500/close100ms及context1500，原完整候选/6人/最新胜场/身份/前后资格不变。Mongo1500/socket2000ms、build8/request10s不放宽；不截断1000人或不确定证据。测试/受限8诊断/24ASGI均有独立证据，不与旧414/629或工具测试加总。

正式/jx3/index.html和/jx3/jjc-ranking-stats.html两套真实Chromium均passed，完整网络JSON/授权撤销链/DOM/整数统计/图标和队友/紫武名单逐项吻合；每套固定同一装备revision，两个入口的checked_at分别记录，不混版。最终全榜N/L/P/U=1000/70/849/81；紫武名单849人且零橙武，未知不归紫武。四范围及各心法均通过。所有核心请求完成及正文成功、无requestfailed/body_error/5xx，无tab或close取消豁免，成功响应不清除历史失败。

首入口完整自然驻留900.006s，38次自然装备响应全部200。独立资源观察900.000s/74样本通过，reasons空，RSS范围124340–270044KiB，最终线程23/FD30，CPU峰64.119%。这是该窗口证据，不声明长期容量或生产P95；角色业务GET样本0不冒充该路径错误率0。首个仅进程绑定启动包装器的竞态尝试原件保留，未形成验收；修正仅包装器的bounded proc观察，核心工具SHA ca463bf7337c5656c2e84944d1ea925c65d3fe4f754eb752ef6a82a30024ce08和实际验证逻辑未变。两真实运行都有spawn前/实际命令/结束后source绑定。

目标慕容十三·唯我独尊不在当前最新1794769209榜，因此不声称其当前榜卡片DOM通过。R2f实际源上3候选与完整原始260场、同checked_at/current scope/stable binding对照一致；最新本地当前赛季3v3胜场271587982，2026-11-09 21:49:21+08，紫武靖荒太岁·抱剑轻剑(quality4)，队友似小卿·梦江南(九幽阎罗·试花扇quality4)及溪向晚月·梦江南(星弈云台·烬蕊卷quality4)，team_ready。写入/pending0、受控probe清理由独立回读确认。

原182文件完整私有备份与各阶段增量精确衔接，R1f四改一增的新文件beforeNone/mode记录正确。R2f单runtime备份+prior/manifest及对应rollback-r2f.py（SHA be6072f12b87ac55e38386e8795558c929bb04d82ddc2022f31a5f34359db56b）真实--check通过，standalone R2f --apply未执行。回滚须仅LOCAL0且保留新兼容reader/writer，不恢复不识别schema2旧程序，不回退事实/队列/水位；工具持独占锁和完整源码/实例/备份守卫，未知漂移拒绝。旧R2d/e实际恢复记录全保留。

旧R2e真实503 read_failed根因仍未确定，未用8次成功诊断或新观察追认旧失败；本轮修复的是可证的无用IO和预算覆盖缺口，当前观察未复发。R3自然生成/新summary冻结CAS/新榜退役完整生产生命周期仍待自然21:00/03:00事件，不强制生成，不发QQ测试消息、不commit/push、不归档change。静态PASS不等于全Harness CLEAR或外部CI/独立安全门禁通过。

证据：r2f-final/、r2f-target/、live-r2f-browser-index/、live-r2f-browser-ranking/、r1f/r2f-backup-verification.*、dep14-review.md、test-final-summary.md及独立DEP14测试目录。
