# 记忆图谱与 Wiki 时间戳 / 新鲜度详细设计（终稿）

*基于 AUDIT 1–4 既有结论与一轮对抗性复核撰写。范围：`wiki/orca-context-wiki.json`、`orca-context-bridge/scripts/build_knowledge_graph.py`、`.orca/context/reviewed-startup-pack-manifest.json` 及其校验路径 `orca-context-bridge/scripts/build_startup_bundle.py::verify_reviewed_pack`。本稿在候选设计基础上，逐条核对了对抗性复核提出的 10 项发现（1–3、6 为 Critical/需要重新设计，4、5、9 为需要改写代码级 diff 的事实性错误，7、8、10 为文档自洽性与信息完整性问题），并把修复直接写入相应章节，而不是在文末附加勘误表。*

*第 6 节风险 8 要求原始审计报告可被独立核对原文——已按此落盘，供交叉核对：
[Audit 1：wiki 内容现状](MEMORY-GRAPH-WIKI-AUDIT-1-WIKI-CONTENT-2026-08-20.md) ·
[Audit 2：知识图谱 schema](MEMORY-GRAPH-WIKI-AUDIT-2-GRAPH-SCHEMA-2026-08-20.md) ·
[Audit 3：新鲜度机制](MEMORY-GRAPH-WIKI-AUDIT-3-FRESHNESS-MECHANISM-2026-08-20.md) ·
[Audit 4：完整性缺口](MEMORY-GRAPH-WIKI-AUDIT-4-COMPLETENESS-GAPS-2026-08-20.md) ·
[候选设计（对抗性复核前）](MEMORY-GRAPH-WIKI-DESIGN-CANDIDATE-2026-08-20.md) ·
[对抗性复核意见](MEMORY-GRAPH-WIKI-DESIGN-CRITIQUE-2026-08-20.md)。
本文档是这条链路的终稿，是唯一建议直接拿去实现的版本。*

---

## 摘要

四份审计报告和一轮对抗性代码复核共同指向同一个结论：wiki 和知识图谱缺的不是字段，是**一套能被真正强制执行、且与代码现状对齐的写入纪律**。本设计据此做了三层交付：

1. **时间戳语义层**（第 1 节）：把"时间"拆成 `created_at`/`updated_at`/`verified_at`/`source_mtime` 四个互不可替代的字段，统一 ISO 8601 UTC 秒精度格式，并对"迁移日统一时间戳会制造虚假新旧感"这一新发现的风险给出了具体缓解（记录 `migration_sequence` 保序号，而不是让 13 个页面看起来一样新）。
2. **写屏障 + 校验层**（第 2 节）：把 `wiki_edit_guard.py`（编辑时强制版本递增）从"设计层描述、本轮不实现"提升为**本轮必须交付的核心组件**——因为对抗性复核证明了，如果只做 §5 的 schema 扩展和 session 启动时的校验，Audit 3 发现的"改了 wiki 没重签、10 小时后才发现"这个具体问题**完全没有被缩短一分钟**（新增的 `content_version` 在 session 启动时和现有 sha256 校验是信息冗余的，只有编辑时的守卫才提供新价值）。同时，本节已经确认仓库里**实际生效的 manifest 是 schema_version=2，而当前代码只接受 3/4**，这是一个先于本设计存在的既有落差，必须先被确认/处理，本设计对 manifest 的扩展直接建立在 v3/v4 的真实字段形状上（`project_git`/`content_source_closure`，不包含已被废弃的 `authority_signed_at`/`signed_by`/`signature_nonce`），并把这一落差本身列为第 6 节的独立风险项，不能被这份设计悄悄"顺带修好"。
3. **迁移与内容扩充层**（第 3、4 节）：`source_mtime` 回填真实值，`verified_at` 一律诚实为 `null`，`created_at` 用"目录化时间"而非编造的源文件创建时间，并新增顺序保留字段以避免"看起来一样新"的次生误导。14 个新页面的优先级判断所依赖的审计结论以内联方式在第 4 节自证，不依赖仓库外部的、未提交的 Audit 1–4 文件。

第 5 节给出的知识图谱 schema diff 已经对照 `build_knowledge_graph.py` 的真实代码（`GraphBuilder.__init__` 不含 `generated_at`、`add_node` 的 `self.nodes` 是字典而非列表、`add_sessions`/`add_github` 目前不接收 `sources`、`parse_timestamp` 从不抛异常而是返回 `None`）逐处修正，不再是"看起来像"却实际无法直接落地的伪代码。第 6 节在原有三条风险之外新增三条：`GRAPH_VERSION` 升级对下游消费者的破坏性、当前 manifest schema 落差本身的风险、以及审计结论未随本设计一并落盘的可追溯性风险。

---

## 0. 设计前提与总体思路

四份审计报告与一轮对抗性复核共同指向同一个根因，不是"缺字段"而是**缺一个能真正被强制执行、且与代码现状对齐的写入纪律**：

- Audit 1：wiki 目录里没有任何时间戳字段，14 个页面里至少 5 个内容已经和它引用的源文件/脚本实际状态不一致，且没有任何机制提示读者"这条可能过期了"。
- Audit 2：知识图谱里已经零散存在 3 个原始时间戳字段（`session.updated_at`、`pr.updated_at`、`reviewed_knowledge.signed_at`），但都是未校验的原样透传，7 个节点类型完全没有时间信息，边（edge）从未带时间戳。
- Audit 3：仓库里唯一真正生效的"新鲜度"机制是 `verify_reviewed_pack` 的哈希比对（fail-closed，正确），但它和 wiki 的实际编辑动作之间没有任何联动——wiki 改了没人重新签名，要等到下一次 session 启动才被动发现，实测缺口约 10 小时。
- Audit 4：wiki 只覆盖了这个仓库的"测试/验证层"，覆盖不到实际工程交付物（memory bridge、Prime Agent、SSD/R2 闭环家族等），当前的扁平 `pages[]+links[]` 结构本身也撑不住这些新增内容。
- **对抗性复核**：候选设计的意图（四种时间语义分离、不用 TTL、宁可 `null` 不可编造 `verified_at`）是对的，但落到代码的具体 diff 存在多处与真实文件不符的地方，其中一处（manifest `wiki` 字段从字符串变对象后，`add_reviewed_manifest` 里 `expected == observed` 的字符串比较会永久失效为 `unbound`；`verify_reviewed_pack` 里 `valid_sha256(expected)` 会在见到 dict 时直接报错拒绝，而不是走到设计里描述的 isinstance 分支）如果原样实现，会让"安全权威校验"这条唯一生效的机制**先坏后修**，这本身就是本设计必须避免的那类错误。

因此终稿在候选设计的三条线之外，补了第四条：

1. **时间戳线**：给 wiki 页面/链接、知识图谱节点，加上语义明确、来源可追溯的时间戳字段（第 1、5 节）。
2. **写屏障线**：把新增的时间戳字段真正接入编辑动作和 `verify_reviewed_pack` 的校验逻辑，堵住 Audit 3 发现的"改了内容、没重新钉哈希"这个具体漏洞，且写屏障本身（不是仅有校验）必须在本轮交付（第 2 节）。
3. **迁移线 + 内容扩充线**：分别处理存量数据迁移和内容扩充路线（第 3、4 节）。
4. **代码对齐线（新增）**：所有落到 `build_knowledge_graph.py`/`build_startup_bundle.py` 的 diff，必须先对照真实代码（行号、函数签名、数据结构）逐处核实，不能停留在"看起来对"的伪代码层面；对当前仓库里已经存在、与代码要求不一致的既有状态（manifest 落在 schema_version=2），本设计如实标注为独立风险，不假装顺手修好（第 2.2.3 节、第 6 节风险 5）。

第 6 节列出全文中风险最高、最容易被做错的点，并给出对应的测试要求。

---

## 1. 时间戳方案

### 1.1 先定义清楚"时间"到底有哪几种语义

四种语义必须严格区分，不能合并成一个笼统的 `timestamp`：

| 语义 | 含义 | 谁负责产生 | 能否自动化 |
|---|---|---|---|
| **创建时间 `created_at`** | 这条 wiki 页面 / 图谱节点第一次被写入目录的时刻 | 首次写入该条目的脚本或人 | 可自动（首次落盘时打一次戳，之后不变） |
| **最后更新时间 `updated_at`** | 页面/节点**自身记录的内容**（`title`/`summary`/`status`/`meta`）最后一次发生变化的时刻 | 每次编辑该条目的脚本或人 | 可自动（每次写入前重算 diff，变了就更新） |
| **最后人工核实时间 `verified_at`** | 有人（或授权的验证脚本）**实际重新检查过**该条目描述的事实仍然成立的时刻——不是"文件被碰过"，是"内容被确认为真" | 只能由人工审查或明确设计为"验证器"的脚本产生，不能由普通编辑脚本顺手带出 | **不可**随意自动化，见 1.4 和第 6 节风险 2 |
| **源文件 mtime `source_mtime`** | 该条目所引用的物理源文件（`path` 字段指向的文件）本身的文件系统 mtime | `os.stat()` 直接读取 | 可自动，但**只是参考信号，不是证据**（mtime 可被 checkout/复制无意义地刷新） |

这四者互不可替代：`updated_at` 新不代表 `verified_at` 新（`resource-gate` 页面的 `updated_at` 是 8-10，但它描述的脚本行为已经在 8-16 被彻底改变，`verified_at` 应该早就该被标记为过期）；`source_mtime` 新也不代表内容真的变了（复制/touch 不改内容）。三者必须是三个独立字段，不允许用一个字段兼职表达。

### 1.2 数据类型与格式：ISO 8601 UTC，秒精度，`+00:00` 后缀

**格式**：`YYYY-MM-DDTHH:MM:SS+00:00`（UTC，秒精度，`isoformat()` 原生输出，不用 `Z` 后缀）。

**理由**：这是仓库现有的实际约定——`build_context_digest.py` 与 `build_knowledge_graph.py` 都各自写了同一个表达式 `datetime.now(timezone.utc).replace(microsecond=0).isoformat()`，产出的就是这个格式。新字段必须跟随既有约定，不能引入 `Z` 后缀或毫秒精度这类第二种风格。已存在的 `parse_timestamp`（`orca-context-bridge/scripts/build_context_digest.py:139-155`）同时接受 `Z` 和 `+00:00` 两种输入并统一转成 UTC-aware `datetime`，所以**读入时兼容、写出时统一**：任何要落盘的新时间戳字段，写之前必须过一遍 `parse_timestamp`，再用上述表达式重新格式化后写出，不允许把上游原始字符串直接透传落盘。

**关于 `parse_timestamp` 的一个必须遵守的实现约束（本节新增，对应第 5.2 节的具体修正）**：`parse_timestamp` 在输入无法解析时**返回 `None`，而不是抛异常**——它内部已经把 `ValueError` 吞掉了。任何调用它的新代码，都不能写 `try: parse_timestamp(x).isoformat() except (ValueError, TypeError): ...` 这种形式，因为 `None.isoformat()` 抛的是 `AttributeError`，不会被这个 `except` 捕获，会直接让调用方（例如整个知识图谱构建）崩溃。正确写法是先判断返回值是否为 `None` 再调用 `.isoformat()`，第 5.2 节给出的 `make_timestamps` 已按此修正。

`created_at`/`updated_at` 字段类型为**必填字符串**（ISO 8601）；`verified_at` 类型为**可空字符串**（`null` 表示"从未核实"，见 1.4）；`source_mtime` 类型为**可空字符串**（`null` 表示"该条目无单一物理源文件可对应"）。

### 1.3 每一类节点/页面的具体获取策略

#### wiki `pages[]`

| 情形 | `created_at` 来源 | `updated_at` 来源 | `source_mtime` 来源 |
|---|---|---|---|
| `path` 指向的文件在 git 中有提交历史（目前仅 `agent_capacity.py` 一例） | `git log --follow --format=%aI --diff-filter=A -- <path> \| tail -1`（首次新增该文件的提交时间） | `git log -1 --format=%aI -- <path>`（最近一次提交时间） | `os.stat(path).st_mtime` 仍然单独记录，作为交叉校验，两者理论上应接近但不强制相等 |
| `path` 指向的文件是 git 未跟踪文件（当前 13/14 属于此类） | **不可从 git 获得**；写入 wiki 该条目那一刻的时间（由编辑该条目的脚本/流程打戳，即"这条 wiki 记录本身第一次出现的时间"，而不是源文件的创建时间——两者本来就是两件事，不要混淆） | 每次该 page 对象的 `title`/`summary`/`status`/`path` 任一字段值发生 diff 时，由写入该 wiki 文件的脚本重新打戳（见 3.1/3.2 迁移脚本的同一套逻辑，后续所有编辑必须走这条路径而不是手工改 JSON） | `os.stat(path).st_mtime`，直接读取；若文件不存在（迁移后源文件被删除/改名的未来情形），置为 `null` 并在该 page 追加 `source_missing: true` 标记 |
| page 描述的是**探针/脚本的运行结果**而非静态文档（`orca-capability-tests`、`orca-readonly-probe`、`orca-cli-capability-catalog`） | 同"未跟踪文件"规则 | 同上，但额外要求：这类页面的 `summary` 中**任何具体数字（如"16/18 通过"）必须与 `verified_at` 绑定出现**，不允许写死一个数字却不说明是哪次运行产生的 | 若该页面同时引用了一个 JSON 结果文件，取该文件的 mtime；若只是文字描述、没有单独产物文件，则为 `null` |
| page 描述的是**策略/规则文档** | 同"未跟踪文件"规则 | 同上 | 同上 |

**`verified_at` 的获取策略统一，不分情形**：只能由以下两种途径产生，二选一，且必须记录是哪一种：

1. **人工审查**：人读了源文件/重新跑了探针，确认 `summary` 仍然成立，手动调用一个专门的核实命令（如 `wiki_verify.py --page <id> --by <人名或账号>`）来打戳，禁止直接手改 JSON 里的 `verified_at` 字段。
2. **自动化验证脚本**（仅适用于"探针类"页面）：脚本重新跑一遍对应探针，把新结果和 `summary` 里记录的结论做字符串/数值比对，**完全一致才自动打 `verified_at`**，不一致则脚本必须报错退出并**不**打时间戳，同时把新旧结果的 diff 打印出来供人工决定是否更新 `summary`。

#### wiki `links[]`

`created_at`：链接第一次出现在 `links[]` 数组中的时间，由编辑脚本在追加该条目时打戳。

`verified_at`：一条 link 描述的是 `from` 页面和 `to` 页面之间"关系仍然成立"，这个关系的正确性依赖于两端页面内容本身没有漂移。因此设计一条**派生约束而非独立获取动作**：

```
link.verified_at 只能被设置为 min(from_page.verified_at, to_page.verified_at)
（若任一端为 null，则 link.verified_at 也必须为 null）
```

即链接的可信度永远不能超过它连接的两个页面里更旧/更未核实的那一个。这条约束由写入脚本（`wiki_edit_guard.py`，见 2.2.2）强制执行，不接受手工覆盖。

#### 知识图谱节点（`build_knowledge_graph.py`）

按节点类型逐一给策略（统一放进第 5 节的 `timestamps` 子对象里，这里先定获取来源）：

| 节点类型 | `content_at` 来源 | `source_mtime` 来源 | `observed_at` 来源 |
|---|---|---|---|
| `session` | `entry.get("updated_at")`，**必须先过 `parse_timestamp` 规范化**（当前是未校验透传） | 该 session 索引产物文件的 `source_mtime_ns`（`sources["sessions"]` 记录） | 本次构建统一的 `generated_at`（见 5.2 的状态修正） |
| `pr` | `pr.get("updated_at")`，同样必须过 `parse_timestamp` | `sources["github"]` 的 `source_mtime_ns` | 同上 |
| `reviewed_knowledge` | `payload.get("authority_signed_at")`——**注意**：这个字段是当前 manifest schema_version=2 遗留概念，v3/v4 的真实 manifest 结构里已经没有 `authority_signed_at`/`signed_by`（见 2.2.3）；本设计以 `payload.get("authority_signed_at")` 为**兼容读取**（存在则用，不存在则为 `null`），不假定它一定存在 | `sources["reviewed_manifest"]` 的 `source_mtime_ns` | 同上 |
| `wiki`（GitHub 与 local 两种 scope） | **无自然事件时间，设为 `null`**；`add_github`/`add_local_wiki` 现在根本没读取这个字段，宁可显式 `null` 也不要编造 | local wiki 取 `sources["wiki"]` 的 `source_mtime_ns`；GitHub wiki 若 API payload 本身带时间字段则用它（需要 `index_github.py` 先采集，不在本次范围，先留 `null`） | 同上 |
| `project` | `null`（静态标识节点） | `null` | 同上 |
| `pane` / `terminal` | `null`（现有 `--processes` 输入不带任何时间字段） | 若来源快照文件本身有 mtime（此时允许两者相等，不视为冗余） | 同上 |
| `agent_process` | `null`（`etime` 是持续时长不是绝对时间，不能拿来冒充 `content_at`） | 同上 | 同上 |
| `capability` | `null`（静态审阅目录条目） | 能力目录 JSON 文件的 mtime | 同上 |
| `code_graph` | `null` | `graph_path` 指向的 Graphify 产物文件 mtime | 同上 |
| `route` / `skill` | `null` | `skill` 节点有 `path`/`sha256`，取该 `path` 的 mtime；`route` 节点取路由目录 JSON 文件 mtime | 同上 |

**边（edges）不加时间戳字段**——这是一个明确决策而非遗漏，理由见第 5.3 节。

### 1.4 是否需要区分这几种时间——结论，以及 `created_at` 的一个已知局限性

**需要，且必须四个字段都落地，不能只加一个笼统字段。** `resource-gate` 页面如果只有一个 `updated_at`，读者会以为它是新的；但真正需要的信息是"这条描述自 `agent_capacity.py` 改动后从未被人核实过"——这个信息只有 `verified_at=null` + `source_mtime` 晚于 `updated_at` 才能被结构化地表达出来。

**新增局限性说明（对应对抗性复核发现 8，不是折中而是明确记录一个已知代价）**：迁移执行时，13 个未跟踪文件页面的 `created_at` 会被统一写成同一个迁移执行时刻（见第 3 节）。这意味着**这 13 个页面的 `created_at` 之间不携带任何相对新旧信息**——它们在 `created_at` 这一维度上"看起来一样新"，即便这些条目实际是在项目历史的不同阶段被陆续加入 wiki 目录的。这不是伪造（因为"目录化时间"本身的定义就诚实地限定了它的含义），但如果不做任何提示，读者/下游 UI 很容易把"created_at 相同"误读成"这些内容同等新鲜"，这与本设计对 `verified_at` 保持警惕的精神是同一类风险，必须同样正视。缓解措施：

- 迁移脚本额外记录一个 `migration_sequence` 整数字段（迁移时 `pages[]` 数组中的原始顺序下标，从 0 开始），不携带真实时间信息，但保留了"哪一条在原始文件里排得更靠前"这一相对顺序，供人工后续排查历史时作为弱信号参考。
- wiki schema 文档（第 5.4 节的 diff）里必须显式加一句字段说明："13 个迁移生成的 `created_at` 共享同一个迁移日时间戳，不代表相对新旧关系；需要相对新旧信息时改看 `migration_sequence` 或直接查 git blame 里编辑该 JSON 文件的历史提交。"

---

## 2. 新鲜度校验机制与现有 hash-pinning 的结合

### 2.1 不能做的事

- **不能**用时间戳（哪怕是 `verified_at`）替代或弱化 `verify_reviewed_pack` 现有的哈希相等判断。哈希判断是 fail-closed 的，是对的，必须原样保留。
- **不能**引入基于墙钟的 TTL（"距上次签名不超过 N 小时就算新鲜"）。TTL 短了会对合法的慢变更 wiki 频繁误报；TTL 长了会在窗口期内悄悄放行一个已经被篡改/过期的 wiki，比现状更差。**本设计不采用任何 TTL 方案。**

### 2.2 采用的方案：内容版本号绑定 + 编辑时写屏障（双层防御，两层都必须在本轮交付）

**关于交付边界的一个重要修正**：候选设计原先把 `wiki_edit_guard.py` 和 `check_wiki_freshness.py` 都标注为"设计层描述，不在本次实现"，只把 §2.2.4 的校验逻辑扩展列为"本次可实现"。对抗性复核指出这样切分之后，本轮**真正能落地的部分对 Audit 3 的原始事故没有任何改善**：`content_version` 在 session 启动时和现有 sha256 检查是同一时刻生效、信息冗余的（wiki 内容变了没重新钉哈希，sha256 本来就已经会不匹配、已经会 fail-closed，这是 Audit 3 认可为"正确"的既有机制）；`content_version` 唯一的新增价值发生在**编辑时**，而编辑时的检查点正是 `wiki_edit_guard.py`。如果这个脚本不在本轮交付，那么本轮所做的一切都只是给已经存在的检测点多加了一个字段，"10 小时缺口"这个事故本身完全没有被缩短。

因此终稿把交付范围调整为：

- **第一层——检测层（`wiki_edit_guard.py`，写屏障，事前，本轮必须交付）**：让"编辑 wiki 内容"这个动作本身强制要求编辑者同步递增一个版本计数器；如果版本计数器没有递增但内容语义变了，编辑脚本直接拒绝写入。这一层的目的是让"忘记重新签名"这件事**在编辑发生的那一刻**就被发现，而不是等到下一次 session 启动才发现——这是唯一真正缩短 Audit 3 缺口的机制，不能再被列为"本次不实现"。
- **第二层——验证层（会话启动时校验 + 独立 CLI，`check_wiki_freshness.py`，事后兜底，本轮必须交付）**：`verify_reviewed_pack` 除了比对哈希，再多比对一个内容版本号；哈希和版本号任一不匹配都 fail-closed。这一层是对第一层失效（比如有人绕过编辑脚本直接改了 JSON）的兜底，也是 CI/人工随时可调用的独立检查点。

#### 2.2.1 wiki JSON 增加 `meta` 子对象

```json
{
  "version": 1,
  "meta": {
    "content_version": 3,
    "updated_at": "2026-08-20T10:00:00+00:00"
  },
  "project": "...",
  "pages": [...],
  "links": [...]
}
```

`meta.content_version` 是一个**单调递增整数**，代表这份 wiki JSON 内容被有意义编辑过多少次（不是文件被 touch 多少次）。规则：

- 任何脚本/人工流程，只要对 `pages[]`/`links[]` 里任意一个条目的实质内容（`title`/`summary`/`status`/`path`/`relation`，不含时间戳字段本身）做出改动，就**必须**把 `meta.content_version` 加一、把 `meta.updated_at` 更新为当前时间，两者在同一次写入中原子完成。
- 只改时间戳字段本身（比如跑核实脚本只更新某页的 `verified_at`）**不**触发 `content_version` 递增——否则 `content_version` 会被核实动作污染，变成没意义的计数器。

#### 2.2.2 写屏障脚本 `wiki_edit_guard.py`（本轮交付范围，函数级契约）

任何编辑 `wiki/orca-context-wiki.json` 的路径（无论是人工还是脚本）都必须经过这个守卫，而不是直接写文件。具体契约：

```python
def guard_wiki_write(old_payload: dict, new_payload: dict) -> None:
    """
    old_payload: 磁盘上当前的 wiki JSON（已解析）
    new_payload: 调用方准备写入的新内容（已解析）
    成功：无返回值（表示允许写入）
    失败：抛出 ValueError，附带人类可读原因，调用方不得吞掉这个异常后继续写盘
    """
    old_meta = old_payload.get("meta") or {}
    new_meta = new_payload.get("meta") or {}
    old_version = old_meta.get("content_version")
    new_version = new_meta.get("content_version")
    if not isinstance(old_version, int) or isinstance(old_version, bool):
        raise ValueError("old wiki content_version missing or invalid — refuse to guess a baseline")
    if not isinstance(new_version, int) or isinstance(new_version, bool):
        raise ValueError("new wiki content_version missing or invalid")

    semantic_changed = compute_semantic_diff(old_payload, new_payload)  # 忽略 created_at/updated_at/
                                                                         # verified_at/verification_status/
                                                                         # source_mtime/migration_sequence
                                                                         # 等纯时间戳/派生字段的变化

    if semantic_changed:
        if new_version != old_version + 1:
            raise ValueError(
                f"semantic content changed but content_version did not advance by exactly 1 "
                f"(old={old_version}, new={new_version})"
            )
        old_updated_at = parse_timestamp(old_meta.get("updated_at"))
        new_updated_at = parse_timestamp(new_meta.get("updated_at"))
        if old_updated_at is None or new_updated_at is None or new_updated_at <= old_updated_at:
            raise ValueError("meta.updated_at must be a valid timestamp strictly newer than the prior value")
    else:
        if new_version != old_version:
            raise ValueError(
                f"only timestamp/verification fields changed but content_version moved "
                f"(old={old_version}, new={new_version}) — verification passes must not bump content_version"
            )

    # 附加约束：link.verified_at 的 min() 派生规则（1.3 节）在这里一并强制校验
    validate_link_verified_at_derivation(new_payload)
```

调用流程：

```
步骤 1：读取当前磁盘上的 wiki JSON，记为 old
步骤 2：接受调用方提交的新内容，记为 new
步骤 3：调用 guard_wiki_write(old, new)；异常直接向上抛出，不落盘
步骤 4：断言通过后，原子写入（沿用仓库既有 write_private 的 temp-file + os.replace 模式）
步骤 5：若 semantic_changed 为真，写入成功后打印明确提示：
    "wiki content_version 已从 N 升至 N+1，central reviewed manifest 需要重新对齐
     shared_source_sha256s.wiki，请运行 check_wiki_freshness.py 确认当前是否已失配，
     并按仓库既有的 manifest 重新生成流程处理（见 2.2.3 对当前 manifest 版本落差的说明，
     该流程本身不在本设计范围内新造，只是消费既有机制）。"
    并以非零退出码结束（如果被当作 pre-commit hook 调用，这会阻止 commit 落地，
    除非调用方明确加 --acknowledge-resign-pending 之类的显式确认标志）
```

**这个脚本只做检测和阻断，不做自动重签**——重签必须走仓库既有的 manifest 生成/签名机制，这是安全权威机制，不能被写屏障脚本绕过（否则写屏障本身就变成了一个可以自我签发信任的后门，比现状更危险）。**本设计不新造一个重签工具**，因为第 2.2.3 节已经确认当前仓库里"重签"这一步的既有实现路径本身存在落差（`sign_reviewed_authority.py` 是未接线的原型脚本，见下），在那个落差被独立处理之前，写屏障能做的只是**阻断并提示**，不能假装能调用一个目前并不可靠的重签流程。

#### 2.2.3 manifest 侧改动——先厘清当前版本落差，再给出面向真实 v3/v4 结构的字段扩展

**必须先核实的既有状态（这是一个先于本设计存在的问题，不是本设计制造的）**：

- 仓库里**实际生效**的 `.orca/context/reviewed-startup-pack-manifest.json` 当前 `schema_version` 为 `2`，字段集合为 `{schema_version, authority, authority_git, authority_signed_at, signed_by, signature_nonce, pack, project_git, shared_source_policy, shared_source_sha256s}`。
- 但 `build_startup_bundle.py` 当前代码（`REVIEWED_PACK_MANIFEST_SCHEMA_VERSIONS = {3, 4}`，`verify_reviewed_pack` 第 1357 行的严格字段集合断言 `set(payload) != {"schema_version","authority","pack","shared_source_sha256s","shared_source_policy","project_git","content_source_closure"}`）**只接受 3/4**，且严格字段集合里已经没有 `authority_git`/`authority_signed_at`/`signed_by`/`signature_nonce`，取而代之的是 `content_source_closure`（基于 git 内容闭包的绑定，而不是人工签名时间戳）。
- 也就是说，**当前磁盘上的这份 manifest 用现在的代码校验会直接因为字段集合不匹配而失败**——这是一个独立于本设计存在的既有落差，本设计不负责、也不能顺手把它修好（那属于另一轮变更，且大概率触及"安全/发布/最终验收"红线，需要独立走双模型复核）。
- 唯一在仓库里存在的 `sign_reviewed_authority.py`（`orca-context-bridge/reviewed-pack-multi-project-fix/sign_reviewed_authority.py`）在文件头明确写着"这个工具不会被 SessionStart hook 自动调用""这些依赖本应从 build_startup_bundle 导入，这里假设它们可用或给出最小实现"——即它是一个**未接线的原型/占位脚本**，不能被当作"已确立、必须保留其约束"的生产级不变式来对待。候选设计里反复引用的"非自签、≥300 秒陈化"约束，需要先确认这套约束在 v3/v4 实际生效的签发路径里是否仍然存在、由哪个脚本真正执行，再决定是否继续作为本设计的强约束——**本设计不擅自断言它仍然生效，也不擅自断言它已经失效**，把这一条列为第 6 节风险 5，要求作为独立前置任务确认。

**基于以上事实，本设计对 manifest 的字段扩展只针对 v3/v4 的真实形状，不再使用 v2 遗留的签名字段框架**：`shared_source_sha256s.wiki` 从纯字符串升级为对象，其余 `capabilities`/`graphify_catalog`（v4 下还有 `routes`）暂不需要这个升级：

```json
"shared_source_sha256s": {
  "wiki": {
    "sha256": "538fd671d651a9381d1c7e18920ddfd631ce1cfae38cb69a431116dee7f17f1d",
    "content_version": 3,
    "pinned_at": "2026-08-20T10:05:00+00:00"
  },
  "capabilities": "13758f19...fae59",
  "graphify_catalog": "7454e66b...09ef"
}
```

**前置条件（必须先满足，才能落地这个 diff）**：当前生效的 manifest 必须先被独立地重新生成为 schema_version 3 或 4（这是第 6 节风险 5 描述的独立前置任务），本设计的 `wiki` 字段对象化只在那之后叠加，不能在一份本来就已经无法通过校验的 v2 manifest 上叠加新字段——那样做出来的东西两次都通不过校验，无法验证本设计本身是否正确。

#### 2.2.4 `verify_reviewed_pack` 校验逻辑——对照真实代码位置的具体 diff（不是独立函数）

对抗性复核指出，真正的每源校验循环在 `orca-context-bridge/scripts/build_startup_bundle.py:1416-1447`，不是一个独立的 `verify_source_binding(name, path, expected)` 函数；而且该循环第 1443 行 `if expected is not None and not valid_sha256(expected): raise ValueError(...)` 会在 `expected` 是字典时**直接报错拒绝**（`valid_sha256` 先判断 `isinstance(value, str)`），根本走不到后面比较 `observed != expected` 的分支——如果按候选设计的独立函数原样实现，一旦部署，**任何 session 启动都会在这一行直接失败**，而不是像设计描述的那样进入 isinstance 判断分支。终稿改为直接给出该循环本身的 diff：

```diff
     for name in reviewed_shared_sources:
         if name not in expected_sources:
             raise ValueError(f"central reviewed source hash missing: {name}")
         policy = source_policy.get(name)
         if not isinstance(policy, dict) or set(policy) != {"required", "reason"}:
             raise ValueError(f"central reviewed source policy invalid: {name}")
         required = policy.get("required")
         reason = policy.get("reason")
         if not isinstance(required, bool):
             raise ValueError(f"central reviewed source policy invalid: {name}")
         if required:
             if reason is not None:
                 raise ValueError(f"central reviewed required source reason must be null: {name}")
         elif not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
             raise ValueError(f"central reviewed optional source reason invalid: {name}")
         source_path = shared_source_paths.get(name)
         if source_path is None:
             raise ValueError(f"central reviewed source unavailable: {name}")
         checked_source, source_error = graphify_path_allowed(str(source_path.absolute()), root)
         if source_error or checked_source is None:
             raise ValueError(f"central reviewed source rejected: {name}")
         observed_file = stable_regular_file_sha256(checked_source, MAX_GRAPHIFY_CATALOG_BYTES)
         observed = observed_file[0] if observed_file is not None else None
         expected = expected_sources.get(name)
         if required and (expected is None or observed is None):
             raise ValueError(f"central reviewed required source unavailable: {name}")
-        if expected is not None and not valid_sha256(expected):
-            raise ValueError(f"central reviewed source hash invalid: {name}")
-        if observed != expected:
-            raise ValueError(f"central reviewed source freshness mismatch: {name}")
-        observed_sources[name] = observed
+        expected_content_version = None
+        if isinstance(expected, dict):
+            # 新格式（目前仅 wiki）：{"sha256":..., "content_version":..., "pinned_at":...}
+            expected_hash = expected.get("sha256")
+            expected_content_version = expected.get("content_version")
+            if not valid_sha256(expected_hash):
+                raise ValueError(f"central reviewed source hash invalid: {name}")
+            if not isinstance(expected_content_version, int) or isinstance(expected_content_version, bool):
+                raise ValueError(f"central reviewed source content_version invalid: {name}")
+        elif expected is not None and not valid_sha256(expected):
+            raise ValueError(f"central reviewed source hash invalid: {name}")
+        else:
+            expected_hash = expected
+        if observed != expected_hash:
+            raise ValueError(f"central reviewed source freshness mismatch: {name} (hash)")
+        if expected_content_version is not None:
+            observed_payload_for_version = strict_json_object(
+                read_stable_regular_file(checked_source, MAX_GRAPHIFY_CATALOG_BYTES, f"central reviewed source: {name}"),
+                f"central reviewed source: {name}",
+            )
+            observed_content_version = (observed_payload_for_version.get("meta") or {}).get("content_version")
+            # 缺失/None 一律视为不匹配，不允许"缺失就放行"
+            if (
+                not isinstance(observed_content_version, int)
+                or isinstance(observed_content_version, bool)
+                or observed_content_version != expected_content_version
+            ):
+                raise ValueError(
+                    f"central reviewed source freshness mismatch: {name} "
+                    f"(content_version observed={observed_content_version!r} expected={expected_content_version!r})"
+                )
+        observed_sources[name] = observed
```

关键设计点：

- **哈希不匹配和版本号不匹配都必须 fail-closed**，报错信息分别注明是哪一种不匹配。
- **`content_version` 缺失/类型不对被当作不匹配处理，不是当作"跳过检查"处理**——布尔值会被 `isinstance(x, int)` 判定为 `True`（Python 里 `bool` 是 `int` 子类），因此显式加了 `isinstance(x, bool)` 排除，防止 `content_version: true` 这种畸形值被静默接受为"整数 1"。
- **旧格式（str）和新格式（dict）必须并存**：`isinstance` 分支判断是简单且明确的兼容方式，`capabilities`/`graphify_catalog` 保持字符串形式不受影响。
- **重新读取一遍 wiki 源文件来取 `content_version`**：这是必要的额外一次 `read_stable_regular_file` 调用（因为 `stable_regular_file_sha256` 只返回哈希，不返回可解析的 JSON payload），复用已有的 `strict_json_object` 校验入口，不新增一条平行的、未经边界检查的解析路径。

#### 2.2.5 CI/周期性检查（`check_wiki_freshness.py`，本轮交付）

除了写屏障和 session 启动校验，额外提供一个可以随时手动/CI 调用的独立命令，复用 2.2.4 同一段校验逻辑（直接调用 `verify_reviewed_pack` 或抽出其中每源校验的公共部分供两处复用），让"wiki 改了但没重签"这件事不必等到下一次 agent session 启动才被发现。

---

## 3. 存量条目迁移策略

### 3.1 结论：`source_mtime` 回填真实值，`verified_at` 一律标记为"未核实"，`created_at` 用目录化时间并附带顺序信息，不伪造

**`source_mtime`：回填真实值。** 这是纯粹的文件系统事实，不涉及任何"这个内容对不对"的判断。所有 14 个页面直接用 `os.stat(path).st_mtime` 回填即可；若未来出现某条目 `path` 已不存在的情形，`source_mtime` 置 `null` 并追加 `source_missing: true`。

**`created_at`：能拿到 git 历史的用 git，拿不到的记录"写入 wiki 目录那一刻"，并同时记录 `migration_sequence` 保留相对顺序信号。** 13/14 个文件在 git 里没有任何提交记录，无法得知它们"真正"是什么时候创建的；与其编一个基于 mtime 的假创建时间，不如诚实记录"这条 wiki 记录本身是什么时候第一次被目录化的"。但如 1.4 节所述，这会让 13 个页面在 `created_at` 上"看起来一样新"，因此迁移时额外写入 `migration_sequence`（迁移时该页面在原始 `pages[]` 数组里的下标），供人工排查历史相对顺序时使用，并在 schema 文档里显式声明这条限制。

**`verified_at`：一律设为 `null`，并新增 `verification_status: "unverified"`，绝不用 mtime 或迁移执行时间冒充。** 迁移执行的这一刻，`resource-gate`、`capacity-preflight`、`orca-capability-tests`、`orca-readonly-probe`、`orca-cli-capability-catalog` 五个页面已经被证明内容与现实不符。如果迁移脚本图省事把"迁移执行时间"填进 `verified_at`，就等于对外宣称"这些错误的内容在今天被核实过"——这比完全没有 `verified_at` 字段更糟，因为它会被下游消费者（包括第 2 节设计的自动化校验、包括未来读这份 wiki 的 agent）当作真实的核实记录来信任。**没有核实的字段就应该诚实地是 `null`，宁可信息不全，不可信息虚假。**

### 3.2 迁移脚本行为（设计层规格）

```
对 wiki/orca-context-wiki.json 中每一个 page（按原始数组顺序，下标 0..n-1）：
    若 path 指向的文件存在：
        source_mtime ← os.stat(page.path).st_mtime，转 ISO8601
        source_missing ← false
    否则：
        source_mtime ← null
        source_missing ← true
    若 path 在 git 中有提交历史：
        created_at ← git log 首次提交时间
        updated_at ← git log 最近提交时间
    否则：
        created_at ← 本次迁移执行时刻（记录清楚这是"目录化时间"不是"源文件创建时间"）
        updated_at ← 本次迁移执行时刻
    migration_sequence ← 该 page 在原始数组中的下标（迁移前的顺序，保留相对信号，不代表时间）
    verified_at ← null
    verification_status ← "unverified"

对每一条 link：
    created_at ← 本次迁移执行时刻（link 本身也没有历史可考）
    verified_at ← null（依据 1.3 节的 min() 约束，两端页面都是 null，link 也必然是 null）

写入顶层 meta：
    content_version ← 1（迁移本身视为一次内容确定的起点）
    updated_at ← 本次迁移执行时刻

迁移脚本本身必须经过 2.2.2 的 guard_wiki_write 校验后再落盘
（迁移是一次"语义未变但结构变了"的特殊写入：旧文件没有 meta.content_version，
 guard 对"old_version 缺失"的情形要单独放行——仅在 old_payload 完全不含 meta 键时，
 允许 new_payload.meta.content_version 从 1 开始，这是本规则唯一的一次性例外，
 迁移完成后不再适用）
```

### 3.3 迁移之后的必要后续动作（不是可选项）

迁移只是让 schema 完整，**不代表内容变准确**。已知有 5-6 个页面内容本身就是错的（不是"缺时间戳"，是"写的东西不对"）。这些必须作为迁移之后**立刻排期**的独立任务去做真正的内容修正 + 人工核实（把 `verification_status` 从 `unverified` 提升为 `verified` 并填入真实 `verified_at`），具体列在第 4.2 节。**不能让这批已知错误的页面停留在"结构完整但内容仍然是错的"这个状态太久**，否则时间戳字段本身反而会造成"这个 schema 看起来很完善"的假象，掩盖内容问题没解决的事实。

---

## 4. Wiki 完善路线

### 4.0 支撑本节优先级判断的审计结论（内联自证）

原候选设计大量引用"Audit 1 §2/§3""Audit 4 §2"等未提交到仓库的外部文件作为优先级依据，导致这份设计本身在仓库里不是自洽、可独立审计的。终稿把这些结论直接内联陈述如下（后续应将 Audit 1–4 完整报告一并提交到仓库归档，见第 6 节风险 8）：

- `resource-gate`/`capacity-preflight` 两个页面描述的"红灯不启动"强制行为，与 `agent_capacity.py` 在提交 `76e7d88ccb` 之后的实际输出结构（顶层 `recommendation` 固定值、真实红黄绿计算被降级为 `advisory_true_recommendation`）不一致，且与用户全局 CLAUDE.md 规则的措辞存在已知冲突。
- `orca-capability-tests`/`orca-readonly-probe`/`orca-cli-capability-catalog` 三个页面里各自写死的通过率数字（如"16/18""235 个命令入口"）互相之间及与最新一次探针实际运行结果之间存在不一致，且这些数字在页面里都没有关联"是哪一次运行产生的"这个上下文。
- `desktop-mcp-realtime-control` 页面引用的 benchmark 数据实际来自 `wiki/Orca能力测试与知识图谱.md`，而不是该页面 `path` 字段指向的 `desktop-mcp/README.md` 自身，两者数据来源被张冠李戴。
- wiki 当前只覆盖仓库的"测试/验证层"（`orca-capability-tests`、`orca-readonly-probe` 等），完全未覆盖实际工程交付物：memory bridge 三件套、Prime Agent 集成、SSD/R2 备份归档退役闭环家族、启动安装器/加载器/事务闭环家族、编排动态调度器、Paperclip 集成、`review-orca-workflow-learning` 元技能、以及贯穿所有 closure 目录的验收生命周期词汇表——这些均已独立核实为仓库中真实存在的目录/文件（见 4.1 各条目对应路径）。
- `orchestration-lifecycle`/`resource-gate` 两个现有页面与新提议的 `orchestration-dynamic-scheduler` 页面描述的是同一层面的调度/门限问题，目前互相之间没有 wiki 内部链接，读者无法从任一条目发现另一条目的存在。

### 4.1 新增页面清单（按优先级排序，14 条，≤15 条要求内）

**P0：信任根，必须最先补齐**

1. `id: startup-reviewed-authority-pipeline`
   `title: Startup 可信上下文交付管线`
   `summary: 描述 startup_context.py + install_startup_injection.py + .orca/context/reviewed-startup-pack-manifest.json 组成的会话启动可信交付链路：ORCA_CONTEXT_NACK_V1 challenge-response、schema v3/v4 必需源集合、shared_source_sha256s 内容哈希钉住机制。写作时必须明确标注：本页描述的是 verify_reviewed_pack 当前代码要求的 v3/v4 形状，而磁盘上生效的 manifest 目前是 v2（见本设计 2.2.3 节的风险记录），两者的落差需要读者知悉，不能在 wiki 里含糊带过。`

2. `id: reviewed-authority-signing`
   `title: 可信内容签名与重新钉哈希流程`
   `summary: 记录 manifest 重新生成/重新钉哈希的实际操作路径与当前已知缺口——仓库里可见的 sign_reviewed_authority.py 是未接线的原型脚本，不能等同于生产级签发工具；本页作为 2.2.3 节风险 5 的可执行结论落点，一旦该缺口被独立处理完，页面需要同步更新为准确描述。`

3. `id: context-bridge-pipeline-overview`
   `title: orca-context-bridge 核心管线总览`
   `summary: 串联 sync_sessions.py / build_context_digest.py / index_processes.py / index_github.py / build_knowledge_graph.py / auto_index.py / install_hook.py / tmux_bridge.py 的数据流与职责边界，替代当前 wiki 只描述侧路探针却遗漏主管线本体的现状。`

**P1：仓库里工作量/评审量最大的两个子系统**

4. `id: claude-codex-memory-bridge`
   `title: Claude-Codex 记忆桥接`
   `summary: install_bridge.py / claude_memory_hook.py / write_candidate_capture.py 三件套构成的跨模型写触发自动学习管线；经过多轮 opus+sol 双 review 定案并已实际安装生效。`

5. `id: prime-agent-integration`
   `title: Prime Agent 集成与安全加固`
   `summary: install_prime_agent.py 的多轮安全评审，含一次被连续多轮定位并关闭的真实 RCE 链条，最终双模型双 GO，但因上游依赖漂移目前仍未安装（阻塞态）。`

6. `id: route-catalog-context-route`
   `title: 能力路由目录 (context_route.py)`
   `summary: capability→project 精确哈希绑定解析器：source_drift 检测到不一致即 fail-closed、区分候选与已审阅两种查询模式；是路由安全的关键守门组件，目前 wiki 完全未提及。`

7. `id: completion-backlog-master-index`
   `title: COMPLETION-BACKLOG 主索引`
   `summary: 指向 reports/COMPLETION-BACKLOG-2026-08-16.md 的九大编号领域目录，作为 wiki 与仓库现有全局索引之间的显式交叉链接锚点，避免两套索引长期互不知情。`

**P2：几个规模相当的领域家族**

8. `id: ssd-hardening-closure-family`
   `title: SSD 原生存储加固闭环`
   `summary: ssd-native-storage-closure / ssd-runtime-closure / ssd-pty-lifetime-closure / ssd-runtime-remote-policy-independent-acceptance 四个目录构成的多轮加固工作，覆盖原生存储、PTY 生命周期与运行时策略的 SSD 安全性。`

9. `id: r2-archive-retirement-family`
   `title: R2 备份与归档/Android 退役闭环`
   `summary: r2-run-identity-closure / r2-production-trust-closure / r2-public-only-acceptance / archive-provenance-normalization-closure / android-retained-retirement-closure；状态均为待验收候选、尚未安装的备份与退役领域。`

10. `id: startup-installer-loader-family`
    `title: 启动安装器/加载器/事务闭环家族`
    `summary: startup-installer-closure / startup-loader-closure / startup-transaction-closure(+v2) / startup-p1-hardlink-closure / startup-permission-remediation-closure / startup-reviewed-pack-closure(+schema3-fastfix)；产出 reviewed-startup-pack-manifest.json 本身的底层写入机制，是页面 1 描述的管线的实现基础，也是页面 1/2 标注的 schema 版本落差的实现层出处。`

11. `id: orchestration-dynamic-scheduler`
    `title: 编排动态调度器`
    `summary: scheduler.py + ORCA-NATIVE-ARCHITECTURE.md：账号绑定、模型路由、与容量门限叠加的调度策略层；需与既有 orchestration-lifecycle / resource-gate 页面互链（见 4.2 表格的对应更新项），目前两者互不知道对方存在。`

12. `id: paperclip-integration-family`
    `title: Paperclip 第三方集成闭环`
    `summary: paperclip-orca-closure / paperclip-privacy-gate-closure / paperclip-skill-candidate / paperclip-orca-independent-acceptance；隐私门控与独立验收覆盖的第三方集成域。`

13. `id: review-orca-workflow-learning-skill`
    `title: review-orca-workflow-learning 元技能`
    `summary: skilld.py / event_journal.py / telemetry_projection.py / memory_eval.py / startup_admission.py / policy_gate.py / attestation_verify.py / method_ledger.py / release_identity.py 等组成的独立学习元技能，与 orca-context-bridge 并列存在，目前 wiki 从未提及。`

14. `id: dual-review-acceptance-protocol`
    `title: 双模型复核与验收协议词汇表`
    `summary: 候选态/NO-GO/勘误/轮次编号约定等贯穿几乎所有 closure 目录的验收生命周期词汇，首次作为一等概念被独立文档化，而不是仅在其他页面里顺带提及。`

### 4.2 现有页面需要更新的清单

| id | 需要的更新 | 依据 |
|---|---|---|
| `resource-gate` | 移除"红灯不启动新重型 worker"的强制性表述，改为准确描述 `agent_capacity.py` `76e7d88ccb` 之后的 `gate_removed` 实际行为，并显式标注这与用户全局 CLAUDE.md 规则的措辞存在已知冲突（不要试图在 wiki 里悄悄"修正"用户规则，只需如实标注冲突，交给人决定） | 4.0 节内联结论第 1 条 |
| `capacity-preflight` | 同上，精确描述当前输出结构（顶层 `recommendation` 固定值 + 被降级到 `advisory_true_recommendation` 的真实红黄绿计算） | 4.0 节内联结论第 1 条 |
| `orca-capability-tests` | 把写死的证据数字改为不再硬编码，必须与产生该数字的 `verified_at` 绑定出现（依据 1.3 节的规则） | 4.0 节内联结论第 2 条 |
| `orca-readonly-probe` | 同上，更新为最新探针结果并附 `verified_at` | 4.0 节内联结论第 2 条 |
| `orca-cli-capability-catalog` | 说明其快照已过期且与另外两处结果不一致，需要重新跑一次生成脚本后再更新 | 4.0 节内联结论第 2 条 |
| `desktop-mcp-realtime-control` | 修正 benchmark 数据的归因——该数字实际来自 `wiki/Orca能力测试与知识图谱.md` 而非页面自身 `path` 指向的 `desktop-mcp/README.md`，需要在 summary 里明确标注数据来源页面，或迁移数据到正确文件 | 4.0 节内联结论第 3 条 |
| `orchestration-lifecycle` / `resource-gate` | 各增加一条到新页面 `orchestration-dynamic-scheduler` 的 link（relation 建议为 `overlaps_with`） | 4.0 节内联结论第 5 条 |
| `ego-capability-regression` | 标记 `verification_status: unverified`（而非直接判定为过时或直接判定为仍然正确），交给下一轮人工核实 | 4.0 节内联结论第 4 条 |

---

## 5. 知识图谱 schema 调整

### 5.1 是否需要统一 `timestamps` 子对象——需要，理由

7 个节点类型目前完全没有时间信息，另外 3 个节点类型有时间信息但格式不统一、未校验。与其在每个节点类型上各自加不同名字的字段，不如**统一成一个 `timestamps` 子对象**，字段名固定为 `content_at`/`source_mtime`/`observed_at`（语义见 1.3 节），所有节点类型都有这三个键，值可以是 `null` 但键必须存在。

### 5.2 具体 diff（对照 `build_knowledge_graph.py` 真实代码逐处核实）

**状态修正 1：`generated_at` 必须在构建开始前就确定，并传入 `GraphBuilder`**

真实代码里 `GraphBuilder.__init__`（`build_knowledge_graph.py:174-184`）不持有 `generated_at`；`generated_at` 目前只在 `run_build()` 里、`build_graph()` 返回**之后**才作为局部变量算出（第 1087 行），这意味着所有节点构建期间根本访问不到它。终稿改为在 `run_build()` 最开始就计算一次，同时传给 `GraphBuilder` 和最终输出的顶层字段，保证同一次运行里所有 `observed_at` 与顶层 `generated_at` 完全一致：

```diff
 class GraphBuilder:
-    def __init__(self, home: Path) -> None:
+    def __init__(self, home: Path, generated_at: str) -> None:
         self.home = home
+        self.generated_at = generated_at
         self.nodes: dict[str, dict[str, Any]] = {}
         self.edges: list[dict[str, str]] = []
         ...
```

```diff
 def build_graph(
     home: Path,
+    generated_at: str,
     sessions_payload: dict[str, Any] | None,
     processes_payload: dict[str, Any] | None,
     github_payload: dict[str, Any] | None,
     local_wiki_payload: dict[str, Any] | None = None,
     capabilities_payload: dict[str, Any] | None = None,
     graphify_payload: dict[str, Any] | None = None,
     reviewed_manifest_payload: dict[str, Any] | None = None,
     routes_payload: dict[str, Any] | None = None,
     sources: dict[str, dict[str, Any]] | None = None,
 ) -> GraphBuilder:
-    builder = GraphBuilder(home)
-    builder.add_sessions(sessions_payload)
+    builder = GraphBuilder(home, generated_at)
+    builder.add_sessions(sessions_payload, sources or {})
     builder.add_processes(processes_payload)
-    builder.add_github(github_payload)
+    builder.add_github(github_payload, sources or {})
     builder.add_graphify(graphify_payload)
     builder.add_capabilities(capabilities_payload)
     builder.add_local_wiki(local_wiki_payload)
     builder.add_reviewed_manifest(reviewed_manifest_payload, sources or {})
     builder.add_routes(routes_payload, sources or {})
     builder.add_session_pr_mentions()
     return builder
```

```diff
 def run_build(args: argparse.Namespace) -> int:
     home = Path.home()
+    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
     sessions_payload, sessions_source = load_json_source(args.sessions)
     ...
     builder = build_graph(
         home,
+        generated_at,
         sessions_payload,
         ...
         sources,
     )
     nodes = list(builder.nodes.values())
     edges = builder.edges

     graph = {
         "version": GRAPH_VERSION,
-        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
+        "generated_at": generated_at,
         "sources": sources,
         "nodes": nodes,
         "edges": edges,
     }
```

**状态修正 2：`add_node` 的真实签名与存储结构**

真实代码是 `def add_node(self, node_id: str, node_type: str, label: str, meta: dict[str, Any]) -> None`，且 `self.nodes` 是**以 `node_id` 为键的字典**（`self.nodes[node_id] = {...}`），不是列表（`run_build` 里用 `list(builder.nodes.values())` 转成列表才写进最终 JSON）。diff 如下：

```diff
     def add_node(self, node_id: str, node_type: str, label: str, meta: dict[str, Any]) -> None:
         if node_id in self.nodes:
             return
-        self.nodes[node_id] = {"id": node_id, "type": node_type, "label": label, "meta": meta}
+        self.nodes[node_id] = {
+            "id": node_id,
+            "type": node_type,
+            "label": label,
+            "meta": meta,
+            "timestamps": timestamps if timestamps is not None else make_timestamps(observed_at=self.generated_at),
+        }
```

对应的签名也要加一个可选参数：

```diff
-    def add_node(self, node_id: str, node_type: str, label: str, meta: dict[str, Any]) -> None:
+    def add_node(
+        self,
+        node_id: str,
+        node_type: str,
+        label: str,
+        meta: dict[str, Any],
+        timestamps: dict[str, Any] | None = None,
+    ) -> None:
```

**新增辅助函数（放在 `add_node`/`add_edge` 附近），已修正 `parse_timestamp` 从不抛异常这一事实错误**：

```diff
+def make_timestamps(
+    content_at: Any = None,
+    source_mtime_ns: int | None = None,
+    observed_at: str | None = None,
+) -> dict[str, Any]:
+    """构建标准 timestamps 子对象。content_at 若非空须先过 parse_timestamp 归一化。
+
+    注意：parse_timestamp 对无法解析的输入返回 None，从不抛异常
+    （它内部已经吞掉了自己的 ValueError）。因此这里不需要、也不能用
+    try/except 包裹 parse_timestamp(...).isoformat() 这种写法——
+    None.isoformat() 抛的是 AttributeError，不在任何"预期的解析失败"
+    异常类型里，会让整个图谱构建意外崩溃。
+    """
+    parsed = parse_timestamp(content_at) if content_at else None
+    normalized_content_at = parsed.isoformat() if parsed is not None else None
+
+    normalized_source_mtime = None
+    if source_mtime_ns:
+        normalized_source_mtime = (
+            datetime.fromtimestamp(source_mtime_ns / 1e9, tz=timezone.utc)
+            .replace(microsecond=0)
+            .isoformat()
+        )
+
+    return {
+        "content_at": normalized_content_at,
+        "source_mtime": normalized_source_mtime,
+        "observed_at": observed_at,
+    }
```

**`load_regular_source` 把已经算出来却被丢弃的 mtime 保留下来**（真实代码里 `identity()` 闭包内部已经读取了 `st_mtime_ns` 用于比对，但没有存进最终 `record` 字典，这里只是把已经拿到手的值透传出去）：

```diff
     record: dict[str, Any] = {
         "path": str(candidate),
         "sha256": hashlib.sha256(raw).hexdigest(),
         "size_bytes": len(raw),
+        "source_mtime_ns": after.st_mtime_ns,
     }
     return raw, record
```

（用 `after`——即读取后再次 `fstat` 拿到的状态——而不是 `before`，因为 `after`/`path_after` 是这段函数已经确认过身份一致性的那次快照，语义上更贴近"这份被读取的字节对应的文件系统状态"。）

**`load_json_source` 不需要改**：`source_mtime_ns` 已经通过 `record` 字典自动带过来；只需在最终写出 `sources[name]` 时把 `source_mtime_ns` 转成 ISO8601 字符串对外展示（键名从内部的 `source_mtime_ns` 重命名为对外的 `source_mtime`，这一步在 `run_build()` 序列化 `sources` 字典前统一做一次转换，不需要改 `load_json_source` 本身）。

**状态修正 3：`add_sessions`/`add_github` 需要新增 `sources` 参数**（真实代码里目前只有 `add_reviewed_manifest`/`add_routes` 接收 `sources`，`add_sessions`/`add_github` 只接收 `payload`；但 `run_build()` 里 `sessions_source`/`github_source` 在调用 `build_graph()` 之前已经算好并塞进了 `sources` 字典，所以这不是新增一次昂贵计算，只是把已有值传进去）：

```diff
-    def add_sessions(self, payload: dict[str, Any] | None) -> None:
+    def add_sessions(self, payload: dict[str, Any] | None, sources: dict[str, dict[str, Any]]) -> None:
         if payload is None:
             return
         sessions = payload.get("sessions")
         if not isinstance(sessions, list):
             return
+        session_source_mtime_ns = sources.get("sessions", {}).get("source_mtime_ns")
         for entry in sessions:
             ...
             label = redact_text(str(entry.get("title") or ""), self.home, TITLE_LIMIT) or session_id
             node_id = f"session:{session_id}"
             self.add_node(
                 node_id,
                 "session",
                 label,
                 {
                     "provider": entry.get("provider"),
                     "source": entry.get("source"),
                     "cwd": normalize_cwd(entry.get("cwd")),
-                    "updated_at": entry.get("updated_at"),
                 },
+                timestamps=make_timestamps(
+                    content_at=entry.get("updated_at"),
+                    source_mtime_ns=session_source_mtime_ns,
+                    observed_at=self.generated_at,
+                ),
             )
```

```diff
-    def add_github(self, payload: dict[str, Any] | None) -> None:
+    def add_github(self, payload: dict[str, Any] | None, sources: dict[str, dict[str, Any]]) -> None:
         if payload is None:
             return
         repo = payload.get("repo")
         repo = repo if isinstance(repo, str) and repo else None
         project_id = self.resolve_repo_project(repo) if repo else None
+        github_source_mtime_ns = sources.get("github", {}).get("source_mtime_ns")

         pr_numbers: set[int] = set()
         pr_bodies: dict[int, str] = {}
         prs = payload.get("prs")
         if isinstance(prs, list):
             for pr in prs:
                 ...
                 self.add_node(
                     node_id,
                     "pr",
                     title or f"PR #{number}",
                     {
                         "number": number,
                         "head": head,
                         "base": base,
-                        "updated_at": pr.get("updated_at"),
                         "body_preview": body_preview,
                     },
+                    timestamps=make_timestamps(
+                        content_at=pr.get("updated_at"),
+                        source_mtime_ns=github_source_mtime_ns,
+                        observed_at=self.generated_at,
+                    ),
                 )
```

**状态修正 4（对抗性复核发现的关键 bug）：`add_reviewed_manifest` 的 `source_map` 绑定循环必须同步修正，否则 wiki 绑定会永久性失效为 `unbound`**

原候选设计的 §5.2 diff 只改了 `meta["signed_at"]` 那一行，完全没碰同一个方法里的绑定判定循环（`build_knowledge_graph.py:375-378`）：

```python
for manifest_key, graph_key in source_map.items():
    expected = source_hashes.get(manifest_key)
    observed = sources.get(graph_key, {}).get("sha256")
    bindings[manifest_key] = "exact" if expected and expected == observed else "unbound"
```

一旦 §2.2.3 把 manifest 里 `shared_source_sha256s.wiki` 从字符串变成对象，这里的 `expected == observed` 就变成"字典 == 字符串"，Python 里恒为 `False`——`bindings["wiki"]` 会**永久卡死在 `"unbound"`**，即使 wiki 内容完全没漂移、哈希和版本号都对得上。这条边界情形不是候选设计漏掉一行 diff 这么简单，而是**如果不修，第 2 节的整个改动一旦上线，知识图谱里 `reviewed_knowledge` 到所有 `wiki` 节点的 `reviews` 边会全部消失**（`add_edge` 那几行是 `if bindings.get("wiki") == "exact":` 才会建边）。修正：

```diff
         for manifest_key, graph_key in source_map.items():
             expected = source_hashes.get(manifest_key)
+            expected_hash = expected.get("sha256") if isinstance(expected, dict) else expected
             observed = sources.get(graph_key, {}).get("sha256")
-            bindings[manifest_key] = "exact" if expected and expected == observed else "unbound"
+            bindings[manifest_key] = "exact" if expected_hash and expected_hash == observed else "unbound"
```

（这里只比对哈希是否绑定"exact"，不比对 `content_version`——`bindings` 这个字段本来的语义就是"这份图谱引用的 wiki 字节和 manifest 钉住的字节是否一致"，`content_version` 是 2.2.4 节 session 启动校验的职责，不需要在图谱构建这个只读展示层再重复判定一次，避免和 5.3 节"时间戳只在一个地方存一份"的原则冲突。）

`reviewed_knowledge` 节点自身的时间戳则按 1.3 节的兼容读取规则处理：

```diff
         self.add_node(
             node_id,
             "reviewed_knowledge",
             redact_text(authority, self.home, ROUTE_VALUE_LIMIT),
             {
                 "authority": authority,
                 "schema_version": payload.get("schema_version"),
                 "pack_sha256": pack.get("sha256"),
                 "pack_size_bytes": pack.get("size_bytes"),
-                "signed_at": payload.get("authority_signed_at"),
                 "source_bindings": bindings,
                 "reference_only": True,
             },
+            timestamps=make_timestamps(
+                content_at=payload.get("authority_signed_at"),  # v2 遗留字段，v3/v4 manifest 可能没有，
+                                                                  # make_timestamps 对 None/空输入天然返回 null
+                source_mtime_ns=sources.get("reviewed_manifest", {}).get("source_mtime_ns"),
+                observed_at=self.generated_at,
+            ),
         )
```

**其余没有自然事件时间的节点类型**（`project`/`pane`/`terminal`/`agent_process`/`capability`/`code_graph`/`wiki`/`route`/`skill`），统一模式，以 `wiki` 为例：

```diff
-self.add_node(node_id, "wiki", label, meta)
+self.add_node(node_id, "wiki", label, meta, timestamps=make_timestamps(
+    content_at=None,
+    source_mtime_ns=sources.get("wiki", {}).get("source_mtime_ns"),
+    observed_at=self.generated_at,
+))
```

**顶层 `sources[name]` 增加 `source_mtime`**：

```diff
 "sources": {
   "<name>": {
     "path": "...",
     "sha256": "...",
     "size_bytes": 123,
+    "source_mtime": "2026-08-20T09:00:00+00:00",
     // 可选透传: generated_at / version / schema_version / schemaVersion / authority
   }
 }
```

（这一步在 `run_build()` 序列化 `sources` 字典之前统一遍历一次，把每个 `record["source_mtime_ns"]` 转成 ISO8601 字符串写入 `source_mtime` 键，并从对外输出里剔除内部专用的 `source_mtime_ns` 键，避免同一份数据在输出里出现纳秒整数和 ISO 字符串两种表示。）

**`GRAPH_VERSION` 从 2 提升到 3**：

```diff
-GRAPH_VERSION = 2
+GRAPH_VERSION = 3
```

这个改动的下游影响已被列为第 6 节风险 4 的独立条目（不再只是本节末尾一句"这本身也是第 6 节要专门提示的风险点"的空引用——第 6 节现在确实包含这一条）。

### 5.3 边（edges）为什么不加时间戳——明确决策，不是遗漏

考虑过给 `reviews` 这类由 `reviewed_knowledge` 发出的边额外加一个 `valid_at`，但决定不做：`reviews` 边的有效性完全可以通过它连接的 `reviewed_knowledge` 节点自身的 `timestamps.content_at` 反查得到，没有必要在边上再复制一份同样的值。**复制同一份时间戳到两个地方，本身就制造了一个新的"两处数据可能漂移"的风险点**——这正是 wiki 内容和 manifest 哈希两处记录不同步这类问题的同构版本。因此本设计的原则是：**时间戳只在一个地方存一份，边通过端点节点间接获得时间信息，不做冗余存储**。所有其他 relation 类型同理，边 schema 保持 `{"from", "to", "relation"}` 三键不变。

### 5.4 wiki JSON 与 manifest 的 schema diff 汇总

```diff
 {
   "version": 1,
+  "meta": {
+    "content_version": 3,
+    "updated_at": "2026-08-20T10:00:00+00:00"
+  },
   "project": "...",
   "pages": [
     {
       "id": "...", "title": "...", "path": "...", "summary": "...", "status": "...",
+      "created_at": "2026-08-15T01:35:14+00:00",
+      "updated_at": "2026-08-20T10:00:00+00:00",
+      "verified_at": null,
+      "verification_status": "unverified",
+      "source_mtime": "2026-08-15T01:35:14+00:00",
+      "source_missing": false,
+      "migration_sequence": 0
     }
   ],
   "links": [
     {
       "from": "...", "to": "...", "relation": "...",
+      "created_at": "2026-08-20T10:00:00+00:00",
+      "verified_at": null
     }
   ]
 }
```

> 字段说明（必须原样保留在实际 schema 文档里）："13 个迁移生成的页面共享同一个迁移日 `created_at`，不代表相对新旧关系；需要相对新旧信息时改看 `migration_sequence`（迁移前原始数组下标）或直接查 git blame 里编辑该 JSON 文件的历史提交。"

```diff
 {
   "schema_version": 3,
   "authority": "orca-central-reviewed-l1-l3",
   "pack": {...},
   "project_git": {...},
   "content_source_closure": {...},
   "shared_source_sha256s": {
-    "wiki": "538fd671...",
+    "wiki": {
+      "sha256": "538fd671...",
+      "content_version": 3,
+      "pinned_at": "2026-08-20T10:05:00+00:00"
+    },
     "capabilities": "13758f19...",
     "graphify_catalog": "7454e66b..."
   },
   "shared_source_policy": {...}
 }
```

> 前提说明（必须原样保留在实际 schema 文档里）："此 diff 以 schema_version 3 为基线；仓库里当前实际生效的 manifest 是 schema_version 2，字段集合与此不同（含 `authority_signed_at`/`signed_by`/`signature_nonce`，无 `content_source_closure`）。在把这个 diff 应用到生产 manifest 之前，必须先独立完成 v2→v3/v4 的迁移，本设计不在其中隐含这一步。见第 6 节风险 5。"

---

## 6. 已知风险与后续验证方式

### 风险 1（最高）：`verify_reviewed_pack` 的新旧格式兼容分支写错，会把 fail-closed 悄悄改成 fail-open

这条改动直接触碰仓库里唯一真正生效的安全权威校验路径，属于用户全局规则里明确要求"安全/发布/最终验收"必须 Claude opus+max 与 Codex sol+max 双路独立只读复核的范畴。最容易犯的具体错误：

- 把 `content_version` 缺失（`None`）或类型错误（如误写成字符串 `"3"`、或布尔值 `True`）当作"旧格式，跳过版本检查"处理，而不是当作"不匹配"处理——这会让一个内容已经变了但版本号忘记递增的 wiki 直接通过校验，正好复现 Audit 3 发现的原始漏洞，只是换了个位置。第 2.2.4 节的 diff 已经显式加了 `isinstance(x, int) and not isinstance(x, bool)` 双重判断，实现和 code review 阶段都要专门检查这一条是否被原样保留。
- `isinstance(expected, dict)` 判断写反或者漏了某个分支，导致 dict 类型的 `expected` 落进 `elif expected is not None and not valid_sha256(expected)` 分支，被 `valid_sha256` 判定为无效直接拒绝——这在功能上是"过度安全"（拒绝了本该通过的合法 wiki），不是安全漏洞，但会导致 wiki 源永远无法通过校验，必须在测试里覆盖。
- 实现完成后必须专门为以下四种反例写测试，缺一不可：**哈希对但版本号错**、**哈希错但版本号对**、**两者都缺失**、**版本号类型不是 int（比如被误写成字符串 `"3"` 或布尔值 `true`）**——这四种都必须触发 fail-closed，且报错信息里能区分是哪一种不匹配。

### 风险 2：迁移阶段用 mtime 或迁移执行时间冒充 `verified_at`

第 3.1 节已经把结论定死为"一律 `null`"，但这是工程实现里最容易被"图省事"绕过的一条——填个看似合理的时间戳比留 `null` 在视觉上更"完整"，诱惑很大。一旦有人在实现时把 `verified_at` 默认填成迁移脚本运行时刻，就会让已知内容有误的 5-6 个页面看起来像是"今天被人核实过是对的"，这比完全没有这个字段更危险，因为它会被第 2 节设计的自动化新鲜度校验和任何读这份 wiki 的下游 agent 当作真实凭据来信任。**实现和 code review 阶段都要专门检查这一条：`verified_at` 字段除了显式调用人工核实命令或自动核实脚本产生的值之外，不允许出现任何其他来源的非 null 值。**

### 风险 3：把 schema 迁移和第 4 节的 14 个新页面内容混在同一次改动里提交/复核

如果把"给 wiki/图谱加时间戳字段和校验逻辑"这个结构性改动，和"新增 14 个内容页面"这个内容性改动放在同一个 PR/同一轮复核里，会造成两个具体问题：一是复核者的注意力会被大量新增文字内容分散，容易漏审真正安全敏感的 `verify_reviewed_pack` 逻辑改动；二是一旦某个新增页面的 `summary` 被复核者挑出事实错误，会导致连带整个 PR 被打回，包括本来已经验证正确的 schema/校验逻辑改动也要跟着重新走一遍复核流程，浪费复核成本。**必须严格分两阶段独立提交、独立双审、独立签名**：

- **阶段 A**：schema 迁移 + `wiki_edit_guard.py` + `check_wiki_freshness.py` + `verify_reviewed_pack`/`build_knowledge_graph.py` 校验逻辑改动 + 全部存量条目回填为 `unverified`（不新增任何页面内容），先落地验证空转。**阶段 A 必须以第 6 节风险 5 描述的 v2→v3/v4 manifest 迁移作为独立前置任务完成为条件**，不能在一份仍是 v2 的 manifest 上叠加本设计的 `wiki` 字段对象化。
- **阶段 B**：阶段 A 稳定生效之后，才作为纯内容 PR 提交第 4.1 节的 14 个新页面和第 4.2 节的既有页面修正，且阶段 B 提交时机不早于对应页面真正完成人工核实（不能把还没查证的新页面也标成 `verified`）。

### 风险 4（新增）：`GRAPH_VERSION` 2→3 与节点 `timestamps` 键的引入会破坏任何依赖固定 v2 形状的下游消费者

节点新增 `timestamps` 键、`sources[name]` 新增 `source_mtime` 键、`add_reviewed_manifest` 输出的 `meta` 里移除了 `signed_at` 原始透传字段（改由 `timestamps.content_at` 承载）——这些都是对已发布 JSON 输出形状的破坏性变化。任何脚本/UI 如果做了 `node["meta"]["signed_at"]` 这类字段路径的硬编码读取，会在升级后读到 `KeyError`/`None`。**必须在合并前**：（1）全仓库检索 `signed_at`、`updated_at`（在 `meta` 上下文里）、`GRAPH_VERSION` 的所有读取点，逐一确认是否需要跟随迁移；（2）`GRAPH_VERSION` 的语义本身要在图谱构建脚本的文档字符串里写清楚"v2→v3 是一次破坏性输出形状变化，不是纯新增字段"，避免未来有人误以为版本号只是元数据、不需要下游联动升级。

### 风险 5（新增，先于本设计存在但必须独立处理）：当前生效的 manifest 是 schema_version=2，与代码要求的 v3/v4 之间存在既有落差

第 2.2.3 节已核实：`.orca/context/reviewed-startup-pack-manifest.json` 磁盘上的当前内容是 schema_version 2、含 `authority_git`/`authority_signed_at`/`signed_by`/`signature_nonce`；但 `build_startup_bundle.py` 当前代码只接受 3/4，且严格字段集合断言里已经不含这些 v2 字段。这意味着：**这份 manifest 用现在的代码去验证，本来就会失败**，与本设计是否落地无关。这是一个独立的、优先级更高的既有缺口，必须先被确认（是否已有另一条迁移/发布路径在途、是否是配置漂移、还是这份文件本身就是过期产物）并独立处理，才能安全地在其上叠加本设计 2.2.3 节的 `wiki` 字段对象化。**本设计明确不在自己的范围内解决这个缺口**，只是如实记录、并把它列为阶段 A 的前置条件（风险 3），避免它被误当作本设计"顺手带过"的次要问题而被静默忽略。同样，`sign_reviewed_authority.py` 是否是当前真正生效的重签工具、其"非自签、≥300 秒陈化"约束是否仍然是活跃的生产不变式，需要在同一轮里一并确认，本设计不擅自断言其现状。

### 风险 6（新增）：`make_timestamps`/`parse_timestamp` 调用点如果漏加空值判断，会让整个图谱构建因单条脏数据而崩溃

第 5.2 节已经修正了 `make_timestamps` 内部对 `parse_timestamp` 返回 `None` 的处理（不用 try/except，直接判空），但这个模式必须在代码评审时作为一条通用检查项：任何新代码只要调用 `parse_timestamp(...)`，后面紧跟 `.isoformat()` 之前都必须先判断返回值是否为 `None`。测试要求：为 `make_timestamps` 单独写单元测试，输入至少覆盖：`None`、空字符串、格式错误的字符串（如 `"not-a-date"`）、缺 `tzinfo` 的裸日期字符串、`Z` 后缀、`+00:00` 后缀、以及数字型 epoch 时间戳——确认每一种都不抛出未被捕获的异常，脏数据一律归一化为 `null` 而不是让整次构建失败。

### 风险 7（新增，对应 1.4 节新增内容）：迁移生成的统一 `created_at` 可能被下游误读为"这些内容一样新"

见 1.4 节已给出的分析和 `migration_sequence` 缓解措施。后续验证方式：在阶段 A 落地后，抽查任意一个消费 wiki JSON 的下游 UI/脚本，确认它没有把 13 个页面相同的 `created_at` 值渲染成"看起来都是今天新增的"这类误导性展示；如果发现有这类展示逻辑，需要在该消费方同步接入 `migration_sequence` 或改为优先展示 `verification_status`。

### 风险 8（新增）：Audit 1–4 报告未提交到仓库，本设计第 4 节的优先级判断依赖未持久化的外部结论

对抗性复核独立核实了 4.1 节列出的 14 个新页面所引用的目录/文件在仓库里确实存在（不是虚构目标），第 4.0 节也已经把优先级判断所需的具体事实内联进本文档，使其不再依赖外部未提交文件才能自证。但根本问题仍未解决：**Audit 1–4 的完整报告本身仍不在仓库里**，本项目自身的双模型复核/可追溯性规范要求关键结论应当可被后续复核方独立核对原文，而不是仅有转述。后续验证方式：作为阶段 A 或阶段 B 的一个附带任务，把 Audit 1–4 的完整报告提交到仓库（例如放在 `reports/` 下），并在本设计文档顶部补充指向这些文件的相对路径引用，使 4.0 节的内联结论和外部原文可以交叉核对。

---

## 附录A：SSD 上完整对话记录纳入索引辨别

*本附录基于两份独立审计撰写——一份现场复核了 Codex 与 Claude Code 完整对话记录在 SSD 上的真实物理布局，另一份核对了 `add_sessions()`/`sync_sessions.py`/`build_context_digest.py` 现有索引深度与仓库既有隐私/访问边界政策——用以回应用户新增需求："对话记录保存到 SSD 也要作为索引辨别的一部分，Codex 和 Claude Code 都要覆盖"。本附录初稿完成后又经过一轮独立对抗性代码复核，发现初稿里 §A.2/A.3 的代码引用有一处引用了仓库里不存在的符号（`ALLOWED_CATALOG_SESSION_KEYS`）、遗漏了 `Session` 数据类本身就丢弃文件路径这一前置缺口、`record_logical_home` 的取值设计会让一个既有代码明确要隐藏的账号 UUID 重新出现在图谱可读范围内、`record_path` 在账号池场景下独立地带有同一个 UUID 泄露面、以及"两个账号完全不在 SSD 上"这句话在符号链接被正确解析后其实不成立、并因此掩盖了一个更值得关注的真实 bug（跨 source 的会话重复计数与账号归属误判）。以下正文已经把这些发现直接改写进设计里，不再是勘误列表。*

*范围界定：本附录只扩展 `session` 节点类型，只关闭既有审计称为 **Gap A** 的缺口——"没有任何字段能从图谱节点走到磁盘上那份具体记录文件"——不触碰第 1–6 节已经定案的时间戳语义、写屏障、wiki 校验逻辑，也不改变第 2 节反复确认的既有隐私边界："知识图谱只读取已脱敏 JSON，从不读取原始会话文本"。Gap B（内容从未被读入任何可被发现的产物）、Gap C（`redact_text()` 不足以处理全文）、Gap D（政策明确排除原始记录）三者本附录**明确保持不做**，理由见 A.5，不是遗漏。*

### A.1 现状精确定位：两家完整对话记录在 SSD 上的真实位置

现场用 `readlink` 复核确认三条符号链接均为真实符号链接（`lrwxr-xr-x`）：`$HOME/.claude -> /Volumes/Extreme SSD/Orca/local-homes/.claude`、`$HOME/.codex -> /Volumes/Extreme SSD/Orca/local-homes/.codex`、`$HOME/.codex-profiles -> /Volumes/Extreme SSD/Orca/local-homes/.codex-profiles`。但"`$HOME` 是符号链接"**不等于**"这台机器用到的所有 Codex home 都在 SSD 上"，也**不等于**"没有走 `$HOME` 符号链接的账号池目录就一定不在 SSD 上"——账号池里的路径即使物理落在系统盘的账号目录下，其内部文件也可能只是指回 SSD 的符号链接。这两层可能性都必须分别核实，不能互相替代，见下表账号池行与本附录风险 9/10。

**Claude Code**（真实目录 `/Volumes/Extreme SSD/Orca/local-homes/.claude/projects/`）：

| 维度 | 实际情况 |
|---|---|
| 目录命名 | 一个子目录对应一个不同的 cwd，目录名 = 该 cwd 绝对路径把 `/` 及其他非字母数字的运行边界字符（空格、标点）全部折叠替换为 `-`。例如 cwd `/Volumes/Extreme SSD/Orca/projects/orca` → 目录 `-Volumes-Extreme-SSD-Orca-projects-orca`。现场实测 104 个此类项目目录，另有 1 个非项目目录 `memory/`（用户自身 MEMORY.md 引用的自动记忆存储，不是会话记录目录，不在本附录索引范围内）。 |
| 文件命名 | 每个项目目录下直接放会话文件，一个 session 一个文件：`<session-uuid>.jsonl`（如 `00a95664-4091-49c2-aeec-b09ccc4f7bed.jsonl`）。同一 UUID 有时还有一个同名兄弟目录，装该 session 的旁路数据 `subagents/agent-<id>.jsonl`+`.meta.json`、`tool-results/*.txt`——这些是该 session 的辅助产物，**不是**额外的独立记录，本附录索引不单独收录它们。 |
| 单条记录 schema | 每行一个 JSON 对象，`type` 字段区分行类型：`last-prompt`/`mode`/`permission-mode`（小型会话状态行）；`attachment`（`sessionId, uuid, parentUuid, timestamp(ISO8601), isSidechain, userType, cwd, version, gitBranch, entrypoint`）；`user`（同一信封字段 + `message:{role:"user",content}` + `promptId,promptSource,origin,permissionMode`）；`assistant`（信封 + `message:{id,type,role:"assistant",model,content:[{type:"text",text}],stop_reason,...}` + `requestId,session_id,effort`）；`system`（`subtype,isMeta,durationMs,messageCount`+信封）；以及 `file-history-snapshot`/`file-history-delta`/`queue-operation`/`ai-title`/`bridge-session` 等辅助行。**`sessionId` 与文件名一致、每行带 `timestamp`、`parentUuid`/`uuid` 构成消息链**——具备一份可索引记录所需的最小结构。 |

**Codex**（`discover_codex_files`/`discover_codex_homes`，`orca-context-bridge/scripts/build_context_digest.py`；账号池发现在 `orca-context-bridge/scripts/sync_sessions.py::all_primary_sessions`）：

| 维度 | 实际情况 |
|---|---|
| home 发现逻辑 | `discover_codex_homes(default_home, profiles_root, include_profiles)` → `[("codex", default_home)] + [("codex:<name>", p) for p in profiles_root.iterdir() if (p/"sessions").is_dir()]`；`--codex-home` 默认取 `os.environ.get("CODEX_HOME") or ~/.codex`（**优先用 `CODEX_HOME` 环境变量，不是无条件用 `~/.codex`**）。账号池不经过 `discover_codex_homes`：`sync_sessions.py::all_primary_sessions` 单独对 `orca-codex-accounts-root` 下每个 `*/home` 目录调用 `Path.resolve()` 并以 `(st_dev, st_ino)` 身份算出一个**不可逆的哈希标签** `codex:orca-<sha256(dev:ino)[:12]>`（`opaque_codex_account_source()`，`sync_sessions.py:707-710`，函数自身文档字符串写明"按目录身份命名账号，不用路径别名"），这个哈希标签直接就是该账号 session 的 `source` 字段值——不是 `discover_codex_homes` 产出的标签，两条发现路径是独立的两段代码。 |
| 常规目录 | `/Volumes/Extreme SSD/Orca/local-homes/.codex/sessions/2026/<MM>/<DD>/rollout-<ISO时间戳>-<codex-turn-uuid>.jsonl`（现场 310 个文件）。示例：`rollout-2026-08-14T16-18-00-019fff59-7329-7e43-9b1f-c4fc54d20a38.jsonl`。`~/.codex-profiles` 下 `acct-a`/`acct-b`/`acct-c` 三个账号目录物理上也全在 SSD（经 `.codex-profiles` 符号链接到达），但 `acct-c` 没有 `sessions/` 子目录，会被 `discover_codex_homes` 的 `(p/"sessions").is_dir()` 判断**静默跳过**——这不是账号池整体在 SSD 之外，而是发现逻辑本身的一个静默跳过分支，见风险 11。 |
| **账号池路径细节（现场重新核实，纠正了本附录初稿的计数）** | `orca-codex-accounts-root` 的真实位置是 `~/Library/Application Support/orca/codex-accounts/`——**系统盘上的真实目录，不是符号链接**。4 个账号 UUID 目录中，`0b4cd443-...`/`b9f32a51-...` 的 `home` 本身就是符号链接、指向 SSD `local-homes/codex-accounts/<uuid>/home`；`235a57b1-...` 与 `8d7db875-...`（当前 shell `$CODEX_HOME` 实际绑定的账号）的 `home` 是系统盘上的**真实目录**。但真实目录不等于其内容不在 SSD 上：现场用 Python `Path.is_file()`（`discover_codex_files` 真实使用的判断，会跟随符号链接，不同于 shell `find -type f`）重新统计，`235a57b1-.../home/sessions/` 下 **0 个常规文件、308 个符号链接**，这 308 个符号链接**全部**指回 `~/.codex/sessions/...`（即 SSD 上的默认 home，示例 `rollout-2026-03-10T04-32-40-019cd44d-....jsonl`）——`is_file()` 对它们返回 `True`，也就是说真实代码路径会把这 308 个文件当作"存在的 session 文件"发现到，且解析后落在 SSD 上，不是 0 个、也不是不在 SSD 上。`8d7db875-.../home/sessions/` 下有 **81 个常规文件（489MB，只存在于系统盘，没有对应符号链接）+ 310 个符号链接（同样全部指回 SSD 上的 `~/.codex/sessions/...`）**。 |
| 单条记录 schema | 每行 `{"timestamp","ordinal","type","payload"}`。`type` 含 `session_meta`（`payload.id`/`payload.session_id` 与文件名 UUID 一致，另有 `cli_version,originator,source,thread_source,cwd,git:{branch,commit_hash,repository_url}`）、`event_msg`、`response_item`（`payload.role ∈ {developer,user,assistant,...}, type:"message", content:[{type:"input_text"|"output_text"|...,text}]`）、`world_state`、`turn_context`（`model,effort,sandbox_policy,approval_policy,workspace_roots`）、`compacted`。同样具备 session id、逐行 `timestamp`、`role`（嵌套在 `payload.role` 下，与 Claude 顶层 `role` 的位置不同）。 |

**结论（修正后，不含糊表述）**：Claude Code 一侧（`$HOME/.claude`）**已核实全部在 SSD 上**，无此项风险。Codex 一侧**不是均匀地**"部分在 SSD、部分不在"——真正只存在于系统盘、SSD 上没有任何等价副本的记录，只有 `8d7db875-...` 账号下那 **81 个常规文件**；`235a57b1-...` 账号下的 308 个"记录"和 `8d7db875-...` 账号下额外的 310 个"记录"，经真实代码路径（跟随符号链接的 `is_file()`）解析后，物理内容其实就落在 SSD 上的默认 `~/.codex/sessions/...` 里，是**同一份物理文件通过账号池符号链接被重复看见**，不是账号池自己产生的、系统盘独有的新数据。这个区分本身就是本附录必须做对的地方——如果只按"账号目录是不是符号链接"这一层浅层信号判断，会把 308+310 个其实已经在 SSD 上的记录也误判为"不在 SSD 上"；但如果反过来因为"两个账号的 home 目录都能在 SSD 上找到对应内容"就假定这两个账号完全没有游离于 SSD 之外的数据，又会漏掉 `8d7db875-...` 那 81 个真正只存在于系统盘的常规文件。两种简化都不对，必须逐条按解析后的物理路径判断，展开为风险 9；而"同一份物理文件被两个不同 source 标签重复索引"这个现象本身，是一个此前未被识别的独立正确性问题，展开为风险 10。

### A.2 现有 `add_sessions()`/`sync_sessions.py`/`build_context_digest.py` 做到了什么、和"索引辨别完整记录本身"之间具体缺什么

**已经做到的（元数据级）**：本设计第 5.2 节已经把 `add_sessions()` 改为 `add_sessions(self, payload, sources)`，为每个 `session` 节点写入 `meta={"provider":..., "source":..., "cwd":...}` 加上 `timestamps=make_timestamps(content_at=entry.get("updated_at"), source_mtime_ns=..., observed_at=self.generated_at)`。这一层的输入来自 `sync_sessions.py::validate_catalog_session`（`sync_sessions.py:745-796`）产出的目录（catalog）。**该函数内部并没有一个名为 `ALLOWED_CATALOG_SESSION_KEYS` 的模块级常量**（这是本附录初稿的一处错误引用，仓库里不存在这个符号）；真实的允许字段集合是函数体内的局部变量 `expected`（`sync_sessions.py:749-755`），且校验方式是**精确集合相等**：`if set(entry) != expected: raise ValueError(...)`，不是"在允许列表里即可"的子集/成员校验。这意味着 A.3 往这个集合里新增字段，等价于把新字段变成**每一条**目录条目（不区分 provider）都必须携带的必填键，而不是"新增了一个可选能力"——下面的 diff 与说明据此改写。

**具体缺口（对照代码实现，不是抽象地说"不够完整"）**：

- **Gap A——没有定位指针字段，且缺口比初稿描述的更靠前一层**。真正在磁盘发现阶段拿到每个 session 具体文件路径的，是 `select_sessions()`（`build_context_digest.py`）内部 `for path in recent:` 循环里的局部变量 `path`，它被传给 `parse_claude_session(path, ...)`/`parse_codex_session(path, ...)`。但这两个函数返回的 `Session` 数据类（`build_context_digest.py:118-128`）**当前没有 `path` 字段**——`path` 只在函数体内被用来算 `session_id`（`path.stem`/正则提取）和兜底 `updated_at`（`path.stat().st_mtime`），随后就被丢弃，不会被写回 `Session` 对象。也就是说，从"知道文件在哪"到"这个信息进入目录 JSON"之间，路径信息在**第一步**（`parse_*_session` 返回 `Session` 对象时）就已经不再存在，`build_catalog()`（`sync_sessions.py:963-993`，遍历 `result.sessions` 逐条构造 catalog 条目的地方）根本拿不到 `session.path` 可用——这是比"catalog 允许字段集合没加字段"更靠前一层、且必须先修的前置缺口，A.3 的 diff 需要同时覆盖这一步。
- 现有允许字段集合里唯一路径形状的字段是 `codex_home`，且只对 Codex 有值，指向的是**home 根目录**（`build_catalog()` 里赋值为 `str(source_home) if result.provider == "codex" else None`），不是某一个 session 自己的 `.jsonl` 文件；而这个字段甚至连图谱节点都到不了——`add_sessions()` 目前只从 `entry` 里拷贝 `provider`/`source`/`cwd`/`updated_at` 四个键（本设计 5.2 节的 diff 同样只涉及这四个键 + 新增的 `timestamps`），`codex_home` 在拷贝时被丢弃。
- **辨别粒度：现有 `source` 字段已经比初稿以为的更细，不需要再造一个新字段去做同一件事**。`provider` 只有 `"claude"`/`"codex"` 两个值，粒度确实太粗；但 `source` 字段的真实取值枚举已经现场核实清楚：Claude 侧固定为 `"claude"`；Codex 侧为 `"codex"`（默认 home）、`"codex:<profile-name>"`（`.codex-profiles` 下的具名 profile）、或 `"codex:orca-<sha256[:12]>"`（账号池账号，来自 `opaque_codex_account_source()`）。也就是说：**"能不能单独挑出账号池里某一个具体账号"这件事，`source` 字段现在就已经能做到**（只是标签是稳定的哈希而不是可读名字，这是既有代码刻意的隐私选择，见 A.3）。真正缺的只是"这条 session 具体落在磁盘哪个文件"和"这个文件是否在 SSD 上"这两件事——也就是 A.3 要新增的 `record_path`/`record_on_ssd` 两个字段，不需要第三个字段来重复 `source` 已经提供的辨别能力。

结论：**"一个 session 被索引"目前等于一条脱离物理文件的元数据摘要**（provider/source 标签、cwd、`updated_at`）；从图谱节点走到磁盘上那份完整记录，没有任何字段能承担这一步——这正是 Gap A，也是本附录 A.3 要关闭的唯一缺口。

### A.3 具体设计：给 session 节点新增定位指针字段

**设计原则**：只存路径指针，不把原文塞进图谱/wiki JSON。原因直接对应既有隐私边界政策（`SKILL.md` "Privacy boundary" 与知识图谱章节，已在两份审计中逐条核实）：知识图谱当前"只读取已脱敏 JSON，从不读取原始会话文本"是明确写在文档里的既有设计（不是遗漏），wiki 页面"不得含凭据、记录原文或命令输出"同样是明文要求；一条文件系统路径字符串本身不含对话内容，只是"内容存放在哪"这一事实，不构成对上述边界的突破。

**新增字段收窄为两个，不是三个**：初稿提出的 `record_logical_home` 被本轮直接取消，原因不是"多余"这么简单，而是它按初稿给出的取值形状（账号池账号用 `"codex-account:<raw-uuid>"`）会**主动逆转**一处既有代码刻意做出的隐私选择——`opaque_codex_account_source()` 的文档字符串原文是"按目录身份命名账号，不用路径别名"，函数体用 `sha256(f"{st_dev}:{st_ino}")[:12]` 生成不可逆标签，就是为了不让账号池的原始 UUID 出现在任何面向 catalog/图谱的字段里。A.2 已经确认这个哈希标签本来就是 `source` 字段的真实取值，`add_sessions()` 也早就把 `source` 拷进了节点 `meta` ——按 provider/账号筛选的能力已经存在，不需要再造一个字段，更不需要再造一个会把哈希故意还原回原始 UUID 的字段。

`record_path` 单独构成一个例外，需要专门处理：即便不新增 `record_logical_home`，`record_path` 字符串本身在账号池场景下也会带着账号 UUID——但这个泄露面并不像 Claude 侧 cwd 编码目录名那样"反正 `meta.cwd` 已经原样存了同一份信息、不构成新暴露"，因为 Codex 侧现有字段里**没有**任何地方已经存了账号池的原始 UUID（`codex_home` 在拷进图谱节点前就被丢弃，`source` 只存哈希）。经 A.1 现场复核确认，这个泄露面比初稿设想的窄：账号池目录下的绝大多数 `.jsonl` 其实是指回 SSD 默认 home 的符号链接，`record_path` 写入前统一执行 `Path(...).resolve(strict=False)`，符号链接会被解析穿透，解析结果落在 `~/.codex/sessions/...`（即 SSD 默认 home），**不带任何账号 UUID**；真正会把 UUID 保留在解析后路径里的，只有账号自己的**常规（非符号链接）文件**——现场只有 `8d7db875-...` 账号下的 81 个文件符合。对这一小类，`record_path` 写入 catalog 前必须做一次显式替换：若解析后路径的某一段等于 `orca-codex-accounts-root/<uuid>/home` 这个前缀形状，把 `<uuid>` 段替换成该账号在 `source` 里已经使用的同一个哈希标签（`orca-<hash12>`，去掉 `codex:` 前缀），例如 `.../codex-accounts/8d7db875-541c-4499-b219-64ec9b7486d6/home/sessions/...` 写入 catalog 时变成 `.../codex-accounts/orca-<hash12>/home/sessions/...`。这样 `record_path` 依然是可用于区分"这是不是同一份物理文件"的真实指针，但不会成为图谱/wiki 里第二个泄露账号原始 UUID 的地方。

三个字段收窄为两个后的类型与取值形状：

| 字段名 | 类型 | 含义 | 示例值 |
|---|---|---|---|
| `record_path` | `string \| null` | 该 session 完整记录文件的绝对路径，写入前经 `Path(...).resolve(strict=False)` 解析符号链接，指向物理落盘位置而非逻辑 `$HOME` 路径；若路径落在 `orca-codex-accounts-root/<uuid>/home/...` 下，`<uuid>` 段替换为该账号 `source` 字段已用的同一哈希标签（见上）；发现阶段确实拿不到（理论上不应发生，但必须优雅降级而非让整次构建崩溃，同 `parse_timestamp` 返回 `None` 而非抛异常的既有原则）时为 `null` | Claude：`"/Volumes/Extreme SSD/Orca/local-homes/.claude/projects/-Volumes-Extreme-SSD-Orca-projects-orca/00a95664-4091-49c2-aeec-b09ccc4f7bed.jsonl"`；Codex 常规：`"/Volumes/Extreme SSD/Orca/local-homes/.codex/sessions/2026/08/14/rollout-2026-08-14T16-18-00-019fff59-7329-7e43-9b1f-c4fc54d20a38.jsonl"`；Codex 账号池符号链接条目（解析后落回 SSD 默认 home，不带 UUID）：同上一条常规路径；Codex 账号池 `8d7db875` 常规文件（UUID 段已替换为哈希标签）：`"/Users/www1adwawd/Library/Application Support/orca/codex-accounts/orca-<hash12>/home/sessions/2026/08/15/rollout-....jsonl"` |
| `record_on_ssd` | `bool` | `record_path` 解析后的真实路径，是否落在本机 SSD 挂载根之下；用 `Path.is_relative_to(ssd_root)`（Python 3.9+ 原生方法，边界安全）判断，**禁止用 `str.startswith()`**——原始 UUID 字符串前缀比较无法区分"落在 SSD 挂载根内"与"路径字符串恰好以同一段字符开头但其实是另一个相邻目录"（例如 `/Volumes/Extreme SSD/Orca-decoy/...` 对 `/Volumes/Extreme SSD/Orca`），必须走真正的路径分段比较；发现阶段计算一次，不在图谱构建时重算 | `true`（`.claude`/`.codex`/`.codex-profiles` 常规路径下，以及账号池里解析后落回 SSD 的符号链接条目）；`false`（仅 `8d7db875-...` 账号下那 81 个常规文件） |

**在 `sync_sessions.py::validate_catalog_session` 的局部变量 `expected` 中新增两个键（精确集合相等校验，新增即必填）**：

```diff
     expected = {
         "provider", "source", "id", "title", "cwd", "updated_at", "cwd_available",
         "original_cwd", "cwd_remapped", "remap_status", "path_map_id", "source_limited",
-        "parse_errors", "codex_home",
+        "parse_errors", "codex_home", "record_path", "record_on_ssd",
     }
     if set(entry) != expected:
         raise ValueError("catalog session entry schema is invalid")
     ...
+    record_path = entry["record_path"]
+    if record_path is not None:
+        _catalog_string(record_path, "record_path", maximum=4096, absolute=True)
+    _catalog_bool(entry["record_on_ssd"], "record_on_ssd")
```

**`build_context_digest.py::Session` 必须先获得一个 `path` 字段，否则 `build_catalog()` 无从取值**（这是 A.2 指出的前置缺口，diff 落在这里）：

```diff
 @dataclass
 class Session:
     provider: str
     session_id: str
     cwd: str | None
     updated_at: datetime
+    path: Path | None = None
     launch_cwd: str | None = None
     source: str | None = None
     title: str | None = None
     messages: list[Message] = field(default_factory=list)
     source_limited: bool = False
     parse_errors: int = 0
```

`parse_claude_session`/`parse_codex_session` 各自在末尾的 `return Session(...)` 调用里加一个 `path=path,`（两处函数签名本来就有 `path: Path` 形参，只是从未写回返回值）。

**`sync_sessions.py::build_catalog()` 在已有的 `codex_home` 赋值旁新增 `record_path`/`record_on_ssd`**（新增一个模块级 `_is_under_ssd_root(path, ssd_root)` 辅助函数，内部用 `path.is_relative_to(ssd_root)`；账号池 UUID 替换逻辑封装在 `_sanitize_account_uuid(path, source_label)` 里，仅当 `source_label` 形如 `codex:orca-<hash>` 且解析后路径经过 `orca-codex-accounts-root/<uuid>/home` 时生效）：

```diff
                     "source_limited": session.source_limited,
                     "parse_errors": session.parse_errors,
                     "codex_home": str(source_home) if result.provider == "codex" else None,
+                    "record_path": (
+                        str(_sanitize_account_uuid(session.path.resolve(strict=False), source_label))
+                        if session.path is not None else None
+                    ),
+                    "record_on_ssd": (
+                        _is_under_ssd_root(session.path.resolve(strict=False), ssd_root)
+                        if session.path is not None else False
+                    ),
                 }
             )
```

**`add_sessions()` 在本设计第 5.2 节已改写版本基础上叠加这两个字段**（延续同一处 diff，不是另开一个方法）：

```diff
         session_source_mtime_ns = sources.get("sessions", {}).get("source_mtime_ns")
         for entry in sessions:
             ...
             self.add_node(
                 node_id,
                 "session",
                 label,
                 {
                     "provider": entry.get("provider"),
                     "source": entry.get("source"),
                     "cwd": normalize_cwd(entry.get("cwd")),
+                    "record_path": entry.get("record_path"),
+                    "record_on_ssd": entry.get("record_on_ssd"),
                 },
                 timestamps=make_timestamps(
                     content_at=entry.get("updated_at"),
                     source_mtime_ns=session_source_mtime_ns,
                     observed_at=self.generated_at,
                 ),
             )
```

**为什么"只存路径指针、不把原文塞进图谱/wiki JSON"是正确设计，而不是偷懒**：这正是既有隐私边界政策已经确立、且被两份审计逐条核实为"深思熟虑而非疏漏"的分界线——`build_knowledge_graph.py` 目前导入 `build_context_digest.py` 时只导入 `redact_text, write_private`，从不导入任何读取 session 内容的函数（`parse_claude_session`/`parse_codex_session`/`read_jsonl_window`）；`--sessions` 输入结构性地无法携带消息内容。新增 `record_path` 不改变这条边界的任何一环——它依然是"目录 JSON 里的一个字符串字段"，图谱构建脚本依然不打开、不解析它指向的文件；对账号池 UUID 做的替换处理，是在这条既有边界之上再补一层，确保这个新字段不会成为账号身份这一单独维度上的新泄露点。

### A.4 支持"按 provider / 时间范围 / 项目 cwd 辨别"的索引结构设计

**决策：不新增知识图谱 edge 类型，也不新建独立轻量索引文件——复用现有 `session` 节点 + 现有 `meta.source` 字段 + A.3 新增的 `record_path`/`record_on_ssd` 两个 `meta` 字段 + 本设计第 5 节已经确立的 `timestamps` 子对象，把 `knowledge_graph.json` 本身当作这份索引。**

**为什么不新增 edge 类型**：`record_path`/`record_on_ssd` 描述的是 session 节点自身的固有属性（"这条记录存在哪""在不在 SSD 上"），不是两个独立实体之间的关系——边（edge）在本设计里专门用来表达"两个不同节点之间存在某种关联"（如 `reviews`、`overlaps_with`）。第 5.3 节已经把"时间戳只在一个地方存一份、边通过端点节点间接获得信息、不做冗余存储"确立为明确原则；同一原则同样适用于定位指针——如果改用一条 `session --located_at--> record_file` 的边，`record_file` 又得是一个新节点类型，那就需要给这个新节点类型再补一整套 `timestamps`/`meta` 字段，纯粹为了表达一个本来就是 1:1、单值、只读的属性而制造出一个新的"两处数据可能漂移"风险点，得不偿失。

**为什么不新建独立索引文件**：一份独立的 `records-index.json` 会和 `knowledge_graph.json` 里的 `session` 节点描述同一批 session、却分别由两条构建路径产生——这正是本设计从第 2 节 wiki/manifest 双写不同步问题里已经反复论证过的反模式（"两处数据可能漂移"）。`knowledge_graph.json` 已经是全仓库对"session 有哪些、属于谁、什么时候更新"这件事的唯一权威索引来源（第 5 节新增的 `timestamps.content_at` 已经是可排序的 ISO 8601 字符串），没有理由为同一批实体再造一份平行数据。

**具体查询方式**（对既有节点数组的谓词过滤，不需要新的查询引擎或索引结构）：

```python
def find_sessions(nodes, provider=None, source=None, cwd=None, since=None, until=None, ssd_only=None):
    for n in nodes:
        if n["type"] != "session":
            continue
        m = n["meta"]
        if provider is not None and m.get("provider") != provider:
            continue
        if source is not None and m.get("source") != source:
            continue
        if cwd is not None and m.get("cwd") != cwd:
            continue
        if ssd_only is not None and bool(m.get("record_on_ssd")) != ssd_only:
            continue
        content_at = n["timestamps"]["content_at"]
        if since is not None and (content_at is None or content_at < since):
            continue
        if until is not None and (content_at is None or content_at > until):
            continue
        yield n
```

四个筛选维度都直接落在已有/新增字段上：**按 provider** → `meta.provider`（粗粒度）；**按具体账号/来源** → `meta.source`（细粒度，取值已在 A.2 现场核实清楚，能单独挑出账号池里某一个具体账号——但**必须**同时读一下风险 10：由于跨 source 目前没有基于 session id 的去重，同一份物理文件可能同时挂在 `source="codex"` 和 `source="codex:orca-<hash>"` 两条记录下，`meta.source` 筛选出来的结果在去重之前不能直接当作"这个账号独有的会话集合"使用）；**按时间范围** → `timestamps.content_at`（ISO 8601 字符串天然可比较排序，第 1.2 节已确立的统一格式在这里直接复用）；**按项目 cwd** → `meta.cwd`（已经过 `normalize_cwd` 规范化，第 5.2 节既有逻辑不变）。额外获得的第五个筛选维度是 `meta.record_on_ssd`——这不是本次要求的三项之一，但它是 A.3 设计的直接副产品，且恰好是审计出的真实缺口（风险 9）所需要的排查手段：运维/复核时可以直接筛出 `record_on_ssd=false` 的 session，逐条核对是否已被纳入迁移或备份范围。

### A.5 隐私/脱敏边界：明确不做全文内容索引，以及为什么这是克制而不是遗漏

**本设计明确不做的事**：不读取 `record_path` 指向的文件内容、不对其做全文关键词索引、不做摘要、不把任何一条消息文本写入 `knowledge_graph.json`/wiki JSON 的任何字段。`record_path` 是唯一新增的、与文件内容相关的字段，且它本身只是一个文件系统路径字符串。

**为什么现在没有把 `redact_text()` 那套元数据脱敏惯例直接套用到全文内容索引上，是合理的克制，不是遗漏**——两份审计已经把这一点核实清楚，直接引用其结论：

- `redact_text()`（`build_context_digest.py:166-183`，模式定义在 34-72 行）是一个**针对短字符串的固定模式/形状拒绝列表**：PEM 私钥、URL userinfo、固定查询参数名、`Bearer` 头、固定厂商 token 形状前缀（`sk-`/`sk-ant-`/`gh[pousr]_`/`xox[baprs]-`/`AKIA`/`AIza` 等）、JWT 形状、固定 `key: value` 字段名列表、≥96 字符的 base64 疑似 blob、home 路径剥离，再配合 `trim_text` 做硬截断（标题/命令 300 字符、摘要窗口约 1200 字符）。它的设计目标和现有全部调用点都是**短、有界、结构简单的字符串**（标题、命令、从头尾窗口截出的小段摘录），从未被设计为处理完整对话记录这种数量级、自由格式、可能包含任意粘贴代码/配置/命令输出的内容。
- 如果不假思索地把它直接套在全文内容上，会产生三类具体失效，而不是"大体够用、还差一点"：**(a)** 超出截断长度的部分根本不会被扫描，只是被静默丢弃，等于对超长内容完全不脱敏；**(b)** 任何不匹配固定形状/固定字段名的敏感信息——自定义内部 token、嵌在句子中间的密钥、非常规命名的环境变量、姓名/邮箱/电话等个人信息、专有业务数据——会原样通过；**(c)** `redact_text()` 自身不做角色/内容类型过滤，现有 digest 管线对 `tool_use`/`tool_result` 载荷的排除是在更上游的 `content_text(content, allowed_types)` 完成的，不在 `redact_text()` 内部——如果直接把它指向一份未经过滤的原始记录，这层过滤也会一并丢失。
- 这与仓库现有政策的措辞完全一致，不是本附录新提出的限制：知识图谱"从不读取原始会话文本"、wiki 页面"不得含记录原文"、启动上下文包"从不复制原始记录"，都是明文写在 `SKILL.md` 里的既有边界；唯一允许对话内容出现的地方是 `build_context_digest.py` 产出的独立本地摘要，且被明确要求只写到用户本地可控路径、不得由 agent 在用户未明确要求时主动读取/总结。**未来如果要支持"按内容关键词搜索"这类更深功能**，需要的不是把 `redact_text()` 拿来直接套用，而是一整套专门针对长文本设计的脱敏/分类机制，加上一次比照第 2.2.3 节 L1/L3 已审阅包同等级别的、需要人工审阅通过的显式例外——**本附录不做这件事，也不建议在没有这套机制之前做**，只交付路径级引用，这个边界必须在实现时原样保留，不能被后续 PR 顺手扩大。

### A.6 小结

`record_path`/`record_on_ssd` 两个新字段把 Gap A（无法从图谱节点走到磁盘上的具体记录文件）关闭，辨别能力（按 provider、按具体账号/来源、按时间范围、按项目 cwd，外加按是否在 SSD 上）全部落在对既有 `knowledge_graph.json` 节点数组的直接过滤上——按账号这一维度复用已有的 `meta.source`（无需新增第三个字段，也避免了重新暴露账号池原始 UUID），不引入新的 edge 类型、不引入独立索引文件、不读取任何记录内容。落地前必须先补上 `Session` 数据类缺失的 `path` 字段这一前置缺口，否则 `build_catalog()` 拿不到源。Gap B/C/D 按既有政策原样保持不做。跨 source 的重复计数问题（风险 10）独立于本附录存在，但会在 `record_path`/`meta.source` 上线后首次变得**可被观察到**，实现时应当一并处理或至少在文档里显式标注为已知限制，不能只字不提。

### A.7 新增风险（延续第 6 节风险编号）

#### 风险 9（最高，源自本次审计的真实发现，已按符号链接解析修正）：把账号池里"目录不在 SSD 上"等同于"内容不在 SSD 上"

**已核实、精确点名**：`orca-codex-accounts-root`（`~/Library/Application Support/orca/codex-accounts/`，系统盘上的真实目录）下 4 个账号中，`0b4cd443-...`、`b9f32a51-...` 的 `home` 是指向 SSD 的符号链接。`235a57b1-...`、`8d7db875-...`（当前 `$CODEX_HOME` 实际绑定的账号）的 `home` 是系统盘上的真实目录，但这**不直接等于**其内容不在 SSD 上——真实代码路径（`Path.is_file()`，会跟随符号链接）现场核实：`235a57b1-.../home/sessions/` 下 308 个 `.jsonl` **全部**是指回 SSD 默认 home（`~/.codex/sessions/...`）的符号链接，解析后 100% 落在 SSD 上；`8d7db875-.../home/sessions/` 下有 310 个同样指回 SSD 的符号链接，**外加 81 个只存在于系统盘、没有 SSD 对应副本的常规文件（489MB）**。真正"不含糊地不在 SSD 上"的，只有这 81 个常规文件。

失败场景（两个方向都要防）：**(a)** 如果实现时图省事，只看"账号 home 目录本身是不是符号链接"就下结论——`235a57b1-...`/`8d7db875-...` 的 home 都是真实目录，若因此把这两个账号名下**全部**记录标成 `record_on_ssd=false`，会把 618 条实际已在 SSD 上的记录（308+310）错误标记为"需要额外迁移/备份"，制造大量假阳性，浪费运维核对成本，长期会让人对这个字段失去信任、开始忽略它。**(b)** 反过来，如果实现时假定"$HOME 是符号链接、所以账号池一切都在 SSD 上"，直接把 `record_on_ssd` 硬编码为 `true`、或者干脆不实现这个字段，那么 `8d7db875-...` 那 81 个真正只在系统盘的记录会被**系统性地、静默地**漏掉——任何后续依赖"完整记录已全部在 SSD 上"这一假设的下游动作（备份/归档范围、SSD 容量规划、迁移脚本的目标目录枚举、"退役 SSD 前先确认所有记录已迁出"这类检查）都会在真正需要那 81 条数据、而 SSD 已经不可用时才发现漏了。`record_on_ssd` 字段必须在发现阶段对**每一条记录、按 `record_path` 解析后的真实路径独立计算**（`Path.is_relative_to()`，见 A.3 的边界安全要求），不能对整个 provider、整个 home、或"目录本身是不是符号链接"做一次性的粗粒度假设。测试要求：至少覆盖 `.codex`（应为 `true`）、`.codex-profiles/acct-a`（应为 `true`）、账号池符号链接账号 `0b4cd443`（应为 `true`）、账号池 `235a57b1` 的符号链接条目（应为 `true`，不是 `false`）、账号池 `8d7db875` 的符号链接条目（应为 `true`）、账号池 `8d7db875` 的常规文件条目（应为 `false`）六种真实场景各一条断言，专门覆盖"账号目录本身是不是符号链接"和"文件内容解析后是否落在 SSD 上"这两层判断不能互相替代的情形。

#### 风险 10（新增，本轮复核发现的独立正确性问题）：账号池符号链接会让同一份物理会话被跨 source 重复索引、并被错误归属给错误的账号

`sync_sessions.py::all_primary_sessions()` 用 `seen_codex_homes` 只防止**同一个 home 目录路径**被重复加入 `codex_sources` 列表，但 `235a57b1-.../home`、`8d7db875-.../home` 各自是独立于默认 `~/.codex`（解析后指向 SSD）的路径，不会被这个集合拦下，因此都会作为独立 source 参与 `select_sessions()`。`select_sessions()` 内部的 `seen_session_ids` 去重**只在单次调用范围内生效**（即只在同一个 source 内部去重），`build_catalog()` 把多个 source 的结果拼进同一个 `sessions` 列表时**没有跨 source 的 session-id 去重**；`load_catalog()` 的唯一去重检查是 `(provider, source, id)` 三元组不能重复——但 `235a57b1`/`8d7db875` 各自的 `source` 值（`codex:orca-<hash>`）本来就和默认 `codex_home` 的 `source="codex"` 不同，所以同一个 session id（现场实例：`019cd44d-4806-7290-8562-275a97a502c5`）会以**三条不同的 catalog 条目**出现——一条 `source="codex"`（默认 home 直接发现），一条 `source="codex:orca-<hash of 235a57b1>"`（通过符号链接被 `235a57b1` 账号"发现"），一条 `source="codex:orca-<hash of 8d7db875>"`（通过符号链接被 `8d7db875` 账号"发现"）——这三条全部通过 `load_catalog()` 的唯一性校验，因为三元组确实互不相同。

这直接削弱 A.4 给 `meta.source` 赋予的"能单独挑出账号池里某一个具体账号"这个用途：`235a57b1`/`8d7db875` 名下通过 `find_sessions(source=...)` 筛出的结果，**很大一部分其实是默认 `~/.codex` home 的数据被符号链接重复带进来的**，不是这两个账号真正独有的会话；如果不加处理地把这类计数当作"这个账号有多少条记录"展示或用于统计，会产生系统性虚高，且虚高的比例（235a57b1 是 100%，8d7db875 是 310/391 ≈ 79%）在两个账号之间还不一样，容易被误读成"这两个账号本身活跃度差异"。缓解要求：在 A.3 的 `build_catalog()` 改动落地时，一并给账号池分支的 session 收集加一层"按解析后 `record_path` 去重、若与已有 source 的记录物理指向同一文件则跳过或合并标注"的逻辑（可以选择直接跳过、也可以选择保留但在条目上加一个 `record_duplicate_of` 之类的显式标注——具体取舍留给实现阶段决定，但**不能保持现状的静默重复**）；至少要作为已知限制显式写进实现文档，而不是等下游消费者自己发现计数对不上。测试要求：为"同一 session id 从默认 home 和至少一个账号池符号链接账号被两条 source 分别发现"这一具体场景写一条断言，确认最终产出（无论选择跳过还是标注）不会让读者在不知情的情况下把重复计数当作两条独立会话使用。

#### 风险 11（原风险 10，序号顺延）：`discover_codex_homes` 的静默跳过分支（`acct-c` 无 `sessions/` 目录）会让"账号存在但未被索引"和"账号确实没有记录"无法区分

`.codex-profiles/acct-c` 物理上确实存在，但因为没有 `sessions/` 子目录，被 `discover_codex_homes` 的 `(p/"sessions").is_dir()` 判断静默跳过——这本身是既有代码的既有行为，不是本附录制造的问题，但 A.3/A.4 的设计如果不专门处理这一点，会继承同一个盲区：`record_path`/`record_on_ssd` 这类新字段只能覆盖"被发现到的"记录，对于"账号目录存在但因缺 `sessions/` 而整个被跳过"的情形，索引里不会留下任何"这个账号被跳过了"的痕迹——读者看到 `acct-a`/`acct-b` 被索引、`acct-c` 完全不出现，无法区分"acct-c 从未产生过任何 Codex 记录"和"acct-c 的记录因为目录结构不符合发现逻辑的假设而被漏掉"这两种截然不同的情况。缓解要求：发现阶段对每一个被枚举到、但因缺少 `sessions/` 目录而跳过的 profile/账号，记录一条独立的、结构化的"发现被跳过"日志/清单条目（而不是仅仅让它在最终索引里无声消失），供后续人工确认是"确实无记录"还是"目录结构漂移导致漏发现"。测试要求：为 `acct-c` 这一具体场景写一条断言，确认它出现在"被跳过"清单里，而不是既不在索引里也不在任何日志里。

#### 风险 12（原风险 11，序号顺延，并补充比较运算符本身的正确性要求）：`record_on_ssd` 依赖的判断方式如果实现成裸字符串前缀比较、或挂载根常量写死两份，都会悄悄失真

这条风险其实包含两个必须分别测试的独立失效模式，不能只测一个就当作已覆盖：

- **比较运算符本身不安全**：A.3 已把 `record_on_ssd` 的判断方式明确写成 `Path.is_relative_to(ssd_root)`，而不是 `str(record_path).startswith(str(ssd_root))`。这不是风格偏好——裸字符串前缀比较没有路径分段边界，`/Volumes/Extreme SSD/Orca-decoy/...` 会被误判为落在 `/Volumes/Extreme SSD/Orca` 挂载根之下，纯属字符串巧合造成的假阳性。测试要求：至少覆盖一条"路径字符串以挂载根前缀开头、但下一个字符不是路径分隔符"的反例（如 `.../Orca-decoy/...`），确认 `record_on_ssd` 判定为 `false`。
- **挂载根常量本身可能写死两份并随时间漂移**：`record_on_ssd` 依赖一个"SSD 挂载根前缀"字符串（本机现场核实为 `/Volumes/Extreme SSD/Orca`）。如果实现时在 `sync_sessions.py` 里单独写死这个前缀，而仓库其他地方（安装器、备份闭环、per-workspace 环境脚本等）已经有另一份独立定义这个前缀的常量或配置项，两处定义会随时间漂移——例如 SSD 换盘后卷名变化、或本机之外的另一台机器挂载点不同——其中一处更新了、另一处没有，`record_on_ssd` 就会开始产出错误结果，而且是**看起来正常运行、不报错**的那种错误（判定为 `false` 的记录实际在 SSD 上，或反过来），比完全不实现这个字段更危险，因为它会被风险 9 描述的下游消费者当作可信信号使用。缓解要求：实现前必须先在仓库里检索是否已存在权威的 SSD 挂载根常量/配置（例如现有 SSD 相关 closure 目录、安装脚本里通常会有的路径配置），若存在则直接复用而不是重新声明一份字符串字面量；若确实不存在，本附录要求新增的这一份必须是仓库里**唯一**的定义来源，并在其定义处的注释里显式列出所有已知消费点，防止未来再长出第二份。

这两个失效模式必须分别写测试用例覆盖，缺一不可——比较运算符正确不能替代常量只有一份，反之亦然。
