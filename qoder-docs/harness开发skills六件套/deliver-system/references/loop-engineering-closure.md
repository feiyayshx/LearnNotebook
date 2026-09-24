# Loop Engineering closure

## 何时读取

在 M3 规划完成或快速路径 Change 获批后、开始 `Apply tasks` 前、每轮实现/修复后、独立评估后以及准备 `Sync specs`、`Archive change` 或声明 `acceptance-ready` 前读取。

## 闭环模型

Stage B 不修改 M0–M3 helper 状态。它用仓库内的执行制品把七层循环串起来：

```text
Source loop      Spec/原型变化 → 影响分析 → 重新批准
Plan loop        Requirement/Acceptance/Change/任务覆盖 → Planning Seal（增强路径）
Change loop      一个垂直 Change → 小批实现 → 目标测试
Feature loop     AC → task → code → test → evidence → review → integration
Repair loop      失败分类 → 最小修复 → 重验 → 有界停止
Delivery loop    集成回归 → 质量评分 → 独立评估 → 验收报告
Learning loop    缺陷/偏差 → 更新 Spec、ADR、规则、测试与后续计划
```

任何一层发现范围、验收、风险或设计事实变化，都返回相应上游权威制品；不能只改代码、报告或聊天结论。

## Stage B 权威制品

每个 Change 使用独立目录：

```text
delivery-docs/state/execution/<change-id>/
├── execution-manifest.json   # 执行授权、范围、起始 commit、预算、环境
├── verification-matrix.json # AC 到任务/代码/测试/证据/集成的唯一映射
├── attempts.jsonl           # 追加式实现与修复轮次
└── quality-scorecard.json   # 硬门禁、100 分评分、独立评估
```

有原型时，先从模板建立 `delivery-docs/product/prototype-contract.json`。每个原型项目必须有稳定 ID、定位、Requirement/Acceptance、状态、断点、可访问性期望和视觉 Oracle。原型不适用时，在执行授权中记录具体原因，不能默认为空。

## 建立执行授权

只有在用户批准当前 Change 的范围、非目标、Acceptance、风险与环境后，才能运行（脚本路径相对于 SKILL.md 解析；Windows 上使用 `python` 代替 `python3`）：

```text
python3 scripts/loopctl.py init-execution \
  --project-root . \
  --change-id <change-id> \
  --route <greenfield|brownfield|resume> \
  --path <quick|enhanced> \
  --source <project-relative-spec-or-openspec-path> \
  --approval-ref <durable-reference> \
  --requirement <REQ-ID> \
  --acceptance <AC-ID> \
  --scope <scope-item> \
  --non-goal <non-goal> \
  --integration-owner <owner> \
  --prototype-contract delivery-docs/product/prototype-contract.json \
  --prototype-required
```

无原型时去掉两个 prototype 参数，增加 `--prototype-not-applicable-reason <reason>`。`--source` 可重复；快速路径至少绑定一个真实的项目内 Spec/OpenSpec/需求文件，工具从这些绑定自动计算 source digest。增强路径还要提供规划中的 `--source-digest <sha256>` 并绑定现有 `delivery-docs/state/planning-manifest.json`。命令拒绝覆盖已存在的执行目录；恢复工作时读取它，不能重新初始化。

`execution-manifest.json` 是 M3 后开始实现和 OpenSpec Apply 的授权桥。Planning Seal 中的 `implementation_authorized=false` 仍然正确：Seal 只证明规划完整；后续显式批准和执行清单才授权实际实现。派生的 Quest/Goal/To-do 不能扩大这份授权。

## 每轮实现与修复

每轮开始写清单一目标、Oracle、起止 commit、允许范围和预算。执行最小有效批次，运行匹配 Gate 的测试，然后从 `assets/templates/execution/attempt.json.tmpl` 建立一次事实记录并追加：

```text
python3 scripts/loopctl.py record-attempt \
  --project-root . \
  --change-id <change-id> \
  --attempt-file <project-relative-attempt.json>
```

只有新增通过项、失败减少、有效 diff、状态前进或最终通过才算有效进展。工具达到总尝试、相同失败或无进展上限时返回 `stop=true`；此时保存报告并进入 `WAITING_USER`、`BLOCKED` 或 `PAUSED`，不得换提示词继续相同循环。

失败进入修复循环时必须：

1. 在验证矩阵登记缺陷及其 AC、任务和证据；
2. 将相应行退回 `implemented` 或 `planned`，OpenSpec 任务恢复为未完成；
3. 更新项目计划与日志；
4. 实施最小修复，重跑受影响测试；
5. 合并后在集成 commit 重验；
6. 追加新 attempt，保留第一轮失败事实。

## Feature verification matrix

每个授权 AC 只能由一个行拥有；多个 AC 可以在同一行表示一个不可分割的验收场景。最终每行必须绑定：

- Requirement、Acceptance 和适用的 prototype item；
- OpenSpec task 或稳定任务引用；
- 真实存在的代码路径；
- 测试 ID、层级、可复现命令和结果；
- 仓库内证据路径及 SHA-256；
- 集成 commit 和环境；
- 独立 Reviewer、结果和证据；
- `integrated` 状态。

不能以行数、任务勾选或 Agent 自述代替完整覆盖。证据文件变化后旧摘要立即失效。

## 原型闭环

原型不是一张首页截图。至少验证适用项目的默认、加载、空、错误、成功、权限/禁用状态，关键交互，响应式断点，可访问性期望和视觉 Oracle。使用 Playwright 保存可重复的旅程、断言、截图/trace；Chrome/Browser MCP 用于探索和诊断。

若实现有意偏离原型，先把偏差、原因、影响和批准引用写入 prototype contract。没有批准的偏差不得把质量门禁设为通过。

## 独立 evaluator-optimizer 循环

实现者完成 Change Gate 后，由独立 Qoder Expert/Reviewer 或不继承实现结论的独立评估任务读取：原始 Spec/原型、OpenSpec Change、真实 diff、测试/浏览器原始证据、验证矩阵和风险要求。评估者输出仓库内报告，按 AC 列出通过、缺陷、证据不足和建议。

发现缺陷就回到对应 Feature/Repair loop；不得只修改评分。全部缺陷关闭并重验后，评估者再复核集成 commit。高风险或大范围 Change 不允许实现者自评替代独立评估。

## 90 分质量门禁

最终结论同时满足：

1. `quality-scorecard.json` 的十个硬门禁全部为 `true`；
2. 七个质量维度均达到自己的 minimum；
3. 总分至少 90；
4. 每个维度引用验证矩阵中的新鲜证据；
5. 独立评估报告路径和摘要有效；
6. 最后一轮 attempt 为 `passed`；
7. execution manifest 状态为 `complete`。

评分是验收完整度门槛，不允许用可维护性高分抵消功能缺失、安全失败或原型漂移。无 UI 的 Change 如需调整权重，必须保留总权重 100、满足最低比例并记录用户批准引用。

最终校验：

```text
python3 scripts/loopctl.py validate \
  --project-root . \
  --change-id <change-id> \
  --final
```

只有输出 `valid=true` 且 `acceptance_ready=true` 才能生成最终验收报告、Sync/Archive 或向用户声称 `acceptance-ready`。工具只验证链接、摘要、覆盖和声明一致性，不代替测试本身、产品判断或业务签收。

## Resume 与漂移

恢复时先运行非 final `validate`，再核对当前 Git、OpenSpec tasks、最新 attempt 和报告。以下变化使受影响证据失效并触发影响分析：Spec/原型摘要、授权 AC、Planning Seal、集成 commit、测试配置、环境、证据文件、Worktree 合并或 rebase。恢复点必须是第一个仍有效且未完成的 AC，而不是“上次聊到哪”。

## 边界

`loopctl` 不执行测试、浏览器、OpenSpec workflow、Worktree、部署、付费调用、凭据或生产操作；这些由 Qoder 和项目原生工具完成。它也不允许风险豁免、业务验收或生产发布。用户仍是范围、风险接受、最终业务验收与发布的授权者。
