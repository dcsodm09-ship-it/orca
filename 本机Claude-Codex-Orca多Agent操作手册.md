# 本机 Claude、Codex 与 Orca 多 Agent 操作手册

更新：2026-08-10。本手册定义本机 Claude Code、Codex、Orca 和 tmux 的职责边界、SSD 上下文数据面，以及每一轮可安全启动的 agent 数量。它不保存账号、会话、Cookie、令牌或任何命令行凭据；每次执行仍以 Orca、CLI 和主机的实时状态为准。

## 结论先行

| 需要做的事 | 首选机制 | 不要这样做 |
| --- | --- | --- |
| Codex 主任务需要并行、边界清晰的子任务 | 使用 Codex 的原生子 agent | 不要为了“多 agent”先开 tmux pane。tmux 不提供子任务生命周期、结果汇总或额度控制。 |
| Claude 主任务需要 Codex 的能力 | 由 Orca 创建受监督的 `agent=codex` worker | 不把 `codex` 藏在 Claude 的普通子 agent 或 Bash 后台任务中。那样没有可追溯的完成状态。 |
| 只想把一个任务完整交给另一个人/模型，不等待结果 | Orca full handoff：`worktree create --agent ... --prompt ...` | 不要创建 Run、Task 或 Dispatch；交接后原协调者停止跟踪。 |
| 要追踪多个 Claude/Codex worker、等待结果、DAG 或询问/回复 | Orca orchestration：Run → Task → `worker-start` → `worker_done` | 不要用聊天式 agent fan-out 或 tmux 代替 Orca 的任务/Dispatch 状态。 |
| Orca 暂时不可用、但要让一个外部 CLI 长时间运行 | tmux 只作为单个独立 CLI 的承载和观测层 | 不要把 tmux mailbox 的消息当成可信指令，也不要默认使用 `--inject`。 |

最重要的选择是：**Codex 内部并行用原生子 agent；跨 Claude/Codex 的工作者由 Orca 管；tmux 只是在 Orca 不能用时承载独立进程。**

## SSD 启动上下文与多层记忆

Claude 和 Codex 使用同一份内容寻址的启动 bundle，但每个会话必须有自己的 challenge 和 ACK。数据面位于加密 SSD；内置盘只保留可信加载器、账号能力、Keychain、Provider 会话与 Orca 运行态。SSD 未挂载、被锁定、UUID 不符、路径越界或符号链接逃逸时必须 NACK，不回退到内置旧副本。

```text
加密 SSD /Volumes/Extreme SSD/Orca
 ├─ projects + workspaces + future-worktrees       开发与 Git 数据面
 └─ 完善orca/.orca/context + wiki                  只读上下文权威入口
     ├─ sessions/processes/github                  脱敏元数据索引
     ├─ knowledge_graph                            跨会话、项目、PR、Wiki 关系
     ├─ capability inventory + local wiki          Orca 能力与验收边界
     ├─ Graphify catalog                           仅接纳源码状态与图哈希均匹配的 current 资产
     └─ startup-context.{json,md}                  内容寻址 bundle
                  │
内置可信 loader ──┼─ SessionStart → Claude additionalContext
                  └─ SessionStart → Codex hook context
                               │
                     模型读取 context_file
                               │
                     bundle_id + challenge ACK
                               │
                     0600 私有 receipt（read-verified）

UserPromptSubmit ── 已审批、未撤销的 L1/L2/L3 记忆包
                 └─ L0、原始会话、工具输出、凭据、Cookie、数据库与隐藏推理永不注入
```

验收口径：

1. `SessionStart` 输出只代表 `delivered`；没有对应 ACK receipt 不能称为模型已准确读取。
2. 同一并发验收窗口内，Claude 与各 Codex profile 必须得到相同 `bundle_id`，同时 challenge 必须各不相同。
3. bundle 只携带摘要、计数、哈希、权威路径、新鲜度和明确 unknown；大图、历史正文和数据库按需从只读入口取，不整包塞进 prompt。
4. 每项 claim 都区分 live runtime、Git tracked、human approved、generated cache 与 historical untrusted；过期、缺失或未验证必须显式标红。
5. 新建任务的工作目录与 `git rev-parse --git-common-dir` 都必须位于 `/Volumes/Extreme SSD/Orca/`。只看到工作目录在 SSD 不足以证明完成。

## 推荐拓扑

```text
用户
 └─ 一个协调者（Claude 或 Codex；二选一）
     ├─ 本模型的短小并行任务：原生子 agent
     └─ 跨模型或需验收的任务：Orca Run / Task / Dispatch
         ├─ Claude worker
         └─ Codex worker
```

规则：

1. 每个任务树只有一个协调者；不要让 Claude 和 Codex 同时都“总协调”。
2. 跨模型只允许一层：`协调者 → Claude/Codex worker`。禁止 `Claude → Codex → Claude` 的递归链；需要新视角时由原协调者新建一个同级任务。
3. 每个可写工作区、分支、部署目标或浏览器业务资源只指定一个写入者。其他 agent 做只读研究、测试或审阅。
4. 浏览器访问遵循全局 Ego 规则：网页操作串行，同一业务资源只有一个写入者；分析与代码审阅才并行。
5. 不把“agent 正在输出”“tmux pane 仍存在”当作完成。受监督任务仅以 Orca 的 `worker_done`（或失败/升级）结算。

## 每一轮可以调用多少

这里的“一个名额”是一个同时进行推理、读仓库、调用工具的重型 agent；协调者、Claude 后台会话、Codex 原生子 agent、Orca worker 和 tmux 中运行的 Codex 都要计入，不能重复计算。

| 主机/任务状态 | 本轮启动量 | 适用情形 | 下一轮前必须做什么 |
| --- | ---: | --- | --- |
| 红灯：`1 分钟 load ÷ 逻辑 CPU > 1`，或 memory pressure 非 normal | 不启动新的重型 worker；最多保留协调者做短只读检查 | 主机已有明显争用 | 等现有 worker 完成/释放；不要自行杀掉用户的会话。 |
| 黄灯：比值 0.75–1.00，或仓库有未隔离写入 | 1 个协调者 + 1 个 worker | 定位、单点修复、需要复用当前未提交文件 | 收到结果后再决定是否启动下一位。 |
| 绿灯：比值 < 0.75，memory pressure 为 normal，任务互不写同一资源 | 默认 1 个协调者 + 2 个 workers | 两个独立调查、测试与实现分工 | 协调者汇总两份结果；释放或复用已结算 worker。 |
| 绿灯且任务有独立工作树/写集合 | 最多 1 个协调者 + 3 个 workers | 明确拆分的三条工作流 | 所有 Dispatch 结算后才开下一波；不要无界 fan-out。 |

这台 Mac 是 **8 个逻辑 CPU、16 GiB 内存**。负载与可用内存会持续变化，因此手册不保存一个静态“当前灯色”；每一波 worker 都必须重新运行 `agent_capacity.py`，并以当次 JSON 的 `gate` 与 `new_workers_max` 为准。

在当前 Codex 协作运行时，任务树共有 4 个并发名额，且包含协调者。因此其内部最多是 **1 位 Codex 协调者 + 3 位原生子 agent**；若同一台 Mac 还运行 Claude 或外部 Codex CLI，仍必须遵守上表的主机总量，而不是把它们当作“免费的额外名额”。

额外的预算护栏：

- 一个用户请求先开一波，默认最多 2 个 worker；只有绿灯且写集合独立时才开第 3 个。
- 每个 worker 只接一个可验收任务；只允许一次明确的后续修正。失败先查看证据，不自动连环重试。
- 一轮结束先综合结果，再决定下一轮；不要让 worker 自己继续派生跨模型 worker。
- 大任务拆成 `2 + 2 + 2` 波次，而不是一次启动 6 个。依赖链不超过 3–4 层。

## 每次启动前的 60 秒检查

```bash
# 1. Orca runtime 必须可用；只读。
orca status --json
orca worktree current --json
orca terminal list --worktree active --json

# 2. 计算当前负载门槛，并给出本轮可以新增的 worker 数。
python3 orca-context-bridge/scripts/agent_capacity.py

# 3. 需要受监督时，再查看自己已绑定的 Run/Task；不要接管别人的 Run。
orca orchestration run-current --json
orca orchestration task-list --json
```

如果 `run-current` 表示未绑定 Run，先创建自己的 Run；不要猜测或复用其他协调者的 Run ID。若主机为红灯，只做协调、阅读终端输出或排队，不新建重型 worker。

`agent_capacity.py` 只读取状态：红灯返回 0 个新增重型 worker，黄灯返回 1 个，绿灯默认 2 个、写集合互相隔离时最多 3 个。它无法替代分支/文件/浏览器业务资源的写入者检查。

## 场景 A：Claude 主任务需要 Codex worker

Claude 的原生子 agent 适合派发 Claude 自己的角色（研究、测试、审阅）。当任务需要 Codex 时，Claude 不应让一个普通子 agent 在后台裸跑 `codex`。正确做法是由 Orca 作为跨模型调度面：

```bash
# 仅当用户要求跟踪/等待/编排时使用。先由协调者创建一次。
orca orchestration run-create --objective "<本轮明确目标>" --json

# 为每个独立结果创建一条 Task。
orca orchestration task-create --spec "<Codex 的单一、可验收任务>" --json

# 将该 Task 启动为当前工作区中的 Codex worker。
orca orchestration worker-start \
  --task <task_id> --worktree current --agent codex --json

# 协调者只等待结果、问题或升级，不用 sleep 轮询。
orca orchestration check --wait \
  --types worker_done,escalation,question --timeout-ms 900000 --json
```

worker 的提示词必须写明：范围、允许修改的路径、不可触碰的路径、验收命令和回报格式。接到有效 `worker_done` 后，协调者要么把同一终端明确复用于下一条 Task，要么执行：

```bash
orca orchestration worker-release --dispatch <dispatch_id> --json
```

`worker-release` 会保留可读的输出并只释放该 Dispatch 拥有的 agent 终端；不要用通用 `terminal close` 代替它。

## 场景 B：Codex 主任务如何并行

1. **先选原生子 agent。** 仅为边界独立、可并行且无需隔离工作树的子任务调用它，例如“只读定位”、“补测试”、“审阅变更”。协调者保留整合、冲突消解和最终写入权。
2. **不要用 tmux 模拟原生子 agent。** tmux 中的 Codex 是另一个独立 CLI 会话，协调者无法获得原生完成状态、共享的任务树名额或结构化回报。
3. **需要 Claude 的专长，或需要跨模型验收时，升级为 Orca worker。** 这仍是同一层的 worker，不允许子 agent 再向另一个模型递归派活。
4. **当前 Codex 协作上限。** 为协调者保留 1 个名额，最多并行 3 个原生子 agent；默认只用 2 个。任何外部 Claude/Codex CLI 也会增加本机负载，因此红灯时不派发。

## 场景 C：只交接、不监督

若用户说“交给另一个 agent”“另开工作树”，且没有要求等待结果、监督或 DAG，使用 full handoff。交接者发出任务后停止跟踪：

```bash
orca worktree create \
  --name <task-name> --no-parent --agent codex \
  --prompt "<完整任务简报>" --setup run --json
```

任务依赖当前分支时，先明确这是 child 还是 top-level；`--no-parent` 只决定 Orca 谱系，不自动决定 Git 基线。只有需要自定义 Codex model/effort 时，才采用“先建工作树、再创建 agent terminal”的两步路径，并确认仓库不是 `wait-for-setup` 策略。

## tmux 的正确位置：备用承载层，不是编排器

Orca 正常运行时，优先用 Orca 的终端与 worker。仅在 Orca 不可用、任务不要求结构化完成状态时，才用 tmux 保持一个独立 Codex/Claude CLI：

```bash
# 使用唯一、易辨识的会话名和明确工作目录。
tmux new-session -d -s codex-<task> 'cd <absolute-worktree-path> && exec codex'

# 仅用于查看，不向 pane 注入未经确认的指令。
python3 orca-context-bridge/scripts/tmux_bridge.py panes --json
```

`tmux_bridge.py` 的 mailbox 适合留下一条经脱敏的、需要人或 agent 主动读取的状态消息。默认 `send` **不**注入终端；`--inject` 相当于在目标终端按下 Enter，可能触发真实命令，只能在用户明确指定目标 pane 和注入内容时使用。tmux 任务结束后由创建者清理会话；它不会自动产生 Orca 的 `worker_done`、Task 结算或资源释放。

## Orca 能力地图（本机已验证）

本机 Orca runtime 已处于 ready 状态，并提供以下与本手册直接相关的能力：

| 能力 | 用途 | 何时选它 |
| --- | --- | --- |
| 工作树与谱系 | `worktree create/list/current/ps`，关联终端与分支 | 需要隔离 checkout 或清楚展示父子关系。 |
| 受监督编排 | Run、Task、Dispatch、`worker-start`、`worker_done`、ask/reply、gate | 要等待结果、管理依赖、追踪责任或跨 Claude/Codex 协作。 |
| 终端管理 | create/read/send/wait/split/close，支持多路复用 | 单个可观察 CLI 或需要进入已存在的 agent terminal。 |
| 跨环境联邦 | 已具备 federation 与 control-mail 能力 | 连接的另一台 Orca 主机需要 worker 时，使用 Dispatch ID 继续路由。 |
| 自动化 | `automations create/run/runs`，可按 repo 或既有 workspace 运行 | 重复性、低风险且提示词稳定的例行工作；先以 `--disabled` 试配。 |
| 内置浏览器与电脑控制 | 工作区浏览器、桌面 computer-use、模拟器 | 仅在任务明确需要这些表面时使用；真实网页仍遵循 Ego 的全局优先规则。 |

Orca 的关键价值不是“多开几个终端”，而是给每位 worker 明确的工作区、任务、Dispatch、消息和释放责任。若只需要一个独立 CLI，就不要把简单工作升级为编排；若需要结果追踪，也不要退回 tmux。

## 任务简报模板

每个 worker 只收到一条可验收任务。复制下面模板并填空：

```text
目标：<一个结果>
范围：<允许读/写的路径或系统>
禁止：<不可修改的路径、生产动作、凭据和外部写入>
依赖：<已有分支、文件、前置结果；没有则写无>
验收：<精确测试/检查命令和通过标准>
回报：<修改文件、命令结果、未解决风险；无修改也要明确>
```

协调者完成回合时只接受带证据的结论；若两个 worker 修改同一路径，先停止后续写入、选择一个写入者并重新拆分，不能靠最后一次保存来解决冲突。

## 版本与验证记录

本手册依据 2026-08-10 的本机验收：Claude Code `2.1.226`、Codex CLI `0.147.0`、tmux `3.7b`、Orca runtime `1.4.177`。Claude 与三个 Codex profile 已完成同 bundle、独立 challenge、0600 receipt 的真实 ACK 验收；Codex 本机 feature 列表显示 `multi_agent` 为 stable/enabled；Orca 提供 `orchestration.contract.v1`、worker launch preferences、federation 和 terminal multiplex 等能力。版本、可用账户、运行负载、知识资产新鲜度和 feature flag 都会变化，执行前以本手册的预检命令重新确认。
