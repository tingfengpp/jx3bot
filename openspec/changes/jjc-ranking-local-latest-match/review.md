# 实现审核记录

## 评审结论

2026-11-13 首轮独立实现审核：需修改。后端B01–B07、前端U01–U02、装配W01–W02共11项。首轮发现已逐项修复。最终独立静态复审：本次实现及受影响范围 PASS，B01–B07、U01–U04、W01–W03 共14项全部闭环。没有新增凭据硬编码发现。没有部署授权或动作。

## 检查范围与依据

本任务以实施前`/tmp/jjc-equipment-baseline-20261113/source-before.tar.gz`及其manifest对照当前工作区，HEAD为e61b368eb9c6c9e9bca7c122e8e52bc154479464；保留已有dirty及并行前台身份改动。三位未参与实现的子agent分别审查：

- `backend reviewer`：纯规则、编排、ranking新分支、storage/schema及对应7组测试源码。
- `UI/security reviewer`：完整UMD、双一致HTML、router、QQ renderer、前端/路由/真链测试，独立DOM/API安全及6张真实PNG视觉检查。
- `wiring reviewer`：flag/runtime/singletons/bot关闭、job/handler保存等待、对应装配测试与文档。

Harness最新main已于03:06:21Z拉取，固定c6694cdf11063ea5e76a1fd75bd9f8bbf4dc55a3；复审准备03:20:14Z重新拉取，SHA相同。来源回执见evidence/review-round1；第二轮来源在隔离测试目录review-round2。规范DRAFT作为评审依据；Python无专属CR，不套用Java工具链要求。使用agent-code-review技能；只读审查禁止项目执行。主执行者另行跑测并核实Finding，不将审核者称为测试执行者。

范围限定本需求及受影响上下文，**未执行整仓coverage ledger verify，未通过Harness保存校验，不声称全仓审查或完整Harness CLEAR**。独立安全审查限新增API/DOM/配置/连接/日志边界；无生产权限、账号最小权限或SBOM/CVE扫描回执。用户禁止部署，不另造发布审批动作。

## 规则覆盖

| 规则组 | 首轮结果 | 依据 |
|---|---|---|
| API R01/R02 | 发现问题 | B01旧包升级、B07 int64公开精度；能力/输入/通用错误门禁已查 |
| API R03 | 已检查未发现额外问题 | 单文档CAS、未知写只读核对、不重放陈旧候选 |
| API R04 | 发现问题 | B06 metadata无界扫描；其余固定1000成员 |
| RES R01/R02/R04/R07 | 部分完成 | 预算、配额、singleflight、退避已查；U01、W02恢复/生命周期缺口 |
| RES R03 | 不适用 | 未引入熔断器 |
| RES R05/R08/R09 | 发现问题 | B04错误与撤销区分、U02首份降级、W01告警及W02关闭阶段 |
| TEST R01–R06 | 发现覆盖缺口 | 对应11项补组合回归；不以原通过数反推正确 |
| PERF R01–R05 | 未验证完成 | 有原始失败/优化诊断及正式样本正在执行 |
| SEC R01/R03/R04 | 已检查未发现新增安全问题 | 冻结榜边界、公开白名单、escapeHtml、HTTP(S)图标、无任意角色ID入口 |
| SEC R02/R05/R07 | 已检查未发现新增秘密问题 | URI注入、固定异常类型、无新硬编码秘密；不回显值 |
| SEC R06 | 未检查/未验证 | 无当前输入SBOM/CVE回执；未新增生产依赖 |
| SEC R08 | 不适用 | 本次无已确认秘密泄露事件 |

### 五项检查摘要

| 检查项 | 状态 | 实际主体/依据 |
|---|---|---|
| 新上下文静态审查 | 本次范围复审完成，无剩余Finding | 上述3名未参与实现reviewer |
| Finding核实 | 14项已闭环 | 主执行者核实并修复，原独立reviewer逐项复审 |
| 独立安全审查 | 切片安全检查已做，完整独立门禁未验证 | 新上下文未参与实现，但功能/安全同切片；未另立只做安全的上下文，权限/CVE未验证 |
| 自动化检查 | 本地最终分组完成，外部CI未代签 | 作者RED/GREEN+主执行者独立分组复跑，不由静态reviewer执行 |
| 人工批准 | 不代签 | 用户已授权编码/本地测试，明确不部署 |

### Spec与实现一致性

| 场景 | 首轮结论 | Finding |
|---|---|---|
| 最新3v3胜场/不借旧装备/稳定身份 | 基本一致，时间与类型组合缺口 | B02/B04/B05 |
| 三处统一及冻结policy | 不一致 | B03 |
| 完整升级CAS/原子发布 | 不一致 | B01 |
| 严格metadata完整性及有界读取 | 完整性已实现、资源缺口 | B06 |
| 完整int64公开 | 不一致 | B07 |
| 成员重载失败可恢复 | 不一致 | U01 |
| 首份V2就绪前整体加载/失败 | 不一致 | U02 |
| 非法配置告警、关闭完整drain | 不一致 | W01/W02 |
| 历史/最高分隔离、原sync、保存后发送 | 静态一致 | 无新增Finding |

## 问题清单

| ID/级别 | 已确认根因与影响 | 修复/验证 |
|---|---|---|
| B01 P1 | 公共load_summary剥_id，旧包升级失去完整CAS基线 | strict原始一次读取load_publication_baseline；真实Mongo build→publish回归 |
| B02 P1 | 旧时间别名在冲突检查前跳过 | 仅所有别名一致合法旧时间才跳过；双顺序测试 |
| B03 P1 | frozen公开成员保持旧state，但ranges用当前白名单重分类 | 构建时分类、后续按冻结state聚合；policy指纹纳入摘要；变更当前白名单回归 |
| B04 P2 | roster标量在编排索引阶段先崩，无法判矛盾撤销 | 索引只遍历list；坏对方roster仍交领域；服务级回归 |
| B05 P2 | 明确非3v3被视为矛盾并盖掉合法3v3 | 一致明确2/5跳过，冲突非法仍保留；共存反序测试 |
| B06 P2 | metadata搬全历史正文无界 | 总/命令/扫描容量预算、finally关cursor、history限season；超限明确read_failed |
| B07 P2 | JSON number丢完整int64精度 | 公开match_id统一十进制string，内部排序int；Node真JSON roundtrip |
| U01 P1 | reload bootstrap一次503后永久停 | 独立pending/epoch+30/60退避；切页/隐藏取消旧epoch，真实HTML回归 |
| U02 P1 | schema1转V2首次就绪前展示/故障恢复legacy统计 | 首份pending/错误整体守卫，仅明确disabled/retired整体legacy回退 |
| W01 P2 | 新flag非法值默默关闭无诊断 | 统一strict parser，固定键名/来源告警，不含原值 |
| W02 P2 | close漏baseline/资格await请求 | 公共入口完整内部request任务追踪，全部cancel/drain后关client |

全部为本次新增或加重；未发现存量P0红线。复审闭环在此追加，不能把已接受修复方案写成已修复。

## 验证结果

reviewer仅静态阅读/归档对照/PNG查看，未执行代码、构建或测试。主执行者已对B01/B02/B03/B05取得有效RED，新增回归与相关组107项GREEN；B04回归初始mock需修正FrozenInstanceError，修正后GREEN，不冒称该初始fixture错误是业务RED。B06作者预算测试双Python46项GREEN；B07真实Node完整int64 JSON测试RED后实现。最终同版双Python/浏览器/性能结果待汇总。

## 未验证项与剩余风险

所有Finding已完成独立复审；PF03独立收尾已通过，仍不启用LOCAL或部署。metadata超扫描预算会明确失败，未来大规模历史需分页/投影设计，不能隐瞒为完整成功空列表。图标离线fixture替换，PNG仅证明布局与数量；生产自然榜单、真实图标、权限和账号未验证。没有运行生产恢复、迁移或发布。

## 最终增量复审闭环

- B01–B07：`implementation_review_backend` round3全部关闭；完整read singleflight新增实现未发现需修改问题。
- W03：真实Python3.9复现“多层取消打断异步finally”后，按requests→read flights→pending→owners分层收束；共享close只执行一次，caller取消也等drain后传播。`implementation_review_wiring`确认所有W问题关闭，作者双版120项GREEN。
- U03：无撤销legacy整体回退后恢复旧poller，V2正常commit不重启；U04：适用当前绑定的撤销阻止不安全legacy回退/导出，首次动态失败也使用bootstrap身份basis。`implementation_review_ui_security`确认U01–U04全部关闭，双HTML一致。
- 全部审核者只读静态，未执行项目测试。最终性能及主执行者同版复跑记录在acceptance；完整Harness/全仓/独立安全及CI等限制保持上述真实口径，不将静态PASS冒称全门禁CLEAR。

最终执行证据：主执行者双Python各629项/skip0；PF03 settled report为passed_controlled_driver_faults、failures=[]，源14项无变化；42候选代码/测试文件最终指纹一致。其他不在本次范围的脏工作区未代签。具体实际命令和外部验证限制见acceptance。
