---
name: env-bootstrap
description: 在任何项目开始 AI 协作开发前,对照最佳实践基线准备/体检开发环境:Harness 资产(AGENTS.md/rules)、OpenSpec 变更管理、文档体系、ADR 系统、流水线 skills、IDE/MCP 工具链;multi-repo 工作区额外生成 service-map.yaml 服务拓扑。当用户要求初始化项目环境、接入 Qoder 协作、环境体检、新成员入职检查,或在 spec-writing 之前准备环境时使用。不写业务代码、不做需求澄清、不做运行时预检。
---

# env-bootstrap(环境准备与体检)

env-bootstrap 是协作开发流水线的"第 0 环":在任何项目开始 AI 协作开发前,对照一份最佳实践基线清单([references/baseline.md](references/baseline.md)),对开发环境执行**基线清单驱动、幂等的"体检-收敛"循环**——先只读扫描出差距,再交互挑选补齐项,最后生成产物并重扫验证。首次初始化、二次补齐、入职体检、季度巡检都是同一个循环;验证清单即基线本身,不维护两套。同时支持 single-repo(工作区即仓库)与 multi-repo(工作区承载多个仓库,业务仓统一放 `repos/<repo-name>/`,额外维护 `service-map.yaml` 服务拓扑)两种工作区形态。

流水线位置(第 0 环):

```
env-bootstrap → spec-writing → spec-review → deliver-system → spec-loop-verify
└────────────────── ai-trace(横切度量,任务结束时聚合指标)──────────────────┘
```

## When to use

以下典型场景使用本 skill:

1. **新项目第一天初始化**:空目录/新仓库,从零建立全部协作环境资产;
2. **Brownfield 项目中途接入 Qoder 协作**:已有部分资产的项目,增量补齐缺口,绝不覆盖已满足基线的内容;
3. **新成员 clone 仓库后入职体检**:只读扫描 + 体检报告,确认本地环境与仓库资产是否就绪;
4. **季度环境巡检**:周期性重跑体检,体检报告保留上次状态列,可对比环境演进;
5. **多仓微服务工作区建立全局协作上下文**:逐仓分析并生成/校验 `service-map.yaml` 服务拓扑与工作区级 Harness 资产。

## 核心模型

### 三态判定

对基线清单中的每个检查项,扫描后落入三态之一:

| 状态 | 含义 | 默认动作 |
|------|------|----------|
| `[缺失]` | 项目无对应文件/能力 | 默认勾选补齐 |
| `[已有·建议补齐]` | 有文件但缺基线期望的章节 | 默认勾选,增量补齐(不动已有内容) |
| `[已覆盖]` | 已满足基线 | ✅ 跳过,绝不覆盖 |

### 确定程度排序

判定时以证据的确定程度排序取信:

```
CI/构建文件 > 代码事实 > 文档 > 口头描述 > 模板默认写法
```

### 域清单一览(D1-D7)

| 域 | 覆盖内容 | 详见 |
|----|----------|------|
| D1 Harness 资产 | AGENTS.md 入口、`.qoder/rules/` P0 铁律与提交前自检、`.gitignore` 协作条目、Git 协同约定、diff 门禁钩子、远程排障路径规则 | [references/baseline.md § D1](references/baseline.md#d1-harness-资产) |
| D2 变更管理 | openspec CLI 安装、`openspec/` init、`config.yaml` 项目上下文与 artifact 规则 | [references/baseline.md § D2](references/baseline.md#d2-变更管理) |
| D3 文档体系 | `docs/README.md` 索引与**流水线产物路由表**、`docs/specs/` 落盘约定、`docs/open-questions.md` 机制、全链路产物目录骨架 | [references/baseline.md § D3](references/baseline.md#d3-文档体系) |
| D4 ADR 系统 | `docs/decisions/adr/` 目录(全链路唯一 ADR 落点)、MADR 精简模板、`0000-index.md`、首条 ADR | [references/baseline.md § D4](references/baseline.md#d4-adr-系统) |
| D5 Skills 就位 | 流水线五件套(四环+ai-trace)、市场三件套(superpowers / chrome-devtools / playwright)、`skills-backup/` 备份约定、经验回流约定 | [references/baseline.md § D5](references/baseline.md#d5-skills-就位) |
| D6 IDE/工具链 | MCP 可用性(实际调用探测)、Node/npm/git 版本、模型与上下文窗口建议、Repo Wiki | [references/baseline.md § D6](references/baseline.md#d6-ide工具链) |
| D7 多仓拓扑(仅 multi-repo) | `service-map.yaml` 存在且结构合规、`repos/<repo-name>/` 路径约定、各子仓 git 状态、depends_on 引用合法 | [references/baseline.md § D7](references/baseline.md#d7-多仓拓扑仅-multi-repo) |
| D8 项目地图(已有代码项目必检) | 五张地图:代码/业务域/接口/数据/环境(`docs/maps/`),Repo Wiki 可判等价覆盖 | [references/baseline.md § D8](references/baseline.md#d8-项目地图已有代码的项目必检空项目建骨架即可) |

D7 仅在 Phase 0 判定为 multi-repo 时启用;single-repo 工作区跳过该域,差距报告不出现 D7 行。D8 在空项目(无存量代码)时仅建目录骨架,地图内容随首个模块交付后补齐。

## Workflow

体检-收敛循环,有界。进入 multi-repo 分支时,先读 [references/multi-repo.md](references/multi-repo.md) 获取逐仓分析与 service-map 生成/校验规则。

### Phase 0 探测项目形态

- git 仓库? 空/已有? 单仓/多仓? 技术栈? → 输出形态结论(一次交互确认)
- 多仓信号:已有 service-map.yaml,或 repos/ 下有多个 git 仓;不确定时交互确认
- **工具**:Bash(git/文件探测)、Read(读取现有配置)、AskUserQuestion(形态确认)

### Phase 1 六域扫描(只读;multi-repo 时含 D7,共七域)

- 按 baseline.md 逐项探测,三态判定 → 差距报告表(域 × 检查项 × 状态 × 建议动作)
- [multi-repo] 逐仓分析:构建描述、技术栈、对外端口/依赖线索,汇总为 service-map 草稿(规则见 [references/multi-repo.md](references/multi-repo.md))
- **工具**:Bash(命令/文件探测)、Read(对比文件章节结构);本阶段只读,不写任何文件

### Phase 2 交互挑选

- AskUserQuestion 按域分组(multiSelect),`[缺失]` 默认勾选,`[已覆盖]` 不出现;
- 交互式收集必要输入:P0 铁律(2-3 条,提供分项目类型示例库兜底)、项目名与项目简介、决策者(用于 ADR-0001 署名,默认取当前用户/团队名,可一并确认)、分支协作模型(主干直提交/功能分支+PR,默认功能分支+PR)
- [multi-repo] service-map.yaml 不可跳过(可审阅草稿,不能不生成);依赖图有不确定推断时阻塞确认
- **早停选项**:用户可在此选择"只体检不生成",Phase 1 差距报告即为最终输出,流程到此结束
- **工具**:AskUserQuestion(按域分组挑选与必要输入收集)

### Phase 3 生成与引导

- 仓内文件:按 templates/ 生成或增量补齐;D4 域生成时,ADR 索引(0000-index.md)与首条 ADR-0001 同批落盘、绑定生成,保持索引条目与实际 ADR 文件一一对应(对应 baseline.md D4-3);
- IDE 级项:写入 docs/env/todo-manual.md(人工待办清单,含操作步骤,素材取自 [references/ide-manual-steps.md](references/ide-manual-steps.md));
- CLI 类:征得同意后执行(npm i -g @fission-ai/openspec + openspec init)
- [multi-repo] Harness 资产落工作区根;不修改任何子仓文件;openspec init 落点由用户选择(工作区级或指定主仓)
- **统一清理规则**:按模板生成定稿文件时,删除模板头部的占位符说明注释行
- **工具**:Bash(目录创建、CLI 执行)、Read(读模板与已有文件后增量合并)

### Phase 4 验证(= 重跑 Phase 1 扫描)

- 逐项 ✅/⚠️/❌ → docs/env/env-report.md(体检报告,保留上次状态列可对比);
- [multi-repo] 追加校验:service-map 的 depends_on 不引用不存在的服务、无凭据明文
- **有界**:生成→验证最多 2 轮,未收敛项如实列入报告交用户
- **工具**:Bash(重扫探测)、Read(核对生成产物)

## 判定细则

已有文件覆盖程度的判断方法:

1. 取基线清单中该检查项**期望的章节标题结构**(baseline.md 中逐项列明);
2. Read 项目已有文件,提取其实际标题结构;
3. 期望章节全部存在 → `[已覆盖]`;文件存在但缺少一个或多个期望章节 → `[已有·建议补齐]`;文件不存在 → `[缺失]`;
4. 判定证据冲突时按确定程度排序取信:`CI/构建文件 > 代码事实 > 文档 > 口头描述 > 模板默认写法`;
5. **无法判断覆盖程度时**(如文件结构非标准、内容语义不明),一律标 `[已有·建议补齐]`,在 Phase 2 交用户决定,绝不擅自覆盖。

## 错误处理与降级

| 情况 | 行为 |
|------|------|
| npm/网络不可用,openspec CLI 装不上 | D2 标 ❌ + 写入 todo-manual.md 手工安装步骤,不阻塞其他域 |
| MCP 调用失败 | D6 对应项标 ❌ + 引导步骤,不重试超过 1 次 |
| 无法判断已有文件覆盖程度 | 标 `[已有·建议补齐]` 交用户决定 |
| 非 git 仓库 | 提示建议 `git init`(征得同意后执行),拒绝则继续但 D1 的 .gitignore 项标 ⚠️ |
| [multi-repo] 子仓缺失/clone 失败/dirty | 登记到 docs/open-questions.md,service-map 中该服务标注"待确认",不阻塞其他仓分析 |
| [multi-repo] 依赖关系只能从命名推测 | 写入 service-map 草稿但标注低置信度,Phase 2 阻塞确认后才定稿 |
| 用户中途要求只体检不生成 | Phase 1 结束即停,输出差距报告(早停选项) |

## Boundaries (I do NOT)

- 不写业务代码;
- 未经确认不动 CI/依赖/已有规范;
- 不做需求澄清(spec-writing 的职责);
- 不做运行时预检——数据库/服务/网关连通性属"验证时环境",归 spec-loop-verify 的 env-preflight(边界互相引用,写明分工);
- 未经用户确认不覆盖任何已有文件;`[已覆盖]` 项直接跳过;
- 不读 .env / 密钥值,只记录路径;不把凭据写入任何产物;
- 未确认的内容不写成"必须/禁止",只能写"建议"或进 open-questions.md;
- CLI 全局安装、git init 等必须先征得用户同意;
- 不动业务代码、CI、依赖版本、lockfile。

### multi-repo 禁忌

- 不修改任何子仓文件;
- 不默认执行 git pull(已有仓只 fetch,merge 需用户确认);
- 不把带 token 的 URL、密码、AK/SK 写入 service-map 或任何产物;
- 不把某个仓的局部做法推广为全局规则。

## Output

### 在目标项目中生成的产物布局

```
<project-root>/
├── AGENTS.md                     # Agent 入口:项目简介/铁律/目录索引/流水线用法
├── .qoder/rules/
│   ├── always-on-rules.md        # P0 铁律(交互收集;未确认的只写"建议")
│   ├── pre-commit-checklist.md   # 提交前自检(白皮书附录 B 裁剪版)
│   └── git-collaboration.md      # Git 协同约定(分支策略/提交规范/评审/AI 提交归因)
├── scripts/diff-gate.mjs         # diff 门禁脚本(D1-6,对照 .qoder/diff-scope.txt 拦截越界提交)
├── openspec/                     # openspec init 产出 + config.yaml 注入项目上下文
├── docs/
│   ├── README.md                 # 文档体系索引 + 流水线产物路由表(全链路唯一登记处)
│   ├── specs/                    # spec-writing 落盘约定目录(.gitkeep + 说明)
│   ├── maps/                     # D8 五张项目地图(代码/业务域/接口/数据/环境)
│   ├── decisions/adr/
│   │   ├── template.md           # MADR 精简模板
│   │   ├── 0000-index.md         # ADR 索引
│   │   └── 0001-adopt-qoder-harness-env.md  # 首条 ADR:本次环境决策记录
│   ├── env/
│   │   ├── env-report.md         # 体检报告(每次运行更新,保留上次状态)
│   │   └── todo-manual.md        # IDE 级人工待办清单
│   └── open-questions.md         # 低置信度推断/待确认项集中地
└── .gitignore                    # 追加 .qoder/settings.local.json 等条目
```

下游技能产物目录(本 skill 建骨架并在路由表登记,内容由对应技能运行时产生):`delivery-docs/`(deliver-system)、`.qoder/ai-trace/`(ai-trace)、`.qoder/verify-runs/`(spec-loop-verify)。

**multi-repo 工作区**在上述布局(落工作区根)基础上追加:

```
<workspace-root>/
├── service-map.yaml              # 服务拓扑:services/depends_on/build_order,无凭据明文
└── repos/<repo-name>/            # 各业务仓(skill 只读分析,不修改子仓文件)
```

### 结果输出格式

每次运行结束按以下模板输出:

```markdown
## 环境准备结果
工作区形态:single-repo / multi-repo;项目状态:空/已有
差距报告:D1-D7 各域 [缺失]x条 [已有·建议补齐]x条 [已覆盖]x条
本次生成:...;增量补齐:...;跳过([已覆盖]):...
人工待办:见 docs/env/todo-manual.md(n 项)
体检结论:✅ n / ⚠️ n / ❌ n,详见 docs/env/env-report.md
```

## Examples

### 示例 1:空的 SaaS 新项目完整初始化

> **用户**:这是个全新的 SaaS 项目仓库,帮我把 Qoder 协作环境初始化好,之后要走 spec-writing。
>
> **Agent(Phase 0)**:探测到 git 仓库已 init 但无业务文件,无 repos/ 与 service-map.yaml → 判定 single-repo、空项目,与用户确认通过。
>
> **Agent(Phase 1)**:六域扫描,差距报告:D1-D4 全部 `[缺失]`;D5 流水线四件套未检出、市场三件套已装 2/3;D6 MCP 探测通过、Repo Wiki 未生成。
>
> **Agent(Phase 2)**:按域分组提问,`[缺失]` 项默认勾选,用户全部确认;收集项目简介一句话;分支协作模型采用默认的功能分支+PR;P0 铁律用户说不出,从 baseline.md 的 SaaS 类示例库挑 3 条改写确认(如"多租户数据查询必须带租户隔离条件")。
>
> **Agent(Phase 3)**:按 templates/ 生成 AGENTS.md、.qoder/rules/ 三文件、docs/ 骨架与 ADR-0001;征得同意后执行 `npm i -g @fission-ai/openspec` + `openspec init`;playwright skill 安装与 Repo Wiki 生成写入 docs/env/todo-manual.md。
>
> **Agent(Phase 4)**:重扫验证,输出"环境准备结果":本次生成 12 个文件;人工待办 2 项;体检结论 ✅ 21 / ⚠️ 2 / ❌ 0(⚠️ 均为 IDE 级人工待办),详见 docs/env/env-report.md。

### 示例 2:已有 openspec/ 的 Brownfield 项目二次体检

> **用户**:这个项目之前跑过一次环境准备,最近补了些文档,再体检一遍。
>
> **Agent(Phase 0)**:探测到已有 AGENTS.md、openspec/、docs/,git 仓库有历史提交 → 判定 single-repo、已有项目。
>
> **Agent(Phase 1)**:六域扫描:D2 openspec/ 已 init 且 config.yaml 含项目上下文 → `[已覆盖]`;D1 AGENTS.md 存在但缺"流水线用法"章节 → `[已有·建议补齐]`;D4 ADR 目录缺 0000-index.md → `[已有·建议补齐]`;其余 `[已覆盖]`。
>
> **Agent(Phase 2)**:提问中 `[已覆盖]` 项(含整个 D2)不出现,只列 2 个增量项,用户确认补齐。
>
> **Agent(Phase 3)**:增量补齐 AGENTS.md 的"流水线用法"章节(不动已有内容)、生成 0000-index.md;openspec/ 与 config.yaml 零改动。
>
> **Agent(Phase 4)**:重扫验证,env-report.md 保留上次状态列,呈现"上次 ❌ → 现在 ✅"对比;输出"环境准备结果":跳过([已覆盖])21 项;体检结论 ✅ 23 / ⚠️ 0 / ❌ 0。

## 附加资源

- [references/baseline.md](references/baseline.md) —— 六域基线清单(核心资产:检查方法+判定标准+铁律示例库)
- [references/multi-repo.md](references/multi-repo.md) —— 多仓专属流程:逐仓分析、service-map 生成与校验规则
- [references/ide-manual-steps.md](references/ide-manual-steps.md) —— IDE 级配置人工操作步骤库(待办清单素材)
- [templates/AGENTS.md.tmpl](templates/AGENTS.md.tmpl) —— Agent 入口文件模板
- [templates/always-on-rules.md.tmpl](templates/always-on-rules.md.tmpl) —— P0 铁律规则模板
- [templates/pre-commit-checklist.md.tmpl](templates/pre-commit-checklist.md.tmpl) —— 提交前自检清单模板
- [templates/git-collaboration.md.tmpl](templates/git-collaboration.md.tmpl) —— Git 协同约定模板
- [templates/openspec-config.yaml.tmpl](templates/openspec-config.yaml.tmpl) —— openspec 项目上下文配置模板
- [templates/project-maps.md.tmpl](templates/project-maps.md.tmpl) —— D8 五张项目地图骨架模板
- [templates/diff-gate.mjs.tmpl](templates/diff-gate.mjs.tmpl) —— D1-6 diff 门禁脚本模板(跨平台 Node)
- [templates/service-map.yaml.tmpl](templates/service-map.yaml.tmpl) —— 多仓服务拓扑模板
- [templates/docs-readme.md.tmpl](templates/docs-readme.md.tmpl) —— 文档体系索引模板
- [templates/adr-template.md.tmpl](templates/adr-template.md.tmpl) —— MADR 精简模板
- [templates/adr-index.md.tmpl](templates/adr-index.md.tmpl) —— ADR 索引模板
- [templates/adr-0001-env.md.tmpl](templates/adr-0001-env.md.tmpl) —— 首条环境决策 ADR 模板
- [templates/env-report.md.tmpl](templates/env-report.md.tmpl) —— 体检报告模板
- [templates/todo-manual.md.tmpl](templates/todo-manual.md.tmpl) —— IDE 级人工待办清单模板
