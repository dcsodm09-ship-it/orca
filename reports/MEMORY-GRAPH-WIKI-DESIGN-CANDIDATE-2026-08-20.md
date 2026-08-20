# 候选设计（对抗性复核前）

# 记忆图谱与 Wiki 时间戳 / 新鲜度详细设计

*基于 AUDIT 1–4 既有结论撰写，不重新调查。范围：`wiki/orca-context-wiki.json`、`orca-context-bridge/scripts/build_knowledge_graph.py`、`.orca/context/reviewed-startup-pack-manifest.json` 及其校验路径 `build_startup_bundle.py::verify_reviewed_pack`。*

---

## 0. 设计前提与总体思路

四份审计报告共同指向同一个根因，不是"缺字段"而是**缺一个能真正被强制执行的写入纪律**：

- Audit 1：wiki 目录里没有任何时间戳字段，14 个页面里至少 5 个内容已经和它引用的源文件/脚本实际状态不一致，而且没有任何机制能提示读者"这条可能过期了"。
- Audit 2：知识图谱里已经零散存在 3 个原始时间戳字段（`session.updated_at`、`pr.updated_at`、`reviewed_knowledge.signed_at`），但都是未校验的原样透传，7 个节点类型完全没有时间信息，边（edge）从未带时间戳。
- Audit 3：仓库里唯一真正生效的"新鲜度"机制是 `verify_reviewed_pack` 的纯哈希比对（fail-closed，正确），但它和 wiki 的实际编辑动作之间没有任何联动——wiki 改了没人重新签名，10 小时后才在 session 启动时被动发现。
- Audit 4：wiki 只覆盖了这个仓库的"测试/验证层"，覆盖不到实际工程交付物（memory bridge、Prime Agent、SSD/R2 闭环家族等），而且当前的扁平 `pages[]+links[]` 结构本身撑不住这些新增内容。

因此整份设计分两条线并行，不能只做其中一条：

1. **时间戳线**：给 wiki 页面/链接、知识图谱节点，加上语义明确、来源可追溯的时间戳字段（第 1、5 节）。
2. **写屏障线**：把新增的时间戳字段真正接入 `verify_reviewed_pack` 的校验逻辑，堵住 Audit 3 发现的"改了内容、没重新钉哈希"这个具体漏洞（第 2 节）。

第 3、4 节分别处理存量数据迁移和内容扩充路线；第 6 节列出全文中风险最高、最容易被做错的点。

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

这四者互不可替代：`updated_at` 新不代表 `verified_at` 新（Audit 1 里 `resource-gate` 页面的 `updated_at` 是 8-10，但它描述的脚本行为已经在 8-16 被彻底改变，`verified_at` 应该早就该被标记为过期）；`source_mtime` 新也不代表内容真的变了（复制/touch 不改内容）。三者必须是三个独立字段，不允许用一个字段兼职表达。

### 1.2 数据类型与格式：ISO 8601 UTC，秒精度，`+00:00` 后缀

**格式**：`YYYY-MM-DDTHH:MM:SS+00:00`（UTC，秒精度，`isoformat()` 原生输出，不用 `Z` 后缀）。

**理由**：Audit 2 已经指出这是仓库现有的实际约定——`build_context_digest.py` 第 710 行和 `build_knowledge_graph.py` 第 1087 行都独立写了同一个表达式 `datetime.now(timezone.utc).replace(microsecond=0).isoformat()`，产出的就是这个格式。新字段必须跟随既有约定，不能引入 `Z` 后缀或毫秒精度这类第二种风格——否则会在同一个仓库里制造两套时间格式，任何做字符串比较或排序的下游代码都要多处理一种变体，纯属自找麻烦。已存在的 `parse_timestamp`（`build_context_digest.py` 139-155 行）本来就同时接受 `Z` 和 `+00:00` 两种输入并统一转成 UTC-aware `datetime`，所以**读入时兼容、写出时统一**：任何要落盘的新时间戳字段，写之前必须过一遍 `parse_timestamp`，再用上述表达式重新格式化后写出，不允许把上游原始字符串直接透传落盘（这正是 Audit 2 指出的现有 3 个字段的问题所在，新字段不能重犯）。

`created_at`/`updated_at` 字段类型为**必填字符串**（ISO 8601）；`verified_at` 类型为**可空字符串**（`null` 表示"从未核实"，见 1.4）；`source_mtime` 类型为**可空字符串**（`null` 表示"该条目无单一物理源文件可对应"，例如描述一整个策略而非某个文件的页面）。

### 1.3 每一类节点/页面的具体获取策略（不笼统带过）

#### wiki `pages[]`

| 情形 | `created_at` 来源 | `updated_at` 来源 | `source_mtime` 来源 |
|---|---|---|---|
| `path` 指向的文件在 git 中有提交历史（目前仅 `agent_capacity.py` 一例） | `git log --follow --format=%aI --diff-filter=A -- <path> \| tail -1`（首次新增该文件的提交时间） | `git log -1 --format=%aI -- <path>`（最近一次提交时间） | `os.stat(path).st_mtime` 仍然单独记录，作为交叉校验，两者理论上应接近但不强制相等 |
| `path` 指向的文件是 git 未跟踪文件（当前 13/14 属于此类） | **不可从 git 获得**；写入 wiki 该条目那一刻的时间（由编辑该条目的脚本/流程打戳，即"这条 wiki 记录本身第一次出现的时间"，而不是源文件的创建时间——两者本来就是两件事，不要混淆） | 每次该 page 对象的 `title`/`summary`/`status`/`path` 任一字段值发生 diff 时，由写入该 wiki 文件的脚本重新打戳（见 3.1 迁移脚本的同一套逻辑，后续所有编辑必须走这条路径而不是手工改 JSON） | `os.stat(path).st_mtime`，直接读取 |
| page 描述的是**探针/脚本的运行结果**而非静态文档（`orca-capability-tests`、`orca-readonly-probe`、`orca-cli-capability-catalog`） | 同"未跟踪文件"规则 | 同上，但额外要求：这类页面的 `summary` 中**任何具体数字（如"16/18 通过"）必须与 `verified_at` 绑定出现**，不允许写死一个数字却不说明是哪次运行产生的（这正是 Audit 1 发现三处数字互相矛盾的直接原因——数字脱离了产生它的时间点） | 若该页面同时引用了一个 JSON 结果文件（如 `orca-cli-capability-inventory.json`），取该文件的 mtime；若只是文字描述、没有单独产物文件，则为 `null` |
| page 描述的是**策略/规则文档**（`ego-global-policy`、`codex-claude-bypass-dispatch` 等） | 同"未跟踪文件"规则 | 同上 | 同上 |

**`verified_at` 的获取策略统一，不分情形**：只能由以下两种途径产生，二选一，且必须记录是哪一种：
1. **人工审查**：人读了源文件/重新跑了探针，确认 `summary` 仍然成立，手动调用一个专门的核实命令（如 `wiki_verify.py --page <id> --by <人名或账号>`）来打戳，禁止直接手改 JSON 里的 `verified_at` 字段（防止顺手写一个假值）。
2. **自动化验证脚本**（仅适用于"探针类"页面，如 `orca-readonly-probe`）：脚本重新跑一遍 `orca_readonly_probe.py`，把新结果和 `summary` 里记录的结论做字符串/数值比对，**完全一致才自动打 `verified_at`**，不一致则脚本必须报错退出并**不**打时间戳，同时把新旧结果的 diff 打印出来供人工决定是否更新 `summary`。这类脚本要新增（不在本次范围内实现，但设计上必须存在，否则 `verified_at` 永远只能人工填，跟不上探针类页面的更新频率）。

#### wiki `links[]`

`created_at`：链接第一次出现在 `links[]` 数组中的时间，由编辑脚本在追加该条目时打戳。

`verified_at`：一条 link 描述的是 `from` 页面和 `to` 页面之间"关系仍然成立"，这个关系的正确性依赖于两端页面内容本身没有漂移。因此设计一条**派生约束而非独立获取动作**：

```
link.verified_at 只能被设置为 min(from_page.verified_at, to_page.verified_at)
（若任一端为 null，则 link.verified_at 也必须为 null）
```

即链接的可信度永远不能超过它连接的两个页面里更旧/更未核实的那一个——不允许一条 link 声称"已核实"而它两端的页面之一从未被核实过。这条约束由写入脚本强制执行，不接受手工覆盖。

#### 知识图谱节点（`build_knowledge_graph.py`）

按 Audit 2 列出的 13 种节点类型逐一给策略（统一放进第 5 节的 `timestamps` 子对象里，这里先定获取来源）：

| 节点类型 | `content_at` 来源 | `source_mtime` 来源 | `observed_at` 来源 |
|---|---|---|---|
| `session` | `entry.get("updated_at")`，**必须先过 `parse_timestamp` 规范化**（当前是未校验透传，这是 bug，第 5 节会给出具体 diff 修正） | 该 session 记录所在的索引产物文件（`sync_sessions.py` 输出）的 mtime | 本次 `build_knowledge_graph.py` 运行的 `generated_at` |
| `pr` | `pr.get("updated_at")`，同样必须过 `parse_timestamp` | GitHub 索引产物文件（`index_github.py` 输出）的 mtime | 同上 |
| `reviewed_knowledge` | `payload.get("authority_signed_at")`，过 `parse_timestamp` | manifest 文件自身 mtime | 同上 |
| `wiki`（GitHub 与 local 两种 scope 都一样） | **无自然事件时间，设为 `null`**；不要伪造一个"GitHub wiki 页面理论上应该有 updated_at"就去猜一个值出来——`add_github`/`add_local_wiki` 现在根本没读取这个字段，宁可显式 `null` 也不要在没有真实数据源的情况下编造 | local wiki 取 `wiki/orca-context-wiki.json` 自身 mtime；GitHub wiki 若 API payload 本身带有该页时间字段则用它（需要 `index_github.py` 先采集，不在本次范围，先留 `null`） | 同上 |
| `project` | `null`（静态标识节点，没有"内容变化"的概念） | `null` | 同上 |
| `pane` / `terminal` | `null`（现有 `--processes` 输入不带任何时间字段，见 Audit 2 已确认） | 若来源快照文件本身有 mtime（进程快照通常是即时生成，`source_mtime` 约等于 `observed_at`，此时允许两者相等，不视为冗余） | 同上 |
| `agent_process` | `null`（`etime` 是持续时长不是绝对时间，不能拿来冒充 `content_at`；如果未来想要真正的起始时间，必须让上游 `--processes` 采集脚本自己算 `now - etime` 再作为新字段传入，本次不做，因为那属于实现层改动而非设计层可以直接断言的事） | 同上 | 同上 |
| `capability` | `null`（静态审阅目录条目） | 能力目录 JSON 文件的 mtime | 同上 |
| `code_graph` | `null` | `graph_path` 指向的 Graphify 产物文件 mtime | 同上 |
| `route` / `skill` | `null` | `skill` 节点有 `path`/`sha256`，取该 `path` 的 mtime；`route` 节点本身是目录条目，取路由目录 JSON 文件 mtime | 同上 |

**边（edges）不加时间戳字段**——这是一个明确决策而非遗漏，理由见第 5.3 节。

### 1.4 是否需要区分这几种时间——结论

**需要，且必须四个字段都落地，不能只加一个笼统字段。** 理由直接来自 Audit 1/3 的证据：`resource-gate` 页面如果只有一个 `updated_at`（2026-08-10），读者会以为它是新的；但真正需要的信息是"这条描述自 2026-08-16 `agent_capacity.py` 改动后，从未被人核实过"——这个信息只有 `verified_at=null` + `source_mtime`(agent_capacity.py 的) 晚于 `updated_at`(wiki 页面的) 才能被结构化地表达出来。单一字段无法承载这个对比。

---

## 2. 新鲜度校验机制与现有 hash-pinning 的结合

### 2.1 不能做的事（明确排除，呼应 Audit 3 的第 5 点建议）

- **不能**用时间戳（哪怕是 `verified_at`）替代或弱化 `verify_reviewed_pack` 现有的哈希相等判断。哈希判断是 fail-closed 的，是对的，必须原样保留。
- **不能**引入基于墙钟的 TTL（"距上次签名不超过 N 小时就算新鲜"）。Audit 3 已经论证过这个方案的两难：TTL 短了会对合法的慢变更 wiki 频繁误报；TTL 长了会在窗口期内悄悄放行一个已经被篡改/过期的 wiki，比现状（哈希不匹配就直接拒绝）更差。**本设计不采用任何 TTL 方案。**

### 2.2 采用的方案：内容版本号绑定 + 编辑时写屏障（双层防御）

核心思路分两层，缺一不可：

**第一层——检测层（写屏障，事前）**：让"编辑 wiki 内容"这个动作本身强制要求编辑者同步递增一个版本计数器；如果版本计数器没有递增但内容哈希变了，编辑脚本直接拒绝写入。这一层的目的是让"忘记重新签名"这件事**在编辑发生的那一刻**就被发现，而不是等 10 小时后 session 启动失败才发现。

**第二层——验证层（会话启动时校验，事后兜底）**：`verify_reviewed_pack` 除了比对哈希，再多比对一个内容版本号；哈希和版本号任一不匹配都 fail-closed。这一层是对第一层失效（比如有人绕过编辑脚本直接改了 JSON）的兜底。

#### 2.2.1 wiki JSON 增加 `meta` 子对象（具体 schema 见第 5.4 节）

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
- 只改时间戳字段本身（比如跑核实脚本只更新某页的 `verified_at`）**不**触发 `content_version` 递增——因为这不改变"内容宣称的事实"，只是确认它仍然为真。这个区分很重要：否则 `content_version` 会被核实动作污染，变成没意义的计数器。

#### 2.2.2 写屏障脚本 `wiki_edit_guard.py`（设计层描述，不在本次实现）

任何编辑 `wiki/orca-context-wiki.json` 的路径（无论是人工还是脚本）都必须经过这个守卫，而不是直接写文件：

```
步骤 1：读取当前磁盘上的 wiki JSON，记为 old
步骤 2：接受调用方提交的新内容，记为 new
步骤 3：计算 old 与 new 在 pages[]/links[] 层面的语义 diff（忽略纯时间戳字段的变化）
步骤 4：
    如果语义 diff 非空：
        断言 new.meta.content_version == old.meta.content_version + 1
        断言 new.meta.updated_at 晚于 old.meta.updated_at
        若断言失败 → 拒绝写入，报错退出，不落盘
    如果语义 diff 为空（只是时间戳更新）：
        断言 new.meta.content_version == old.meta.content_version（不变）
步骤 5：断言通过后，原子写入（沿用 write_private 的 temp-file + os.replace 模式，第 5.2 节复用同一套 I/O 纪律）
步骤 6：写入成功后，打印明确提示：
    "wiki content_version 已从 N 升至 N+1，manifest 需要重新签名，请运行：
     python3 sign_reviewed_authority.py --source wiki ..."
    并以非零退出码结束（如果被当作 pre-commit hook 调用，这会阻止 commit 落地，
    除非调用方明确加 --acknowledge-resign-pending 之类的显式确认标志）
```

这个脚本**只做检测和阻断，不做自动重签**——重签必须走 `sign_reviewed_authority.py` 现有的非自签、≥300 秒陈化约束，这是安全权威机制，不能被写屏障脚本绕过（否则写屏障本身就变成了一个可以被脚本自己签发信任的后门，比现状更危险）。

#### 2.2.3 manifest 增加内容版本绑定字段

`.orca/context/reviewed-startup-pack-manifest.json` 里 `shared_source_sha256s.wiki` 从纯字符串升级为对象（其余 `capabilities`/`graphify_catalog` 暂不需要这个升级，因为它们没有"内部版本号"这个概念，保持字符串形式即可，见 5.4 节的兼容处理）：

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

#### 2.2.4 `verify_reviewed_pack` 校验逻辑（具体到可以直接实现）

```python
def verify_source_binding(name, path, expected):
    observed_hash = stable_regular_file_sha256(path)

    if isinstance(expected, str):
        # 旧格式（capabilities / graphify_catalog 保持不变）：纯哈希比对
        if observed_hash != expected:
            raise ValueError(f"central reviewed source freshness mismatch: {name}")
        return

    if isinstance(expected, dict):
        # 新格式（目前仅 wiki）：哈希 + 内容版本号，两者都必须匹配
        if observed_hash != expected.get("sha256"):
            raise ValueError(f"central reviewed source freshness mismatch: {name} (hash)")

        observed_payload = json.loads(read_bytes(path))
        observed_version = observed_payload.get("meta", {}).get("content_version")
        expected_version = expected.get("content_version")

        # 版本号缺失/None 一律视为不匹配，不允许"缺失就放行"
        if observed_version is None or expected_version is None or observed_version != expected_version:
            raise ValueError(
                f"central reviewed source freshness mismatch: {name} "
                f"(content_version observed={observed_version} expected={expected_version})"
            )
        return

    raise ValueError(f"malformed manifest binding for source: {name}")
```

关键设计点，直接回应 Audit 3 的"具体校验逻辑"要求：

- **哈希不匹配和版本号不匹配都必须 fail-closed**，报错信息里要分别注明是哪一种不匹配（便于诊断，不能笼统只说 "freshness mismatch"）。
- **`content_version` 缺失被当作不匹配处理，不是当作"跳过检查"处理**——这是防止未来有人误改出一个"缺字段就放行"的宽松分支（这正是第 6 节风险 1 要专门测的反例）。
- **旧格式（str）和新格式（dict）必须并存**，因为 `capabilities`/`graphify_catalog` 没有内部版本号概念，不应该被强迫套用同一个 schema；`isinstance` 分支判断是简单且明确的兼容方式，不需要额外的 `schema_version` bump 就能做到向后兼容（现有 v3/v4 判断逻辑不受影响，这个改动是在字段值的形状上做区分，不是在顶层 schema_version 上）。

#### 2.2.5 CI/周期性检查（独立于 session 启动）

除了写屏障和 session 启动校验，额外提供一个可以随时手动/CI 调用的独立命令 `check_wiki_freshness.py`（设计层要求存在，复用 2.2.4 同一段校验函数），让"wiki 改了但没重签"这件事不必等到下一次 agent session 启动才被发现，缩短 Audit 3 说的"10 小时缺口"到接近于零。

---

## 3. 存量条目迁移策略

### 3.1 结论先说：`source_mtime` 回填真实值，`verified_at` 一律标记为"未核实"，不伪造

这不是各打五十大板的折中，是两个独立决策，理由分别给出：

**`source_mtime`：回填真实值。** 这是纯粹的文件系统事实（`os.stat().st_mtime`），不涉及任何"这个内容对不对"的判断，回填它不会制造虚假信任，只会让读者多一个可以自行判断的参考信号。所有 14 个页面直接用 `os.stat(path).st_mtime` 回填即可，风险为零。

**`created_at`：能拿到 git 历史的用 git，拿不到的记录"写入 wiki 目录那一刻"（即本次迁移执行的时间），而不是伪装成源文件的创建时间。** 13/14 个文件在 git 里没有任何提交记录（Audit 1 已确认），无法得知它们"真正"是什么时候创建的；与其编一个基于 mtime 的假创建时间（mtime 完全可能是最后一次编辑时间而非创建时间，两者常年混淆但语义不同），不如诚实记录"这条 wiki 记录本身是什么时候第一次被目录化的"——这本身就是一个真实、有意义的事件，只是不等于源文件的创建时间，字段名和文档要写清楚这个区别。

**`verified_at`：一律设为 `null`，并新增 `verification_status: "unverified"`，绝不用 mtime 或迁移执行时间冒充。** 这是本节最关键的决策，理由直接来自 Audit 1 的实证：迁移执行的这一刻，`resource-gate`、`capacity-preflight`、`orca-capability-tests`、`orca-readonly-probe`、`orca-cli-capability-catalog` 五个页面已经被证明内容与现实不符。如果迁移脚本图省事把"迁移执行时间"填进 `verified_at`，就等于对外宣称"这些错误的内容在今天被核实过"——这比完全没有 `verified_at` 字段更糟，因为它会被下游消费者（包括第 2 节设计的自动化校验、包括未来读这份 wiki 的 agent）当作真实的核实记录来信任。**没有核实的字段就应该诚实地是 `null`，宁可信息不全，不可信息虚假。**

### 3.2 迁移脚本行为（设计层规格）

```
对 wiki/orca-context-wiki.json 中每一个 page：
    source_mtime ← os.stat(page.path).st_mtime，转 ISO8601
    若 path 在 git 中有提交历史：
        created_at ← git log 首次提交时间
        updated_at ← git log 最近提交时间
    否则：
        created_at ← 本次迁移执行时刻（记录清楚这是"目录化时间"不是"源文件创建时间"）
        updated_at ← 本次迁移执行时刻
    verified_at ← null
    verification_status ← "unverified"

对每一条 link：
    created_at ← 本次迁移执行时刻（link 本身也没有历史可考）
    verified_at ← null（依据 1.3 节的 min() 约束，两端页面都是 null，link 也必然是 null）

写入顶层 meta：
    content_version ← 1（迁移本身视为一次内容确定的起点）
    updated_at ← 本次迁移执行时刻
```

### 3.3 迁移之后的必要后续动作（不是可选项）

迁移只是让 schema 完整，**不代表内容变准确**。Audit 1 已经明确指出 5-6 个页面内容本身就是错的（不是"缺时间戳"，是"写的东西不对"）。这些必须作为迁移之后**立刻排期**的独立任务去做真正的内容修正 + 人工核实（把 `verification_status` 从 `unverified` 提升为 `verified` 并填入真实 `verified_at`），具体列在第 4.2 节。**不能让这批已知错误的页面停留在"结构完整但内容仍然是错的"这个状态太久**，否则时间戳字段本身反而会造成"这个 schema 看起来很完善"的假象，掩盖内容问题没解决的事实。

---

## 4. Wiki 完善路线

### 4.1 新增页面清单（按优先级排序，14 条，≤15 条要求内）

**P0：信任根，必须最先补齐**

1. `id: startup-reviewed-authority-pipeline`
   `title: Startup 可信上下文交付管线`
   `summary: 描述 startup_context.py + install_startup_injection.py + .orca/context/reviewed-startup-pack-manifest.json 组成的会话启动可信交付链路：ORCA_CONTEXT_NACK_V1 challenge-response、schema v3/v4 必需源集合、300 秒 ACK TTL、以及 shared_source_sha256s 内容哈希钉住机制。这是仓库其余全部信任声明的根，当前完全没有 wiki 覆盖。`

2. `id: reviewed-authority-signing`
   `title: 可信内容签名与重新钉哈希流程`
   `summary: sign_reviewed_authority.py 的签名流程与约束（signed_by 不可自签、签名需 ≥300 秒陈化）、内容变更后必须重新钉哈希的操作步骤；直接对应本设计第 2 节写屏障机制，是维护 wiki/图谱新鲜度必须执行的配套动作。`

3. `id: context-bridge-pipeline-overview`
   `title: orca-context-bridge 核心管线总览`
   `summary: 串联 sync_sessions.py / build_context_digest.py / index_processes.py / index_github.py / build_knowledge_graph.py / auto_index.py / install_hook.py / tmux_bridge.py 的数据流与职责边界，替代当前 wiki 只描述侧路探针（agent_capacity.py 等）却遗漏主管线本体的现状。`

**P1：仓库里工作量/评审量最大的两个子系统**

4. `id: claude-codex-memory-bridge`
   `title: Claude-Codex 记忆桥接`
   `summary: install_bridge.py / claude_memory_hook.py / write_candidate_capture.py 三件套构成的跨模型写触发自动学习管线；经过 14+ 轮 opus+sol 双 review 定案，2026-08-19 用户授权实际安装、2026-08-20 完成脱敏修复后重新安装并生效。`

5. `id: prime-agent-integration`
   `title: Prime Agent 集成与安全加固`
   `summary: install_prime_agent.py 的 17 轮安全评审，含一次被连续 7 轮定位并关闭的真实 RCE 链条，最终 Claude opus/max + Codex sol/xhigh 双 GO，但因上游 npm 依赖漂移目前仍未安装（阻塞态）。`

6. `id: route-catalog-context-route`
   `title: 能力路由目录 (context_route.py)`
   `summary: capability→project 精确哈希绑定解析器：source_drift 检测到不一致即 fail-closed、区分候选与已审阅两种查询模式；是路由安全的关键守门组件，目前 wiki 完全未提及。`

7. `id: completion-backlog-master-index`
   `title: COMPLETION-BACKLOG 主索引`
   `summary: 指向 reports/COMPLETION-BACKLOG-2026-08-16.md 的九大编号领域目录（启动/hook注入、SSD原生存储、R2/归档/Android退役、Paperclip、记忆/知识图谱、Prime Agent、桌面/浏览器、兄弟worktree），作为 wiki 与仓库现有全局索引之间的显式交叉链接锚点，避免两套索引长期互不知情。`

**P2：几个规模相当的领域家族**

8. `id: ssd-hardening-closure-family`
   `title: SSD 原生存储加固闭环`
   `summary: ssd-native-storage-closure / ssd-runtime-closure / ssd-pty-lifetime-closure / ssd-runtime-remote-policy-independent-acceptance 四个目录构成的多轮加固工作，覆盖原生存储、PTY 生命周期与运行时策略的 SSD 安全性。`

9. `id: r2-archive-retirement-family`
   `title: R2 备份与归档/Android 退役闭环`
   `summary: r2-run-identity-closure / r2-production-trust-closure / r2-public-only-acceptance / archive-provenance-normalization-closure / android-retained-retirement-closure；状态均为 GO_OFFLINE_CANDIDATE_ONLY、尚未安装的备份与退役领域。`

10. `id: startup-installer-loader-family`
    `title: 启动安装器/加载器/事务闭环家族`
    `summary: startup-installer-closure / startup-loader-closure / startup-transaction-closure(+v2) / startup-p1-hardlink-closure / startup-permission-remediation-closure / startup-reviewed-pack-closure(+schema3-fastfix)；产出 reviewed-startup-pack-manifest.json 本身的底层写入机制，是页面 1 描述的管线的实现基础。`

11. `id: orchestration-dynamic-scheduler`
    `title: 编排动态调度器`
    `summary: scheduler.py + ORCA-NATIVE-ARCHITECTURE.md：账号绑定、Sol/Terra/Luna 模型路由、与容量门限叠加的调度策略层；需与既有 orchestration-lifecycle / resource-gate 页面互链，目前两者互不知道对方存在。`

12. `id: paperclip-integration-family`
    `title: Paperclip 第三方集成闭环`
    `summary: paperclip-orca-closure / paperclip-privacy-gate-closure / paperclip-skill-candidate / paperclip-orca-independent-acceptance；隐私门控与独立验收覆盖的第三方集成域。`

13. `id: review-orca-workflow-learning-skill`
    `title: review-orca-workflow-learning 元技能`
    `summary: skilld.py / event_journal.py / telemetry_projection.py / memory_eval.py / startup_admission.py / policy_gate.py / attestation_verify.py / method_ledger.py / release_identity.py 等组成的独立学习元技能，与 orca-context-bridge 并列存在，目前 wiki 从未提及。`

14. `id: dual-review-acceptance-protocol`
    `title: 双模型复核与验收协议词汇表`
    `summary: GO_OFFLINE_CANDIDATE_ONLY / NO-GO / ERRATUM / round 编号约定等贯穿几乎所有 closure 目录的验收生命周期词汇，首次作为一等概念被独立文档化，而不是仅在 codex-claude-bypass-dispatch 页面里顺带提及。`

### 4.2 现有页面需要更新的清单

| id | 需要的更新 | 依据 |
|---|---|---|
| `resource-gate` | 移除"红灯不启动新重型 worker"的强制性表述，改为准确描述 `agent_capacity.py` 76e7d88ccb 之后的 `gate_removed` 实际行为，并显式标注这与用户全局 CLAUDE.md 规则的措辞存在已知冲突（不要试图在 wiki 里悄悄"修正"用户规则，只需如实标注冲突，交给人决定） | Audit 1 §3 |
| `capacity-preflight` | 同上，精确描述当前输出结构（顶层 `recommendation` 固定值 + 被降级到 `advisory_true_recommendation` 的真实红黄绿计算） | Audit 1 §3 |
| `orca-capability-tests` | 把"16/18"这类写死的证据数字改为不再硬编码，必须与产生该数字的 `verified_at` 绑定出现（依据 1.3 节的规则） | Audit 1 §2、§3 |
| `orca-readonly-probe` | 同上，更新为最新探针结果并附 `verified_at` | Audit 1 §2、§3 |
| `orca-cli-capability-catalog` | 说明其 `liveProbe` 快照已过期且与另外两处结果不一致，需要重新跑一次生成脚本后再更新 | Audit 1 §3 |
| `desktop-mcp-realtime-control` | 修正 benchmark 数据的归因——该数字实际来自 `wiki/Orca能力测试与知识图谱.md` 而非页面自身 `path` 指向的 `desktop-mcp/README.md`，需要在 summary 里明确标注数据来源页面，或迁移数据到正确文件 | Audit 1 §2（表格倒数第二行）、§3（Minor） |
| `orchestration-lifecycle` / `resource-gate` | 各增加一条到新页面 `orchestration-dynamic-scheduler` 的 link（relation 建议为 `overlaps_with`） | Audit 4 §2 |
| `ego-capability-regression` | 标记 `verification_status: unverified`（而非直接判定为过时或直接判定为仍然正确），交给下一轮人工核实 | Audit 4 §2 |

---

## 5. 知识图谱 schema 调整

### 5.1 是否需要统一 `timestamps` 子对象——需要，理由

7 个节点类型目前完全没有时间信息，另外 3 个节点类型有时间信息但格式不统一、未校验。与其在每个节点类型上各自加不同名字的字段（`session.updated_at` vs 某个新节点自己取名 `last_seen`之类），不如**统一成一个 `timestamps` 子对象**，字段名固定为 `content_at`/`source_mtime`/`observed_at`（语义见 1.3 节），所有节点类型都有这三个键，值可以是 `null` 但键必须存在——这样任何消费这份图谱的下游代码只需要认识一种形状，不用为每种节点类型写不同的时间戳读取逻辑。

### 5.2 具体 diff（清楚到可以直接照着改代码）

**新增一个辅助函数**（放在 `add_edge`/`add_node` 附近）：

```diff
+def make_timestamps(content_at=None, source_mtime_ns=None, observed_at=None):
+    """构建标准 timestamps 子对象。content_at 若非空必须先过 parse_timestamp。"""
+    normalized_content_at = None
+    if content_at:
+        try:
+            normalized_content_at = parse_timestamp(content_at).isoformat()
+        except (ValueError, TypeError):
+            normalized_content_at = None  # 无法解析的原始值一律丢弃为 null，不做静默猜测
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

**`load_regular_source` 把已经算出来却被丢弃的 mtime 保留下来**：

```diff
 def load_regular_source(path, ...):
     ...
     raw = fd_read(...)
     st_after = os.fstat(fd)
     ...
     record = {
         "path": str(resolved),
         "sha256": hashlib.sha256(raw).hexdigest(),
         "size_bytes": len(raw),
+        "source_mtime_ns": st_after.st_mtime_ns,
     }
     return raw, record
```

**`load_json_source` 透传这个新字段到 `sources[name]`**（同时也让整份图谱的"这个源文件整体多新"变得可见，回应 Audit 2 的"near-miss"发现）：

```diff
 def load_json_source(path, ...):
     raw, record = load_regular_source(path, ...)
     payload = json.loads(raw)
     for key in ("generated_at", "version", "schema_version", "schemaVersion", "authority"):
         value = payload.get(key)
         if isinstance(value, (str, int)) and not isinstance(value, bool):
             record[key] = value
     return payload, record
```
（此函数本身不用改，`source_mtime_ns` 已经通过 `record` 字典自动带过来；只需在最终写出 `sources[name]` 时把 `source_mtime_ns` 转成 ISO8601 字符串对外展示，键名从内部的 `source_mtime_ns` 重命名为对外的 `source_mtime`。）

**`add_node` 签名增加 `timestamps` 参数**：

```diff
-def add_node(self, id_, type_, label, meta):
-    node = {"id": id_, "type": type_, "label": label, "meta": meta}
+def add_node(self, id_, type_, label, meta, timestamps=None):
+    node = {
+        "id": id_, "type": type_, "label": label, "meta": meta,
+        "timestamps": timestamps or make_timestamps(observed_at=self.generated_at),
+    }
     self.nodes.append(node)
```

**三处已有原始时间戳字段的调用点，改为规范化后放进 `timestamps`，同时从 `meta` 里移除原始透传**（避免同一份数据出现两份、格式还不一样）：

```diff
 # add_sessions
     meta = {
         "provider": provider,
         "source": source,
         "cwd": normalized_cwd,
-        "updated_at": entry.get("updated_at"),
     }
-    self.add_node(node_id, "session", label, meta)
+    self.add_node(node_id, "session", label, meta, timestamps=make_timestamps(
+        content_at=entry.get("updated_at"),
+        source_mtime_ns=sessions_source_record.get("source_mtime_ns"),
+        observed_at=self.generated_at,
+    ))
```

```diff
 # add_github, pr 节点
     meta = {
         "number": pr["number"],
         "head": pr.get("head"),
         "base": pr.get("base"),
-        "updated_at": pr.get("updated_at"),
         "body_preview": redact_text(pr.get("body", ""), home, LIMIT),
     }
-    self.add_node(pr_id, "pr", label, meta)
+    self.add_node(pr_id, "pr", label, meta, timestamps=make_timestamps(
+        content_at=pr.get("updated_at"),
+        source_mtime_ns=github_source_record.get("source_mtime_ns"),
+        observed_at=self.generated_at,
+    ))
```

```diff
 # add_reviewed_manifest, reviewed_knowledge 节点
     meta = {
         "authority": authority,
         "schema_version": schema_version,
         "pack_sha256": pack_sha256,
         "pack_size_bytes": pack_size_bytes,
-        "signed_at": payload.get("authority_signed_at"),
         "source_bindings": source_bindings,
         "reference_only": True,
     }
-    self.add_node(node_id, "reviewed_knowledge", label, meta)
+    self.add_node(node_id, "reviewed_knowledge", label, meta, timestamps=make_timestamps(
+        content_at=payload.get("authority_signed_at"),
+        source_mtime_ns=manifest_source_record.get("source_mtime_ns"),
+        observed_at=self.generated_at,
+    ))
```

**其余没有自然事件时间的节点类型**（`project`/`pane`/`terminal`/`agent_process`/`capability`/`code_graph`/`wiki`/`route`/`skill`），统一模式：

```diff
-self.add_node(node_id, "wiki", label, meta)
+self.add_node(node_id, "wiki", label, meta, timestamps=make_timestamps(
+    content_at=None,
+    source_mtime_ns=source_record.get("source_mtime_ns"),
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

**`GRAPH_VERSION` 从 2 提升到 3**（输出形状发生了变化，节点新增 `timestamps` 键，`sources[name]` 新增 `source_mtime` 键，任何依赖固定 v2 形状的下游消费者需要显式适配——这本身也是第 6 节要专门提示的风险点）：

```diff
-GRAPH_VERSION = 2
+GRAPH_VERSION = 3
```

### 5.3 边（edges）为什么不加时间戳——明确决策，不是遗漏

考虑过给 `reviews` 这类由 `reviewed_knowledge` 发出的边额外加一个 `valid_at`，但决定不做，理由：`reviews` 边的有效性完全可以通过它连接的 `reviewed_knowledge` 节点自身的 `timestamps.content_at`（即 `signed_at`）反查得到，没有必要在边上再复制一份同样的值。**复制同一份时间戳到两个地方，本身就制造了一个新的"两处数据可能漂移"的风险点**——这正是 Audit 1/3 花了大量篇幅证明会真实发生的那类问题（wiki 内容和 manifest 哈希两处记录不同步）。因此本设计的原则是：**时间戳只在一个地方存一份，边通过端点节点间接获得时间信息，不做冗余存储**。所有其他 relation 类型同理，边 schema 保持 `{"from", "to", "relation"}` 三键不变。

### 5.4 wiki JSON 与 manifest 的 schema diff 汇总（第 2、3 节已给出具体字段，此处汇总成一张完整示意）

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
+      "source_mtime": "2026-08-15T01:35:14+00:00"
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

```diff
 {
   "schema_version": 4,
   "authority": "orca-central-reviewed-l1-l3",
   "authority_signed_at": "...",
   "signed_by": "...",
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
   ...
 }
```

---

## 6. 风险最高、最容易做错的地方

### 风险 1（最高）：`verify_reviewed_pack` 的新旧格式兼容分支写错，会把 fail-closed 悄悄改成 fail-open

这条改动直接触碰仓库里唯一真正生效的安全权威校验路径，属于用户全局规则里明确要求"安全/发布/最终验收"必须 Claude opus+max 与 Codex sol+max 双路独立只读复核的范畴，且必须通过 `sign_reviewed_authority.py` 的非自签流程，不能自己签自己。最容易犯的具体错误：

- 把 `content_version` 缺失（`None`）当作"旧格式，跳过版本检查"处理，而不是当作"不匹配"处理——这会让一个内容已经变了但版本号忘记递增的 wiki 直接通过校验，正好复现 Audit 3 发现的原始漏洞,只是换了个位置。
- `isinstance(expected, dict)` 判断写反或者漏了某个分支，导致 dict 类型的 `expected` 被当字符串比较，Python 里 `dict != str` 恒为 `True`,表面上看起来"永远拒绝"反而更安全一点，但会导致 wiki 源永远无法通过校验，属于另一种需要在测试里覆盖的边界情形。
- 实现完成后必须专门为以下四种反例写测试，缺一不可：**哈希对但版本号错**、**哈希错但版本号对**、**两者都缺失**、**版本号类型不是 int（比如被误写成字符串 `"3"` 而不是 `3`）**——这四种都必须触发 fail-closed。

### 风险 2：迁移阶段用 mtime 或迁移执行时间冒充 `verified_at`

第 3.1 节已经把结论定死为"一律 `null`",但这是工程实现里最容易被"图省事"绕过的一条——填个看似合理的时间戳比留 `null` 在视觉上更"完整"，诱惑很大。一旦有人在实现时把 `verified_at` 默认填成迁移脚本运行时刻，就会让 Audit 1 已经证实是错误内容的 5-6 个页面（`resource-gate`、`capacity-preflight` 等）看起来像是"今天被人核实过是对的",这比完全没有这个字段更危险，因为它会被第 2 节设计的自动化新鲜度校验和任何读这份 wiki 的下游 agent 当作真实凭据来信任。**实现和 code review 阶段都要专门检查这一条：`verified_at` 字段除了显式调用人工核实命令或自动核实脚本产生的值之外,不允许出现任何其他来源的非 null 值。**

### 风险 3：把 schema 迁移和第 4 节的 14 个新页面内容混在同一次改动里提交/复核

如果把"给 wiki/图谱加时间戳字段和校验逻辑"这个结构性改动，和"新增 14 个内容页面"这个内容性改动放在同一个 PR/同一轮复核里,会造成两个具体问题:一是复核者的注意力会被大量新增文字内容分散,容易漏审真正安全敏感的 `verify_reviewed_pack` 逻辑改动;二是一旦某个新增页面的 `summary` 被复核者挑出事实错误(比如某个 `id` 归类、某个状态判断不准),会导致连带整个 PR 被打回,包括本来已经验证正确的 schema/校验逻辑改动也要跟着重新走一遍复核流程,浪费复核成本。**必须严格分两阶段独立提交、独立双审、独立签名**:阶段 A 只做 schema 迁移 + 校验逻辑 + 全部存量条目回填为 `unverified`（不新增任何页面内容），先落地验证空转；阶段 A 稳定生效之后，阶段 B 才作为纯内容 PR 提交第 4.1 节的 14 个新页面和第 4.2 节的既有页面修正，且阶段 B 提交时机不早于对应页面真正完成人工核实（不能把还没查证的新页面也标成 `verified`）。