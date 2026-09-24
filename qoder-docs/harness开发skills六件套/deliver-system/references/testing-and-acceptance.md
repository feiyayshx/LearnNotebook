# Testing and acceptance

## 何时读取

在编写 Delivery Contract、选择测试层级、进入任何验证 Gate、处理失败/flaky、准备 OpenSpec Verify/Archive、里程碑集成或验收签署前读取。

## 总原则

每个 OpenSpec 功能 Change 都必须有风险匹配的自动化代码测试；但不在每次微小编辑后运行全量 E2E。每个用户可见、跨模块、跨服务或跨数据边界的 Change 必须有可重复的验收级测试，Web 旅程优先 Playwright。

**含 UI 的交付另受 [ui-loop-verification.md](ui-loop-verification.md) 的闭环走查硬门禁约束**：走查 → 缺陷 → 修复 → 重验，直到全部旅程通过或达轮次上限；证据落 `delivery-docs/verification/loop-<时间戳>/` 并绑定 commit。

“端到端”指从最外层稳定入口验证可观察结果：Web 用浏览器、API 用真实 HTTP、Worker 用队列/数据库黑盒流程、CLI 用独立进程、库用公共契约与消费者测试。

## 风险与最低测试

| 风险 | 典型变更 | 最低要求 |
|---|---|---|
| R0 | 纯文档、无行为变化 | 文档结构、链接和规格一致性 |
| R1 | 内部重构、日志、局部样式 | 静态检查、目标单元、受影响回归、核心 Smoke |
| R2 | 普通页面、API、业务规则 | R1 + 组件/契约/集成 + 目标验收测试 |
| R3 | 认证、权限、公共 API、跨模块、第三方 | R2 + 负向、安全、故障和兼容路径 |
| R4 | 支付、多租户、不可逆迁移、生产基础设施 | 全层、迁移/回滚、恢复、性能、安全及人工审批 |

影响不明时提升一级。覆盖率只是辅助指标；范围内 Requirement 到 Acceptance Test 的映射必须达到 100%。

## 分层 Gate

1. **Gate 0 — 开始前：** 固定环境、Seed/Reset、账号和 baseline commit；运行 Baseline Smoke，Brownfield 单独记录旧失败。
2. **Gate 1 — 小批次：** 格式、Lint、类型检查、目标单元/组件；API/数据库变化增加契约与集成测试；结果进入当前 execution attempt。
3. **Gate 2 — Change 完成：** 全部受影响测试、目标验收级 E2E、**UI 闭环走查全部通过**、错误/负向状态、Console/Network/服务日志、独立 QA，逐 AC 填写 Feature verification matrix 并生成绑定 commit 的证据。
4. **Gate 3 — Verify/Archive：** 对账 Requirement、Acceptance、Delta、任务、代码、测试、决策、文档和证据；运行目标 E2E 与受影响回归。
5. **Gate 4 — 里程碑集成：** 合并后的干净构建、全量集成/E2E、**全旅程 UI 闭环走查**、原型状态/交互/视觉一致性、迁移/回滚、兼容、安全、性能、可访问性和必要视觉回归。
6. **Gate 5 — 发布/验收：** 预发布复验、关键旅程、发布/回滚演练、最终追踪矩阵、独立 evaluator、质量 scorecard、`loopctl validate --final`、已知问题和 Acceptance Report。

## 证据要求

Gate Manifest 至少记录：Change ID、commit、环境、配置摘要、测试套件摘要、开始/结束时间、各门禁结果、制品路径和总状态。截图、Trace、视频、Console/Network 摘要必须可追溯到同一 run；大型文件可外置，但仓库保留校验值和索引。

证据 commit、配置、测试摘要、源 Spec/原型摘要或 evidence SHA-256 与当前状态不一致时立即过期。测试通过不等于业务接受；技术验证与用户/安全/发布审批分别记录。

## 失败和 flaky

1. 分类为产品、测试代码、环境、权限、外部服务或 flaky。
2. 保存稳定失败指纹、最小复现和每次尝试的有效变化。
3. 同一失败自动修复最多 3 次；相同指纹连续 2 次无进展即暂停。
4. 重试不能把产品缺陷改写为 flaky；flaky 必须登记原因、Owner、隔离条件和修复期限。
5. 验收报告显示所有重试、跳过和不稳定测试，不用“最后一次通过”隐藏历史。
6. 每次失败在 verification matrix 登记并重新打开对应 Acceptance/OpenSpec task；修复后在集成 commit 复验并追加 attempt。

## 输入与输出

**输入：** Delivery Contract、风险、Acceptance Catalog、项目测试框架、环境、实现和 baseline。

**输出：** 测试计划/代码、Gate Manifest、运行制品、失败分类、追踪关系和技术验收结论。

## 硬门禁

- 没有明确 Oracle、环境和测试数据时不得宣称验证完成。
- 用户可见/跨边界 Change 没有目标验收测试不得进入 `ACCEPTANCE_VERIFIED`。
- 含 UI 的 Change 未完成闭环走查（全部旅程通过或用户明示接受余留缺陷）不得宣称验证完成。
- Worktree 证据合并后必须在集成 commit 上重跑必要 Gate。
- 跳过、隔离或 flaky 测试不得静默计为通过。
- R3/R4 的负向、安全、迁移/回滚或人工审批缺失时不得发布。
- 不存在完整 verification matrix、独立评估、全通过硬门禁和至少 90 分质量 scorecard 时不得声明 `acceptance-ready`。

## 停止条件

无法建立可信环境；相同失败达到阈值；两轮没有新增通过项或减少失败；测试结果不可重复；需要真实凭据或危险数据操作；Oracle 依赖未决业务判断。保存证据和准确恢复点后报告用户。
