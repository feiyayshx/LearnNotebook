---
name: ai-trace
description: 量化 AI coding 的人效、质量与风险,回答"AI 是否真提效/是否越界/占比多少"。维护 .qoder/ai-trace/ 指标目录:init 初始化(含 git AI 提交归因约定),close 在任务结束时从 git 历史、delivery-docs/、verify-runs/ 等既有产物自动挖掘生成单任务 trace 与摘要,report 聚合输出周报与 metrics.csv 及 AI 提交占比。当用户要求初始化 AI coding 度量、统计 AI 提交占比、生成任务 trace、输出人效周报/管理层报告,或流水线技能(deliver-system/spec-loop-verify)完成一次交付需要留痕指标时使用。不写业务代码、不重复记录既有证据、不上报任何外部服务。
---

# ai-trace（AI Coding 量化度量）

横切于交付流水线全程的度量层：流水线技能负责把活干好，本 skill 负责**用数据证明活干得好**。

```
env-bootstrap → spec-writing → spec-review → deliver-system → spec-loop-verify
└────────────────── ai-trace(横切度量,任务结束时聚合指标)──────────────────┘
```

**核心原则：不重复记账**——deliver-system 的 `delivery-docs/`、spec-loop-verify 的 `verify-runs/` 已经是单次交付的证据账本，本 skill 只在任务结束时**挖掘聚合**这些既有产物 + git 历史，生成管理指标，绝不要求各环节额外手工填报。

## Git 提交归因约定（AI 占比统计的基础）

**方案：`--author` 归因，committer 保持真人。**

- AI 主导产生、经用户认可的改动，提交时使用：
  ```bash
  git commit --author="qoder <qoder@ai.local>" -m "feat: <说明>"
  ```
  author 标记来源为 AI，committer 仍是开发者本人 git 配置——**问责链不断**（谁批准合入谁负责），统计链清晰（author 区分人机）。
- 人类手写的改动正常提交，不加 `--author`。
- **禁止修改任何 git config**（全局或仓库级 user.name 都不改）；归因只靠单次提交的 `--author` 参数，忘加也只是统计遗漏，不会污染身份。
- 降级方案：团队不接受改 author 时，改用提交信息尾部 trailer `AI-Assisted: qoder`，统计命令相应换为 `git log --grep="AI-Assisted: qoder"`。选择哪种方案在 init 时与用户确认并写入 config.yaml。

**配套提交纪律**（init 时征得用户同意后写入项目 `.qoder/rules/git-collaboration.md`，未同意则只写入本 skill config 作会话内约定）：

1. **小步频提**：每完成一个用户认可的任务/检查点/修复即提交一次，不攒大提交（提交粒度 = 统计粒度）；
2. 只提交**用户已认可**的改动；提交前照常跑项目自检，禁止为提频跳过钩子；
3. 提交信息照常遵守项目提交规范（Conventional Commits 等），归因不改变信息格式。

## 目录与产物

```
.qoder/ai-trace/                     # 随 git 同步,团队共享
├── config.yaml                      # 项目名、归因方案(author/trailer)、脱敏规则、基线口径
├── runs/RUN-<yyyymmdd>-<nn>/
│   ├── trace-manifest.json          # 单任务结构化指标(schema 见下)
│   └── summary.md                   # 单任务一页纸摘要(人读)
└── dashboards/
    ├── weekly-summary.md            # 周报(人读,含 AI 提交占比与趋势)
    └── metrics.csv                  # 指标宽表(每任务一行,可导入 Excel/BI)
```

trace-manifest.json 核心字段（缺数据的字段写 null，不编造）：

```json
{
  "run_id": "RUN-20260807-01", "task": {"type": "feature|bugfix|verify", "title": "", "status": ""},
  "range": {"start_commit": null, "end_commit": null},
  "cycle": {"spec_final": null, "impl_done": null, "verify_pass": null},
  "requirements": {"clarifications": 0, "spec_defects_found": 0, "open_questions_left": 0},
  "execution": {"files_changed": 0, "out_of_scope_files": 0, "ai_commits": 0, "human_commits": 0,
                "ai_lines_added": 0, "human_lines_added": 0},
  "verification": {"checks_pass": 0, "checks_fail": 0, "fix_iterations": 0, "human_decisions": 0},
  "risk": {"p0_violations": 0, "diff_gate_blocks": 0, "sensitive_approvals": 0}
}
```

## 四个子流程

### `/ai-trace init`（每项目一次）

1. 与用户确认：归因方案（author / trailer）、是否把提交纪律写入 git-collaboration.md、统计基线起点（默认今天）；
2. 生成 `.qoder/ai-trace/` 目录与 config.yaml、metrics.csv 表头；
3. 确认 `.gitignore` 不排除本目录（须随 git 同步给协作者）；
4. 输出"归因自检"：现场做一次 `git log --author=qoder` 演示，确认统计通路可用。

### `/ai-trace start`（任务开始，可选）

轻量登记：创建 RUN 目录 + manifest 骨架（任务类型/标题/起始 commit）。跳过也没关系——close 时可从 git 与产物时间戳重建。

### `/ai-trace close`（任务结束，核心动作）

自动挖掘，按存在什么挖什么（都不存在也能出 git 维度）：

**统计区间口径**：自 start 登记的起始 commit（未执行 start 时取上一 RUN manifest 的 `range.end_commit`，均无则取 init 确定的基线起点）至当前 HEAD；close 时必填本 RUN 的 `range.end_commit` 供下次接续。

| 数据源 | 挖什么 |
|--------|--------|
| git 历史（必有） | 本任务区间的 AI/人类提交数与行数：`git log --author="qoder" --numstat <start_commit>..HEAD` 对比全量（无起始 commit 时兜底 `--since=<起点>`） |
| `delivery-docs/`（如有） | 任务数、越界文件数、门禁通过、修复轮次、人工裁决（读 state/events/report） |
| `.qoder/verify-runs/`（如有） | 检查点通过/失败/BLOCKED、fix 次数、SPEC-ISSUE 数（读 plan/report/fixes） |
| `docs/specs/`（如有） | 澄清问题数、Open Questions 余量（读 spec 确认记录章节） |

产出 trace-manifest.json + summary.md，并向 metrics.csv 追加一行。**挖不到的指标写 null 并在 summary 里注明缺口**，禁止用估算冒充实测。

### `/ai-trace report`（周期性，聚合）

聚合 runs/ 与 git 历史生成 `dashboards/weekly-summary.md` + 刷新 metrics.csv，包含五类指标：人效（周期）、质量（缺陷前置/修复轮次）、风险（越界/红线）、协作（人工裁决/返工）、**AI 占比**（提交数与行数、周趋势）。

## 安装后怎么看结果（观测闭环）

**① 随时看占比（不依赖本 skill 运行，两条命令）：**

```bash
git shortlog -sn --since="1 month ago"          # 提交数排名,qoder 行即 AI 提交数
git log --author="qoder" --shortstat --since="1 month ago" \
  | grep -E "files? changed" | awk '{f+=$1; i+=$4; d+=$6} END {print f"文件 +"i" -"d}'
```

**② 看单任务**：打开 `.qoder/ai-trace/runs/RUN-*/summary.md` —— 一页纸：周期、澄清数、修复轮次、越界数、本任务 AI 提交占比。

**③ 看趋势**：打开 `.qoder/ai-trace/dashboards/weekly-summary.md`（周报）；`metrics.csv` 拖进 Excel/BI 画趋势图。周报样例结构：

```markdown
# AI Coding 周报（2026-W32）
- 本周 RUN 5 个：4 完成 / 1 BLOCKED；需求→验证通过中位周期 1.5 天
- AI 提交占比：提交数 68%（34/50），代码行 82%；上周 61%/79%
- 质量：Spec 缺陷前置 9 个；E2E 前置缺陷 6 个；UAT 前遗留 0
- 风险：P0 红线 0；diff 门禁拦截 2 次（均在提交前）
```

## 使用方法（最短路径）

1. 安装本 skill → 项目里执行 `/ai-trace init`（一次，2 分钟）；
2. 日常开发照旧，唯一变化：AI 产生并经你认可的提交带 `--author="qoder <qoder@ai.local>"`（流水线技能会自动遵守，见协作节）；
3. 每个任务结束 `/ai-trace close`；每周 `/ai-trace report`；
4. 看结果：上面三个入口。管理层只看 `dashboards/`。

## Boundaries (I do NOT)

- 不写业务代码、不改业务文件；
- 不修改 git config、不代为 push、不跳过任何提交钩子；
- 不重复记录 delivery-docs/verify-runs 已有的明细，只做聚合引用；
- 不把指标上报任何外部服务，全部落在仓库内；
- 挖不到的指标如实写 null，禁止估算冒充实测；
- 不记录密钥、凭据、个人隐私字段（config.yaml 脱敏规则约束）。

## 流水线协作（可选上下游，存在时优先接驳）

- **env-bootstrap**：其 D5 基线核对本 skill 是否就位；`docs/README.md` 路由表登记 `.qoder/ai-trace/` 落点；提交纪律写入其 D1-5 的 git-collaboration.md。
- **deliver-system / spec-loop-verify**：两者产生提交时遵守本约定（用户已在 init 认可的前提下，提交带 `--author="qoder <qoder@ai.local>"`）；其证据目录是 close 的首要挖掘源。
- **独立使用**：未装任何流水线技能时，本 skill 仅凭 git 历史 + 用户口述任务边界工作，占比统计与周报功能完整可用，仅质量/修复类指标为 null。
