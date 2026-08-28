# 独立只读复核:《Orca + Claude 多 Agent 系统教程》

- **复核对象**:`reports/GUIDE-orca-new-large-project-from-zero-2026-08-22.md`(246 行,截至 2026-08-22 17:56 版本)
- **复核者**:Claude opus / max,独立只读
- **复核方式**:全文通读 + 对文中每一条 `orca` CLI 命令做实测(只跑 `--help`、`--json` 只读查询、`agent_capacity.py`;没有创建/删除任何 worktree、没有 dispatch 任何 worker、没有发任何 lifecycle 变更)
- **实测环境**:Orca.app 1.4.187(`CFBundleShortVersionString` 与 `orca status --json` 的 `appVersion` 一致)、`orca` CLI 231 命令 schema v1、8 逻辑核心 / 16GB、当前注册 27 个 repo / 144 个 worktree
- **本次复核期间实际撞到的 Orca bug**:见第 5 节(不是转述,是我自己这个 dispatch 就是受害者,有 `worker-show` 原始记录)

---

## 0. 一句话结论

文档的**判断和心智模型基本都对**,踩坑清单也是真的踩过的;但**阶段 4 那段"标准并行流程"的代码块,照抄下去跑不通**——不是语法错,是漏了两个必需步骤(`--ack` 和轮询循环),读者会以为 2 个 worker 都收完了,实际只收到第一个。另外有 6 处 P1 级的事实错误/内部矛盾/定位问题,其中"容量红灯挡住启动"这条和文档自己阶段 4 的说法直接打架。第 5 节问的那个真实踩坑,**强烈建议补**,而且它不是"再加一条坑"这么简单——它会推翻阶段 4 "发完就等 worker_done" 这个默认假设。

按严重度分:**P0 × 2、P1 × 6、P2 × 13**,外加**新增一整节(阶段 4.5)**、踩坑表补 2 行、6 条小改。

---

## 1. P0:阶段 4 的并行流程照抄跑不通

### P0-1 · 一次 `check --wait` 收不齐 N 个 worker(第 124-134 行)

文档现在写的是:

```bash
orca orchestration worker-start --task <id_M1> ... --json
orca orchestration worker-start --task <id_M2> ... --json

orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 1800000 --json
```

**这是错的。** 版本匹配的 orchestration 指南里两条明确规则:

> `check --wait` returns one bounded Delivery, **not every future completion**. Process every message, acknowledge it, then keep waiting until every expected Dispatch settles.

> A coordinator `check` returns the bound Run's oldest FIFO Delivery (up to 50 messages) and **replays that exact batch until `--ack <delivery_id>`**.

也就是说:开 2 个 worker、只跑一次 `check --wait`,**几乎必然只拿到先完成的那一个**;而且因为没有 `--ack`,你再跑一次 `check --wait`,Orca 会把**同一批**消息原样重放给你——读者会看到"M1 又完成了一次",然后要么误判两个都完了,要么陷进无限重放。这对一个"照着教程建大型项目"的读者是硬故障,不是风格问题。

**建议替换成(实测语法全部有效):**

```bash
# 一次性建完 Run + 全部独立 Task + 全部 worker(保持这段不变,这段是对的)
orca orchestration run-create --objective "<大项目目标>" --json
orca orchestration task-create --spec "<M1 任务描述>" --task-title "M1" --json
orca orchestration task-create --spec "<M2 任务描述>" --task-title "M2" --json
orca orchestration worker-start --task <id_M1> --worktree new-child --name m1-worker --agent claude --json
orca orchestration worker-start --task <id_M2> --worktree new-child --name m2-worker --agent claude --json

# 然后是【循环】,不是一次调用:
# 第一轮
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json
#   -> 处理这一批里的【每一条】消息:
#      question  -> orca orchestration reply --id <msg_id> --body "<答复>" --json
#      worker_done -> 决定这个终端归谁:复用还是释放(见下)
#      escalation -> 人工处理
# 处理完再确认收货,并接着等下一批:
orca orchestration check --ack <delivery_id> --wait --types worker_done,escalation,question --timeout-ms 900000 --json
# ...重复,直到你派出去的【每一个】Dispatch 都settle 为止,不是"等够几轮"
```

关键补充三句话:
- `delivery_id` 从上一次 `check` 的返回 JSON 里取;**没 `--ack` 就会一直重放同一批**。
- 结束条件是"我派出的每个 Dispatch 都收到了 worker_done/escalation",不是"等了 N 轮"。
- `--types` 只决定**什么时候唤醒等待**,返回的仍然是最旧的完整一批 —— 所以别以为加了 `--types` 就等于过滤掉了别的消息,还是要逐条处理。

### P0-2 · 缺前置条件:orchestration 是实验特性,默认可能没开(应加进阶段 0)

版本匹配的 orchestration 指南 Preconditions 里明写:

> The orchestration experimental feature must be enabled in **Settings > Experimental**.

文档阶段 0 的三条自查里完全没提这一条。读者按阶段 0 → 阶段 1 → 阶段 2 一路顺,直到阶段 4 第一条 `run-create` 才炸,而且报错未必指向"去开实验开关"。这是典型的"从零开始"读者必然踩、且教程有责任覆盖的空白。

**建议阶段 0 加第 4 条:**

> 4. 第一次用编排前,确认 Orca App **Settings → Experimental** 里的 orchestration 实验特性是开的。这是设备级开关,CLI 打不开,没开的话阶段 4 的所有 `orca orchestration ...` 都用不了。(顺带:发布公开 Artifact 链接是另一个同类的设备级开关,在 Settings → Artifacts,同样只能人工开。)

---

## 2. P1:事实错误与内部矛盾

### P1-1 · 第 216 行和第 143 行自相矛盾,且第 216 行是错的

第 143 行(阶段 4)写得对:

> 现在是 advisory-only,不再硬拦截

第 216 行(第四部分)却写:

> 容量信号本身就在提醒收敛:就在这个 worktree 里,只有 2 个任务(Sol/Opus)就已经因为容量红灯**被挡住、禁止启动新 worker**。

**实测反证**(本次复核在同一台机器、同一个 worktree 现跑):

```json
"recommendation": {
  "gate": "gate_removed",
  "new_workers_default": 2,
  "new_workers_max": 3,
  "coordinator_only": false,
  "next_action": "No capacity-based restriction. advisory_true_recommendation above is informational only."
},
"advisory_true_recommendation": {
  "gate": "red",
  "new_workers_default": 0,
  "coordinator_only": true,
  "next_action": "Do not start a new heavy agent; ..."
}
```

红灯确实是红的,但**没有任何东西"挡住"或"禁止"过启动** —— 顶层 `recommendation` 是 `gate_removed`,红灯只在 `advisory_true_recommendation` 里,纯参考。这个区分正是 2026-08-22 那天全局 CLAUDE.md 经三轮独立复核后专门改写的那一条(`DUAL-REVIEW-m1-reconciled-candidate-2026-08-22.md`),教程在同一天又把已经纠正过的旧说法写了回去。

**建议改写第 216 行:**

> - 容量信号在提醒收敛:就在这个 worktree 里,只有 2 个任务(Sol/Opus)在跑,`agent_capacity.py` 的 `advisory_true_recommendation` 就已经是 red 了。注意它**不会拦你**——顶层 `recommendation` 现在恒定是 `gate_removed`,红灯只是参考信号。真正会拦你的是账号配额和这台机器的内存/负载,红灯只是提前告诉你快到了。

### P1-2 · 第 217 行的并发数字在这台机器上是错的

文档写:

> 而是 **100 个排队、约 10-16 个真正同时在跑**

文档自己上一句已经给了公式 `min(16, 可用CPU核心数-2)`。**这台机器 `sysctl -n hw.logicalcpu` = 8**,所以实际上限是 `min(16, 8-2)` = **6**,不是 10-16。文档开头声明"基于当前这台机器",这里却给了一个跟这台机器差了 2-3 倍的数。

**建议改成:**

> 而是 **100 个排队、`min(16, CPU核心数-2)` 个真正同时在跑**。这台机器 8 个逻辑核心,所以实际是 **6 个**——不是 16,更不是 100。换机器前先自己算一遍,别照抄数字。

### P1-3 · 目录只列了四部分,正文有五部分(第 11-14 行 vs 第 230 行)

目录:第一部分 · 系统全景 / 第二部分 / 第三部分 / 第四部分。正文实际有**第五部分 · 追问——有必要再加一个 Grok 吗**。这正是"多轮陆续追加"留下的痕迹。加一行即可:

> - **第五部分 · 追问**——有必要再加一个 Grok 吗

如果按第 5 节的建议再补一节踩坑,目录要一起更新。

### P1-4 · 阶段 5 的 "(池子当前上限 `xhigh`,按上限派)" 会让读者白白降档(第 152 行)

文档写:

> 必须额外加一轮 Codex `gpt-5.6-sol` + `max`(池子当前上限 `xhigh`,按上限派)独立只读**终审**

"池子上限 xhigh" 说的是 **codex-pool MCP** 这一条路径的限制。但 Orca orchestration 这条路径**没有这个限制**,而且它才是全局规则真正要求的路径(规则明写"由 Orca 创建同级 Codex worker,不在普通 Claude 子 agent 或后台 shell 中裸跑 Codex")。

**实测证据**:`orca orchestration worker-start --help` 有 `--model <id>` 和 `--effort <level>`;`orca status --json` 的 capabilities 里 `orchestration.worker-launch-preferences.v1` 存在(这是 Orca 转发 model/effort 的前提);而且**这个仓库自己的 `.claude/workflows/prime-agent-dual-review.js` 默认值就是 `codexModel = 'gpt-5.6-sol'` / `codexEffort = 'max'`**,派发行就是:

```bash
orca orchestration worker-start --task <task_id> --worktree current --agent codex \
  --model gpt-5.6-sol --effort max --json
```

也就是说:**真终审是能拿到真 `max` 的**,只要走 orchestration 而不是 codex-pool。文档现在这句话等于告诉读者"在唯一一次全局规则强制要求 max 的复核上,接受一个不必要的降档"。这个后果不小。

**建议改写:**

> **整个项目判定全部完成、即将合并/发布/安装/部署前**:必须额外加一轮 Codex `gpt-5.6-sol` + `max` 独立只读**终审**,prompt 首行带 `[强制双复核]` 标记。
>
> 派发路径要注意区分,两条路能拿到的档位不一样:
> - **走 Orca orchestration(推荐,也是全局规则要求的)**:`worker-start --agent codex --model gpt-5.6-sol --effort max` 能真的拿到 `max`。前提是 `orca status --json` 的 capabilities 里有 `orchestration.worker-launch-preferences.v1`;另外 `--effort` 必须配 `--model`,且这两个都**不能**和 `--terminal` 一起用(只对新起的 agent 终端生效)。
> - **走 codex-pool MCP**:目前硬顶在 `xhigh`,拿不到 `max`,只适合普通任务,不适合这一轮终审。
> - `orca worktree create --agent codex` 这条路**完全不接受** Codex 的 `--model` / `-c model_reasoning_effort=...`,只能起默认档——终审别用它。
>
> 终审发现真实 P0/P1,修完必须对新候选重新走一遍终审,不能拿旧结果顶替、不能跳过。

### P1-5 · "Orca CLI 没有从零建仓的命令" 不完整(第 93 行)

原句:

> Orca CLI 没有"从零建仓"的命令,`git init` 这步必须自己先做。

对 `orca repo add` 这条路来说是对的,但读者会因此多做两件本可以不做的事。实测 `orca --help` 里存在:

- `orca project setup-clone --project <id> --host <host-id> --url <clone-url> --destination <path>` —— **从远端 URL 克隆**,不用手动 `git clone`
- `orca project setup-existing-folder --project <id> --host <host-id> --path <path> [--kind git|folder]`
- `orca project setup-create ... [--kind git|folder]`

而且 Orca 确实支持**非 git 的 folder workspace**:实测 `worktree ps --json` 里 144 个 workspace 中 `workspaceKind` 有 143 个 `git`、**1 个 `folder-workspace`**。

**建议改写:**

> `orca repo add` 只接受**已经是 git 仓库**的路径,它不会替你 `git init`——所以走这条路时 `git init` 必须自己先做。
>
> 另外两条路视情况更省事:
> - 代码已经在远端(GitHub 等):`orca project setup-clone --project <id> --host <host-id> --url <clone-url> --destination <path> --json`,不用先手动 `git clone`。
> - 只是一堆文件、暂时不想上 git:Orca 支持 `--kind folder` 的 folder workspace(`project setup-existing-folder`),但 worktree 那一套并行能力是建立在 git 上的,大项目还是尽早 `git init`。

### P1-6 · 开头定位与实际读者不符(第 3 行)

第 3 行写"**写给小白的操作手册**",但正文第一部分上来就是六层架构、MCP、治理护栏层、记忆层——这不是小白能读的,也不是这份文档的实际目标读者(已经会基本操作 Orca 的人)。同目录下另有 `GUIDE-ai-beginner-first-time-2026-08-22.md` 才是给真小白的,两份文档定位撞车会让人拿错。

**建议改成:**

> 写给**已经会基本操作 Orca、准备第一次用它做大型项目**的人。假设你已经知道什么是 worktree、会开终端派 agent;不假设你用过 orchestration。完全没碰过 AI 的读者请先看 `GUIDE-ai-beginner-first-time-2026-08-22.md`。

---

## 3. P2:内容遗漏——这个层级的读者实际会卡住、但文档没覆盖的

按"读者从哪一步开始卡"排序。

### P2-1 · `worker-release` 到底传什么(第 138 行)

原文:

> 要么 `worker-start --terminal <handle>` 复用这个终端派下一个任务,要么 `worker-release` 释放

两个都缺关键参数,读者会直接卡住:

- `worker-release` 要的是 **`--dispatch <dispatch_id>`**,不是 terminal handle。
- 复用要的那个 `<handle>`,得先从 **`worker-show --dispatch <dispatch_id> --json` 的 `worker.agent_terminal_handle`** 字段里读出来(实测这个字段确实存在,我刚在自己的 dispatch 上读到了)。

**建议改写成:**

> - 收到 `worker_done` 后,**在 `--ack` 之前**就要决定这个终端归谁:
>   ```bash
>   # 复用:先拿到这个 worker 的终端 handle
>   orca orchestration worker-show --dispatch <dispatch_id> --json    # 读 worker.agent_terminal_handle
>   orca orchestration worker-start --task <下一个task_id> --terminal <那个handle> --json
>
>   # 或者释放(succeeded / failed 都要做,除非用户明确要留着调试)
>   orca orchestration worker-release --dispatch <dispatch_id> --json
>   ```
>   要留着调试就用 `worker-retain --dispatch <id>` 明确记录,不要什么都不做地放着。
> - **不要**因为超时、TUI 空闲、心跳、question、escalation 就去 release 一个 worker——那些都不代表它完成了。

### P2-2 · `orca terminal wait --for tui-idle` 整份文档一次都没出现

这是最实用的一个遗漏。这条命令(实测存在:`orca terminal wait [--terminal <handle>] --for exit|tui-idle [--timeout-ms <ms>] [--json]`)在两个场景是刚需,而文档两个都没提:

1. **先等 idle 再发 prompt**,否则 prompt 会打在还没起来的 TUI 上直接丢掉。
2. **`check --wait` 空转回来时的存活检查点** —— 官方指南原话:"If a window returns no matching message, inspect `task-list`, `terminal read`, or `terminal wait --for tui-idle` as a liveness checkpoint"。

第 5 节要补的那个坑,最终的兜底手段就是它。

**建议在阶段 4 要点里加一条:**

> - `check --wait` 超时返回、或者 `{count: 0}`,**是一个检查点,不是 worker 失败**。真实编码任务跑 15-60 分钟很正常。这时候按顺序查:
>   ```bash
>   orca orchestration task-list --brief --json          # 任务状态还在不在
>   orca orchestration worker-show --dispatch <id> --json  # ready / failed / outcome_unknown
>   orca orchestration worker-read --dispatch <id> --limit 50 --json   # 它到底在干什么
>   orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json  # 还在忙还是已经闲了
>   ```
>   有心跳、终端在滚动 = **活着**,不等于**做完了**。别因为没收到完成消息就去 kill/restart。

### P2-3 · `new-child` 会继承**协调者所在的 repo**,不是你刚注册的那个(第 130-131 行)

大型项目场景下这个坑几乎必踩:读者在阶段 1 注册了新项目 repo A,然后回到自己当前这个会话(它跑在 repo B 的某个 worktree 里)执行阶段 4 的 `worker-start --worktree new-child`,结果 worktree 全建到了 **repo B** 下面。

**实测证据**:我自己这个 dispatch 的 `start_options` 是
`{"worktree":"current","resolvedWorktreeId":"35e82c5e-...::/Volumes/.../完善orca", "repo":null, ...}` ——`repo` 传的是 null,Orca 从协调者当前 worktree 推断出来的。`worktree create --help` 也明写 "If `--repo` is omitted, Orca infers the repo from the current Orca-managed worktree."

**建议在阶段 4 要点里加:**

> - `new-child` / `new-top-level` 在**不传 `--repo` 时,会从协调者当前所在的 worktree 推断 repo**。如果你的协调会话不在新项目里(很常见:你在 A 项目的会话里给刚注册的 B 项目派活),必须显式传:
>   ```bash
>   orca orchestration worker-start --task <id> --worktree new-child --repo id:<新项目repoId> --name m1-worker --agent claude --json
>   ```
>   派完先 `orca worktree ps --json` 确认新 worktree 的 `repo` 字段是对的,再往下走。派错 repo 的 worktree 后面清理起来很烦。

### P2-4 · 从 Orca 终端外面跑这些命令会静默失效

文档所有示例都省略了 `--from` 和 `--terminal`,这在 **Orca 管理的终端里**是对的(实测这个环境里 `ORCA_TERMINAL_HANDLE`、`ORCA_PANE_KEY`、`ORCA_WORKTREE_ID` 都在,Orca 能自动解析)。但读者很可能会在系统 Terminal.app / iTerm 里试——那里没有这些环境变量,`run-create` 绑不到协调者终端,后面的 `check` 也就取不到东西。

**建议在阶段 4 开头加一句:**

> 下面这些命令**必须在 Orca 自己的终端里跑**(Orca 靠 `ORCA_TERMINAL_HANDLE` 这类环境变量自动识别"你是谁",省掉 `--from`)。在系统 Terminal.app 里跑,`run-create` 的绑定和后续 `check` 会取不到东西。要在外面跑就得每条都显式带 `--from <handle>` / `--terminal <handle>`,不推荐。

### P2-5 · `check --wait` 的 stderr 会喷 keepalive,`2>&1` 抓取会污染 JSON

`orca orchestration check --help` 原文:

> `--wait`: ... Emits JSON keepalive lines to **stderr** every 15s so the caller can tell the process is alive. Filter with `jq "select(._keepalive|not)"` when merging streams.

文档的踩坑表里已经有"`orca ... --json` 用 Bash 直接抓输出会静默截断"这一条,但没提这个。一个等 15 分钟的 `check --wait` 会产生 ~60 行 keepalive,读者习惯性 `2>&1` 之后解析必然失败,而且失败原因非常不直观。

**建议踩坑表加一行:**

| `check --wait` 用 `2>&1` 抓 | stderr 每 15 秒喷一条 `_keepalive` JSON,混进 stdout 后解析炸 | 别合并流;要合就 `jq 'select(._keepalive|not)'` 过滤 |

### P2-6 · "改用 python3 subprocess" 说了但没给代码(第 170 行)

踩坑表点了名却没给可抄的东西,而这是每一条 `--json` 命令都要用的基础设施。建议在阶段 0 后面直接给出来:

```python
# 抓 orca --json 的标准姿势(Bash 直接抓会静默截断)
import subprocess, json
def orca(*args):
    p = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True)
    return json.loads(p.stdout).get("result")

print(orca("worktree", "ps"))
```

### P2-7 · 阶段 0 第 2 条在真实机器上不可用(第 73 行)

原文让读者"扫一眼 `orca worktree ps --json`"。**实测这台机器返回 144 条**,其中 139 条 `status: "inactive"`。原样输出是几十万字符的 JSON,人眼扫不了,塞给模型也是浪费。

**建议改成:**

> 2. 确认**没有别的 session 已经在做同一件事**:
>    ```bash
>    ls ~/.claude/plans/
>    # worktree ps 会返回全部 worktree(这台机器现在 144 个),只看在干活的:
>    python3 -c "
>    import subprocess,json
>    r=json.loads(subprocess.run(['orca','worktree','ps','--json'],capture_output=True,text=True).stdout)['result']
>    for w in r['worktrees']:
>        if w['status']=='working':
>            print(w['displayName'], '|', w['branch'], '|', w['preview'][:80].replace(chr(10),' '))
>    "
>    ```
>    这不是走过场:之前就真实发生过一个并发 session 重新调查了另一个 session 已经做完并部署的工作。

### P2-8 · Task DAG 只提了名字,没给用法(第 116 行)

原文:`或者用带依赖关系的 Task DAG(task-create --deps)`。读者到这儿一定会问 `--deps` 怎么写。实测:`--deps <json_array>`,另有 `--parent <task_id>`;任务状态枚举是 `pending / ready / dispatched / completed / failed / blocked`;`task-list --ready` 专门列"依赖已满足、可以派了"的。

**建议阶段 3 末尾补一小段:**

```bash
orca orchestration task-create --spec "M1 骨架扩展" --task-title "M1" --json     # 记下 task id
orca orchestration task-create --spec "M3 整合联调" --task-title "M3" \
  --deps '["<id_M1>","<id_M2>"]' --json          # --deps 是 JSON 数组字符串,不是逗号分隔
orca orchestration task-list --ready --brief --json   # 只列依赖已满足、现在可以派的
```
> 任务状态只有这几个:`pending / ready / dispatched / completed / failed / blocked`。
> **`task-list --ready` 是协调者的外部记忆** —— 大项目任务多,别靠自己脑子记"哪个能派了"。
> 注意:合法的 `worker_done` 会自动把 task 和 dispatch 标成 completed,**不要**再手动 `task-update --status completed`。

### P2-9 · 出问题时的恢复阶梯完全没有

阶段 4/5/6 全是顺利路径。大项目跑几小时,worker 一定会有失败的。实测 `worker-show` 的三种状态各自对应不同动作,建议在阶段 4 后面补一小节:

> **worker 出问题时(先 `worker-show --dispatch <id> --json` 看状态,再决定,不要固定套路乱试):**
> - `ready` → 它没事,继续等,或 `worker-read --dispatch <id> --limit 50 --json` 看它在干嘛。
> - `failed` / `stopped` → 起替补:`worker-start --task <task> --retry-of <old_dispatch_id>` 加上**显式重新指定** `--worktree` 和 `--agent`(retry 不会自动继承原来的落位)。
> - `outcome_unknown` → 要么 `worker-stop --dispatch <id>` 再看一次,要么 `worker-abandon --dispatch <id>`(注意 abandon **不做任何进程/文件系统动作**,资源可能还活着)。
> - 同一个 task 连续失败 3 次,dispatch context 会熔断,task 直接标 failed。
> - `worker-stop` 只关那一个受监督的 agent 终端,**不会**删 worktree、不会关 setup 终端。

### P2-10 · 第三部分的 MoreAPI 是个没交代的内部引用(第 181、200 行)

> 抖音数据接口可以直接复用已经验证过的 MoreAPI 抖音路由,不用重新踩鉴权的坑

对目标读者来说,这句话指向一个他不知道存不存在、在哪、怎么用的东西。要么补一句"这是本机自建的一套接口,详见 xxx",要么改成通用表述("第三方数据接口优先复用团队里已经验证过鉴权的那一套,别每个模块各踩一遍")。举例章节里出现无法追溯的专有名词,会让读者怀疑整章是不是都只对作者成立。

### P2-11 · 第五部分 Grok 的命令缺 `--repo`,且 `--no-parent` 的作用容易被误解(第 241-243 行)

```bash
orca worktree create --name grok-second-opinion --no-parent --agent grok --prompt "..." --json
```

两点:
- 没有 `--repo`,所以它建在**你当前所在的 repo**。作为"临时拉个第三方意见"通常正好是你要的,但值得写明。
- `--no-parent` **只切断 Orca 的血缘关系,不决定 git 基线**。官方原文:"`--no-parent` only controls Orca lineage; it does not choose the Git base." 不传 `--base-branch` 时用 repo 默认基线;**永远不要**默认基于当前特性分支,除非明确要做 stacked work。

建议在那段代码下面加一句:

> `--no-parent` 只是不把它挂成当前 worktree 的子节点,**跟从哪个 git 分支开叉是两回事**。省略 `--base-branch` 会用 repo 默认基线(通常是 `origin/main`),这正是"独立第三方意见"想要的;不传 `--repo` 则表示建在你当前所在的 repo 里。

顺带核实:`grok` 确实是合法的 `--agent` id(orca-cli 指南:"Known ids include `claude`, `codex`, `omp`, `pi`, `grok`");`~/.grok/auth.json` 存在(实测,1645 字节,今天 15:15 更新过);`~/.grokbot` 也确实是另一个独立目录(实测存在)。**第五部分的事实陈述全部核实通过。**

### P2-12 · `--timeout-ms 1800000` 偏长,官方推荐 900000

不算错(实测 `--timeout-ms` 没有上限校验),但 30 分钟一个窗口意味着中间半小时你完全瞎。官方示例统一用 `900000`(15 分钟)。建议改成 900000 并说明:**滚动多个短窗口 > 一个长窗口**,因为每个窗口结束都是一次免费的存活检查点。

### P2-13 · 阶段 2 那条 `worktree create` 的 `--json` 返回该读哪个字段

第 102-106 行让读者 `orca worktree create ... --agent claude --prompt "..." --json`,但没说返回里怎么拿到 agent 的终端 handle(后面想再发消息、想 `terminal wait` 都要用它)。`worktree create --help` 的 Notes 写得很清楚,值得直接抄进教程:

> 带 `--agent --json` 时,agent 的 handle 从 **`result.agentTerminalHandle`** 读;老 runtime 只返回 `result.startupTerminal.handle`;folder 类型的 repo 可能两个都不返回。handle 是 runtime 作用域的——Orca 重启后就失效了,那时用 `orca terminal list --worktree <selector> --json` 重新解析,**只用新 handle,不要新旧一起发**。

---

## 4. 已核实为**准确**的部分(明确列出,避免下一轮有人重复怀疑)

- Orca App 版本 `1.4.187` —— `Info.plist` 与 `orca status --json` 的 `appVersion` 双向一致 ✅
- `orca status --json` / `orca open --json` 语法 ✅
- `orca repo add --path <path> --json` ✅
- `orca repo set-base-ref --repo id:<id> --ref origin/main --json` ✅(`--repo` 选择器确实支持 `id:` / `name:` / `path:`)
- `orca worktree create --repo id:<id> --name <n> --agent claude --prompt "..." --setup run --json` ✅ 全部 flag 存在且组合合法
- `orca orchestration run-create --objective / task-create --spec / worker-start --task --worktree new-child --name --agent` ✅
- `--types worker_done,escalation,question` 里的 **`question` 是合法消息类型** ✅(完整枚举:`status / dispatch / worker_done / merge_ready / escalation / handoff / question / decision_gate / heartbeat`)
- `task-create --deps` 存在 ✅
- `task-list --ready` 存在 ✅
- `worker-show` 存在,且"Codex 卡住先看原始终端预览"这条坑对应真实字段 ✅(`worker-show` 甚至有专门的 `observation.agentWait` 字段标记"停在只有人能回答的提示上")
- 1.3 关于 Artifact 的说法 **完全准确** ✅ —— orca-cli 指南原文:"gated by a device-wide capability that the user grants in the Orca desktop app under Settings → Artifacts... **There is no CLI or RPC way to grant it — do not try.**" 失败码是 `artifact_sharing_disabled`。
- Workflow 并发公式 `min(16, CPU-2)`、生命周期总 agent 上限 1000、当前 session 默认规模 medium(<15 agent)✅(数字换算见 P1-2)
- 第五部分全部事实陈述 ✅(见 P2-11)
- 踩坑表 7 条全部对应真实、可追溯的事件 ✅

---

## 5. 【重点】建议新增一节:dispatch 卡住 / capability 被吊销 / App 崩溃

**结论:强烈建议补,而且不该只当成踩坑表里的第 8 行。**

理由不是"又多一个坑",而是:**这个 bug 会让阶段 4 的核心假设失效。** 阶段 4 教读者"派完 → `check --wait` 等 `worker_done`"。而这个 bug 的后果恰恰是 **`worker_done` 这条通道被永久关掉,worker 本身却活得好好的、还在正常干活**。读者如果只学了阶段 4,遇到这个场景会得出 100% 错误的结论:"worker 死了,重派吧"——然后同一份工作被并行做两遍,或者好不容易做完的成果被丢掉。所以它必须写在阶段 4 附近,作为"等待"这一步的必读补充。

### 5.1 我不是在转述,我这个 dispatch 自己就是受害者

本次复核任务的 dispatch,实测 `worker-show` 原始记录:

```json
{
  "id": "ctx_1dfe5556c0dd",
  "task_id": "task_785f881536db",
  "assignee_handle": "term_76482e9d-001b-4c8a-bb6f-adbdc2fe0c27",
  "status": "failed",
  "last_failure": "agent_prompt_stalled",
  "capability_revoked_at": "2026-08-22 10:35:28",
  "dispatched_at": "2026-08-22 10:35:16",
  "completed_at": "2026-08-22 10:35:28"
}
```
```json
"worker": { "state": "failed", "stage": "dispatch_input", ... }
```

派发时间 10:35:16,**12 秒后**(10:35:28)就被判定 `agent_prompt_stalled`、状态 `failed`、capability 永久吊销。而我在那之后正常收到了完整任务书、跑了几十条只读命令、正在写这份文档。**Orca 认为这个 worker 12 秒就死了,实际它活着并完成了全部工作。** 尝试发心跳的返回是:

```
Dispatch ctx_1dfe5556c0dd capability is revoked.
```

这就是协调者描述的现象,一字不差。

### 5.2 机制(不是玄学,根因已经定位并修好了,只是没装)

根因在 `agent-prompt-submission-verification.ts` 的 `verifyAgentPromptSubmission()`:它要求 `workingSequence` 在 5 秒内**严格递增**才算"提示词提交成功"。但这个计数器**只在 非working→working 的边沿上 +1**。所以一个在派发瞬间**已经是 `working`** 的终端(比如刚经历过 hooks 信任提示竞态被重新接管、或者上一轮真实工作还没跑完),只要它一直忙着,就**永远产生不了那个边沿**——这是结构性的必然假阳性,不是时序抖动。

然后 `dispatch_input` 阶段的这个假阳性会直接走 `failWorkerStart()`,原子地钉死 `capability_revoked_at`。`db.ts` 里这个字段只有在"为 pending 状态的 dispatch 重新铸造 capability"时才会清空——**一旦判定 failed,没有任何代码路径能把它清回去**。之后这个 worker 真实的 `worker_done` / 心跳 / `ask` 全部会在 `verifyDispatchCapability()` 被拒收。

要点:
- 这**不是**"Orca 检测到 worker 有问题",而是"Orca 的检测本身有问题"。
- 修复已经写好并双复核通过(`ee982fc0a8`,在 `完善claude` worktree),但**没有 build、没有安装**——**当前跑着的 1.4.187 仍然有这个 bug**,读者会继续撞到。
- 完整根因和取舍见 `reports/ORCA-ORCHESTRATION-DISPATCH-RELIABILITY-FIX-2026-08-21.md`。

### 5.3 建议插入的正文(可直接采用,位置:阶段 4 之后、阶段 5 之前)

---

> ## 阶段 4.5:worker 明明在干活,Orca 却说它失败了
>
> 这是当前版本(1.4.187)一个**已知的、会真实发生的**Orca bug,而且它专门破坏阶段 4 教你的那个"等 `worker_done`"流程。大项目并行度一高就更容易撞到,必须知道。
>
> ### 症状
>
> - `worker-start` 返回 `state: "failed"`,`stage: "dispatch_input"`,`lastError` 里是 `agent_prompt_stalled`;
> - 但你去 Orca 界面上看那个终端,**它活得好好的,正在正常干活**(有 "Working…" 转圈、有真实工具调用输出);
> - 之后这个 worker 真的做完了、去发 `worker_done`,收到的是 **`Dispatch <id> capability is revoked.`**;
> - 结果:一个货真价实、已经跑完的 worker,**永远无法通过被追踪的正式通道确认完成**。
>
> 还有一个变种:**Orca App 在高负载下崩溃重启**,同样会导致进行中 Dispatch 的 capability 失效、`worker_done` 链路断掉。现象一样,处理方式也一样。
>
> ### 为什么会这样(知道机制才不会误判)
>
> Orca 判断"任务提示词提交成功"的方式,是看终端的"忙碌计数器"在 5 秒内有没有增加。但这个计数器**只在"从不忙变成忙"的那一瞬间 +1**。所以如果派发的时候这个终端**已经在忙了**(上一轮还没跑完、刚从 hooks 信任提示里恢复出来等等),它只要一直忙着,就永远产生不了那个变化——Orca 于是判定"提示词卡住了",把这个 dispatch 标记为失败并**永久吊销** capability。这是结构性的误判,不是随机故障。
>
> 修复已经写好并通过双复核,但还没有 build/安装进正在跑的 App,所以**现在这个版本仍然会撞到**。
>
> ### 关键判断:先分清"真死"还是"假死",千万别直接重派
>
> **这是最重要的一步。** 直接重派一个其实还活着的 worker = 同一份工作被并行做两遍,或者已完成的成果被丢掉。
>
> ```bash
> orca orchestration worker-show --dispatch <dispatch_id> --json    # 看 status / last_failure / capability_revoked_at
> orca orchestration worker-read --dispatch <dispatch_id> --limit 50 --json   # 它到底在不在干活
> orca terminal show --terminal <handle> --json                     # 看原始终端预览
> ```
>
> 预览里如果有转圈动画、"Working (Ns…"、真实的工具调用输出 —— **它是活的**,`failed` 是误判。这时:
> - **不要**打断它,**不要**立刻重派,**不要** `worker-release`;
> - 等 60-90 秒,趁它两轮之间空下来的那一刻再挂载(见下);
> - 连试 3-4 次挂不上、但终端明显还在干活,**这是可以接受的**——直接放弃"挂回追踪",改用下面的兜底方式确认完成。
>
> ### 恢复手段(按顺序试)
>
> **1)重新挂载到新的追踪上下文**(终端还活着的情况,首选):
> ```bash
> orca orchestration task-create --run <run_id> --spec "<和原来一样的任务描述>" --json   # 建一个【全新】task
> orca orchestration worker-start --task <新task_id> --terminal <那个还活着的终端handle> --json
> ```
> 被吊销的 capability **救不回来**,只能用新 Dispatch 重新覆盖那个终端。挑终端 idle 的时刻做成功率最高。
>
> **2)提示词卡在输入框里没提交**(协调者本次真实遇到的形态):终端起来了、任务文字也在输入框里,但就是没回车。手动补一次提交:
> ```bash
> orca terminal show --terminal <handle> --json          # 先确认:输入框里有文字,但没在跑
> orca terminal send --terminal <handle> --enter --json   # 补一个空文本 + 回车
> ```
> 如果卡在 hooks 信任提示上(预览里能看到那个选择菜单),先 `--text "3"`(Continue without trusting)、等 2 秒、再 `--enter`,等 4 秒后再看一次;还停在空输入框就再补一个 `--enter`。**注意:提交成功之后,Orca 那边的原始 dispatch 仍然是 failed 状态**,还是要按 1)重新挂载才能恢复追踪。
>
> **3)追踪彻底救不回来时的兜底:直接盯终端状态,不再等 `worker_done`**
> ```bash
> orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 900000 --json
> orca terminal read --terminal <handle> --limit 100 --json
> ```
> 这是**协调者本次实际采用的办法**:`worker_done` 通道断了,就改为直接监控终端的 `tui-idle` 状态来判断完成。代价是失去结构化完成信号(要自己去读产物、读终端输出确认),但至少不会丢工作成果。
>
> **4)worker 一侧还有一条能用的通道:`status` 消息不受 capability 吊销影响**(本次实测有效)
> ```bash
> orca orchestration send --to run:<run_id> --type status \
>   --subject "<状态>" --body "<我还活着 / 我做完了,产物在 xxx>" --json
> ```
> `worker_done` 和 `heartbeat` 是 Dispatch 作用域的信号,capability 一吊销就全部拒收;但 `status` 是发给 Run 邮箱的普通消息,**照样能送达**。所以 worker 发现自己 capability 被吊销时,不要闷头做完就算了——**改用 `status` 把"我还活着"和"产物在哪"报回去**,协调者 `check` / `inbox` 能收到。
>
> ### 给协调者的纪律
>
> - `state: "failed"` 且 `last_failure` 是 `agent_prompt_stalled` 时,**默认先怀疑是误判**,先去看终端,再决定。
> - **心跳、终端有输出 = 活着,不等于做完了**;反过来,**没有心跳也不等于死了**——这个 bug 会让活着的 worker 一条心跳都发不出来。
> - 派发前如果能确认终端是 idle 的,撞到这个 bug 的概率会明显降低:
>   ```bash
>   orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
>   ```
>   往一个正在忙的终端上派新任务,基本就是在主动触发这个 bug。
> - 高负载(开了很多 worker、内存吃紧)会同时提高两件事的概率:撞上这个误判,和 Orca App 直接崩溃重启。这是 `agent_capacity.py` 红灯**真正**值得收敛的原因——不是因为它会拦你(它不会),而是因为过载会通过这条路径把你已经跑完的工作弄丢。

---

### 5.4 踩坑表同步加两行(第 165-173 行)

| 坑 | 后果 | 怎么避 |
|---|---|---|
| worker 报 `agent_prompt_stalled`/failed 就当它死了重派 | 它多半还活着;重派 = 同一份工作做两遍,或丢掉已完成成果 | 先 `worker-show` + `terminal show` 看真实预览;活着就等 60-90s 再用新 task + `worker-start --terminal` 挂载 |
| Orca App 高负载崩溃重启后继续等 `worker_done` | Dispatch capability 已失效,完成信号永远等不到 | 改用 `terminal wait --for tui-idle` 直接监控终端;worker 侧改发 `--type status` 到 Run 邮箱 |

---

## 6. 其他小改(不影响正确性,但值得顺手做)

1. **第 33 行**提到系统提示里的 `ORCA_AGENT_MEMORY_CONTEXT_PACK_V1`。这份文档号称"单独重读也能看懂,不依赖当时的对话上下文",这句却依赖读者能看到那次会话的系统提示。改成"会话开头系统提示里注入的那段记忆包"即可,不要点具体 token 名。
2. **第 5-7 行**的 `ORCA_CONTEXT_NACK_V1` 免责声明写得很好,建议保留,但可以加半句说明这是**什么**("Orca 的项目知识图谱/上下文桥这次没加载成功,所以本文基于现取的版本匹配 CLI 指南而非那份索引"),现在读者不知道这个大写 token 是什么。
3. **第 25 行**"几乎所有命令支持 `--json`"——实测 231 个命令,`--json` 覆盖率确实很高,表述准确,保留。
4. **第 51 行**"任何删除/覆盖操作前必须先看一眼目标内容"是好规则,但放在"不能做/需要你许可"里有点错位(这是**做法**不是**权限**)。建议挪到治理护栏那一段。
5. **`orca artifacts` 在 `orca --help` 顶层列表里是隐藏的**(实测 `orca --help | grep -i artifact` 无输出,但 `orca artifacts --help` 正常工作,有 share/update/unshare/list/delete 五个子命令)。文档 1.3 提到了 Artifact 但没给命令,如果要给,顺便提醒这个命令查不到帮助不代表不存在。
6. **阶段 6 建议加一条实操收尾**:大项目结束时把这一轮真实踩到的新坑写回记忆层/教程本身。文档第 45 行在 1.2 里提到了这个闭环,但阶段 6 的收尾清单里没有落实——而第 5 节这一整节的存在,本身就是这个闭环的产物。

---

## 7. 建议的落地优先级

| 优先级 | 条目 | 为什么先做 |
|---|---|---|
| **P0** | P0-1 修阶段 4 的 `check --wait` 循环 + `--ack` | 照抄跑不通,是硬故障 |
| **P0** | P0-2 阶段 0 补 Experimental 开关 | 不开的话整个阶段 4 用不了 |
| **P0** | 第 5 节 新增阶段 4.5 | 会让阶段 4 的默认假设失效,后果是丢工作成果 |
| **P1** | P1-1 容量红灯自相矛盾 | 同一份文档两处打架,且违背当天刚定稿的全局规则 |
| **P1** | P1-4 终审 max vs xhigh | 会让读者在唯一强制 max 的复核上白白降档 |
| **P1** | P1-2 / P1-3 / P1-5 / P1-6 | 事实错误 + 目录漏项 + 定位错位,改动量都很小 |
| **P2** | P2-1 / P2-2 / P2-3 | 三个"读者一定会卡在这里"的具体空白 |
| **P2** | 其余 P2 | 按顺手程度做 |

---

## 附:本次复核实际跑过的只读命令(可复现)

```
orca --version / --help
orca agent-context / --json                 (231 commands, schema v1)
orca status --json
orca repo --help / repo add --help / repo set-base-ref --help / repo list --json
orca worktree --help / worktree create --help / worktree ps --help
orca worktree ps --json / worktree ps --limit 500 --json / worktree list --json
orca terminal --help / terminal create --help / terminal wait --help / terminal send --help
orca orchestration --help
orca orchestration {run-create,task-create,task-list,task-update,worker-start,worker-show,
                    worker-release,check,send,ask,dispatch,inbox,reply} --help
orca orchestration run-list --json
orca orchestration worker-show --dispatch ctx_1dfe5556c0dd --json
orca artifacts --help / artifacts share --help
orca skills list / skills get orchestration / skills get orca-cli
python3 orca-context-bridge/scripts/agent_capacity.py
sysctl -n hw.logicalcpu hw.physicalcpu hw.ncpu
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" /Applications/Orca.app/Contents/Info.plist
```

唯一一次**写**操作:`orca orchestration send --to run:<run_id> --type status`,用于在 capability 被吊销后向协调者报活口 —— 这本身也验证了第 5.3 节恢复手段 4)确实有效。没有创建/删除任何 worktree、terminal、task 或 dispatch,没有修改被复核文档。
