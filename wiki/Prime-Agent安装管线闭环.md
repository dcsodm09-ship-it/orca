# Prime Agent 安装管线：TOCTOU 修复与双复核闭环

## 现状

`prime-agent-integration/install_prime_agent.py` 已真实安装并在本机上线（2026-08-22）：

- 安装位置：`/Volumes/Extreme SSD/Orca/local-homes/.shared-tools/prime-agent/releases/v0.7.2/`
- 命令入口：`~/.local/bin/prime-agent`（软链接，`enable` 前禁用）
- 状态目录软链接：`~/.prime`
- 操作序列：`plan` → `install` → `verify` → `enable` → `audit`，全部 `ok:true`；
  `production_lock_sha256` 与独立复核的锁定值逐字符一致；`audit` 仅报告白名单内的
  `extract-zip`/`GHSA-jmr9-qjv8-65gv` 一条 advisory。

## 背景：round 58-65 修复—双复核循环

`audit_installed_lock()`/`verify()`/`write_pending_install()` 围绕"同 UID 攻击者能否在
校验通过后、`npm audit` 真正读取/执行文件内容之前完成篡改"这一 TOCTOU 问题，经历了 8 轮
连续的修复→双复核循环（每一轮的独立复核都在上一轮自己的修复里发现了新的真实缺口），直到
round 64 才达成两路（Claude opus/max + Codex sol/max）各自独立、都报告 0 P0/P1 的**真正
双 GO**，round 65 收尾清理后完成真实安装。完整逐轮记录见仓库内
`reports/COMPLETION-BACKLOG-2026-08-16.md` 第 15-24 节。

## 可复用的知识模式

1. **双 GO 的定义必须严格**：只有两路复核都**独立、真正完成**且各自报告 0 个可复现
   P0/P1，才算数。一路完成、另一路因基础设施故障（Trusted Access 墙、`operator_close`
   终端死亡、`consumer_fenced`、会话用量限额、或编排子 agent 提前返回占位文本）未完成，
   都不能被当作"默认通过"处理——必须补跑，或从 Orca 的任务记录里直接取回真实结果。
2. **变异测试证明"承重"**：任何新增的回归测试都要在手工变异过的代码副本上验证它确实
   会失败（且失败信号能定位到目标检查，不是巧合失败），再确认它在真实代码上通过。仅仅
   "写了断言、跑了绿"不足以证明测试真的覆盖了目标缺陷。
3. **TOCTOU 加固的分层**：(a) 早期捕获的值必须绑定到安装期就已固定、`verify()` 自己会
   核对的持久 receipt 字段，而不能只跟自己前后比较（否则持久性伪造无需任何时序竞争即可
   绕过）；(b) 逐文件的紧凑括号（捕获→子进程前复核→子进程后复核）能把窗口压到毫秒级，
   但只覆盖被显式列出的文件；(c) 整树/子树级别的 `tree_digest()` 遍历提供的是同等强度但
   更宽松（几百毫秒级）的兜底覆盖；(d) 路径校验与 `subprocess.run()` 实际 exec 之间存在
   结构性 gap——检查的是路径内容，`subprocess.run()` 独立重新解析同一路径去执行，两者不
   绑定到同一个文件描述符。Darwin 上没有可从 Python 触达的 `fexecve(2)` 等价物（round 47
   `validate_exec_target()` 已用真实 fork/exec 双路径证实），这是平台级限制，只能收窄、
   不能完全消除。
4. **文档准确性会持续腐化**：本文件历史上至少 5 次出现"把某段代码/某个 bug 错误归因到
   前一轮而非引入它的那一轮"，每次都被下一轮独立复核抓到——说明即使是纯文本的轮次归因，
   也值得每轮复核时专门核实（`git show <commit> | grep -c <symbol>` 这类客观核验比阅读
   记忆更可靠）。

## 相关基础设施故障模式

见 `~/.claude/projects/*/memory/reference_codex_operator_close_and_overlapping_dispatch_collision.md`：
`operator_close` 终端静默死亡、并发双复核派发的 `consumer_fenced` 冲突、Claude 会话级
用量限额中途命中、以及 Workflow 编排子 agent 提前返回"仍在等待"占位文本（真实 Codex
worker 其实几分钟后正常完成，可直接查 `orca orchestration run-list`/`task-list` 取回）。
