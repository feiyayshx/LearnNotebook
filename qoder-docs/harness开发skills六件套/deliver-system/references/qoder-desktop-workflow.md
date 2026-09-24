# Qoder Desktop end-to-end workflow

## 何时读取

每次在 Qoder Desktop 中启动、恢复或交付一个系统/功能时读取。本文是完整交付的主运行手册；确定性 `deliveryctl` 只增强侦察和规划，不负责代替 Qoder 编码、测试、浏览器和 OpenSpec 工作流。

在 Qoder Desktop 打开包含 `.qoder/skills/deliver-system/SKILL.md` 的项目根目录，在输入框键入 `/` 并选择 `/deliver-system`，或直接用自然语言触发。用 `@spec.md`、`@prototype.html` 附加输入；`@` 只是提供上下文，不等于调用 Skill。若斜杠菜单尚未出现该 Skill，新建任务或重载项目窗口后再确认。

## 双轨与两条保障路径

启动时先按 SKILL.md 的双轨路由判定 EXPRESS / FULL：

### EXPRESS 轨（体验优先）

适用于 ≤R2、用户希望尽快看到可操作版本的 Web 应用/功能。按 [express-workflow.md](express-workflow.md) 执行 E1–E4：意图速记 → 骨架+mock → dev server + RunPreview + UI 闭环走查 → 用户体验确认。不建 OpenSpec Change、不启用 helper；用户决定继续投入时升级到 FULL 轨补齐。

### FULL 轨 — 快速路径（仅限有边界的小 Change）

适用于边界清楚、影响面小的 R0–R2 Change。直接采用用户现有 Spec、原型、项目文档和 OpenSpec Change，补齐范围、非目标、验收标准、风险与测试计划后开始实现。无需填写整套 JSON 规划图，也不强制 Stage B 确定性清单；用轻量验收清单 + UI 闭环走查作为门禁。

### FULL 轨 — 增强保障路径（R3/R4 opt-in）

适用于 R3/R4、迁移、权限、隐私、付费或生产数据 Change，以及用户明确要求高保障的大型 Greenfield/复杂 Brownfield/并行 Worktree 工作。先运行 M0–M3 helper，生成 Requirement/Acceptance/Slice/Contract 和 Planning Seal；helper 到 `PLANNING` 为止，之后仍由 Qoder 原生能力继续实施，并需 Stage B 执行授权与验证矩阵。

若 helper 不兼容当前仓库，记录原因并评估能否退回快速路径；路径越界、权威不明、高风险操作不可控，或退回会丢失需求/验收覆盖时必须停止，不得静默降级。

## 三种入口

### 从 Spec/原型新建系统

1. 读取原始 Spec 和原型，登记原路径，不改写原件。若命中 EXPRESS 轨条件，先向用户建议体验优先路线。
2. 输出功能地图、缺失状态、非功能要求、开放问题和建议默认值。
3. 只询问会改变架构、数据、安全、范围或验收的阻塞问题；其余作为显式假设。
4. 将系统拆成可独立验收、可按依赖排序的垂直 Change。
5. R3/R4 或用户要求高保障时选择增强路径，建立原型契约、规划图和 Planning Seal；否则维护轻量路线图与验收清单。
6. 选择第一个依赖已满足的 Change 进入 OpenSpec Propose。

### 在已有系统新增或修改功能

1. 先读适用 Rules、架构、活跃计划、ADR、测试/启动命令和当前 OpenSpec。
2. 运行现有核心 Smoke 或最小基线；旧失败单独记录，不伪装成本 Change 引入。
3. 把必须保持的现有行为写成 Preservation Requirements。
4. 完成影响面、兼容、迁移、回滚和回归范围评估，再创建一个有边界的 Change。

### 恢复中断工作

1. 读取 OpenSpec Change、未完成 tasks、Git 状态、最近 run 报告和计划日志。
2. 检查 Spec/代码/环境是否已漂移；漂移时先更新计划和影响分析。
3. 从第一个未满足且仍有效的验收条件继续，不根据聊天记忆猜测完成度。

## OpenSpec 在 Qoder 中怎么用

优先使用 Qoder 已安装的 OpenSpec workflow。界面名称与常见命令对应如下；若当前安装显示不同名称，以 Qoder 列出的 workflow 为准：

| 时机 | Qoder workflow | 作用 |
|---|---|---|
| 需求尚不清楚 | Explore ideas | 探索方案，不写实现承诺 |
| 新 Change 完整提案 | Propose change | 一次生成/补齐 proposal、spec、design、tasks |
| 新 Change 需逐项审阅 | New change → Continue change | 若已启用，逐个创建缺失 artifact |
| 信息完整、低风险的快速补齐 | Fast-forward | 若已启用，一次补齐规划 artifacts，不实施代码 |
| 已存在规划需修订 | update | 修改已有规划 artifacts；范围变化仍需重新确认 |
| 开始编码 | Apply tasks | 按 tasks 实施，并在完成后勾选真实完成项 |
| 当前规格需回写主 specs | Sync specs | OpenSpec 中可选；本 Skill 建议验证后由唯一集成者同步 |
| Change 已完成 | Archive change | 测试、文档、追踪和审批通过后归档；Archive 的提示/警告本身不是门禁 |
| 多个 Change 批处理 | Bulk archive | 仅在里程碑统一复核且相互独立时使用 |
| 结构/追踪复核 | Verify change | 若已安装则使用；未安装时由 Agent 按同等清单复核 |

不要为完整系统一次创建一个巨型 Change。一个 Change 应交付一个用户可观察结果，并能单独测试、回滚和验收。

## 完整执行顺序

1. **启动报告**：声明入口、权威输入、当前 Change、风险、缺失信息、选择快速或增强路径的理由。
2. **需求基线**：补齐范围、非目标、Acceptance、Preservation、接口、迁移、回滚和人工门禁；把重大选择写入 ADR/PDR。
3. **OpenSpec Propose**：创建一个依赖已满足的 Change，复核 proposal/spec/design/tasks；不得以 OpenSpec 结构有效代替业务批准。
4. **执行授权**：有原型时先建立 prototype contract；用户批准范围后用 `loopctl init-execution` 固定 Change、REQ/AC、源摘要、起始 commit、环境、预算和批准引用。没有执行清单不得 Apply。
5. **Qoder 路由**：小改用普通 Agent；复杂长任务用 Quest；有量化终点的修复使用带 Oracle、预算和停止条件的 Goal-style 契约（若当前 Qoder 有独立 Goal 入口则使用，否则放进 Agent/Quest）；独立评估用 Experts/Reviewer；确需隔离或无共享写入时才用 Worktree。
6. **实现小批次**：每批只完成一个可说明的目标。修改行为时同步测试；发现设计变化时先更新 OpenSpec/ADR/计划再继续。
7. **快速反馈**：每个有意义的实现批次运行 format/lint/typecheck 和目标单元/组件测试。不要每改一行都跑全量 E2E。每轮把结果追加到 attempts ledger。
8. **Feature 验证**：Change 完成后运行受影响回归、契约/集成测试和目标验收测试，把每个 AC 映射到 task、code、test、evidence、commit 和环境。含 UI 的 Change 必须完成 [ui-loop-verification.md](ui-loop-verification.md) 的闭环走查；Web 用户旅程使用 Playwright；Chrome/Browser MCP 适合探索和诊断，但一次人工浏览不能作为唯一验收证据。
9. **独立 evaluator-optimizer**：独立 Reviewer 读取原始 Spec/原型、真实 diff 和原始证据。发现缺陷就登记并重新打开对应 AC/task，回到最小修复和重验；不得仅修改评分或报告。
10. **集成与 90 分门禁**：在集成 commit 完成回归、原型状态/交互/视觉检查、风险门禁和质量 scorecard。硬门禁必须全过、各维度达标、总分至少 90，并由 `loopctl validate --final` 证明制品一致。
11. **文档收口**：更新 OpenSpec、项目计划、ADR/PDR、运行手册、变更日志和精确恢复点。证据绑定当前 commit 和环境。
12. **Sync/Archive**：只有 final validate 通过后，由唯一集成者按项目需要 Sync specs，再 Archive Change。OpenSpec 的 Sync 可选、Archive 可能询问同步；本 Skill 的验证门禁不依赖这些 UI 提示。归档后开始下一个依赖已满足的 Change。
13. **里程碑验收**：在集成 commit/干净环境运行全量回归、核心 E2E 及风险触发的安全/性能/迁移/回滚检查，生成 Acceptance Report，请用户确认业务验收。

## Qoder Rules

对于完整 Greenfield、多会话、Quest/Worktree 或长期 Change，建议把 `assets/templates/qoder/deliver-system-rule.md.tmpl` 复制为项目 `.qoder/rules/deliver-system.md`，但只在用户同意后创建或合并。Rules 只保存跨任务稳定原则，不复制整份 Skill，也不记录一次性计划或敏感信息。规则用于防遗忘，执行清单、OpenSpec、Git 和证据仍是事实源。

## Worktree

仅当 Change 能独立修改、拥有明确文件边界、独立端口/数据且有唯一集成者时使用。OpenSpec 主 specs、归档目录和全局交付状态由集成者串行维护。Worktree 内测试通过后，合并 commit 上的关键测试必须重跑。

## 有界 ReAct / 修复循环

默认同一失败最多自动修复 3 次；相同失败指纹连续 2 次无进展，或连续 2 轮没有新增通过项/减少失败/有效 diff 时停止。每轮用 `loopctl record-attempt` 持久化并采用它的 stop 结论。停止时生成进度报告，写明失败、已尝试方案、证据、推荐选项和精确恢复点；不要换一种措辞继续无限循环。

## 完成语义

- `implemented`：代码已写，不代表测试通过。
- `verified`：所需自动化与验收 Gate 在当前 commit/环境通过，不代表业务接受。
- `acceptance-ready`：规格、代码、测试、文档、风险与证据已对账，可交给用户验收。
- `accepted`：授权业务方已明确接受。
- `released`：已获生产授权并有部署后证据。

默认交付到 `acceptance-ready`。生产发布、真实数据操作、付费资源、凭据、不可逆迁移和高风险豁免必须逐项向用户请求明确授权。

## 给 Qoder 的启动提示

EXPRESS（体验优先）：

```text
/deliver-system 走 EXPRESS 轨：目标是 <一句话描述>。先写一页 express-intent（目标 + 3~5 条核心旅程 + 不做项 + mock 边界），然后脚手架搭骨架、mock 数据，尽快把 dev server 跑起来给我预览。预览前先按旅程清单做 UI 闭环走查（真实浏览器逐步断言，缺陷即修，最多 3 轮），证据放 delivery-docs/verification/loop-<时间戳>/。全部过程文件只允许放 delivery-docs/。我体验确认后再讨论是否升级 FULL 轨补真实数据层、测试和文档。
```

Greenfield：

```text
/deliver-system 以 @<SPEC路径> 和 @<原型路径> 为原始权威，按完整 Greenfield 的增强保障路径检查缺失信息、建立原型契约并拆成可独立验收的 OpenSpec Changes。先完成规划批准和 Stage B 执行授权，再从第一个依赖已满足的 Change 开始。逐 AC 维护 task→code→test→evidence→review→integration 追踪，每轮持久化结果；Change 完成后做 Playwright/风险匹配测试和独立评估，缺陷必须回到对应任务修复重验。硬门禁全过且质量至少 90 分才报告 acceptance-ready。持续维护计划/ADR/日志，达到阻塞或重试上限就报告我，不要生产发布。
```

Brownfield：

```text
/deliver-system 在现有系统实现：<需求>。先采用项目现有 Rules、架构和测试体系，记录基线与 Preservation Requirements，创建一个有边界的 OpenSpec Change，再实现、回归、适用的目标 E2E、文档和验收报告。保留我的未提交修改；高风险或破坏性动作先问我。
```

Resume：

```text
/deliver-system 恢复当前项目。读取现有 OpenSpec Change、Git 状态、计划日志和最近验证报告，检查漂移，从第一个仍有效的未完成验收项继续；不要重复已证实完成的工作，也不要根据聊天记录猜测状态。
```
