---
name: deliver-system
description: Deliver a system or bounded feature in Qoder Desktop through a dual-track workflow. EXPRESS track ships a user-operable experience build fast (scaffold + mock data + running preview + closed-loop UI walkthrough) before deeper engineering; FULL track adds OpenSpec planning, automated tests, evidence-bound acceptance, and documentation. All process artifacts live in delivery-docs/ at the project root, never inside engineering source directories. Every UI-facing delivery must pass a closed-loop browser walkthrough (walk → defect → fix → re-verify until all journeys pass). Use for Greenfield, Brownfield, or Resume work; supports upgrading EXPRESS to FULL, never downgrading to bypass safety gates. Never perform production release, destructive data work, paid actions, credential use, or risk acceptance without explicit user authority.
---

# Deliver System（双轨交付）

## Purpose

以两条可选轨道交付一个有界系统或功能：

- **EXPRESS（体验优先轨）**：最短路径产出"用户可实际操作的体验版本"（可运行网页 + UI 闭环走查证据），用户确认体验后再补齐工程纵深。
- **FULL（完整保障轨）**：从意图澄清、OpenSpec 规划、有界实现、自动化测试到证据化验收的完整流程。

自身只做编排：使用 Qoder 与项目原生工具执行工作，不替代运行时、测试框架、CI、浏览器驱动或部署系统。

## 双轨路由（每次启动先执行）

启动时先回答三个判定问题，选定轨道并向用户声明：

```text
Q1 风险：涉及支付/权限/认证/迁移/生产数据/多租户/不可逆操作？（R3/R4）
    → 是：强制 FULL，禁止 EXPRESS。
Q2 诉求：用户希望"尽快看到能跑起来、可操作的版本"？
    → 是：倾向 EXPRESS。
Q3 范围：单一应用/功能（Web、API、CLI 等）、影响面有限（≤R2）？
    → 是：允许 EXPRESS。
```

规则：

1. **EXPRESS → FULL 允许升级**（体验确认后补规划、真实数据层、测试、文档）；**FULL → EXPRESS 禁止降级**，不得借降级绕过任何安全或授权门。
2. 判定结果、理由与用户确认记录到 `delivery-docs/state/track.md`。
3. 用户未明确表达且 Q1 为否时，向用户提出轨道建议并等待选择；不得默认走重流程。
4. 无论哪条轨道，含 Web/小程序 UI 的交付都必须通过 **UI 闭环走查**（见下文）才能宣称完成。

## 过程制品目录：`delivery-docs/`（工程零侵入）

所有过程文件统一放在项目主目录下的 `delivery-docs/`，**严禁**散落到工程源码目录（`src/`、模块目录、`docs/` 等）：

```text
<project-root>/delivery-docs/
├── intent/           # 原始 Spec/原型/需求速记（EXPRESS: express-intent.md）/用户反馈
├── product/          # requirements.json、acceptance.json、feature-ledger.json、prototype-contract.json、source/
├── plans/            # delivery-roadmap.json、里程碑计划
├── decisions/
│   ├── adr/          # 架构决策记录
│   └── product/      # 产品决策记录（PDR）
├── architecture/     # 架构说明（FULL 轨）
├── governance/       # 稳定项目规则
├── state/            # 机器状态：manifest/state/events/questions/assumptions/work-items/runs/execution/track.md
├── verification/     # runs/<run-id>/、loop-<时间戳>/（UI 闭环走查证据）
└── reports/          # 阶段报告、验收报告
```

例外与映射：

- `openspec/` 是 OpenSpec CLI 的工具约定目录，保留在项目根，不迁入 delivery-docs。
- Brownfield 项目已有权威文档体系时，遵循 adopt, don't replace：在 `delivery-docs/state/manifest.json` 中做路径映射，不搬迁已有文件；同一事实仍只允许一个权威位置。
- 详细归属见 [artifact-ownership.md](references/artifact-ownership.md)。

## EXPRESS 轨（体验优先）

读 [express-workflow.md](references/express-workflow.md) 后执行。阶段概览：

```text
E1 意图速记（分钟级）
   delivery-docs/intent/express-intent.md：目标 + 3~5 条核心用户旅程 +
   明确不做项 + mock 边界。不产出任何其他规划制品。
E2 骨架搭建
   脚手架初始化 → 核心页面/路由 → mock/内存数据。唯一目标：跑起来。
E3 可操作体验版 + UI 闭环走查
   启动 dev server，用 RunPreview 交给用户；同时按旅程清单执行
   UI 闭环走查 loop（见 ui-loop-verification.md），缺陷即修，
   直到全部旅程通过或达轮次上限。
E4 用户体验确认门（EXPRESS 轨唯一强制人工门）
   用户实际操作后反馈收敛，记录到 delivery-docs/intent/feedback.md。
E5 升级补齐（可选 → 切换 FULL 轨对应阶段）
   替换 mock → 真实数据层/接口 → 自动化测试 → 文档 → 验收报告。
```

EXPRESS 轨不使用确定性 helper、不建 OpenSpec Change、不做验证矩阵；这些在升级到 FULL 时补齐。E1–E3 期间新需求直接追加到 express-intent.md 并在下一轮走查覆盖。

**非 Web 交付的体验版等价物**（E3 的"可操作体验 + 走查"按形态替换，完成门槛不变）：API = 可真实调用的接口集合 + 示例请求脚本（curl/httpie），逐旅程真实调用并断言响应；CLI = 可执行命令 + 典型用法演示脚本，断言退出码与输出；走查证据同样落 `delivery-docs/verification/loop-<时间戳>/`，E4 用户确认门照常执行。

## FULL 轨（完整保障）

### 不变式

1. 仓库文件（delivery-docs/ 与 openspec/）是持久记忆；对话与 Qoder Memory 只是辅助上下文。
2. OpenSpec 是变更规格权威；Qoder Specs 和 To-dos 是派生视图。
3. 保留原始 Spec/原型及其摘要，不静默覆盖或收窄已批准意图。
4. 每个范围内 Requirement 映射到验收标准、OpenSpec Change、任务、测试与证据。
5. 一个可独立验收的垂直切片 = 一个 OpenSpec Change；不建巨型 Change，也不按源文件建 Change。
6. 每个改行为的 Change 必须有代码级自动化测试。
7. 在最外层稳定边界验证可观察行为：Web 用浏览器（UI 闭环走查 + Playwright）、API 用真实 HTTP、Worker 用队列/数据库黑盒、CLI 用独立进程。
8. 实现期跑目标快测，Change 完成跑目标验收，集成/里程碑/发布跑回归。
9. 已验证状态必须绑定新鲜证据、源摘要与 Git commit；不接受 Agent 断言作为证明。
10. 重大变化后更新计划、规格、ADR/PDR、文档与项目地图（docs/maps/）、状态与日志。
11. 缺授权、产品冲突未决、破坏性迁移、生产部署、付费服务、凭据、风险接受、最终业务验收——一律停下问用户。
12. 有界修复循环达到尝试/时间/重复失败/无进展上限即停止。

### 分级保障（按风险收缩仪式感）

| 风险 | 规划与状态 | 验证与证据 |
|---|---|---|
| R0–R2 | 轻量：express-intent 或一页需求 + OpenSpec Change + 验收清单（Markdown/JSON 手工维护） | 目标测试 + UI 闭环走查 + 走查证据绑定 commit |
| R3–R4 | **opt-in 确定性 helper**：`scripts/deliveryctl.py` M0–M3 规划 + Planning Seal + `loopctl` Stage B 执行清单 | 完整验证矩阵 + 独立评估 + 质量 scorecard ≥90 + `loopctl validate --final` |

确定性 helper 仅在 R3/R4 或用户明确要求高保障时启用；其编译相位上限是 M3（`PLANNING / plan_ready`），不得解锁或伪造 M4+ 状态。helper 的机器状态目录为 `delivery-docs/state/`。**helper 脚本需 Python ≥ 3.10**（POSIX 用 `python3`，版本不足时改用更高版本解释器命令，如 `python3.12`；Windows 用 `python`）。使用 helper 时读 [reconnaissance.md](references/reconnaissance.md)、[planning-and-traceability.md](references/planning-and-traceability.md)、[loop-engineering-closure.md](references/loop-engineering-closure.md)。

### 路由三选一

- **Greenfield**：无既有实现，用户提供 Spec/原型。登记源件 → 依赖感知垂直切片 → 逐 Change 实现验收。见 [greenfield-workflow.md](references/greenfield-workflow.md)。
- **Brownfield**：已有系统上做功能/修复/迁移/重构。采纳既有架构与工具链 → 基线健康 → Preservation Requirements → 有界 Delta Change。见 [brownfield-workflow.md](references/brownfield-workflow.md)。
- **Resume**：`delivery-docs/state/` 存在未完成工作。重算源/计划/环境事实，一致则从首个未完成有效项继续，变化则先失效旧证据。见 [resume-workflow.md](references/resume-workflow.md)。

### 每个 Change 的有界执行循环

1. 校验基线健康与 Delivery Contract（Change ID、范围与非目标、验收标准、风险级、测试与证据要求、修复预算、停止条件）。
2. 实现最小连贯批次 → 跑目标快测。
3. 垂直切片完成 → Change 门禁（受影响回归 + 目标验收 + UI 闭环走查）。
4. 证据绑定 source/commit/环境写入 `delivery-docs/verification/`。
5. R3/R4：`loopctl record-attempt` 记录轮次并服从其停止判定；独立 QA/评审。
6. 缺陷 → 关联 Acceptance/task → 重开 → 有界循环修复。
7. OpenSpec 顺序：`Propose change` → `Apply tasks` → 发现改变方案时 `update` → `Verify change`（或等效人工核对） → `Sync specs` → `Archive change`。代码和测试真实存在前不得勾任务框。
8. **地图保活门**：Change 涉及模块、表、对外接口、环境组件的新增/更名/删除时，`Archive change` 前更新对应 `docs/maps/` 地图（项目无地图体系则跳过并在报告登记），未更新不得归档。

默认修复上限（项目更严则从严）：

```text
max_repair_attempts = 3
max_same_failure = 2
max_no_progress_rounds = 2
```

## UI 闭环走查（两轨共用的强制 Gate）

读 [ui-loop-verification.md](references/ui-loop-verification.md) 后执行。核心逻辑：

```text
1. 从 intent/acceptance 生成走查清单：每条旅程 = 操作步骤 + 可断言预期
   （DOM 状态、接口返回、console 无报错、必要时数据落库值）。
2. chrome-devtools / playwright / browser-use 真实驱动浏览器逐步执行；
   小程序用微信开发者工具自动化。
3. 每步留证据（截图 + console/network 摘要）→
   delivery-docs/verification/loop-<时间戳>/。
4. 缺陷 → 登记 defects.md（指纹+复现步骤）→ 修复 →
   只重验受影响旅程 + 核心冒烟。
5. 结束条件：全部旅程通过；或达轮次上限（默认 3 轮）→ 停止并出证据化报告。
6. 走查结果绑定当前 commit；之后代码变更即证据过期，需重验。
```

含 UI 的交付，未完成闭环走查（全部通过或用户明示接受余留缺陷）不得宣称完成、`acceptance-ready` 或结束会话。禁止只截一张首页图就宣称"页面正常"。

## 用户交互处理

把每条实质用户消息归类为：新需求 / 澄清 / 产品决定 / 架构决定 / 验收变化 / 缺陷报告 / 延后项 / 批准或否决 / 临时假设，先更新 delivery-docs 权威制品再继续实现。基线批准后的新范围走变更控制与影响分析，并使受影响计划/测试/证据失效。

非阻塞问题批量提问：每个问题说明为何重要、影响哪些 Requirement/Change、推荐选项与安全默认值。仅阻塞性不确定或缺授权时立即停止。

## Qoder 执行能力选择

读 [qoder-harness-routing.md](references/qoder-harness-routing.md)。默认：Quest 用于复杂切片；Experts 用于高风险/跨领域独立评审；Local 用于小型低风险编辑；Worktree 用于多模块/并行 Change（单一集成 Owner，合并后重跑受影响门禁）；Browser/Chrome 用于探索与诊断；Playwright 用于可重复 Web 验收证据。能力不可用时产出完整交接件并告知用户启动哪个动作，不得谎称已运行。

## 报告与安全停止

暂停、阻塞、Change 完成、里程碑完成时报告：当前轨道/阶段/Change/commit、目标与范围、门禁通过情况、证据位置与新鲜度、尝试次数与失败指纹、未决问题/假设/风险、建议下一动作、精确恢复点。报告写入 `delivery-docs/reports/`。

状态语义：`WAITING_USER`（等用户决定）、`BLOCKED`（外部条件未解）、`REWORK`（验证失败）、`PAUSED`（主动暂停）。授权用户接受前不得声明 `accepted`；无部署及部署后证据不得声明 `released`。

细节见 [loop-and-reporting.md](references/loop-and-reporting.md) 与 [safety-and-release.md](references/safety-and-release.md)。

## 完成标准

**EXPRESS 轨完成**（体验版就绪）：

- 核心旅程全部可操作、dev server 可复现启动；
- UI 闭环走查全部通过且证据在 `delivery-docs/verification/loop-<ts>/`；
- 用户实际操作并确认（E4 门）；
- 余留缺陷与不做项明示记录。

**FULL 轨完成**（`acceptance-ready`）：

- 范围内 Requirement 与验收标准全部可追踪；
- 代码/集成/验收/风险触发门禁全部通过，UI 闭环走查全部通过；
- 证据新鲜并绑定当前源与 commit；无未授权跳过/豁免/缩范围/关键 flaky；
- OpenSpec 与 delivery-docs 文档匹配已交付行为；
- 独立 QA/评审满足（R3/R4 另需验证矩阵完整、独立评估通过、质量分 ≥90、`loopctl validate --final` 通过）；
- 余留问题与已接受风险明示，验收报告已生成。

最终业务验收与生产发布始终是独立的显式授权动作。

## 流水线协作（可选上下游，存在时优先接驳）

```
env-bootstrap → spec-writing → spec-review → deliver-system → spec-loop-verify
└────────────────── ai-trace(横切度量,任务结束时聚合指标)──────────────────┘
```

- **第 0 环**：新项目/新接入建议先跑 env-bootstrap 完成协作资产基线（AGENTS.md、OpenSpec init、文档与 ADR 体系、`docs/maps/` 五张地图）；其产物是本 skill 的 Brownfield 采纳对象，openspec/ 已就绪可直接复用，五张地图是 Repo Wiki 探索的高优先级输入。**ADR 归一**：env-bootstrap 项目中，本 skill 的 ADR 经 `delivery-docs/state/manifest.json` 路径映射统一落 `docs/decisions/adr/`，不自建第二套。
- **上游**：存在 spec-writing 定稿/spec-review 评审通过的 spec 时，登记为 `delivery-docs/intent/` 权威源件（引用路径，不复制多份）；其 Open Questions 未决项转入 questions/assumptions，阻塞性的先问用户。
- **diff 门禁联动**：项目存在 `scripts/diff-gate.mjs`（env-bootstrap D1-6）时，每个 Change 施工前把 proposal Impact/tasks 的文件清单写入 `.qoder/diff-scope.txt`，Change 归档后清空——越界改动从评审问题变成机制拦截；触及模块/表/接口/环境组件变化的 Change，应把对应 `docs/maps/*.md` 一并写入白名单（配合地图保活门）。
- **ai-trace 联动**：项目启用 AI 提交归因时，本 skill 产生的提交带 `--author="qoder <qoder@ai.local>"`（小步频提：每完成一个用户认可的任务批次即提交）；交付结束建议执行 `/ai-trace close` 聚合指标（delivery-docs/ 是其首要挖掘源）。
- **下游**：FULL 轨的独立验收可交 spec-loop-verify 执行（以本次 spec/acceptance 为交付标准；其 RUN_DIR 证据与 `delivery-docs/verification/` 互补，验收判定以其**当轮新取证据**为准）；验证中发现规格缺陷回流 spec-review/spec-writing。

## 按需加载 references

只读当前阶段需要的文件，不默认全读：

- EXPRESS 轨全流程：[express-workflow.md](references/express-workflow.md)
- UI 闭环走查引擎：[ui-loop-verification.md](references/ui-loop-verification.md)
- 事实归属与 delivery-docs 布局：[artifact-ownership.md](references/artifact-ownership.md)
- 测试分层与验收门禁：[testing-and-acceptance.md](references/testing-and-acceptance.md)
- Qoder Desktop 工作流与启动提示词：[qoder-desktop-workflow.md](references/qoder-desktop-workflow.md)
- Greenfield / Brownfield / Resume：对应 workflow 文件
- OpenSpec 生命周期：[openspec-adapter.md](references/openspec-adapter.md)
- 执行能力路由：[qoder-harness-routing.md](references/qoder-harness-routing.md)
- 有界循环与报告：[loop-and-reporting.md](references/loop-and-reporting.md)
- 授权/安全/发布门：[safety-and-release.md](references/safety-and-release.md)
- R3/R4 helper：[reconnaissance.md](references/reconnaissance.md)、[planning-and-traceability.md](references/planning-and-traceability.md)、[loop-engineering-closure.md](references/loop-engineering-closure.md)、[git-worktree-scope.md](references/git-worktree-scope.md)
