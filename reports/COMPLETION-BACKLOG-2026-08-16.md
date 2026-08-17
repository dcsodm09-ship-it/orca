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
| codex-restore-tool（恢复CLI，非 完善orca 内） | `BLOCKED_DUAL_REVIEW`（sol 与 op5/max 都已回，双双 NO-GO） | **2026-08-16 完整时间线（真正跑完一次 sol+op5max 对抗双复核）**：① 我自己修复 2 个已知 P1 + 顺带修复 2 个真实功能 bug + 1 个测试断言错误（commit `401b717c63`）② Codex `gpt-5.6-sol/xhigh` 独立复核：确认原修复方向正确，但发现 2 个新 P1——scratch ref 命名不唯一导致并发丢 stash（我已修复，commit `2e3e393173`）、refs 指纹比对对多分支/`refs/notes` 仍不完整（**未修复，见 `CODEX-SOL-XHIGH-REVIEW-2026-08-16.md`**）③ Claude `opus5/max` 独立复核（互不知情、结论不重叠）：确认测试真实通过（745/745，但要绕过一个在本机 Node 24.19.0 上误报红灯的 `ensure-native-runtime` 前置脚本），又发现 **2 个全新 P1**——(a) `git apply --index` 与 `git diff --binary` 的 index/worktree 状态格式不对称，导致任何带未暂存改动的仓库恢复后必然被判 `passed=false`（数据其实正确，是比对本身的假阴性，e2e 测试因为把改动 stash 掉了从未走到这条路径）；(b) `createStashBundles()` 出错时仍只 `console.warn` 后静默清空 `validStashKeys` 并把 stashes 步骤标 passed，UUID 化只堵了一个触发路径，同类静默丢失设计仍在——外加 1 个 P2（`git bundle create --all` 会枚举到 scratch ref 命名空间）。**2026-08-16 后续：完成了真正的重新设计**（commit `a3d1375f75`），不是第三次补丁：① 工作区差异改为分别捕获/恢复 staged（`git diff --cached`）与 unstaged（`git diff`）两份 patch，按正确顺序应用（先 `--index` 应用 staged，再无 flag 应用 unstaged），从根上解决 index/worktree 状态不对称问题；② stash 打包失败不再被吞掉——`createStashBundles()` 改为收集 per-stash `failures` 而不是抛错，调用方据此把 'stashes' 步骤标记为失败而不是静默清空后仍报 passed；③ 恢复端不再用 `git clone --no-checkout`（只会材出默认分支、永不拉 `refs/notes/*`），改为 `git init` 到一个永不会被 commit 的占位分支 + 对 bundle 做逐字 `fetch 'refs/*:refs/*'`——分支、tag、notes 等任何命名空间都按原名一次性带回，refs 指纹比对相应从白名单改成黑名单（只排除 `refs/remotes/*`）；④ 验证过程中额外发现并修复：verbatim fetch 会带回 `refs/stash` 的值但不带 reflog，导致 `restoreStashes()` 的 `update-ref --create-reflog` 因"值未变化"而静默不写 reflog——修复为 fetch 后立即删除该 ref，保证后续 update-ref 一定是真实的 unset→set 转换。全部改动都补了真实（非 mock）回归测试，59 文件/750 测试 3 次重跑零 flaky。**2026-08-16 第二轮双复核（针对 `a3d1375f75` redesign）**：Codex `gpt-5.6-sol/xhigh`（`run_17f75e5c3815`/`task_d777f7f8640f`）与 Claude `opus5/max`（同一 run/`task_958c9e5361dd`）互不知情独立派发，均确认 4 项原始修复的正常路径真正生效（隔离克隆 59 files/750 tests 全过），但各自发现新问题——两路**独立收敛到同一个最严重发现**：stash 步骤已正确标记失败，但上传流程未读该状态，仍会覆盖固定 key 上的上一份可用备份（Codex 定为 P1-3，opus5 定为既存 P2 但两者复现步骤一致）。此外 Codex 另发现 2 个 P1（旧 manifest schema 无版本号直接拒绝解析、固定占位分支名遇同名源分支时 exit 128 且 `--initial-branch` 违反项目 Git 2.25 基线）；opus5 另发现 1 个新 P1（`fetch 'refs/*:refs/*'` 不匹配 `HEAD`，detached-HEAD-零-ref 源仓库同步报成功但恢复必然失败）+ 3 个 P2（并发 scratch ref 污染指纹、上述覆盖问题、Git 2.25 基线合规）。详见 `CODEX-SOL-XHIGH-REVIEW-ROUND2-2026-08-16.md`、`OPUS5-INDEPENDENT-REVIEW-ROUND2-2026-08-16.md`。**已实施第二轮修复**：① manifest 加 `R2_BACKUP_MANIFEST_SCHEMA_VERSION` 字段+明确版本不匹配错误；② 占位分支名加 `randomUUID()` 后缀且改用 Git 2.25 兼容的 `git init` + `git symbolic-ref` 两步法（不再依赖 2.28+ 的 `--initial-branch`）；③ fetch 显式带上 `HEAD` refspec 并在 fetch 后用 `git cat-file -e` 断言 manifest 记录的 headSha 对象确已到位，否则在 checkout 之前就明确报错；④ `stashFailures.length > 0` 时整段跳过上传（含 manifest），保留上一份好备份不被覆盖；⑤ `excludeRemoteTrackingRefs()` 黑名单扩展排除 `refs/orca-recovery-scratch/*`，堵住并发 scratch ref 污染指纹的路径；⑥ `restoreStashes()` 对称改为返回 `failures` 数组，调用方据此标记 restore 侧的 stashes 步骤，不再报告与实际不符的"已恢复"。新增 2 个真实（非 mock）端到端回归测试：detached-HEAD-零-ref 仓库可正确同步+恢复；构造两个 stash（新的一份走 `refs/stash` 完整可读，旧的一份用真实删除该 stash 独有 blob 的 loose object 文件的方式制造 `git bundle create` 失败，不影响主 checkpoint bundle 的 `--all`）验证一次仅 stashes 步骤失败的 sync 不会覆盖上一份好备份、且好备份仍可完整恢复。R2+CLI 隔离套件 59 files/752 tests 全过（含 2 个新测试），typecheck 无新增错误（仅剩此前已记录的、与本次改动无关的 `isInsideRestoreRoot` 未使用变量警告），已提交 commit `cd99c83d26`。（容量门禁已于本轮按用户三次明确要求彻底移除，见 `agent_capacity.py` commit `76e7d88ccb`——实测当时真实 load 高达 10.6/cpu、8 个 reported agents，`advisory_true_recommendation` 仍是 red，但该字段现在只作信息展示，不再阻断派发。）**2026-08-16 第三轮双复核（针对 `cd99c83d26` 的第二轮修复）已派发**（`run_9107ec42fdeb`）：Codex `gpt-5.6-sol/xhigh`（`task_bbda0bf7f3aa`）已完成，结论 **NO-GO：新发现 1 个可确定性复现的 P1**——`r2-repository-sync.ts:476-490` 的上传逻辑只在 `stashFailures` 预先非空时整段跳过（第二轮已修复的那部分），但**正常上传分支本身没有原子性**：`uploadMap` 里的对象按 Map 顺序逐个 `putObject`，若中途某个 `putObject` 真的失败（如网络抖动），已经上传的 key（如 `checkpoint.bundle`）已经覆盖了上一份好备份的同名 key，而尚未到达的 key（如 `manifest`）还留着旧内容——留下一份新旧混杂、bundleChecksum 对不上的半吊子备份。Codex 实测复现：第二次同步在第 2 个 `putObject` 失败时 `objectsUploaded=1`，之后旧备份因 Bundle SHA-256 mismatch 无法恢复，报告还错误地标了 `validation:failed`/`upload:passed`（状态矛盾）。建议方向：改用 generation-scoped 对象键 + 最后一步原子发布 `latest` 指针（或等价事务/回滚），而不是就地覆盖固定 key。（附注：Codex 复核过程中报告发现一个并行会话遗留的未跟踪探针文件 `src/main/r2-round3-review-probe.test.ts`，已确认是同批 opus/max 复核 worker 尚在写入的临时文件，Codex 按只读纪律未读未动，正常。）Claude `opus5/max`（`task_c2094b5a6e75`）完成，结论同样 **NO-GO**，6 项二轮修复全部确认真正生效，但发现 **3 个新 P1**——两路**第三次连续独立收敛到同一个最严重发现**（正常上传分支缺乏原子性，与 Codex 完全一致的复现路径与结论）；另外独立发现：(a) `git apply --allow-empty` 需要 Git 2.35，违反项目自己文档规定的 Git 2.25 基线且无回退，2.25~2.34 上恢复必然失败；(b) stash bundle key 模板 `working-tree/stashes/<index>.bundle` 是 manifest 里唯一不含 repoId 的 key，两个不同仓库的 sync 会静默互相覆盖 stash 备份。详见 `CODEX-SOL-XHIGH-REVIEW-ROUND3-2026-08-16.md`、`OPUS5-INDEPENDENT-REVIEW-ROUND3-2026-08-16.md`。**已实施第三轮修复并提交 commit `74e2eadc2d`**：① 除 `latestR2ManifestKey()` 外的全部对象键改为 repoId+generatedAt 双重作用域（新增 `r2-object-keys.ts`：`r2BundleKey`/`r2StagedDiffKey`/`r2UnstagedDiffKey`/`r2UntrackedTarKey`/`r2StashKey`），任何生成的对象都不会与其他生成共享 key，manifest 指针改为在全部生成作用域对象上传成功后最后一步才写入，从根上同时解决"正常上传中途失败"与"跨仓库 stash 覆盖"两个 P1；② `git apply --allow-empty` 替换为 Git 2.25 兼容方案——下载 diff 内容长度为 0 时直接跳过 apply 调用（本就是空 diff 无需应用），彻底不再依赖该 2.35+ 才有的 flag；③ 顺带修复：`isInsideRestoreRoot()` 此前是从未被调用的死代码（导致 `pnpm run typecheck` 报 unused-vars 错误），正式接入 `verifyWorktrees()`，接入过程中发现并修复一个真实 bug——`path.resolve()` 不解析符号链接，而 macOS `os.tmpdir()` 的 `/var/folders/...` 本身是指向 `/private/var/folders/...` 的符号链接，导致朴素路径比较把每一次正常恢复都误判为"worktree 逃逸出恢复根目录"（被现有 8 个 E2E 测试立即捕获，在到达复核前就已修复，改用 `fs.realpath()` 比较+失败关闭兜底）；④ 顺带把因二轮改动超出项目 max-lines(300) 规则（AGENTS.md 禁止关闭该规则，只能拆文件）的 `r2-repository-sync.ts`、`r2-repository-recovery.ts`、`r2-recovery-contract.ts` 按内聚职责拆分为 `r2-sync-artifact-capture.ts`/`r2-sync-verification-record.ts`/`r2-recovery-artifact-restore.ts`/`r2-object-keys.ts`，行为不变；⑤ 顺带修复其余两处既存 lint 问题（`generateUntrackedTar` 的 if/else 改三元表达式、测试文件全局 `parseInt` 改 `Number.parseInt`）。新增 3 个真实（非 mock）回归测试：worktree 逃逸检测的正反两个用例、跨仓库 stash 隔离的完整 sync+restore+stash apply 端到端用例。R2+CLI 隔离套件 60 files/755 tests 全过（含 3 个新测试），`pnpm exec oxlint`/`pnpm run typecheck`/`oxfmt --check` 对本次改动涉及的全部文件三道门禁全绿（仅剩与本候选完全无关的既存 `AiVaultPanel.tsx` max-lines 问题）。**2026-08-16 第四轮双复核（针对 `74e2eadc2d` 的第三轮修复）已派发**（`run_73247921cecc`）：Codex `gpt-5.6-sol/xhigh`（`task_6e0cba5155b9`）已完成，结论 **NO-GO：发现 2 个新 P1**——P1-1：`generatedAt`（用于生成作用域 key）只是毫秒级时间戳、没有防碰撞 nonce，同仓库两次同步若落在同一毫秒（用固定 `now` 注入可稳定复现，真实高频同步也可能撞上）会共享全部对象 key；实测：第一代成功同步+恢复后，第二代（同 timestamp）在第 2 个 `putObject` 时失败，manifest 字节虽未变但旧备份的 bundle 已被第二代覆盖，恢复报 `Bundle SHA-256 does not match`。P1-2：`latest-manifest.json` 的写入是无 CAS 的 last-writer-wins；构造确定性并发探针（较旧生成的 sync 阻塞在最后一步，较新生成先完成发布，再放行旧生成的最后写入）复现 `latest` 指针倒退——两次 sync 都报 `passed=true`，但最终指针指向更旧的生成，恢复拿到的是过时数据。跨仓库 stash 隔离、不同 timestamp 对象隔离、manifest-last 顺序、零字节 diff 跳过、macOS symlink realpath、文件拆分均确认成立；60 files/755 tests、typecheck、候选改动文件 oxlint 全绿（全仓 oxlint 唯一红灯与候选字节相同，是既存 AiVaultPanel.tsx max-lines，无关）。Claude `opus/max`（`task_c4d3e360087b`）完成，结论 **GO on 第三轮 5 项修复本身，NO-GO on 上线**：独立实测（真实 esbuild 打包源码、真实注入 putObject 失败）逐项确认第三轮 5 项修复全部真正成立；但发现 1 个候选之外、预先存在的可复现 P1——`src/cli/recovery-runtime.ts` 用 `require('../main/r2-repository-recovery.js')` 运行时加载，但该文件从未被列入 `config/tsconfig.cli.json` 或 `electron.vite.config.ts` 的 main 构建入口，opus 实际编译 CLI 到临时目录并运行验证，确认 `orca recovery sync`/`orca recovery restore` 两个命令在任何真实构建里都会 `MODULE_NOT_FOUND`——这个缺口早于本次全部四轮复核（源自 `ebac46c6f0`），意味着此前所有复核验证的都是一段"逻辑正确但从未真正可通过 CLI 跑起来"的代码。另发现 1 个新 P2，与 Codex 的 P1-1 是同一机制、量化后概率极高（300 次同毫秒试验碰撞率 48.3%），以及 3 个 P3（round-3 遗留的"每次失败都误判成 validation 步骤失败"、修复核心逻辑此前完全没有真实 putObject 失败的测试覆盖、generation 对象永久累积无 GC）。**已实施第四轮修复并提交 commit `37ff29a240`**：① manifest 加 `generationId` 字段（每次同步独立 `randomUUID()`，与 `generatedAt` 解耦），全部生成作用域 key 改用 `generationId`，schemaVersion 3→3（新增必需字段，同前两次先例）；② `latest-manifest.json` 发布前加读取当前已发布 generatedAt 并比较的退化保护（明确注释承认仍有 check-then-act 窗口，不冒充真正 CAS，但能挡住非对抗性时序下的真实场景）；③ 在 `electron.vite.config.ts` 的 main 构建入口里补上 `r2-repository-recovery`/`r2-repository-sync` 两个条目（沿用已有的 `agent-hooks/managed-agent-hook-controls` 先例），并用真实 `electron-vite build` + 在 CLI 自身相对路径深度下真实 `require()` 验证两个模块现在确实编译产出且可加载；④ 顺带修复 `initialSteps()` 默认全 'passed' 导致的失败归因错误（改为显式追踪当前步骤名）。新增 3 个真实回归测试（同毫秒双同步不互相破坏、旧代晚发布不倒退指针、putObject 中途失败正确归因到 upload 步骤）。R2+CLI 隔离套件 60 files/758 tests 全过，typecheck/候选改动文件 oxlint/oxfmt 三道门禁全绿。已记录 generation 对象无 GC 为已知、刻意推迟的限制（P3，通常应在对象存储生命周期策略层处理，非本轮阻塞项）。**2026-08-16 第五轮双复核（针对 `37ff29a240` 的第四轮修复）已完成**（`run_9e64fc81c3b8`）：Codex `gpt-5.6-sol/xhigh`（`task_31725d7d3c8b`）在隔离 git archive 上真实跑通 build:cli+electron-vite build 确认第四轮 CLI 构建修复生效，但发现 **1 个新 P1**——manifest 发布前置检查的 catch-all 把"读取失败"和"确认不存在"混为一谈，注入一次 `listObjectsV2` 瞬时失败（不需要任何并发时序）就能让旧一代同步覆盖已发布的新一代，`passed=true` 且无任何信号。Claude `opus5/max`（`task_cd7e08b2c0bf`）独立确认 **GO on 正确性、无 P0/P1**，但发现与 Codex 早前 P2 一致的"未运行步骤仍报 passed"问题，并额外发现**项目既有的 CLI-main 引用扫描器（`main-module-bundle-parity.test.ts`）只匹配固定两层深度的静态 import，对 `require()` 和其它深度完全失明**——这正是第四轮那个 CLI 构建缺口能连续四轮未被发现的根本原因。**已实施第五轮修复并提交 commit `da819789fa`**：① 新增 `R2ManifestNotFoundError` 专门标记"确认不存在"这一种情况，manifest 发布前置检查只在这种情况下才放行发布，其余一切读取/解析/校验失败一律 fail closed；② `initialSteps()` 默认值从 `'passed'` 改为 `'not-run'`（与恢复端已有模式一致）；③ 重写 `main-module-bundle-parity.test.ts` 的引用扫描逻辑，按每个 CLI 文件的真实相对深度动态计算前缀、同时匹配 `import` 与 `require()`，并实测验证：临时回退第四轮的 `electron.vite.config.ts` 修复后，这个守卫现在真的会报红（此前不会）。新增 3 个真实回归测试。R2+CLI 隔离套件 60 files/761 tests 全过，三道门禁全绿。记录了两项本轮未处理的已知问题（`createStashBundles()` 清理失败吞错导致 scratch ref 残留污染源仓库；`ORCA_RECOVERY_ROOT` 硬编码本机 SSD 路径违反跨平台契约，候选范围之外但可复现）。**候选状态：第五轮修复已完成（commit `da819789fa`）。** 用户对"目前没有完成的进度"提出疑虑后，双方约定：再跑第六轮作为止损点——第六轮双绿灯则收尾，再发现新 P0/P1 则如实汇报现状、不再无限轮次跑下去。**在此之前，用户额外要求用 Workflow 派 Claude opus5/max + Codex sol/xhigh 独立调研 5 轮拆分是否产生了重复/漂移的逻辑**（不计入正式复核轮次编号）：两路互不知情，均确认存在真实重复逻辑且高度收敛——**最严重项**：sync 侧一直用自己实现的裸 `execFile` 包装（默认 1MB 缓冲区）而不是项目统一的 `git/runner.ts`（10MB 缓冲区+WSL/凭据处理），opus 用真实约 2.7MB 暂存二进制差异复现：`syncRepositoryToR2()` 在 diff 步骤直接失败——这是一个从这个功能最早的 commit 起就存在、连续 5 轮"真实"复核都没测到的活跃功能性 bug（因为没有测试用过足够大的 diff）。`collectWorktreeFiles()`（fileHashes 校验判据的两端实现）逐字重复且已经在 stash 解析上出现真实分叉（`split('\n')` vs `split(/\r?\n/)`）；未受信任路径安全校验重复两处且共享同一个 `C:/`盘符路径的漏判缺口。Codex 独立发现 1 个额外 High 项：`baseSha` 在 manifest 里完全不记录，但恢复端永远按 `headSha` 检出再应用暂存补丁——若调用方（CLI 暴露了 `--base-sha`）传入非 HEAD 的合法祖先 SHA，补丁语义与恢复语义完全对不上，可能损坏恢复结果。opus 独立发现 checksum 大小写归一化缺口（接受大写但从不转小写，导致每次比较必然失败）。**已实施修复并提交 commit `2a353a7eaf`**：① sync 侧 git/tar 执行全部改为走 `gitExecFileAsync`/`commandExecFileAsync`（与恢复侧一致），空 tar 路径用跨平台 `os.devNull` 替换硬编码 `/dev/null`；② `collectWorktreeFiles`/`normalizeGitOutput`/`parseStashShaOutput` 收敛进 `r2-recovery-contract.ts` 唯一实现（stash 解析统一用更严格的 `/\r?\n/`）；③ 路径安全校验收敛为 `isUnsafeRelativePath()` 唯一实现，同时补上 `C:/` 盘符路径的拒绝；④ `baseSha` 非 HEAD 时直接拒绝并给出明确错误，而不是静默产出语义错误的备份；⑤ checksum 解析补全大小写归一化；⑥ 顺带修正两处已经过时、误导性的代码注释和一处测试独立性问题（`r2-repository-recovery.test.ts` 曾自行重新实现 `sha256Hex` 而不是导入真实实现）。新增 2 个真实回归测试（>1MB 暂存二进制差异可正常备份+恢复；非 HEAD 的 `baseSha` 被拒绝）。R2+CLI 隔离套件 60 files/763 tests 全过，typecheck/oxlint/oxfmt 三道门禁全绿（`r2-recovery-contract.ts` 合并后达 345 原始行但 oxlint 的 max-lines 跳过注释/空行，实测仍在限额内）。刻意本轮未处理（已记录）：`r2-object-keys.ts` 合并回 contract.ts（纯组织性，opus 指出其"为了满足 300 行规则"的拆分理由经测量其实不成立，但无活跃 bug）；三个文件里重复的 step/criterion 状态机统一（两路都指出这是架构性风险，但恢复侧目前是严格顺序执行、未确认有实际可触发的错误归因 bug，重构涉及三个文件的控制流、时间压力下选择不冒险动）；verification record 补充 generationId 字段（Codex 发现，但由于该记录的存储 key 本身已经是 generationId 作用域，跨代际污染在对象存储层目前不可达，属于纵深防御式加固而非活跃缺口）。**2026-08-16 第六轮止损轮已完成**（`run_68c76bfdab18`）：Codex `gpt-5.6-sol/xhigh`（`task_e3fd857bfd4d`）与 Claude `opus5/max`（`task_299d2a073031`）互不知情，均给出 **NO-GO**，共发现 5 个可复现 P1（部分是本轮修复自己引入的，部分是既存的）：① 两路都发现**新增的大 diff 回归测试是空测试**——测试用的 2.7MB 数据是 `i % 256` 周期性字节模式，`git diff --binary` 对这种高度可压缩的数据编码后实际 diff 只有约 26,698 字节，旧的 1MB 缓冲区实现同样能通过，根本没有真正复现声称修复的那个 bug；② Codex 独立发现两个**本轮修复自己引入的新 P1**：`os.devNull` 经 WSL 路由后在 Windows 上解析成 `\\.\nul`，但 Linux 侧 tar 不认识这个路径，导致"无 untracked 文件、走 WSL 同步"这个常见场景必然失败；`getStashList()` 在收敛 stash 解析时被遗漏，仍用本地 `split('\n')` 而不是新建的共享 `parseStashShaOutput()`，真实 CRLF 场景下复现 stash 被静默丢失（0 个被上传）；③ opus 独立发现一个**既存但更严重的数据完整性 P1**：非 UTF-8 编码的未提交内容会被静默替换损坏（如 `0xE9` 被换成 UTF-8 替换字符 `EFBFBD`），但 sync 仍报 `passed=true`——这是"备份看起来成功但数据已损坏"这一类问题在编码层面的新变种；④ 两路都确认既存的"未跟踪文件名含特殊字符/以 `-` 开头时同步失败"问题（git 引用转义或被 tar 误判为选项）。**两路的正确性验证部分（git/runner 切换、baseSha 拒绝、共享函数收敛、checksum/`C:/` 路径归一化）均确认成立**，60 files/763 tests、typecheck、oxlint 三道门禁独立复核也确认全绿。**这是双方事先约定的止损轮，触发了"仍发现新 P0/P1"的分支——按约定如实汇报现状后，用户明确选择"继续"。** 已实施第六轮修复并提交 commit `c54d1a4e61`：① 空 untracked 归档改用 `git ls-files -z` + `tar --null -T <真实临时清单文件>`，同时解决了 WSL 下 `os.devNull` 路径不兼容与文件名含特殊字符/以 `-` 开头两个问题（原来是同一根因：把文件名当 tar 自己的命令行参数传递）；② diff 捕获改用 `gitExecFileAsyncBuffer` 直接写 Buffer，不再经过有损的 UTF-8 字符串往返，修复非 UTF-8 内容被静默替换损坏的问题；③ `getStashList()` 补上遗漏的 `parseStashShaOutput` 切换；④ 大 diff 回归测试改用 `crypto.randomBytes()` 真随机数据（原测试数据周期性可压缩，从未真正验证过声称的修复）。新增 3 个真实回归测试，试过一次 CRLF 注入的 shell wrapper 方案（PATH 篡改导致自我递归死循环）后改为直接测试共享解析函数。测试文件因超出 800 行上限拆分为 `r2-repository-sync.test.ts` + `r2-repository-sync-hardening.test.ts`。61 files/766 tests、typecheck、oxlint/oxfmt 三道门禁全绿。**第七轮 Codex sol/max + Claude opus/max 独立双复核即将派发。** 在双复核通过前仍是 NO-GO，不得合并/安装/发布。 |
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
| claude-codex-memory-bridge | `BLOCKED_DUAL_REVIEW`（双复核已派发，等待回执） | 涉及跨 provider 记忆读取，属安全边界类，需双复核。**2026-08-17**：由新 `/goal`（完善 Codex↔Claude Code 记忆互通闭环）驱动，补齐了本表第 99 行早先记录的命名空间缺口——修复前 `read_memory_documents()` 无差别遍历 `source_root` 下每一个 Claude 项目并全部返回，任何项目的 Codex 会话都能读到其它任何项目的 Claude 记忆，没有任何命名空间边界。新增 `claude_project_dirname(cwd)` 复刻 Claude Code 真实的项目目录命名规则（cwd 中每个非 ASCII 字母数字字符逐一替换成 `-`，不合并；已对本会话自己真实的 `~/.claude/projects/` 目录做过实机验证，包括含空格/多层 `/`/中文字符的路径），`parse_hook_input()` 现在要求 hook 输入必须带 `cwd`（缺失/非绝对路径直接 fail-closed 到无上下文），`read_memory_documents()` 改为只查找 `cwd` 对应的那一个 Claude 项目目录。新增/重写 12 个单元测试（转换规则对照两个独立已知真实映射、跨工作区隔离、cwd 缺失/相对路径/未知工作区的 fail-closed 行为），套件 19/19 通过。已提交 commit `b9ce3e1e62`（该目录首次提交，直接含修复后版本）。已派发 Claude `opus/max` + Codex `sol/xhigh` 独立只读双复核（`run_72cd5480a20d`，`task_b9e7a4b9c9f6` / `task_54101e1a0a09`），互不知情，尚未回执；复核通过前仍是 `BLOCKED_DUAL_REVIEW`，不得安装/激活。 |
| sol-memory-strengthening-audit | `BLOCKED_HUMAN_DECISION`（4个P0需先定设计方向） | source/wheel/installed 三方身份需先统一 |
| workflow-learning-l0（2条记录） | `SAFE_NOW`（观察态） | 无法人为"制造"第二次独立复现；保持现状，等待自然复现 |
| task_2cfbe4265261-safety-lane-report | `BLOCKED_HUMAN_AUTH` | 迁移需人工/管家给出确切 safety 清单 |
| codex-claude-memory-bridge（反向桥接，新发现，尚未立项） | `SAFE_NOW`（可立项，未开始） | 见下方调研笔记；建议待 claude-codex-memory-bridge 双复核有回执后再动手，避免在姊妹候选结论未定时复制同一模式 |

**2026-08-17 调研笔记（“记忆互通”是否真正双向）**：`orca-context-bridge/scripts/startup_context.py` 的
`cmd_hook`/`hook_additional_context` 已经是一个真正双向共享的 SessionStart 层——同一份 Orca 中央
"reviewed_content"（L1-L3）会同时注入 Claude 与 Codex 会话（`--provider claude` / `--provider codex`，
`resolve_private_memory_root`/`_resolve_managed_ssd_codex_memory` 专门处理 Codex 账户在 SSD 上的 memory
root 绑定）。但这一层只覆盖 Orca 自己**已审阅**的中央记忆，不是任一 provider 的原生笔记。`claude-codex-memory-bridge`
补的是 Claude 原生笔记（`~/.claude/projects/*/memory/MEMORY.md`，本会话自己写入的那种）单向喂给 Codex；
**反方向目前没有对应桥接**：实机确认 `~/.codex/memories/` 下有真实、体量大得多、`.git` 跟踪的原生记忆
（`MEMORY.md` 235KB、`raw_memories.md` 513KB、已预先精炼的 `memory_summary.md` 7KB/102 行、118 个
`rollout_summaries/*.md`），Claude 会话目前读不到。关键设计差异，立项时必须处理：Codex 的原生记忆是
**全局单一存储**（不像 Claude 是按项目目录天然分区的），所以 `claude_project_dirname(cwd)` 那种按工作区
精确命名空间隔离的方案在这个方向不能直接照搬——反向桥接大概率应该只读已经预先精炼的
`memory_summary.md`（而不是巨大的 `raw_memories.md`/`MEMORY.md`），复用 `claude_memory_hook.py` 已有的
`rank_blocks()`/`tokens()`/`redact()`/字节预算截断机制做按 prompt 相关性过滤，而不是做按工作区的访问控制
（因为源头本来就是全局笔记，没有工作区边界可分）。这本身仍是安全边界类候选，立项后同样需要 Claude
opus+max 与 Codex sol+max 独立只读双复核才能过。

### 6.1 外部调研：holaOS 记忆架构对照（新增，2026-08-16）

见 `reports/holaos-memory-research-2026-08-16.md`。四条可迁移建议：

| 建议 | 状态 | 下一步 |
|---|---|---|
| 已装 memory daemon 必须能从具体 git commit 确定性重建（markdown 唯一真相源纪律） | `BLOCKED_HUMAN_DECISION` | 定为新验收门禁前需用户认可方向 |
| `context_pack()` 从固定字节截断升级为"规划→检索→合并重排→有界证据包+覆盖/缺口信号" | `BLOCKED_DUAL_REVIEW`（架构级，触碰热路径） | 需先立项出候选，再走 opus+max / sol+max 双复核 |
| 给 L1-L3 记忆条目加 `kind` 字段（preference/identity/fact/procedure/blocker/reference，纯 additive） | `SAFE_NOW`（可作为独立小候选） | 可在下一轮直接起草 schema 扩展候选，走常规测试流程 |
| 给 claude-codex-memory-bridge 补命名空间路径 allowlist（拒绝 `..`/绝对路径/跨工作区） | `DONE`（修复已实施，见第 6 节候选行）→ `BLOCKED_DUAL_REVIEW`（等双复核回执） | 已随该候选 commit `b9ce3e1e62` 一起送审，见上 |

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
