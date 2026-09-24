# Loop and reporting

## 何时读取

在开始或恢复任一 Change、设置 Goal/Quest 预算、自动修复失败、用户提供新信息、暂停/阻塞、Change/里程碑完成以及 Stop Hook 运行前读取。

## 有界闭环

每轮只推进一个 Ready 的主切片：

```text
读取权威状态与当前 Change
→ 声明本轮目标、范围、Oracle 和预算
→ 执行有限修改
→ 运行目标验证
→ 分类失败并判断进展
→ 保存状态、事件、证据和报告
→ 通过则迁移；失败则有限修复或请求用户
```

建议默认预算：单个相同失败最多自动修复 3 次；相同失败指纹连续出现 2 次且无有效进展即停止；连续 2 轮没有新增通过项、失败减少、有效 Diff 或状态迁移即判定无进展。项目可收紧，不能在未记录理由时放宽。

## 每轮步骤

1. 规划阶段从 manifest/state/events 和 OpenSpec Change 恢复；执行阶段从 Stage B execution manifest、verification matrix、attempt ledger、OpenSpec tasks、Git、Qoder run、计划日志和验证报告恢复，不依赖聊天记忆猜测。
2. 声明一个可验证目标、非目标、Oracle、风险、最大尝试和本轮允许的工具/环境。
3. 记录起始 commit、配置/测试摘要和预期状态迁移。
4. 实施最小有效批次，更新必要的规格、计划、ADR/PDR、文档和事件。
5. 运行匹配当前 Gate 的验证，保存原始结果和失败指纹，并更新 AC → task → code → test → evidence → review → integration 映射。
6. 比较本轮与上轮：新增通过、失败减少、状态迁移、有效代码/页面变化至少出现一项才算进展。
7. M0–M3 helper 状态只通过脚本更新；Qoder-native 执行进度由 OpenSpec tasks/Change lifecycle、Git、Qoder run 和验证报告记录，不伪造 `delivery-docs/state/state.json` 的 M4+ 阶段。
8. 从模板建立 execution attempt，并用 `loopctl record-attempt` 追加；若返回 stop，禁止继续自动修复。
9. 生成 run result 和用户报告，写出精确下一动作与恢复位置。

## 用户新信息处理

执行中收到消息时先分类：新需求、澄清、产品/架构决定、验收变化、假设确认、阻塞答案或延期项。范围和验收变化必须进入 Change Control、更新权威制品、做影响分析并使旧证据失效；不得在循环中静默吸收。

## 报告契约

每次暂停、阻塞、Change 完成和里程碑完成生成：

- `delivery-docs/state/runs/<run-id>/result.json`：机器可读状态、失败和恢复点。
- `delivery-docs/verification/runs/<run-id>/report.md`：用户可读结论和证据索引。

报告至少包含阶段、里程碑、Change、本轮目标/范围、完成内容、Requirement/Acceptance/原型覆盖变化、commit/环境、测试和浏览器证据、失败指纹与已尝试方案、是否有有效变化、阻塞影响、独立评估、硬门禁与质量分、推荐选项、未确认假设、剩余预算和下一准确动作。

## 状态语义

- `WAITING_USER`：存在只有用户能决定或授权的问题。
- `BLOCKED`：外部依赖、环境或不可自动恢复故障阻止进展。
- `PAUSED`：预算/窗口结束，但状态一致且可继续。
- `REWORK`：验证失败，需要回到实现或规格阶段。
- `ACCEPTANCE_VERIFIED`：目标验收证据通过，不代表业务已签署。
- `ACCEPTANCE_READY`：最终闭环校验通过、质量至少 90，可交给用户验收，不代表业务已签署。
- `ACCEPTED`：所需业务验收已记录。

## Stop Hook 边界

Stop Hook 只做快速、幂等的证据新鲜度检查、报告和通知，不运行全量 E2E。若 `stop_hook_active=true` 必须放行；最多因缺失必需报告阻止一次，避免递归停止循环。

## 输入与输出

**输入：** 权威项目制品、当前状态/事件、Delivery Contract、预算、工具结果和用户消息。

**输出：** 有限修改、Gate 证据、适用的规划状态或 OpenSpec task 更新、机器/用户报告和可恢复下一动作。

## 硬门禁

- 每轮必须有 Oracle、预算和单一主切片。
- 没有新鲜证据不得在报告或 OpenSpec 中声称代码/验收已验证。
- 用户未批准的新范围不得成为实现的一部分。
- 达到重试或无进展阈值后必须停止，不能换措辞继续同一失败。
- 暂停或失败前必须保存状态、事件、证据索引和恢复点。
- 最终报告必须来自集成 commit 上通过的 `loopctl validate --final`；评分或任务勾选不能覆盖失败硬门禁。

## 停止条件

达到轮数、时间、成本或工具预算；相同失败/无进展达到阈值；需求冲突；权限、凭据、付费资源或生产决定；外部服务持续不可用；状态/证据无法对账；用户输入改变当前 Delivery Contract。停止后报告问题、影响、已尝试内容和推荐选择。
