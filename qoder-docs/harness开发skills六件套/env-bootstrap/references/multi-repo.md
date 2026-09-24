# 多仓专属流程(multi-repo)

本文件是 [SKILL.md](../SKILL.md) Workflow 的 multi-repo 分支展开:仅当 Phase 0 判定工作区形态为 multi-repo 时适用。逐仓分析支撑 [baseline.md § D7](baseline.md#d7-多仓拓扑仅-multi-repo) 四个检查项(D7-1~D7-4)的探测与判定;service-map 生成与校验规则以本文件为准,与 baseline.md D7 的判定标准一一对应,不另设第二套口径。

各章与 Workflow 各 Phase 的对应关系:判定信号 → Phase 0;逐仓分析流程 → Phase 1;service-map 生成规则 → Phase 1(草稿)+ Phase 2(阻塞确认)+ Phase 3(定稿);校验规则 → Phase 4 追加执行;openspec 落点 → Phase 3。

## 判定信号

Phase 0 按以下顺序探测工作区形态,命中任一强信号即可判定,无需继续:

| 顺序 | 信号 | 探测方法 | 结论 |
|------|------|----------|------|
| 1 | 工作区根存在 `service-map.yaml` | `test -f service-map.yaml` | multi-repo(此前已初始化过,本次为更新/体检) |
| 2 | `repos/` 目录下存在 ≥ 2 个 git 仓 | `ls repos/*/.git` 计数(目录含 `.git/` 即算一个仓) | multi-repo |
| 3 | `repos/` 下仅 1 个 git 仓,或工作区根既有 `.git/` 又有 `repos/` | 同上 + `test -d .git` | 不确定,进入交互确认 |
| 4 | 无 `service-map.yaml`、无 `repos/`,工作区根即 git 仓(或空目录) | `test -d .git` | single-repo |

不确定时(第 3 行,或目录结构与上述均不符)必须用 AskUserQuestion 让用户在「single-repo / multi-repo」二选一,不得凭目录命名猜测形态。判定为 single-repo 则跳过本文件全部内容与 baseline.md 整个 D7 域。

## 逐仓分析流程

Phase 1 对 `repos/` 下每个 git 仓执行**只读**采集(不在 `repos/` 下的业务仓也纳入分析,路径如实记录,差异按 D7-2 处理)。每仓采集四类线索:

1. **构建描述文件**:按存在性依次探测 `pom.xml` / `build.gradle` / `package.json` / `go.mod` / `pyproject.toml`(含 `requirements.txt`),Read 提取工件名、模块结构、构建工具;
2. **技术栈**:由构建描述文件与锁文件推断语言、框架与构建工具(如 Maven+Spring Boot、npm+Vue、Go modules);证据冲突时按确定程度排序取信:`CI/构建文件 > 代码事实 > 文档 > 口头描述`;
3. **对外端口线索**:grep 配置文件中的 `server.port`(application.yml/properties)、`PORT`(.env.example/Dockerfile/docker-compose,只读文件名与键,不读 `.env` 密钥值)、启动脚本中的 `--port` 参数;探测不到写 `未知`,不编造;
4. **依赖线索**:grep 配置中的服务名引用(如 `xx-service` 的 base-url/注册名)、RPC 客户端声明(Feign/gRPC/openfeign 依赖)、构建文件中对同工作区其他仓工件的依赖;每条线索记录出处文件,供后续标注置信度。

同时执行 `git -C repos/<repo-name> status --porcelain` 与 `git -C repos/<repo-name> remote -v` 采集 git 状态(支撑 D7-3;remote 只记录脱敏路径,不记录带 token 的 URL)。

每仓输出一行,汇总为逐仓分析表(字段固定,不可缺列):

| 仓名 | 技术栈 | 端口 | 疑似依赖 | git 状态 |
|------|--------|------|----------|----------|
| order-service | Maven + Spring Boot | 8081 | user-service(配置显式引用) | clean |
| admin-web | npm + Vue 3 | 5173 | gateway(仅凭命名推测) | dirty(2 个未提交文件) |

git 状态取值:`clean` / `dirty(n 个未提交文件)` / `缺失`(登记的仓本地不存在)/ `异常`(无法访问、remote 不匹配等)。dirty/缺失/异常仓照常登记本表并继续分析其余仓,处置见「禁忌」章。

## service-map 生成规则

草稿由逐仓分析表汇总生成,结构以 [../templates/service-map.yaml.tmpl](../templates/service-map.yaml.tmpl) 为准:每个服务至少含名称、路径、描述基本字段(对应 D7-1 结构合规要求),路径填仓的实际所在位置,不做任何美化。生成分三步:

1. **草稿(Phase 1)**:分析表每行映射为一个 `services:` 条目;「疑似依赖」列映射为 `depends_on`,每条依赖必须标注置信度——**高置信度 = 配置中显式引用**(base-url、注册名、构建文件工件依赖等有出处文件的证据);**低置信度 = 仅凭命名推测**(只有名称相似,无任何配置或代码证据)。dirty/缺失/异常仓对应的服务条目标注「待确认」。
2. **阻塞确认(Phase 2)**:低置信度依赖必须逐条经 AskUserQuestion 确认后才能写入定稿——确认成立则升级为高置信度并去除标注;确认不成立则删除该依赖;用户确认不了的,从定稿中移除并登记 `docs/open-questions.md`。未经确认的低置信度依赖不得进入定稿,此步不可跳过。service-map.yaml 本身也不可跳过:用户可审阅、修改草稿,但不能选择不生成(D7-1 补齐方式)。
3. **定稿(Phase 3)**:`build_order` 只含构建依赖(编译/包依赖,如共享库、api 契约包),运行时调用、路由、消息订阅不参与排序;按构建依赖做拓扑排序,**必须无环**——发现环时不强行输出顺序,将环上的服务与证据登记 `docs/open-questions.md`,`build_order` 中环内服务以注释标注待定。

更新模式(工作区已有 service-map.yaml):只追加新服务条目、只修正经确认的变更,不覆盖已有条目;已有条目信息变化(新增依赖、端口变更)列入 Phase 2 交互项,经用户确认后再更新。

## 校验规则

Phase 4 在六域重扫之外,对定稿 service-map.yaml 逐条追加执行以下校验(与 SKILL.md Phase 4 的 [multi-repo] 追加校验行一致):

| 编号 | 规则 | 执行方法 | 不通过时 |
|------|------|----------|----------|
| V1 | `depends_on` 不引用不存在的服务 | 提取全部 `depends_on` 服务名,逐个核对是否在 `services:` 列表中存在 | 属非法引用,按 D7-4 判 `[缺失]`,必须修正后重跑本校验才能定稿 |
| V2 | 无 token/密码/AK/SK 明文 | grep 全文 `token` / `password` / `secret` / `accesskey`(含大小写变体与 `ak`/`sk` 键名),人工复核命中行是否为凭据明文 | 立即提示用户清除,按 D7-1 判 `[已有·建议补齐]`,清除后重跑本校验 |
| V3 | `repos/` 路径约定不在 service-map 中重复声明 | 检查文件中是否出现声明「业务仓统一放 repos/ 下」这类约定性文字或全局配置块 | 删除约定声明——约定的唯一出处是 SKILL.md 与 baseline.md D7-2;service-map 只逐服务记录实际路径(事实),不重复声明规则,避免两处口径漂移 |

三条规则全部通过,且 D7-4 无未确认的低置信度依赖残留,service-map 才算校验通过;任一条不通过按 SKILL.md Phase 4 有界规则处理(生成→验证最多 2 轮,未收敛项如实列入 env-report.md 交用户)。

## 禁忌

multi-repo 分支全程遵守(与 SKILL.md「multi-repo 禁忌」一致,此处为执行细则):

1. **不修改子仓文件**:所有分析只读;Harness 资产、docs/、service-map.yaml 一律落工作区根,任何 Phase 都不向 `repos/<repo-name>/` 内写入或改动文件;
2. **不默认 git pull**:已有仓最多执行 `git fetch`(只更新远端引用,不动工作区);merge/pull/rebase/切分支必须列入方案经用户逐仓确认后才可执行,未确认前基于本地现状分析;
3. **缺失/dirty 仓不阻塞**:仓本地缺失、clone 失败、工作区 dirty 或状态异常时,登记 `docs/open-questions.md`(缺失仓的 clone 步骤另写入 `docs/env/todo-manual.md`),service-map 中对应服务标注「待确认」,继续分析其余仓,绝不因个别仓中断整体流程(对应 D7-3 判定标准);
4. **不把单仓局部做法推广为全局规则**:只有在多数仓中一致出现且有明确证据的模式,才可写入工作区级规则;仅个别仓存在的做法登记 `docs/open-questions.md`,不写成「必须/禁止」级条目。

## openspec 落点

multi-repo 工作区的 `openspec init` 落点没有默认值,Phase 3 执行 init 前必须用 AskUserQuestion 让用户二选一(对应 baseline.md D2-2 的 [multi-repo] 补齐方式):

| 选项 | 适用场景 | init 位置 |
|------|----------|-----------|
| 工作区级 | 变更经常跨多个仓(如契约变更牵动网关+前端+多个服务),需要一处统一管理变更提案 | 工作区根 `openspec/` |
| 指定主仓 | 变更集中于某一个仓,其余仓基本稳定(如只有主服务在活跃开发) | 用户指定的 `repos/<repo-name>/openspec/`(该仓须为用户明确点名,不得代选) |

选择结果记录进本次运行生成的 ADR-0001(环境决策记录),便于后续体检时核对落点未漂移;用户当场决定不了时,D2-2 项标 `[缺失]` 保留在差距报告,登记 `docs/open-questions.md`,不代为选择。
