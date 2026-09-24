# OpenSpec adapter

## 何时读取

检查/规划 OpenSpec 配置、拆分切片、创建下一个 Change、生成 runtime artifact 指令、验证规划、处理 OpenSpec 与其他制品冲突，或以后准备 Apply/Verify/Archive/Sync 时读取。

## 权威边界

- `openspec/specs/`：当前已交付并同步的可观察行为。
- `openspec/changes/<change>/`：一个已批准 in-flight 行为 Change 的 proposal/specs/design/tasks。
- JSON 规划图：跨 Change 的 Requirement/Acceptance/Feature/roadmap/批准和证据关系。
- Qoder Quest Spec/To-do：携带 IDs 和 digest 的派生执行视图，不是第二规格权威。

OpenSpec 的 `status: done` 只表示 artifact 文件存在；strict validate 只证明结构/格式。`/opsx:verify` 是 Agent 检查且不阻断 Archive。以上都不能代替本 Skill 的批准、确定性测试和证据 Gate。

## M3 能力边界

允许的机器链路：

```text
version/schemas --json
→ new change <next-ready-id> --json
→ status --change <id> --json
→ instructions <artifact> --change <id> --json
→ Agent 按 runtime instructions 写草案
→ validate <id> --strict --json --no-interactive
→ Planning Seal
→ stop before Apply
```

M3 不调用 Apply、Verify、Sync、Archive，也不使用 `--yes`、`--skip-specs`、`--no-validate`。所有 `tasks.md` checkbox 保持未勾选。

## Qoder-native implementation lifecycle

上述限制只约束确定性 M3 adapter。Planning Seal 或快速路径的 Change 评审完成后，必须先由用户批准当前执行范围并用 `loopctl init-execution` 建立 Stage B 授权，再由 Qoder 已安装的 OpenSpec workflow 执行后续生命周期；不要扩展 Python adapter 去运行项目代码或浏览器：

1. `Apply tasks`：Qoder 按已批准 tasks 实现；任务只有在对应实现和要求的目标测试存在时才勾选。
2. `update` / `Continue change`：发现改变方案、范围、Acceptance、迁移或风险时，先更新 Change/ADR/计划。若改变已批准范围，返回用户确认；增强路径同时 invalidate/reseal。
3. `Verify change`：若 workflow 已启用，使用独立 Qoder reviewer 复核规格、任务、实现和测试。OpenSpec strict validate 仅证明结构，不能替代项目测试或验收证据。
4. `Sync specs`：Change 验证通过且 `loopctl validate --final` 返回 acceptance-ready 后同步主 specs；不得在实现或回归失败时提前同步。
5. `Archive change`：完整验证矩阵、独立评估、质量硬门禁、项目测试、目标验收、文档和所需审批通过后归档。若 `Verify change` 未启用，至少要求独立 reviewer 按同一清单检查并在报告中记录该替代路径。

实现产生代码、Git HEAD 和 OpenSpec task 变化是预期行为，不应反复要求 M3 Seal 对实现树保持相同。只有原始意图、范围、Acceptance、风险、设计或路线图变化时，才返回规划阶段失效并重新批准。

## JIT Change 粒度

一个 Change 对应一个可独立验收的垂直切片，不是完整系统、单技术层、文件或微任务。完整 roadmap 为所有切片保留稳定 kebab-case Change ID 和 Draft Delivery Contract，但一次只创建下一个：

- 依赖已经满足；
- 范围、非目标、Acceptance/Oracle 和风险已审查；
- 用户明确批准；
- 与现有 active Change 不重名/不冲突；
- OpenSpec root/config 和 Git scope 已验证。

前一 Change 交付并 Sync 后再 materialize 下游 Change，避免 Delta 基于陈旧 current specs。

## Runtime artifact 约定

默认 `spec-driven` 通常包含 proposal、specs、design、tasks，但依赖关系和 required 状态必须以运行时 `status/instructions --json` 为准，不复制内置模板假设。

Delta spec 使用官方结构：

```text
## ADDED|MODIFIED|REMOVED|RENAMED Requirements
### Requirement: REQ-... — name
#### Scenario: AC-... — name
```

Requirement 使用 SHALL/MUST 且至少一个 Scenario；Scenario 明确 WHEN/THEN。MODIFIED 必须包含当前 Requirement 完整块；REMOVED 需要 Reason/Migration；RENAMED 使用 FROM/TO。实现类名/框架/步骤属于 design/tasks，不属于行为 spec。

## Config/root 策略

稳定字段是 `schema`、`context`、`rules`；OpenSpec v1.6 还识别 beta `references`、`store`。配置不会随使用自动完善，`context/rules` 是提示注入而非确定性门禁。

1. 同时存在 `config.yaml` 与 `config.yml` 立即阻塞。
2. 缺失配置：生成带 digest 的审查候选；明确批准后 exclusive-create。
3. 空文件也属于用户：先 proposal，不静默覆盖。
4. 非空现有配置默认逐字节保留；不通过通用 YAML parse/serialize 重写。更新必须使用 preimage digest + 审查后的 exact candidate + CAS atomic write。
5. 保留注释、顺序、未知字段、`references` 和 `store`；M3 不新增或修改 `store`。
6. 未批准 `store` 或外部 `references` 扩展 authority root 时，在 CLI 前阻塞。
7. duplicate key、anchor/alias/merge、自定义 tag、超限 context、无效 schema/rules 或 unsafe OpenSpec tree 失败关闭。
8. config 只放简洁稳定的技术栈/目录、兼容/架构原则、测试命令和 artifact 生成约束；不放完整架构、当前状态、一次性讨论、Change 细节或结果。

## CLI 安全

- 只从项目外绝对 PATH 或明确外部路径固定 executable；不运行项目内同名程序。
- 使用最小环境、无 shell、有 timeout 和 stdout/stderr 上限，所有支持操作按 JSON 严格解析。
- CLI 前审计 `openspec/`：不跟随 link，不打开 special node，不越出已授权 root。
- `new change` 需要 kebab ID、无同名、审批引用和 config preimage digest；不得手工创建 `.openspec.yaml`。
- 命令失败、warning/shape/version 不兼容、输出非 JSON/超限或运行中配置变化都阻塞，不猜测成功。

## 用户必须确认

- Change 切分、范围/非目标、capability 和 Acceptance 场景；
- Breaking、REMOVED、RENAMED、API/数据模型/迁移、认证/安全/隐私；
- 外部依赖、费用、架构后果和 config context/rules；
- 从规划进入实现，以及以后 Sync/Archive。

## 停止条件

Change 过大/不可独立验收；依赖未满足；配置/root/CLI 不可信；当前 specs 与受影响运行行为关键冲突；IDs/Oracle/Delivery Contract 不完整；active Changes 重叠；Quest 派生视图冲突；用户批准缺失。M3 进入 `WAITING_USER`/`BLOCKED` 或停在 `PLANNING`，绝不 Apply。
