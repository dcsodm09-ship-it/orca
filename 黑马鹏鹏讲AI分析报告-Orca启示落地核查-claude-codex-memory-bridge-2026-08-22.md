# 黑马鹏鹏讲AI 分析报告 → Orca启示 落地核查（claude-codex-memory-bridge）

本报告是对上一份"黑马鹏鹏讲AI"内容分析报告中提出的"对Orca系统的10条启示"，逐条核对进 `claude-codex-memory-bridge` 这个具体代码库后的诚实结果。目的不是为了强行把每一条启示都落地成一次代码改动——能在现有实现或文档里确认已经覆盖的，就如实说已覆盖；目标子系统根本不在这个仓库里、不适用于这里的，就如实说不适用；只有在真正发现可复现的真实缺口时，才给出具体的补丁提案。以下四段核查分别对应四组启示，第五部分给出10条启示的结论清单，末尾附上唯一一份出现过的diff补丁提案（未应用、未安装）。

---

## 一、PII与记忆隐私边界（启示#2）核查结果

### 定位

`redact()` 定义在 `claude_memory_hook.py:1794-1828`，被 `split_blocks()`（`1853`行：`redact(text)[:4_000]`）在每次向 Codex 输出前统一调用；整个文件本身是**只读桥接**（文件头 `2-6` 行docstring自述："intentionally read-only"），不写入 Claude 原生 `MEMORY.md`——写入发生在 Claude Code 自身，不在这个仓库的控制范围内。

### 逐条对照

**① "长期记忆写入前做PII识别与脱敏" —— 部分覆盖，有真实缺口**

`redact()` 现有的 9 道处理（`1795-1827`行）覆盖的是**凭据/网络标识符类**结构化敏感字段：PEM私钥、URL userinfo、Bearer/Token/JWT、`password=`/`key:` 形式的键值对（`_ASSIGNMENT_RE`）、query密钥、IPv4/IPv6/MAC、邮箱、48+字符base64 blob、中英文表格/行内标注的secret字段，以及 `$USER_HOME` 路径归一化。这部分与"结构化敏感字段"高度吻合，且在"对外发布给Codex"这一等效边界上正确地做了脱敏（该bridge没有"写入"路径，这是本文件语境下该原则的正确落点）。

但 `redact()` **完全不识别通用个人身份信息**（手机号、身份证号等）——`README.md:9` 自己也写明范围仅是 "redacts common secret and server-address forms"。已实测验证（直接调用仓库里的真实 `redact()`，非模拟）：

```
>>> redact('联系人张三，手机号 13800138000，身份证号 110101199003077758')
'联系人张三，手机号 13800138000，身份证号 110101199003077758'   # 原样输出，未拦截
```

姓名（张三/王小明）、地址（"北京市海淀区中关村南大街5号"）同样原样透传——这两类因为没有可靠的结构化形状，用正则处理确实不可靠（这一点恰好呼应启示#2自己的顾虑），不为它们提方案。但手机号、身份证号是标准GB格式、结构固定，属于可以安全用正则精确识别的"结构化敏感字段"，是一个真实、具体、可复现的缺口：如果 Claude 原生 `MEMORY.md` 里记了这类内容（长期项目笔记里完全可能出现，比如记某个联系人电话），会被原样转发进 Codex 的 `UserPromptSubmit` 上下文——正是启示#2警告的"跨Agent上下文泄漏"的现实版本。

**② "明确区分全局共享记忆与单Agent/单任务私有记忆" —— 已覆盖**

- `README.md:23-35`："Scoped by the invoking Codex session's own `cwd`" —— 只读取 cwd 派生出的**唯一**项目目录的 `MEMORY.md`，且额外要求该项目自己的 session transcript 记录过这个确切 cwd。
- `read_memory_documents()`（`852-927`行）的头部注释明确记录了它修复的正是"全局/私有"混淆问题："previous implementation... read every Claude workspace's memory indiscriminately... a cross-workspace memory leak"，现在改为只读 cwd 派生的单一项目目录。
- `_session_recorded_cwd_matches()`（`773-849`行）更进一步：只要同一目录下发现**任何**第二个真实 cwd 的记录（证明这目录被多个工作区共享），就对**所有人**（包括请求方自己）拒绝服务（`843-848`行），宁可服务失败也不冒着把一个私有记忆当成"共享"提供出去的风险。

这是对"全局共享 vs 单任务私有"边界的一个偏保守、fail-closed的明确实现，没有发现缺口，不提改动建议。

### 针对①缺口的最小补丁提案

完整 diff 见文末《附：补丁提案》一节。核心思路：在 `_LONG_BLOB_RE` / `_HOME_RE` 之后新增两个独立正则 `_CN_ID_NUMBER_RE`（18位身份证号）与 `_CN_MOBILE_RE`（11位手机号），并在 `redact()` 现有9道pass之后追加两道新pass，替换为 `[REDACTED_ID]` / `[REDACTED_PHONE]`。

**为什么安全**（均已用仓库里的真实正则实测，非猜测）：

1. **不会误伤已有alnum标识符**：边界用 `(?<![A-Za-z0-9])…(?![A-Za-z0-9])`（与 `_LONG_BLOB_RE`/`_EMAIL_RE` 同一惯用法），实测 `"ORD1385551234567X99"`、`"deadbeef13800138000cafebabe"` 这类字母粘连的订单号/哈希串**不会**被误判为手机号/身份证号。
2. **不会partial-leak**：身份证18位/手机号11位都是定长连续数字，`(?<![0-9…])`/`(?![0-9…])` 边界保证不会像历史上MAC/IPv6那样只吃掉一部分、留下"5/6段泄漏"式的半脱敏结果（本文件`1172-1179`行记录过这个教训，实测里 `"tracking 84123800138000123456789"`、`"repeated 222222222222"` 均不匹配，无残留片段）。
3. **CJK粘连边界正确**：`[A-Za-z0-9]` 不包含CJK字符，所以 `"手机13800138000该"` 这种无空格中文粘连场景能正确整体脱敏——这正是本文件反复修过的那类bug（`_BEARER_RE`注释 `966-993`行），新增规则直接复用了同一个已验证惯用法，而不是引入新的边界写法。
4. **幂等**：`redact(redact(x)) == redact(x)`——替换后的文本是纯字母的 `[REDACTED_ID]`/`[REDACTED_PHONE]`，新正则要求连续数字，不会二次匹配自己的输出，实测验证通过，兼容 `tests/test_claude_memory_hook.py:1075-1103` 的幂等性要求。
5. **不影响已有120,000+ containment-fuzz结论**（该结论记录在 `claude_memory_hook.py:955` 行注释，针对的是 `_URL_USERINFO_RE`/`_BEARER_RE`/`_TOKEN_RE`/`_JWT_RE` 四个模式自身的边界改造）：本补丁完全没有触碰这四个模式或它们的边界逻辑，只是在 `redact()` 管线里追加两个独立、互不重叠的新pass，不改变既有pass的匹配范围或顺序语义。
6. **不影响现有测试fixture**：搜了 `tests/test_claude_memory_hook.py` 里所有9位以上连续数字（`222222222222`、`444444444444`、`1234567890`等），均因不满足"手机号第2位3-9"或"身份证年份/月份/日期段"的格式约束、或因数字段更长导致边界不成立而不会被新增正则误匹配。

如果这个补丁被采纳，建议再补两条对应用例（`test_redacts_cn_mobile_number_glued_to_cjk`、`test_redacts_cn_id_number_does_not_leak_partial_digits`），但这已超出"最小补丁"范围，未包含在diff里。

---

## 二、记忆分层/主动遗忘/混合检索（启示#1、#3、#4）适用性核查

### 结论先行

**claude-codex-memory-bridge 项目本身：三条启示均不适用，仓库里没有它们的目标子系统。**
**整个 完善orca worktree 里：确实存在一个相关的真实产物，但它是一份"只读架构审计"，不是可安装运行的代码；它审计的对象（真正的向量存储/混合检索引擎）也不在这个 worktree 里，而在别的 Orca sibling worktree 中。**

### 2.1 claude-codex-memory-bridge：逐一核实，全部是噪音命中

在 `claude_memory_hook.py` / `install_bridge.py` / `write_candidate_capture.py` 及其测试文件里搜索 `vector|embed|semantic|retrieval|faiss|chroma|pinecone|weaviate|qdrant|milvus`，共约 30 处命中，逐条核实后**没有一处是真实的向量/语义检索实现**：

- `std::vector<int>`（C++ 容器类型名，出现在测试用例和注释里，用于验证正则表达式不会把 `d::` 误判成压缩 IPv6 地址）
- `embedded`（英文常用词"嵌入在……中"，指字符嵌入文本、JSON 转义嵌入等，与 embedding 模型毫无关系）
- `semantics`（英文常用词"语义/含义"，指 shlex 语义、UTF-16 语义、fail-closed 语义等程序行为，不是语义检索）
- `vectors`（测试用例里指"攻击/回归测试向量"，不是嵌入向量）

`README.md` 第 7 行原文已经明确排除了这个方向：

> "The bridge does **not** merge memory databases, modify Claude, rebuild Orca, or grant historical notes authority. It selects only prompt-relevant excerpts, redacts common secret and server-address forms, labels every excerpt as untrusted historical reference, and emits at most 7,000 UTF-8 bytes."

这个 hook 的实际逻辑是：按关键词/触发器（T1-T7 等规则）从 `MEMORY.md` 里做**词法级**片段抽取 + 正则脱敏 + 字节截断，再原样转发给 Codex 的 `UserPromptSubmit`。没有向量库、没有 embedding 调用、没有存储路由判断（"精确查找走 KV 还是语义查找走向量库"这个分支根本不存在）、没有 TTL/重要性衰减、没有 agent_id/task_id 结构化过滤 + 语义打分的混合排序——因为它压根不做任何"检索排序"，只是单一项目单一 `MEMORY.md` 文件的规则匹配抽取。

**结论：如果强行把#1/#3/#4 落地到这个 hook 里，就是在一个明确声明"不做记忆数据库"的脱敏转发工具里凭空造一个向量库，这既不是这三条启示的本意，也会违背这个项目自己的 README 承诺和现有 14+ 轮双复核建立起来的窄职责边界。不建议这样做。**

### 2.2 整个 worktree：确实有一处相关但性质不同的产物

`sol-memory-strengthening-audit/`（2026-08-11，Codex/Sol 出具的独立离线架构审计，结论 `GO_OFFLINE_CANDIDATE_ONLY`，运行态/安装态明确 `NO_GO`）。其中：

- `retrieval_strength_evaluator.py` 是一个"确定性、无正文、无网络、无副作用"的离线评分原型，docstring 自陈"never opens a memory store, calls a provider, or changes runtime state"。它的打分权重表（`lexical` / `semantic` / `task-intent-step` / `causal-dependency` / `evidence-outcome` / `time-scope` / `relation_strength` / `confidence` / `evidence_strength`）、四类硬关系（`CONTRADICTS`/`REVOKES`/`SUPERSEDES` 硬门禁）、L0-L3 分层状态机（`active`/`conflict_review`/`revoked`/`expired`/`superseded`），在概念形态上确实接近启示#1（分层）、#3（状态含 expired/revoked，暗示遗忘机制）、#4（结构化过滤+语义打分融合排序）。
- 但它审计的**对象**——真正跑起来的向量存储引擎——是 `tools/orca-agent-memory/memory_storage.py`，其中包含真实的 `text_vector()`、`vector_json` 列、`cosine()` 相似度、lexical-RRF + vector-RRF + graph-RRF 融合排序、近重复去重（cosine ≥ 0.93）等实现。**这个文件不在 完善orca 这个 worktree 里**，而是分散存在于若干个同级 sibling worktree 中（如 `优化本机code/tools/orca-agent-memory/memory_storage.py`、`codex-track3-commit-tiering/...`、`codex-account-binding-fix/...`、`orca-w2-memory-skills-9959/...` 等各自持有的拷贝），以及审计报告里提到的一份"SSD authority source"和临时 wheel 候选路径。
- `REPORT.md` 自己对这套 live 系统的诊断，几乎逐字对应启示#4 想解决的问题："graph boost 只是 lexical top-10 的近邻布尔加分，不能称为关联强度治理""CONTRADICTS、REVOKES、SUPERSEDES 尚未参与运行态检索硬过滤""score ≥ 0.020 不是经校准的置信度，也没有 margin、coverage 或 reason-coded abstention"。也就是说，这三条启示所指向的问题，Sol 在这份审计里已经独立发现并写清楚了——只是落地目标不在这个 worktree 能编辑的范围内。

### 2.3 给用户的诚实结论

- 如果目标是 `claude-codex-memory-bridge` 这个具体仓库：三条启示的目标子系统不存在，也不该在这里制造一个。它是单文件规则抽取+脱敏转发，不是记忆检索引擎。
- 如果目标是"Orca 的 agent 向量记忆/检索引擎"本身：它确实存在，但物理上位于 `完善orca` 之外的其它 Orca sibling worktree（`tools/orca-agent-memory/memory_storage.py` 及其 SSD authority source），而不在这个 worktree 里，也不是 r2-memory MCP（r2-memory 是另一个独立系统，这次没有证据显示它也做向量检索，本次未展开核实）。
- `完善orca/sol-memory-strengthening-audit/` 是目前离这三条启示最近的真实产物，但它是一份**未安装、未运行**的只读审计+离线原型，不是生产代码，其结论本身已经指出了和#1/#3/#4 高度重合的具体缺口，可以作为下一步设计讨论的现成参考，但不代表这个 worktree 里有可以直接改的运行态代码。

相关路径（均为绝对路径）：
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/claude-codex-memory-bridge/README.md`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/claude-codex-memory-bridge/claude_memory_hook.py`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/sol-memory-strengthening-audit/retrieval_strength_evaluator.py`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/sol-memory-strengthening-audit/REPORT.md`
- `/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code/tools/orca-agent-memory/memory_storage.py`（真实向量存储实现所在的 sibling worktree 示例）

---

## 三、透明定位与可解释中间态（启示#5、#6）核查结果

### 结论

README 已经在**行为设计**层面充分体现了两条启示的精神，但在**总结性陈述**层面各缺一句话，把已经存在的做法明确"点破"给读者。分述如下。

### 启示#5（可解释的中间态弥合信任分裂）—— 已大量体现，缺一句点题总结

证据（原文引用）：

1. **标注型中间态**："It selects only prompt-relevant excerpts, redacts common secret and server-address forms, **labels every excerpt as untrusted historical reference**, and emits at most 7,000 UTF-8 bytes."（第7-10行）—— 每条注入的记忆都带着"未经信任的历史参考"标签，而不是静默混入上下文，这正是"可解释中间态"而非黑盒。

2. **复核轨迹直接写进产品文档，而非压缩成"已修复"**："Known limits" 一整段（第54-120行）逐轮标注是谁发现的、什么级别——"independent Claude opus5/max review, 2026-08-17, round 2"、"independent Codex sol/xhigh review, 2026-08-17, P1-R2-1"、"round-4 correction, R4-P3-2" 等。这就是启示#5 说的"复核轨迹、双模型交叉复核记录"被当作可对外沟通的内容，而且细到"哪一轮、哪个模型、修的什么、之前的说法哪里错了"，比一般项目的 CHANGELOG 透明得多。

3. **收据机制**："install... first writes private, content-addressed runtime files and private backups, then records a durable pending transaction before it atomically replaces only the discovered Codex hook configurations... A partial config or receipt failure restores the original hook files."（第139-144行）以及 verify/recover 命令（第162-168行）——安装动作本身产生可核验的"receipt"，不是"装了就装了，信我"。

4. **Acceptance boundary 明确划出"证明了什么/没证明什么"的边界**（第162-168行）："Neither is proof that an old note is current or permission to perform actions described in that note; the underlying fact must still be re-verified." —— 这正是不做黑盒承诺、把可验证边界摊开给用户看。

**差的一句**：以上证据都存在，但README从未用一句话点明"我们为什么把这些复核细节、标签、收据都写出来"——读者要自己从四处证据里拼出"这是刻意的信任设计"这个结论。建议加一段总结。

**建议插入位置**：第10行（`emits at most 7,000 UTF-8 bytes.` 段落结束）之后、`## Storage and integrity contract` 小节之前，新增一段：

> 本README刻意保留每轮独立复核的具体发现、模型名与修复记录，而非只写"已修复"。把复核轨迹、异常标注与安装收据这些可解释的中间态公开，是为了在"全自动黑盒"与"逐行人工审查"两极认知间提供可验证的中间层——不读代码也能核实谁在何时发现并修复了什么。

（126字，可直接插入）

### 启示#6（自主纠错的显式可配置升级策略）—— 精神已体现，但"策略"从未被命名，也不可配置

证据：

1. **默认拒绝服务而非猜测**（fail-closed 是全文反复出现的核心原则）："A missing or non-absolute `cwd`... fails closed to no context rather than falling back to scanning every project."（第32-35行）；"Invalid input, storage drift, integrity drift, unsafe files, or unavailable SSD state emits no context and exits without blocking Codex."（第121-123行）。

2. **明确划出"必须人工介入"的节点，而不是静默重试或降级**："Installing this bridge therefore risks temporarily disabling every already-working hook on the same events... until a human interactively re-trusts hooks for that Codex account. **Budget for that human step as part of any install, not as an afterthought.**"（第174-180行）—— 这就是"升级到人工"这条路径被显式点名，而不是二元黑盒里"要么全自动、要么完全不管"。

3. **安装/回滚的自动纠错有明确边界，超出边界就停手交给人**："If any hook config changed afterward, uninstall stops instead of overwriting that newer work."（第157-159行）—— 可安全判定范围内自动回滚（自动纠错），超出可证明安全的范围就停止并保留现场（升级人工），而不是继续猜测式重试。

**差的一句**：这套"能证明安全就自动处理、不能证明就一律拒绝并显式要求人工"的**策略本身**分散在 Storage/Commands/Acceptance boundary 三处，从未被命名成一条统一策略，也没有说明"为什么这里选择不做自动重试"（对比启示#6 提到的"该问工程问题问成UE"这类边界模糊场景，这个bridge至少没有模糊——但也没有一句话讲清楚"我们把升级策略定成了'零重试、失败即拒绝、只有一个人工介入点'，因为后果是上下文注入错了目标（体验差）还是可能把不该看的内容带进结果（实质风险）"）。

**建议插入位置**：`## Commands` 小节结束（第160行`Content-addressed runtime and backups are retained for recovery.`之后）、`## Acceptance boundary` 小节之前，新增一个小节：

```
## Failure and escalation policy
```
> 本bridge不做自动重试或猜测性降级：任何输入、存储或权限的不确定情况一律拒绝提供上下文而非蒙对（fail-closed）。唯一需要人工介入的节点是安装后Codex hooks的重新信任——这是刻意设计，不是遗漏的自动化：上下文缺失只是体验变差，而静默猜测可能把不该看的内容混进结果。

（143字，可直接插入）

### 总体判断

两条启示的**设计精神**在 bridge 的实际行为和 README 的详尽记录里都已经真实存在，证据充分且具体到代码行为（标签、收据、fail-closed、人工再信任步骤）和文档实践（逐轮复核记录直接写入 README）。缺的不是设计，而是各一句"总结性点题"——把分散的证据收束成一句读者一眼能看懂的"我们为什么这样做"，分别加在上述两处即可，均在150字以内。这属于**文档措辞建议**，不是代码补丁，因此未列入文末补丁提案。

---

## 四、完成声明可复现验证/demo-vs-生产/跟踪upstream（启示#7、#8、#9）核查结果

基于直接统计与抽样验证，证据如下：

**数量证据**：仓库顶层共 31 个 `OPUS5-INDEPENDENT-REVIEW-*.md` + 12 个 `CODEX-*-REVIEW-*.md`，合计 43 份复核报告，其中与 claude-codex-memory-bridge / install-bridge / redaction / write-candidate-capture 相关的正好是这 43 份全部（命名即按这些主题分组，如 install-bridge 单独就有约 25 轮，从 ROUND2 一路到 ROUND53，跨度 2026-08-17 至 2026-08-21）。

**启示#7（关键决策可复现验证）**：抽样读取 ROUND53（opus/max）和 ROUND14（Codex terra/high）两份报告，内容不是自我声称，而是可复现的执行证据——ROUND53 用 monkeypatch 拦截真实函数调用、真实文件复现 `ModuleNotFoundError` 未被捕获的 P1 缺陷；ROUND14 用真实 SIGKILL 中断、`chmod 000` 制造不可读文件、在 Python 3.9.6 与 3.14.6 双解释器上跑 206/316 个单元测试两次，并给出非空对照基线（baseline commit diff）防止复核"打空拳"。两路复核明确"parallel Codex leg ran with no coordination"，体现真正独立而非协同确认。

**启示#8（警惕demo冒充生产）**：多轮复核判定为 NO-GO（如 ROUND53 因 P1 阻断，ROUND14 之前多轮同理），说明复核确实在拦截未达标代码，而非走过场；PRIME-AGENT-ROUND53-CODEX-QA-REPORT 还明确指出"四个包字节级锁定，但不能宣称整棵196包依赖树等同于v0.7.2源码提交的确切树"，主动戳破了范围夸大的产品级声明。

**启示#9（主动跟踪upstream）**：同一份报告实测发现约36处版本漂移（zod 3.25.76→4.4.3、@types/node 22→26等）、一个未修补的HIGH级漏洞公告，以及"约2天的pin半衰期"，证明已建立主动比对upstream registry证据的常态机制，而非静态锁定后不再复查。

三条启示均有充分数字与可复现证据支撑，当前复核文化已满足要求，未发现需要补丁的缺口。

---

## 五、结论清单

原分析报告称对Orca系统提出了"10条启示"，但本轮实际提供并核查的四段材料只明确点名了启示#1至#9；材料中没有出现关于启示#10主题的任何信息，为避免编造，下表如实将其标注为"本轮未核查"，而不是代入猜测性状态。

| # | 启示主题（据本报告小节归纳） | 状态 | 一句话理由 |
|---|---|---|---|
| 1 | 记忆分层（分级存储/状态机） | 不适用 - 属于别的子系统 | claude-codex-memory-bridge 是单文件规则抽取+脱敏转发，没有分层记忆的目标子系统；相关的L0-L3状态机原型存在于本worktree外的sibling worktree向量存储实现中。 |
| 2 | PII识别脱敏 + 全局共享vs单任务私有记忆边界 | 发现真实缺口已提出补丁提案 | `redact()`覆盖凭据/网络标识符但不识别手机号/身份证号等结构化PII（已实测复现，已提出正则补丁）；而"全局共享vs单Agent私有"边界已通过cwd-scoped读取+fail-closed拒绝服务实现，此部分未发现缺口。 |
| 3 | 主动遗忘（过期/撤销/超越机制） | 不适用 - 属于别的子系统 | 该bridge没有存储/过期机制，是单次抽取转发；相关的expired/revoked状态机同样只存在于worktree外的审计原型与sibling worktree实现里。 |
| 4 | 混合检索（结构化过滤+语义打分融合排序） | 不适用 - 属于别的子系统 | 该bridge按关键词/触发规则做词法级片段抽取，没有向量库、embedding或排序分支；真正的lexical-RRF+vector-RRF+graph-RRF融合排序实现于`tools/orca-agent-memory/memory_storage.py`，不在本worktree内。 |
| 5 | 可解释的中间态弥合信任分裂 | 已覆盖 | README已用"untrusted historical reference"标签、逐轮复核细节、安装收据、acceptance boundary四类证据体现这条精神，只缺一句总结性点题（文档措辞建议，非代码缺口）。 |
| 6 | 自主纠错的显式可配置升级策略 | 已覆盖 | fail-closed默认拒绝、明确的人工再信任介入点、安装/回滚的可证明安全边界均已实现，只是这套策略从未被统一命名成一节（文档措辞建议，非代码缺口）。 |
| 7 | 完成声明的可复现验证 | 已覆盖 | 抽样的ROUND53/ROUND14复核报告显示复核用monkeypatch拦截真实调用、SIGKILL/chmod 000等真实故障注入、双解释器测试、非空对照基线，不是自我声称。 |
| 8 | 警惕demo冒充生产 | 已覆盖 | 多轮复核实际判定NO-GO拦截未达标代码，且PRIME-AGENT-ROUND53报告主动戳破"整棵196包依赖树等同v0.7.2源码树"的范围夸大声明。 |
| 9 | 主动跟踪upstream | 已覆盖 | 同一报告实测发现约36处版本漂移、一个未修补HIGH级漏洞公告、约2天pin半衰期，证明存在主动比对upstream证据的常态机制。 |
| 10 | （本轮四段核查材料中未提及具体主题） | 本轮未核查（无对应材料） | 提供的四段核查内容仅覆盖启示#1-#9，不含启示#10的任何信息；按"只使用提供内容、不编造"的要求，如实标注为本轮未核查。 |

---

## 附：补丁提案（未应用，未安装）

以下diff是本报告"一、PII与记忆隐私边界"一节中针对①号缺口（`redact()` 不识别手机号/身份证号）给出的唯一补丁提案，原样收录：

```diff
--- a/claude-codex-memory-bridge/claude_memory_hook.py
+++ b/claude-codex-memory-bridge/claude_memory_hook.py
@@ -1779,6 +1779,17 @@
 )
 _LONG_BLOB_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{48,}(?![A-Za-z0-9])")
 _HOME_RE = re.compile(r"/Users/[^/\s]+")
+# Structured PII beyond credentials (启示#2 gap, 2026-08-22): a mainland
+# China mobile number and 18-digit resident ID number are exactly the kind
+# of fixed-shape "structured sensitive field" that must not reach another
+# agent's context unredacted -- unlike a personal name or street address,
+# both have a checkable format a regex can target without an NLP model.
+# Same `[A-Za-z0-9]` boundary idiom as `_LONG_BLOB_RE`/`_EMAIL_RE` above
+# (not `\b`, which silently fails at a CJK-glued edge -- see `_BEARER_RE`'s
+# own comment on this exact bug class), so this cannot partially match a
+# digit run embedded inside a longer alnum token (order id, hash) and
+# cannot leave a boundary-adjacent fragment leaked either.
+_CN_ID_NUMBER_RE = re.compile(
+    r"(?<![A-Za-z0-9])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
+    r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![A-Za-z0-9])"
+)
+_CN_MOBILE_RE = re.compile(r"(?<![A-Za-z0-9])1[3-9]\d{9}(?![A-Za-z0-9])")
 
 
 def _redact_ipv6(match: re.Match[str]) -> str:
@@ -1808,6 +1819,8 @@
     text = _MAC_ADDRESS_RE.sub("[REDACTED_IP]", text)
     text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
     text = _LONG_BLOB_RE.sub("[REDACTED_BLOB]", text)
+    text = _CN_ID_NUMBER_RE.sub("[REDACTED_ID]", text)
+    text = _CN_MOBILE_RE.sub("[REDACTED_PHONE]", text)
     # CJK-secret passes run last (round-2 dual-review finding, 2026-08-22, item 1): every pattern
     # above already gets first crack at any ASCII run in the text, so these three passes can only
     # add further redaction on top of what's left -- never truncate an ASCII run that one of the
```

**这些改动只是提案，没有写入任何文件。** 如需推进，必须走这个项目已有的 Claude opus/max + Codex sol/max 双路独立只读复核流程，且不得未经用户明确授权自行安装到 `~/.codex/hooks.json`。
