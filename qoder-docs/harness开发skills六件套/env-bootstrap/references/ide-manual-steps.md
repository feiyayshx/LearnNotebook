# IDE 级配置人工操作步骤库(ide-manual-steps)

本文件是 [baseline.md](baseline.md) D5/D6 域(及 D1-2 生成后的核对动作)中「IDE 级操作」的人工步骤素材库:这些配置发生在 Qoder IDE 设置界面内,本 skill 无法代为执行,只能在 Phase 3 把缺失项连同下述步骤写入 `docs/env/todo-manual.md`,由用户逐项操作并在二次体检时确认完成。操作路径与命令以《Qoder开发白皮书》第二章(环境准备与项目初始化)为准。

每节固定三要素:适用检查项(Dx-y)、操作步骤(编号列表,精确到菜单路径)、完成判据(用户可自验的一条命令或现象)。写入待办清单时按节整段摘抄,不改写判据口径。

## 市场 Skill 安装

**适用检查项**:D5-1(流水线四件套)、D5-2(市场三件套)

**操作步骤**:

1. 打开 Qoder Settings → Skills → Marketplace;
2. 在市场中依次搜索并安装三件套:`superpowers`(开发方法论合集,含 brainstorming / writing-plans / TDD / systematic-debugging 等 20+ 子 skill)、`chrome-devtools`(Chrome DevTools MCP 接入,管理端 UI 自动化必备)、`playwright`(端到端浏览器测试自动化);
3. 流水线四件套(spec-writing / spec-review / deliver-system / spec-loop-verify)不在市场,走 zip 导入:打开 Qoder Settings → Skills → Import,选择团队分发渠道(如 `skills-backup/` 目录或共享盘)中的对应 zip 包,逐个导入;
4. 导入/安装完成后回到 Settings → Skills 列表,确认七个 skill 均显示为已启用。

**完成判据**:新开会话输入 `/spec-writing`,该 Skill 能被识别并调起(市场三件套同理可用 `/superpowers`、`/chrome-devtools` 验证)。

## 上下文窗口设置

**适用检查项**:D6-3(模型与上下文窗口建议)

**操作步骤**:

1. 打开 Qoder Settings → Context Window;
2. 日常开发场景设置为 200k tokens(足够 spec + design + tasks 三份文档同时在会话里);
3. 执行复杂长交互(如 deliver-system 全流程、spec-loop-verify 多轮循环验证)前,临时调到 1M,避免中途丢上下文导致漂移;
4. 长任务结束后可调回 200k 以控制消耗。

**完成判据**:Settings → Context Window 设置页显示的当前值与所选场景一致(日常 200k / 长交付 1M)。

## 模型配置

**适用检查项**:D6-3(模型与上下文窗口建议)

**操作步骤**:

1. 打开 Qoder 会话界面的模型选择入口(或 Settings 中的模型配置项);
2. 按任务类型选择模型:Spec 撰写 / Spec 评审等长文档理解场景,选中文语境与长文档处理强的模型(白皮书 §2.1 实测:Kimi-K3 首选);长交付流程(deliver-system 等)选代码生成与工具调用稳定的模型(实测:GLM-5.2 首选);Qwen3.8-Max-Preview 在代码工程、深度推理等场景亦为实测可用选项;
3. 运行模式保持「智能体模式」(推荐),不使用「专家团模式」(多 Agent 上下文难对齐,实测容易漂移)。

以上模型清单为白皮书当期实测结论,**以团队当期实测为准**,模型迭代后应更新本节与待办清单。

**完成判据**:会话界面当前模型显示为按上述建议选定的模型,且运行模式为智能体模式。

## MCP 接入

**适用检查项**:D6-1(MCP 可用性)

**操作步骤**:

1. 打开 Qoder Settings → MCP;
2. 添加 `chrome-devtools` MCP 服务器(若已通过市场 Skill 安装 chrome-devtools,确认其 MCP 配置已启用);
3. 按需添加 `browser-use` MCP 服务器(浏览器自动化走查场景使用);
4. 保存配置后新开会话,让配置生效。

**完成判据**:新会话中执行一次轻量调用(如 chrome-devtools 的 `list_pages`)成功返回;调用失败时把错误信息记回 `docs/env/todo-manual.md` 供排查。

## Rules 项目级开关

**适用检查项**:D1-2(P0 铁律,规则文件生成后的核对动作)

**操作步骤**:

1. 确认 `.qoder/rules/` 目录下规则文件已生成(至少含 `always-on-rules.md`,由本 skill Phase 3 落盘);
2. 打开 Qoder Settings → Rules;
3. 确认规则作用域设置为**项目级**而非全局(全局会污染其他项目),且项目路径指向当前工作区;
4. 确认各规则文件的开关为启用状态;若发现规则被无视(如新会话不认铁律),优先检查此处的项目路径与开关。

**完成判据**:新开会话提问「请列出本项目的铁律」,Agent 能在无额外提示下准确答出 `always-on-rules.md` 中的铁律条目。

## Repo Wiki 生成

**适用检查项**:D6-4(Repo Wiki 已生成)

**操作步骤**:

1. 用 Qoder 打开项目根目录(多仓形态打开工作区根),等待 Qoder 对仓库的自动扫描索引完成;
2. 若 `.qoder/repowiki/` 未自动生成,在 Qoder 内触发仓库扫描 / Repo Wiki 生成动作(可在会话中直接要求 Agent 生成或更新 Repo Wiki);
3. 生成完成后抽查 Wiki 内容:模块拆解、架构概览、各模块技术栈与规范应与实际代码一致,明显过时或缺失时重新触发生成。

**完成判据**:执行 `ls .qoder/repowiki/`,目录存在且非空、含模块条目文件。

## QoderCLI(可选)

**适用检查项**:无固定检查项(可选增强项,不参与差距报告三态判定;仅 CI/CD 或远程服务器批量执行 Agent 任务的团队需要,由用户在 Phase 2 按需勾选)

**操作步骤**:

1. 在终端执行 `npm i -g qodercli`(也可在 Qoder 会话里让 Agent 代为安装);
2. 日常 IDE 内开发用不到 QoderCLI,无 CI/远程批量场景可直接跳过本节;
3. 若用于 CI,把安装命令写入流水线镜像或前置步骤,不依赖本机全局安装。

**完成判据**:终端执行 `qodercli --version` 能输出版本号。
