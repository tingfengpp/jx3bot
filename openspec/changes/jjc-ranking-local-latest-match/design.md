# 本地最新胜场统一装备口径（V2）

日期：2026-11-12，Asia/Shanghai。阶段：按用户最新要求修订方案，两位子agent已完成本轮初审及闭环复核，发现和文档口径冲突均已修订；未编码、测试或部署。当前权威文档为本design、proposal及spec；design-v1-superseded/review-v1-superseded仅为历史，不作为当前验收依据。

## 用户确认的业务目标

原有页面位置直接展示本地当前赛季最新一场3v3胜场的武器、本人来源心法和同队两名队友，不新增栏目或第二套配置。卡片装备、橙武统计/比例和紫武成员/分布统一采用同一份胜场装备。明确禁止“紫武榜成员卡片显示橙武”，也禁止卡片已更新但统计仍为旧值。

本次仅统一装备口径；排名顺序、评分与角色分类心法仍沿用榜单冻结值，不能用胜场本人来源心法改分组或覆盖率。来源本人心法仅用于原卡片图标，与分类字段分开。

## 首轮只读诊断证据（历史时点）

以下证据来自首轮诊断时点，不是持续线上状态；PROJECT_CONTEXT已记录11月12日后续目录修正，本次复用当前目录而不重复修复。

首轮最新当前赛季榜单timestamp=1794418306。慕容十三（唯我独尊）rank=446、score=2447；成员采用cache心法、冻结基线19，原快照没有武器、队友或display_packet。

角色展示包版本21，对局271587982，比赛时间2026-11-09 21:49:21；本人武器“靖荒太岁·抱剑轻剑”、quality=4可用，但team_ready=false、teammates=[]。11月12日10:18:50尝试为partial/team_unresolved；连续失败9次，队列pending，next_run_at=11:18:50。next_run_at是最早领取时间，积压时不保证准点。

本地按global_id、当前赛季、3v3、match_time倒序查询，最新一场与最新胜场都是271587982。jjc_match_detail、jjc_match_participants及装备快照已保存完整同场资料。本人同队为似小卿·梦江南（周天功10786）和溪向晚月·梦江南（灵素10626），三人的武器快照均存在。

线上展示校验只接受心法目录ID并额外兼容10026。灵素目录ID为10616，不接受本场来源10626。独立进程把本地同场详情及装备交给线上build_display_update，复现partial/team_unresolved；仅在该诊断进程允许10626后，同份输入生成full及两名队友。没有修改线上文件、服务进程、数据库、队列。未取得上次上游原始响应，此实验确认校验缺陷，不等同逐字段重放上次网络响应。

生产与本地的tuilan_kungfu_client、jjc_ranking_display、jjc_kungfu_policy及排名HTML哈希一致；kungfu_refresh和ranking_inspect不同，相关结论直接读取生产版本。

## 当前刷新逻辑

1. 榜单优先读心法缓存。cache命中时成员只带心法及绑定/版本，不从本地对局取武器和队友；未命中现场查询可带回本次展示包。
2. bot内串行后台刷新角色画像，当前配置正常6小时、最大可用24小时、上榜追踪48小时；失败按60、120、240秒等退避，上限1小时。任务实际执行受队列积压影响。
3. 展示采集另取indicator和最近40条history，选择当前赛季最新胜场，再请求detail，必要时replay。最多四次请求，角色取得查询锁后总预算60秒；不利用已同步本地详情补齐展示包。
4. 发布到role_identities.jjc_display。武器和队友必须来自同场；任一队友身份或心法不通过校验，会令整组team_ready=false。partial仍可保留本人武器，并带错误提示。
5. 页面先展示冻结快照，仅对最新当前榜的可见、已展开成员轮询current-displays：正常15秒一次，每批最多100人，读取失败退避至30/60秒。隐藏页和视口外成员不主动检查；历史榜不动态叠加。
6. current-displays仅批读本地jjc_display，不外呼、不入队。cache成员只采用高于冻结基线的版本；live可采用同版本，并校验身份绑定、赛季与时效。等待后显示更多资料，来自这次轮询取得已有或后台新发布资料，不表示浏览器逐人查询推栏。
7. 战绩弹窗走独立role-recent缓存（默认24小时，强刷/缺失时另查history），并从本地参与者/详情补充对阵图标；不会回填排行榜展示包。“刚更新”是接口缓存时间，不是比赛发生时间。

## A. 本地胜场事实选择

按冻结成员绑定且复验的global_id，从权威jjc_match_detail主表批量选最新胜场。目标角色与won=true必须在同一队，严格布尔；赛季起点 <= 有效match_time <= 本轮取数起始时间，match_id为正整数，按(match_time,match_id)倒序。主表显式赛季/类型声明不能冲突；无类型时仅允许严格两队各三人且身份无重复的3v3推断。本人在六人中必须唯一，同队三人身份确认后取两名队友。不以姓名/区服猜身份，不依赖心法cache/live/found/publish_version；心法未知但身份可靠者仍可展示和判断装备，分类保留unknown。

参与者投影仅为可选定位提示，不能代表完整事实集；主表已有新胜场而投影缺失也必须可见。不能把“任意队赢”与“另一队有本人”组合成胜场。权威结构/身份/胜负矛盾且影响选场时标记data_inconsistent，不能借过期投影证明胜场。

先选最新有效胜场，再读取装备；最新场缺装备不回退旧完整胜场，不与历史场拼接。专用strict批量方法读取必要主表字段及装备，支持内联armors与equipment_snapshot_hash，仅hydrate所需装备，不调用会检查旧奇穴schema的完整load_match_detail/get_match_detail。不外呼、不入队、不写对战事实、同步水位或画像。未知心法保留已确认队友姓名并用未知图标，来源10626兼容；不为本地容错放宽旧上游发布规则。

## B. 唯一有效装备值及未知状态

每名成员本轮只有一个effective_equipment，内部可保存local_win_packet来携带比赛、队友和装备，但它是统计、筛选和渲染共同的输入，不是只供卡片的第二套资料。旧weapon_*只作legacy读取兼容，启用V2时不能再作为统计/筛选回退。

有效装备记录包含source=local_win_v2、match_id、match_time、装备名称/品质/图标、equipment_state及缺项原因。橙武判定继续复用项目品质5且名称命中白名单的既有业务规则；资料齐全且明确满足既有非大橙武口径者才作为紫武候选。不能以!isLegendaryWeapon对缺失装备默认判紫武。名称/品质不足以确定类别时为unknown；仅图标缺失但名称/品质足以确定时，可分类并在原图标位显示缺图提示，不能因此改用旧武器。

no_local_win、binding_invalid、data_inconsistent及确实缺装备的角色装备为unknown，不计橙、不入紫，卡片不显示旧装备；保留可确认队友/原分类信息及准确提示。无胜场只表述“暂无本地胜场记录”，不宣称游戏近期没有获胜。

每一范围/分类分组严格满足N=L+P+U，其中N为角色数、L为已确认大橙武、P为已确认符合紫武筛选口径、U为装备未知。橙武数量=L；橙武比例保留原分母，采用L/N，表示“已确认橙武占该范围/分类总人数”；在现有说明处标明装备覆盖(L+P)/N和未知U，不能暗示未知装备均为紫武。N=0时比例不可用。紫武页的成员和人数/分布直接来自同一份P集合，不能用旧总人数减旧橙武数推导。unknown不冒充紫武。本次不改变原占比分母；缺失数据通过unknown显式表达且不进入紫武集合；不新增栏目，复用现有比例及覆盖提示。

未知分类成员可按装备状态出现在全榜/紫武的未知分类区域；已分类心法的人数、原排名和评分不因装备变动改变。

## C. 同一份整榜装备视图，整体更新

仅轮询可见100人再替换图标无法同时保证全榜橙武比例与紫武集合正确。因此V2将更新单位改为“当前榜单的一份整榜有效视图”：冻结成员集合/排名/分类 + 对该集合每人选出的最新本地胜场装备/队友 + 从这些值一次计算的所有范围统计和紫武集合。全量上限1000人，四个范围按原rank截取，逐范围聚合，不能从前50变化代表前1000。

服务端提供新的纯本地equipment-view读取用例（拟GET /api/jjc/ranking-stats/equipment-view?timestamp=...&equipment_contract=2，并携带tasks.md约定的expected_member_revision/expected_frozen_source），返回共同view_revision、scope、checked_at、成员有效资料及top_50/100/200/1000橙武数/比例、紫武成员和分布/装备覆盖。快照详情与flat-members的旧响应不再直接驱动启用V2的装备统计；V2页面在客户端按同一view_revision的完整成员及统计渲染分布和列表。成员只返回白名单数据，不透传内部定位私有信息。

读取过程每人只装配一次，整份视图计算完成后才可返回；视图内部一致，不宣称Mongo多批读取具备跨集合事务快照。checked_at记录取数起点，另有completed_at，内容修订摘要view_revision不是递增代次。更晚请求的成功视图可整体替换，前端用请求epoch/顺序丢弃旧响应，不能按摘要字典序比较。同一内容view_revision仅可复用内容渲染，不能跳过enabled/retired/source/read_failed恢复等生命周期处理；同场装备补齐会改变revision并同时更新统计、紫武和卡片。

页面每15秒触发一次“检查当前整榜装备视图”，同一页面不重叠；隐藏页、切榜、最高分页、离开及导出期间停止或丢弃响应。成功后在一次状态提交中整体替换当前view；DOM按稳定键(range,lane,kungfu,rank)增量更新或在同次提交恢复原展开、滚动、分页位置和焦点，不能直接每15秒grid.innerHTML全页重建关闭详情。焦点成员被移出紫武集合时，移焦到对应组标题或相邻合法成员；需要紫武页移除或加入角色时同次提交完成，不能先换图标再更新筛选。仅展开哪些人影响DOM渲染，不影响服务端整榜装备选择。导出固定一个view_revision及其统计、成员、配置，全程不能混版。

跨请求合并在service层实现有界、单flight的内存短缓存，只保存完整不可变视图；按timestamp+scope+source+固定成员版本键控，最多15秒复用，服务重启可丢弃。不得为每个用户/每次轮询发起并行整榜重算，不创建无界任务、额外调度队列或新的事实writer。跨实例各自缓存，不能宣称全局互斥。开启前必须通过1000角色/多用户重复轮询性能验收。缓存不缓存“仍为最新”的资格：OVERLAY关闭先零库访问返回disabled；开启时每次命中及重算返回前均校验最新完整发布timestamp、scope、来源及固定成员版本/身份绑定，结果已退役返回retired=true、latest_timestamp，不返回或重新缓存A的动态值。资格查询故障为read_failed，不能冒充退役。计算跨越新榜/成员版本变化时丢弃候选，同步重取资格，不将旧值发布为当前。

## D. 生成、保存、旧榜和故障的一致性

生成新榜时：完成冻结排名/分类后，按同一整榜本地胜场结果计算装备字段、各范围统计与紫武集合，作为local_win_v2冻结视图保存。存储summary、details、flat-members采用相同equipment_view_revision和明确equipment_source，不能只加包而沿用旧legendary_count_map/旧紫武字段。内部原始统计输入可保留用于回滚，但V2有效响应只认共同版本，页面不显示两套装备。

V2具体采用单文档完整发布：在jjc_ranking_stat_summaries中嵌入完整equipment_view（唯一成员数组、冻结分类/排名、同场配置、四范围聚合/集合、scope、equipment_view_revision、member_revision、equipment_source=local_win_v2、equipment_complete=true）。候选全部在内存完成并验证后，通过一次原子写入发布完整文档；V2不能先调用原save_snapshot逐项写summary/details并以其作为成功。写失败必须传播，保留此前完整版本，不能被旧suppress_errors吞成成功。

新文档schema_version=2；实际BSON编码整份文档限制8MiB（低于16MiB），成员≤1000并限制姓名/武器名称≤256字符、图标URL≤2048字符和字段白名单。无关原始装备/奇穴/回放不进入视图。超出整文档限制明确失败，不截断成员、统计或revision。对同timestamp重跑，使用读取时的expected_revision作条件发布；并发候选失配必须丢弃或重新完整构建，不拿旧候选覆盖较新完整视图。历史榜不可被普通重跑覆盖。LOCAL开关关闭的legacy生成也不得将已发布V2同timestamp文档降级覆盖。

V2 summary/details/flat/导出/QQ全部从该完整equipment_view派生，禁止再读取同timestamp旧jjc_ranking_stat_details混装；旧details仅为legacy资料/兼容保留。V2页面一次取整份view，后续展开在内存完成；仍开放的分请求API须传expected_view_revision，不匹配返回version_mismatch并要求整份重取，不能无提示返回新版本详情。list_timestamps/list_seasons/latest仅公布完整可读版本，schema2要求equipment_complete与revision/成员完整校验；schema1按原完整legacy兼容策略。读接口以equipment_view内部统计为V2权威，不将外层legacy旧字段混入有效结果。

对已上线最新旧榜：有强身份、冻结成员和合规scope时，可只读重建一份V2整榜有效视图供页面使用，卡片/统计/筛选全部取它，不改写历史快照。缺强绑定成员为装备unknown，不凭昵称补全；缺少完整成员集合或scope的legacy榜不部分启用V2，保留整份旧口径并提示不能启用新来源，不能只给部分卡片叠加。

数据库/装备查询异常或预算超时是read_failed，不能当成no_local_win/缺装备。刷新失败保留整份此前成功视图和其统计/集合/卡片，提示检查失败；首次无成功V2视图时显示整体读取失败，不能用旧统计拼新卡片。同一次成功取数确认的无胜场/装备真实缺项按B变成unknown，统计及列表同步变化。确认绑定失效/数据矛盾时，在候选视图将其置unknown后整份发布，不能只清卡片。已知身份失效的撤销不能被故障回退或关闭叠加恢复到被证伪装备；需要保留撤销掩码并用同一纯聚合函数重算掩码后的视图，仍维持统一口径。掩码键至少为timestamp+scope+member_revision+可靠身份绑定（不只rank），记录确认时刻和原因；客户端在当前页面会话保存，服务端在有界视图缓存生命周期内保存，不冒充持久历史修正。故障、disabled、缓存内容过期、退役或来源关闭不能解除对应掩码；新榜B成员不能继承A同rank掩码。只有同一冻结绑定经过确认时刻之后成功重验有效的新视图才可解除。对冻结回退/切源/退役恢复同样先应用掩码再统一重算L/P/U和成员集合。

历史榜采用其自身完整冻结视图，不用今天最新胜场重算。A榜退役为历史后，页面从A动态整份恢复为A冻结整份（统计、紫武、卡片一起），在没有撤销差异或使用相同有效撤销上下文时与重开A一致。会话内已确认撤销优先于形式上的重开一致；当前只读方案不承诺跨页面关闭/服务端缓存失效后仍记得该撤销，也不把会话掩码当作历史事实的持久修正。最高分榜保持原数据源，不进入本变更。

## E. 开关和兼容回滚

新增JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED（取代V1拟定DISPLAY键，尚未实现无需迁移），默认0，只接受0/1、启动生效，控制V2生成及整榜读取。旧QUERY只管旧画像采集，不是V2前提，旧writer/队列/代次/正常sync/prefetch保持原职责，本次不削减detail/replay生产。

OVERLAY仅控制动态更新：关闭时equipment-view零数据库访问，前端恢复同一份冻结视图，不是只恢复统计或只清图标。LOCAL_WIN_EQUIPMENT关闭时不走新主表/装备聚合；已保存V2快照仍作为其冻结完整来源读取，不能忽略V2字段让新卡片配旧统计；旧快照继续完整legacy来源。来源切换以整份视图为单位，拒绝跨源拼接。V2生效时旧current-displays不能再单独更新卡片；旧端点仅服务legacy页面/分支。

运行文件回滚必须恢复一套理解所选快照来源的兼容读者，或整体回退到其保存的legacy完整版本（若没有则保留兼容读者，不能把旧二进制直接上线并声称无损回滚）。不清空事实、不重置队列/代次、不改写旧历史榜；候选/恢复点和版本兼容在发布计划明确。


### 旧客户端与读者能力门禁

V2读者显式声明equipment_contract=2；equipment-view固定返回schema/source。原read/details/flat-members等入口遇V2快照，或已启用V2的最新legacy榜需要动态转换时，未声明能力的请求返回明确equipment_contract_upgrade_required（HTTP409及业务错误），不得将V2成员/统计交给会按N-L筛紫武的旧HTML。若提供legacy回退，必须是完整保留且来源一致的一整份legacy视图，不能只替换summary字段。本轮默认升级错误方案，避免维持两套实时口径。

current-displays服务端对V2快照或当前启用V2的榜单返回overlay_enabled=false及需要新版整榜接口的原因，禁止旧HTML继续单卡叠加；此判断不能只依赖新版前端主动停轮询。OVERLAY全关仍优先零库访问返回禁用。旧缓存HTML可收到明确升级提示/停止叠加，不能读取V2后按旧!isLegendary/N-L算法重建紫武。新HTML在V2初次整份就绪前保持加载/错误状态，不先套用旧统计与新卡片。

## F. 模块与已有契约变更

ranking service新增整榜本地胜场装备视图编排/统一聚合；ranking_display迁移V2来源状态；inspect/snapshot repo专用strict批量只读；stats repo完成视图保存/完整发布/冻结读取；router新增equipment-view；singletons只装配；两个HTML同步采用共同revision，不新增栏目；QQ/导出/其他统计消费者须消费同一有效结果，不能保留旧橙武计数。具体修改src/renderers/jx3/jjc_ranking.py的_prepare_template_data、_calculate_legendary_percent、_equipment_incomplete及combined/split模板：V2只读service给出的L/P/U/N、L/N与覆盖，不再检查旧weapon_*重算，不复用缓存心法分支equipment_incomplete=true就隐藏整栏的旧门禁；有U仍显示准确已确认数量/比例和覆盖，N=0明确空值。定时与手动QQ用本轮同一生成视图，不二次查胜场。config及README/README-Docker/runbook记录开关，数据库字段/索引变更同步数据库设计，PROJECT_CONTEXT在实际生效时更新。

明确有意替代：V1“只改展示、冻结装备统计不变”全部失效；旧active jjc-ranking-display-refresh中统计/紫武不随动态层变化、可见成员独立更新的条款仅在legacy分支保留。V2初始/动态/导出必须整份统一。保留原排名、分类心法、同场、防串号、胜场筛选、只读、不改变sync和历史冻结等要求。

## G. 性能、验收与阶段边界

基于两队global_id索引限定成员查询，不全表扫描；生成/视图最多1000角色，固定分块、只投影必要字段、去重详情和装备hash、strict集中加载，无逐人count/find或无界并发。maxTimeMS/整次预算/缓存上限/单flight取消由隔离Mongo explain与1000人多用户实测后确定，预算内不完整不能伪装成功或无胜场；未通过性能门槛不启用。

建议验证（尚未执行）：最新败场跳过、全败/无本地胜场、缺装备/未知名称品质/仅缺图标、橙转紫/紫转橙、新场缺武器不借旧场、心法未知、投影滞后、主表胜负变化、未来时间/非法ID/重复身份、旧奇穴/内联armors、各读取故障；四范围/各分类满足N=L+P+U、橙数与卡片一致、紫武集合每人均非橙且图标同版；同场补齐同步影响三处；N=10/L=2/P=5/U=3时网页、QQ合图/拆图均为20%橙武、70%装备覆盖、紫武5人；整个view替换、原子写失败/8MiB超限不露半成品、同timestamp revision竞争/版本不匹配、列表/分布/导出/QQ一致、旧最新榜和legacy/自然退役/最高分隔离、开关组合/跨源回滚、失效掩码与失败保留、1000人多用户轮询及索引预算；旧HTML无capability请求的升级门禁、当前榜缓存命中时退役/新榜在构建中发布、同revision故障恢复、展开滚动焦点保持、掩码跨榜不串用。

当前仅修订需求方案并完成两位子agent审核，不开始业务代码或部署。方案已获用户确认，tasks.md/test-plan.md/release-plan.md已形成并经两位子agent静态复核；计划审核见plan-review.md。当前仍不编码、运行项目测试或部署，等待用户确认执行计划。
