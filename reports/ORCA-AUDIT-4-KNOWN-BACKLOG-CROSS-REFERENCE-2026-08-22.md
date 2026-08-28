# 审计 4：已知台账全景交叉核对

# Orca 完善orca 项目 — 已知未完成/被阻断工作全景图

来源：`reports/COMPLETION-BACKLOG-2026-08-16.md`（全文541行，本项目的主台账，逐节读完）+ `reports/*.md` 中其余35个报告文件（重点是2026-08-19/20的、晚于台账快照日期的报告）+ 会话可见的 MEMORY.md 条目交叉核对 + 当前 `git status`（4个文件已修改未提交，172条未跟踪文件，与下文条目对应）。

---

## Part A — COMPLETION-BACKLOG-2026-08-16.md 逐节状态（按原文记录，阻断类型图例见文件第8-16行：BLOCKED_ROOT_AUTHORITY / BLOCKED_DUAL_REVIEW / BLOCKED_CAPACITY / BLOCKED_APPLE_ENTITLEMENT / BLOCKED_HUMAN_AUTH / BLOCKED_HUMAN_DECISION / SAFE_NOW / DONE）

### 第1节 · 根源阻断（先于一切"合并/安装"结论），4项，全部 BLOCKED_HUMAN_DECISION 或 BLOCKED_ROOT_AUTHORITY
1. `projects/orca@main` 身份（真实历史 vs 非VCS证据库）— **BLOCKED_HUMAN_DECISION**，已提出等待答复
2. 内部盘 rollback-backup 退役政策 — 文中列为 **BLOCKED_HUMAN_DECISION**（注：与第0节记录矛盾——第0节称三选一已获用户批准并已执行完毕`DONE`，17项已迁移到SSD并删源；第1节表格本身未同步更新，如实记录这处内部不一致）
3. 已装 Orca 1.4.182 与本地任意 checkout 缺少可证明同源性 — **BLOCKED_HUMAN_DECISION** + 需fetch/对齐origin；明确禁止靠重建/替换Orca.app"修复"
4. 知识图谱-Workflow-完善 分支去留 — **BLOCKED_HUMAN_DECISION**，分支已落后origin/main 183个commit

### 第2节 · 启动上下文/Hook注入（16项）
- startup-installer-closure/independent-acceptance — **BLOCKED_HUMAN_AUTH**（4个真实目标2/4文件、4/4父目录权限不达标）
- startup-permission-remediation-closure — v1-v3差异已查清`DONE`，真实写入仍**BLOCKED_HUMAN_AUTH**；**新发现待办**：`codex-account-01`当前真实权限状态未知（8/11曾从0600倒退到0644），需一次独立授权的只读核查，未做
- startup-loader-closure — **BLOCKED_DUAL_REVIEW**（需针对已修补字节的全新独立安全复审，非自测）
- startup-p1-hardlink-closure↔startup-p1-offline-acceptance — 文档修复`DONE`，正式验收**BLOCKED_DUAL_REVIEW**（独立复核仍未真正发生）
- startup-reviewed-pack-closure/independent-acceptance — **BLOCKED_HUMAN_AUTH**（v3已GO_OFFLINE_CANDIDATE_ONLY，需root显式授权原子提交进`.orca/context`）
- startup-reviewed-pack-schema3-fastfix (v4-v7+activation layer) — **BLOCKED_DUAL_REVIEW**（v7已过期，需先出新版本再复核）
- startup-transaction-closure/v2-independent-acceptance — **BLOCKED_DUAL_REVIEW**（4天未被真正执行过）
- startup-live-integration-v4 — **BLOCKED_ROOT_AUTHORITY + BLOCKED_HUMAN_DECISION(ACK)**（NO_GO_CONTEXT_NACK本质是第1节两个根源问题的下游症状）
- reports/startup-context-*-candidate×2、W0-W10-SOURCE-BOUNDARY-MANIFEST、W10-ACCOUNT-BINDING-STATIC-REVIEW — **BLOCKED_CAPACITY**（需绿灯重跑Vitest/typecheck/lint）

### 第3节 · SSD原生存储与PTY沙箱（8项）
全部 **BLOCKED_APPLE_ENTITLEMENT**（需EndpointSecurity/system-extension entitlement+已签名打包App，超出本会话能力）；`ssd-archive-cleanup-v5`额外 **BLOCKED_HUMAN_AUTH**。本轮不可推进，只能保持候选就绪。

### 第4节 · R2备份恢复/归档/Android退役（9项）
- **codex-restore-tool**（恢复CLI）— **DUAL_GO_REVIEW_COMPLETE**：经9轮修复→双复核循环（每轮都真实发现并修复P0/P1，多次两路独立收敛到同一根因），第9轮 Codex gpt-5.6-sol/xhigh 与 Claude opus5/max 双GO，无P0/P1；但**是否合并/安装/发布仍需用户另行明确授权，未执行任何上线动作**
- r2-run-identity-closure — **SAFE_NOW**，本轮已修复
- archive-provenance-normalization-closure — **SAFE_NOW**，本轮已修复
- r2-production-trust-closure — **BLOCKED_HUMAN_AUTH**（需真实签名方/公钥与pin的生产policy SHA）
- android-retained-retirement-closure — **BLOCKED_HUMAN_AUTH**+依赖上一条
- internal-projects-cold-preflight — **BLOCKED_HUMAN_DECISION**（卷条件门禁失败）
- reports/android-ssd-switch — **DONE**
- reports/cleanup-internal-green-wave2-v4-candidate — **BLOCKED_HUMAN_AUTH**（需授权对14个真实Trash源执行归档）

### 第5节 · Paperclip第三方集成（3项）
- paperclip-privacy-gate-closure — **DONE**（已提交commit `b3009f043d`）
- paperclip-orca-closure/independent-acceptance — **BLOCKED_HUMAN_DECISION**（"受信任broker"机制未设计，64种组合里32种报`TRUSTED_BROKER_NOT_IMPLEMENTED`）
- paperclip-skill-candidate — **BLOCKED_HUMAN_DECISION**（是否/如何并入6份参考文档）

### 第6节 · 记忆/知识图谱/协作（5项+多条子调查）
- 知识图谱-Workflow-完善（独立worktree）— **BLOCKED_HUMAN_DECISION**
- **install_bridge.py**（事务性安装器）— 文档记为 **DONE**：2026-08-19获用户授权后实际安装（install_id `20260819T084541.383715Z`，release `bb8f8eb2fd`），14轮双复核周期收敛，opus5/max与Codex terra/high都GO零P0/P1。*但见下方Part B——`install-bridge-install-recommendation-2026-08-20.md`纠正了这个记录：写触发器相关增量的**最后一次正式复核结论其实是NO-GO**，是被用户明确决定停止迭代而收尾的，不是靠后续复核转正的GO；round 15-19的Codex侧复核报告在磁盘上根本不存在，只有文字声称。*
- **claude-codex-memory-bridge**（读侧记忆桥，`claude_memory_hook.py`）— **DUAL_GO_REVIEW_COMPLETE**（第4轮双GO，commit `4dfe5619c6`，无P0/P1，4轮收敛）；台账原文明确写"是否安装/激活仍需用户另行明确授权，本记录不代表已执行任何安装/激活动作"（*该桥接脚本本体后续经历了18轮review、多个真实P0安全漏洞被发现修复——见6.x新发现——最终于2026-08-20随install_bridge一起真正安装，见Part B*）
- codex-claude-memory-bridge（反向桥接，Codex原生记忆→Claude方向）— **SAFE_NOW（可立项，未开始）**：仅有调研笔记，尚未有任何代码；已确认Codex侧记忆是全局单一存储（无工作区分区），需要与正向桥接完全不同的设计（复用`memory_summary.md`+相关性过滤而非访问控制），立项后同样需要双复核
- sol-memory-strengthening-audit — **BLOCKED_HUMAN_DECISION**（4个P0需先定设计方向，source/wheel/installed三方身份需先统一）
- workflow-learning-l0（2条记录）— **SAFE_NOW（观察态）**：无法人为制造第二次独立复现，只能等待自然复现
- task_2cfbe4265261-safety-lane-report — **BLOCKED_HUMAN_AUTH**（迁移需人工/管家给出确切safety清单）
- **holaOS记忆架构对照的4条可迁移建议**：① daemon须可从git commit确定性重建 — **BLOCKED_HUMAN_DECISION**；② `context_pack()`升级为规划/检索/合并重排架构 — **BLOCKED_DUAL_REVIEW**（架构级，触碰热路径，需先立项）；③ `kind`字段 — 仓库校验层`DONE`，但发现这与一个**已知P0同根**：正在服务真实会话的活跃memory daemon哈希与任何源码分支都不一致、不在任何git分支上，仍是同一个`BLOCKED_HUMAN_DECISION`；④ 命名空间路径allowlist — `DONE`（已随桥接candidate一起送审）
- **`ORCA_CONTEXT_NACK_V1`（wiki新鲜度不匹配）**根因已查清（wiki内容手工改动后从未重新钉哈希）— **BLOCKED_HUMAN_AUTH**（wiki内容需人工复核）+ **BLOCKED_DUAL_REVIEW**（重新钉哈希的manifest候选需双复核）；*Part B确认截至2026-08-20仍未修复*
- 已安装的hook脚本仍停留在`REVIEWED_PACK_MANIFEST_SCHEMA_VERSION=2`，仓库已到v3/v4（含routes）— 部署滞后，**SAFE_NOW但本轮未执行**（不确定是否有其他会话正在用，未擅自替换）
- orca-context-bridge当前sessions.json/acks/handoffs机制被诊断为经典Blackboard架构弱点（无单写者约束、无持久事件历史）——5条按优先级排序的改进建议**均未被机制强制执行**，包括：①事件日志schema已设计但从未接入运行时；②"恰好一次"发布+心跳TTL自动回收未实现（直接对应实际发生过的"worker静默卡死超6小时被误判仍在跑"事故）；③**`agent_capacity.py`把`gate`字段语义从红黄绿静默改成永远`"gate_removed"`，真实红黄绿挪到新字段`advisory_true_recommendation`，任何还按旧语义读`gate`的调用方（包括用户自己CLAUDE.md里那条容量门禁规则）都会被静默误导**——这是一个值得特别提请上级注意的发现；④完成回执缺乏"反向对照"强制要求；⑤未审计每个状态变更入口

### 第7节 · Prime Agent集成（`install_prime_agent.py`）
从最初Codex发现4个P1开始，经历**52轮**fix→verify→双复核循环（比其余任何候选都长），追出并关闭了一条持续7轮的RCE向量、多轮供应链完整性收敛问题（tarball/工具链摘要钉住、TOCTOU窗口、符号链接绕过等）。**round 52**是这条线索第一次真正双GO（opus/max与Codex都0 P0/P1）。随后上游npm锁定哈希已用更严谨证据链重新钉好（commit `bafce01bf5`），`sandbox_e2e.py`第一次真实跑通`ok:true`。**round 53双复核已派发，结果在本文档记录范围内尚未回执** — 当前状态实质是 **BLOCKED_DUAL_REVIEW（round 53待决）**，通过后仍需用户另行授权`install`/`enable`（第二重独立阻断）。

### 第8节 · 桌面/浏览器（2项）
`desktop-mcp`、`ego-capability-fixture` — 均 **BLOCKED_HUMAN_DECISION**（peer认证机制未设计 / ship-or-discard未决策）

### 第9节 · 兄弟worktree未提交工作（6+23项）
- ego浏览器/深度学习编排/codex-track1/codex-track4 — **BLOCKED_HUMAN_DECISION**（占用问题已用lsof排除，剩下是"该不该代为提交数百行未逐行评审的功能（尤其ego浏览器涉及浏览器会话安全）"这一产品/信任判断）
- 优化本机code — **BLOCKED_HUMAN_DECISION**（需先确认是否与W10-ACCOUNT-BINDING-STATIC-REVIEW同一候选+清理无关scratch残留）
- 智能体安装 — **DONE**

---

## Part B — 台账日期（2026-08-16）之后、reports/*.md 中新增的独立未完成工作（台账本身未覆盖，多数标注 NOT activated / pending / staged-not-committed 一类语言）

1. **`STATUSLINE-SESSION-ID-2026-08-20.md`** — Claude Code状态栏@提及标识：`id:xxxxxxxx`短标识已实现并对所有会话生效（`DONE`部分）；但**真正可`@`的可预测短名字被明确调研确认技术上不可行**——没有任何hook/settings.json字段/环境变量能在会话启动时设置名字，唯一路径是Orca启动时传`-n`参数，这是Orca.app自身编译产物，落在用户"不得重建/替换Orca"规则边界内，未触碰。真正解决需要(a)用户/Orca维护方给启动逻辑加`-n`，或(b)向Anthropic提交功能请求——两条都不在本次可落地范围，**长期悬而未决**。

2. **`install-bridge-install-recommendation-2026-08-20.md`**（Opus独立综合分析）— §4.1明确指出：`install_bridge.py`磁盘上**最新的正式复核结论其实是NO-GO**（round 15/17），是被用户决定停止迭代收尾、非复核转GO；§4.2指出round 15-19的Codex侧复核文档在磁盘上不存在，只是文字声称，"unverifiable verdict deserves to be named"；§4.7明确记录：**用户此前已被建议轮换已泄露的凭据，这项轮换截至该报告撰写时仍未完成，比这次install本身更紧迫**——这是一条独立、真实、可能仍未处理的安全待办。

3. **`write-trigger-status-2026-08-19.md`**（更新至2026-08-20，与MEMORY.md条目`project_write_trigger_auto_learn_in_progress`吻合并提供大量细节）— write-trigger自动学习能力：代码完成，298/298测试，两个真正独立待决决策（用户明确要求"分开不要捆绑"）：
   - **决策1**：是否运行`install-write-trigger`真正激活写触发器——除代码就绪外，design §3.1标准要求的"130个真实候选人工拒绝率≤10%"判断**至今没有做**，只有工具无法替代的用户人工判断可以产出这个数字；且dogfood扫描发现两个系统性假阳性模式（T1对`Correction:`标签86%命中来自非人类来源；T3对通用skill样板句77%误判为用户专属事实）
   - **决策2**：是否把`claude_memory_hook.py`的diskutil超时修复（2s→按调用方4s/15s分离）重新部署到**已经在生产环境实际运行**的install——当前活跃安装仍带着过紧的2s超时，意味着真实并发负载下UserPromptSubmit hook**此刻**可能正在静默间歇性失败、注入不了记忆上下文
   - 当前`git status`已验证：`claude_memory_hook.py`/`install_bridge.py`及其测试共4个文件确实处于已修改未提交状态，与本报告描述吻合
   - 期间意外发现并已修复一个真实、活跃泄露的安全缺口（`redact()`函数CJK邻接`\b`边界bug导致邮箱及疑似真实服务凭据原样泄漏），round 18收尾GO；泄露的凭据已提醒用户轮换（呼应上一条）

4. **`MEMORY-GRAPH-TIMESTAMP-WIKI-DESIGN-2026-08-20.md`**（"终稿"）— wiki/知识图谱时间戳与新鲜度机制的完整设计已定稿（基于4份Audit报告+1轮对抗性复核），但**代码零实现**，明确要求落地前须过`BLOCKED_DUAL_REVIEW`（触碰`verify_reviewed_pack`热路径）。附录A额外发现4个新的正确性风险（风险9-12：账号池符号链接导致`record_on_ssd`误判、跨source重复索引同一物理会话且按账号错误归属统计、目录发现静默跳过无痕迹、比较运算符裸字符串前缀假阳性风险）——这些必须在未来实现阶段一并处理，目前只是文档记录。

5. **`MEMORY-GRAPH-WIKI-AUDIT-4-COMPLETENESS-GAPS-2026-08-20.md`** — 直接对应用户任务里问的"所有索引和功能特性"缺口：**约35个实质性子系统里只有2个在wiki里有任何页面**（且其中一个仅覆盖18个脚本中的4个）；列出13个按重要性排序、**全部尚未创建**的候选wiki页面（含claude-codex-memory-bridge、prime-agent-integration、orca-context-bridge整条流水线、SSD加固家族、R2备份家族、启动安装器家族、Paperclip家族、`review-orca-workflow-learning`元技能、双复核验收协议本身等）；flat pages+links schema被诊断为架构上撑不住（无分组/命名空间、无round历史表示、无"已安装"vs"已复核候选未安装"结构化状态字段、未链接到COMPLETION-BACKLOG主索引）；**并且重新确认了`ORCA_CONTEXT_NACK_V1`的wiki哈希不匹配截至2026-08-20仍未修复**（pinned `ff93b6d7...` vs live `538fd671...`，与第6节诊断一致，4天过去仍未处理）。

6. **`DOWNLOADS-DOCUMENTS-SSD-*` 系列（2026-08-20，4份文档）** — 一个全新的、尚未执行的迁移项目：
   - `~/Downloads`当前是指向一个**缺失的**`dh-work.sparseimage`的悬空符号链接（2026-07-18创建），全盘/日志/Trash搜索均未找到该镜像文件下落，其内容"事实上不可寻回"——这是**早于本次任务、独立存在的既成数据丢失风险**，设计建议不要仓促尝试恢复（有进一步破坏残留证据的风险），应作为独立取证任务由用户拍板（授权点0）
   - `~/Documents`（196MB，含2个疑似GCP服务账号密钥的JSON文件名）设计了符号链接迁移方案，复用已验证过的Android迁移脚本机制
   - 详细设计+对抗性复核均已完成，**复核发现4个具体缺口**：①声称"任何中断都自动回滚"不实——rename与建符号链接之间有一个进程被kill/断电会跳过自动回滚的窗口，且两条既有恢复路径（`switch --execute`和`rollback`）在这个精确窗口都失效；②唯一持续运行的哨兵LaunchAgent只检测"解析失败"，对设计自己点名的最大风险R4（macOS静默把符号链接换成一个新的真实空目录）**结构性失明**（该目录能正常resolve）；③Downloads现有的破损符号链接处置只在文中散文承诺，没有被纳入审计过的事务化步骤（Phase A0缺失）；④一处"亲眼验证过"的Finder行为断言未经Finder GUI实际验证
   - **状态：纯设计+批判阶段，零执行动作**，落地前需要设计§5列出的多个显式用户授权点

---

## Part C — 与会话可见 MEMORY.md 条目的交叉核对

| MEMORY.md条目 | 本仓库reports/*.md印证情况 |
|---|---|
| `project_write_trigger_auto_learn_in_progress`（NOT activated；2个待决：run install-write-trigger、redeploy timeout fix） | **完全印证**，见Part B.3；且当前git status（4个已修改未提交文件）与之吻合 |
| `project_redaction_fix_installed_2026_08_20`（已装，release 1fa909cd...，需交互式re-trust hooks，凭据仍需轮换） | **完全印证**，见Part B.2/B.3；"凭据仍需轮换"被install-bridge-install-recommendation §4.7独立标记为**比安装本身更紧迫** |
| `project_install_bridge_dual_review_closed`（14轮周期完成，GO，2026-08-19已装） | **部分需要修正**：台账原文称"DONE"/GO，但`install-bridge-install-recommendation-2026-08-20.md`（更晚、更细）明确指出这是对读侧hook脚本本体（claude_memory_hook.py）14轮复核的GO描述，而"把非默认event/write-trigger相关的安装器改动接上"这一增量的**最后正式复核实际是NO-GO**，未经复核转GO，是用户决定停止迭代 |
| `feedback_default_op5_sol_for_decisions` / 双模型复核规则 | 全篇台账严格遵循，多处因任一路P1即阻断（如Prime Agent 52轮、install_bridge多轮、R2 saga 9轮） |
| `reference_orca_json_bash_truncation_bug` | 未在本次读取的报告中直接复现，但与容量门禁读数机制相关（第7节多次提到容量红黄绿波动） |
| `project_desktop_control_deepseek_mouse_trail`（桌面控制方案进行中，SiliconFlow key位置未确认） | 本仓库reports/*.md未见对应文件，应属另一独立、未在本项目内追踪的任务线 |
| `project_codex_tier_skill_routing_debloat` / `project_codex_managed_home_resource_dual_review_closed`（staged, not committed） | 本仓库grep未命中，属于另一workspace（"完善codex"），不在本次reports/*.md范围内 |

---

## Part D — 按阻断类型汇总的"已知未完成"条目速查（供上级优先级排序）

**BLOCKED_HUMAN_DECISION（需用户先做产品/政策决策，技术工作才能继续）**：projects/orca@main身份、Orca 1.4.182同源性、知识图谱分支去留、内部盘rollback政策（表格未同步）、启动上下文NACK的ACK层、internal-projects-cold-preflight、paperclip受信任broker设计、paperclip-skill并入决策、sol-memory-strengthening 4个P0设计方向、holaOS建议①③、桌面desktop-mcp peer认证、ego-capability-fixture去留、4个兄弟worktree代提交判断、优化本机code候选归属。

**BLOCKED_DUAL_REVIEW（需Claude opus+max与Codex sol+max两路独立通过）**：startup-loader-closure、startup-p1-offline-acceptance正式验收、startup-reviewed-pack-schema3-fastfix v4-v7、startup-transaction-closure v2、Prime Agent round 53（进行中）、wiki重新钉哈希的manifest候选、holaOS建议②context_pack()升级、MEMORY-GRAPH-TIMESTAMP-WIKI-DESIGN落地前。

**BLOCKED_HUMAN_AUTH（需一次性明确授权不可逆动作）**：startup-installer-closure、startup-reviewed-pack-closure原子提交、r2-production-trust-closure、android-retained-retirement-closure、cleanup-internal-green-wave2 14个Trash源、task_2cfbe4265261迁移、codex-account-01权限真实核查、ORCA_CONTEXT_NACK_V1的wiki内容人工复核、（未来）Prime Agent的`install`/`enable`、（未来）DOWNLOADS-DOCUMENTS-SSD迁移的多个授权点。

**BLOCKED_APPLE_ENTITLEMENT**：SSD原生存储与PTY沙箱全部8项（本会话不可推进）。

**BLOCKED_CAPACITY**：4个startup候选的测试/复核重跑。

**代码完成但激活/安装被明确暂缓，等用户拍板（不是阻断，是"随时可做但故意没做"）**：codex-restore-tool（9轮双GO）、claude-codex-memory-bridge读侧桥接（4轮双GO）、Prime Agent安全修复本体（round 52双GO）、write-trigger的`install-write-trigger`本身。

**确认技术上不可行、需要外部/上游动作才能解开**：statusline可@短名字（需Orca启动逻辑加`-n`或Anthropic upstream功能）。

**已诊断根因但未修复，且被后续报告独立复核确认"至今仍未修复"**：`ORCA_CONTEXT_NACK_V1`wiki哈希不匹配（2026-08-18诊断，2026-08-20 Audit-4复核仍确认未修）。

**独立安全待办，与任何"候选/复核"周期无关，仍未处理**：已泄露凭据轮换（写在两处独立文档里，均标记为紧迫）。

**尚未立项/设计完成但零代码**：反向记忆桥接（codex→claude）、DOWNLOADS-DOCUMENTS-SSD迁移执行、wiki时间戳/新鲜度机制实现、wiki索引完整性缺口（13个候选页面全部未建）、orca-context-bridge完成态跟踪架构性改造（5条建议全部未落地，含破坏用户自己CLAUDE.md容量门禁规则读数含义的字段语义静默变更）。

---

**关键文件路径**（供后续深入核对）：
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/COMPLETION-BACKLOG-2026-08-16.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/STATUSLINE-SESSION-ID-2026-08-20.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/install-bridge-install-recommendation-2026-08-20.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/write-trigger-status-2026-08-19.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/MEMORY-GRAPH-TIMESTAMP-WIKI-DESIGN-2026-08-20.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/MEMORY-GRAPH-WIKI-AUDIT-4-COMPLETENESS-GAPS-2026-08-20.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/DOWNLOADS-DOCUMENTS-SSD-DESIGN-CANDIDATE-2026-08-20.md`
`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/reports/DOWNLOADS-DOCUMENTS-SSD-DESIGN-CRITIQUE-2026-08-20.md`