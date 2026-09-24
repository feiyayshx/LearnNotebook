# Greenfield workflow

## 何时读取

当仓库没有可交付系统，用户提供 Spec/PRD 和可选 HTML、截图或可读取原型，希望从零构建 V1 支持的 Web 应用、Web API 或数据库业务系统时读取。

## 输入

- 原始 Spec、原型、补充约束和用户目标。
- 目标技术栈或允许 Skill 提议的选型范围。
- 交付环境、时间/成本预算和生产部署授权边界。
- 仓库、Git、Qoder、OpenSpec 和可用测试工具现状。

## 步骤

先按 SKILL.md 双轨路由判定：≤R2 且用户希望尽快看到可操作版本时走 EXPRESS 轨（见 express-workflow.md，不执行本文后续步骤）；FULL 轨中，R3/R4 或用户要求高保障时走增强保障路径，其余走快速路径（轻量路线图 + 验收清单）。

1. **只读登记。** 保存来源、版本和摘要；识别仓库与工具现状，不先写业务代码。
2. **规范化需求。** 生成角色、旅程、业务规则、异常流程、非功能需求、范围/非目标、Requirement 和 Acceptance ID。
3. **分析原型。** 提取页面、路由、组件、交互以及加载、空、错误、权限、响应式和可访问性状态，写入 prototype contract 并绑定 Requirement/Acceptance；原型缺失的后端、安全和数据语义不得自行当作已确认事实。
4. **澄清基线。** 把阻塞问题提交用户；把可逆、低风险假设落盘并注明确认期限。
5. **建立治理。** 初始化项目制品映射、交付状态、文档入口、PDR/ADR、测试策略和最小 Qoder 项目规则；不得覆盖现有文件。
6. **建立架构基线。** 明确系统边界、模块/领域、数据所有权、API 契约、身份权限、外部服务、部署模型、威胁和主要 ADR。
7. **拆分交付。** 建立 Feature Ledger、依赖图和里程碑；先规划可运行的 Walking Skeleton，再按用户价值拆成可独立验收的垂直切片。
8. **建立 OpenSpec。** 审查稳定上下文的配置候选；所有切片保留稳定 Change ID 和 Draft Delivery Contract，但只为下一个依赖已满足且获批的切片创建 OpenSpec Change，不把完整系统做成一个巨大 Change。
9. **授权逐 Change 实施。** 用户批准后建立 Stage B execution manifest，再将不可扩大的 handoff 交给 Agent/Quest/Goal/Experts，按依赖顺序执行有限修改、分层测试、验收级验证和 attempt 记录。
10. **Feature 闭环。** 逐 AC 维护 task/code/test/evidence/review/integration 映射；独立 evaluator 发现缺陷时重新打开对应任务并修复重验。
11. **系统集成。** 每个里程碑在合并后的干净环境运行系统级门禁；硬门禁全过、质量至少 90 且 final validate 通过后生成运行手册和验收报告，再 Verify、Sync、Archive。

## 每个 Delivery Contract 至少包含

- 包含与明确不包含的范围。
- Requirement、Acceptance、Feature 和 Change ID。
- 必须保留的已交付行为。
- 风险等级、依赖、迁移和回滚要求。
- 实现 Oracle、测试层级、验收场景和证据类型。
- 执行预算、人工门禁和停止条件。

## 输出

- 已版本化的输入和规范化需求/验收目录。
- 架构说明、ADR/PDR、路线图、Feature Ledger 和 Delivery Contracts。
- 一组按依赖排序的 reserved Change IDs、Delivery Contracts，以及下一个 Ready 切片的 OpenSpec Change。
- 可运行实现、自动测试、绑定 commit 的验证证据及验收报告。

## 硬门禁

- 在进入实现前，范围内 Requirement 到 Acceptance 和计划切片的映射必须完整。
- 阻塞问题未清零、关键架构决定未确认或 Change 没有 Delivery Contract 时不得 Apply。
- 一次只选择一个 Ready 的主切片；只有无共享写冲突且有单一集成者时才并行。
- 用户可见或跨边界 Change 必须有可重复的验收级测试；Web 旅程优先 Playwright。
- Change 未通过目标门禁和证据对账不得归档；里程碑未通过集成门禁不得声明 acceptance-ready。
- 原型项目未覆盖状态、交互、响应式、可访问性和获批偏差，或质量分不足 90 时不得声明 acceptance-ready。

## 停止条件

需求互相矛盾且会改变产品方向；缺少关键业务/权限/数据语义；需要未授权凭据、付费服务、破坏性操作或生产发布；测试环境无法形成可信基线；循环达到预算或无进展阈值。停止时保留检查点并生成问题报告。
