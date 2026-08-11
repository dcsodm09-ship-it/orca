# 完善 Orca 未完成工作·交叉审核修订版(2026-08-12)

来源:本机 Claude Code 协调者读取仓库/worktree/git 状态,派 `codex-bulk` 独立只读核查形成草稿,
再分别派**真实 Codex(gpt-5.6-sol,design 档,xhigh 推理)**与**独立 Opus 5 子代理**两路互不知情的
交叉审核(与本项目 `codex-prompts-model-tiering-memory-ssd-p1.md` 里已验证过的方法论一致)。
本文档是审核后的合并结果,不是原始草稿。全程只读,未修改任何文件、未跑任何写入类 `orca` 命令、
未安装/激活任何东西、未联网。

状态标签说明:`已验证`=有实测证据(实际运行进程、真实文件比对、命令输出);`已提交未合并`=有 git
commit 但未进 origin/main 或任何集成分支;`离线候选`=只有测试/静态验证,无生产/运行态证据;
`阻断`=明确需要人工决策才能继续。

---

## P0:最高优先级——发布权威根未定(几乎所有下游问题的根因)

**发现**(Opus 独立复核,git 命令实测):

```
projects/orca@main = 828b5d8a96,parents=[],树内 0 个文件
git rev-list --count main            → 1
git rev-list --count origin/main     → 367
git merge-base main origin/main      → (无输出,无共同祖先)
```

`完善orca` 与 `projects/orca` 共用这个空孤儿提交作为检出,因此:
- `完善orca` 下 `git ls-files | wc -l` = 0(零受控文件),45 个 closure 目录全部 untracked。
- W0 所谓"5,061 条 evidence untracked 待分类"是这棵空树的**结构性必然**,不是待办积压——
  不管分类多少次,只要根还是空的,新增内容永远是 untracked。
- freshness 闸门(`build_startup_bundle.py:1387-1395`)对比的是这棵空树的 `git status`;因为
  注入器自己把 `.orca/context/` 写进这棵没有 `.gitignore` 的树,状态必然常年是脏的——本会话
  收到的 `ORCA_CONTEXT_NACK_V1` 就是这个自触发闭环的直接产物,不是外部条件在报警。
- 真实已提交的工作分散在 **7 个以上互不相通的分支头**(W2 记忆、W10 账号绑定、W6 SSD/PTY、
  W3、track2、track3、track7 等),全部以 `6db25c5e9a`(它本身也不在 origin/main 上)为基线各自
  演化,没有一个能收敛的合并目标。
- 其中 2 组分支存在**完全重复的 patch-id**(`ae27ef5e...` 与 `c0b07398...`,后者是 82 files /
  24,674 insertions),如果分别合并会把同一份改动重放两次。

**阻断,需要人工决策**:`projects/orca` 到底应该接上一条真实的、从 `origin/main` 派生的历史,
还是正式宣布 `完善orca` 是非 Git 的证据库(freshness pin 改绑内容清单而非 `git status`)。
这个不决定,下面所有"合并进集成分支"都没有实际目标。**本报告不代为决定,也不会擅自改写这个
分支**,因为它是当前活跃 Orca 编排 Run(`run_9959a2273423`)和另一个协调者终端共用的检出。

---

## 1. 记忆(Memory)/ 技能(Skills)——含 Claude/Codex 共享与"底层完全互通"

**已验证**:base-layer 共享是真的,而且已经在服务真实会话,不是"离线候选"。
- `~/.claude/settings.json` 有 `UserPromptSubmit` hook 接了
  `orca-agent-memory-context-pack-hook`(`--scope orca.shared-agent-context`)。
- 记忆守护进程实测在跑(pid 1052),真实 SQLite 数据(`memory.sqlite3` 573,440 字节 +
  WAL 2,167,152 字节)。
- 本报告协调者在本轮对话里亲历过这个 hook 真实注入一次 `ORCA_AGENT_MEMORY_CONTEXT_PACK_V1`
  context pack——base-layer 共享不是设计声明,是当场观测到的行为。

**已提交未合并 / 有具体缺口**:
1. **导入目前单向**:只有 `import-native-memory`(provider → sidecar),`native_memory_importer.py`
   里 grep `export` 零命中,没有任何回写原生 MEMORY.md 的路径。"双向导入"目前是缺失的功能,
   不是缺一个测试。
2. **跨 provider 读取测试绕过了治理层**:`test_native_memory_importer.py:50` 的集成测试存在且
   真实启动 daemon,但第二个"reader"是直连同一 state-dir 的 SQLite 句柄,不是走
   capability/context-pack 的受审路径。证明的是存储层共享,不是被治理的跨 provider 互通。
3. **部署漂移(比"未合并"更严重)**:已安装的 `orca_memory.py` SHA-256 与
   `orca-w2-memory-skills-9959` 分支上的完全不同,且已安装目录里**没有** `native_memory_importer.py`。
   也就是说正在给真实会话供数据的守护进程,是一份**不在任何 git 分支上的构建**。
4. `feat(memory): import bounded native memory slices` / `route explicit cloud escalations` 两个
   commit 仅存在于 `orca-w2-memory-skills-9959`;`add governed sidecar ship set` 与集成基线里的
   `91b7f94e6a` patch-id 完全相同(见上方 P0 的重复 patch 风险)。

**技能(Skills)**:`orca skills list/get/install/update` 本身 provider-agnostic,是已完成的跨
provider 分发机制。但 `review-orca-workflow-learning` 仍需 isolated forward-test 与 rollback E2E;
`paperclip-orca-closure` 离线验收通过但 `activation_permitted=false`;另外发现
`知识图谱-Workflow-完善` 工作树里藏着一个完全未受追踪的 `graphify` 技能(见第 6 节),原始清单
遗漏了它。

**要让"底层完全互通"这句话完全成立,还差**:(a)版本锁定已部署的 daemon 到一个真实 commit;
(b)补上导出/回写路径,做到真正双向;(c)把跨 provider 读取测试改走 capability/context-pack
受审路径;(d)决定是否/如何把 context-pack 挂上真正的 SessionStart hook(目前 README 明确说
"deliberately an explicit command today")。

---

## 2. 编排(Orchestration)

- **Scheduler**(`orchestration-dynamic-scheduler/`,独立 Python 规划器,非 Orca TS 源码):
  completion-plan 3/3、scheduler 12/12 本地测试通过,`README.md:16` 明确"output is a decision
  receipt only, no side effects"——"需要编译进 Orca"是范畴错误,这个模块本来就不需要编译。
- **W10 per-worker 账号绑定**(`orca-w10-scheduler-9959`,HEAD `4a58589d9e`,36 文件,669 Vitest
  测试通过,typecheck/lint/build 全绿):代码完成度高,但该分支自己的 `package.json` 版本
  `1.4.177-rc.0` **比正在跑的 App(1.4.179)还旧**,运行态确认不 advertise 这个能力,需要在正确
  基线上重新编译 + 重启才算真正部署。
- **已提交但原清单遗漏**:`dc114041ad feat(orchestration): add auditable MCP bridge`(22 文件,
  +2088/-25,新增 `orchestration-mcp-server.ts`/`orchestration-mcp-service.ts`/
  `orchestration-mcp-write-policy.ts`)已提交,且在全部四条 model-tiering track 基线里。
- **Model tiering(Track 0-4)**:Track 0(统一基线)、Track 2(按账号能力探测)、Track 3
  (diff 规模分级,已修正接入顺序)已提交;Track 1(worker-start tier 字段)在合并基线上但有
  12 个文件未提交(全部未暂存);Track 4(automations tier)有 28 个文件改动但 26 个已暂存——
  按暂存状态看,Track 4 实际推进度不比 Track 1 差,原始判断"Track 4 最落后"是误读。
- **Desktop MCP peer 鉴权**(生产阻断,W9):owner-only Unix socket 无法区分同 UID 下的不同进程,
  代码文档原话"do not authenticate one same-user process from another; production use remains
  blocked"。目前没有任何候选方案,是纯待办,需要 code-signed XPC + audit-token。

---

## 3. 注入 / 启动上下文(Startup Context / Hook,W0/W1)

- **NACK 优先于内嵌 ACK 的规则已经存在且有测试**,但没接进真正给模型看的文本:
  `startup-live-integration-v4/integration_guard.py:290-291` 明确
  `if first == "ORCA_CONTEXT_NACK_V1": return {"accepted": False, "code": "CONTEXT_NACK"}`,
  对应测试套件用的就是本次会话实际遇到的那条 reason 文本,19/19 通过
  (`startup-live-integration-v4/REPORT.md:79`)。但 `projects/orca/.orca/context/startup-context.md`
  第 9 行仍然无条件写着要求模型输出 `ORCA_CONTEXT_ACK_V1 ...`——规则存在,但没有下沉到模型
  实际读到的注入文本里。
- **live 注入路径比自己的审核闸门更松,方向和原草稿判断相反**:真正注入模型的知识图谱是
  `projects/orca/.orca/context/knowledge_graph.json`(35 节点/54 边),其 Graphify 资产来自
  `优化本机code` 工作树(pin 在 `6db25c5e9a`),该工作树当前有 19 个未提交文件,其中 18 个
  `src/` 文件的 mtime **晚于**图谱生成时间。根因是 `build_startup_bundle.py:663-666` 的 scope
  检查只在 `content_source_closure is not None` 时触发,live 构建路径传的是 None,绕开了这道闸。
  与此同时,`完善orca` 作用域内的同类候选(`startup-reviewed-pack-schema3-fastfix`)因同样的
  scope mismatch 被**正确拒绝**——审核层比 live 注入层更严格,这是一个安全语义倒挂。
- **Claude 侧 ACK 回执为零**:`.orca/context/acks/` 下 10 份回执全部前缀 `codex-`,没有一份
  `claude-`;`launches/*/startup-context.json` 的 `launch.provider` 也全是 `"codex"`。这直接反证
  了 `完善orca` worktree comment 里"Claude+3 Codex 同 bundle 注入 E2E 已通过"的说法——目前没有
  磁盘证据支持这句话。
- `startup-live-integration-v4/REPORT.md:131-133`:frozen reviewed-pack v3 候选的 manifest 实际是
  **schema 2**,而当前 builder 要求 **schema 3**,这是一条具体的、可执行的版本错配阻断项,原始
  清单未提及。
- `startup-loader-closure`:仍是 `NO-GO_PENDING_FRESH_INDEPENDENT_SECURITY_REAUDIT`。

---

## 4. SSD 迁移索引 / R2(W3-W6)

- 全部离线候选,无生产执行:
  - R2 生产 policy 未 pin(`policy_sha256: None`,`r2-production-trust-closure/REPORT.md:29`),
    combined_tests 13/13(不是 12)通过但生产命令 fail-closed。
  - 唯一的 reader 验证只覆盖单个 53 字节文件、0.1% 抽样(2026-08-10),不是完整恢复演练。
  - 内置盘归档(`archive/internal-old-orca-final-...`):`XATTR-DIAGNOSTIC-RECEIPT.json` 的
    `copy_progress` 显示 `fully_verified_roots: 2, copied_but_incomplete_roots: 1,
    unstarted_roots: 13`——**16 个根里 13 个根本没开始复制**,不是"卡在元数据噪音这最后一步"。
    另外 `archive/` 下有两次独立归档尝试(`...010254Z.acfbb078` 与 `...011749Z.c943d885`),
    原始清单只提了一次。
  - SSD native storage / PTY(`orca-w6-native-release-9959` 的
    `4542e6b4e4 feat: enforce native storage and PTY lifetimes`,62 文件,+5786/-90)已经是真实
    源码提交,不只是测试报告——`production_ready=false` 是因为 XPC/Mach service 未签名/未安装/
    未注册进真实 App,不是代码不存在。
  - Android 退役:冷备 + 默认路径切换已完成,真实删除(退役)未授权、未执行。
- P0 里"最近一次 R2 备份失败(`orca-ssd-r2-backup.failed`,context canceled)"这条,两路独立审核
  均未能在磁盘上找到对应文件证据(`find` 全目录未命中)。**在有人指出具体路径前,不应继续把它
  当阻断项使用**。

---

## 5. 知识图谱(Knowledge Graph)

- `.orca/context/knowledge_graph.json` 存在**两份、体量相差 30 倍**,原始清单张冠李戴:
  - `完善orca/.orca/context/knowledge_graph.json`:636,957 字节,1183 nodes / 1115 edges——**这份
    不是被注入的那份**。
  - `projects/orca/.orca/context/knowledge_graph.json`:28,025 字节,**35 nodes / 54 edges**——
    这才是真正注入给模型的那份,与 `startup-context.md:21` 一致。
- **原始清单遗漏的最大工作量**:`知识图谱-Workflow-完善` 工作树(分支
  `dcsodm09-ship-it/知识图谱-Workflow-完善`,HEAD `b5ad2ac163`,2026-08-06)有:
  - `git status --porcelain | wc -l` = 157
  - `git diff --stat HEAD` = 118 files changed,+4473/-859(**另有 39 项 untracked 完全没被
    算进这个 diff**,真实改动体量比 +4473 更大)
  - **两路独立审核均确认**这不是无关的知识图谱旁支工作,而是直接碰了 W1
    (`src/main/providers/ssh-agent-session-*.ts`,6 处修改)与 W2
    (`src/main/runtime/orca-runtime.ts` 里的 `buildAgentStartupPlan`/`MemorySnapshot`,+798 行)
    的核心文件,并且与 automations-tiering(Track 4)在 `src/main/persistence.ts`、
    `src/main/persistence.test.ts`、`src/main/runtime/orca-runtime.ts`、
    `src/renderer/src/lib/launch-agent-background-session.ts` 这 4 个文件上直接冲突。
  - 里面还藏着一个完全未受追踪的 `graphify` 技能(`skill-guides/graphify.md`、
    `skill-stubs/graphify.md`、`skills/graphify/SKILL.md`、
    `config/scripts/graphify-skill-guidance.test.mjs`,均 untracked,`origin/main` 上零命中)。
  - HEAD 是 `origin/main` 的祖先(`git merge-base --is-ancestor` 通过),但 `origin/main` 已经
    走到 2026-08-10 的 `a63df91790`,落后约 4 天。
- **阻断,需要人工决策**:这份工作要归并进 W1/W2,还是继续作为独立分支——目前没有任何地方在
  追踪它,是本次调研发现的最大未受管理的工作量。

---

## 6. Wiki

`完善orca/wiki/` 4 个文件、约 100KB(CLI capability inventory、context-wiki.json、两份中文文档),
各 closure 内部还有零散副本。纯治理类:按 W9 清单处于"审核后纳入 ship-set 或归档"待决策阶段,
没有更多技术缺口。

---

## 7. Git / Release(W8)

- `release-cut.yml`(不是 `release.yml`,原始清单文件名有误)已经是修好的形态:
  `36b66e5a54 ci: gate release publish on immutable E2E` 已提交在
  `dcsodm09-ship-it/orca-w6-native-release-9959` 上(5 文件,+692/-48,含新增
  `config/scripts/run-release-e2e-workflow.mjs` 299 行),`publish-release` 已经依赖
  `release-e2e`。这是**已落为分支源码**,不只是离线 patch,但尚未进 `origin/main` 或任何集成
  分支。
- 孤儿提交 Track 7 已确认**提交**(`codex-track7-landing` 上 `fc97382537`,基于正确的
  `6db25c5e9a` 基线做的新 cherry-pick),但因为 `6db25c5e9a` 本身不在 `origin/main` 历史里,
  准确说法是"已提交在未合并分支上",不是"已落地"。
- 缺口:没有真实 GitHub Actions dispatch/draft tag 的运行回执;没有 E2E 失败、超时、重复发现、
  tag 被移动、receipt 被篡改这几种场景的真实测试,只有离线模拟。

---

## 建议的推进顺序(只读研究结论,不代表已授权执行)

1. **人工决策**:`projects/orca@main` 的定位(真实历史 vs 非 VCS 证据库)——不决定,后面所有
   "合并进集成分支"都没有目标,且 freshness 闸门会持续自触发 NACK。
2. **人工决策**:`知识图谱-Workflow-完善` 工作树归并去向。
3. 补 `projects/orca` 的 `.gitignore`(或让注入器写到工作树之外),解开 freshness 自触发闭环
   ——这是一个改动面很小、风险很低的修复,但仍需要在决定第 1 条之后再动,避免和活跃编排 Run
   冲突。
4. 关掉 live 知识图谱注入路径比审核闸门更松这个具体 bug(`content_source_closure` 传 None
   绕过 scope 检查)——这是当前唯一一处"注入层比审核层弱"的地方,安全语义上最不该保留。
5. 去重两组重复 patch-id 分支,再收敛 7 个分叉头。
6. 在此基础上,W1 的 ACK E2E、W3-W6 的 R2/SSD、W8 的 release 才具备可验证的合并前提。

以下动作仍需各自独立、明确授权,本报告不代为批准:生产安装、Hook/记忆/Skill 激活、live
settings 或 `.orca/context` 修改、R2 启动、真实归档复制、内置盘退役/删除、进程 signal、
Hammerspoon reload、GitHub/远端写入。

---

## 方法说明与局限

本文档基于文件、git 状态与既有报告的只读观察;凡引用"X/Y 测试通过"均来自既有报告或本次只读
命令的直接输出,不代表本轮重新跑过全部测试。仅覆盖用户点名的领域(记忆/编排/技能/注入/特性/
SSD 迁移/知识图谱/wiki/git);`完善orca/` 下仍有若干目录(scripts/tests/reports 治理、
ego-capability-fixture、`.pty-*` 可重建候选等)未在此文档展开,详见原始
`UNFINISHED-WORK-CONFIRMATION-2026-08-11.md`。
