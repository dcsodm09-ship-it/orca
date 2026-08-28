# 完善 Orca 未完成工作确认清单

状态：`CONFIRMED_EXECUTION_IN_PROGRESS`

用户已确认：按完整清单规划，并由协调器使用 Orca 编排先拆成 5 个核心 Codex
任务负责规划、巡检和收口；5 个不是总数上限，后续可按 DAG 和容量继续增加
Luna/Terra/Sol Worker。该确认授权隔离候选整合、独立验收、版本治理、
测试与报告；它不自动授权生产安装、Hook/记忆/Skill 激活、live settings 或
`.orca/context` 修改、R2 启动、真实归档复制、内置盘退役/删除、进程 signal、
Hammerspoon reload、GitHub/远端写入。上述动作继续在各自 fresh 门禁前单独确认。

本清单只固化当前只读盘点结果。它不授权安装 Hook、注入记忆、修改
Claude/Codex/live settings、启动 R2、复制或删除内置盘数据、激活 Skill、
修改远端服务、发布 GitHub Release 或 signal 任何进程。

## 历史盘点快照（以其下方 W0 fresh reconciliation 为准）

- 工作目录：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`
- Git common-dir：`/Volumes/Extreme SSD/Orca/projects/orca/.git`
- HEAD：`828b5d8a963d076bce9c3b88d3aae455db251d19`
- Orca：runtime `ready`，graph `ready`
- Run：`run_9959a2273423`，coordinator generation `3`，legacy `0`
- Run 任务状态：completed `23`、blocked `6`、failed `1`、pending `3`、ready `5`
- 先前两个既有 Worker Dispatch 均已完成；新建五任务 DAG 尚未产生新 Dispatch
- 启动态：`ORCA_CONTEXT_NACK_V1`，原因是 central reviewed Git freshness mismatch；
  不得把 Orca context、Hook 或同包 ACK 当作已加载/已上线
- Git tracked files：`0`
- Git untracked rows（`-uall`）：`39005`（历史计数，已由下方 fresh reconciliation 取代）
- 工作区占用：约 `3.1G`
- R2：restic absent；rclone PID `2343` 只读观察；in-progress/verified absent；
  failed marker mode `0600`、28 bytes、SHA-256
  `40946482f96a4d8eb4f641261e727292e2659be28a68856fafc7bedfa0701ee4`

### W0 fresh source-authority reconciliation (2026-08-11)

The preceding snapshot is historical evidence, not a release authority. A
fresh local-only reconciliation found that its `39,005` untracked count has
drifted and must not be used for a cleanup or release decision.

| Boundary | Fresh identity | Current state | Authority decision |
|---|---|---|---|
| source candidate | `优化本机code`, `p0-ssd-r2-integration`, `6db25c5e9a9af14d1f32bf68eb625aab9943b71e` | 12,877 tracked; 15 modified; 368 untracked | Candidate only; no clean tracked ship-set yet |
| evidence/candidate area | `完善orca`, `828b5d8a963d076bce9c3b88d3aae455db251d19` | 0 tracked; 5,061 untracked | Never a release source; retain as evidence until each item is classified |

The 15 modified source files and the two untracked `src/main/runtime/orchestration/`
files are the W10 per-worker account-binding candidate. They have offline
test/build evidence but remain uncommitted and are not installed runtime bytes.
The remaining source untracked roots (`graphify-out/`, `tools/`, plus two
separately quoted-path entries) remain unclassified; they cannot enter a
ship-set, be ignored as cache, or be deleted by this reconciliation. The
largest evidence roots are `ssd-native-storage-closure/` (2,377 entries) and
`ssd-pty-lifetime-closure/` (2,356 entries); their size is evidence of work,
not proof that either is releasable or disposable.

W0 remains open until a reviewed change set is isolated from pre-existing
untracked content, committed to the selected source branch, and accompanied by
a content-addressed classification manifest for all 5,061 evidence entries.
Only after separate archive and deletion authority may a later task act on any
classified retention/disposal result. This reconciliation performed no copy,
archive, delete, Git cleanup, installation, or remote action.

## Orca 执行 DAG（2026-08-11T14:30:35+0800）

- `task_2b8476cf4352`：Codex 1，W0/W1 Startup 与路径权威，`ready`
- `task_456342e63aa3`：Codex 2，W2/W7 L1-L3 Memory 与 Skills，`ready`
- `task_09af7f9074ae`：Codex 3，W3/W4/W5 R2、迁移与 Android，`ready`
- `task_4e607b80d041`：Codex 4，W6/W8/W9 Native、Release 与 Desktop，`ready`
- `task_5682494464cd`：Codex 5，W0-W9 跨线集成验收，依赖前四项，`pending`
- `task_4619bce391f1`：Codex 6，W10 Orca-native 多 Codex 调度与 per-worker
  managed-account binding，`ready`
- `task_0a805809c98b`：Codex 7，W11 最终编排验收，依赖 Codex 5 与 Codex 6，
  `pending`

五项任务均已建立在 `run_9959a2273423`，协调器仍为
`term_8fece896-b850-4fb1-a0f2-1c352436f174`，generation `3`、legacy `0`。
权威源码父工作树为
`/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code`，基线 branch
`p0-ssd-r2-integration`、HEAD
`6db25c5e9a9af14d1f32bf68eb625aab9943b71e`；本工作区继续作为证据/候选区，
不得把任何 untracked 内容直接当 release authority。

Fresh `agent_capacity.py` snapshot now reports `yellow`: 8 logical CPUs, 1-minute
load `7.101`, 57% free memory, 2 working worktrees/agents, and
`new_workers_max=1`. The existing Run remains owned by its coordinator; this
reconciliation neither starts a worker nor creates a child worktree. A later
coordinator may start at most one independent local worker only after it takes
its own fresh snapshot. This is a resource gate, not task completion or
cancellation.

### 2026-08-11 最新实现进展与工期

- 长上下文最短修复已生成 `startup-reviewed-pack-schema3-fastfix/candidate/`：
  当前 builder 接受 schema v3 manifest，reviewed pack 为当前合同的 8 个 L1
  items，finite content closure 与当前 Git authority 已绑定；候选报告状态为
  `verified_offline_candidate_not_installed`，live `.orca/context` 未修改。
- Graphify 当前两个 asset 的 source closure 均属于另一工作树，现按
  `source_closure_scope_mismatch` 拒绝，并在读大 graph body 前 fail closed；它未
  被声称 verified，仍需 fresh 本项目 Graphify receipt。
- 新增 `orchestration-dynamic-scheduler/` 隔离候选，覆盖容量、任务风险、
  Sol/Terra/Luna effort、隔离 worktree 和秘密字段拒绝；completion-plan `3/3`
  与 scheduler `12/12` 本地测试通过。
- W10 source candidate now adds an opaque per-worker account reference and a
  process-only managed-home override. Its focused Vitest suite passed `669`
  tests, typechecks/lint/build passed, but the installed Orca runtime does not
  advertise the new capability. Therefore no account isolation/rotation is
  claimed and no global account setting is changed to emulate it.

按全部真实 E2E 和生产门禁完成计算，容量及时恢复且权限及时给出时预计还需
`3-5` 个工作日；最快约 `2` 个工作日。若 R2 restore、四目标 Hook 安装、Android/
内置盘退役或账号绑定能力继续阻断，日历时间可能延长到 `1-2` 周。该估算包含
约 `0.5-1` 天长上下文/调度独立复审与集成、`1-2` 天四条实现线、`1-2` 天真实
E2E/R2/迁移与最终整合；等待容量和生产授权会增加日历时间。

### Worker 模型与波次策略

模型选择属于编排合同，实际启动时必须以 `worker-start` 回执中的
`launch.requested` / `launch.effective` 为准；未得到有效回执不得声称模型已选中。
当前偏好如下：

| Task | 请求模型 | Effort | 原因 |
|---|---|---|---|
| `task_2b8476cf4352` | `gpt-5.6-sol` | `xhigh` | P0 startup authority、dirfd、manifest 与回滚安全 |
| `task_456342e63aa3` | `gpt-5.6-terra` | `high` | 大量 L1-L3/Skill 合同整合与测试，兼顾吞吐 |
| `task_09af7f9074ae` | `gpt-5.6-terra` | `high` | R2/迁移门禁以清单、测试和 fail-closed 审计为主 |
| `task_4e607b80d041` | `gpt-5.6-sol` | `xhigh` | XPC/EndpointSecurity/release 安全边界复杂 |
| `task_5682494464cd` | `gpt-5.6-sol` | `xhigh` | 跨线冲突、集成、最终逐项验收与回滚排序 |
| `task_4619bce391f1` | `gpt-5.6-sol` | `xhigh` | Orca CLI/RPC、账号隔离、并发与失败恢复是 P0 控制面 |
| `task_0a805809c98b` | `gpt-5.6-sol` | `max` | 独立验证真实多 Codex 编排、账号隔离和全线收口 |

`orca account list --json` 当前返回 `runtime_error`（local-only command attempted
to access the Orca runtime），所以账户目录没有被当作模型可用性证明。启动时若
任一 opaque model ID 被运行态拒绝，必须停止该启动、保留失败回执，并按运行态
明确返回的支持列表重新选择；禁止静默退回默认模型。即使容量转绿，也按
`new_workers_max` 分波启动，不强行同时运行四个重模型。

三种 GPT 子模型均可在各自隔离工作树内编辑、测试并提交，不限定为只读：

- `gpt-5.6-luna`（`medium`/`high`）：快速枚举、复现、fixture/测试生成、机械性
  重构与证据整理；允许编辑低耦合代码、测试和文档，但不单独作 P0 authority
  或生产上线决定。
- `gpt-5.6-terra`（`high`）：主实现者，负责跨文件业务逻辑、调用链、迁移工具、
  L1-L3/Skill 合同和完整回归；可编辑其任务声明范围内的生产源码。
- `gpt-5.6-sol`（`xhigh`，最终总验收必要时 `max`）：P0 安全架构、manifest/
  dirfd/签名/权限/回滚、XPC/EndpointSecurity 与跨线集成；既负责独立复审，
  也可直接修正其工作树内发现的问题。

长上下文注入优先采用 `Luna 快速复现与测试 -> Terra 主实现 -> Sol 安全修正和
最终验收`。各阶段有依赖时串行，不依赖的测试/文档/实现才并行；同一生产写入
仍只有一个 writer。

账号池与并发是两个独立控制面：用户说明本机可用约 200 个订阅账号，可按用量
动态选择；但物理并发仍受 `agent_capacity.py` 限制。账号调度只使用 Orca 托管
账户身份、额度/冷却/模型兼容状态，不读取、复制、打印或持久化 raw token。
一个 Dispatch 启动后不热换账号；限额或冷却只影响后续新 Dispatch 的账户选择。
`orca account list --json` 当前的 `runtime_error` 修复前，账号池可见性记为
`BLOCKED_NOT_VERIFIED`，不得声称 200 账号轮转已生效。

## 执行顺序与验收清单

### W0 — 建立唯一版本与发布权威（P0，所有后续工作的前置）

- [ ] 确定真实发布源码工作树与当前审计/证据工作树的职责边界。
- [ ] 对当前 5,061 条 evidence untracked 内容分类为：ship-set、受审证据、可重建缓存、
  失败现场、应归档、待人工批准删除。
- [ ] 在真实源码工作树建立 clean tracked ship-set；不得用当前空 Git 树或
  任意 untracked 目录作为 release authority。
- [ ] 建立 source / patch / built artifact / installed bytes 的内容身份链和
  rollback preimage。
- [ ] 修正 Orca worktree comment 与 `ORCA_CONTEXT_NACK_V1` 冲突的状态漂移。

完成证据：clean tracked source SHA、分类清单、保留/归档/删除 receipt、
changed-during-read 复核、没有越界删除。

### W1 — Startup Context / Hook 生产闭环（P0）

- [ ] 生成真正由当前 startup builder 接受的 reviewed manifest schema v3，
  并由外部 reviewed authority pin Git/source/pack identity。
- [ ] 将实际 `startup_context` 入口改为只消费 sealed loader 提供的
  `verified_resources` 或 held descriptors，不再按路径重新发现权威资源。
- [ ] 对 sealed loader exact bytes 做 fresh 独立安全复审。
- [ ] 完成 transaction v2 fresh 独立 QA；当前
  `startup-transaction-v2-independent-acceptance/` 只有未收口的 `.work` 候选，
  没有最终独立 acceptance report。
- [ ] 重新发现四个 live targets，生成 fresh permission preimage/receipt；
  目标必须 `0600`、直接父目录必须 `0700`，任何 ctime/path/inode 漂移停机。
- [ ] 生成并独立复审 four-target preimage-bound reversible patch。
- [ ] 经单独安装授权后，串行执行 permission remediation、pack commit、Hook
  transaction，并证明 crash recovery 与 exact rollback。
- [ ] 真实 Claude + 全部 Codex 账户完成 trust review、restart、delivery、
  same-bundle/different-challenge ACK、replay/expiry、SSD missing/remount、卸载回滚 E2E。

完成证据：`ORCA_CONTEXT_ACK` 对当前 authority 生效、真实入口来自
`verified_resources`、transaction v2 独立报告、fresh four-target receipt、
全部账户同 bundle E2E；不能以 v4 guard `164/164` 替代这些证据。

### W2 — L1–L3 Agent Memory 与检索治理（P0/P1）

- [ ] 在真实 authority source 建立 tracked sidecar ship-set，关闭 source/wheel/
  installed 三向身份断裂和缺 payload 的旧 wheel。
- [ ] pack contract version bump；跨 provider equality 必须绑定 `safety_items`、
  regular `items`、policy、完整 receipt 和容量语义。
- [ ] 人工复核并迁移旧 manifest 缺失的 `content_sha256`；禁止自动回填。
- [ ] 人工给出可进入 non-filterable safety lane 的旧 L1 constraint 清单，
  使用受审、幂等、有 receipt 的专用迁移入口。
- [ ] truth snapshot 统一 `truth_as_of`/`ranking_as_of`，hard exclude revoked、
  superseded、contradicted、inactive ancestor、L0 和 secret。
- [ ] local-first 先使用已批准未撤销 L1–L3 + Skill + active graph projection；
  仅在 reason-coded abstention 后允许 Sol admission，Sol 首次输出只能是
  `l0_candidate` 且 `activation_permitted=false`。
- [ ] 补齐 TTL/rotate、capability 撤销、ACK/launch GC、per-category quality gate、
  calibration/ablation、pack overflow、跨平台 CI 与 packaged-runtime canary。

完成证据：tracked clean ship-set、9/9 entrypoints、wheel/install exact receipt、
safety lane 跨 provider tamper tests、人工 migration receipt、撤销/冲突/secret/
capacity 回归、真实安装副本 E2E。

### W3 — R2 writer/reader/trust 与当前恢复身份（P0）

- [ ] 对 generated-v3 writer/reader patches 做 fresh preimage recheck、串行应用和
  installed-byte 独立审计。
- [ ] provision 并独立复核真实 public verifier identities、threshold/role policy
  与 pinned production policy SHA；测试不得生成或读取私钥。
- [ ] 将内部 recovery state 的实际 mode/owner 规范到受审私有边界；任何 live
  权限动作需单独授权和 rollback。
- [ ] fresh 核对无 restic/backup writer、无 in-progress marker、所有本地 writer
  已收口后，只执行一次当前增量备份；失败不自动重试。
- [ ] recovery-reader 对新 snapshot 做真实隔离 restore、全文件/受审 manifest
  identity、snapshot bytes/time/run identity 和 trust receipt 验收。
- [ ] 所有 Hook/Skill/archive/Android/项目迁移变化完成后，再执行最后一次增量
  备份与 reader 验收。

完成证据：新的 verified marker、fresh snapshot identity、reader restore receipt、
production trust receipt、replay claim 和最终增量 receipt。旧 snapshot reader PASS
不能替代当前 failed marker。

### W4 — 两个内置项目 + Wave2 十四根迁移归档（P0）

- [ ] 决策：正式以 `ssd-archive-cleanup-v5` 替代旧
  `task_938617a3e3cd`/旧 Wave2 脚本双线维护，或继续加固旧脚本；建议只保留 v5。
- [ ] 对 v5 exact bytes 做 fresh 独立复审和完整 adversarial matrix。
- [ ] 对科技公司、传媒公司的 exact source/future SSD leaf 重新执行 nofollow、
  recursive `lsof +D`、Git identity/status、target absence、容量和源稳定门禁。
- [ ] 对 `1 + 1 + 14` 个根建立 descriptor-derived recursive manifests，证明
  global logical-path uniqueness 与 plan/root/record/copy/probe token/version 链。
- [ ] 在真实目标卷对每种 leaf kind 完成 APFS provenance structural probe；
  现有 fail-closed probe 不能当作通过。
- [ ] 经单独 archive-only/cold-copy 授权后，单 writer 串行复制到全新 0700
  staging；禁止覆盖或 resume 任何 `INCOMPLETE`。
- [ ] 16/16 content/symlink/xattr/ACL/ResourceFork/quarantine/provenance/bytes/
  manifest/completion-marker 全量验证。
- [ ] post-copy fresh `lsof=0`、R2、reader restore identity 完成后，才进入 setup
  retirement；删除仍是另一个人工作废门。

完成证据：独立 v5 report、fresh APFS probes、16/16 receipts、post-copy lsof、
R2/restore receipts。此前递归 lsof=0 仅是历史时点证据。

### W5 — Android 保留原件退役（P0 destructive gate）

- [x] 冷备与活动副本完成。
- [x] 默认路径切换到 SSD、Orca 冷启动/AX/关机、rollback_ready 完成。
- [ ] fresh 构建两个 retained-root exact manifests。
- [ ] 最终 R2 snapshot 绑定 Android cold/active receipts、retained manifests、
  reader/trust threshold receipts。
- [ ] 对 packaged retirement runtime 和 all-context plan 做独立验收。
- [ ] 只有人工明确接受 exact 两个 retained targets 和 journaled 临时 mode
  transitions 后，才允许监督式退役；失败保留 journal，不能批量删除。

旧 failed `task_9d2fc436c308` 已由成功冷备与切换任务替代，不应重做。

### W6 — SSD native storage / remote policy / PTY lifetime（P0 产品闭环）

- [ ] 将已独立接受的 offline overlay 以 content-bound patch 合入真实源码。
- [ ] 构建、嵌入、harden、codesign XPC Mach service，并接入实际 Node-to-XPC
  transport；验证 app/helper designated requirements 和不可写资源身份。
- [ ] installed App E2E 覆盖 accepted/hostile caller、crash/restart/replay、
  remount/UUID、path swap、capacity、rollback 和真实 init/clone/worktree。
- [ ] PTY lane 实现 exhaustive EndpointSecurity event translator、incoming listener
  audit-token/code-sign validation、canonical vnode/path binding、signed addon、
  request rebind 与 ES-loss supervisor。
- [ ] 取得 Apple EndpointSecurity entitlement、FDA/user approval、notarized runtime、
  exact Electron ABI addon，并做真实 terminal/fork/daemon/remount E2E。

完成证据：不能是 compile/unit-only；必须是安装签名 App 的真实 enforcement receipt。

### W7 — 新 Skills 与 Paperclip（P1）

- [ ] `review-orca-workflow-learning` 做 isolated forward-test、真实重复成功/业务
  acceptance、人工 L0→L1–L3 review、exact installed digest、revocation 与 rollback E2E。
- [ ] 决定是否接入 SessionStart/Graphify；任何自动学习只能落 L0，不能自动激活。
- [ ] `paperclip-orca-closure` 已通过离线独立验收，但必须继续保持
  `activation_permitted=false`，直到 packaged identity、broker isolation、Hook delivery
  和生产安装验收全部通过。
- [ ] 决定旧 `paperclip-skill-candidate` 是否仅保留为 provenance 或归档，避免与
  `paperclip-orca-closure/skills/paperclip` 形成双 authority。

完成证据：fresh installed digest、rollback、真实 workflow receipts、无 secret/raw
session/hidden-reasoning、人工 approval，且 activation 由受审 descriptor 驱动。

### W8 — Orca Release workflow（P1）

- [ ] 将 `release-closure` content-bound patch 应用到真实源码并运行现有与新增
  release contract suites。
- [ ] 保证 `release-e2e` 成功是 `publish-release` 的前置，不允许发布后 warning-only。
- [ ] disposable draft tag 真实证明 parent/child run identity、tag/commit/workflow
  path/run-attempt、receipt 下载与重哈希。
- [ ] child failure、timeout、duplicate discovery、tag move、receipt tamper 均必须
  保持 draft，不能公开 release。
- [ ] 增加 packaged-asset E2E：SSD admission、startup ACK、desktop broker denial、
  migration restore、privacy/telemetry、SBOM/OSV/DSSE/in-toto、immutable action pins、
  installed binary identity 和 rollback preimage。

任何真实 GitHub dispatch、tag、release 或远端写入都需要另行明确授权。

### W9 — Desktop MCP、测试合同与证据卫生（P1/P2）

- [ ] Desktop MCP 增加可信 broker peer（例如 code-signed XPC + audit-token），关闭
  owner-only Unix socket 无法鉴别 same-user process 的生产阻断。
- [ ] 经授权 reload Hammerspoon、验证 Accessibility、做 no-effect state/move E2E，
  再验证 Codex/Claude 双客户端注册；任何 input 前必须 fresh `desktop_state`。
- [ ] 修复 sandbox fixture contract：diskutil、Unix IPC、`getpwuid` 与 deny-network
  分层；policy-blocked 测试记为 `not_run_by_policy`，不能伪装 PASS。
- [ ] 补齐 tmux/gh/packaged-runtime 条件测试和 macOS/Linux/Windows CI matrix。
- [ ] 对约 3.1G 证据分类：约 2G 两个 Node overlay、约 637M 三个 Swift release
  树、55M Swift test、约 400M native/PTY closure、8.1M transaction-v2 `.work`，
  以及其他小型 `.pty-*`。
- [ ] 只有在 frozen evidence manifest、报告引用检查和用户批准后，才可归档或
  删除可重建缓存/失败 staging；不得直接递归删除。
- [ ] 对 manuals、wiki、fixtures、reports、scripts、tests、`.gitignore`、`.DS_Store`
  做 ship/archive/discard 决策，最终 Git status 与 release manifest 必须一致。

## 当前 Run 中不属于本工作区的任务

- `task_51909e15079f`：Warpgate 网络暴露只读验收，工作区为
  `/Volumes/Extreme SSD/Orca/workspaces/hgfast/节点专用`。
- `task_07719f093850`：Warpgate 配置持久化只读验收，同上。

这两项保持 blocked，不并入 `完善orca` 当前执行队列。若用户要求纳入，必须先按
服务器连接规则读取 `SERVER_CONNECTION_RULES.md` 并执行静态 route verifier；任何
SSH/远端动作仍需独立范围和授权。

## 顶层候选目录归属表

| 路径或集合 | 工作包 | 当前处置 |
|---|---|---|
| `orca-context-bridge/`、`startup-*`、`task_2cfbe4265261-safety-lane-report.md` | W1/W2 | 保留；候选未上线 |
| `skills/review-orca-workflow-learning/`、`sol-memory-strengthening-audit/` | W2/W7 | 保留；未安装 |
| `paperclip-orca-*`、`paperclip-privacy-gate-closure/` | W7 | 保留；activation false |
| `paperclip-skill-candidate/` | W7 | 等待归档/authority 决策 |
| `r2-run-identity-closure/`、`r2-production-trust-closure/`、`r2-public-only-acceptance/` | W3 | 保留；未应用、未运行 live backup |
| `archive-provenance-*`、`ssd-archive-cleanup-v5/`、`internal-projects-cold-preflight/` | W4 | 保留；真实 copy/archive 未执行 |
| `android-retained-retirement-closure/`、Android scripts/report | W5 | 保留；切换完成、退役未授权 |
| `ssd-runtime-*`、`ssd-native-storage-*`、`ssd-pty-lifetime-*` | W6 | 保留；offline-only，未合入/安装 |
| `release-closure/` | W8 | 保留；patch 未应用 |
| `desktop-mcp/` | W9 | 保留；生产 peer auth 与 live E2E 未闭合 |
| `reports/`、`scripts/`、`tests/`、`wiki/`、两份操作手册 | W0/W9 | 审核后纳入 ship-set 或归档 |
| `ego-capability-fixture/` | W9 | 测试 fixture，等待 ship/discard 决策 |
| `.pty-*`、native validation/build overlays | W9 | 可重建候选，但删除需 evidence+用户批准 |
| `.orca/context/` | 不纳入 Git 操作 | live context 边界；保持 NACK，不修改 |

## 用户确认模板

推荐确认文本：

> 确认按 W0→W9 顺序执行。先只授权候选整合、独立验收和版本治理；Hook 安装、
> R2 启动、真实复制、内置盘退役/删除、Skill 激活、Hammerspoon reload、GitHub
> 外部写入分别到门禁时再确认。Warpgate 暂不包含。

如果用户需要一次确认覆盖不同边界，应逐项明确列出；“确认”本身不自动扩展到
生产安装、远端写入或不可逆删除。
