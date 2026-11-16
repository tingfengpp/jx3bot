## ADDED Requirements

### Requirement: 当前赛季最新本地胜场
系统 SHALL 按冻结强身份global_id从主表选目标所在队伍严格获胜的最新当前赛季3v3，同场取装备和队友，不依赖参与者投影完整性，不因装备缺失回退旧场。读取 SHALL 不外呼/入队/写事实，不被奇穴schema影响。

#### Scenario: 最新败场而更早胜场
- WHEN 最近对局为败场而本地有更早胜场
- THEN 选择最近胜场，按其装备/队友统一装配

### Requirement: 展示统计筛选同源
卡片装备、橙武数量及占比、紫武成员及分布 SHALL 从同一view_revision的整榜有效装备计算，不新增栏目。原排名、分数、分类心法 SHALL 保持原榜口径。unknown装备 SHALL 不入橙或紫；每组N=L+P+U，占比保留L/N并明确装备覆盖率和unknown数量，N=0不可用。

#### Scenario: 紫武转橙武
- WHEN 新胜场确认某角色使用大橙武
- THEN 同一次视图提交更新卡片、橙武计数并移除紫武集合，任何时候不得出现紫武榜中该卡片显示橙武

#### Scenario: 缺装备
- WHEN 最新胜场装备不足以确定分类
- THEN 该角色装备unknown，不借旧场/旧字段装备，不算橙也不入紫；保留可确认队友

### Requirement: 整榜完整视图及版本
系统 SHALL 将整榜成员一次读取的有效资料、四范围统计/紫武集合形成完整不可变视图，生成及持久化读者只采用完整且revision一致的数据。动态更新 SHALL 整份替换，不能只刷可见卡片。列表/分布/导出/QQ SHALL 同版，导出固定一个视图。

#### Scenario: 同场补齐
- WHEN 同场装备后续补齐
- THEN 新视图同时修正卡片、橙武统计与紫武集合，旧响应不能回滚新视图

#### Scenario: 完整summary原子发布失败
- WHEN 候选视图尚未完整、原子写失败、BSON超过8MiB或expected_revision条件竞争失败
- THEN 不公开半成品新视图，保留此前完整版本；legacy details是否更新不是V2可见条件

### Requirement: 故障与生命周期
read_failed SHALL 保留整份此前成功视图，初始无V2视图不拼接legacy统计。成功无胜场/真实缺装备、绑定失效和资料矛盾 SHALL 统一在视图中置unknown并同步统计。已确认失效不能因回退恢复。历史榜 SHALL 使用自身冻结整份视图；最高分榜不叠加当前装备。

#### Scenario: 新榜退役旧榜
- WHEN A被B替代为最新
- THEN A整份恢复冻结版本，统计、卡片和紫武集合同时恢复；无撤销差异或相同有效撤销上下文时与重开一致，会话已确认撤销优先且不承诺跨会话持久修正

### Requirement: 有界纯本地刷新与回滚
LOCAL_WIN_EQUIPMENT SHALL 控制V2生成/读取，OVERLAY关闭的equipment-view SHALL 零库访问并恢复整份冻结来源。来源切换 SHALL 不跨源混装；V2启用时旧current-displays不能再独立刷新卡片。整榜读取 SHALL 有界、索引化、单flight缓存复用，并通过1000角色多用户性能门槛后启用。

## 有意替换的旧要求
V1只改展示且装备统计/紫武不变的条款废止。V2替代active jjc-ranking-display-refresh中可见成员独立装备叠加且统计不变的规则；legacy分支保留旧契约。排名/分类、同步事实所有权、纯本地和历史冻结保持。


### Requirement: 完整发布和版本化读者
V2 SHALL 以schema2单个summary文档中的完整equipment_view作为唯一有效读源，整文档BSON不超过8MiB，原子发布失败/超限保留旧完整版本。同timestamp更新 SHALL 条件比较expected_revision。所有分请求 SHALL 校验expected_view_revision，旧客户端无equipment_contract=2不得取得V2后套旧统计算法；旧current-displays SHALL 服务端阻止V2单卡更新。

#### Scenario: 老客户端读新榜
- WHEN 未声明V2能力的旧页面请求V2快照或已启用V2的最新榜
- THEN 明确要求升级，不向旧算法返回V2混合字段

### Requirement: 所有消费者同一聚合
V2网页、QQ合图/拆图和导出 SHALL 消费service统一L/P/U/N、L/N及覆盖，不能被旧weapon重算或equipment_incomplete全栏隐藏规则覆盖。

#### Scenario: 未知装备仍可显示统计
- WHEN N=10、L=2、P=5、U=3
- THEN 各消费者均显示20%橙武和70%装备覆盖，紫武集合5人

### Requirement: 缓存资格和页面交互
缓存命中/构建返回前 SHALL 重验最新完整榜、scope、成员版本和身份；资格变化即使revision相同也必须处理。页面整份采用 SHALL 保留稳定键对应展开/滚动/焦点。撤销掩码 SHALL 绑定榜单/成员版本/身份，恢复冻结和切源前统一应用，不因故障/禁用自动解除。

#### Scenario: A缓存后B发布
- WHEN A命中完整内容缓存但B已成为最新完整榜
- THEN A返回retired及latest_timestamp，整份恢复A冻结来源，不继续叠加
