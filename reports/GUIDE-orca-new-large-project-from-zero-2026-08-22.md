# Orca + Claude 多 Agent 系统教程:从系统全景到从零建大型项目不踩坑

> 写给**已经会基本操作 Orca 的人**(会开项目、会跟一个 agent 对话、大概知道 worktree 是什么)。
> 目标是:从零登记一个大型项目、先搭骨架、再按独立性并行,以及这台机器上已经验证过的编排坑。
> 完全没碰过 AI、不会写代码的人,请先看《写给完全没碰过 AI 的你》(`GUIDE-ai-beginner-first-time-2026-08-22.md`),这份不是给零基础读的。
>
> **对照运行时**:Orca App v1.4.187(以 `orca status --json` 的 `result.runtime.appVersion` 为准,不要死记这份文档里的版本号)。
> **命令语法**以 `orca skills get orca-cli` / `orca skills get orchestration` 现取的版本匹配指南为准,不要背这份文档里的 flag——Orca 更新后 flag 会变,教程只保证今天写的这一份是对的。
>
> v2 · 2026-08-22,经 Claude opus(max)、Codex sol(max)、Grok 三方独立复核后修订。三方共同确认的核心问题:
> 阶段 4 的并行示例照抄跑不通(一次 `check --wait` 收不齐多个 worker,缺 `--ack` 循环)、缺 Settings→Experimental 这个必需前置开关、
> 骨架 worktree 示例缺 `--no-parent`(会把新项目挂成当前对话所在 worktree 的子节点,污染侧边栏)、
> 把 Orca orchestration、对话内 Workflow 工具、`codex-pool` MCP 三套不同系统写成了一套,
> 以及必须补一节记录本轮复核自己真实踩到的 bug(dispatch 卡在输入框 / capability 被吊销 / App 高负载崩溃重启)。

## 目录

- **第一部分 · 系统全景**——这套系统到底由哪几层组成,一次请求是怎么穿过这些层的,我能做什么/不能做什么
- **第二部分 · 从 0 开始建一个大型项目**——名词、开工自查、注册项目、拆解规划、搭骨架、并行、运行时坑、复核节奏、收尾
- **第三部分 · 举例**——获客软件网站,「短视频创作」+「抖音获客」两块怎么同时进行不冲突
- **第四部分 · 追问**——100 个任务同时进行呢
- **第五部分 · 追问**——有必要再加一个 Grok 吗

---

# 第一部分 · 系统全景

## 1.1 分层地图:这套系统到底由哪几层组成

把你每次提需求到我动手之间发生的事拆成六层,从下到上:

1. **硬件/账号层**——这台 Mac、加密 SSD 卷、Claude/Codex 账号的用量配额、SSH 到服务器的网络路由规则。这层是所有上层的天花板,配额打满、内存耗尽、卷没解锁,上面几层的"正确操作"都会失败。
2. **编排层:Orca App**——桌面运行时,管理仓库(repo)/工作副本(worktree)/终端(terminal)/内置浏览器/自动化(automations)/产物发布(artifacts)/技能分享。它是整个系统的"状态权威":谁在哪个项目、哪个分支、开着哪些终端,都由它记账。通过 `orca` CLI 操作,几乎所有命令支持 `--json`。它自己**不写代码**,只负责把 agent 摆在正确的位置。
3. **执行层:Agent CLI**——真正干活的是跑在某个终端里的程序:`claude`(Claude Code,也就是我)、`codex`(Codex CLI)、以及 omp/pi/grok 等其他 TUI agent。这台机器上还给 Codex 挂了一层"按模型/算力分 bulk/design/fast/work 四档"的路由——**这是这个 Claude session 的技能路由约定,不是 Codex CLI 本身的属性**,换一台机器、换一个协调者不一定有这层路由。
4. **协调层:Orca Orchestration**——多个 agent 需要互相配合、有依赖关系、需要等结果时才用得上。核心概念是 Run(命名空间/收件箱)→ Task(工作项)→ Dispatch(某次尝试分配给某个终端)。它提供"记账 + 消息传递",不做调度决策——并发数开多少、放哪个 worktree,都要调用方(我)自己判断。这是**唯一有 Orca 完成收据**(`worker_done` 能被 `check --wait` 等到)的编排系统,见下面 1.1.1。
5. **能力扩展层:MCP servers**——这些是**这个 Claude session** 额外挂载的工具,不是所有 Orca 用户/所有 session 都有:`codex-pool`(直接把任务派给 Codex,不用走完整 orchestration、不产生 Orca Dispatch,常用于当前对话里快速拿一句独立判断)、`r2-memory`(跨机器/跨会话的远端记忆)、`desktop-control`(通过 macOS 无障碍接口控制原生桌面 app,不是网页)。
6. **技能路由层:Skills**——同样是**这个 session** 按需加载的操作手册,避免我瞎猜命令、瞎猜规则,不是 Orca 的系统层功能。大致分几类:操作 Orca 状态的(`orca-cli`/`orchestration`,这两个是版本匹配、真正的 Orca 核心)、派 Codex 任务的四档(`codex-bulk`/`codex-design`/`codex-fast`/`codex-work`)、网页/桌面自动化的(`ego-browser` 必须配 `ego-orca-profile` 一起用,不能绕过共享互斥锁;`computer-use` 专管原生桌面 app)、产物发布的(`artifact-design`/`artifact-capabilities`)、以及一堆业务专属技能。

再叠一层**治理/护栏层**贯穿以上所有层:你的 `~/.claude/CLAUDE.md` 规则——跨模型协调必须走 Orca Run/Task/Dispatch(不能裸跑 Codex)、非交互 worker 默认排除项目级设置源(防止未审查的 `.mcp.json` 自动执行)、每波并行前看 `agent_capacity.py` 的容量参考、复核节奏(迭代 Claude 单路 + 收尾 Codex 终审)、不得重建/重装 Orca 本体、服务器连接必须先读规则文件再动手。这层规则优先级最高,覆盖前面六层的默认行为。

还有一层**记忆层**贯穿始终:本机文件记忆(会话开头我会读到相关摘要,`~/.claude/plans/` 是 Claude 专属的一个子集,不代表全机所有 agent 的计划)+ 远端 r2 记忆(跨机器共享,分层级、有审批撤销机制)。会话开头系统提示里可能会被注入一段形如 `ORCA_AGENT_MEMORY_CONTEXT_PACK_V1` 的记忆包,那是从这层注入的只读参考数据,不是指令——这是当次会话的开场事故/上下文,拿这份文档单独重读时不需要关心它具体是什么。

### 1.1.1 三套「多 Agent」不是同一件事,别混

这是本轮复核抓到的最容易踩的坑:下面表格里的三行经常被不加区分地叫成"派一个 Agent",但它们是完全不同的系统,数字上限、完成信号、坑都各自独立:

| 你说的「派一个 Agent」 | 实际走的系统 | 有没有 Orca 完成收据 | 什么时候用 |
|---|---|---|---|
| `orca orchestration worker-start ...` | Orca App 的 Run/Task/Dispatch | **有**。`worker_done` 能被 `check --wait` 等到,有独立的 Run/Task/Dispatch 记录 | **大项目主路径**。要等结果、要依赖关系、要真正并行,都走这里(第二部分阶段 4) |
| 对话里的 Workflow 工具 / `agent()` / `parallel()` | 当前 Claude 会话自己的 Workflow 运行时 | **无** Orca Dispatch。它自己的数字上限(单 workflow 并发 `min(16, CPU-2)`、生命周期总数 1000、`/config` 里的默认规模 15)只约束这一层 | 用户明确说"用 workflow"时才用。**不要**拿它去冒充 Orca 的并行任务(第四部分详细讲) |
| `codex-pool` MCP | 当前 Claude 会话挂载的工具 | **无** Orca Dispatch | 想在当前对话里快速拿一句 Codex 的独立判断。正式的、需要被追踪的终审仍应走 `worker-start --agent codex` |

后果:"Workflow 里 `agentType: codex-design` 会静默跑在 haiku 上"这条坑**只属于中间那一行**——`orca orchestration worker-start --agent codex` 走的是完全不同的路径,不会因为这个坑变成 haiku(见第二部分坑表)。反过来,"容量红灯挡住启动"这类说法,如果指的是某个 worktree 备注里协调者自己主动停的记录,那也不是 Orca 系统层面的硬拦截——见阶段 4 的容量说明。

## 1.2 一次典型请求,这几层怎么联动

用你已经问过的"建获客软件网站"串一遍全链路:

1. 你提需求 → 我先查记忆层(有没有人做过类似的、`~/.claude/plans/` 里有没有冲突的计划)
2. 涉及新项目 → 走 Orca App 的 repo/worktree 层注册项目、开骨架 worktree
3. 骨架 worktree 里跑的是 Claude Code(执行层)在写代码
4. 骨架确认后要并行 → 走 Orchestration 协调层建 Run/Task/Dispatch,把两个模块分给两个新 worktree 各自的 Claude
5. 过程中要复核 → 我自己(Claude opus/max)先做只读复核,收尾前走 Orca orchestration 拉起一个真正独立的 Codex 终端做终审(不是 `codex-pool`,见阶段 5)
6. 全程受 CLAUDE.md 护栏约束:容量门禁看 `agent_capacity.py`、不能裸跑未审查的项目级 MCP、涉及 SSH 要先读服务器规则
7. 关键决定/习惯被记下来 → 写回记忆层,下次同类任务不用你再重复交代一遍

## 1.3 我在这套系统里能做什么、不能做什么

**能做:** 调用 `orca` CLI 操作真实状态(repo/worktree/terminal/orchestration);发起子 agent 做只读探索或编写;通过 Workflow 工具做确定性多 agent 编排(需要你显式发起,比如说"用 workflow",或者触发 ultracode);通过 codex-* agent 拿 Codex 的独立判断;读写你的本机记忆文件。

**不能做/需要你许可:** 重建、重写、重装或替换 Orca 本体;在你没有明确要求编排的情况下发起大规模多 agent 扇出;发布公开 Artifact 分享链接(设备级开关需要你在 Orca App 设置里手动打开,我没有 CLI/RPC 途径绕过);SSH 连接不能猜参数、不能削弱主机密钥检查(`alexsw` 密码兜底是你明确要求过的唯一例外,不能推广)。

**一条做法(不是权限规则,但同样重要):** 任何删除/覆盖操作前必须先看一眼目标内容再动手。

---

# 第二部分 · 从 0 开始建一个大型项目,不踩坑

## 名词先搞清楚(30 秒)

| 词 | 意思 |
|---|---|
| 仓库(repo) | Orca 里的"项目",一个 git 仓库,注册后拿到一个 UUID(`repoId`) |
| worktree | 这个项目下的一个独立工作副本,可以同时开很多个,互不干扰。完整 id 是 `<repoId>::<绝对路径>`,只抄 repoId 是不完整的 |
| worker / agent | 在某个 worktree 的某个终端里跑的 Claude / Codex 实例 |
| Run / Task / Dispatch | Orca orchestration 的记账系统:Run 是命名空间/收件箱,Task 是工作项,Dispatch 是"某次尝试把某个 Task 分配给某个终端"。只有需要**等结果、协调多个 agent** 时才用得上,而且**必须先在 Orca App 的 Settings → Experimental 里打开 orchestration 实验特性**,这是设备级开关,CLI 打不开 |

---

## 阶段 0:开工前 30 秒自查(不是可选项)

大项目一旦开始,后面每一步的成本都会被放大,这一步省下来的时间远小于踩坑赔进去的时间。

1. `orca status --json` —— 确认 Orca App 在跑,没跑就 `orca open --json`
2. 确认**没有别的 session 已经在做同一件事**:
   ```bash
   ls ~/.claude/plans/
   # worktree ps 会返回全部 worktree(这台机器现在 144 个,原始 JSON 25 万+字节,
   # 直接塞给对话窗口大概率被截断),只看真正在干活的:
   python3 -c "
   import subprocess,json
   r=json.loads(subprocess.run(['orca','worktree','ps','--json'],capture_output=True,text=True).stdout)['result']
   for w in r['worktrees']:
       if w.get('status')=='working':
           print(w['displayName'], '|', w['branch'], '|', w.get('preview','')[:80].replace(chr(10),' '))
   "
   ```
   这不是走过场:之前就有一次真实发生过重复劳动,一个并发 session 重新调查了另一个 session 已经做完并部署的工作,白做了一遍。`~/.claude/plans/` 只是 Claude 这一个 agent 的计划目录,不代表全机所有 agent 的计划,两个都要看。
3. 如果这个大项目涉及服务器/SSH/部署,先完整读一遍服务器连接规则文件,再跑对应的静态校验脚本——不要凭记忆猜连接参数。
4. **第一次用 orchestration(阶段 4 会用到)前,确认 Orca App 的 Settings → Experimental 里 orchestration 实验特性是开的。** 这是设备级开关,CLI 打不开;没开的话阶段 4 全部 `orca orchestration ...` 命令都用不了,而且报错未必直接告诉你"去开这个开关"。(顺带:发布公开 Artifact 链接是另一个同类的设备级开关,在 Settings → Artifacts,同样只能人工开。)

**抓 `orca ... --json` 输出的标准姿势**(Bash 直接 `2>&1` 抓大概率不完整——命令输出可能超过对话工具的显示上限,`check --wait` 还会往 stderr 每 15 秒喷一条 `_keepalive` 心跳,混进 stdout 解析必炸):

```python
import subprocess, json
def orca(*args):
    p = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True)
    return json.loads(p.stdout).get("result")

print(orca("worktree", "ps"))
```

---

## 阶段 1:注册项目

**已有本地 git 仓库:**
```bash
orca repo add --path /abs/path/to/project --json      # 从返回 JSON 的 result.id 拿到 repoId(UUID)
orca repo set-base-ref --repo id:<repoId> --ref origin/main --json   # 只在有远程时做,全新本地仓没有 origin/main,这行会失败
```
`orca repo add` 只接受**已经是 git 仓库**的路径,不会替你 `git init`。

**全新项目(本地还没有 git 仓库):**
```bash
cd /abs/path/to/new-project
git init -b main
# 确保有真实可提交的内容(空目录 git add -A 之后没东西可提交,commit 会失败);
# 建议至少有 README/.gitignore,别让 .DS_Store、后续的 node_modules/.env 混进首个 commit
git add -A
git diff --cached --check
git commit -m "chore: initialize project"
orca repo add --path /abs/path/to/new-project --json
```
Orca CLI 没有"从零建仓"的命令,`git init` 这步必须自己先做。代码已经在远端(GitHub 等)时可以省掉手动 `git clone`:`orca project setup-clone --project <id> --host <host-id> --url <clone-url> --destination <path> --json`。只是一堆文件、暂时不想上 git 也可以:Orca 支持 `--kind folder` 的 folder workspace,但 worktree 那一套并行能力是建立在 git 之上的,大项目还是尽早 `git init`。

---

## 阶段 2:大项目先拆解,别一上来就写代码

这是大项目和小任务最大的区别。直接让 agent "把这个大项目做出来" 几乎必然导致范围失控、返工、多 session 重复劳动。正确做法是先产出一份**里程碑计划**(M0、M1、M2……),每个里程碑范围清楚、有明确的"完成"定义,写成文件固化下来(不要只留在对话里,防止别的 session 或者你自己以后忘了已经想过什么)。

```bash
orca worktree create --repo id:<repoId> --name plan-and-skeleton \
  --no-parent \
  --agent claude --setup run \
  --prompt "先做需求拆解,产出 M0..Mn 里程碑计划,写到 reports/PLAN-xxx.md;
每个里程碑写清楚范围、验收标准、依赖关系;骨架代码搭好后停下等我确认,不要自己往后接着做" \
  --json
```

**`--no-parent` 这个 flag 不能省**——`orca worktree create` 默认会在能推断当前上下文时,把新 worktree 记成**你发命令时所在的那个 worktree** 的子节点,不是"新 repo 的子节点"。如果你是坐在一个已有的项目对话里(比如这个仓库自己的 `完善orca` worktree)给刚注册的全新项目开骨架,不加 `--no-parent` 会把新项目挂成当前项目的子 worktree,污染侧边栏。`--repo id:<repoId>` 只决定 git 检出落在哪个仓库,**不决定 Orca 的父子关系**。

计划文件写完之后,**先确认一遍再往下走**——这一步花 5 分钟检查,能省掉后面几个里程碑跑偏后的返工。

---

## 阶段 3:先用一个 worktree 把骨架搭起来,别一上来就开多个 agent

大项目最容易犯的错是"计划一出来就立刻拆成 5 个 worktree 并行开干"。实际应该是:先用**一个** worktree 把项目骨架(目录结构、基础配置、CI、核心接口)搭好、提交,这是后面所有并行任务的公共地基。地基没定,并行的几个 agent 各自基于不同假设写代码,后面合并时冲突成本比串行慢多了。

骨架确认没问题后,记录一个明确的停点再往下走:

```bash
git status --short
git rev-parse HEAD
orca worktree current --json
```

记下骨架 commit、完整 worktree id(`<repoId>::<绝对路径>` 这种形态)和预期的 git 基线,三者一致了再开始拉并行 worker——未提交的改动不会神奇地出现在新 worktree 里。

骨架稳定之后,再判断:各个里程碑之间**是否真的相互独立**(不改同一批文件、不依赖彼此未完成的接口)。独立的才适合并行,不独立的按依赖顺序串行做,或者用带依赖关系的 Task DAG:

```bash
orca orchestration task-create --spec "M1 骨架扩展" --task-title "M1" --json     # 记下返回的 task id
orca orchestration task-create --spec "M3 整合联调" --task-title "M3" \
  --deps '["<id_M1>","<id_M2>"]' --json          # --deps 是 JSON 数组字符串,元素是已存在的 task id,不是逗号分隔
orca orchestration task-list --ready --brief --json   # 只列依赖已满足、现在可以派的
```
任务状态只有这几种:`pending / ready / dispatched / completed / failed / blocked`。`task-list --ready` 是协调者的外部记忆——大项目任务多,别靠自己脑子记"哪个能派了"。合法的 `worker_done` 会自动把 task 和 dispatch 标成 completed,**不要**再手动 `task-update --status completed`。

---

## 阶段 4:真的要并行时,按这个顺序做

先把 Run 和全部独立 Task 一次建完,再一次性拉起全部 worker,最后**滚动等待**——不要"建一个等一个"(那样并行度归零),也不要指望"一次长 `check --wait` 就能等完整个模块"(下面会讲为什么不行)。

**这些命令必须在 Orca 自己的终端里跑**——Orca 靠 `ORCA_TERMINAL_HANDLE` 这类环境变量自动识别"你是谁",省掉了每条命令都要写的 `--from`。在系统 Terminal.app / iTerm 里跑,`run-create` 绑不到协调者、后面的 `check` 也取不到东西。

```bash
orca orchestration run-create --objective "<大项目目标>" --json

orca orchestration task-create --spec "<M1 任务描述>" --task-title "M1" --json
orca orchestration task-create --spec "<M2 任务描述>" --task-title "M2" --json

# new-child 的"父"是发命令的这个 Orca 上下文,不是 --repo 指向的那个 git 仓库。
# 人正坐在骨架 worktree 的对话里时,new-child 才是"骨架的孩子":
orca orchestration worker-start --task <id_M1> --worktree new-child --name m1-worker --agent claude --setup run --json
orca orchestration worker-start --task <id_M2> --worktree new-child --name m2-worker --agent claude --setup run --json
# 人还停在别的项目对话里(比如从"完善orca"这类治理 worktree 给全新项目派活),绝不能用 new-child,改成:
# orca orchestration worker-start --task <id_M1> --worktree new-top-level --repo id:<repoId> --name m1-worker --agent claude --setup run --json
```
派完先 `orca worktree ps --json` 确认新 worktree 的 `repo` 字段是对的,再往下走——派错 repo 的 worktree 后面清理起来很烦。

**滚动等,不是一次性等完**(这是本轮三方复核共同指出的最大硬故障——原样照抄下去,2 个 worker 只会收到第 1 个的完成消息,第二次再等还是同一批重放):

```bash
# 派发前如果复用已有终端,先确认它真的空闲:
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json

orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json
#   -> 处理这一批 Delivery 里的【每一条】消息:
#      question    -> orca orchestration reply --id <msg_id> --body "<答复>" --json
#      escalation  -> 先 worker-show / worker-read / terminal read,再决定
#      worker_done -> 在 ack 之前先决定这个终端归谁(复用还是释放,见下)
# 处理完整批再确认收货,并接着等下一批:
orca orchestration check --ack <delivery_id> --wait --types worker_done,escalation,question --timeout-ms 900000 --json
# 重复,直到你派出去的【每一个】Dispatch 都 settled 为止,不是"等够几轮"
```

关键点:
- `delivery_id` 从上一次 `check` 返回的 JSON 里取;**没有 `--ack` 就会一直重放同一批**,不是"又完成了一次"。
- `--types` 只决定**什么时候唤醒等待**,返回的仍然是最旧的完整一批,收到消息类型不在 `--types` 里的也要处理。
- `check --wait` 超时返回、或者 `{count: 0}`,**是一个检查点,不是 worker 失败**。真实编码任务跑 15-60 分钟很正常,这时候按顺序查:
  ```bash
  orca orchestration task-list --brief --json
  orca orchestration worker-show --dispatch <id> --json    # ready / failed / outcome_unknown
  orca orchestration worker-read --dispatch <id> --limit 50 --json
  orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
  ```
  有心跳、终端在滚动 = **活着**,不等于**做完了**;反过来没收到完成消息也不等于死了(见阶段 4.5)。别因为没收到完成消息就去 kill/restart。

**收到 `worker_done` 后,在 ack 之前就要决定这个终端归谁:**
```bash
# 复用:先从 worker-show 读出这个 worker 的终端 handle
orca orchestration worker-show --dispatch <dispatch_id> --json    # 读 worker.agent_terminal_handle
orca orchestration worker-start --task <下一个task_id> --terminal <那个handle> --json

# 或者释放(succeeded / failed 都要做,除非用户明确要留着调试)
orca orchestration worker-release --dispatch <dispatch_id> --json
```
`worker-release` 要的是 **`--dispatch <dispatch_id>`**,不是 terminal handle,裸写 `worker-release` 跑不起来。要留着调试用 `worker-retain --dispatch <id>` 明确记录,不要什么都不做地放着。**不要**因为超时、TUI 空闲、心跳、question、escalation 就去 release 一个 worker——那些都不代表它完成了。

**worker 出问题时**(先 `worker-show --dispatch <id> --json` 看真实状态,再决定,不要固定套路乱试):
- `ready` → 没事,继续等,或 `worker-read --dispatch <id> --limit 50 --json` 看它在干嘛。
- `failed` / `stopped` → 起替补:`worker-start --task <task> --retry-of <old_dispatch_id>`,并**显式重新指定** `--worktree` 和 `--agent`(retry 不会自动继承原来的落位)。**先看阶段 4.5**——`failed` 很可能是误判,不是真的失败。
- `outcome_unknown` → 要么 `worker-stop --dispatch <id>` 再看一次,要么 `worker-abandon --dispatch <id>`(注意 abandon **不做任何进程/文件系统动作**,资源可能还活着)。
- 同一个 task 连续失败 3 次,dispatch context 会熔断,task 直接标 failed——对"看起来卡住"的 worker 不要无脑重试满三次。
- `worker-stop` 只关那一个受监督的 agent 终端,**不会**删 worktree、不会关 setup 终端。

**每一波开始前**跑一次容量参考:
```bash
python3 "/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/agent_capacity.py"
```
脚本**顶层 `recommendation` 现在恒定是 `gate_removed`,不会拦你**——真正的信号在 `advisory_true_recommendation` 字段,red 时表示真实负载已经吃紧,这是参考,不是系统拦截。红灯时要主动收敛并发、把多出来的任务放 deferred,而不是硬按默认值上——不是因为它会拦你(它不会),而是因为过载会通过阶段 4.5 那条路径,真的把你已经跑完的工作弄丢。

依赖链别叠太深,不超过 3-4 层,链条太深并行度会被吃掉。

---

## 阶段 4.5:worker 明明在干活,Orca 却说它失败了

**这是当前版本(1.4.187)一个已知的、会真实发生的 Orca bug,而且它专门破坏阶段 4 教你的那个"等 `worker_done`"流程。** 大项目并行度一高就更容易撞到,必须知道——本轮三方复核过程中,三个复核 worker 里有两个自己就真实撞上了这个 bug。

### 症状

- `worker-start` 返回 `state: "failed"`,`stage: "dispatch_input"`,`lastError` 是 `agent_prompt_stalled`;
- 但去 Orca 界面上看那个终端,**它活得好好的,正在正常干活**(有转圈动画、有真实工具调用输出);
- 之后这个 worker 真的做完了、去发 `worker_done`,收到的是 **`Dispatch <id> capability is revoked.`**;
- 结果:一个货真价实、已经跑完的 worker,**永远无法通过被追踪的正式通道确认完成**。

还有一个变种:**Orca App 在高负载下崩溃重启**,同样会导致进行中 Dispatch 的 capability 失效、`worker_done` 链路断掉。现象一样,处理方式也一样。

### 为什么会这样(知道机制才不会误判)

Orca 判断"任务提示词提交成功"的方式,是看终端的"忙碌计数器"在 5 秒内有没有增加。但这个计数器**只在"从不忙变成忙"的那一瞬间 +1**。所以如果派发的时候这个终端**已经在忙了**(上一轮还没跑完、刚从 hooks 信任提示里恢复出来等等),它只要一直忙着,就永远产生不了那个变化——Orca 于是判定"提示词卡住了",把这个 dispatch 标记为失败并**永久吊销** capability,而且**一旦判定 failed,没有任何代码路径能把它清回去**。这是结构性的误判,不是随机故障。

修复已经写好并通过双复核(`ee982fc0a8`,在 `完善claude` worktree),但**没有 build、没有安装进正在跑的 App**——按"不得重建/重装 Orca.app"的规则,当前这个 1.4.187 仍然会撞到,这一节写的是 workaround,不是"已经修好了"。

### 关键判断:先分清"真死"还是"假死",千万别直接重派

**这是最重要的一步。** 直接重派一个其实还活着的 worker = 同一份工作被并行做两遍,或者已完成的成果被丢掉。

```bash
orca orchestration worker-show --dispatch <dispatch_id> --json    # 看 status / last_failure / capability_revoked_at
orca orchestration worker-read --dispatch <dispatch_id> --limit 50 --json   # 它到底在不在干活
orca terminal show --terminal <handle> --json                     # 看原始终端预览
```

预览里如果有转圈动画、"Working (Ns…"、真实的工具调用输出——**它是活的**,`failed` 是误判。这时:
- **不要**打断它,**不要**立刻重派,**不要** `worker-release`;
- 等 60-90 秒,趁它两轮之间空下来的那一刻再挂载(见下);
- 连试 3-4 次挂不上、但终端明显还在干活,这是可以接受的——直接放弃"挂回追踪",改用下面的兜底方式确认完成。

### 恢复手段(按顺序试)

**1)重新挂载到新的追踪上下文**(终端还活着的情况,首选):
```bash
orca orchestration worker-start --task <sameTask> --retry-of <oldDispatch> --terminal <handle> --json
```
被吊销的 capability **救不回来**,只能用新 Dispatch 重新覆盖那个终端。`--retry-of` 只连接 lineage,**不继承** `--worktree/--agent/--terminal/--on`,这些要重写。挑终端 idle 的时刻做成功率最高。

**2)提示词卡在输入框里没提交**(这是最常见的现场形态:终端起来了、任务文字也在输入框里,但就是没回车):
```bash
orca terminal show --terminal <handle> --json          # 先确认:输入框里有文字,但没在跑
orca terminal send --terminal <handle> --text "" --enter --json   # 补一个空文本 + 回车
orca terminal read --terminal <handle> --json           # 确认开始工作了
```
如果卡在 hooks 信任提示上(预览里能看到那个选择菜单),先 `--text "3"`(Continue without trusting)、等 2 秒、再补一次空文本 + 回车。**注意:提交成功之后,Orca 那边的原始 dispatch 仍然是 failed 状态**,还是要按上面 1)重新挂载才能恢复追踪。

**3)追踪彻底救不回来时的兜底:直接盯终端状态,不再等 `worker_done`**
```bash
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 900000 --json
orca terminal read --terminal <handle> --json
```
这是实际可行的办法:`worker_done` 通道断了,改为直接监控终端的 `tui-idle` 状态判断"这一轮交互回到提示符"。但 `tui-idle` **只说明输入框空闲,不是任务完成、更不是验收通过**——必须再读完整输出、产物文件、测试结果确认,代价是失去结构化完成信号,好处是至少不会丢工作成果。

**4)worker 一侧还有一条能用的通道:`status` 消息不受 capability 吊销影响**(本轮复核实测有效):
```bash
orca orchestration send --to run:<run_id> --type status \
  --subject "<状态>" --body "<我还活着 / 我做完了,产物在 xxx>" --json
```
`worker_done` 和 `heartbeat` 是 Dispatch 作用域的信号,capability 一吊销就全部拒收;但 `status` 是发给 Run 邮箱的普通消息,**照样能送达**。worker 发现自己 capability 被吊销时,不要闷头做完就算了——改用 `status` 把"我还活着"和"产物在哪"报回去,协调者 `check` / `inbox` 能收到。

### 给协调者的纪律

- `state: "failed"` 且 `last_failure` 是 `agent_prompt_stalled` 时,**默认先怀疑是误判**,先去看终端,再决定。
- **心跳、终端有输出 = 活着,不等于做完了**;反过来,**没有心跳也不等于死了**——这个 bug 会让活着的 worker 一条心跳都发不出来。
- 派发前如果能确认终端是 idle 的,撞到这个 bug 的概率会明显降低——往一个正在忙的终端上派新任务,基本就是在主动触发这个 bug。
- 高负载(开了很多 worker、内存吃紧)会同时提高两件事的概率:撞上这个误判,和 Orca App 直接崩溃重启。这是 `agent_capacity.py` 红灯**真正**值得收敛的原因。
- App 崩溃重启后,不要继续沿用旧的 runtime/handle:
  ```bash
  orca status --json                                             # 对比新的 _meta.runtimeId
  orca terminal list --worktree id:<repoId>::<绝对路径> --json    # 重新拿替代 handle,只用新的,不要新旧一起发
  ```
  如果新协调者终端没有绑定 Run,先 `orca orchestration run-use --id <runId> --json` 显式绑回去。

### 坑表同步补充(见本部分末尾附表)

| 坑 | 后果 | 怎么避 |
|---|---|---|
| worker 报 `agent_prompt_stalled`/failed 就当它死了重派 | 它多半还活着;重派 = 同一份工作做两遍,或丢掉已完成成果 | 先 `worker-show` + `terminal show` 看真实预览;活着就等 60-90s 再用 `--retry-of` + `worker-start --terminal` 挂载 |
| Orca App 高负载崩溃重启后继续等 `worker_done` | Dispatch capability 已失效,完成信号永远等不到 | 改用 `terminal wait --for tui-idle` 直接监控终端;worker 侧改发 `--type status` 到 Run 邮箱 |

---

## 阶段 5:复核节奏——大项目尤其别省

- **迭代阶段**:每个里程碑内部的修修改改,用 Claude opus + max 单路只读复核就够,不用每轮都拉 Codex。
- **每个里程碑完成、准备合并时**:值得先做一次内部复核确认没有遗留 P0/P1。
- **整个项目判定全部完成、即将合并/发布/安装/部署前**:必须额外加一轮 Codex `gpt-5.6-sol` + `max` 独立只读**终审**,prompt 首行带 `[强制双复核]` 标记。

**派发路径要注意区分,不同路径能拿到的档位不一样**(这一条本轮三方复核一致认为原文写错了方向):

- **走 Orca orchestration(推荐,也是全局规则要求的路径)**:
  ```bash
  orca orchestration worker-start --task <task_id> --worktree current \
    --agent codex --model gpt-5.6-sol --effort max --json
  ```
  这条路**真的能拿到 `max`**,不受 `xhigh` 上限约束——本轮复核实测确认(状态栏显示 `gpt-5.6-sol max`),这个仓库自己的 `prime-agent-dual-review.js` 默认值也是这个组合。前提是 `orca status --json` 的 capabilities 里有 `orchestration.worker-launch-preferences.v1`;`--effort` 必须配 `--model`,且这两个都**不能**和 `--terminal` 一起用(只对新起的 agent 终端生效)。
- **走 `codex-pool` MCP**:目前硬顶在 `xhigh`,拿不到 `max`,只适合日常对话里快速拿一句判断,**不适合这一轮终审**。
- `orca worktree create --agent codex` 这条路**完全不接受** Codex 的 `--model` / `-c model_reasoning_effort=...`,只能起默认档——终审别用它。

终审发现真实 P0/P1,修完必须对新候选重新走一遍终审,不能拿旧结果顶替、不能跳过。

---

## 阶段 6:收尾/验收

P0/P1 清零是**必要条件,不是充分条件**。大型项目至少还要过这张表:

- [ ] **冻结精确候选**:记录 commit/tree hash、有没有未提交的改动;复核期间代码如果又变了,旧的复核结论就失效了。
- [ ] **在合并后的候选上**跑测试、lint、typecheck、build(仅在获授权时)、迁移检查——记确切命令和退出码,别把"跑过静态检查"和"真实跑通"混为一谈。
- [ ] **迁移/备份/回滚/权限**:数据库 schema 迁移、密钥授权、第三方 API 权限范围、数据合规,这些不是代码复核能覆盖的。
- [ ] **所有 Dispatch 都 settled**:预期的 Task 都完成、question/escalation 都处理完、Delivery 都 ack 过、每个 worker 都复用/retain/release 了,没有游离的终端。
- [ ] **不要重建、重写、重新打包、重装或替换任何已经在用的正式产物**——如果某个里程碑的目标恰好是改造一个已有的正式系统,默认只做可回滚的增量修复,build/install/replacement 需要用户再次明确授权。
- [ ] **最终发布/安装/部署仍需要人类明确授权**——复核 worker 给的 GO,不等于这一步的授权。

---

## 附:大项目专属的坑(小项目不明显,项目一变大就爆雷)

| 坑 | 后果 | 怎么避 |
|---|---|---|
| 复核小步高频("改一点发一轮") | 一件事拖成几十轮,真实发生过 8 轮、14 轮、65 轮才收敛 | 攒够一批改动再发一轮复核 |
| 报告文件散落根目录 | 项目一大,几十上百个未跟踪文件淹没真正的代码改动 | 统一放进 `reports/` 目录,及时清理一次性文件 |
| 多 session 并发做同一件事 | 白做一遍,已真实发生过 | 开工前扫 `~/.claude/plans/` 和 `orca worktree ps --json`(见阶段 0) |
| `orca ... --json` 用 `2>&1` 直接合并抓取 | 命令输出可能超出显示上限;`check --wait` 的 `_keepalive` 心跳混进 stdout 会让解析失败 | 用 python3 subprocess 只读 stdout(见阶段 0);要合并流就用 `jq 'select(._keepalive|not)'` 过滤 |
| 新项目骨架 worktree 忘了 `--no-parent` | 会挂成当前对话所在 worktree 的子节点,污染侧边栏(见阶段 2) | 独立顶层项目一律 `--no-parent`,或干脆用 `new-top-level --repo` |
| Workflow 里用 `agentType: codex-design` | 静默跑在 haiku 上,不是真 Codex——**这条坑只属于对话内 Workflow 工具**(见 1.1.1) | `orca orchestration worker-start --agent codex` 不会有这个问题;需要真 Codex 走 Orca orchestration |
| Codex worker 卡住就当它死了重试 | 可能只是卡在 Trusted Access 安全横幅确认上,也可能是阶段 4.5 那个 bug | 先查 `worker-show` 的 `preview` 字段(出现 `Trusted Access` 相关字样就换个模型或升级问人)再决定要不要重试 |
| worker 报 `agent_prompt_stalled`/`failed` 就当它死了重派 | 它多半还活着,重派 = 同一份工作做两遍或丢掉成果 | 见阶段 4.5 |
| Orca App 高负载崩溃重启后继续傻等 `worker_done` | Dispatch capability 已失效,完成信号永远等不到 | 见阶段 4.5,改盯 `tui-idle` |
| 把 `agent_capacity.py` 顶层 `recommendation` 当成会拦你 | 顶层现在恒定 `gate_removed`,不会拦;真信号在 `advisory_true_recommendation` | 看 `advisory_true_recommendation`,red 时自己收敛,不是等系统拦 |

---

# 第三部分 · 举例——获客软件网站,「短视频创作」+「抖音获客」两块怎么同时进行不冲突

**先分骨架和模块。** 骨架(路由框架、认证权限、核心数据库表、部署配置、通用 UI)必须先由一个 worktree、一个 agent 做完、提交——两个模块都要用,谁都不该单独碰。骨架稳了之后,两个模块才各自独立并行:
- 短视频创作:脚本生成、素材库、AI 文案、剪辑流程,自己的路由/组件/数据表
- 抖音获客:账号绑定、私信自动回复、线索抓取、CRM 导出,自己的路由/组件/数据表(如果本机已经有验证过鉴权的数据接口,在任务 spec 里写清楚路径给两个 worker 参考;这是具体项目的私有资产,不是所有读者都会有的东西——没有就当新依赖处理,别让两个 worker 各自发明一遍鉴权)

**代码结构提前物理隔离,这才是真正不冲突的关键**,不是靠 agent 自觉:
```
src/
  core/                   # 骨架,两个 worker 都不该改
  modules/
    short-video/          # 只有短视频那个 agent 会碰
    douyin-leads/         # 只有抖音获客那个 agent 会碰
```
最容易撞车的地方是**两边都想改同一个"路由注册表"/"菜单配置"文件**——骨架阶段就改成约定优于配置(自动扫描 `modules/*/routes.ts` 注册),而不是每加一个模块就手动改一行共享文件。数据库同理:表名按模块加前缀(`short_video_*` / `douyin_leads_*`),迁移文件各自独立。

**两模块之间的真实依赖单独处理**:如果短视频内容后续要喂给抖音获客发布,提前定一份简单契约(如"视频草稿 → 待发布队列"的数据结构),双方照契约写、谁都不用等谁;真正联调放在两边都做完之后单独起一个整合里程碑,不要塞进并行任务里。

**落到 Orca 操作**(骨架完成确认无误后,命令细节、`--ack` 循环、worker 出问题的处理方式和阶段 4 完全一样,这里只列关键任务描述):
```bash
orca orchestration run-create --objective "获客软件网站:短视频创作+抖音获客双模块" --json

orca orchestration task-create --spec "实现 modules/short-video:脚本生成/素材库/AI文案;只改 modules/short-video/** 和自己的迁移文件" --json
orca orchestration task-create --spec "实现 modules/douyin-leads:账号绑定/私信自动回复/线索抓取/CRM导出;只改 modules/douyin-leads/** 和自己的迁移文件" --json

# 人正坐在骨架 worktree 的对话里,new-child 才是"骨架的孩子"(见阶段 4 的 --repo 说明)
orca orchestration worker-start --task <id_shortvideo> --worktree new-child --name short-video-worker --agent claude --setup run --json
orca orchestration worker-start --task <id_douyin> --worktree new-child --name douyin-worker --agent claude --setup run --json

orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json
# 处理这一批的每一条消息,ack 后接着等下一批,直到两个 Dispatch 都 settled(见阶段 4)
```
任务描述里明确写"只改 modules/xxx/\*\*"——文字护栏配合目录物理隔离,双保险。两边都 `worker_done` 后别同时合并:先合一个到 main、跑测试确认骨架没被破坏,再合第二个;每个 child worktree 通常是独立 git 分支,合的是 git 分支,不是"Orca 卡片",不要让两个 worker 同时往同一个 main 快进。

---

# 第四部分 · 追问——100 个任务同时进行呢

## 现实里不会真的有 100 个 agent 同一秒都活着,这样设计本身就是错的方向

- 账号用量限额会先打爆,不用等到 100——真实碰到过 dispatch 过程中命中 Claude session usage limit,那时并发数远不到 100。
- 容量信号本身就在提醒收敛:`agent_capacity.py` 的 `advisory_true_recommendation` 字段是这台机器真实负载的参考(见阶段 4),red 时应该主动收敛,而不是等它拦你——脚本顶层 `recommendation` 本身不会拦。
- 这条数字上限**只属于对话内 Workflow 工具,不是 Orca orchestration 的属性**(见 1.1.1):单次 workflow 内 `agent()` 并发上限是 `min(16, 可用CPU核心数-2)`,超出自动排队;生命周期总 agent 数硬上限 1000。也就是说即使说"跑 100 个",Workflow 执行层面也从来不是 100 个同时活着,而是**100 个排队、`min(16, CPU核心数-2)` 个真正同时在跑**——这台机器 8 个逻辑核心,算出来是 **6 个**,不是 10-16,换机器前先自己按公式算一遍,别照抄数字。这个 session 当前默认规模上限是 15 个 agent(/config 里 Dynamic workflow size,默认 medium),要撑到 100 需要先明确调高。

## 正确心智模型

不是"100 个同时跑",是"100 个任务的队列 + 一个恒定的并发闸口"。把 100 个任务定义好扔进任务池,但只保持符合真实容量的并发数在跑,跑完一个补一个,而不是试图让 100 个同一时刻全部活着。

- **走对话内 Workflow**:`pipeline()`/`parallel()` 原生就是这个模型,不用自己写调度。
- **走 Orca orchestration**(大项目真正的路径):一次性 `task-create` x100 建好任务清单,但**只按本波准入数量分批 `worker-start`**,配合 `task-list --ready --brief --json` 当外部记忆——收到并处理完一个 settled Dispatch、释放/复用资源、重新读一次容量,再从 ready 列表里补一个。不要写成"100 个同时拉起",Orca orchestration 本身不自带自动调度器,补位循环要协调者自己维护。

## 这个量级下冲突和复核成本会快速上升,不是线性的

- 共享文件冲突点会明显增多,需要插件式架构(每个任务=独立叶子模块/文件,零共享文件改动)或每任务独立 worktree,合并顺序化处理。
- 依赖关系不能是扁平的 100 节点 DAG,要按依赖层级分波次(骨架 → 独立叶子任务的一波或几波 → 整合),深依赖链不超过 3-4 层。
- 复核要用维度化扇出+对抗性验证,不能人工线性过一遍 100 个任务。

## 成本是真实决策

100 个并发意味着真实 token 消耗和费用,建议先定预算上限(对话内 Workflow 支持按 `budget.total` 动态收缩深度),而不是无限跑到打满配额。

---

# 第五部分 · 追问——有必要再加一个 Grok 吗

**技术上零门槛**:这台机器上 Grok CLI 已装好(`~/.grok/auth.json` 文件存在),Orca `--agent` 也原生支持 `grok`,语法受支持。

**但不建议定成标准第三条腿**:
- 用户级 `~/.claude/CLAUDE.md` 里"双模型复核"那条规则只写死了 Claude opus+max + Codex sol+max,Grok 没有对应的路由规则/容量门禁,凭空加一个没有规则约束的模型,大概率重演之前 Codex 路由臃肿那次要花 4 轮才理清楚的过程。
- 真正反复踩的坑(复核轮次太长、文件散落、并发重复劳动)是流程卫生问题,不是"缺第三方意见",加模型只会让协调开销更重,跟规则从"每轮双路"改成"迭代单路+收尾终审"的方向相反。
- 两个独立模型对收尾终审通常已经够用,第三个模型的边际收益小、协调成本却是线性往上加的。

**什么时候值得用**:不改规则,按需临时拉一个——Claude+Codex 两边都吃不准、争议特别大、或单次高风险判断时,现拉一个当第三方参考。这属于**full handoff**,不是受监督的 Dispatch:对面是独立的 Grok 会话,不会自动带 orchestration preamble,不会发 `worker_done`。

```bash
orca worktree create --name grok-second-opinion --no-parent --repo id:<repoId> --agent grok \
  --prompt "<争议点简述,要求独立判断>" --setup run --json
```
不传 `--repo` 的话会建在你当前所在的 repo 里——临时拉个第三方意见时这通常正好是你要的,但值得写明。如果想让 Grok 走受监督的 Dispatch、要等 `check --wait` 收到 `worker_done`,改用 `worker-start --agent grok`,不能用带 `--prompt` 的 `worktree create`。

如果这种情况反复出现、Grok 真的多次补上 Claude+Codex 都漏掉的发现,那才是该把它正式写进规则的信号——先有真实反复出现的需要,再固化成规则,不要反过来。

(旁注:机器上另有一个独立的 `~/.grokbot` 目录/桌面 app,和这里说的 Grok 编码 CLI 不是一回事。)
