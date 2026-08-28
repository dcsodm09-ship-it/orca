# 《Orca + Claude 多 Agent 系统教程》独立只读复核（Codex gpt-5.6-sol / max）

## 结论先行

**当前版本不建议直接作为“已会基本操作 Orca 的读者”的高级实操手册发布；建议先做一轮重点修订。** 大多数示例命令的 flag 拼写与 Orca App 1.4.187 当前 CLI 一致，但文档对这些命令的生命周期语义讲得不够准确，照抄后最容易在 `check` 收件、worker 回收、worktree 基线、失败恢复和最终验收上出错。

我给出的文档级判定是：

- **命令拼写层面：大体通过。** `status`、`repo add`、`repo set-base-ref`、`worktree create`、`run-create`、`task-create`、`worker-start`、`check --wait`、`task-list --ready`、`worker-show`、`terminal wait --for tui-idle` 等命令/flag 均能在当前版本指南或 `--help` 中找到。
- **流程正确性层面：NO-GO，需修后再发布。** 目前至少有 5 个会实际误导执行的 P1 文档问题，尤其是把一次 `check --wait` 写成“统一等完”、没有写 Delivery 的 `--ack` 循环、没有覆盖 `agent_prompt_stalled` 与 App 重启后的失联恢复、混淆 Orca lineage 与 Git 基线、以及把“P0/P1=0”当成完整发布验收。
- **本轮指定的真实事故必须补进教程。** 它不是容量脚本“红灯”已有说明的重复，而是独立的控制面/完成信号链路故障；没有恢复章，读者会把“派发回执失败”误判成“worker 没干活”，或在 worker 已经工作时重复派发，造成双写和重复劳动。

## 复核边界与证据

- 原教程：`reports/GUIDE-orca-new-large-project-from-zero-2026-08-22.md`
- 冻结时 SHA-256：`52b6b32f631df407f9b6e211aaa395994dcbcbc1fa97d4ff9a09df5e28b189f7`
- 冻结时状态：246 行、19,981 bytes，Git 状态为未跟踪文件（`??`），所以本次只能以 SHA-256 而不是 commit 标识候选。
- 实测运行时：Orca App `1.4.187`，`orca status --json` 返回 `runtime.state=ready`；本轮运行时 ID 为 `253d2786-0bb0-4fb0-8e6d-2db1f88c19d8`。
- 已完整读取当前二进制返回的 `orca skills get orca-cli` 与 `orca skills get orchestration`，并以 `--help` 只读核对主要命令。
- 已只读运行并验证为合法 JSON：`orca status --json`、`orca worktree ps --json`、`orca worktree current --json`；没有运行任何创建/删除 worktree、启动 worker、重新派发、发送终端按键或发布 Artifact 的实测。
- 已运行容量脚本。2026-08-22 本轮结果是：顶层 `recommendation.gate=gate_removed`、`new_workers_default=2`、`new_workers_max=3`；同时 `advisory_true_recommendation.gate=red`、建议值为 0。也就是“硬门禁已移除，但真实负载参考为 red”。
- 当前 Dispatch 的 `worker-show` 还实证了：当前 Orca 能以 `worker-start` 有效启动 `codex / gpt-5.6-sol / max`，回执为 `state=ready`、`stage=input_accepted`；`worker-show` 提供的是有界 `terminal.preview` 和 `observation.agentWait`，完整/分页输出应走 `worker-read`。
- 本轮任务书明确给出的现场事实：协调者真实遇到一次提示词停在输入框、未提交的 `agent_prompt_stalled`，需要发送空文本加 Enter 才推进；高负载下 Orca App 又真实崩溃重启一次，旧 Dispatch capability 被撤销，`worker_done` 链路失效，只能退回终端状态与持久化产物核验。
- 工作区已有的 `reports/ORCA-ORCHESTRATION-DISPATCH-RELIABILITY-FIX-2026-08-21.md` 第 15、30–34、40、50、54、77、82–90 行，与本轮事故相互印证：`agent_prompt_stalled` 不只可能代表“没有输入”，也可能是忙碌/遥测判断的假阴性；一旦 capability 被吊销，后续 heartbeat / ask / `worker_done` 会被拒收。该报告同时明确说候选修复当时并未安装到正式 Orca.app，因此不能把已提交源码修复当成当前正式 App 已修好。

## Orca 命令逐项核对表

| 原文位置 | 命令/flag | 当前 1.4.187 核对 | 结论与所需修改 |
|---|---|---|---|
| 72 | `orca status --json` | 已实跑，exit 0、JSON 可解析、App/runtime ready | 正确；建议教读者从 `result.runtime.appVersion` 取版本 |
| 72 | `orca open --json` | 当前指南/总帮助存在；因 App 已运行且会启动应用，本轮未实跑 | 语法正确，保留为 status 失败后的条件恢复 |
| 73/169 | `orca worktree ps --json` | 已实跑，exit 0、JSON 可解析 | 正确；只是当前活动摘要，不能单独证明“无人做同一任务” |
| 82 | `orca repo add --path ... --json` | `--help` 确认 | 语法正确；它注册已有 Git 路径，不会初始化仓库 |
| 83 | `orca repo set-base-ref --repo id:... --ref origin/main --json` | `--help` 确认 | 语法正确；必须先确认 `origin/main` 真实存在 |
| 102–106 | `orca worktree create --repo ... --agent claude --prompt ... --setup run --json` | `--help` 确认全部 flags | 语法正确；缺 lineage/base 决策、回执检查和骨架确认门说明 |
| 116 | `task-create --deps` | `--help` 确认；值类型为 JSON array | 说法正确但不可照抄；需给 `--deps '["task_a"]'` 示例 |
| 125/197 | `orca orchestration run-create --objective ... --json` | `--help` 确认 | 正确；需说明它绑定协调者终端，Run 本身不调度 |
| 127–128/199–200 | `task-create --spec ... --json` | `--help` 确认 | 正确；大型项目还应使用 title、deps 和完整任务合同 |
| 130–131/202–203 | `worker-start --task ... --worktree new-child --name ... --agent claude --json` | `--help` 确认 | 语法正确；建议显式 `--setup run`，并先证明当前 parent/base 正确；必须读取 start receipt |
| 133/205 | `check --wait --types ... --timeout-ms ... --json` | `--help` 确认 | 语法正确、流程严重不完整；必须补 Delivery 全批处理、release/reuse、ack 和滚动等待 |
| 138 | `worker-start --terminal <handle>` | `--help` 显示仍必需 `--task` | 原文是不可执行简写；改成 `worker-start --task <nextTaskId> --terminal <handle> --json` |
| 138 | `worker-release` | `--help` 显示必需 `--dispatch` | 原文是不可执行简写；改成 `worker-release --dispatch <dispatchId> --json`，且只用于 settled worker |
| 141 | `python3 .../agent_capacity.py` | 已实跑 | 命令正确；本轮真实输出是 `gate_removed` + advisory red，后文必须同步 |
| 172 | `worker-show` | 已对本轮 Dispatch 实跑 | 命令正确；“原始终端预览”不准确，完整输出用 `worker-read` |
| 219 | `task-list --ready` | `--help` 确认；本轮 worker 终端因未绑定 Run 实跑得到 `run_required` | flag 正确；应明确由绑定 Run 的协调者运行，推荐加 `--brief --json` |
| 241–243 | `worktree create --no-parent --agent grok --prompt ... --json` | `worktree create --help` 与版本指南确认 `grok` 为已知 agent id | 语法正确；缺 `--setup run`，且默认 base 可能不是待审候选；未实测网络认证/启动 |

本轮还确认 `orca terminal send --terminal <handle> --text "" --enter --json`、`terminal wait --for tui-idle --timeout-ms ... --json`、`worker-read --dispatch ... --source auto --limit ... --json` 都是当前 parser 支持的语法；为了保持只读，没有向任何活终端实发按键，也没有以失败 Dispatch 复演事故。

## P1：发布前必须修的文档问题

### P1-1：一次 `check --wait` 不会“统一等完”所有 worker

涉及原文第 122–134、195–207 行。

当前写法只运行一次：

```bash
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 1800000 --json
```

这条命令语法正确，但流程解释错误。当前版本的真实语义是：

1. `check --wait` 返回一个**有界、FIFO、可重放的 Delivery**，不是“直到全部 worker 完成为止”。
2. 在 `--ack <deliveryId>` 之前，同一批会反复返回。
3. 一批里可能同时有多条消息，必须全部处理；不能只看第一条 `worker_done`。
4. 每个已接受的 `worker_done` 都要先决定该终端是立即复用、`worker-retain`，还是 `worker-release --dispatch <dispatchId>`；然后才能 ack 整批。
5. `--timeout-ms` 到时或返回 0 条只是检查点，不等于 worker 失败。
6. `check --wait --json` 每 15 秒会向 stderr 发 `_keepalive`；如果脚本把 stdout/stderr 合并，必须过滤 keepalive，不能把合并流当成一个普通 JSON 文档。

建议把阶段 4 的示例改成明确循环，而不是一次等待：

```bash
orca orchestration check --wait \
  --types worker_done,escalation,question \
  --timeout-ms 900000 --json

# 处理返回 Delivery 中的每一条消息；对每个已接受且不复用的完成项：
orca orchestration worker-release --dispatch <dispatchId> --json

# 完成整批处理后，确认上一批并继续等下一批：
orca orchestration check --ack <deliveryId> --wait \
  --types worker_done,escalation,question \
  --timeout-ms 900000 --json
```

应补一句：“一直循环到所有**预期 Dispatch** 都 settled；不是循环固定次数。”

### P1-2：必须新增 `agent_prompt_stalled`、capability 撤销、App 崩溃重启恢复章

涉及原文“附：大项目专属的坑”；当前表格只有容量红灯和 Trusted Access，没有控制面失联的恢复流程。

**建议新增，而且应放在阶段 4 后面作为高优先级运行手册，不应只在表格里加一句。** 原因：

- 容量 red 是派发前的态势信号；`agent_prompt_stalled` 是已经进入派发输入阶段后的提交/观测故障，两者不是一回事。
- App 崩溃重启会改变 runtime epoch，终端 handle 是 runtime-scoped；旧 handle 可能 stale，必须重新发现。
- capability 撤销后，worker 真实完成也可能无法发送 `worker_done`。这时“没有完成信号”不能直接推出“没有完成工作”。
- 反过来，`tui-idle` 也只表示 TUI 回到可接收输入状态，**不是验收成功**；必须读完整输出、报告文件、测试结果和候选身份。

本报告后面给出一段可直接加入教程的完整草稿。

### P1-3：worktree 隔离、Orca lineage、Git 基线和合并策略被混为一谈

涉及原文第 102–106、112–143、179–207、241–243 行。

具体问题：

- `worktree create` 默认会在能推断当前上下文时记录父子 lineage。阶段 2 没有写 `--no-parent` 或显式 `--parent-worktree`，同一条命令从不同终端执行可能得到不同 Orca 层级。
- `--no-parent` 只影响 Orca UI/lineage，**不选择 Git 基线**。独立 top-level worktree 默认用 repo 的默认 base；它不会自动看到当前 feature worktree 的未提交改动。
- `worker-start --worktree new-child` 的语法正确，但示例没有先确认协调者当前就在已经提交骨架的 worktree。若协调者还在另一个 worktree，两个 worker 可能从错误基线启动。
- “会碰同一批文件的任务必须分到不同 worktree”只解决并发写同一目录的文件系统冲突，不能解决后续 Git merge conflict；它还与第 116 行“会改同一批文件就不应并行”前后矛盾。正确规则应是：同文件任务优先串行；只有确需并行时才用独立 worktree 隔离编辑，并提前指定单一集成 owner/顺序。
- `worker_done` 不代表“修改已提交”“测试已过”“可以合并”。Orca 不会自动把 child worktree 合并到父分支。
- 第 207 行让两个分支依次直接合到 `main` 风险过大。应该先进专用 integration branch/worktree，冻结合并后的候选，再跑整体验收，最后由有权限的人决定是否进 main。
- 第五部分的 Grok `--no-parent` 示例如果用于审查当前未提交候选，会从默认基线启动，极可能审错代码。若只是审文字争议可以独立 top-level；若审当前代码，必须传精确 commit/ref，或在当前 worktree 新开只读 agent 终端。

建议在骨架提交后加一个明确停点：

```bash
git status --short
git rev-parse HEAD
orca worktree current --json
```

记录骨架 commit、完整 worktree id 和预期 base；只有三者一致才启动 child workers。未提交改动不会神奇地出现在新 worktree 中。

### P1-4：容量说明前后直接矛盾，100 任务章节又给了静态并发数字

涉及原文第 139–144、213–226 行。

- 第 143 行说容量脚本 advisory-only、不硬拦截，这是当前脚本的真实行为。
- 第 216 行又说“只有 2 个任务就因为容量红灯被挡住、禁止启动新 worker”，这是旧硬门禁语义，与当前行为冲突。
- 第 219 行建议“比如 5–10 个”并发，但本轮实时 advisory 是 red；固定 5–10 反而会绕过文档自己要求的逐波容量判断。

建议统一改成：

> 容量脚本当前不执行硬拦截。每波读取顶层操作默认值和 `advisory_true_recommendation`；red 时主动降低到 0 或只保留协调者，等现有 worker settled/released 后再评估。教程不提供跨机器通用的固定并发数。

“100 个任务”应该描述成协调者补位循环：先创建任务清单；只启动本波准入数量；每收到并处理一个 settled Dispatch 后释放/复用资源、重新读容量、再从 `task-list --ready --brief --json` 补一个。不要写成“100 个同时拉起”，也不要暗示 Orca orchestration 自带自动调度器。

### P1-5：“P0/P1=0 才能发布”不是完整验收门

涉及原文第 148–160 行。

P0/P1 清零只是必要条件，不是充分条件。对大型项目至少还要补：

1. 冻结精确候选：commit/tree/hash、dirty/untracked 状态；复核期间若漂移，旧结论失效。
2. 在**合并后的候选**上运行测试、lint、typecheck、build（仅在获授权时）、迁移检查和真实边界/E2E；记录确切命令、退出码和未跑项目。
3. 区分静态检查、隔离测试、演示 UI、真实生产 E2E，不把前者写成上线能力。
4. 核对 schema migration、备份、回滚、监控、权限/密钥、第三方 API 授权和数据合规。
5. 所有预期 Task/Dispatch 已 settled；所有 question/escalation 已处理；所有 Delivery 已 ack；每个 worker 已复用、retain 或 release。
6. 最终发布/安装/部署仍需要明确的人类授权；复核 worker 的 GO 不等于授权。

建议把阶段 6 改成一张可勾选的候选验收表，而不是两条口号。

## P2：准确性、过时内容和内部矛盾

### P2-1：目标读者定位不一致，目录漏了第五部分

- 第 3 行写“给小白的操作手册”，本次指定读者却是已经会 Orca 基本操作的人。建议改成“面向已掌握 repo/worktree/terminal 基础操作的进阶运行手册”，删减百科式层级罗列，把篇幅让给生命周期、故障恢复和验收。
- 目录只列第一至第四部分，正文有第五部分 Grok。补目录，或将 Grok 降为可选附录。

### P2-2：把 session 私有能力写成 Orca 的通用系统层

涉及第 26–33、39–50 行。

- `codex` CLI 并不存在通用的“bulk/design/fast/work 四档”这一 Agent CLI 概念；这些更像某个 Claude session 的路由别名/技能，不应写成 Codex CLI 本身属性。
- 本轮 `orca skills installed --json` 的安全发现列表中没有 `codex-bulk`、`codex-design`、`codex-fast`、`codex-work`、`artifact-design` 或 `artifact-capabilities` 这些精确名称。它们即使存在于协调者的某个 session，也应标成“可选、会话相关扩展”，并要求读者先发现，不能当安装必备层。
- `codex-pool`、`r2-memory`、`desktop-control` 不是当前版本匹配的 Orca 核心 CLI 表面。不要把某次会话加载的 MCP server 写成所有 Orca 用户都有的系统层。
- 第 31 行说跨模型必须走 Run/Task/Dispatch，第 43 行又建议用“不走完整 orchestration”的 `codex-pool`，内部冲突。若本机治理规则要求唯一 Run/Task/Dispatch，就必须统一走监督编排；否则明确写成另一种 full handoff，并说明没有 `worker_done` 追踪。
- “能读写记忆文件”也是权限/治理行为，不是所有 agent 的平台固有能力；写回记忆应服从用户授权和当前技能规则。

建议把“系统全景”拆成两张表：

- **Orca 核心且当前可发现：** project/repo/worktree/terminal/browser/automation/artifact/skills/orchestration。
- **本机或本 session 的可选扩展：** 逐项写发现命令、是否已加载、是否有用户授权；不要放进核心架构图。

### P2-3：`repo` 不等于 Orca 的全部“项目”概念

第 61 行把 repo 定义成 Orca 里的项目，一个 Git 仓库。当前 CLI 已同时有 durable `project`、project host setup、Git repo 和 folder workspace 概念。对教程主线可以继续用 Git repo，但名词表应说清：这里选择的是“本地 Git repo 路径”，不是 Orca 所有项目形态。

“Orca CLI 没有从零建仓命令”可保留，但应改得更精确：当前没有 `repo init`；`project setup-create` 只建 host setup 元数据，不能代替 `git init`。

### P2-4：全新仓库示例会在空目录和错误默认分支上失败

涉及第 86–93 行。

- 空目录执行 `git add -A` 后没有可提交内容，`git commit -m init` 会失败。
- `git init` 的默认分支未必叫 `main`；后面硬设 `origin/main` 可能不存在。
- 没有远程时根本不该写 `origin/main`。
- 没提醒检查 Git identity、remote 和首个 commit。

建议示例至少改为：

```bash
git init -b main
# 创建 README、.gitignore、许可证或最小骨架，保证确实有首个候选
git add -A
git diff --cached --check
git commit -m "chore: initialize project"
git rev-parse HEAD
orca repo add --path /abs/path/to/new-project --json
```

只有经 `git ls-remote --heads origin main` 或 `orca repo search-refs` 确认远程 ref 存在时，才设 `origin/main`；纯本地仓库可用 `main`。

### P2-5：`worker-start`/`worker-release` 的简写不够可执行

第 138 行写“`worker-start --terminal <handle>` 复用”与“`worker-release` 释放”，缺少必需参数。应给完整命令：

```bash
orca orchestration worker-start --task <nextTaskId> --terminal <handle> --json
orca orchestration worker-release --dispatch <settledDispatchId> --json
```

复用前应从 `worker-show --dispatch <oldDispatchId> --json` 的 `worker.agent_terminal_handle` 取 handle，并在 ack Delivery 之前完成所有权转移。只有 settled 的监督 worker 才 release；timeout、heartbeat、question、escalation 或单独的 `tui-idle` 都不构成 release 条件。

### P2-6：把“Bash 会静默截断 JSON”写成了错误根因

第 170 行过度概括。Bash 管道本身不会无条件静默截断 `orca --json`；常见问题是 agent 工具的显示上限、命令替换后再次作为 argv 引发大小限制，或 `terminal read` 本来就是有界分页。

建议改成：

> 不要根据终端 UI 中的折叠/尾部预览断言 JSON 完整。直接把 stdout 送入 `jq -e` 或由 `subprocess.run(..., check=True, capture_output=True)` 解析；分页接口按 cursor 读到 `limited=false`。`check --wait` 的 keepalive 在 stderr，合并流时单独过滤。

### P2-7：Trusted Access 故障说明使用了不精确字段

第 172 行说看 `worker-show` 的“原始终端预览”。当前 `worker-show` 返回的是有界 `terminal.preview`，并可能返回 `observation.agentWait`；`agentWait=null` 表示 Orca 看过且未发现等待，字段缺失则表示没能观察，不能解释成“肯定没有等待”。需要完整输出时用：

```bash
orca orchestration worker-read --dispatch <dispatchId> --source auto --limit 50 --json
```

若 source 返回 cursor，继续按 cursor 分页；不要猜 provider session ID 或 transcript 路径。

### P2-8：复核模型/effort 的文字自相矛盾

第 152 行同时写 `gpt-5.6-sol + max` 和“池子上限 `xhigh`，按上限派”。这两个 effort 不是同一个值。当前本轮 `worker-show` 已确认经 Orca `worker-start` 请求和生效的都是 `max`；如果另一个 `codex-pool` 通道只支持 `xhigh`，就必须按通道分别写，不能合并成一句。

建议给出当前 Orca 监督路径的可执行模板，并提醒先看回执中的 `launch.requested/effective`：

```bash
orca orchestration worker-start --task <reviewTaskId> --worktree current \
  --agent codex --model gpt-5.6-sol --effort max --json
```

`--effort` 必须配 `--model`，且二者不能和 `--terminal` 同用。模型名/effort 是可漂移配置，教程应标注核验日期，不应称永久规则。

### P2-9：Workflow 数字和路由事故不属于 Orca CLI 教程的已验证事实

第 171、217、219、222、226 行的 `agentType: codex-design`、haiku 静默路由、`min(16, CPU-2)`、总数 1000、默认 15、`pipeline()`、`budget.total` 等，本轮无法从当前 Orca CLI/version-matched skill 验证；它们显然来自另一套 Workflow 工具。

建议移到“协调者专属 Workflow 附录”，每项注明工具版本、来源和验证日期；若无法给来源就删除。不要用这些数字解释 Orca orchestration 的容量，后者明确“不自动调度”。

### P2-10：Grok 的“零门槛/已认证”表述过度

本轮只读确认 `grok` 可执行文件存在、`~/.grok/auth.json` 文件存在，且当前 Orca 指南列出 `grok` 为已知 agent id；没有进行网络认证或真实启动，因此只能说“安装痕迹和凭据文件存在，语法受支持”，不能说“零门槛”或“认证有效”。

同时，示例应补 `--setup run`，并按任务性质选择基线：

- 纯观点 handoff：`--no-parent` 可以，但应在 prompt 中带完整材料，不要事后监控。
- 监督审查：建 Run/Task 后用 `worker-start`，等待 `worker_done`。
- 审当前候选：冻结 commit/hash 并确保新 worktree 基于该 ref；如果候选含未提交文件，就不能用默认 top-level worktree 假装审到它。

### P2-11：产品例子夹带未经证明的业务/合规假设

第 181 行说抖音接口“可以直接复用已经验证过的 MoreAPI 路由”，但本文没有给候选版本、权限范围、证据或失效日期。接口能调用也不等于账号获批，更不等于私信自动回复、线索抓取、CRM 导出符合平台条款和数据合规。

建议删掉这个产品特定断言，或改成“仅在重新核对当前官方权限、账号授权、数据最小化和 E2E 后复用”。通用架构教程不要把旧项目经验写成当前生产授权。

### P2-12：还有几处小的前后不一致/重复

- 第 5 行说 `ORCA_CONTEXT_NACK_V1`，第 33 行又说收到了 `ORCA_AGENT_MEMORY_CONTEXT_PACK_V1`。两者可能分别代表项目上下文桥失败和本地/另一层记忆注入，并非必然冲突，但文档应解释来源边界，否则读者会以为前面说“没加载”、后面又说“已经注入”。
- 阶段 2 的 `plan-and-skeleton` prompt 已要求“骨架代码搭好”，阶段 3 又像是另起步骤。应明确阶段 3 是继续使用同一 worktree，还是人工确认后重新派发下一轮；不能让 agent 因第一条 prompt 自己越过确认门。
- “所有独立 Task 一次建完、一次性拉起全部 worker”和第四部分“分批启动”冲突。统一成“Task 可先建全；worker 只按本波容量启动”。
- 第 221 行称冲突成本“指数级”没有量化依据，改成“快速上升/常呈超线性”更准确。

## 目标读者真正会卡住、但目前没覆盖的内容

建议新增以下操作性内容，优先级从高到低：

1. **每个 session 先加载版本匹配指南。** `orca skills get orca-cli`；涉及监督编排再读 `orca skills get orchestration`。不要从旧教程猜 flags。App 版本从 `orca status --json` 的 `result.runtime.appVersion` 读；本轮实测 `orca --version` 只打印总帮助，不是可靠版本命令。
2. **CLI 选择规则。** 当前 Mac/Orca managed terminal 用 `orca`；WSL 先尊重 `ORCA_CLI_COMMAND`；dev checkout 可能是 `orca-dev`；Linux 非 Orca 终端用 `orca-ide`，避免误跑 GNOME 屏幕阅读器。选定后全程不换二进制。
3. **Run 绑定与角色边界。** `run-create` 会把协调者终端绑定到 Run；普通 worker 不需要也不应自行 `task-list` 协调整个 Run。本轮从 worker 终端运行 `task-list` 得到 `run_required`，说明这条角色边界值得写明。
4. **Task spec 模板。** 至少写：精确候选/基线、允许与禁止修改的路径、依赖、验收命令、交付物路径、是否允许网络/安装/部署、完成回执要求。只写功能名不足以隔离大型项目。
5. **setup policy。** `--setup run` 只会执行 repo 已配置的 setup；默认 start-immediately 会让 setup 和 agent 并行，只有 repo 配置 `wait-for-setup` 才会先等 setup 成功。必须读 `worker-start` 回执的 `setup/effects/residualResources`，不能把 `ready + setup=running` 误判成失败，也不能把 setup 失败后残留资源忽略。
6. **依赖 DAG 语法。** 给一个真实例子，例如 `--deps '["task_id_a","task_id_b"]'`；说明依赖完成前任务不会 ready，并建议用 `task-list --ready --brief --json` 做协调者外部记忆。
7. **worker 启动回执门。** 只有命令 exit 0 且回执 `state=ready`、`stage=input_accepted` 才表示这次启动完成。`failed` 或 `outcome_unknown` 要读 `stage/effects/residualResources/recovery`，不要盲重试。
8. **heartbeat / ask / escalation / worker_done 合同。** worker 必须使用注入的 taskId、dispatchId、capability，从自己的终端发送一次且仅一次带明确 `outcome` 的 `worker_done`；阻塞问题用 `ask`，需协调者动作才用 escalation。heartbeat 只是存活，不是完成。
9. **终端 handle 恢复。** handle 是 runtime-scoped；遇到 `terminal_handle_stale` 或 App 重启后用 `terminal list --worktree <exact selector> --json` 重新拿，只用新 handle，禁止新旧双发。
10. **集成 owner 与 merge train。** 每个 worker 输出 commit/变更清单/测试证据；由单一集成 owner 顺序合并到 integration branch，解决冲突并运行合并后测试；再冻结最终候选做终审。
11. **失败重试 lineage。** 只有证明旧 worker failed/stopped 且没有继续编辑，才用 `worker-start --task <taskId> --retry-of <oldDispatchId> ...` 启动替代；不要另建同内容 Task 丢失 lineage，也不要在旧 worker 可能仍活着时双编辑。
12. **跨机器可选附录。** 若要用 `--on <saved-environment>`，remote `current` 和 `new-child` 是无效的；要用精确远端 worktree selector，或 `new-top-level` 加明确 remote repo。后续按 dispatchId 路由，不要重复 `--on`。

## 建议直接加入教程的故障恢复章节草稿

### 已知坑：任务提示停在输入框、App 重启、`worker_done` 失联

这是一组控制面故障，不等同于 worker 业务代码失败。先保住“只允许一个 editor”原则，再判断任务是否真实启动/完成。

#### A. 派发前和启动回执

复用现有 agent 终端前先确认它真正空闲：

```bash
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
```

正常监督任务优先用 `worker-start`，并读取完整 JSON 回执。只有 exit 0、`state=ready`、`stage=input_accepted` 才算这次启动已确认。若返回 `agent_prompt_stalled`、`failed` 或 `outcome_unknown`，先观察，禁止立即把同一任务再派一遍。

#### B. 提示词已经在输入框里，但没有提交

先查 Dispatch 和终端，不要重发全文：

```bash
orca orchestration worker-show --dispatch <dispatchId> --json
orca orchestration worker-read --dispatch <dispatchId> --source auto --limit 50 --json
orca terminal show --terminal <handle> --json
```

只有在可见证据确认“完整任务文本已经在输入框中、尚未提交、worker 也没有开始执行”时，才发送一次空文本加 Enter：

```bash
orca terminal send --terminal <handle> --text "" --enter --json
```

不要再发送一次任务正文。发送后先观察到新的输出/working 转移，再进入等待；因为发送前的 TUI 本来就可能是 idle，不能把那个初始 idle 误认为任务完成。

#### C. 之后出现 capability revoked / `worker_done` 被拒收

capability 已撤销时，不要让 worker 循环重试 heartbeat 或 `worker_done`，也不要伪造新 capability。记录原 taskId、dispatchId、报错、终端和候选路径；保持该 worker 为唯一 editor。此时生命周期信号失败，不自动证明工作失败。

用滚动窗口等待并读取终端：

```bash
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca terminal read --terminal <handle> --json
```

`tui-idle` 只表示这一轮交互回到提示符。协调者还必须核对完整报告、文件哈希、测试退出码、候选 commit/dirty 状态。若证据完整，可把这是一次“completion channel recovery”写入 Run 的审计结果；只有明确的人工恢复/override 才能用 `orca orchestration task-update --id <taskId> --status completed --result '<audit-json>' --json`。这个 override 不会制造 `worker_done`，也不应改写旧 Dispatch 的失败史。若证据不完整，则任务保持 failed/outcome_unknown，派一个冲突隔离的**验证或替代** Dispatch；旧 worker 仍可能写文件时禁止启动第二个 editor。

#### D. Orca App 崩溃重启

先重新建立运行时事实，不要继续使用缓存 handle：

```bash
orca status --json
orca worktree current --json
orca terminal list --worktree id:<fullRepoId>::<absoluteWorktreePath> --json
```

对比新的 `_meta.runtimeId`，从 `terminal list` 取替代 handle；只用新 handle，不向旧、新两个 handle 同时发送。随后检查旧任务/Dispatch：

```bash
orca orchestration worker-show --dispatch <oldDispatchId> --json
orca orchestration dispatch-show --task <taskId> --json
```

如果新协调者终端没有绑定 Run，先用明确的 runId 执行 `orca orchestration run-use --id <runId> --json`。若运行时返回 legacy/recovery 的类型化 next step，原样执行它给出的参数；不要凭记忆改写成当前 mutation。

终端仍在时，用新 handle 走 `tui-idle` + `terminal read` 的有界恢复，但仍按上一节核验持久化产物。终端消失、输出不完整或候选身份不明时，不能补写 GO；应把原 Dispatch 记为 outcome unknown/failed，再用 `--retry-of` 或独立验证任务恢复。不要因为 timeout、idle 或 capability 故障直接 `terminal close` / `worker-release`。

#### E. 何时恢复正常监督链路

只有在新的 Dispatch 获得新的有效 capability、启动回执为 `input_accepted` 后，才重新依赖 heartbeat / ask / `worker_done`。旧 Dispatch 的 ID/capability 不可复用。对本次已靠终端/产物恢复的结果，要在最终报告中明确写成“控制面完成信号失联，协调者人工核验”，不要冒充正常端到端回执。

## 推荐的整体重排

面向“已会 Orca 基本操作”的读者，建议正文重排成：

1. 适用范围、当前 App 版本与 CLI 发现方法。
2. 三个容易混淆的边界：Orca lineage / Git base / orchestration lifecycle。
3. 大项目开工门：仓库初始化、setup policy、骨架 commit、候选冻结。
4. Task DAG 与容量准入：先建任务、分波启动。
5. 监督循环：`worker-start` 回执 → heartbeat/ask → Delivery 全批处理 → ack → reuse/release。
6. 集成与候选验收：integration owner、合并后测试、独立终审。
7. 故障恢复：Trusted Access、`agent_prompt_stalled`、capability revoked、App restart、stale handle、outcome unknown。
8. 100 个任务的补位队列范式。
9. 可选模型/Grok/跨机器附录；所有 session 私有 MCP/Workflow 内容移到这里并注明发现方式。

这样会比目前“六层百科 + 大量当前 session 私有能力 + 一条理想路径”更符合目标读者，也更能避免真实现场事故。

## 修改优先级清单

发布前最小修订集：

- [ ] 补全 `check --ack` / 多 Delivery 循环与 `worker-release --dispatch` 完整命令。
- [ ] 加入本文的 `agent_prompt_stalled` / App crash / capability revoked 恢复章，并明确 `tui-idle != 完成/验收`。
- [ ] 写清 lineage 与 Git base 不同、child 不继承未提交改动、需要 integration owner。
- [ ] 统一容量为“gate_removed + advisory red”，删除“红灯强制禁止”和静态 5–10 并发。
- [ ] 把阶段 6 扩成候选身份、测试/E2E、回滚、权限与全部 Dispatch settled 的验收表。
- [ ] 修正空仓库初始化、远程 ref 检查和 `worker-start --terminal` / `worker-release` 必需参数。
- [ ] 移除或降级 session 私有 MCP/Workflow/skill 名称与未验证硬数字。
- [ ] 修正 `gpt-5.6-sol + max` 与“xhigh 上限”的冲突，按真实启动通道分别写。
- [ ] 修正 Bash 截断、Trusted Access preview、Grok 零门槛、MoreAPI 已验证等过度断言。
- [ ] 统一目标读者，补目录第五部分，并消除“全拉起”与“分波启动”的矛盾。

完成以上最小集后，这份文档才适合从“会看懂的经验总结”升级为“可照着执行、出了故障也知道如何收敛”的高级教程。
