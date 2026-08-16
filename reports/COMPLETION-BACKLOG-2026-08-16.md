# Orca 完善开发 · 完成度总台账（持续更新）

> 由 `/goal 请全部完善并且必须全部在ssd` 驱动的活跃工作清单。基于 2026-08-15 的 9 路只读普查
> （见 `reports/orca-prime-agent-gap-audit-20260815.md`、`reports/UNFINISHED-WORK-CROSS-REVIEWED-2026-08-12.md`
> 与本次已发布的 Artifact 台账）整理为可执行 backlog。每轮只做 `SAFE_NOW` 标记的项，其余按
> 阻断类型排队，阻断解除后立刻转为可推进。

**阻断类型图例**：
- `BLOCKED_ROOT_AUTHORITY` — 依赖 `projects/orca@main` 身份的人工决策，决策前任何"合并进集成分支"都没有目标
- `BLOCKED_DUAL_REVIEW` — 按用户规则需要 Claude opus+max **与** Codex gpt-5.6-sol+max 两路独立只读复核都通过
- `BLOCKED_CAPACITY` — 需要绿/黄容量灯才能派 reviewer；红灯禁止
- `BLOCKED_APPLE_ENTITLEMENT` — 需要 Apple 签发的 EndpointSecurity/system-extension entitlement + codesign，超出本会话能力
- `BLOCKED_HUMAN_AUTH` — 需要用户对不可逆/生产/破坏性动作的一次性明确授权（install、删除、GitHub dispatch、R2 execute）
- `BLOCKED_HUMAN_DECISION` — 需要用户先做一个政策/设计决策，技术工作才能继续
- `SAFE_NOW` — 在现有授权范围内可以立即推进（文档纠错、核对矛盾报告、不与其他会话共享的 worktree 上的 git 整理）
- `DONE` — 本轮已完成

---

## 0. 容量与运行态快照（每轮开工前必读，容量会波动，每次派工前必须重新查）

- 2026-08-16 T1 容量门：`gate=yellow, new_workers_max=1`（`worktree_summary` 报 `invalid_worktree_schema`，host-only 模式保守判黄）。
- 2026-08-16 T2（约 40 分钟后重查）：`gate=red`，`worktree_summary` 已恢复正常（107 worktrees / 6 working / 10 reported agents），红灯原因"load exceeds logical CPU capacity"。**尝试派发 Prime Agent 的 Codex sol/max 复核前二次核对时命中红灯，已取消本次派发，未创建 Run/Task/Dispatch。** 等灯转黄/绿后重试，派发前必须再查一次，不能复用旧读数。
- `必须全部在ssd` 核查结果：
  - `archive/`、全部 59 个候选目录、全部 29 个兄弟 worktree、live Codex home 及两个受管账户 home 均已确认在 `/Volumes/Extreme SSD/Orca/` 下或正确软链接到 SSD。
  - ~~唯一未落地的例外：内部盘仍留有 17 个 `*rollback-backup*` 目录~~ **`DONE`（2026-08-16）**：用户批准"迁移到SSD保留"后，17 项全部 rsync 到 `/Volumes/Extreme SSD/Orca/archive/rollback-backups-retired-20260811/`（0700），逐项校验字节数+文件数一致后才删源；过程中发现 `claude_desktop_config.json` 带 `uchg` 标志导致首次中止，已 diff 核实内容一致后清除。**独立复核确认**：`find /Users/www1adwawd -iname "*rollback-backup*"` 为空（内部盘已清空），SSD 目标含全部 17 项，live `.claude`/`.codex` 软链接未受影响。回执：`archive/rollback-backups-retired-20260811-RECEIPT.md`。**至此"必须全部在SSD"对已知存量数据无剩余已知例外。**

---

## 1. 根源阻断（先于一切"合并/安装"结论）

| 项 | 状态 | 下一步 |
|---|---|---|
| `projects/orca@main` 身份（真实历史 vs 非 VCS 证据库） | `BLOCKED_HUMAN_DECISION` | 已在对话中向用户提出，等待答复；决策前不对该检出做任何 commit/reset/合并 |
| 内部盘 rollback-backup 退役政策（删除 / 迁移到 SSD 保留 / 原地保留但加固权限） | `BLOCKED_HUMAN_DECISION` | 已在本轮对话中提出三选一问题，等待答复 |
| 已装 Orca 1.4.182 与本地任意 checkout 缺少可证明同源性 | `BLOCKED_HUMAN_DECISION` + 需要 fetch/对齐 origin | 不通过重建/替换 Orca.app 来"修复"；等根分支身份决定后再处理 |
| 知识图谱-Workflow-完善 分支去留 | `BLOCKED_HUMAN_DECISION` | 已具体化为独立问题，见第 6 节 |

---

## 2. 启动上下文 / Hook 注入（16 项）

| 项 | 状态 | 下一步 |
|---|---|---|
| startup-installer-closure / independent-acceptance | `BLOCKED_HUMAN_AUTH` | 4 个真实目标里 2/4 文件、4/4 父目录权限不达标；需先跑 `startup-permission-remediation-closure`（见下）并获授权 |
| startup-permission-remediation-closure | `DONE`（v1-v3 差异已查清） / `BLOCKED_HUMAN_AUTH`（真实写入） | 见 `startup-permission-remediation-closure/LIVE-PREIMAGE-DRIFT-ANALYSIS-2026-08-16.md`：v1↔v2 一致（符合报告自述）；v2↔v3（8/11 10:33→10:58）是**未被报告记录的外部活动**——4 个目标文件全部换了新 inode（整体重写，非本候选自身原语能做到的行为），4 个父目录统一变安全(0755→0700)，但目标文件权限结果不一致：`claude` 变安全、`codex-global` 不变、**`codex-account-01` 从 0600 倒退到 0644**、`codex-account-02` 维持 0644 不安全。**新发现待办**：`codex-account-01` 当前(8/16)真实权限状态未知，是否仍处于比 8/11 更不安全的状态需要一次单独授权的只读真实核查（`BLOCKED_HUMAN_AUTH`，未做，不属于本次"解释历史差异"任务范围）。 |
| startup-loader-closure | `BLOCKED_DUAL_REVIEW` | 需要针对已修补字节的全新独立安全复审（不是自测） |
| startup-p1-hardlink-closure ↔ startup-p1-offline-acceptance | `DONE`（文档修复）→`BLOCKED_DUAL_REVIEW`（正式验收） | 已在 `startup-p1-offline-acceptance/SUPERSEDED-NOTE-2026-08-16.md` 写明其 NO-GO 结论已被 hardlink-closure 的修复取代、且"独立验收"这一步至今仍未真正发生；真正的独立复核仍需另一 agent，未开始 |
| startup-reviewed-pack-closure / independent-acceptance | `BLOCKED_HUMAN_AUTH` | v3 已 GO_OFFLINE_CANDIDATE_ONLY，需 root 显式授权才能原子提交进 `.orca/context` |
| startup-reviewed-pack-schema3-fastfix (v4-v7 + activation layer) | `BLOCKED_DUAL_REVIEW` | v7 已过期于 03:58 之后的 builder 改动；需先出一个针对当前字节的新版本，再走双复核 |
| startup-transaction-closure / v2-independent-acceptance | `BLOCKED_DUAL_REVIEW`（4天未被真正执行过） | 需要一个不同 agent 真正运行验收并产出裁决文档 |
| startup-live-integration-v4 | `BLOCKED_ROOT_AUTHORITY` + `BLOCKED_HUMAN_DECISION`(ACK) | 结论 NO_GO_CONTEXT_NACK 本质是上面第1节两个根源问题的下游症状 |
| reports/startup-context-*-candidate ×2、W0-W10-SOURCE-BOUNDARY-MANIFEST、W10-ACCOUNT-BINDING-STATIC-REVIEW | `BLOCKED_CAPACITY`（需绿灯才能重新起测试/复核） | 容量转绿后重跑 Vitest/typecheck/lint |

## 3. SSD 原生存储与 PTY 沙箱（8 项）

全部 `BLOCKED_APPLE_ENTITLEMENT`：需要 EndpointSecurity / system-extension entitlement + 已签名打包 App，本会话无法获取。`ssd-archive-cleanup-v5` 额外是 `BLOCKED_HUMAN_AUTH`（需要真实源根路径授权）。**本轮不可推进**，只能保持候选就绪。

## 4. R2 备份恢复 / 归档 / Android 退役（9 项）

| 项 | 状态 | 下一步 |
|---|---|---|
| codex-restore-tool（恢复CLI，非 完善orca 内） | `BLOCKED_DUAL_REVIEW`（sol 与 op5/max 都已回，双双 NO-GO） | **2026-08-16 完整时间线（真正跑完一次 sol+op5max 对抗双复核）**：① 我自己修复 2 个已知 P1 + 顺带修复 2 个真实功能 bug + 1 个测试断言错误（commit `401b717c63`）② Codex `gpt-5.6-sol/xhigh` 独立复核：确认原修复方向正确，但发现 2 个新 P1——scratch ref 命名不唯一导致并发丢 stash（我已修复，commit `2e3e393173`）、refs 指纹比对对多分支/`refs/notes` 仍不完整（**未修复，见 `CODEX-SOL-XHIGH-REVIEW-2026-08-16.md`**）③ Claude `opus5/max` 独立复核（互不知情、结论不重叠）：确认测试真实通过（745/745，但要绕过一个在本机 Node 24.19.0 上误报红灯的 `ensure-native-runtime` 前置脚本），又发现 **2 个全新 P1**——(a) `git apply --index` 与 `git diff --binary` 的 index/worktree 状态格式不对称，导致任何带未暂存改动的仓库恢复后必然被判 `passed=false`（数据其实正确，是比对本身的假阴性，e2e 测试因为把改动 stash 掉了从未走到这条路径）；(b) `createStashBundles()` 出错时仍只 `console.warn` 后静默清空 `validStashKeys` 并把 stashes 步骤标 passed，UUID 化只堵了一个触发路径，同类静默丢失设计仍在——外加 1 个 P2（`git bundle create --all` 会枚举到 scratch ref 命名空间）。**2026-08-16 后续：完成了真正的重新设计**（commit `a3d1375f75`），不是第三次补丁：① 工作区差异改为分别捕获/恢复 staged（`git diff --cached`）与 unstaged（`git diff`）两份 patch，按正确顺序应用（先 `--index` 应用 staged，再无 flag 应用 unstaged），从根上解决 index/worktree 状态不对称问题；② stash 打包失败不再被吞掉——`createStashBundles()` 改为收集 per-stash `failures` 而不是抛错，调用方据此把 'stashes' 步骤标记为失败而不是静默清空后仍报 passed；③ 恢复端不再用 `git clone --no-checkout`（只会材出默认分支、永不拉 `refs/notes/*`），改为 `git init` 到一个永不会被 commit 的占位分支 + 对 bundle 做逐字 `fetch 'refs/*:refs/*'`——分支、tag、notes 等任何命名空间都按原名一次性带回，refs 指纹比对相应从白名单改成黑名单（只排除 `refs/remotes/*`）；④ 验证过程中额外发现并修复：verbatim fetch 会带回 `refs/stash` 的值但不带 reflog，导致 `restoreStashes()` 的 `update-ref --create-reflog` 因"值未变化"而静默不写 reflog——修复为 fetch 后立即删除该 ref，保证后续 update-ref 一定是真实的 unset→set 转换。全部改动都补了真实（非 mock）回归测试，59 文件/750 测试 3 次重跑零 flaky。**2026-08-16 第二轮双复核（针对 `a3d1375f75` redesign）**：Codex `gpt-5.6-sol/xhigh`（`run_17f75e5c3815`/`task_d777f7f8640f`）与 Claude `opus5/max`（同一 run/`task_958c9e5361dd`）互不知情独立派发，均确认 4 项原始修复的正常路径真正生效（隔离克隆 59 files/750 tests 全过），但各自发现新问题——两路**独立收敛到同一个最严重发现**：stash 步骤已正确标记失败，但上传流程未读该状态，仍会覆盖固定 key 上的上一份可用备份（Codex 定为 P1-3，opus5 定为既存 P2 但两者复现步骤一致）。此外 Codex 另发现 2 个 P1（旧 manifest schema 无版本号直接拒绝解析、固定占位分支名遇同名源分支时 exit 128 且 `--initial-branch` 违反项目 Git 2.25 基线）；opus5 另发现 1 个新 P1（`fetch 'refs/*:refs/*'` 不匹配 `HEAD`，detached-HEAD-零-ref 源仓库同步报成功但恢复必然失败）+ 3 个 P2（并发 scratch ref 污染指纹、上述覆盖问题、Git 2.25 基线合规）。详见 `CODEX-SOL-XHIGH-REVIEW-ROUND2-2026-08-16.md`、`OPUS5-INDEPENDENT-REVIEW-ROUND2-2026-08-16.md`。**已实施第二轮修复**：① manifest 加 `R2_BACKUP_MANIFEST_SCHEMA_VERSION` 字段+明确版本不匹配错误；② 占位分支名加 `randomUUID()` 后缀且改用 Git 2.25 兼容的 `git init` + `git symbolic-ref` 两步法（不再依赖 2.28+ 的 `--initial-branch`）；③ fetch 显式带上 `HEAD` refspec 并在 fetch 后用 `git cat-file -e` 断言 manifest 记录的 headSha 对象确已到位，否则在 checkout 之前就明确报错；④ `stashFailures.length > 0` 时整段跳过上传（含 manifest），保留上一份好备份不被覆盖；⑤ `excludeRemoteTrackingRefs()` 黑名单扩展排除 `refs/orca-recovery-scratch/*`，堵住并发 scratch ref 污染指纹的路径；⑥ `restoreStashes()` 对称改为返回 `failures` 数组，调用方据此标记 restore 侧的 stashes 步骤，不再报告与实际不符的"已恢复"。新增 2 个真实（非 mock）端到端回归测试：detached-HEAD-零-ref 仓库可正确同步+恢复；构造两个 stash（新的一份走 `refs/stash` 完整可读，旧的一份用真实删除该 stash 独有 blob 的 loose object 文件的方式制造 `git bundle create` 失败，不影响主 checkpoint bundle 的 `--all`）验证一次仅 stashes 步骤失败的 sync 不会覆盖上一份好备份、且好备份仍可完整恢复。R2+CLI 隔离套件 59 files/752 tests 全过（含 2 个新测试），typecheck 无新增错误（仅剩此前已记录的、与本次改动无关的 `isInsideRestoreRoot` 未使用变量警告），已提交 commit `cd99c83d26`。**候选状态：第二轮修复已完成，等待第三轮 Codex sol/max + Claude opus/max 独立双复核（容量红灯：load 4.42/cpu、8 个 reported agents，暂缓派发，待容量转黄/绿或用户再次明确授权覆盖）。** 在双复核通过前仍是 NO-GO，不得合并/安装/发布。 |
| r2-run-identity-closure | `SAFE_NOW` | **本轮已修复**：加纠错说明（见下） |
| archive-provenance-normalization-closure | `SAFE_NOW` | **本轮已修复**：加纠错说明（见下） |
| r2-production-trust-closure | `BLOCKED_HUMAN_AUTH` | 需要真实签名方/公钥与 pin 的生产 policy SHA |
| android-retained-retirement-closure | `BLOCKED_HUMAN_AUTH` + 依赖上一条 | 只读 plan 已就绪，`--execute` 需要显式人工授权 |
| internal-projects-cold-preflight | `BLOCKED_HUMAN_DECISION` | 卷条件门禁失败（UUID/APFS/FileVault/Owners 或运行时可达性），需要人工先解决底层条件 |
| reports/android-ssd-switch | `DONE` | 已执行并验证，无需进一步动作（回滚演练仍是可选项，非阻断） |
| reports/cleanup-internal-green-wave2-v4-candidate | `BLOCKED_HUMAN_AUTH` | 需要授权对 14 个真实 Trash 源执行归档 |

## 5. Paperclip 第三方集成（4 项）

| 项 | 状态 | 下一步 |
|---|---|---|
| paperclip-privacy-gate-closure | `DONE` | 已提交（commit `b3009f043d`），不再是"写了但随时可能被清掉"的状态 |
| paperclip-orca-closure / independent-acceptance | `BLOCKED_HUMAN_DECISION` | "受信任 broker"机制未设计，64 种组合里 32 种报 `TRUSTED_BROKER_NOT_IMPLEMENTED`——需要先决定信任模型，再谈复核 |
| paperclip-skill-candidate | `BLOCKED_HUMAN_DECISION` | 是否/如何并入 6 份参考文档，属产品范围决策 |

## 6. 记忆 / 知识图谱 / 协作（5 项）

| 项 | 状态 | 下一步 |
|---|---|---|
| 知识图谱-Workflow-完善（独立 worktree） | `BLOCKED_HUMAN_DECISION` | 已在本轮提出：并入 W1/W2 还是继续独立演进；分支已落后 origin/main 183 commit |
| claude-codex-memory-bridge | `BLOCKED_DUAL_REVIEW` | 涉及跨 provider 记忆读取，属安全边界类，需双复核 |
| sol-memory-strengthening-audit | `BLOCKED_HUMAN_DECISION`（4个P0需先定设计方向） | source/wheel/installed 三方身份需先统一 |
| workflow-learning-l0（2条记录） | `SAFE_NOW`（观察态） | 无法人为"制造"第二次独立复现；保持现状，等待自然复现 |
| task_2cfbe4265261-safety-lane-report | `BLOCKED_HUMAN_AUTH` | 迁移需人工/管家给出确切 safety 清单 |

### 6.1 外部调研：holaOS 记忆架构对照（新增，2026-08-16）

见 `reports/holaos-memory-research-2026-08-16.md`。四条可迁移建议：

| 建议 | 状态 | 下一步 |
|---|---|---|
| 已装 memory daemon 必须能从具体 git commit 确定性重建（markdown 唯一真相源纪律） | `BLOCKED_HUMAN_DECISION` | 定为新验收门禁前需用户认可方向 |
| `context_pack()` 从固定字节截断升级为"规划→检索→合并重排→有界证据包+覆盖/缺口信号" | `BLOCKED_DUAL_REVIEW`（架构级，触碰热路径） | 需先立项出候选，再走 opus+max / sol+max 双复核 |
| 给 L1-L3 记忆条目加 `kind` 字段（preference/identity/fact/procedure/blocker/reference，纯 additive） | `SAFE_NOW`（可作为独立小候选） | 可在下一轮直接起草 schema 扩展候选，走常规测试流程 |
| 给 claude-codex-memory-bridge 补命名空间路径 allowlist（拒绝 `..`/绝对路径/跨工作区） | `BLOCKED_DUAL_REVIEW`（该候选本身已在排队等双复核） | 并入该候选下一版一起送审 |

## 7. Prime Agent 集成

`BLOCKED_DUAL_REVIEW`（Codex 一路已派出）+ `BLOCKED_CAPACITY`（Claude opus/max 一侧仍待配额）：Claude opus+max **与** Codex sol+max 需都完成才算数；当前 Claude 账户周配额 100%，Opus/max 暂不可用。

**结果（2026-08-16 19:26 UTC）**：Codex `gpt-5.6-sol`/`xhigh` 独立复核已完成（`run_3cdb6411dd63` / `task_8c03a3e15ba9` / dispatch `ctx_4c00981a9385`，worker 已 release）。**结论 NO-GO，P0=0，P1=4**，四文件现场哈希与冻结报告逐项一致（候选字节未漂移）：
1. `install_prime_agent.py:1115-1139` `make_patched_asset` 无 O_NOFOLLOW/create-only/目标身份校验，可被符号链接诱导覆盖 SSD 外任意文件。
2. `install_prime_agent.py:1635-1665/1676` 公共命令链接创建对父目录只做词法路径校验，无固定父目录 fd，存在 TOCTOU 窗口可静默逃逸且 `verify_link` 验证不出来。
3. `remove_private_file_durable`（378-408）清 pending 时只复核 quarantine 副本，从不确认原路径已清空，可产生"成功"误报。
4. `tests/sandbox_e2e.py` 关键生命周期断言用裸 `assert`，`PYTHONOPTIMIZE=1`/`python -O` 下会被整体优化掉，产生可复现假阳性；README 用例数（55）与实际测试数（56）也对不上。

按用户规则"任一路存在可复现 P0/P1 都不得完成、合并、发布、安装或部署"——**本候选现在是 BLOCKED，不需要等 Claude opus/max 那一路结果，光 Codex 这一路的 4 个 P1 就已经阻断**。下一步：先修复上述四项、冻结新哈希，再对新候选重新走 Codex sol/max **与** Claude opus/max 双复核。全过程只读，未修改/安装/删除任何文件。

## 8. 桌面 / 浏览器（2 项）

`desktop-mcp`、`ego-capability-fixture`：均 `BLOCKED_HUMAN_DECISION`（peer 认证机制未设计 / ship-or-discard 未决策）。

## 9. 兄弟 worktree 未提交工作（6+23 项）

| 项 | 状态 | 下一步 |
|---|---|---|
| ego浏览器 / 深度学习编排 / codex-track1 / codex-track4 | `BLOCKED_HUMAN_DECISION`（占用问题已排除，剩下是"该不该代为提交"这个产品判断） | 2026-08-16 已用 `lsof` 逐一核实：4 个 worktree 均只有无害的 git fsmonitor 后台进程，没有其他活跃终端/agent 在用，占用顾虑已解除。但代为 commit 数百行未经我逐行代码评审的真实功能（尤其 ego浏览器涉及浏览器会话安全）仍是产品/信任层面的决定，不是纯技术安全问题，本次未擅自提交，留给用户明确要求后再做 |
| 优化本机code | `BLOCKED_HUMAN_DECISION` | 需先确认与 `W10-ACCOUNT-BINDING-STATIC-REVIEW` 评审的是否为同一候选，且需清理无关 scratch 残留 |
| 智能体安装 | `DONE` | 已清理：`git checkout -- pnpm-lock.yaml pnpm-workspace.yaml` + 删除 `.aider.chat.history.md`，worktree 现已干净 |

---

## 本轮（2026-08-16）实际完成项

- [x] 容量门禁读数：yellow / max 1
- [x] 内部盘 rollback-backup 全量核实：17 个目录，约 16.77 GiB，全部含义确认，无删除/移动
- [x] `archive/`、live homes 的 SSD 落地状态核实：均已正确落在 SSD
- [x] r2-run-identity-closure 纠错说明
- [x] archive-provenance-normalization-closure 纠错说明
- [x] paperclip-privacy-gate-closure 提交入库
- [x] startup-p1-offline-acceptance 补充说明其结论已被取代
- [ ] 其余标记 `BLOCKED_*` 的项：逐一等待对应阻断解除后转為 `SAFE_NOW`
