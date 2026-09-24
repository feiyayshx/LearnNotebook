# Artifact ownership

## 何时读取

在初始化项目、记录用户新信息、修改需求/架构/计划、判断文档冲突或准备迁移状态前读取。其他流程只引用本文件，不得各自发明事实归属。

## 核心规则

- Skill 保存方法；目标仓库保存事实、状态和证据。
- **全部过程制品统一放在项目主目录的 `delivery-docs/` 下，不散落到工程源码目录**；唯一例外是 OpenSpec CLI 约定的 `openspec/` 保留在项目根。
- 每类事实只能有一个权威位置；其他副本必须标记为派生视图。
- Qoder Memory、聊天、Quest Spec 和 To-do 都不是长期事实源。
- 不静默覆盖原始 Spec、已确认范围、验收标准或历史决策。
- `delivery-docs/state/state.json` 是 M0–M3 helper 的规划检查点，只能由 `deliveryctl` 的 transition、set-context、plan-seal/plan-invalidate 或精确 journal recovery 更新；不得手工扩展为实现/验收状态。

## 权威位置

| 信息 | 权威位置 |
|---|---|
| 双轨路由判定与轨道状态 | `delivery-docs/state/track.md` |
| EXPRESS 意图速记与用户反馈 | `delivery-docs/intent/express-intent.md`、`delivery-docs/intent/feedback.md` |
| 原始 Spec、原型及来源版本 | `delivery-docs/product/source/`（EXPRESS 轨可先放 `delivery-docs/intent/`） |
| 已确认需求与修订 | `delivery-docs/product/requirements.json` |
| 验收标准与修订 | `delivery-docs/product/acceptance.json` |
| Feature Ledger | `delivery-docs/product/feature-ledger.json` |
| 里程碑与垂直切片 | `delivery-docs/plans/delivery-roadmap.json` |
| 产品决定及原因 | `delivery-docs/decisions/product/`（PDR） |
| 当前已交付行为 | `openspec/specs/` |
| 正在发生的变更 | `openspec/changes/` |
| 当前架构 | `delivery-docs/architecture/` |
| 架构决定及替代关系 | `delivery-docs/decisions/adr/` |
| 路线图和计划 | `delivery-docs/plans/` |
| M0–M3 规划状态和恢复位置 | `delivery-docs/state/state.json` |
| 每个切片的 Delivery Contract | `delivery-docs/state/work-items/<change-id>.json` |
| 规划新鲜度与批准绑定 | `delivery-docs/state/planning-manifest.json` |
| 原型语义、状态、交互和允许偏差 | `delivery-docs/product/prototype-contract.json` |
| M3 后 Change 执行授权 | `delivery-docs/state/execution/<change-id>/execution-manifest.json` |
| AC 到 task/code/test/evidence/review/integration | `delivery-docs/state/execution/<change-id>/verification-matrix.json` |
| 有界实现与修复轮次 | `delivery-docs/state/execution/<change-id>/attempts.jsonl` |
| 硬门禁、质量评分和独立评估绑定 | `delivery-docs/state/execution/<change-id>/quality-scorecard.json` |
| 未决问题、临时假设 | `delivery-docs/state/questions.json`、`delivery-docs/state/assumptions.json` |
| 追加式执行记录 | `delivery-docs/state/events.jsonl` |
| 测试结果和验收证据 | `delivery-docs/verification/`；大型制品可外置并保留索引和校验值 |
| UI 闭环走查证据与缺陷台账 | `delivery-docs/verification/loop-<时间戳>/` |
| 阶段/验收报告 | `delivery-docs/reports/` |
| 稳定项目规则 | `delivery-docs/governance/`；简要执行入口可在 `AGENTS.md`、`.qoder/rules/` |

Qoder-native 执行阶段的事实归属：

| 执行事实 | 权威位置 |
|---|---|
| Change 任务进度 | `openspec/changes/<change-id>/tasks.md` |
| 实际代码状态 | Git commit / 当前 Worktree diff |
| 测试事实 | 项目测试或 CI 原始输出及 `delivery-docs/verification/` 索引 |
| Web 验收 | UI 闭环走查 `loop-<时间戳>/report.md` + Playwright report/trace/video；Browser/Chrome 探索只作诊断 |
| 暂停、阻塞和恢复点 | `delivery-docs/state/runs/<run-id>/` 与 `delivery-docs/verification/runs/<run-id>/report.md` |
| Qoder Quest/Goal/To-do | 派生执行视图，不是仓库权威 |

Stage B 制品由 `loopctl init-execution` 首次创建，之后由执行者按照真实工具结果维护，并由 `loopctl record-attempt` / `validate` 追加或校验。执行清单不得覆盖重建；范围、Acceptance、风险或原型契约变化时先进入 Change Control，旧证据失效并建立新的批准绑定。

Brownfield 不强制搬迁已有体系。用 `delivery-docs/state/manifest.json` 将上述概念映射到现有路径，坚持 adopt, don't replace。

机器可校验规划图使用严格 JSON；Markdown 可作为派生可读视图。若 Brownfield 已有权威目录，manifest 必须映射它且只能有一个可修改的权威表示。详见 [planning-and-traceability.md](planning-and-traceability.md)。

## 信息分类步骤

1. 保留用户输入原文、来源、时间和内容摘要。
2. 判断它是新需求、需求澄清、产品决定、架构决定、验收变化、临时假设、阻塞问题还是延后项。
3. 分配稳定 ID：`REQ`、`AC`、`FEATURE`、`SLICE`、OpenSpec kebab-case Change、`CHECK`、`PDR`、`ADR`、`RUN`/`EVID`。
4. 写入对应权威制品，并记录 revision、来源和受影响 ID。
5. 验收或范围变化时，使受影响的计划、测试映射和旧证据失效。
6. 更新追踪关系、Stage B 验证矩阵/attempt ledger 和事件日志，再决定是否恢复实现。

## 输入与输出

**输入：** 用户消息、Spec/原型、OpenSpec 制品、代码和测试结果、既有文档、当前状态。

**输出：** 归类后的权威制品更新、稳定 ID、影响关系、失效标记、追加事件和明确的下一动作。

## 硬门禁

- 同一事实不得存在两个可独立修改的权威副本。
- 所有范围内 Requirement 必须映射至少一个 Acceptance；所有实现 Change 必须关联 Requirement 或明确的工程理由。
- Qoder 派生视图与 OpenSpec 冲突时，以 OpenSpec 为变更规格权威并停止执行，先完成对账。
- ADR/PDR 不覆盖历史；通过 supersede 记录新决定。
- Helper 状态不得由普通文本编辑越过迁移脚本；执行“完成”必须由 OpenSpec task truth、Git 和测试证据共同支持。
- `acceptance-ready` 必须由 execution manifest、完整验证矩阵、最后一轮通过、独立评估、质量硬门禁和 `loopctl validate --final` 共同支持。
- 不得在日志、报告、Spec 或证据中保存密钥和令牌。

## 停止条件

进入 `WAITING_USER` 并报告：事实来源冲突且无法确定权威；用户修改已确认范围但影响未确认；稳定 ID 重复或追踪链断裂；状态文件与事件日志无法对账；原始输入发生未解释变化。
