# 全榜查询性能闭环执行补充（2026-11-13）

用户明确授权：不放宽超时、不截断数据，子agent先补测试与独立审核，通过后按原发布计划部署并开启。沿用原R1/R2各15分钟观察、R3自然事件及兼容reader回滚。

DEP06已完成常见旧时间短路和sort前窄投影；真实r15仍Mongo50，保留证据。r12/r13表明global查询的主表FETCH约0.89秒/15141条，原身份count 0.06秒为索引计数，不能据此称FETCH很快。

DEP07采用两阶段：两队分别仅gid条件、仅投_id，按100角色批次、最多4并发、完整消费覆盖索引；BSON类型精确物理ID全榜去重；按256源ID/批、全1000target执行原身份/时间/证书规则并完整消费，所有分片局部max由Python全局归并。每分片随后_id_覆盖读取确认已枚举源仍存在，缺失整读失败；不缓存枚举集，新增正常下轮可见，原来同样不提供跨批事务快照。未知证据全留、没有胜负/装备/赛季预枚举条件，没有任意limit。driver/socket/build/request预算保持原值。

新增且只新增两个普通索引：idx_equipment_team1_global_source=(data.detail.team1.players_info.global_id:1,_id:1)，team2同理。不删旧索引，不迁移/回填业务事实或参与者投影，不改变写者。Mongo4.4隔离explain已证PROJECTION_COVERED/IXSCAN且totalDocsExamined=0，完整ID并集与原身份查询一致。主表实际约234万文档、131GB逻辑/35.5GB压缩，原两gid索引各74MB；新索引成本必须实测记录，不把6文档合成索引大小冒充生产。

索引显式维护在发布前执行：复核原索引定义、代码/config/runtime与既有备份，持release lock，私有专用临时容器只运行固定两索引维护脚本。合并createIndexes以共享一次集合扫描；DDL原生600秒/客户端660秒仅适用于维护，排行榜读路径1.5秒Mongo、2秒socket、8秒构建/10秒请求绝不修改。宿主监测与DB维护结果保留，出现资源/健康异常停止推进，不盲重试结果未知的DDL、不清数据。保留原索引；回滚应用/LOCAL不依赖删除新索引，后续如确需删索引另行明确维护，不作为紧急恢复必要动作。

测试先行包含repo枚举/批次/presence/物理ID/失败取消、utility分片1000target与全候选差分、覆盖索引、并行close等价；原全部受影响双Python/真实Mongo/浏览器回归重跑。独立审核当前代码/测试/维护脚本后才建生产索引。再做真实全1000人只读shadow、每条命令预算及收束、目标同场武器/两队友、四范围N=L+P+U与P无L。通过后新建/重验发布备份与apply manifest，执行R1/R2/R3。若仍不通过继续定位；不以单角色成功或合成数据代替全榜门槛。


## DEP09/10：原子结构证书与数据库最新胜场选择

DEP07/08在公网回环链路下仍socket超时。已先以无凭据isMaster确认内部入口与原URI的topologyVersion.processId相同，再确认保留认证/端口/数据库/query，未关闭TLS。只给装备专用client新增可选JJC_EQUIPMENT_MONGO_HOST(默认空/RFC1918 IPv4)，原MONGO_URI及其他client保持；有TLS的URI拒绝host替换，不能降低证书校验。r19内部链路单命令已无超时，但完整构建8秒到期：约58.9万索引ID全部枚举后仅完成26源块，说明历史全集逐次FETCH工作仍超预算。现有两个新覆盖索引已建立，应用仍原版本/LOCAL关闭；它们不是发布成功证据。

主表同文档新增四个版本化技术字段：equipment_read_time_v1(正有限一致时间或null)、equipment_read_valid_v1(bool严格结构已证)、equipment_winner_gids_v1(合法胜方3稳定gid或空数组)、equipment_match_id_v1(合法rootmid或null)。15非null时间alias仅int/long/double正有限同值，容器非法为unknown。结构证书完全沿已差分的positive certificate：3v3类型/ID一致、6唯一合法稳定gid、双队bool相反、unavailable缺/null/False；非nullseason_id保守转unknown，不做跨赛季猜测；不检装备/奇穴/心法目录，因此最新缺武器不能回退。每次原raw与compacted data写入都在同一原子$set写四字段，LOCAL0同样维护；不接收上游外带技术字段。

新读取以三个显式维护索引支持：winner_gids,time降序,match_id降序,_id升序；两side的gid,valid,time,_id。每角色在已证明合法的winner索引内，以S<=time<=checked_at和精确time/mid/_id排序取唯一最大项(limit1是业务定义的最大胜场选择，不能套用于unknown历史)。索引覆盖投_id/time/mid以保留expected tuple。未知validFalse按当前time及null时间分别完整读取；legacy valid:null全部读取；validTrue的future另按两队完整枚举，避免未来败场被winner-only遗漏。每个branch独立find，不能合OR/nor导致Mongo4.4扫描所有旧记录。所有source再经原完整领域规则/快照加载；每块presence除存在性还核对winner expected(gid,time,mid)与当前原子元数据一致，源变更整读失败，不能静默显示错误次新胜场。每阶段固定4worker、wire batch有限且完整消费、原1.5/2/8/10秒预算不变。

先让子agent补纯规则、writer原子性、完整BSON差分、winner真实索引、future败场/来源变更、host配置/装配及维护测试，独立审核当前候选后执行R1 LOCAL0兼容writer发布。所有旧writer真实退出并确认新4worker同进程后，才做证书派生回填及三索引构建。Mongo update pipeline在服务器对当前doc一次原子计算四字段，不使用读出旧data后盲写计算值。首轮维护以ObjectId稳定上界/主键分页处理所有该范围旧记录，每页原生1500ms；异类_id不冒充已回填，继续由legacy分支完整读取。维护期间可暂停，partial仍安全unknown fallback；未通过全榜完整shadow不得R2。没有修改原始data/cached_at/match_id、快照、队列或水位，不是事实纠正迁移。

关闭LOCAL保留R1原子writer是标准回滚。若进一步退回不维护元字段的旧writer，证书一致性保障即失效，之后R2前必须重建/清除旧证书并重新验证，禁止直接重开索引读取。schema2兼容reader及原冻结统一口径仍保留。R1观察期间可做已授权派生维护，但要继续核验4worker及资源；R2开启仍须维护/全榜SLO/静态与动态一致性通过。R3只等自然榜，不制造新榜。尚未进行任何R1应用替换。


DEP10最终补充：使用四元字段和winner index的完整结构证书，not just time。第一阶段每角色一次索引top1(S<=time<=checked_at，Int64 time/mid/_id排序)，未知validFalse按current/null，legacy valid:null，validTrue future按两队全部保留。三正式索引名称为idx_equipment_winner_latest_v1(winner_gids:1,time:-1,match_id:-1,_id:1)，idx_equipment_team{1|2}_state_time_source_v1(sidegid:1,valid:1,time:1,_id:1)。这有意取代最初PF01“无每人find”的执行形态：最多1000条固定4worker索引最大项查询属于业务最大值归并，不是任意历史截断；命令数变化须在实测/审核中披露。

为避免winner枚举与点读间ABA，第二阶段完全取消aggregate预裁和后续presence查询。每256source直接find必要raw+四meta(batch64)，完整消费并核对源ID集合；required winner在**同一次文档读取版本**中，校验stored四meta与raw重算均匹配expected(gid,time,mid)，缺失/变化整读失败。未认证/未来raw不裁掉，交原domain。Mongo回填3stage计算time→valid→winners/mid是在一个atomic update里，stage间可引用前值，不能同stage引用新赋值。标准回滚保留R1兼容writer；退回旧writer后所有证书必须重新建立一致性才能再开启。

R1最终allowlist补入新utils/time文件及仅新增装备host键的runtime_config，共22项。配置秘密只在远端私有payload/backup，不进入仓库或公开报告。前两条ID覆盖索引是本次已建的中间优化，保留并记录；原9条索引不得删除。R1备份/私有恢复演练按最终manifest重验。
