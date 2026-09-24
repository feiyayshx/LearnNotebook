# Qoder harness routing

## 何时读取

在每个 Change 开始、风险变化、需要隔离或并行、选择 Quest/Goal/Experts、使用浏览器工具或 Worktree 合并前读取。

## 路由原则

Skill 只选择和约束 Qoder 原生 Harness，不重复实现 Quest、Goal、Experts、Worktree、Browser Agent、Playwright 或 Chrome MCP。选择最小但足以控制风险的模式。

## 模式选择

| 场景 | 推荐模式 |
|---|---|
| 复杂新功能、Greenfield 垂直切片 | Quest Agent，从 OpenSpec Change 派生执行视图 |
| 覆盖率、性能、明确迁移或有量化终点的修复 | Goal-style 契约；若 Qoder 有 Goal 入口则使用，否则写入 Agent/Quest；必须设置 Oracle、轮数和停止条件 |
| 跨前后端、高安全风险、复杂集成 | Experts，分离 Planner/Implementer/QA/Reviewer |
| 小型、低风险、易回滚的局部修改 | Local Agent |
| 中大型、多轮 Apply、身份权限、数据模型或高回归风险 | 独立 Worktree |
| 页面探索、复现和运行时诊断 | Browser Agent / Chrome MCP，结论需固化为可重复测试 |
| 正式 Web 验收 | Playwright 或项目已有等价自动化套件 |

## 路由步骤

1. 读取 Delivery Contract，评估修改范围、耦合、风险、执行轮次和环境需求。
2. 检查当前工作区未提交修改、端口、数据、测试账号、服务和工具可用性。
3. 选择 Local 或 Worktree，再选择普通 Quest、Goal 或 Experts；把环境、集成 owner、理由和预算写入 execution manifest/handoff。
4. 每个完成的 Change 都要有独立 evaluator；高风险/跨层工作优先 Experts，给每个角色稳定输入/输出契约，QA/Reviewer 读取原始 Spec、原型、diff 和证据，不依赖 Implementer 的自我总结。
5. 若并行，只并行无依赖、无共享文件/状态写入的 Changes，并指定唯一集成者和合并顺序。
6. 若用浏览器探索，把确认的缺陷和路径固化为 Playwright/系统测试，不用一次性视觉判断宣布验收通过。
7. Worktree 完成后由集成者合并，并在集成分支重新运行受影响门禁。

若使用增强保障路径，M3 helper 只生成带 Planning Seal、IDs、风险与验收 Oracle 的 handoff；随后由 Qoder 原生启动 Agent/Quest/Goal/Experts 或 Worktree 实现。若当前 Agent 无法直接创建所选环境，输出完整 handoff 并请求用户在 Qoder 中启动，不得假装已经启动。使用 Worktree 前读取 [git-worktree-scope.md](git-worktree-scope.md) 并绑定可信 checkout identity。

实现阶段不尝试让 `deliveryctl` 进入 `EXECUTING`。OpenSpec Change 是变更权威；Stage B execution manifest 是执行授权，verification matrix/attempts/scorecard 是验证权威；Quest Spec/To-do 只携带这些路径和摘要、Change ID、Requirement/Acceptance IDs、起始 commit、Oracle、预算和停止条件，不维护第二份长期规格。

## Worktree 约束

- 每个 Worktree 使用可配置端口、独立测试数据和明确启动/清理方式。
- Worktree 只维护本 Change 制品和运行证据，不并发写主 `openspec/specs/`、归档目录或全局 `delivery-docs/state/state.json`。
- 单一集成者负责同步主 Specs、迁移全局状态、归档和跨 Change 回归。
- 紧密耦合、顺序迁移或修改相同文件集合的 Changes 必须串行。
- Worktree 局部通过不等于集成完成；旧证据在合并 commit 上默认失效。

## 输入与输出

**输入：** Delivery Contract、风险等级、依赖图、工作区状态、工具/环境和预算。

**输出：** 记录在 run 状态中的执行模式、环境、角色契约、隔离策略、预算、集成责任和验证计划。

## 硬门禁

- 未检查用户未提交修改前不得直接在 Local 写目标文件。
- 没有唯一集成者、文件所有权或隔离环境时不得并行 Worktree。
- Goal 必须有确定 Oracle、最大轮数和无进展判定，不能当无限循环。
- QA/Reviewer 结论必须基于原始制品和运行证据，而不是实现者摘要。
- Browser Agent 或 Chrome MCP 的探索结果不能单独迁移到 `ACCEPTANCE_VERIFIED`。

## 停止条件

目标 Worktree/分支身份不明；环境之间共享并污染数据；并行任务发生文件或状态冲突；工具权限不足；执行模式无法提供所需 Oracle；合并产生无法自动判定的语义冲突。可安全降级为串行/Local 时记录降级，否则请求用户。
