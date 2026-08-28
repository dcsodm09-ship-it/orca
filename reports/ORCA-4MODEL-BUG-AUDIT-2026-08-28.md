# Orca 系统 4 模型独立调研报告（2026-08-28）

**范围**：本工作区（完善orca）围绕 Orca.app 构建的可靠性/安全基础设施——claude-codex-memory-bridge、orca-context-bridge（catalog + Gate B/C/D）、orca-dispatch-guard、prime-agent-integration、install_bridge 等。
**方法**：Workflow 编排 7 个 agent：1 个侦察（haiku）→ 4 个独立调研模型并行（Claude opus/max、Codex sol、Grok、Gemini，互不知道彼此结论）→ 1 个综合去重（opus/max，并对关键事实做了自己的独立复核）→ 1 个独立验证（对综合结论里全部 confirmed_bugs 逐条亲自复现）。

## 0. 先说方法论上的一个重要发现：这次实际不是真正的"4 路"

- **Codex** 在 gpt-5.6-sol 达到 codex-pool 900 秒超时前只做完约一半调研，之后由降级到 Haiku 4.5 的传话机制打包了已取得的部分结果。它产出的 2 条"bug"里，**1 条（importlib AttributeError）被验证是它自己加载脚本方式的假阳性，1 条（P0 混合语言脱敏不完整）实测无法复现、引用的输出与真实结果不符**——已被剔除。这是本机已知的 codex-design 长任务超时问题（[[reference_codex_design_tier_dispatch_600s_timeout]]）在 Workflow 的 agentType 派发路径下再次复现，说明之前记的"用顶层 Agent 工具能避免"这条经验不完全成立，Workflow 内 agentType 派发同样会撞这个超时墙。
- **Gemini** 自己在 investigation_notes 里承认"未能直接执行 git diff，基于侦察报告做静态分析推演"——它的两条 bug 全部证据都是转述侦察材料原文，没有一条来自它自己新读的文件，其中一条还把根因指错了文件。它的"附议"不构成独立佐证。
- 真正做了独立代码审查、给出可复现实测证据的是 **Claude（opus/max）** 和 **Grok**，二者互补多于矛盾。

**结论：这次审计的证据强度应按"2 路深度独立审查 + 1 路部分可信 + 1 路基本不算独立"来理解，不是字面意义的 4 路平权。**

---

## 1. 已确认的 bug（9 条，全部被综合环节独立复现，verdict 均为 CONFIRMED）

### P1 · 唯一在用的 Codex 账号丢失脱敏 hook，verify 仍报绿（4 模型一致）
Orca 真实账号注册表有 3 个 Codex 账号，其中 `8d7db875`（唯一活跃，8/20 以来 500+ 会话）的 `hooks.json` 不含记忆脱敏 hook——同目录的 `.bak` 文件证明 hook 曾经在、后来被更老配置覆盖掉了。`install_bridge.py` 把账号枚举硬锚在 SSD 上一份 8/11 建的静态快照（只有 2 个账号）上，对这个账号完全不可见，`verify` 因为共享同一枚举盲区仍然返回 `ok:true`。
**修法**：先用该账号自己的 `hooks.json.bak` 止血；根因是 `install_bridge.py:430` 的枚举根要改成以 Orca 真实注册表 `~/Library/Application Support/orca/codex-accounts/` 为准，`verify` 对"注册表有、没管到"的账号要显式报 `unmanaged` 并返回非零（当前是 fail-open）。

### P1 · 线上 redact() 有实测可复现的二次方回溯（ReDoS），会打穿 5 秒预算且失败完全静默
线上冻结版本对特定形状文本（长 dash 拼接 id、kebab-case URL）触发教科书级 O(n²)：4000 字符 1.4~1.8 秒，8000 字符 5.9~6.2 秒，已超过 hooks.json 写死的 5 秒超时；32000 字符要 109 秒。日常散文/base64/hex 不触发，但一旦触发，`main()` 的 `except: return 0` 会让失败完全静默——不报错、不注入，用户只会觉得"记忆好像没生效"。**已提交的 git HEAD 版本同样有这个问题**（回退版本代码不能规避），唯一有效修复只在未提交的工作区草稿里。
**修法**：把 ReDoS 正则有界化修复从那 7000 行草稿里拆出来单独提交（不等 redact_v2 完成），重新发布 release 并重签哈希。

### P1 ·（本次综合环节新发现，4 个模型都没找到）唯一能修复上面这个 ReDoS 的代码只存在于一块盘上，而自动 git 备份已经静默失败了很久
`com.local.git-backup-sweep.plist` 每 30 分钟跑一次备份，但日志里全是 `Operation not permitted`（launchd 缺完全磁盘访问权限）。当前分支 `完善orca` 无 upstream，最近 5 个提交（含 Gate C/D 安全工作）不在任何远端分支上，根目录 67 个未追踪复核文档、54 个未追踪目录。**好消息：fork 远端 `github.com/dcsodm09-ship-it/orca` 早就配好了，只是从没对这个分支推过**，修复成本是一条 `git push -u` 命令。

### P1 · install_shared.py 的 `--update` 仍是破坏性全树替换，2026-08-26 真的删过 3 个线上文件
事后只把哈希 pin 改成匹配"删除后"的状态，安装器代码一行没改——同样的 `--update` 再跑一次会再删一次，而且删的正是 SessionStart 信任链依赖的文件。
**修法**：`--update` 前先枚举"已部署有、源目录没有"的文件，非空默认拒绝，加 `--allow-delete` 才继续。

### P2 · catalog 把"项目自己的 agent worktree"误判为不同项目，per-project 提醒静默失效
`hgcloud`、`rn邮箱` 各自的仓库根目录和它们自己的 `.claude/worktrees/agent-*` 被判定为 "project_id 歧义"，导致 SessionStart 第二行 per-project 提醒被 poison。已做端到端实测：只影响第 2 行，第 1 行全局摘要仍正常；但会随着"开 worktree"这个日常操作持续扩散。

### P2 · 受保护文件哈希 tripwire 长期红灯，护栏已被告警疲劳架空
`~/.claude/settings.json` 因一个无关改动（8/27 加的 local-canvas-editor hook）而哈希不匹配，测试永久红——真正的恶意/意外改写发生时不会产生任何新信号。同一测试文件里还有另一处长期红（catalog 路径冲突），整个 `orca-context-bridge` 测试套目前是"已知红"状态。

### P2 · orca-dispatch-guard 的故障检测仍是裸子串匹配，误判会导致重复派发
`is_stalled_false_positive` / `is_capability_revoked_failure` 只看 stdout+stderr 全文里有没有 `"agent_prompt_stalled"` 子串——而这个项目派发的任务描述本身经常就包含这个词。已实测复现误判。同文件里的兄弟函数 `_find_capability_revoked_hit` 在 `879b9c904f` 已经改成结构化字段提取，这两个更严重的（会触发真实重复写操作）却被留在原地。

### P3 · `~/.local/bin/orca` 是一条指向不存在文件的悬挂 symlink
当前 PATH 顺序下不会立刻出问题（先命中 Orca.app 自带 bin），但环境一变就会踩雷。

### P3 · 仓库根目录堆积 2.7GB 陈旧 `.pty-*` 临时目录（占仓库体积 79%）
mtime 全部停在 8/11，清掉后仓库从 3.4GB 降到 0.7GB，顺带让上面的备份成本降一个数量级。

---

## 2. 历史冲突（7 条）

1. **MEMORY.md/安装收据说脱敏 hook 已装 3 账号，实际主账号裸奔**——已被证实方向"反了"：保护装在两个零会话的账号上。
2. **线上 release 冻结于 8/20，比中文 PII/CJK 脱敏工作（8/22 提交）早两天**——线上代码一条中文脱敏规则都没有，而这台机器的记忆内容基本是中文，错配正好落在最需要它的地方。
3. **SKILL.md 两处仍写 `draft --from-discovery-hit` "intentionally unimplemented"，上一个提交刚把它实现了**——而且这是"改代码不改文档"这个模式的第二次复发（上上个提交才修过同类问题）。
4. **`assign_project_ids` 的 docstring 断言"歧义只可能来自 fallback tier、零碰撞"，与线上真实 7 条歧义（全部来自 derived tier）直接矛盾**。
5. **CLAUDE.md 撤回 orca-dispatch-guard 默认地位所依据的两个具体 bug 都已修好**（`cmd_wait` 批量 ack、故障恢复 remount 追踪），但文档正文仍用现在时描述"有两个真实 bug"——结论（保持可选）依然对，但依据已经过期，容易误导读到这段的 agent。
6. **SKILL.md 声称 catalog_session_hint "完全只读、最多两行"，两处描述都已不成立**（有一条写路径例外、输出已经是三行）——"改代码不改文档"模式的第三次复发。
7. **prime-agent README 仍写"尚未安装"，实际 8/22 就装好了**；顺带发现本次侦察脚本本身把一条 symlink 的目标搞错了（被 Claude/Grok 各自独立纠正），提醒以后侦察材料也要当"待核实输入"而非事实底座。

## 3. 缺失能力路线图（按优先级）

**高优先级**
- Release ↔ git HEAD 漂移门禁 + hooks.json 定期存在性/哈希巡检（今天加上就会立刻亮红：三份世代互不相同）
- 账号枚举对齐 Orca 真实注册表，verify 对未管理账号 fail-closed
- ~~给记忆桥 hook 加最小可观测性（超时/异常目前完全无声）~~ **已做（2026-08-28，commit `a897e81f63`）**——见下面 §5
- 把复核记录纳入版本控制 + 修好失效的自动备份 + push 到已配好的 fork 远端 → **备份已修好**（见 §5），复核记录已提交，fork push 待人工决定

**中优先级**
- 编目层把嵌套 worktree 从"歧义"里摘出来，而不是连累父项目
- install_shared.py 改成增量/白名单更新
- 补上设计文档要求的 `REDACT_ENGINE` 模式常量，redact_v2 结构上目前无法推进
- 建立真实世界混合场景的脱敏覆盖度测试语料库
- ~~把 `ee982fc0a8`（agent_prompt_stalled 修复）至少 cherry-pick 进开发分支 HEAD~~ **这条作废，不要执行**——已被上游修复取代，见下面 §5

**低优先级**
- Gate B/C/D 全 fleet 0 个项目有 compat-check.json，流水线还没在真实数据上跑过——这恰恰支持了"Stage 6-8 刻意不做"是对的
- ~~reviewed pack 的签名字段在已部署校验器里根本不被读取（Grok 单源，未复核，建议顺手查一下）~~ **已查证属实并已修文档（commit `e790c306e9`）**，但底层完整性缺口仍未关闭——见 §5

## 5. 路线图 4 条小项的复查与处置（2026-08-28）

对上面 §3 里 4 条小项做了独立复查并落地。逐条结论：

### 5.1 记忆桥 hook 可观测性 —— 属实，已修复（`a897e81f63`）

复现确认：拿故意写错的 `--expected-script-sha256` 调用**已部署的 release**（即被篡改或漂移的脚本，
安全上最要紧的那种失败），结果是 **exit 0、stdout 空、stderr 空**。Codex 也不落盘任何 hook 退出码
或 stderr，所以这类失败此前完全无从察觉。

已加 `~/.local/state/claude-codex-memory-bridge/hook-journal.log`（每行一条 JSON，`0600` 文件在
`0700` 目录内，超 1 MiB 轮转）。几个刻意的设计点：

- **放内部盘不放 SSD**：和 doctor plist 的日志同理——`/Volumes/Extreme SSD` 上的日志在 launchd
  会话里根本写不进去（TCC 拒绝可移动卷），而且在最需要它记录的那个故障（SSD 没挂载）里恰好不可用。
- **动手前先写 `start` 记录**：`hooks.json` 的 `"timeout": 5` 是由 harness **杀进程**实现的，进程内
  任何 `except` 都不可能观测到超时。**同一个 `inv` 只有 `start` 没有终结记录 = 被杀**（超时/OOM/SIGKILL）。
  这是唯一能看见那 5 秒杀进程的结构。
- **只记失败的"类别"，绝不记载荷**：只写 phase + 异常类型，消息仅当命中源码里 35 条固定字面量的
  白名单时才写。**这里纠正了调研报告的一个前提**——报告说 `BridgeError` 的消息都是固定字面量，
  但 AST 扫描出 4 条是插值拼出来的，其中 3 条会带着记忆文档的文件名或 policy key 逃逸出来。
  照报告原方案"BridgeError 就记 `str(exc)`"会把它们泄进日志。现在新增的插值消息默认**失败安全**
  （不记录），并有一个测试用 AST 从源码重新推导白名单，防止两边漂移。
- **绝不会让 hook 失败**：`journal_write` 自带裸 handler，不扩大 `main()` 原有的 catch。新增的
  `except BaseException` 是记完就 `raise`，所以 AssertionError/TypeError 仍照旧带 traceback 非零退出。

验证：546 tests + 3239 subtests 通过（原 522/3230，新增 24）；install_bridge 174 tests 通过；两条关键
保证都做了变异测试（绕过白名单会挂 4 个 subtest，删掉 start 记录会挂 6 个 test）；按真实 hooks.json
调用形态端到端跑过，既记下了 digest mismatch，又仍然 exit 0 / stdout 空 / stderr 空，且 prompt、cwd
和里面的假密钥一个都没进日志。实测开销约 22ms（预算 5s）。

**未安装**：已部署的 release 是另一份哈希锁定的副本，本次没动它，所以要等下一次获得授权的重装才生效。

### 5.2 复核记录 / 备份 / fork —— 属实，备份已修好

- **自动备份 `com.local.git-backup-sweep` 确实 100% 失败，已修复。** 根因就是本项目自己早已为 doctor
  plist 记录过的那条：`ProgramArguments[0]` 是 `/usr/bin/python3`（Xcode shim），在 launchd 会话里
  被 TCC 拒绝访问 `/Volumes/...`。改成 `/opt/homebrew/bin/python3` 后，`launchctl list` 的退出码从
  **2 变成 0**，日志里刷屏的 `Operation not permitted` 停止，6 个仓库重新逐个真正检查。
  原 plist 已备份为 `com.local.git-backup-sweep.plist.bak.2026-08-28`。
  （该 plist 不在任何 git 仓库里，属纯系统配置，故无对应 commit。）
- **但要澄清两点**（调研报告已指出，此处复核确认）：这个 sweep 的 `GIT_PROJECTS` 只覆盖 `hgfast`、
  `本机`、`.codex-profiles`、`.claude-r2`、`邮局`、`hgfast-anytls-deploy`，**从来就不包括 `完善orca`**；
  而且那几个仓库本来也没断备份，post-commit hook 一直在工作，sweep 只是冗余兜底。"修好备份"和
  "让 `完善orca` 不只存在一块盘上"是两件独立的事，前者已做，后者未做。
- **复核记录已纳入版本控制**（见随后的 commit）。
- **fork push 未执行**：`git push -u fork 完善orca` 会把 249 个只存在于本盘的 commit、连同 85 份复核
  文档推到 GitHub 上的 fork。这些文档里包含安全缺口分析、hook 路径、账号目录布局等内容，是否公开
  属于人工决定，不在"增量代码修复默认允许"的范围内，因此留给用户拍板。

### 5.3 cherry-pick `ee982fc0a8` —— **作废，不要执行**（本次 4 条里最要紧的结论）

独立复验了 3 个阻断事实：

1. **`完善orca` 分支根本收不了它**：`git ls-tree 完善orca` 里没有 `src/`，整个分支是纯工具树
   （`claude-codex-memory-bridge`、`orca-context-bridge`、`orca-dispatch-guard` 等）。把 4 个
   TypeScript 文件摘上去只会在一个没有构建的仓库里造出孤儿文件。
2. **摘到任何一个现存 Orca 源码分支上都是 4 个文件全冲突**：用
   `git merge-tree --write-tree --merge-base=ee982fc0a8^ <target> ee982fc0a8` 只读干跑过
   `3558cf943f`（`完善orca的桌面控制能力`，即今天的 `origin/main`）、`9635e6822f`、`a27c691fdd`，
   三个目标全部 rc=1、四个文件全部 `CONFLICT (content)`。
3. **上游已经修过同一个 bug，而且修得更彻底，且已在 `origin/main` 上**：`68d5b9206e`
   "fix(agent-prompt): stop reporting delivered prompts as stalled (#16095) (#16590)"，
   2026-08-26，`git merge-base --is-ancestor 68d5b9206e origin/main` 为真。它改了 12 个文件，
   包含 `coordinator-task-dispatch.ts`、`worker-report-settlement.ts`、`worker-dispatch-outcome.ts`
   ——正是 `ee982fc0a8` 明确声明不处理的那段 capability 吊销生命周期。符号核对：本地修复的
   `waitForBusyAgentPromptSettlement` 在 `origin/main` 上 0 命中，上游的 `explicitWorkingStartedAt`
   在 `origin/main` 上存在。两者是同一缺陷的两个独立修复。

**正确动作**：不要 cherry-pick。记录 `ee982fc0a8` 已被上游 `68d5b9206e` 取代；将来某次获得授权的
重新构建应当从 `origin/main` 系分支切出（今天 `完善orca的桌面控制能力` @ `3558cf943f` 就等于
`origin/main`），那样免费拿到更完整的修复。`ee982fc0a8` 唯一还值得做的事是把 `完善claude`
push 到 fork 保住它和它的双复核记录——同 §5.2，属人工决定。

### 5.4 reviewed pack 签名字段 —— 属实，已修掉"说谎"的部分，缺口本身仍未关闭（`e790c306e9`）

独立复验：`signed_by` 在已部署脚本里 **0 命中**，`signature_nonce` **0 命中**，
`authority_signed_at` **1 命中**且只是 `build_knowledge_graph.py` 把它抄进图谱节点当展示元数据
（标了 `reference_only`），没有任何东西以它为门禁。

这三个字段**根本不是签名**：`sign_reviewed_authority.py` 的实现就是一个时间戳、一个调用方传进来的
reviewer-id 字符串、和 `secrets.token_hex(16)`——没有密钥、没有 HMAC、没有签名值、没有任何可校验的
对象。无密钥的 nonce 什么也认证不了。

值得记一笔的是：本树的 `SKILL.md` **早就把这件事写对了**（"advisory only and are currently enforced
by nothing"，并直接称其为 "a real defense-in-depth gap in the trust anchor"）。唯一在说谎的是
`sign_reviewed_authority.py` 自己的 docstring（"contains cryptographic signatures ... allow
verification hooks to distinguish between externally signed and self-signed manifests"）和一行
"these prove external signing" 的注释，二者与同仓库的 SKILL.md 直接矛盾。已改成如实描述。

**刻意没有做的事**：没有让校验器开始读这三个字段。读一个无密钥的 nonce 收益为零，反而会制造
"这里有检查"的假象，比不读更糟。真正的修法是对 manifest 的规范化 JSON 做带密钥的 MAC、密钥存在
manifest 自己目录之外——那是一个设计任务，本次未做。**底层缺口原样保留：manifest 钉住了 pack 哈希、
shared-source 哈希和 authority git 状态，但 manifest 自己的字节是未经认证的，能写它的人可以把所有
哈希重新钉成自己刚装上去的东西。**

### 5.5 顺带发现：reviewed pack 的信任锚早就已经过期了

复查 §5.4 时实测发现，manifest 钉的是 `head = 0ce13d8b64`（2026-08-26 16:09），而本次会话开始时
HEAD 已经是 `a6401c22f8`——**中间隔了 37 个 commit**。也就是说 `verify_reviewed_pack` 的
`authority_git` 比对**在本次工作开始之前就已经失配**，SessionStart 这几天一直在 fail-closed
（发 `ORCA_CONTEXT_NACK_V1` 而不是 `ORCA_CONTEXT_DELIVERY_V1`）。

这是既有状态，不是本次改动造成的；fail-closed 也是安全方向，所以不是安全漏洞，而是"这个功能其实
一直没在生效"。**本次刻意没有重新签名**：SKILL.md §624 有完整的重签流程，但重签是影响全机的操作，
而且由 agent 自己去签正是 §5.4 所批评的那个模式（现存 `signed_by` 的值就写着
`...-agent-executed-...`）。留给用户决定。

## 4. 真实分歧（如实保留，未多数票抹平）

- **脱敏 hook 缺失机制**：Grok 说是默认 CODEX_HOME 那份完全消失，Claude 说是账号覆盖面问题——综合环节实测两处缺口**同时存在**，二者说对了不同的一半。
- **未提交 7000 行草稿的性质**：Claude 认为是"两件事被绑一起"（ReDoS 修复真实有效 + v2 刻意停放待设计），Grok 认为它根本没实现设计文档要求的架构——综合环节确认二者也是各对一半、互补。
- **install_shared.py 全树替换的严重度**：Grok 判 P1，Claude 判 P2，事实描述完全一致，分歧纯粹在"已发生过一次且保证复发"该算多严重。
- **catalog 冲突的严重度与根因**：Claude/Grok 一致判 P2、根因一致；Gemini 判 P1 且根因指错文件——已判定 Gemini 这条不可信。

---

## 附：被推翻的可疑发现（说明这次复核是认真做的，不是照单全收）
- Codex 的两条"bug"：一条是自己加载脚本方式导致的假阳性，一条实测无法复现且引用的输出与真实结果不符——均已剔除。
- Gemini 的两条"bug"：无独立证据支撑，其中一条根因指错文件——不计入确认列表。

---

*完整 4 路原始调研数据、侦察记录见 Workflow run `wf_ea39a442-bc8` 的 journal。*
