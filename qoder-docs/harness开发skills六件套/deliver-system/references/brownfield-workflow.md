# Brownfield workflow

## 何时读取

当已有可运行代码或已交付行为，用户要求新增功能、修改需求、修复跨层缺陷或演进架构时读取。即使项目文档不完整，也优先走 Brownfield，而不是把仓库当新项目重建。

## 核心原则

- Adopt, don't replace：采用已有架构、命令、测试、CI、文档、ADR 和规则。
- 只为受影响能力建立足够可信的当前行为基线，不要求先反向规格化整个系统。
- 新行为用 OpenSpec Delta Change 表达；旧行为用 Preservation Requirements 和 Characterization Tests 固定。
- 以最小侵入方式实现，计划外重构必须单独进入 Change Control。

## 输入

- 现有仓库、运行和部署方式、当前分支/commit。
- 新需求、修改后的验收标准和相关原型。
- 已有 OpenSpec、文档、测试、CI、Rules/Hooks 与已知故障。
- 可使用的测试数据、环境和权限。

## 步骤

1. **只读侦察。** 盘点技术栈、模块边界、启动/测试命令、数据和外部服务、CI、文档、规则与未提交用户修改。
2. **建立健康基线。** 增强规划阶段先记录项目声明的最小构建、Smoke、受影响测试候选、基线 commit 与已知失败；进入 Qoder-native 实现前确认命令和环境，再实际运行最小基线。快速路径直接从这项受控基线开始。
3. **定位影响面。** 映射入口、调用链、数据模型、权限、契约、下游消费者和受影响用户旅程；无法确定时提升风险等级。
4. **保护旧行为。** 写出 observable Preservation Requirements；规划阶段的代码/测试/文档推断只能是 candidate/not_run。进入实现后，为无可靠测试的关键行为先补 Characterization Tests，再修改产品行为。
5. **规范化新需求。** 分配 Requirement/Acceptance ID，明确兼容、迁移、回滚、弃用和非目标；识别 Spec、原型与现状冲突。
6. **登记现有体系。** 用 manifest 映射既有路径，不强制创建或搬迁全部标准目录。
7. **规划 Delta Change。** 一个可独立验收的垂直切片保留一个 Change ID；Delivery Contract 同时关联新验收和保留行为，只 materialize 下一个依赖满足且获批的 Change。
8. **建立执行授权并选择环境。** 用户批准后建立 Stage B execution manifest。中大型、跨层、身份权限、数据模型或高回归风险变更优先 Worktree；小型低风险变更可 Local。
9. **最小侵入实现。** 小批次运行目标测试并记录 attempt；出现范围或设计变化先更新 Requirement/OpenSpec/ADR，再继续代码。
10. **回归与集成。** 运行新功能验收、Preservation 回归、负向/兼容/迁移测试；逐 AC 维护验证矩阵。Worktree 合并后在集成分支重新验证，由独立 evaluator 复核；硬门禁全过、质量至少 90 且 final validate 通过后才进入 acceptance-ready。

## 输出

- 只读侦察和基线报告、已有失败清单。
- 影响分析、Preservation Requirements、Characterization Tests。
- OpenSpec Delta Change、Delivery Contract、必要的 ADR/PDR。
- 最小范围实现、回归证据、迁移/回滚说明及更新后的当前 Specs。

## 硬门禁

- 未建立可信基线前不得把所有失败归因于本次变更。
- 不覆盖用户未提交修改，不擅自替换测试框架、目录、架构或文档体系。
- 关键受影响旧行为没有 Preservation Requirement 或验证 Oracle 时不得实现。
- 新行为通过但保留行为回归时，Change 仍然失败。
- Worktree 内证据不能直接证明集成完成；合并后必须重跑受影响门禁。
- OpenSpec Change 是变更权威，Quest 计划只能作为派生执行视图。
- 新功能通过但 Preservation、独立评估、证据新鲜度或质量硬门禁失败时，Change 仍然失败并重新打开对应任务。

## 停止条件

系统无法启动且失败来源不明；现有失败使新回归无法判定；未提交修改与目标文件重叠；数据/公共 API/权限兼容策略需要用户决定；无法安全构造测试数据；迁移或回滚不可验证；修改范围开始扩张到未批准重构。停止时保存影响、证据和推荐选项。
