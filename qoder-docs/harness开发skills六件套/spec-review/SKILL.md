---
name: spec-review
description: Reviews requirement/specification documents (feature spec, PRD, technical design doc) for consistency, completeness, correctness, clarity and implementability, generates a structured issue report with causes and fix suggestions, then applies user-approved fixes and optionally re-reviews. Use when the user provides a spec document (optionally with UI prototype) and asks for 评审/review/审查/修改意见, or when finalizing a spec before development.
---

# Spec Review（需求规格评审与修复）

通用 spec 评审工作流：**通读 → 评审 → 报告 → 确认 → 修复 → 可选复审**。

## 核心原则

1. **独立评审，不照搬历史**：每个 spec 按通用维度独立分析；禁止把过往项目的问题清单当 checklist 硬套，禁止在报告中写「参照 XX 项目」
2. **每个问题三要素**：原因（为什么是问题）+ 影响（不处理的后果）+ 修改建议（具体可执行）；说不出实际影响的不报
3. **不硬找问题**：质量达标即输出「通过」；P2 不凑数；复审时尤其克制
4. **用户决策优先**：修复前必须经用户确认范围，未确认不得改 spec

## 输入要求

| 输入 | 必需性 | 说明 |
|------|--------|------|
| spec 文档 | **必需** | 缺少时直接向用户索取，不得凭空评审 |
| 产品原型/设计稿 | 可选 | 提供时用于一致性交叉验证；spec 与原型冲突时需明确以谁为准（默认建议：以 spec 为准并同步原型） |
| 项目约束/规范文档 | 可选 | 提供时作为评审依据之一 |

## 工作流程

### 第 1 步：通读与建模
- 完整阅读 spec（长文档分段读完，不得只读开头）；有原型则同步阅读
- 建立文档模型：核心业务流、状态机、数据模型（表/字段）、接口清单、规则编号体系（如 BR/AC）、角色与权限

### 第 2 步：按维度评审
按 [checklist.md](checklist.md) 的 6 大维度逐项检查：一致性 / 完整性 / 正确性 / 明确性 / 可验证性 / 工程约束。

- 维度是思考框架而非凑数清单，某维度无问题就跳过
- 发现矛盾时记录**所有涉事位置**（章节号），修改建议须覆盖每一处
- 报告语言使用 spec 原文语言（中文 spec 出中文报告）

### 第 3 步：生成评审报告
按 [report-template.md](report-template.md) 生成，写入 spec 同目录 `{spec文件名}-评审意见.md`（用户指定其他位置时从其指定），并在对话中输出摘要（总体结论 + 各级问题数 + 关键 P0 列表）。

问题分级：
- **P0 阻断**：开发按此做必错/返工（逻辑矛盾、模型缺失、公式错误）
- **P1 扯皮**：不解决必延期（未定义行为、口径不一、悬空引用）
- **P2 优化**：可读性/结构性改进

### 第 4 步：确认修复范围（必须停下询问）
使用 AskUserQuestion 询问修复范围：

1. 全部按建议修复（P0+P1+P2 采纳建议默认）
2. 仅修复 P0+P1
3. 仅修复 P0
4. 不修复，仅保留报告

用户对具体条目有异议时逐条确认；**以用户最终决定为准**，用户意见与建议冲突时按用户意见执行。

### 第 5 步：执行修复
- 按确认范围修改 spec：同一问题的**所有涉事位置**同步修改，改完全文检索确认无残留
- 保持文档原有风格与结构；只改确认的内容，不顺手优化、不重构无关段落
- 修复完成后输出变更摘要（改了哪些位置）

### 第 6 步：询问是否复审（有界：复审最多 3 轮）
修复后询问用户是否需要再次评审：

- **是** → 回到第 1 步对修复后文档复审，重点：修复是否引入新矛盾、是否满足优秀 spec 最佳实践。**无问题时明确输出「评审通过」并结束**，禁止为显得有产出而硬找问题
- **否** → 结束
- **3 轮复审后仍有 P0** → 停止自旋，输出未决问题清单与双方分歧点，升级用户裁决（继续逐条改只会来回震荡）

## 质量红线（自检）

- [ ] 每个问题都有：位置 + 原因 + 影响 + 具体修改建议
- [ ] 报告无业务词汇过拟合、无历史项目指代
- [ ] P0/P1 判断有依据，不夸大
- [ ] 修复前获得过用户明确同意
- [ ] 复审不超过 3 轮；超限如实升级用户裁决而非继续修改
- [ ] 复审无新问题时如实说「通过」

## 流水线协作（可选上下游，存在时优先接驳）

```
env-bootstrap → spec-writing → spec-review → deliver-system → spec-loop-verify
└────────────────── ai-trace(横切度量,任务结束时聚合指标)──────────────────┘
```

- **上游**：评审对象常为 spec-writing 定稿产物；其 Open Questions 未清零时先提示用户回流澄清（未决项直接按 P1 记录，不重复评审）。spec-loop-verify 产出的 SPEC-ISSUE 清单可作为复审**线索**输入——仅作线索，仍按通用维度独立评审，不照单采纳（核心原则 1）。项目有 env-bootstrap 产物时，「工程约束」维度可对照 `.qoder/rules/` 铁律与 `docs/maps/` 五张地图交叉验证（如 spec 涉及的表/接口/模块是否与数据图、接口图、业务域主流程标注冲突）。
- **下游**：评审通过（或修复定稿）的 spec 可直接作为 deliver-system 的 intent 权威源件与 spec-loop-verify 的交付标准基线；评审结论建议随 spec 一起归档（如 env-bootstrap 建立的 `docs/specs/` 目录）。

## 附加资源

- [checklist.md](checklist.md)：6 大维度详细检查点
- [report-template.md](report-template.md)：评审报告标准模板
