# UI loop verification（UI 闭环走查）

## 何时读取

任何含 Web/小程序 UI 的交付，在体验版就绪（EXPRESS E3）、Change 门禁（FULL Gate 2）、里程碑集成（Gate 4）或用户报告 UI 缺陷时读取并执行。这是强制 Gate，不是可选取证。

## 核心原则

1. **闭环**：走查 → 缺陷登记 → 修复 → 重验，直到全部旅程通过或达轮次上限；不允许"发现问题但先记下来就算验证完成"。
2. **真实驱动**：用 chrome-devtools / playwright / browser-use 真实操作浏览器；小程序用微信开发者工具自动化。禁止仅凭代码阅读、编译通过或单张首页截图宣称"UI 正常"。
3. **逐步断言**：每个操作步骤都有可判定预期——DOM 可见状态、接口请求/响应、console 无新增错误、必要时数据落库值。
4. **证据留痕**：每轮走查的证据完整落盘，绑定 commit；代码再变更即证据过期。

## 走查清单生成

来源优先级：EXPRESS 用 `delivery-docs/intent/express-intent.md` 的旅程列表；FULL 用 `delivery-docs/product/acceptance.json` 中 UI 相关验收项；两者都没有时先补清单再走查。

每条旅程展开为步骤表：

```markdown
## J1: <角色> <操作> <期望结果>
| 步骤 | 操作 | 断言 |
|---|---|---|
| 1 | 打开 /login | 表单渲染，console 无错误 |
| 2 | 输入合法账号并提交 | 调用 POST /auth/login 返回 200，跳转首页 |
| 3 | ... | ... |
```

必含项：核心正向路径；关键负向路径（非法输入、空态、失败提示）；跨页跳转与返回；有原型契约时的状态/交互一致性检查点。

## 单轮走查执行

1. 记录轮次元数据：轮号、commit、启动命令、环境、时间。
2. 逐旅程逐步骤执行：
   - 操作后 `take_snapshot`/`take_screenshot` 采集状态；
   - 断言 DOM/文案/路由；
   - `list_console_messages` 检查无新增 error（已知无关告警登记豁免）；
   - `list_network_requests` 核对关键接口状态码与响应形态；
   - 涉及写操作时按需断言数据结果（数据库值或 mock 状态）。
3. 步骤失败 → 该旅程标 FAIL，继续走完其余旅程（一轮内收集全部缺陷，避免逐个来回）。
4. 断言不可自动化的项（如视觉美感）标注 `MANUAL`，留截图给用户在体验确认门判断。

## 缺陷登记与修复

每个缺陷写入本轮目录的 `defects.md`：

```markdown
## D<n>: <一句话描述>
- 旅程/步骤：J2 / step 3
- 指纹：<稳定特征：报错信息 / 接口+状态码 / 断言差异>
- 复现：<最短复现步骤>
- 分类：产品缺陷 / 测试脚本问题 / 环境问题 / 数据问题
- 状态：open → fixed(commit) → verified(轮次)
```

修复规则：

- 在授权范围内直接修复（含不改设计、不改内容的 UI 一致性微调）；超出范围（需求变更、设计决策）进入 `WAITING_USER`。
- 修复后**只重验受影响旅程 + 核心冒烟旅程**，不必全量重走。
- 同一指纹修复 2 次仍复现 → 停止该缺陷的重试，升级报告。

## 结束条件与轮次上限

- **通过**：全部旅程 PASS（MANUAL 项已移交用户），出 PASS 报告。
- **上限**：默认最多 3 轮（走查+修复算一轮）。达上限仍有 FAIL → 停止，出证据化报告并列出余留缺陷，进入 `WAITING_USER` 或 `REWORK`。
- **例外提前停止**：需要用户提供信息/授权；环境不可建立；缺陷属需求歧义。

## 证据目录结构

```text
delivery-docs/verification/loop-<yyyyMMdd-HHmmss>/
├── plan.md          # 走查清单（旅程 × 步骤 × 断言）
├── round-<n>/
│   ├── J<x>-step<y>-<pass|fail>.png
│   ├── console-summary.md
│   └── network-summary.md
├── defects.md       # 缺陷台账（含状态流转）
└── report.md        # 结论：轮次、通过率、余留项、绑定 commit
```

## 硬门禁

- 走查未全部通过且用户未明示接受余留缺陷时，不得宣称 UI 验证完成、不得进入用户体验确认门、不得声明 `acceptance-ready`。
- 每个 FAIL 必须对应 defects.md 条目；每个 fixed 必须有 verified 轮次记录。
- report.md 必须绑定 commit；commit 变更后旧报告不得复用为当前证据。
- 不得用重试次数掩盖首次失败；首败必须留痕。
