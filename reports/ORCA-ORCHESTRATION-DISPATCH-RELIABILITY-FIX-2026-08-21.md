# Orca orchestration 派发/完成确认可靠性修复 — 诊断 + 修复 + 双复核派发记录

对应用户任务："并且要修复orca完整发送确认成功"（2026-08-21）。

## 0. 结论摘要（写在最前面）

- 真实根因已找到并已验证到具体代码行；一份未提交的修复已写在 `完善claude` worktree（**不是**最初以为的 `orca-w12-orchestration-mcp-9959` —— 见第 2 节的更正）。
- 修复本身、连同它明确"知道但没修"的一个残留缺口，已按标准流程派出 **Claude opus/max + Codex gpt-5.6-sol/max** 两路独立只读复核（`run_7ca7ff97473a`），复核结果出来前，本修复视为"未完成"，不得提交、合并、构建或安装。
- 本轮诊断过程本身，意外提供了这个 bug 的一次**现场真实复现**：round 53（prime-agent-integration 的上游锁定哈希复核）的 Codex 一路，此刻正卡在这个确切的 bug 里 —— 见第 4 节。

---

## 1. 问题背景

本会话反复撞到的失败模式：协调者对一个终端发起新的 `worker-start` 派发时，如果该终端当下**真的**正忙于处理之前的、不相关的真实工作（例如：一次 hooks 信任提示竞态后被重新 `--terminal` 接管的终端，原先真实的一轮还没跑完），Orca 会把这个"忙碌"误判为"派发失败"（`agent_prompt_stalled`），并**永久**吊销该 dispatch 的 capability；随后这个终端即使真的做完了工作、发回真实的 `worker_done`，也会被 `verifyDispatchCapability` 以 "capability is revoked" 拒收 —— 结果是一个货真价实、已经跑完的 worker，永远无法通过被追踪的正式通道确认完成。这正是用户所说"完整发送确认成功"缺失的具体机制。

## 2. 诊断过程（workflow `wj0u7jh60`，3 个 agent 阶段，128 次工具调用，约 28 分钟）

### 2.1 先确认在哪个 worktree 里诊断/修复（重要更正）

Orca 的开发源码是一个多 worktree 单体仓库；`orca-w12-orchestration-mcp-9959`（此前设想的目标）经核实**版本过旧**（`package.json` 锁定 1.4.177-rc.0，本机正在跑的 Orca.app 是 1.4.187）。诊断 agent 逐一检查了 `workspaces/orca` 下全部 39 个 worktree 的 `package.json`，结果：

- 没有任何本地 worktree 精确匹配正在运行的 1.4.187（本地最高只到 1.4.178-rc.2，`projects/orca` 主仓库本身只是个空的 `.git` 登记点，没有实际检出）。
- 在并列最新的 12 个 1.4.178-rc.2 worktree 里，只有 **`完善claude`**（HEAD `227f2b6dc1`）带着一条完整、自成体系的 `fix(orchestration): ...` 12-commit 链，与本会话记忆里"2026-08-19 round 16 关闭：17 个 bug + 全部后续加固，双复核过，推到 227f2b6dc1"的记录逐字对上（commit hash 独立核实一致）。其余 11 个都是同一个共同祖先 commit 之后各走各路的无关分支（Codex 家目录同步、侧边栏改名、抖音技能等）。

结论：**`完善claude` 是正确的诊断+修复目标 worktree**，不是最初设想的 `orca-w12`。

### 2.2 根因（逐行代码验证，非推测）

- `verifyAgentPromptSubmission()`（`src/main/runtime/agent-prompt-submission-verification.ts:40`）等待 5 秒，要求 `workingSequence` 从基线**严格递增**——而这个计数器只在"非工作→工作"这个跳变边沿才会 +1（`recordAgentPromptLifecycleState`，`orca-runtime.ts`）。如果提交这次派发时终端**已经**是 `working` 状态，这个边沿在它持续忙碌期间**结构性地永远不会发生**——不是等不够久，是无论等多久都不会成功。
- `orchestration-worker-topology.ts:303-316` 的 `isUnknownWorkerStartOutcome()` 把"可能只是还不确定，不算彻底失败"这条宽容路径硬编码成只认 `stage === 'worktree_create'`；`dispatch_input` 阶段的任何错误都直接走 `db.failWorkerStart()` 判定为确定性失败——而这恰恰是最不该被这样对待的阶段：往 PTY 写字符、按回车，本身就是"发出去就不再确认"的操作，天然比创建 worktree 更该被容忍歧义，现状恰好反了。
- `failWorkerStart()`（`db.ts:4403-4436`）单向、原子地把 `capability_revoked_at` 钉死；纵览全文件，这个字段只有在**为 pending 状态的 dispatch 重新铸造 capability 时**才会被清空——一旦已经判定 `failed`，没有任何代码路径会再把它清回去。真实的 `worker_done`/心跳/`ask` 后续全部命中 `verifyDispatchCapability()` 里"先查撤销标记，再查 token"（`db.ts:6367-6406`）这一关，被统一拒收并打上 `_orcaLifecycleRejection` 标记——标记本身被持久化、可审计，但没有任何代码读它来做恢复。

诊断 agent 还确认：现有的 `--retry-of` 重试链路（`createStartingWorkerDispatch`）本可以保留 lineage 地重开一个 dispatch，本会话这几个月一直用的"整个重新 `task-create`+`worker-start`"手工规避法，正是这个机制想避免的模式（任务链断裂、丢失 lineage）——虽然它不修复误判本身，但比手工重开更省资源，是一个被低估但已存在的路径。

## 3. 修复内容（未提交，工作树中）

Worktree：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善claude`，4 个文件，187 行新增 / 1 行删除：

- `src/main/runtime/agent-prompt-submission-verification.ts` — 核心修复：仅当 `baseline.status === 'working'`（提交时终端已经在忙）才触发新的 `waitForBusyAgentPromptSettlement()`，给最多 `AGENT_PROMPT_BUSY_GRACE_TIMEOUT_MS = 25_000`（25秒）的宽限期，但**只要**真实字节还在从 pty 持续流出（`outputSequence` 持续推进）才继续等；一旦真的静默满 5 秒（`AGENT_PROMPT_EFFECT_TIMEOUT_MS`）或触到 25 秒总天花板，仍然照旧抛 `agent_prompt_stalled`——原本"提交时终端是空闲的"这条主路径，逻辑上完全没有改动。
- `src/main/runtime/orca-runtime.ts` — 一行管线改动：把已经存在但此前没被这里读取的 `getPtyOutputSequence` 信号接进活动快照。
- `src/main/runtime/agent-prompt-submission-verification.test.ts` / `agent-prompt-submission-runtime.test.ts` — 新增/更新单测与集成级测试。

修复作者自测（作者自报，未经我方独立复核，等待下方双复核）：
- 直接命中的 3 个测试文件：73 → 78 项通过（新增 5 项，无回归）。
- `src/main/runtime/orchestration` + `src/main/runtime/rpc` 广义回归扫描：232 文件 / 2065 测试全过（1 skip）。
- 整个 `src/main/runtime`：366 文件 / 4667 测试全过（两次独立跑，含一次 git stash/pop 往返后重跑，结果一致）。
- `tsc --noEmit` 干净。
- **诚实披露的局限**：两条需要真正构建 Electron App 才能跑的 e2e spec（`orchestration-legacy-worker-*-recovery.spec.ts`、`terminal-send-agent-prompt-submit.spec.ts`）没有跑——任务本身明确禁止构建/打包 App；改用可直接运行的机制级集成测试替代覆盖。
- **明确没有修的残留缺口**：capability 一旦被吊销即永久不可恢复这条链路（`db.ts` 里的 `failWorkerStart`/`verifyDispatchCapability`）没有被触碰——作者的理由是没能在本轮验证清楚这会不会和 `--retry-of` 依赖的"同一 pane 上不能有活跃 dispatch"这条唯一性约束打架，宁可只修"不要一开始就误判"，也不冒险动 capability 生命周期本身。

## 4. 与此同时：round 53（prime-agent-integration）的 Codex 一路，正在现场复现这个确切的 bug

诊断+修复进行期间，我重新检查了 round 53 QA 复核的状态（run `run_4673b87a96ab`，终端 `term_dfcb327d-47a1-45e3-8149-ee1ef150c470`）：该终端已经明确报告"QA 全部做完，报告在 `/tmp/prime-agent-qa.3NVuYg/REPORT.md`，但连续 3 次尝试发送完成消息全部被 `agent_prompt_stalled`/`dispatch_capability_invalid` 拒收"。我按既定恢复模式（新建 task + `worker-start --terminal` 重新挂载，短消息、不重发全文）又试了第 4 次，仍然是 `agent_prompt_stalled`——但这一次终端的实时预览显示的是持续滚动的"Working... 3... 4... 5..."动画，证明它当下**确实在真忙**，不是真死。这正是刚被诊断出来、且已经写好修复的那个假阴性场景本身，活生生地又发生了一次。按既定纪律：不强行打断，等它空闲后再挂载重试，不再连续重试。

## 5. 双复核派发记录（进行中）

标准强制双复核规则触发：这是一个即将写入 Orca 自身代码的候选，能力/证据/最终验收类，必须 Claude opus+max 与 Codex sol+max 两路独立只读复核，任一路 P0/P1 未清零前不得提交/合并/构建/安装。

- Run：`run_7ca7ff97473a`（objective 首行已带 `[强制双复核]` 标记）。
- **Claude opus/max 一路**：直接 `Agent` 工具派发（`model:"opus"`，effort 通过 prompt 显式要求 max），后台运行中，agentId 见内部记录，任务：独立复核诊断是否正确、修复是否真的按描述工作、跑测试套件、评估遗留缺口与时间常数选择、给出明确 P0/P1 判定。
- **Codex gpt-5.6-sol/max 一路**：真实 Orca orchestration 派发，`task_b6bb1017c4a8` / `ctx_d9509c66b1d8`，终端 `term_1f197482-7843-4ace-bffc-b381106a3484`，worktree 显式指定为 `完善claude`，`worker-start` 一次成功（`stage: input_accepted`，未触发本文档第 4 节那个 bug），复核任务书内容与 opus 一路等价（同一份问题清单）。

两路复核完成前，本修复视为未完成；完成后将在本文件追加双方结论，并据此决定是否提交（提交本身仍只是"改代码"，实际 build/安装进正式 Orca.app 需要用户另行明确授权，遵循"不得重建/重装 Orca.app"的标准规则）。

## 6. 双复核结论（2026-08-21 17:05–17:17，两路均已回执）

**两路独立结论一致：P0/P1 均为零，可以提交。**

### 6.1 Claude opus/max（92次工具调用，含12组真实定时器实证探针）

- 复核确认 diff 与描述完全一致（4文件、187增/1删）；把 `agent-prompt-submission-verification.ts` 整个154行、以及 `orca-runtime.ts` 里全部相关函数都通读了一遍，不只看 diff hunk。
- 独立指出并**纠正了我方任务书里的一处事实错误**：`markWorkerStartUnknown`（不只是 `failWorkerStart`）同样会设置 `capability_revoked_at`——也就是说即使把 `isUnknownWorkerStartOutcome` 的容忍范围放宽到 `dispatch_input` 阶段，也救不了这个问题；这个更正让第6节"为什么刻意不修 capability 生命周期"的论证站得更稳，而不是我方原始描述的理由。
- 用 esbuild 把改动模块转译到 scratchpad 里，跑了12组真实定时器场景（空闲基线/忙碌基线 × 冻结/持续输出/中途转移/权限打断/generation变化/abort/异常等），逐条验证：新逻辑不会挂起（硬上限约30.05秒）、不会丢失合法转移（deadline 检查在 poll 前，反而略微宽松不会漏判）、空闲/null 基线路径完全不受影响（即使有输出洪流也仍在~5.0秒判定失败）。
- 测试：目标3文件 78/78 通过；`orchestration`+`rpc` 广义扫描 233 文件里 1 个失败（`terminal-output-frame-chunks-equivalence.test.ts` 的一个800次模糊测试超时），单独重跑该文件 20/20 全过，判定为并行负载下的既有 CPU 密集型 flake，与本次改动无关；tsc 干净。
- **判定：可按现状提交，P0=0，P1=0。**
- P2：忙碌宽限期内若命中上一轮遗留的、不相关的权限提示，会触发 `assertPromptNotBlocked` 提前把这次判定从 `agent_prompt_stalled` 悄悄换成 `agent_prompt_blocked`（同样导致 `failWorkerStart`）——建议补一个测试钉住这个行为，并重新考虑这种情况是否真的应该中止一次字节已经写入的提交；25秒对没有 `tui-idle` 前置检查的 `orchestration.dispatch --inject`/协调者循环这两个调用方可能仍然太短（真实描述的现场事故正好最可能落在这两个调用方，而不是有 tui-idle 前置检查、25秒绰绰有余的 `worker-start`）；桌面发送路径与协调者串行派发队列的阻塞时长从5秒变成最多约30～38秒（含渲染门8秒硬上限）。
- P3：冻结忙碌基线的探测延迟从5秒变成约10.1秒（快速循环没有复用已推进的 `outputSequence`）；`outputSequence` 存在被下游 provider 同步重置变小的可能（会导致失败关闭而非失败开放，方向安全但建议加注释/`Math.max` 钳制）；用的是 `Date.now()` 挂钟而非单调时钟（沿用既有模式）；新循环内的 generation/权限/abort 分支已实证验证正确但缺专门单测钉住，防未来重构回归。

### 6.2 Codex gpt-5.6-sol/max（`run_7ca7ff97473a` / `task_b6bb1017c4a8`，一次派发成功，未撞上本文档第4节的 bug）

- 独立复核 diff、根因、代码路径，逐项结论与 opus/max 高度收敛（根因确认；`agent_prompt_stalled` 并非本 race 独有触发源，还有真正丢输入、无遥测证据、卡死的 working 状态等其它真实触发路径；新分支内 `assertSamePromptGeneration`/`assertPromptNotBlocked` 正确复用，generation 变化/权限打断/abort 都各自抛出对应的类型化错误，未发现能无限挂起的路径）。
- 测试：目标3文件 78/78 通过；广义扫描发现**另外两个独立于 opus/max 那次的、同样与本次改动无关的性能类既有 flake**（`orchestration-creator-authority-performance.test.ts` 的两个耗时断言在并行负载下超出200ms阈值），单独重跑 2/2 文件 25/25 全过，判定同样是并行负载下的 flake；tsc 干净。
- **判定："SAFE TO COMMIT AS AN INCREMENTAL, BOUNDED FIX"，P0=0，P1=0。**
- P2：真实场景里超过5秒的静默（模型推理中、SSH 传输突发）以及超过约30秒总时长的忙碌轮次仍会被误判为失败，并落入未改动的、不可逆的 capability 吊销路径——鉴于后果是永久性的，建议用生产遥测数据来验证/调整这两个时间常数，而不是拍脑袋定。
- P3：冻结忙碌基线现在要多付一轮5秒窗口；deadline 在事件循环延迟/挂钟跳变下可能进一步超出；扩展循环内 generation/权限/abort 分支代码上清晰但缺专门测试；**新增的多行注释比仓库 AGENTS.md 的"简洁注释"约定明显更冗长**（风格问题，不影响正确性）。

### 6.3 两路交叉印证 / 差异

- **完全一致**：根因诊断正确；修复逻辑正确、不会挂起、正常路径不受影响；P0=0、P1=0、可以提交；核心 P2 都指向同一件事——25秒宽限期覆盖不了"真实模型推理静默>5秒"或"忙碌轮次>30秒"这类恰恰最贴近现场事故描述的场景，残留的 capability 永久吊销缺口本身仍未修。
- **独立发现、互不重复**：opus/max 发现了 `markWorkerStartUnknown` 也会吊销 capability 这一纠正性事实、以及"忙碌宽限期内被无关权限打断会静默改变失败原因"这个具体行为；Codex 独立发现了另一对与本次改动无关的性能类 flake、以及新注释违反仓库 AGENTS.md 简洁注释约定。两路测试跑到的"广义扫描里的既有 flake"彼此还不是同一批文件（opus/max 撞到 frame-chunks 超时，Codex 撞到 creator-authority 性能断言），进一步印证这些都是并行负载抖动，不是这次改动引入的。

**结论：genuine dual-GO。已按代码复核时的原样提交（不构建、不安装）。**

## 7. 后续步骤

1. ~~等待两路复核回执~~ ——已完成，见第6节。
2. P2/P3 作为明确的后续技术债记录，暂不在本轮处理：(a) 忙碌宽限期内被无关权限提示打断会静默改变失败分类；(b) 25秒宽限期对无 tui-idle 前置检查的 `dispatch --inject`/协调者循环可能不够；(c) capability 永久吊销这条残留链路本身没有修；(d) 两个独立的、与本次改动无关的既有并行负载性能 flake；(e) 新注释比 AGENTS.md 约定更冗长。
3. 提交本身只是改代码；真要构建/装进正式 Orca.app 仍需要用户另行明确授权，遵循"不得重建/重装 Orca.app"的标准规则。
4. 继续监控 round 53（prime-agent-integration 上游锁定哈希）Codex 一路，终端真正空闲后再尝试挂载完成确认。
