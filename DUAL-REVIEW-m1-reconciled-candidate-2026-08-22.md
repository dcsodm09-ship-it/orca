# 独立双复核报告：m1-reconciled-candidate-STAGED-review-only

> ## ⚠️ 2026-08-22 补记：本文件记录的问题已被另一并发会话真实解决并部署，本文件转为过程记录
>
> 本文件全部正文（下方）记录的是"完善orca"会话对 `m1-reconciled-candidate-STAGED-review-only` 的独立调研/复核过程，**结论本身没有错**，但在完成第三轮"修bug+完善方案"工作后发现：**另一个并发会话（走 `~/.claude/plans/sequential-baking-thunder.md` 的 M1/M2 步骤）已经用一套独立、更完整的流程（M1经5轮双复核、M2经4轮双复核）把同一批文件真实合并、真实部署到现网 `~/.agents/skills/orca-context-bridge/`，manifest 已重签、真实 SessionStart 钩子已验证通过。**
>
> 已核实的现网证据：`MAX_WORKTREE_ROWS=256` 已生效；`.m1-deploy-backup-20260822/`、`.m2-deploy-backup-20260822/` 备份目录真实存在；真实 manifest 的 `signed_by` = `dcsodm09-ship-it-agent-executed-2026-08-22-m2-wiki-guard-deploy-4round-dual-review-go`；`check_wiki_freshness.py`/`wiki_edit_guard.py` 已真实部署。
>
> **因此：本文件下方记录的"批次2不得执行""工具需要先修复这几个bug"等结论，不再是待办事项**——不是判断错了，而是问题已经被别人用更干净的路径解决了。下方内容保留作为调研过程与方法论参考（三层pin机制分析、活仓库时效性处理原则等仍有参考价值），不代表当前还需要执行的行动。详见 `m1-reconciled-candidate-STAGED-review-only/STATUS-SUPERSEDED-2026-08-22.md`。
>
> 相应地：**第三轮"修bug"workflow完成后，本应发起的第三轮 opus+Codex 双复核已取消，不再需要**，因为它要复核的东西已经不是待执行的行动项。

---

**日期**：2026-08-22
**复核对象**：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/m1-reconciled-candidate-STAGED-review-only/`（暂存、未安装、未部署的 orca-context-bridge A/B/C 三版和解候选包，核心是 `agent_capacity.py`）
**性质**：三轮独立只读复核，全程未修改/复制/安装/执行任何文件。三轮结论一致收敛为：**现在不应按原计划 promote**。

---

## 三轮复核概览

| 轮次 | 执行方 | 结论 | 关键点 |
|---|---|---|---|
| 第1轮 | Claude sonnet（8-agent Workflow） | CONDITIONAL-GO | 代码本身无P0/P1；发现 DEPLOYMENT-SOP 安全问题、`recommendation`字段与规则8不符 |
| 第2轮 | Claude opus（独立） | CONDITIONAL-GO（但门槛更严） | **发现"promote=覆盖B"这个任务框架本身是错的**；发现无重签名工具会导致全账号SessionStart降级 |
| 第3轮 | Codex gpt-5.6-sol design（独立，[强制双复核]） | **NO-GO** | 同意opus的manifest失配问题；对`gate_removed` vs 规则8的冲突给出更明确立场——倾向恢复真实红黄绿 |

三轮的**结论标签不同，但实质判断一致**：opus的"CONDITIONAL-GO"所附带的"不可接受、必须先处理"条件，和Codex的"NO-GO"在行动含义上是等价的——**都是"现在不能直接promote，要先做别的事"**。

---

## 最重要的一个发现：我给的复核任务框架本身有错

三轮里的第2轮（opus）指出：我最初交代的"promote = 把候选 `scripts/` 覆盖到 B 对应位置"这个动作定义本身是错的。

候选包是 **A（技能目录版，v2 schema）血统**，而 B（工作区版）已经是 **v3/v4**，比 A 多出约 **250KB 的后续工作**：

| 文件 | B 当前大小 → A/候选版本大小 |
|---|---|
| `sync_sessions.py` | 92,924 → 28,703 字节 |
| `build_startup_bundle.py` | 74,401 → 41,395 字节 |
| `startup_context.py` | 56,482 → 34,916 字节 |
| `build_context_digest.py` | 58,094 → 32,869 字节 |
| `build_knowledge_graph.py` | 47,904 → 18,351 字节 |
| `auto_index.py` | 23,206 → 8,623 字节 |
| `install_startup_injection.py` | 44,964 → 37,001 字节 |
| `refresh_graphify_catalog.py` | 15,969 → 10,023 字节 |

如果照"覆盖B"执行，会**销毁这约250KB的v3/v4工作**，而且 BUILD-NOTES.md 原文明令禁止"整目录覆盖"（"Deployment must be file-by-file against this BUILD-NOTES.md provenance list, never a wholesale directory copy in either direction"）。

对A而言，候选包**真正有意义的增量只有4个文件**：`agent_capacity.py`、`tmux_bridge.py`、`index_processes.py`、`SKILL.md`。其余13个脚本文件对A是空操作（byte-identical 或 A本身更旧无需回填）。

---

## 两个必须先处理的阻断项（两轮独立复核一致认定）

### 阻断项 A：`agent_capacity.py` 没有安全的落地路径

这是唯一真正需要进入 B 的文件（因为 CLAUDE.md 规则8实际调用的是B那份）。但：

- B 的 `agent_capacity.py` 当前 **tracked 且 clean**，签名 manifest 记录的 `status_sha256` 与现状完全匹配。
- 覆盖后必然产生 `M orca-context-bridge/scripts/agent_capacity.py`，导致 `status_sha256` 从 `3a3d6c20cc4e64ea50a764080287f180b9cfd2e21e88938b4a5b839b8129177f` 变为 `297ec3820b541396124ff4e504e83799be3f48ed5e0f15b411c0c86811f05825`（opus 实测），使已签名 manifest 失配。
- 候选包内唯一的"签名器" `sign_reviewed_authority.py` **只能读、不能写**，且输出的 `schema_version: 3` 会被现行部署中的 v2 校验器直接拒绝。
- **后果**：promote 后全账号每个 Claude/Codex 会话的 SessionStart 都会退化——就是这次对话开头你看到的那种 `ORCA_CONTEXT_NACK_V1` 降级状态。恢复手段目前只有"手工编辑一个 0600 JSON + 手算 SHA-256"，没有经测试的工具。

**结论**：在准备出可复现、已演练的 v2 重签步骤之前，不能把 `agent_capacity.py` 落到 B。

### 阻断项 B：`recommendation` 字段 vs CLAUDE.md 规则8 —— 真实的政策冲突，两轮意见略有分歧

- 候选/B 的运行时顶层 `recommendation` 字段被硬编码为 `gate_removed`（`new_workers_default:2, new_workers_max:3`），无论真实负载如何都是这个结果；真实的红/黄/绿判断只在 `advisory_true_recommendation` 里。
- 但 **CLAUDE.md 规则8的字面文字**（"红灯不启动，黄灯最多一个，绿灯默认两个"）描述的是主动限流行为——这条规则今天还在你的全局配置里生效，而脚本已经不再照做。
- **opus 的立场**：这是一个需要用户拍板的政策分歧，不替你选边。
- **Codex 的立场更明确**：A 的 mtime（2026-08-21 22:02:32）只比疑似对应的规则修订晚24秒，"mtime新=过时"这个假设站不住脚；主张应该让顶层 `recommendation` 恢复反映真实红/黄/绿，使脚本行为与规则8的现行文字一致。
- **两轮都实测确认**：用143个真实worktree、9个working agent的当前真实负载代入，`advisory_true_recommendation` 判定是 **red**，但顶层 `recommendation` 报的是"可以起2个默认/最多3个"——如果规则8的字面意思仍然有效，这个字段现在给出的是错误的放行信号。

**这个问题不是本次候选包引入的，是既存问题**——但两轮独立复核都把它列为"必须由用户决定，不能我自己拍板"的事项。

---

## 次要问题（P2/P3，两轮均确认属实）

1. **DEPLOYMENT-SOP.md 不能作为可执行手册直接使用**：无任何"仅供历史参考"声明、开篇写着"procedure for deploying to production"；回滚命令日期格式bug（备份文件名含时分秒，回滚查找只用年月日，大概率对不上）；`pkill -f "startup_context.py"` 无账号/会话范围限定；"atomic operation"标题下没有任何原子替换机制；验证顺序倒置（先改生产settings.json后测试）；签核门槛自相矛盾（正文要求≥2名reviewer，签核模板复选框只写≥1名）；**opus 额外发现**：文档里的 settings.json 示例结构在真机上根本不存在（顶层没有`skills`键）。
2. **那句被写进代码注释的bug定性是错的**：候选包注释说128上限的bug"把真正的green错误降级成advisory yellow"，但opus实跑两个版本证伪——真实情况是**red被bug掩盖成了yellow**，比文档描述的更严重，且这句错误定性如果promote会被永久固化进代码。
3. **`test_integration_real_repos.py` 证据缺失**：COMPLETION_REPORT.md 声称交付的"406行/4个集成测试/4/4 passed"在A/B/候选包/整个`完善orca`里都不存在，无法核实。Codex 指出确实存在一个真实的 `test_verify_reviewed_pack.py`（404行/3个真实测试），所以并非完全没有集成测试覆盖，但被声称的那个具体文件是空口白牙。
4. **误诊断问题仍会复发**：超过`MAX_WORKTREE_ROWS`时代码把"数量超限"和"真正的schema违规"混在一起返回同一个错误串`invalid_worktree_schema`，128→256只是买了时间（今天143，还有113行余量），跨过256时会一模一样地静默复发，且难以自诊断。

---

## 全程只读声明

三轮复核均只执行了 `diff`/`cmp`/`shasum`/`grep`/`ast.parse`/`py_compile`，以及对 `agent_capacity.py`（自述契约明确"never starts/stops/signals an agent"）和 `tmux_bridge.py --help` 的只读试跑。**未修改、复制、安装任何文件；未执行 DEPLOYMENT-SOP.md 中的任何命令；未做任何 promote 动作。**

---

## 需要用户决定的事项（汇总）

1. 是否接受"候选包实际只对A有4个文件的增量"这个纠正后的范围认知，还是希望重新设计更大范围的B→A同步。
2. 是否要先构建、测试一套可靠的 v2 manifest 重签名工具，再考虑把 `agent_capacity.py` 落到 B。
3. `recommendation` 字段该不该恢复真实红/黄/绿以匹配CLAUDE.md规则8的现行文字，还是反过来修改规则8的措辞去匹配"gate_removed"的现状（这是既存问题，不因这次候选包而起）。
4. DEPLOYMENT-SOP.md 是否需要补一句免责声明，还是维持"C原样保留、字节不动"的既定政策。
5. `test_integration_real_repos.py` 的下落——找回，还是从COMPLETION_REPORT撤下这条证据主张。

本报告本身不构成任何决定或执行，仅供参考。

---

# 追加：第二轮工作产出（同日，回应用户 5 项决定）

用户就上述 5 个决定项给出明确答复后，本会话追加完成以下工作（均为新建文件/纯文档改动，未修改任何已有真实文件）：

## 一、低风险直接执行项

1. **CLAUDE.md 规则8措辞已更新**——原"红灯不启动，黄灯最多一个，绿灯默认两个"改为描述现在真实的 advisory-only 行为，注明改动依据。
2. **新增 `m1-reconciled-candidate-STAGED-review-only/ERRATA-AND-DISCLAIMER-2026-08-22.md`**——不改动任何原始暂存文件字节，单独追加：给 DEPLOYMENT-SOP.md 补免责声明说明；确认 `test_integration_real_repos.py` 不是丢失，而是 2026-08-12 提交 `b88eae7957` 主动删除并替换为更严谨的 `test_verify_reviewed_pack.py`（旧测试只检查git状态变化、未真正调用被测函数）。

## 二、更大范围 B→A 同步方案（一场新 Workflow，10 agent，全程只读）

### 关键新发现（三轮之前的复核均未覆盖）

1. **第三层pin机制**：`~/.claude/settings.json` 的 SessionStart hook 命令本身用 `--expected-generator-sha256` 硬编码锁死了 A 的 `startup_context.py` 内容——独立于manifest文件、独立于git检查，是写死在配置里的字符串比对。**实测确认这个pin不止一处，而是 3 处独立文件**：`~/.claude/settings.json` + 两个 Codex 账号（`b9f32a51`、`0b4cd443`）各自的 `hooks.json`，三处必须同步更新，漏一处该账号所有会话立刻 NACK。另有一层独立的 Codex `trusted_hash` 信任缓存（会触发交互式重新信任，非阻断性）。
2. **B 是全账号唯一的、正在被并发编辑的活仓库**——`--knowledge-root` 硬编码指向"完善orca"这个worktree，所有Claude Code会话（不管在哪个项目）共用同一个 knowledge_root。复核过程中实测同一个 `head` 值在15分钟内变了两次（其他并发session的无关提交），证明任何"重签名"操作必须现场重新计算快照，不能复用任何早期报告里的哈希值。
3. **`startup_context.py`/`build_startup_bundle.py` 这一对在B里根本不是"待协调发布"的候选**，而是一次未完成的 schema v2→v3 重写半成品，与A当前生效的v2 schema完全不兼容，且没有配套的、能产出合规manifest的signer。**这是比SHA-pin协调更底层的阻断**，此前三轮复核都未发现。
4. **两个此前从未被发现的"隐藏依赖死文件"风险**：`context_route.py`（B新增）import了A当前`startup_context.py`里根本不存在的符号，一旦被复制到A会立即 `ImportError`；`detect_orca_automation_prompt.py`（A独有）是逐次prompt都触发的钩子，用 `install_shared.py` 整目录复制会把它静默删除且**没有任何报错信号**——这是本轮发现的风险最高的单一动作。
5. **`orca_readonly_probe.py`/`orca_lifecycle_precondition_probe.py` 两个文件上 A 反而领先于 B**（避免Linux裸`orca`误触发GNOME屏幕阅读器的修复），如果照"B更新就该覆盖A"的直觉整批复制，会导致明确的功能回退。
6. `agent_capacity.py`/`SKILL.md` 在A/B工作区内容其实已经逐字节相同——缺的不是"移植代码"，而是"把B的git历史补上一个commit，让签名快照追上A已经在跑的现实"，比原以为的更简单。

### 产出的最终方案（结论）

**只有 1 个文件（`resume_session_loader_py39.py`）当前可以立即安全复制。** 其余分三批：
- **批次2（agent_capacity.py + SKILL.md）**：有明确、可在短时间窗口内完成的操作序列（commit → 现场重算git快照 → 生成临时manifest → 用真实`verify_reviewed_pack()`验证 → 原子替换 → 全新会话验收 → 任何失败原路回滚），但对"B是活仓库"的时间敏感性要求很高。
- **批次3（`startup_context.py`+`build_startup_bundle.py`及其4个耦合文件）**：**当前不具备执行条件**，方案明确列出4项前置阻断条件（内部自洽性验证、经审查的schema v3 signer、独立双复核、确认不破坏批次2成果），建议作为独立工程立项，不并入本次同步窗口。
- **6个大幅增长的文件**（`sync_sessions.py`等）：需要逐文件人工审计A侧独有改动方向后才能归批，方案给出了审计方法，不代为下结论。
- **明确排除**：`install_shared.py --update`（整目录复制）在方案中被列为"不得执行"的操作，因为会连带删除A独有的安全文件、回退A领先于B的两处修复。

### 新建并已测试的工具（均为暂存目录里的新文件，未改动任何已有文件）

1. **`m1-reconciled-candidate-STAGED-review-only/scripts/resign_authority_git.py`**——真正可用的 authority_git 重签名工具，import真实的 `summarize_git`/`verify_reviewed_pack` 函数（不重新实现哈希逻辑），默认dry-run、`--apply`前自动备份、写入后自动用真实校验函数复核、失败自动回滚。**已在完全隔离的临时沙箱git仓库里做了5个场景+2项安全检查的实测**，全部通过，包括验证"重签名后再制造新漂移仍会被正确捕获"（证明没有过度放宽校验）。
2. **`m1-reconciled-candidate-STAGED-review-only/scripts/compute_generator_pin_update.py`**——只读计算/校验辅助工具，给定候选新文件算出SHA-256，和settings.json里所有pin值比对，打印需要同步执行的操作清单，但**不自动写入settings.json或任何生产文件**。已测试SHA一致/不一致两种场景+确认全程无写入。

### 全程只读声明

本轮全部工作只新建了3个文件（1份勘误说明 + 2个工具脚本），均在暂存目录 `m1-reconciled-candidate-STAGED-review-only/` 内；唯一修改的既有文件是用户自己的 `~/.claude/CLAUDE.md`（用户本人明确选择的决定）。未执行 `install_shared.py`/`sign_reviewed_authority.py` 的任何写入分支，未修改A、B、`~/.claude/settings.json`、任何 Codex 账号的 `hooks.json`、`.orca/context/` 下任何真实文件。

**这批新产出（同步方案 + 两个新工具）尚未经过强制双复核，按本项目既定流程即将补做 Claude opus+max / Codex sol+max 独立只读复核，复核通过前不得据此执行任何真实写操作。**

---

# 追加：第二轮工作的独立双复核结果（opus + Codex，均已完成）

两路复核结论高度一致，均判定：**工具本身 CONDITIONAL-GO（需修复具体bug）；方案作为架构指导 CONDITIONAL-GO；方案作为"今天就能照抄执行的runbook" NO-GO；批次2现在执行 = NO-GO，存在真实、当前正在发生的阻断，不只是"活仓库时效性敏感"这么轻描淡写。**

## 两路各自独立发现、且互相印证的关键事实

**批次2现在就会失败，原因是两个独立且都是"当前正在发生"的漂移（两路各自实测到、数值对得上）：**

1. **wiki 内容漂移**——对真实 B 跑一次真实的 `verify_reviewed_pack()`，两路都实测到当场失败：`central reviewed source freshness mismatch: wiki`（manifest 里pin的wiki哈希 `6e27d990...`，当前实际是 `b6b60e74...`，20分钟前被别的并发会话改过）。**`resign_authority_git.py` 只修 `authority_git` 一个维度，设计上不处理这类内容漂移**——现在跑批次2，apply会写入、post-write真实校验会失败、自动回滚，白跑一趟。
2. **B 的 `SKILL.md` 本身还在被并发修改**——Codex 复核期间实测其SHA在复核过程中又变了一次（`413aa026...`→`3dc7c545...`），且新增内容引用了 `wiki_edit_guard.py`/`check_wiki_freshness.py` 这两个至今仍未提交（untracked）的脚本——如果现在就 `commit agent_capacity.py + SKILL.md`，会提交一份指向"历史里根本不存在的文件"的操作文档。

## 工具的具体bug（两路各自发现，部分重合、部分互补）

**`resign_authority_git.py`：**
- **P1（两路都发现）**：git不可用/非git仓库时，工具会签一个 `{available: false, head: null, status_sha256: null}` 的"空绑定"authority，且自己的post-write校验居然通过、报告成功——**静默关闭了git新鲜度检查还宣称成功**。修复方向：apply前显式拒绝 `available is not True`。
- **P2（opus）**：dry-run只diff `authority_git`字段，不跑完整`verify_reviewed_pack()`，所以在今天这种wiki已经漂移的真实情况下，dry-run会显示"看起来没问题"，但apply后其实会失败回滚——正好撞上今天的真实状态，属于会误导操作者的设计缺陷。
- **P2（Codex）**：没有expected-HEAD/manifest preimage的CAS锁机制——活仓库里如果在"读取HEAD"和"落盘签名"之间又有新提交落地，工具会直接签署那个新HEAD，而不是确认它就是批次2审核过的那次commit；显式`--manifest-path`指向symlink时，会先把内容写进解析后的外部目标、之后verifier才拒绝回滚，存在一个写入-校验之间的崩溃窗口；写入操作本身在post-verify的异常保护范围之外（`os.replace`之后的`chmod`阶段抛错不会触发自动回滚）；备份无留存策略、无fsync（原子改名不等于断电耐久）；dry-run无论有无漂移都返回exit 0，不能作为自动化gate的判据。
- **Codex额外指出一个概念性问题**：文档说的"临时manifest先用真实verifier验证、再原子替换"这个设计原则，和工具实际实现不符——真实 `verify_reviewed_pack()` 并不支持传入一个"备选路径"的manifest，工具实际是先替换掉正式路径的manifest、再验证，不是"验证完再替换"。

**`compute_generator_pin_update.py`：**
- 两路一致：确认真正只读、零写入路径。但**默认只扫 `~/.claude/settings.json`**，两个Codex账号的`hooks.json`（同样带着这条pin）完全没扫到——真实机器上有3处pin，工具默认只找到1处，"无需变更"这个结论在当前拓扑下是不完整、会误导人的。

**新发现的第三个Codex账号（opus单独发现）**：`8d7db875-541c-4499-b219-64ec9b7486d6` 这个账号，活的 `hooks.json` 里**完全没有** orca-context-bridge 相关hook（这个账号本来就已经拿不到Orca context了），但它的 `.bak` 备份文件里还留着旧pin——如果哪天有人图省事"恢复备份"，会复活一个过期的pin，是个潜伏地雷。

## 关于同步方案本身，两路都认为方向正确、但需要具体修正

- **认可**：禁止 `install_shared.py --update` 整目录覆盖是对的；不拿B覆盖A的两个probe文件是对的（A确实领先）；批次3当前阻断的判断是对的；6个大幅增长文件需要逐文件人工审计是对的。
- **需要修正**：批次3的真实闭包比方案描述的更大——Codex指出B自己的 `GENERATOR_DEPENDENCIES` 要求完整10文件集合，其中8个当前与A不同或缺失，不只是"startup/build加4个耦合文件"这么简单。
- **需要修正**：批次1里`resume_session_loader_py39.py`单独立项复制其实没有实际功能收益（Codex确认：A的`sync_sessions.py`根本不引用这个文件），复制了也是个孤立文件，应该和真正采用它的`sync_sessions.py`一起审查，不必单独作为"立即可执行"的一批。
- **需要修正**：批次2的commit步骤必须明确写成"路径限定"（`git add orca-context-bridge/scripts/agent_capacity.py orca-context-bridge/SKILL.md`），而不是`git add -A`/`git commit -a`——opus指出B的`orca-context-bridge/`目录下现在约有20个未提交文件（那个没写完的schema v3半成品），一次不慎的全量commit会把整个未审查的重写工程一起提交进历史。

## 最终结论

**在以下三点解决之前，批次2不得执行（两路一致，非"建议"级别，是"当前必然失败/风险不可接受"级别）：**

1. wiki 内容漂移需要独立处理（真实内容复核，不是机械重签），不能靠 `resign_authority_git.py` 顺手带过。
2. `resign_authority_git.py` 的"git不可用时静默签空绑定"这个bug必须先修（这是唯一一个"工具报告成功但实际上关掉了一层完整性检查"的路径）。
3. commit 步骤必须显式路径限定，避免把B目录下~20个未审查的schema v3半成品文件一起卷入。

**本轮全程只读，opus和Codex均未修改任何真实文件（含A、B、settings.json、三个Codex账号的hooks.json）；两路各自独立复现了当前活仓库的真实漂移状态，互相印证，不是各说各话。**

这是本项目对同一批候选做的第二轮独立双复核（第一轮判定原始 m1-reconciled-candidate 不宜直接promote，本轮判定后续的同步方案+新工具同样不宜现在就用于生产）。
