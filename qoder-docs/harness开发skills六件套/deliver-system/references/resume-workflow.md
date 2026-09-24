# Resume workflow

## 何时读取

发现 `delivery-docs/state/manifest.json`、用户要求继续中断交付、Qoder/进程重启、分支或 Worktree 在上次检查点后变化时读取。恢复检查必须先于任何业务代码修改。

## 输入

- `delivery-docs/state/manifest.json`、`state.json`、`events.jsonl`、`questions.json`、`assumptions.json` 和最近的 run 报告。
- 当前 Git 分支、commit、工作区 Diff 和 Worktree 状态。
- 当前 OpenSpec Specs/Changes、计划、ADR/PDR 和源 Spec 摘要。
- 最近 Gate Manifest、测试制品、配置和测试套件摘要。
- 当前 Change 的 `delivery-docs/state/execution/<change-id>/` 执行清单、验证矩阵、attempt ledger 和质量评分。

## 步骤

1. **只读校验控制面。** 验证 manifest/state Schema、Harness 版本、必需路径和事件日志连续性；若存在 `delivery-docs/state/transaction.json`，先停止普通流程，只使用 `deliveryctl recover` 处理与 journal 精确匹配的中断写入。
2. **确认执行上下文。** 比较当前分支、worktree、baseline commit、active Change 与上次恢复位置；保护未知和用户修改。
3. **检查输入漂移。** 比较源 Spec/原型摘要、Requirement/Acceptance revision、Planning Seal 和待确认用户决定。
4. **检查规格漂移。** 对账 active OpenSpec Change、当前 Specs、Delivery Contract 和 Qoder 派生执行视图。
5. **检查规划/证据新鲜度。** 若仍在规划阶段，重新计算 catalog/roadmap/contract/OpenSpec/config/Git scope 与 Planning Seal。若已由 Qoder 开始实现，先对当前 Change 运行非 final `loopctl validate`，再检查 OpenSpec tasks、Git、验证矩阵、attempt ledger、run 报告和测试证据是否对应当前 commit、环境、配置与测试套件；任一关键值变化即使对应证据失效。
6. **分类漂移。** 区分上轮预期实现、外部但兼容的修改、冲突修改、缺失制品和无法解释的状态迁移。
7. **重建基线。** 仅规划时登记 Baseline Smoke/受影响测试候选；Qoder-native 执行恢复时在确认命令和环境后实际运行最小 Baseline Smoke，并把前一 commit 的旧“通过”结论标为失效。
8. **确定恢复点。** 从验证矩阵中第一个仍有效且未完成的 Acceptance 行继续；结合最新 attempt 生成准确的下一动作、Oracle 和剩余预算。
9. **记录恢复。** 规划检查点只通过 `deliveryctl` 更新；实现恢复点写入 execution bundle、OpenSpec tasks、计划日志和 run 报告，不直接手改 state 伪造 `EXECUTING` 或完成阶段。

## 漂移处理

| 情况 | 处理 |
|---|---|
| 仅新增、与 active Change 无冲突 | 登记来源，重新运行受影响门禁后继续 |
| source/catalog/OpenSpec/config/Git scope 变化 | 使 Planning Seal 失效，回退澄清/规划 |
| 代码变化但旧证据对应前一 commit | 使证据失效，回退到可验证状态 |
| prototype contract、evidence digest 或 integration commit 变化 | 使相关验证矩阵行和 scorecard 失效，重新验证 |
| 用户改变需求或验收 | 进入 Change Control，更新权威制品和影响分析 |
| Qoder 视图与 OpenSpec 不一致 | 停止执行，以 OpenSpec 为变更权威完成对账 |
| 状态声称完成但无证据 | 回退至最近合法状态并记录完整性事件 |
| Harness/Schema 版本不兼容 | 只生成迁移建议，不猜测升级 |

## 输出

- 恢复审计结果、漂移分类和失效证据清单。
- 规划阶段的 Baseline Smoke 计划，或 Qoder-native 执行阶段已实际重建的 Baseline/阻塞报告。
- 合法的规划事件或执行进度记录、剩余预算及下一准确动作。

## 硬门禁

- 不能读取或验证控制面时不得继续自动实现；不得手动删除或猜测修复 transaction journal。
- 证据不得跨 commit、配置或测试套件摘要复用。
- 存在未知 Diff 时不得清理、覆盖或假定属于 Skill。
- 不得越过未回答的人工门禁、阻塞问题或失败状态。
- OpenSpec、代码和状态不能对账时不得归档或声明完成。

## 停止条件

Schema 不兼容且无迁移器；事件链损坏；active Change 缺失；当前分支/Worktree 身份不明；输入或验收发生冲突变化；未知修改覆盖目标文件；无法重建基线；需要用户决定保留、合并还是放弃某组变化。生成恢复报告后进入 `WAITING_USER` 或 `BLOCKED`。
