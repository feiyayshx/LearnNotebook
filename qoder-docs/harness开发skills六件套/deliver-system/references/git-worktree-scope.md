# Git checkout and Worktree scope

## 何时读取

在 M3 进入澄清/规划、检测 `.git` 文件、Qoder 选择 Worktree、恢复/移动/合并/变基后，或要把 Planning Seal/证据绑定到 Git 时读取。

## 责任边界

Qoder/Git/用户负责创建、移动、合并、Review、Commit、Move to Local、prune 和删除 Worktree。Skill 只验证当前 checkout，并生成：

- `checkout_kind`；
- opaque `repository_scope_id` / `checkout_scope_id`；
- HEAD、ref/detached、dirty 与 dirty digest；
- integration owner 标志和 authority reference 摘要。

共享制品不得包含本地绝对路径。

## Main checkout

常规项目内 `.git/` 仍遵循 ADR-0004：有界扫描、无链接/特殊节点、无 include/worktree redirect/alternate/graft/replace/filter/helper，并通过收紧环境中的显式 Git 查询交叉核对 root、git-dir、common-dir、HEAD/ref 和 status。

## Linked Worktree

根 `.git` 文件是仓库控制输入。适配器可以有界解析 pointer，但在读取外部目标前必须从仓库外获得：

1. exact expected admin git-dir；
2. exact expected common Git dir；
3. 非空 authority reference；
4. integration owner 判断。

随后必须验证：admin 位于 common `.git/worktrees/<id>`；`commondir` 精确回到 common dir；admin `gitdir` 精确反向指向当前 `.git`；Worktree registry、top-level、HEAD/ref 一致；所有相关 metadata 常规、有界、无危险 indirection。

缺少 authority 时返回阻塞说明，不得为了“发现正确路径”而访问 pointer 目标。不能从 Qoder Worktree 名、分支前缀或猜测的磁盘目录推断授权。

## 新鲜度

以下任一变化使本地规划/证据失效：

- checkout root/admin/common identity；
- HEAD/ref/baseline 或 dirty digest；
- move、rebase、merge、切换 checkout；
- source、OpenSpec/config、Qoder 本地配置；
- Worktree 结果被集成到另一个 checkout。

Worktree 内结果只属于该 checkout。合并后必须在 integration checkout 重新 seal/验证。Worktree 不隔离端口、数据库、缓存、浏览器 profile、远程服务或共享 Git refs/object store；这些隔离要求写入 Delivery Contract。

## 停止条件

缺少外部 authority、路径/反向指针不匹配、stale Worktree、嵌套仓库、未知 dirty、metadata/config 风险、链接/特殊节点、扫描/输出超限、Git executable 不可信或身份在操作中变化。报告精确 blocker，不自动修复、prune 或移动。
