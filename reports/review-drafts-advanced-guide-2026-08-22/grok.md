# 独立只读复核：《Orca + Claude 多 Agent 系统教程》

- 复核者：Grok 4.6（xAI），第三方视角，不在 Claude / Codex 训练-产品体系内
- 对象：`reports/GUIDE-orca-new-large-project-from-zero-2026-08-22.md`（下文称「原文」）
- 日期：2026-08-22
- 约束：只读原文，不改原文；本文件只给可落地的补丁建议
- 读者假设（按本次任务书，不是按原文标题）：**已经会基本操作 Orca 的人**（会开项目、会跟一个 Agent 对话、大概知道 worktree 是什么），要学的是「从零建大型项目、多 Agent 并行、不踩这台机器上已经踩过的坑」
- 运行时对照：本机 Orca App **v1.4.187**（`orca status --json` 实测 `runtime.appVersion`），CLI `/Applications/Orca.app/Contents/Resources/bin/orca`，`orca skills get orca-cli` / `orca skills get orchestration` 现取
- 副作用边界：只跑了只读命令（`status` / `skills get` / `* --help` / `worktree ps|current` / `repo list` / `agent_capacity.py`）。没有 `worktree create`、没有 `repo add`、没有 `worker-start` / `dispatch` / `run-create`
- 重点：找 Claude 和 Codex 因为「自己就住在工具环境里」而容易一起看漏的盲点

---

## 0. 一句话结论

原文把「先骨架、再按独立性并行、复核攒批、容量当参考信号」这条大项目心法写对了，阶段 4 的 `run-create → task-create × N → worker-start × N → check --wait` 骨架也和 v1.4.187 的官方编排指南一致。但它还是一份**对话附录**，不是一份可交给「会基本操作 Orca 的人」独立执行的手册：命令缺关键 flag、把调用方当前 worktree 当成新项目所在地、把 Claude Code Workflow 的数字上限和 Orca orchestration 混成一套系统、目录与正文对不上，而且漏了这次复核任务自己刚又踩到的两条运行时坑（`agent_prompt_stalled` / App 高负载崩溃导致 capability 被撤销）。

不要推倒重写。改写目标是：定读者、拆两套编排系统、把每条示例补成可复制的完整命令、加一节「协调者运行时坑」。

---

## 1. 原文已经做对、改写时请保留

| 原文位置 | 为什么该留 |
|---|---|
| 阶段 0 开工自查 | 大项目最贵的错是重复开工；本机确实发生过 |
| 阶段 2–3「先里程碑文件、先一个骨架 worktree」 | 对；这是并行之前唯一可靠的地基 |
| 阶段 4「先建完全部独立 Task，再一次性拉 worker」 | 与官方 `Preferred Supervised Worker Loop` 逐字同构 |
| 「碰同一批文件必须分 worktree」+ 第三部分的物理隔离目录 | 真正不冲突的是文件系统边界，不是 agent 自觉 |
| 阶段 5 复核节奏（迭代 Claude 单路 + 收尾 Codex 终审） | 与 `~/.claude/CLAUDE.md` 2026-08-22 定稿一致 |
| 容量脚本 advisory-only、红灯要主动收敛 | 与现脚本行为一致（本次实测 `recommendation.gate=gate_removed`，`advisory_true_recommendation.gate=red`） |
| 「100 个同时活着」是错的心智模型 | 对；应升级成「队列 + 闸口」，但要把 Workflow 数字和 Orca 数字拆开（见 P0-3） |
| 第五部分「Grok 不要写成标准第三条腿，按需临时候补」 | 判断仍然对；这次任务本身就是按需临时候补的实例，不要因此改规则 |
| 坑表里的：复核小步高频、报告散落、Trusted Access、Bash JSON 截断 | 方向对，但每条都缺「这是哪一套系统的坑、命令怎么写全」（见第 3、4 节） |

---

## 2. P0：会让目标读者第一步就做错，或按字面执行会把新项目挂到错误的父 worktree 下

### P0-1 标题读者和正文读者不是同一个人

**原文：** 标题下第一句「写给小白的操作手册」。同仓库已有 `GUIDE-ai-beginner-first-time-2026-08-22.md`，并且那份已经把读者指过来：「想学怎么让多个 AI 同时干活……再去看那份更技术向的《Orca + Claude 多 Agent 系统教程》」。本次任务书的读者假设是「已经会基本操作 Orca 的人」。

**问题：** 「小白」会在第一部分被六层地图、MCP、`ORCA_CONTEXT_NACK_V1`、`ORCA_AGENT_MEMORY_CONTEXT_PACK_V1` 劝退；真正的目标读者又会把第一部分当成必读系统课，漏掉他们真正会卡住的操作细节（`--no-parent`、`--ack`、`--repo`、capability 吊销）。

**Claude/Codex 共有盲点：** 两家都默认「对面就是刚才跟我聊了三小时的那个人」，于是把会话开场诊断写进可复用手册。第三方读者打开的是一份 markdown，不是那场对话。

**建议：**

1. 删掉「写给小白」。改成：「写给已经会在 Orca 里开项目、跟一个 Agent 对话的人。不会用 Orca 的先看《写给完全没碰过 AI 的你》。」
2. 开头加 5 行适用范围：这份教的是 **Orca orchestration 多 Agent 大项目**；不是教怎么第一次打开 Orca，也不是教 Claude Code 自带的 Workflow 工具（见 P0-3）。
3. 把 `ORCA_CONTEXT_NACK_V1`、本 session 记忆注入、本 session 两个 Sol/Opus 任务被挡住，全部降为脚注或删掉。可复用手册不该依赖一场对话的开场事故。

可直接替换的题头：

```markdown
> 写给已经会基本操作 Orca 的人：怎么从零登记一个大型项目、先搭骨架、再按独立性并行、
> 以及这台机器上已经验证过的编排坑。
> 对照运行时：Orca App v1.4.187（以 `orca status --json` 的 `runtime.appVersion` 为准）。
> 命令以 `orca skills get orca-cli` / `orca skills get orchestration` 为准，不要背这份文档里的 flag。
> 完全没碰过 AI / 不会写代码的人不要从这份看起。
```

### P0-2 新项目 worktree 示例会继承「当前对话所在的父 worktree」——这是会真实污染侧边栏的命令错误

**原文阶段 2：**

```bash
orca worktree create --repo id:<repoId> --name plan-and-skeleton \
  --agent claude --prompt "..." --setup run --json
```

**原文阶段 4 / 第三部分：**

```bash
orca orchestration worker-start --task <id_M1> --worktree new-child --name m1-worker --agent claude --json
```

**实测：** 当前这份文档所在的 worktree 是

`35e82c5e-5eed-4052-b898-b0dff70ade97::/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`

`parentWorktreeId` 为 `null`，但已经挂了一长串 `childWorktreeIds`。`orca worktree create --help` 写明：从 Orca 管理的 worktree / 当前目录能推断父上下文时，**默认把新 worktree 记成当前上下文的 child**；只有 `--no-parent` 才切断谱系。`worker-start --worktree new-child` 同样是「当前上下文的孩子」，不是「新 repo 的孩子」。

**按字面执行的后果：** 人还坐在「完善orca」这场对话里，让 Agent 去登记一个全新获客网站，骨架 worktree 和两个模块 worker 都会变成 **Orca 源码仓库的子 worktree**。侧边栏谱系被污染，`--repo id:<newRepoId>` 只决定 git 检出落在哪个仓库，**不决定 Orca 父节点**。

第五部分 Grok 示例反而写了 `--no-parent`，和阶段 2 自相矛盾。

**Claude/Codex 共有盲点：** 它们发出命令时永远「已经在正确的项目里」。手册读者不是。`--no-parent` / `--repo` / `new-top-level` 对它们是细节，对读者是会不会把新项目挂错家的分界线。

**建议改成两段，不要混用：**

骨架（独立顶层项目，必须切断与当前对话 worktree 的谱系）：

```bash
orca repo add --path /abs/path/to/new-project --json
# 从 JSON 取 result.id（UUID），下文写作 <repoId>
# worktree id 的完整形态是 <repoId>::<absPath>，不要只抄 repoId

orca worktree create --repo id:<repoId> --name plan-and-skeleton \
  --no-parent \
  --agent claude --setup run \
  --prompt "先做需求拆解，产出 M0..Mn 里程碑计划，写到 reports/PLAN-xxx.md；骨架搭好后停下等确认" \
  --json
# 有远程才做下一行；全新本地仓没有 origin/main，不要抄
# orca repo set-base-ref --repo id:<repoId> --ref origin/main --json
```

模块并行（从**骨架 worktree 内部**发，或显式指定 repo + 顶层/子级）：

```bash
# 方案 A：人已经切到骨架 worktree 的对话里，这时 new-child 才是「骨架的孩子」
orca orchestration worker-start --task <id_M1> --worktree new-child \
  --name m1-worker --agent claude --setup run --json

# 方案 B：人还停在别的项目对话里，绝不能用 new-child。改用：
orca orchestration worker-start --task <id_M1> --worktree new-top-level \
  --repo id:<repoId> --name m1-worker --agent claude --setup run --json
```

并加一句硬规则：

> `new-child` 的「父」是**发命令的那个 Orca 上下文**，不是 `--repo` 指向的那个 git 仓库。从「完善orca」这类治理 worktree 里给全新项目开 worker，一律 `new-top-level` + `--repo`，或先把对话切到新项目再 `new-child`。

### P0-3 把三套完全不同的编排系统写成了一套

原文同时出现、且没有贴标签：

| 系统 | 出现位置 | 实际是什么 |
|---|---|---|
| Orca orchestration | 阶段 4、第三部分（`run-create` / `task-create` / `worker-start` / `check`） | Orca App 的 Run/Task/Dispatch |
| Claude Code / Grok **Workflow 工具** | 1.3「通过 Workflow 工具」、第四部分 `agent()` 并发上限 `min(16, CPU-2)`、生命周期 1000、`/config` Dynamic workflow size 15、`isolation: 'worktree'`、`budget.total`、坑表「`agentType: codex-design` 静默跑在 haiku」 | 当前对话里的 Workflow 运行时，**不是** `orca orchestration` |
| `codex-pool` MCP | 1.1 能力层、1.2 收尾终审 | 当前 Claude session 挂载的 MCP，用来派 Codex，不创建 Orca Dispatch |

**问题：** 目标读者按第四部分去调 Orca，会去找不存在的 `agent()` 上限和 `/config` Dynamic workflow size。按坑表去避免 `codex-design`→haiku，会以为 `orca orchestration worker-start --agent codex` 也会静默变成 haiku——**不会**。那条坑只属于 Workflow 的 `agentType` 字段。

第四部分「就在这个 worktree 里，只有 2 个任务(Sol/Opus)就已经因为容量红灯被挡住、禁止启动新 worker」也是本 worktree 的 `comment` 原文（`orca worktree current --json` 可见），不是 Orca 硬门禁。脚本自 2026-08-16 起 `recommendation.gate=gate_removed`，红灯只在 `advisory_true_recommendation`。正文把「协调者主动停」写成了「系统禁止」。

**建议：**

1. 第一部分地图里加一个「三套编排，不要混」盒子（草稿见 7.1）。
2. 阶段 4 只保留 Orca orchestration。
3. 第四部分拆成两小节：「用 Orca orchestration 排 100 个任务」和「不要用对话内 Workflow 去冒充 100 个 Orca worker」。Workflow 数字可以留，但必须写「这是对话内 Workflow，跟 `orca orchestration worker-start` 无关」。
4. 坑表「`agentType: codex-design`」一行改成「对话内 Workflow，不是 Orca CLI」。
5. 容量那句改成：「advisory 红灯时协调者应当收敛；脚本不再硬拦截。本 worktree 曾据此主动停过两个复核任务，不是 Orca 返回了禁止码。」

### P0-4 `check --wait` 示例少了官方循环里真正会卡住的那一半：`--ack`、滚动等待、超时≠失败

**原文：** 一次 `check --wait --types worker_done,escalation,question --timeout-ms 1800000`，然后当并行结束。

**v1.4.187 `orca skills get orchestration` 原文要点（本次通读，不是凭记忆）：**

- `check` 返回绑定 Run 里**最老的一批 Delivery（最多 50 条）**，并且会**原样重放**直到 `check --ack <delivery_id>`
- 必须处理完这批里的每一条（含 `question` → `orca orchestration reply --id <msg_id>`）再 ack
- `check --wait` 超时或 `{count:0}` 是检查点，不是 worker 失败；真实编码任务经常 15–60 分钟
- 每接受一条 `worker_done`，ack 之前必须先决定终端去向：立刻 `worker-start --task <next> --terminal <handle>` 移交，或 `worker-release --dispatch <dispatch_id>`
- `worker-release` **必须** `--dispatch <dispatch_id>`；原文写的裸 `worker-release` 不能运行
- 不要因为超时、tui-idle、heartbeat、question、被拒的 `worker_done` 去 release

**按原文执行的后果：** 第一个 worker 完成后，同一批 Delivery 会一直重放；协调者以为「wait 卡住了」或「对面没做完」，进而重派——这正好撞上后面的 `agent_prompt_stalled` / 3 次失败熔断。

**建议把阶段 4 的等待段整段换成下面这份（可直接粘贴）：**

```bash
# 滚动等，不要指望一次 30 分钟 wait 覆盖整个模块
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json

# 处理这一批 Delivery 里的每一条：
# - question → orca orchestration reply --id <msg_id> --body "<answer>" --json
# - escalation → 先 worker-show / worker-read / terminal read，再决定
# - worker_done（accepted）→ 立刻二选一，再 ack：
#     复用：orca orchestration worker-start --task <next> --terminal <handle> --json
#     释放：orca orchestration worker-release --dispatch <dispatch_id> --json
# 裸写 `worker-release` 或 `worker-show` 会因缺 --dispatch 失败

orca orchestration check --ack <delivery_id> --wait --types worker_done,escalation,question --timeout-ms 900000 --json
# 重复直到每一个预期 Dispatch 都 settled。超时只说明「这一窗没新邮件」。
```

并加：

> 官方还要求：派发前 `orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json`。`worker-start` 内部会做就绪等待；自己走 `dispatch --inject` 时这一步不能省。`tui-idle` 只表示输入框空闲，**不是任务完成**。

### P0-5 必须补一节「协调者运行时坑」——这次复核任务本身又踩了一次，而且不是容量红灯

**结论：要补。不要只加坑表一行。** 这是命令层容量红灯之外的新发现，而且和 2026-08-21 那份源码修复文档不是同一层：源码修复**没有装进正在跑的 1.4.187 App**（「不得重建/重装 Orca.app」），所以操作手册必须写 workaround，不能写「已经修好了」。

本次复核过程中的第一手证据：

1. 按 worker 前言发送 `heartbeat`，CLI 立即返回：`Dispatch ctx_4833eb47bfd3 capability is revoked.`（exit 1）
2. 任务书事先说明：协调者这边真实遇到过 `agent_prompt_stalled`（任务提示词卡在输入框没提交，需手动发空文本 + 回车才能推进），以及高负载下 Orca App 崩溃重启，导致 capability 被撤销、`worker_done` 信号链路失效，只能改监控终端 `tui-idle`
3. 同源文档 `reports/ORCA-ORCHESTRATION-DISPATCH-RELIABILITY-FIX-2026-08-21.md` 已把 `agent_prompt_stalled` 的一种根因钉到代码行（忙碌终端上 `workingSequence` 不跳变 → 5 秒后误判 → `failWorkerStart` 永久钉上 `capability_revoked_at`）。该修复提交在 `完善claude` worktree，**明确未 build/未安装进正式 Orca.app**。正在跑的 1.4.187 仍是旧行为
4. 该修复还诚实留下缺口：capability 一旦吊销，没有代码路径清回来。所以 **App 崩溃、误判 stalled、busy 假阴性，最终都汇到同一条死胡同：worker 可能已经把活干完，正式通道却永远收不到 `worker_done`**

**Claude/Codex 共有盲点：** 两家的 worker 前言都把 `worker_done` 写成「唯一合法的完成信号」，协调者循环都写成 `check --wait --types worker_done,...`。它们很少写「这条信号链路可以整段死掉，你还得有 Plan B」。人在桌前能看见输入框里躺着一段没提交的 prompt、能看见 App 闪退；模型把 CLI 报错当成「我写错了命令」或「worker 还在跑」，于是重派，把 3 次失败熔断也打满。

建议新增「阶段 4 之后 / 坑表之前」一整节，完整草稿见第 7.2 节。短结论：

| 现象 | 不要做什么 | 要做什么 |
|---|---|---|
| 派发后 prompt 停在输入框、worker 没开始 | 不要马上 `worker-start` 重派同一终端 | `orca terminal send --terminal <h> --text "" --enter --json`（空文本+回车）；再 `terminal read` 看是否开始工作 |
| `agent_prompt_stalled` / `Dispatch ... capability is revoked` | 不要对同一 dispatch 再发 heartbeat / worker_done；不要当成「任务失败」立刻重建 Task | `worker-show --dispatch <id>`；看 `observation` / preview。需要正式收据时用 `worker-start --task <same> --retry-of <oldDispatch> --terminal <h>`（placement 必须重说一遍，`--retry-of` 不继承） |
| App 崩溃重启、旧 capability 全废 | 不要死等 `check --wait` 的 `worker_done` | `orca terminal wait --terminal <h> --for tui-idle --timeout-ms 60000` + `terminal read` / `worker-read` 判断是否真做完；再决定 `--retry-of` 补收据或人工验收 |
| 容量脚本 `advisory_true_recommendation.gate=red` | 不要说「系统禁止启动」 | 主动收敛；这是参考信号。`recommendation` 顶层仍是 `new_workers_default:2` 且 `gate_removed` |

---

## 3. 命令对照表（v1.4.187 现取 help / skills / 只读实测）

凡标注「可运行但不完整」的，原文 flag 没写错，缺的是少了就会踩 P0 的那些。

| 原文命令 | 判定 | 证据 | 应改成 |
|---|---|---|---|
| `orca status --json` | 正确 | 实测 `ok:true`，`appVersion=1.4.187` | 保留；注明 JSON 在 `result.runtime.appVersion` |
| `orca open --json` | 正确 | `orca open --help` | 保留 |
| `orca worktree ps --json` | 正确但不安全 | 实测 `result.totalCount=144`，`truncated=false`，**原始 JSON 252 481 字节**。Agent 工具输出上限约 40KB，中间会静默截断。help 有 `--limit` | 扫重复开工时写：`orca worktree ps --json` 用 python 读文件/stdin，不要靠对话里的截断输出。本机 144 个 worktree，扫显示名/path 即可 |
| `orca repo add --path /abs --json` | 正确 | help 一致 | 明确「从 JSON 取 repo 的 `id`（UUID）」；worktree id 是 `id::path` |
| `orca repo set-base-ref --repo id:<repoId> --ref origin/main --json` | 正确，有前置条件 | help 一致 | 已有「有远程的话」注释，请加粗：全新 `git init` 仓没有 `origin/main`，这行会失败 |
| `orca worktree create --repo id:… --name … --agent claude --prompt … --setup run --json` | flag 合法，谱系错误 | help：默认继承父上下文；`--no-parent` 才切断 | 见 P0-2，必须加 `--no-parent`（新项目） |
| `orca orchestration run-create --objective … --json` | 正确 | help 一致；会把 **当前终端** 绑到这个 Run | 加：换终端后续命令要先 `run-use --id <runId>`。前置：Settings → Experimental 里 orchestration 需开启（官方 skill Preconditions） |
| `orca orchestration task-create --spec … --json` | 正确 | help 还有 `--task-title` `--display-name` `--deps <json_array>` `--parent` | 大项目请带 `--task-title`，侧边栏才读得懂 |
| 正文「`task-create --deps`」 | 不是可运行命令 | 真正语法：`--deps '<json_array>'`，元素是已存在的 task id | 写成 `--deps '["<id_skeleton>"]'`，并说明：依赖未完成时 `task-list --ready` 不会列出它 |
| `worker-start --task … --worktree new-child --name … --agent claude --json` | flag 合法，语义易错 | help：`--worktree current\|selector\|new-child\|new-top-level`；`--agent` 与 `--terminal` 互斥；新 worktree 默认 `--setup run`；远程 `new-child` 非法 | 见 P0-2。补 `--repo` / `new-top-level`。终审 Codex 应写成 `--agent codex --model gpt-5.6-sol --effort xhigh`（`--effort` 必须配 `--model`，且不能和 `--terminal` 一起用） |
| `check --wait --types worker_done,escalation,question --timeout-ms 1800000 --json` | 类型正确，配方不完整 | skill 明文列出 `question` 是合法 type；Windows 才需要给逗号加引号（本机 macOS 无所谓） | 见 P0-4。`--timeout-ms 1800000` 可以留作单窗上限，但不能当「整个模块的总超时」 |
| 正文「`worker-start --terminal <handle>`」 | 不完整 | 仍要 `--task`；复用时不要再传 `--agent/--model/--effort` | `worker-start --task <next> --terminal <handle> --json` |
| 正文「`worker-release`」 | **不能运行** | 必填 `--dispatch <dispatch_id>` | `orca orchestration worker-release --dispatch <dispatch_id> --json` |
| 正文「先查 `worker-show` 的原始终端预览」 | 方向对，命令不完整 | 必填 `--dispatch`。COMPLETION-BACKLOG 证实 Trusted Access 要看 `preview` 字段，不要只看空 transcript。另有 `worker-read --dispatch … --limit 50` | 写成两条：`worker-show --dispatch <id> --json`（看 status / observation.agentWait / preview）+ `worker-read --dispatch <id> --limit 50 --json` |
| `task-list --ready` | 正确 | help 有 `--ready` `--brief` `--status` | 大项目扫状态用 `--brief`，需要完整 spec 时不要 `--brief`（会截到 160 字符并标 `spec_truncated`） |
| `python3 "…/agent_capacity.py"` | 路径真实、行为描述基本对 | 本次实测：顶层 `recommendation.gate=gate_removed`，`new_workers_default=2`；`advisory_true_recommendation.gate=red`，`coordinator_only=true` | 必须声明：这是**这台 Mac 的 Orca 治理脚本**，不是新项目仓库里的文件。新项目里没有这份脚本是正常的。看 `advisory_true_recommendation`，不要看顶层 `recommendation` 就开 2 个 |
| `orca worktree create --name grok-second-opinion --no-parent --agent grok --prompt … --json` | flag 合法 | `--agent grok` 在 orca-cli skill 的 known ids 里 | 仍要 `--repo id:<repoId>`（除非已经在目标项目 worktree）。这是 **handoff 不是 supervised dispatch**：不会注入 `taskId/dispatchId`，对面不应发 `worker_done`。若要等结果，改走 `worker-start --agent grok` |
| `ls ~/.claude/plans/` | Claude 专用 | Codex / Grok / 其他 TUI 不会写这里 | 改成：Claude 看 `~/.claude/plans/`；全机看 `orca worktree ps`；不要只扫一个 vendor 目录 |

未在原文出现、但目标读者并行时会立刻用到、应补进阶段 4 的命令：

```bash
orca orchestration run-use --id <runId> --json          # 换终端后续绑回
orca orchestration task-list --ready --brief --json
orca orchestration worker-show --dispatch <id> --json
orca orchestration worker-read --dispatch <id> --limit 50 --json
orca orchestration worker-release --dispatch <id> --json
orca orchestration worker-retain --dispatch <id> --json  # 用户明确要求留着调试时
orca orchestration worker-start --task <id> --retry-of <oldDispatch> --terminal <h> --json
orca terminal wait --terminal <h> --for tui-idle --timeout-ms 60000 --json
orca terminal read --terminal <h> --json
orca terminal send --terminal <h> --text "" --enter --json   # 仅用于 prompt 卡在输入框
```

官方 skill 另外两条不要写进「从零大项目」主路径，但要在附注里点名，避免读者自己发明：

- `dispatch --inject`：不创建 `worker_dispatches` 行，`worker-release` 不会关这个进程（`unsupervised` / `no_owned_resource`）。要监督生命周期用 `worker-start --terminal`
- 同一 task 连续 3 次失败会熔断并把 task 标 failed。对 stalled 重派要换 `--retry-of`，不要无脑重建

---

## 4. 内部矛盾、过时、重复（多轮追加的痕迹）

按「先出现的 loc → 后出现的 loc」列，改写时选一个说法，删另一个。

| # | 冲突 | 建议保留 |
|---|---|---|
| C1 | 目录只列四部分；正文有第五部分「有必要再加一个 Grok 吗」 | 目录补上第五部分，或把第五部分降为附录。现在是典型「追问直接贴文末」 |
| C2 | 题头「小白」vs 新手指南把它定义为「更技术向」vs 任务书「会基本操作 Orca」 | 见 P0-1 |
| C3 | 阶段 2 骨架 `worktree create` 无 `--no-parent`；第五部分 Grok 示例有 `--no-parent` | 新项目一律 `--no-parent` / `new-top-level`；Grok 临时候补若走 handoff 才 `--no-parent` |
| C4 | 阶段 4 容量「advisory-only，不再硬拦截」vs 第四部分「已经被挡住、禁止启动新 worker」 | 统一为：脚本不硬拦，协调者见红灯应当自己停。把「禁止」改成「本 worktree 当时按红灯主动停」 |
| C5 | 1.2 / 阶段 5「收尾用 codex-pool 或 Orca orchestration 拉 Codex」vs CLAUDE.md「需要 Codex 的任务由 Orca 创建同级 Codex worker，不在普通 Claude 子 agent 或后台 shell 中裸跑 Codex」 | 手册主路径只写 `worker-start --agent codex`。`codex-pool` 标成「当前 Claude 对话里的快捷通道，不产生 Orca 完成收据，不能替代正式终审 Dispatch」 |
| C6 | 1.3「Workflow 工具需要你显式发起」把对话内 Workflow 算进「我能做的」；阶段 4 主路径又全是 Orca orchestration | 见 P0-3，拆开 |
| C7 | 阶段 5「Claude opus + max 单路」vs 1.1 技能路由层仍把四档 `codex-*` 写成日常工具 | 技能层地图可留，但加一句「日常迭代不自动拉 Codex；四档路由是终审/争议时才用」 |
| C8 | 阶段 6 把「不得重建 Orca.app」泛化成「任何已经在用的正式产物」 | 泛化作为习惯可以留，但要分开写：Orca.app 是硬规则；你的获客网站能不能发版是你的产品规则，不要让读者以为 Orca 禁止他们部署自己的网站 |
| C9 | 阶段 0 和坑表都写「扫 `~/.claude/plans/` + `worktree ps`」 | 坑表改成指向阶段 0，不要两段几乎相同的字 |
| C10 | 阶段 4 命令块和第三部分命令块几乎复制粘贴 | 第三部分只保留目录隔离 + 任务 spec 写法，命令用「同阶段 4，把名字换成 short-video / douyin」一行代替 |
| C11 | 「CLAUDE.md 规则 4」——`~/.claude/CLAUDE.md` 里这三条政策块的条目**没有编号**。第四条子弹是双复核节奏，第五条才是不得重建 Orca | 不要用「规则 4」。写成「用户级 CLAUDE.md 里『双模型复核』那一条」 |
| C12 | 第三部分「抖音数据接口可以直接复用已经验证过的 MoreAPI 抖音路由」 | 这是**这台机器/这个用户**的私有资产，不是教程读者会有的东西。改成「如果本机已经有验证过的路由，在任务 spec 里写路径；没有就当新依赖，不要让两个 worker 各自发明鉴权」 |

过时风险（今天还对，但文档写死了容易明天错）：

- App 版本写死 v1.4.187：保留，但加「以 `orca status --json` 为准，flag 以 `orca skills get` 为准」
- Codex 终审「池子当前上限 `xhigh`」：与 CLAUDE.md 一致，可留，标注日期 2026-08-22
- `agent_prompt_stalled` 源码修复未进 App：必须写「1.4.187 仍是旧行为」，否则有人读到 08-21 那份修复记录会以为 workaround 可以删了

---

## 5. 目标读者会卡住、原文没覆盖的内容（按真实操作顺序）

这些不是「可以再写一点背景」，是按原文做大项目时会在具体步骤上停住的洞。

1. **orchestration 是实验功能。** 官方 skill Preconditions：`Settings > Experimental` 必须打开。原文完全没提。命令失败时读者会以为自己写错了 CLI。
2. **JSON 里到底抄哪个 id。** `repo add` → repo UUID；`worktree create` → 完整 `repoId::/abs/path`（本次 `worktree current` 实测 id 就是这种形态）；`run-create` → run id；`task-create` → task id；`worker-start` → dispatch id。原文全程 `<repoId>` / `<id_M1>` 混用，读者会把 repo UUID 填进 `--dispatch`。
3. **`.gitignore` 先于 `git add -A`。** 阶段 1 全新项目直接 `git add -A && git commit -m "init"`，在真实目录里会把 `.DS_Store`、后续的 `node_modules`、`.env` 打进首提交。大项目这步不可逆成本很高。
4. **骨架 worktree 的分支从哪来、模块 worktree 的分支怎么合回去。** 原文第三部分只说「先合一个到 main、跑测试、再合第二个」。没说：每个 child worktree 通常是独立 git 分支；合的是 git 分支不是「Orca 卡片」；不要两个 worker 同时往同一个 main 快进。缺这一段，物理目录隔离会被最后一步 git merge 毁掉。
5. **人要不要切过去看。** Agent 建了三个 worktree 之后，读者不知道侧边栏该点哪一个、对话还停在旧项目。加 3 行：骨架阶段请把人的对话切到新项目；并行阶段协调者可以留在骨架或专门的协调 worktree，但每个 worker 必须在自己的 worktree。
6. **`question` 不是噪音。** worker 会 `orca orchestration ask` 堵住。原文的 `check --types` 写了 `question`，但没写协调者要 `reply`。目标读者会以为那是日志。
7. **Codex Trusted Access 横幅。** 坑表有，但缺可执行识别：`worker-show` 的 `preview` 出现 `Trusted Access` / `enterprise-trusted-access-for-cyber` → 不要重试同一模型。本机已验证的绕法是换档（历史上用过 `gpt-5.6-terra` / high），且那是用户显式授权的例外，不是默认终审模型。终审仍应先尝试 sol；撞墙再升级问人。
8. **非交互 Claude worker 的 `--setting-sources user`。** CLAUDE.md 第二条，原文 1.1 只说「非交互 worker 默认排除项目级设置源」，没给命令层含义。读者自己 `terminal create --command "claude"` 会踩未审查 `.mcp.json`。至少写：「用 `worker-start --agent claude` 让 Orca 按组合启动器来；不要自己拼 `claude -p` 还带着项目 `.mcp.json`。」
9. **网页自动化不是 Orca 内置 browser 也不是 Computer Use。** 获客网站例子几乎必然碰到登录/后台。原文 1.1 提了 ego-browser + ego-orca-profile，但阶段 3–4 的任务 spec 模板里没有「只许走 ego-orca-profile router，禁止直打 ego-browser / Playwright」。两个模块并行时最容易每人开一个浏览器抢锁。
10. **SSH / 部署。** 阶段 0 第 3 点提到了服务器规则，但阶段 6 收尾没有任何「部署前再跑静态校验、不要把密钥写进任务 spec」的回收。大项目最常见的最后一步就是部署。
11. **`worktree ps` 在这台机器上是 250KB+。** 阶段 0「扫一眼」对 Agent 来说会截断，可能刚好把「已经有人在做同一件事」的那一行裁掉——这会让阶段 0 的存在理由落空。见命令表。
12. **心跳、`worker_done` 的 `--outcome`、capability。** 原文从协调者视角写，没告诉读者：如果你自己是被派出去的 worker，`worker_done` 必须带 `--outcome succeeded|failed` 和 task/dispatch 两个 id；capability 被撤后发什么都会失败。目标读者两边都会当。
13. **合并前的终审 Dispatch 怎么写模型。** 阶段 5 说了 `gpt-5.6-sol` + `max` / 池子 `xhigh`，阶段 4 示例却是 `--agent claude` 且无 `--model/--effort`。补一条终审专用 `worker-start`。
14. **失败熔断。** 官方 skill：同一 task 连续 3 次失败就 circuit-break。原文鼓励「卡住就查 preview 再决定要不要重试」，但没说重试三次会把 task 钉死。

---

## 6. 第五部分 Grok：判断保留，措辞改三处

保留「不要定成标准第三条腿，按需临时拉」。这次任务本身就是按需临时拉，而且协调者明确要求第三方视角——这正好印证原文，不否定原文。

要改的三处：

1. 「CLAUDE.md 规则 4」→ 「用户级 CLAUDE.md 的双复核那一条」（见 C11）
2. 示例命令补 `--repo`，并标明这是 **handoff**：对面是独立 Grok 会话，不会自动有 orchestration preamble。若协调者还想 `check --wait` 收 `worker_done`，必须改 `worker-start --agent grok`，不能 `worktree create --prompt`
3. 加一句边界：Grok 临时候补的价值是「训练体系外的盲点」（命令谱系、两套编排混用、信号链路死掉后的 Plan B），不是再加一轮同样的 P0/P1 清单。不要把 Grok 例行化成第三份双复核

---

## 7. 建议直接粘贴进原文的两段新稿

### 7.1 放在 1.1 分层地图之后：「三套编排不要混」

```markdown
## 1.1.1 三套「多 Agent」不是同一件事

| 你说的「派一个 Agent」 | 实际走的系统 | 有没有 Orca 完成收据 | 什么时候用 |
|---|---|---|---|
| `orca orchestration worker-start ...` | Orca App 的 Run/Task/Dispatch | 有。`worker_done` 能被 `check --wait` 等到 | 大项目主路径。要等结果、要依赖、要并行，都走这里 |
| 对话里的 Workflow / `agent()` / `parallel()` | Claude Code 或 Grok 当前会话的 Workflow 运行时 | 无 Orca Dispatch。数字上限（并发 min(16, CPU-2)、生命周期 1000、/config 的 15）只约束这一层 | 用户明确说「用 workflow」时。不要用它去冒充 Orca 的 100 个任务 |
| `codex-pool` MCP | 当前 Claude 会话挂载的工具 | 无 Orca Dispatch | 当前对话里要快速拿一句 Codex 判断。正式终审仍应 `worker-start --agent codex` |

坑：「Workflow 里 `agentType: codex-design` 会静默跑在 haiku 上」只属于中间那一行。
`orca orchestration worker-start --agent codex` 不会因为这个坑变成 haiku。
```

### 7.2 放在阶段 4 要点之后：「协调者运行时坑（容量红灯之外）」

```markdown
## 阶段 4.1 协调者运行时坑：信号链路会断，容量红灯不是唯一的停机原因

下面三条都在这台机器的真实会话里发生过，包括 2026-08-22 这次文档复核自己。
正在运行的 Orca App 是 v1.4.187。`完善claude` worktree 里有一份针对
`agent_prompt_stalled` 忙碌误判的源码修复，但按「不得重建/重装 Orca.app」
**没有装进当前 App**。所以手册写 workaround，不写「已经修好了」。

### A. 任务提示词卡在输入框（`agent_prompt_stalled` 的一种现场形态）

派发看起来成功，worker 终端的输入框里躺着完整任务书，但没按回车，Agent 一直闲着。
协调者侧可能报 `agent_prompt_stalled`，随后 **这个 Dispatch 的 capability 被永久吊销**。

先不要重派。对那个终端发一次空提交：

```bash
orca terminal send --terminal <handle> --text "" --enter --json
orca terminal read --terminal <handle> --json
```

开始工作了就让它做完。需要正式收据时，另开：

```bash
orca orchestration worker-start --task <sameTask> --retry-of <oldDispatch> \
  --terminal <handle> --json
```

`--retry-of` 只连接 lineage，**不继承** `--worktree/--agent/--terminal/--on`，这些要重写。
不要对新 task 做「整个重新 task-create + worker-start」——那是这几个月一直在用、
但官方已经提供了替代的手工规避。

同一 task 连续失败 3 次会熔断。对 stalled 不要无脑重试满三次。

### B. 高负载下 App 崩溃重启

Orca App 崩溃再起来之后，旧 Dispatch 的 capability 仍然是撤销状态
（`verifyDispatchCapability` 先查撤销标记；崩溃不会帮你清回来）。
worker 进程如果还在，可能已经把活干完，但：

```text
Dispatch <id> capability is revoked.
```

heartbeat / worker_done / ask 全都会被拒。这时 `check --wait --types worker_done`
会空等到超时——这不是 worker 没做完。

Plan B（按优先级）：

1. `orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json`
   只说明 TUI 空闲，再 `orca terminal read` / `orca orchestration worker-read --dispatch <id>`
   看它是否真的写完了报告、是否自己说过「做完了」。
2. 若工作已完成、只缺收据：`--retry-of` 挂一个新 Dispatch 到**同一终端**，
   让它只补发 `worker_done`，不要重做任务。
3. 若终端已死、产物在磁盘上：人工验收磁盘产物，不要为了收据再开一个会把工作做第二遍的 worker。

### C. 和容量红灯的关系

`agent_capacity.py` 的红灯是「机器已经很忙，别再加重量级 worker」。
A/B 是「已经派出去的那一个，正式信号可能永远回不来」。
红灯时收敛并发，可以减少 B 这类崩溃；但红灯变绿之后 A/B 照样会发生。
两套对策都要写进协调者循环，不要只写容量。
```

（上面代码块嵌在建议稿里，粘贴到原文时保持为原文的 markdown 代码块即可。）

---

## 8. 建议的目录重排（最小改动）

保留四部分主结构，不要写成新书。

1. 题头换读者 + 去掉会话事故
2. 第一部分保留分层地图，插入 7.1，删掉「你现在这句话之前系统提示里那段…」
3. 第二部分阶段 0–6：按第 3 节改命令；阶段 2 加 `--no-parent`；阶段 4 换成完整 ack 循环；插入 7.2
4. 第三部分只留架构约定和 spec 模板，命令引用阶段 4
5. 第四部分拆「Orca 队列」/「对话内 Workflow 不要拿来凑 100」
6. 第五部分留，按第 6 节改；目录补上
7. 坑表：删与阶段 0 重复的「多 session」行（改成链接）；`codex-design` 行标注 Workflow；`worker-show` 行补全 flag；新增「capability 吊销 / App 崩溃」指向 7.2

---

## 9. 本次只读核验记录（便于下一轮对照，不必写进原文）

| 核验 | 结果 |
|---|---|
| `orca` 可执行文件 | `/Applications/Orca.app/Contents/Resources/bin/orca`；`ORCA_CLI_COMMAND` 未设置 |
| `orca status --json` | `ok:true`，App running pid 53687，`runtime.appVersion=1.4.187`，runtimeId `253d2786-0bb0-4fb0-8e6d-2db1f88c19d8` |
| `orca skills get orca-cli` / `orchestration` | 成功；orchestration Preconditions 含 Settings → Experimental |
| `orca worktree ps --json` | 144 worktrees，`truncated=false`，252 481 字节 |
| `orca worktree current --json` | id=`35e82c5e-…::/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`，`parentWorktreeId=null`，有大量 children |
| `orca repo list --json` | 27 个 repo；`id` 为 UUID |
| `agent_capacity.py` | 顶层 `gate_removed` / default 2 / max 3；advisory `gate=red` / `new_workers_max=0` / `coordinator_only=true`；当时 `worktrees=144`，`working_worktrees=5`，`reported_agents=10` |
| `worker-start/--help` 等 | `--worktree new-child\|new-top-level`、`--retry-of`、`--model`+`--effort`、`worker-release --dispatch`、`worker-show --dispatch`、`task-create --deps <json_array>`、`check --ack` 均与原文缺口对应 |
| 本 worker 发 heartbeat | **失败**：`Dispatch ctx_4833eb47bfd3 capability is revoked.` —— P0-5 的现场复现，不是转述 |
| 未跑 | `repo add` / `worktree create` / `run-create` / `worker-start` / `dispatch` / 任何会创建或删除 worktree 的命令 |

---

## 10. 第三方视角：Claude 和 Codex 这次最可能一起看漏的四件事

1. **谱系默认值。** 它们从不从「别人的治理 worktree」给一个全新 repo 开 child，所以不会觉得 `--no-parent` 值得写成粗体。读者会。
2. **信号链路不是物理定律。** 两家的编排教材都以 `worker_done` 为终点。capability 吊销、prompt 停在输入框、App 崩溃，是桌面运行时的问题，模型当 CLI 用户时很少把「空回车」和「改盯 tui-idle」写进教程。
3. **把当前会话的工具当成世界的全部。** Workflow 上限、codex-pool、`~/.claude/plans/`、本 session 的 NACK、本 worktree comment 里的「禁止启动」，都是「我此刻能看见的东西」。手册读者可能是另一个模型、另一场对话、甚至是人自己在终端里敲。
4. **JSON 很大时，Agent 自己也看不见完整输出。** 本机 `worktree ps` 25 万字节。两家都习惯写「扫一眼 `orca worktree ps --json`」，但它们的工具层会截断，阶段 0 的防重复开工在 Agent 执行时是假的。这是「住在工具环境里」造成的盲点：它们以为自己看到了 ps 的全部。

以上四处，建议改写时优先于任何文风/结构润色。
