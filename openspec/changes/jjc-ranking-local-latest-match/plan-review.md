# 详细执行计划与测试计划：子agent审核记录

日期：2026-11-12，Asia/Shanghai。用户已确认design V2，要求编写详细计划再调用子agent审核，并明确先不编码部署。本报告对应tasks.md、test-plan.md、release-plan.md；不替代review.md中的前序方案审核，也不继承前序PASS作为本轮结果。

## 结论与范围

两位子agent分别完成计划初审与闭环复核。初审7项确定缺口及复核追加1项协议缺口，合计8项，均已在计划层修订闭环，未发现剩余确定冲突。50个功能/边界用例TC01—TC50、5组性能/资源检查PF01—PF05仍全部是未执行的测试设计。

本轮只读检查当前源码/测试/项目文档，并修改计划/方案状态文档。未创建任何业务实现、stub或测试源码，未执行项目代码/测试/构建、未访问生产或数据库、未部署或发消息。文本结构/编号/hash检查不算项目测试。静态计划通过不代表实施通过、性能通过或生产发布资格。

## 审核分工与依据

review_local_win_data审查E0—E5/E7的数据选场、Mongo4.4 CAS、完整发布、历史tag、正常同步、QQ及测试矩阵；初审4项，复核4项均闭环，新增约束未见确定冲突。

review_local_win_display审查API/整榜采用/缓存/版本/撤销/生命周期、浏览器/Node/真链/3.9及回滚；初审3项。第一次复核原3项主体已补，追加客户端manifest请求约束1项；定向第二次复核确认4项全部闭环。

主agent读取AGENTS/PROJECT_CONTEXT/PLANS、当前ranking、stat/inspect/snapshot repo、router、QQ renderer、bot生命周期、原Mongo/browser/chain测试及已发布心法目录记录，独立复核各发现并修订。读取当前工作区不等于读取当前线上；原源码已有未提交修改，本轮未覆盖。OpenViking Experience检索无匹配，规则检索工具本轮未注册，不将缺失检查宣称完成。任务为计划静态兼容性审核，不是完整实现代码审查或Harness运行验收。

## 发现闭环

| ID | 级别 | 计划缺口/证据 | 修订位置与建议验证（未执行） | 复核结果 |
|---|---|---|---|---|
| P-01 | P2 | schema1“顶层值+键数”未明确null与missing、数组whole-value和字段存在，可能不构成完整CAS | tasks E3明确完整键集合/存在/类型/whole-value literal，禁普通query宽松null/数组；TC24在真实4.4验证null↔missing+等键数换键、数组嵌套、$字符串等反例 | data agent闭环 |
| P-02 | P2 | 新V2生成分支未明确首次explicit_tag历史不得查今天胜场；旧ranking explicit_tag已区分历史 | tasks E4入口与发布双层保护，TC50断言历史builder及新装备读0，不升级/覆盖历史；无tag当前正向对照 | data agent闭环 |
| P-03 | P2 | OVERLAY0零访问字面范围可能错误禁止LOCAL1生成或schema2冻结读取 | tasks E5及TC31限定equipment-view动态窗口，新增LOCAL1+OVERLAY0仍生成/发布/冻读场景，仅统计新可选runtime而非全应用repo | data agent闭环 |
| P-04 | P2 | build≤1只测HTTP，没有证明冻结生成和动态共用配额 | tasks预算/E5要求同一实例配额及purpose隔离/取消owner，TC32/PF02/PF05同时执行真实冻结生成与动态构建，等待计预算/HTTP取消不取消独立生成 | data agent闭环 |
| P-05 | P2 | 同timestamp member_revision变化被误当退役，可能永久显示旧成员和旧统计 | tasks接口/E5/E6独立reload_required、epoch失效、重取当前完整冻包；TC33/43/47及PF05验证新成员/三处同版、旧掩码不串用 | display agent闭环 |
| P-06 | P2 | 可选client仅写“bot关闭close”，未列实际生命周期挂接/在途shutdown测试 | tasks E5列bot.py启动/关闭：停止接收→取消有界收尾→close；新增runtime测试及TC31覆盖启用/在途/部分失败/幂等关闭，真实3.9必跑 | display agent闭环 |
| P-07 | P2 | 真链声称只替上游，但router/singletons默认repo无法自动指向随机db，隔离与替换边界矛盾 | tasks真链/test-plan第8节允许装配边界注入真实repo/service/runtime到同一随机库，禁止替换算法/响应；所有client/namespace断言且禁启动bot.py | display agent闭环 |
| P-08 | P2 | 服务端无客户端旧member/source约束，另一实例/新cache无法推断需要重载 | tasks具体GET携带expected_member_revision/expected_frozen_source，bootstrap manifest明确、server/client双比对、frozen来源和dynamic来源分离；pending失败不能恢复被替代冻包，TC33/PF05含跨实例反例 | display agent第二次定向复核闭环 |

## 已确认可执行的计划要素

E0固定dirty全基线并保护并行改动；E1—E8具体模块/函数、数据与接口契约、依赖门槛、TDD测试先行/主agent复跑和共享文件串行整合；Mongo4.4/真实3.9/Node/Chromium及随机库隔离；正常sync用途边界与每轮入队保留；schema2单文档8MiB、结果未知读回、CAS/历史/降级保护；同一视图的QQ/网页/导出与50个TC、5个PF；未来兼容关闭版本、单排名写者、自然榜生命周期与兼容读者回滚。

主agent最后补充：专用inspect与snapshot仓储必须指向同一注入db，避免默认snapshot_repo回到全局目标；与审核过的真链隔离契约一致。现有回归模块清单展开为真实全名，避免复合简称被误当执行模块。

## 仍需在实施后证明

Mongo4.4表达式真实语义及索引执行计划、实际BSON边界/并发/未知写结果、全部TC/PF、主执行者RED/GREEN复跑、真实3.9/app路由导入、实际Node/Chromium/本地html2canvas和QQ产物、1000角色/多客户端预算、在途shutdown与driver资源收束均未执行。缺环境/skip不能作通过；不达性能门槛不启用，不因静态通过跳过实现审核。

用户尚未授权实施或部署；交付待确认的执行计划。之后即使确认编码，生产发布和实际消息仍另按授权范围处理。
