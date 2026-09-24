# Planning and traceability

## 何时读取

在 M3 规范化输入、提问、确认需求、拆分垂直切片、编写 Delivery Contract、创建 Planning Seal、恢复已存在规划或处理追踪失败时读取。

## 当前能力边界

M3 只能把输入推进到 `PLANNING / plan_ready`。它可以生成和校验规划制品，也可以为下一个已获批且依赖满足的切片创建 OpenSpec scaffold；不得修改产品/测试代码、执行项目命令、Apply/Verify/Sync/Archive、声称测试通过或进入 `EXECUTING`。

## 权威规划图

默认路径：

```text
delivery-docs/product/requirements.json
delivery-docs/product/acceptance.json
delivery-docs/product/feature-ledger.json
delivery-docs/plans/delivery-roadmap.json
delivery-docs/state/work-items/<change-id>.json
delivery-docs/state/planning-manifest.json
```

Brownfield 可在 `delivery-docs/state/manifest.json` 映射既有权威路径，但不能复制成第二份可独立修改的事实。Markdown/PDR/ADR 是可读决策记录；Qoder Quest Spec/To-do 是派生执行视图。

## 规范化步骤

1. **登记来源。** 对每个 Spec、原型和补充输入保存项目内相对路径、SHA-256、来源和 locator。HTML 原型只静态读取；不执行脚本、不加载外部资源。
2. **提取候选。** 区分事实、需求、验收、产品决定、架构决定、假设、问题、范围外和延后项。推断内容必须标为候选；每个 Requirement 都要写 `sensitivity_assessment.categories` 和非空 rationale，即使结论为空也必须留下明确评估。
3. **分配稳定 ID。** 使用 `REQ-*`、`AC-*`、`FEATURE-*`、`SLICE-*`、`PDR-*`、`ADR-*`；新增内容不得重排旧 ID。
4. **建立 Oracle。** 每个范围内 Requirement 至少一个外部可观察 Acceptance；记录前置、动作、预期、边界、正/负向、`risk_coverage` 和自动化意图。负向/回归 Acceptance 必须用受控枚举精确声明 failure、permission、compatibility、migration、rollback、security、privacy、external_cost、production_data 等覆盖，不能用一个无关失败场景冒充。`manual` 不等于已验证。
5. **处理 Brownfield 保留行为。** 只提取本次影响面的行为。代码、测试或文档推断是 `candidate/not_run`；必须有 observable statement、稳定边界和 planned regression oracle。
6. **解决问题和决定。** 范围、关键异常、权限、数据、Breaking、迁移/回滚、安全、外部依赖/费用和架构后果需要用户或相应 Owner 确认。ADR 放在 `delivery-docs/decisions/adr/*.md`，PDR 放在 `delivery-docs/decisions/product/*.md`；一级标题必须包含对应 ID，并有 `Status:`。`approved` ID 只有在文档状态为 `Approved`/`Accepted` 且内容摘要被绑定时才有效。R3/R4、非 `none` migration、required rollback 或任一 `sensitive_changes` 必须同时引用已批准 ADR/PDR，Delivery Contract 的 `human_approvals` 只能引用其中已批准的决定。
7. **拆分路线图。** Feature 以用户可见结果组织；按依赖形成无环垂直切片。默认每片最多 8 个范围内 Requirement、12 个 Acceptance，超出需要 PDR 且仍不能混合无关结果。
8. **写 Delivery Contract。** 绑定范围/非目标、IDs、保留行为、风险、影响边界、受控 `sensitive_changes`、ADR/PDR、迁移/回滚、精确 risk coverage、M4 计划测试和停止条件；执行状态固定为 locked。
9. **JIT OpenSpec。** 所有切片保留稳定 Change ID，但一次只 materialize 下一个依赖满足、用户批准的切片；使用 runtime instructions，任务保持未勾选。
10. **确定性验证和 Seal。** 先运行 plan validation。没有 error、无阻塞问题、OpenSpec/config/Git/source binding 新鲜且有审批引用时，才写 Planning Seal 并事件化绑定 state。

## 完整映射

```text
source digest + locator
→ REQ
→ AC + observable oracle
→ FEATURE
→ SLICE + DAG/milestone
→ reserved/materialized OpenSpec Change
→ Delivery Contract
→ Planning Seal
```

以下都失败关闭：重复 ID、孤立 Requirement/Acceptance/Feature/Contract、无 Oracle、deferred 泄漏到活动切片、DAG 环、重复 Change、过大无 PDR、Brownfield 只有文件级保留、高风险/敏感变更缺少批准或精确负向/回归 risk coverage、checked task、缺失 runtime OpenSpec artifact/指导/依赖摘要、任一绑定摘要漂移。Manifest 的规划权威路径不能进入 `.git`、`.qoder`、`openspec`、`delivery-docs/state`（含旧版 `.delivery`），也不能互为祖先/后代。

## 用户审查点

在 seal 前向用户展示：

- 范围与非目标；
- Requirement/Acceptance 覆盖和所有推断/假设；
- 异常、权限、兼容、迁移/回滚和安全情形；
- Feature、依赖、里程碑、切片边界和过大例外；
- 当前 materialized Change 与后续 reserved Changes；
- ADR/PDR、风险、计划测试、人工门禁和 Qoder 执行建议。

“机械覆盖率 100%”只表示引用完整，不代表需求语义正确。审批必须有非敏感引用，不能伪造为用户认可。

## 恢复和变化

恢复时重新计算 source、catalog、decision、roadmap、contract、OpenSpec/config 和 Git scope 摘要。任何变化使旧 Seal 过期并退回澄清/规划；不得靠更新 state 中的 digest 继续。用户新增需求先更新权威图和影响分析，再重新审查、验证和 seal。

## M3 完成声明

唯一允许的里程碑声明：

```text
phase = PLANNING
planning_stage = plan_ready
next_action = await-m4-before-apply
```

`planning-complete` 报告必须再次采集当前 live bindings，不能只复用 Seal 内的旧值。报告必须称“规划已就绪”，不能称“系统完成”“测试通过”“可验收”或“已交付”。
