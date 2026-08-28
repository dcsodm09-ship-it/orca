# OPUS5 独立复核（第三轮）— claude-codex-memory-bridge

- **复核模型**：Claude opus5 / max effort
- **日期**：2026-08-17
- **审查目标 commit**：`1003f5dd9630e5ef5a054fbfd3e4a2cfef2233c0`
- **对比基线**：`01e65fd7f40d00feabdb66d78880fd8ba6297816`（第二轮 Codex sol/xhigh 复核所用基线）
- **审查范围**：`claude-codex-memory-bridge/`（3 个文件有增量：`claude_memory_hook.py` +105/-29，`tests/test_claude_memory_hook.py` +70/-?，README 本轮**未改动**）
- **性质**：只读复核。未修改、未提交、未合并、未安装、未激活任何候选。
- **独立性**：与并行派出的另一路复核完全独立，未读取其结论。所有构造向量均自建，未复用报告中给出的向量，也未直接复用本项目自身的 test fixture（另写了一套 rig 驱动真实 `hook.run()` 全链路）。

---

## 结论：**NO-GO**

**1 个可复现 P1**（修复 ① 不完整，在真实文件系统上端到端复现，且在本机一个**真实存在**的碰撞目录上距离触发仅约 6 次普通会话）。

修复 ② 与 ③ 经独立验证**确实完整修好**；T2/T4/T5/R1/R2 抽查**确实已修好**；测试套件**真实 43/43 全绿**。

另发现 2 个 P2（其中 1 个是本轮新引入的功能回归，1 个是文档与代码相互矛盾）和 3 个 P3。

| 编号 | 级别 | 摘要 | 是否本轮新引入 |
|---|---|---|---|
| R3-P1-1 | **P1** | 16 条 transcript 扫描上限使"两个不同 cwd 即拒绝"规则失效，P1-R2-1 泄漏在 >16 条的共享目录上原样复现 | 修复不完整（旧问题未闭合） |
| R3-P2-1 | P2 | 带 `relocated` 标记的目录现在会拒绝**其真正的所有者**；relocated 代码路径实际上被此次收紧变成近乎死代码 | **是，本轮新引入** |
| R3-P2-2 | P2 | README 仍然描述修复 ① 之前的行为（"serves them the one shared MEMORY.md"），与代码直接矛盾；`read_memory_documents` 内注释同样过期 | **是，本轮新引入**（代码改了、文档没改） |
| R3-P3-1 | P3 | IPv6 zone-id 字符类吞掉相邻标点与最多 64 字符正文（过度脱敏、内容丢失） | 否（N9 修复的副作用，已存在于基线） |
| R3-P3-2 | P3 | 以 `::` 结尾的地址后再跟一个孤立 `:`（如 `2001:db8:::`）不脱敏 | 否（修复 ② 刻意权衡的残留） |
| R3-P3-3 | P3 | `abc::def` / `Dec::Add` / `Cafe::Beef` 等全 hex 字母标识符被误判为 IP 而销毁 | 否（基线同样存在；本轮此项其实**变好**了） |

---

## 复核方法

为避免继承本项目 fixture 中已有的任何假设，我另写了一套独立 rig（`rig.py`），要点：

- 用 `git archive 1003f5dd96` 把审查目标**精确固定**在目标 commit 的字节状态，避开本会话内并行推进的 HEAD 漂移。
- 所有 cwd 都是 `mkdir` 出来的**真实目录**；所有 transcript 都是真实写盘、`chmod 0600` 的真实文件；MEMORY.md 真实写盘 `0600`；项目目录 `0700`。
- 一律驱动**完整的 `hook.run()`**（策略校验 → 脚本摘要校验 → 存储校验 → 解析输入 → 读记忆 → 排序 → 脱敏 → 输出），不走单元级捷径。
- 关键结论都**同时对基线 `01e65fd7f4` 跑一遍**，用来证明"我的复现确实是那个问题的复现"，而不是一个空测试。

---

## (a) P1-R2-1 新行为的独立验证

### (a-1) 基础场景：**通过**

构造两个真实碰撞 cwd（`…/ws/team a` 与 `…/ws/team-a`，二者 sanitize 后同名），各自一份诚实 transcript（UUID 文件名 + 匹配 sessionId + 各自真实 cwd），共享同一份 MEMORY.md（同时含 A、B 两侧标记）。

| 断言 | 结果 |
|---|---|
| A、B 是磁盘上真实存在且互不相同的目录 | PASS |
| 两者派生出同一个 Claude 项目目录名 | PASS |
| 两份 transcript 确实落在同一个共享目录 | PASS |
| 权限：transcript/MEMORY.md `0600`，目录 `0700` | PASS |
| transcript A 诚实记录 A、transcript B 诚实记录 B | PASS |
| **从 A 发起 → 输出为空** | **PASS**（0 字节） |
| **从 B 发起 → 输出为空** | **PASS**（0 字节） |
| 任何一方都拿不到对方的标记 | PASS |
| **任何一方也拿不到自己的标记**（刻意的权衡） | **PASS** |
| 交换 mtime 新旧顺序后仍然拒绝 | PASS |
| 对照组：同样 rig 形状但只有一个真实 cwd → 仍能正常拿到记忆 | PASS（692 字节，含自己的标记） |
| B 侧改用 `relocated` 标记表达 → 仍然拒绝 | PASS |

**这个权衡本身被正确、完整地执行了**：不是只堵了"A 读到 B"，而是双向、且连"读到自己"也一并拒绝；对照组证明 rig 不是空跑。

### (a-2) 对抗性追加：**失败 → R3-P1-1（P1）**

我额外攻击了这条新规则的**边界条件**：规则依赖"扫描到了那条记录不同 cwd 的 transcript"，而扫描本身有上限 `MAX_TRANSCRIPTS_SCANNED_PER_PROJECT = 16`（按 mtime 从新到旧）。

构造：共享目录里 B 只有 1 条诚实 transcript 且是**最旧**的；A 有 16 条诚实 transcript 且都更新。总计 17 条 > 16。

结果（独立最小复现，无任何辅助层）：

```
transcripts in shared dir: 17   scan cap: 16
A requested; output bytes: 514
B's private marker present in A's context: True
```

即 **A 拿到了含 B 私有标记的完整 hook 输出**——正是 Codex 在两轮里都判定为 P1 的那个泄漏，原样复现，只是需要共享目录里超过 16 条 transcript。

`_session_recorded_cwd_matches` 在扫满 16 条后 `break`，此时 `own_match_found` 已为 `True` 且从未见到不同 cwd，于是返回 `True`。源码中**没有任何注释承认这个交互**（`grep MAX_TRANSCRIPTS_SCANNED_PER_PROJECT` 只有定义处和判断处两行）。

#### 真实可达性（这不是理论问题）

我对本机真实 `~/.claude/projects/` 做了只读普查：

- 共 76 个项目目录；其中 **4 个目录的 transcript 记录了 >1 个不同真实 cwd**（与任务描述的"本机当前有 4 组"一致）；另有 3 个目录的 transcript 数 **>16**。
- 4 组碰撞目前**都被正确拒绝**（因为它们都还没超过 16 条）。

但其中一组已经很接近：

```
### -Volumes-Extreme-SSD-Orca-workspaces-ai-----------
    jsonl = 14   MEMORY.md = True (390 B)   ← 真实存在的记忆文件
    两个真实 cwd:
      /Volumes/Extreme SSD/Orca/workspaces/ai获取软件/获客软件训练
      /Volumes/Extreme SSD/Orca/workspaces/ai获取软件/设备控制面板
    按 mtime 从新到旧的归属: A A A A A A A A A A B A B A
```

我按这个**真实归属序列**做了推演（在隔离 rig 里复刻同一模式，逐步增加 A 侧会话）：

```
  + 0 extra A-sessions (total 14 transcripts): refused (safe)
  ...
  + 5 extra A-sessions (total 19 transcripts): refused (safe)
  + 6 extra A-sessions (total 20 transcripts): LEAK -- A receives B's memory
```

**只需该工作区再跑约 6 次普通 Claude 会话（且期间另一侧不再新增会话），这个已经带着真实 MEMORY.md 的真实目录就会重新开始把另一个工作区的笔记喂进 Codex 上下文。** 这是数周级、正常使用即可到达的距离，不是构造攻击。

#### 定级理由

Codex 在 round 1 和 round 2 两次独立把这个场景判为 P1；本轮修复的**全部意义**就是闭合它。修复把可达条件从"任意碰撞目录"收窄到"碰撞目录且 >16 条 transcript 且较旧一方全部落在窗口外"，但**没有闭合**它，而且剩下的可达条件在本机是短期内会自然满足的。按本项目"任一路存在可复现 P0/P1 即不得完成"的规则，**这仍然是 P1**。

（补充：从纯攻击者视角，这条路径不构成权限提升——能往 `~/.claude/projects/<dir>/` 写 16 个伪造 transcript 的同用户攻击者，本来就能直接读 MEMORY.md。真正的风险是**意外碰撞**下的静默串味，也正是本 bridge 存在的意义所在。）

#### 修复方向建议（供参考，本轮未实施）

`_session_recorded_cwd_matches` 里的"发现第二个 cwd 就拒绝"必须是**全量**判断，不能受扫描窗口约束。可行做法之一：`own_match_found` 命中后不要提前 `break`，对该目录的**全部** `.jsonl` 至少做一次 cwd 提取；或者把上限只作用于"寻找匹配"阶段，"寻找冲突"阶段不设上限（冲突扫描可以更廉价——只需 head 窗口的 cwd 字段）。任何保留上限的方案都必须在超限时**fail closed**（扫不完 = 无法证明唯一 = 拒绝），而不是像现在这样超限即视为通过。

---

## (b) P1-R2-2 的独立验证：**通过**

用**我自己构造**的向量（未使用报告中的向量）做了系统性验证。

### (b-1) 单字符后缀全扫描（baseline vs target 对照）

对 6 个代表性地址 × 全部 ASCII 可打印字符做后缀扫描，统计"地址原文仍出现在输出中"的组合：

- **基线 `01e65fd7f4`**：泄漏后缀字符集 = `.` 和 `:`（以及部分地址的十六进制字符）。**确认 P1-R2-2 在基线上真实存在。**
- **目标 `1003f5dd96`**：泄漏后缀字符集中**已不含 `.` 和 `:`**，只剩字母/数字/`_`/`%`——即"地址被粘在一个更长的标识符里"的情况，这是 N2 词边界修复**刻意的**设计（`2001:db8::1g` 本来就不是地址）。

### (b-2) 我的终止符向量

对 `2001:db8:85a3::8a2e:370:7334`、`fe80::1234`、`::1`、`2001:db8::1`、`fd00::dead:beef`、`::ffff:0:0`、`1::` 逐一测试终止符 `.` `:` `,` `;` `)` `]` `!` `?` `"` `'` `>` `...` `.)` `:.` `.:` 及空格——**全部正确脱敏**。

```
'peer at 2001:db8:85a3::8a2e:370:7334.'  -> 'peer at [REDACTED_IP].'
'peer at 2001:db8:85a3::8a2e:370:7334:'  -> 'peer at [REDACTED_IP]:'
'peer at 2001:db8:85a3::8a2e:370:7334,'  -> 'peer at [REDACTED_IP],'
'he said: 2001:db8::1: ok'  base='he said: 2001:db8::1: ok'  target='he said: [REDACTED_IP]: ok'  ← 相对基线修复
```

### (b-3) 合法双冒号结尾不受影响：**通过**

`2001:db8::`、`fe80::`、`::`、`2001:db8:1::`、`fd12:3456:789a::` 单独出现、以及后跟 `.` / `,` 时，**全部正确脱敏**，未被 `(?<![^:]:)` 的回溯规则误伤。

### (b-4) 随机交叉验证

随机生成 4000 个合法 IPv6（压缩形式）+ 7 个特殊形式，每个配 9 种上下文终止符，共 **36063 条 prose 内嵌合法地址**，检查是否有漏网：

- 仅 3 条"幸存"：`:::`、`1:::`、`2001:db8:::`——即 R3-P3-2，全部是"以 `::` 结尾 + 再跟一个孤立 `:`"，文本本身不是合法地址。

### (b-5) N2 反回归：**目标严格优于基线**

```
'std::vector<int>'   base='st[REDACTED_IP]vector<int>'  target='std::vector<int>'   ← 基线在破坏正文
'Foo::Bar'           base='Foo[REDACTED_IP]r'           target='Foo::Bar'            ← 基线在破坏正文
```

### (b-6) zone-id：**通过（含我自己的 zone 形状）**

`eth0` `en0` `eth0.100` `eth_0` `{4D36E972-E325-11CE-BFC1-08002BE10318}` `utun3` `br-lan` `wlp2s0` 全部连同地址一起完整脱敏，接口名未泄漏。

**结论：P1-R2-2 已完整修复**，包括报告里提到的句号与冒号两种，以及我自行补充的逗号、分号、括号、引号等；合法 `::` 结尾未受影响。

---

## (c) T3 的独立验证：**通过**

构造要点（第一次构造时我自己搞错了——两个 cwd 并未真正碰撞，等于空测试；已重做）：

- 受害者 cwd `…/ws/team a`，请求方 cwd `…/ws/team-a`，二者 **sanitize 后同名**（真实碰撞）。
- 共享目录里**只有一条** transcript，其原始字节在 cwd 字段里含非法 UTF-8 字节，且 `decode("utf-8","ignore")` 后**恰好等于请求方的 cwd**。
- 断言：`corrupted.decode("utf-8","ignore") == attacker` 在构造时即已校验。

| 版本 | 请求方输出 | 受害者 secret 泄漏 |
|---|---|---|
| 基线 `01e65fd7f4` | 511 字节 | **True（真实泄漏）** |
| 目标 `1003f5dd96` | 0 字节 | **False** |

第二个变体（非法字节插在中间，`ignore` 解码后合成出另一个真实存在的目录名）结论相同：基线泄漏、目标拒绝。

**这证明我的复现确实是 T3 的复现，且 T3 已修好。**

同时验证修复没有矫枉过正：

- 诚实所有者仍能正常取到记忆（511 字节）。
- 一条损坏行**不会**污染同窗口内其他正常行（仍取到 511 字节）。
- 512 KB 的大 transcript、64KB 窗口边界切在多字节字符中间时，所有者验证**仍然成功**——即按行严格解码没有制造新的假阴性。

---

## (d) T2 / T4 / T5 / R1 / R2 / D1 抽查

| 项 | 结果 | 证据 |
|---|---|---|
| **T2** transcript 伪造需 UUID 文件名 + 匹配 sessionId | **已修复** | 非 UUID 文件名 → 不信任；有 UUID 无 sessionId → 不信任；UUID + sessionId 不匹配 → 不信任；UUID + 匹配 sessionId → 信任（已知残留，README 已诚实标注） |
| **T4** relocated 的 type 与 value 必须同一条记录 | **已修复** | 拆成两行（一行有 `type:"relocated"` 无值，另一行有 `relocatedCwd` 无 type）→ 不采纳，端到端 0 字节；同一行的正对照 → 正常采纳 |
| **T5** mtime 排序 + 上限提到 16 | **已修复** | 常量确为 16；构造 13 条 transcript，所有者按文件名排在第 12/13 位、按 mtime 排第 1 位，`_transcripts_newest_first` 返回所有者在首位，端到端验证成功 |
| **R1** IPv6 候选不再破坏 C++/代码文本 | **已修复** | `std::vector<int>`、`Foo::Bar::baz`、`namespace::fn()`、`std::map<std::string,int>` 全部原样保留（基线会破坏前两个） |
| **R2** MAC 独立正则恢复 | **已修复** | `00:1A:2B:3C:4D:5E`、`aa-bb-cc-dd-ee-ff`、`02:42:ac:11:00:02`、`AC:DE:48:00:11:22` 全部脱敏；且 8 组全展开 IPv6 未被 MAC 正则先咬掉一半（顺序正确） |
| **D1** docstring 措辞 | **部分可验证** | 见下 |

**D1 的诚实边界**：我**没有**反汇编 Claude Code 二进制，因此无法独立确认 `j3` 是不是裸 `readdir`、`hJc`/`uEo`/`XTt` 是否只在长路径 hash 后缀与跨 worktree 路径上运行这类**语义**主张。我做的是有界核对：`~/.local/share/claude/versions/2.1.233`（307 MB）真实存在，且 docstring 引用的全部符号在其中**真实出现**（`j3`×173、`hJc`×6、`uEo`×5、`XTt`×12、`fWe`×12、`Yre`×22、`Iot`×9、`xDy`×3、`fEo`×6，`relocatedCwd`×56）。也就是说这些引用不是编造的符号名；但其行为描述的准确性超出本轮可核验范围，我不为其背书。

---

## (f) 新引入的回归

### R3-P2-1（P2，**本轮新引入**）：relocated 标记会让**真正的所有者**被拒

最贴近现实的场景——**完全没有碰撞**，只是所有者自己目录里有一条带 `relocated` 标记的会话：

- 目录 = `sanitize(V)`；session1 诚实记录 `V`；session2 记录 `V` 且尾部带 `relocated → W`。
- 请求方就是 `V` 本人。

| 版本 | 真正的所有者是否拿到自己的记忆 |
|---|---|
| 基线 `01e65fd7f4` | **True**（496 字节） |
| 目标 `1003f5dd96` | **False**（0 字节） |

纯对照（去掉 relocated 标记，其余不变）：两版都是 True（496 字节）。**变量被隔离干净，这是本轮新引入的功能回归。**

根因：`_session_recorded_cwd` 对带 relocated 标记的文件**优先返回 relocated 目标**，而同目录其他文件返回各自 head 里的 cwd。只要 relocated 目标与其他 transcript 的 cwd 不同（按定义几乎必然不同），就会被新规则判成"两个不同 cwd"而整体拒绝。

**副作用**：relocated 支持（`_find_relocated_cwd`、`_RELOCATED_*_RE`、专门的 T4 修复、专门的测试）在任何拥有 ≥2 条 transcript 且 relocated 目标与本地 cwd 不同的目录里**实际上变成了死代码**。现存测试 `test_relocated_cwd_marker_is_honored` 之所以还绿，是因为它的目录里只有一条 transcript。

方向是安全的（拒绝服务而非串味），且 README 本来就声明 relocated 项目"不被找到"；但这次是**新增**的失效面，且与"权衡只针对碰撞"的表述不符，值得在收敛前明确决策：是接受（并写进文档与测试），还是把 relocated 目标排除在"不同 cwd"判定之外。

### R3-P2-2（P2，**本轮新引入**）：README 与代码互相矛盾

本轮 commit **只改了 `.py` 和测试，未改 README**（`git diff --stat 9d9efac4c9 1003f5dd96` 只有 2 个文件）。于是目标 commit 的 README 仍写着：

> "When that happens, this bridge — like Claude Code itself — **treats the colliding cwds as one project and serves them the one shared `MEMORY.md`**; four such collisions exist on this machine today…"

而修复 ① 恰恰把这个行为改成了**拒绝**。README 描述的是被取代的旧行为。

同样过期的还有 `claude_memory_hook.py` 里 `read_memory_documents` 的注释（第 727–734 行）：

> "…`_session_recorded_cwd_matches` **only additionally requires that at least one** of that *shared* directory's own transcripts actually recorded the exact requesting cwd…"

这正是修复 ① 之前的语义。

这两处不是文字瑕疵：README 里"serves them the one shared MEMORY.md"这句，正是前两轮复核用来把碰撞场景定性为"已接受的权衡"的依据。本项目其余部分对来源与理由的记录极其严谨，唯独这次行为收紧没有同步文档，会直接误导下一轮复核者与安装者。

### R3-P3-1（P3，非本轮新增）：zone-id 字符类吞掉相邻正文

`(?:%[^\s%]{1,64})?` 里 `[^\s%]` 包含标点，`ipaddress` 又不校验 zone 内容，于是紧跟的标点和正文被一起吞进脱敏区：

```
'gw fe80::1%en0, see the runbook'          -> 'gw [REDACTED_IP] see the runbook'      （逗号被删）
'gw fe80::1%en0. Next step is deploy'      -> 'gw [REDACTED_IP] Next step is deploy'  （句号被删）
'gw fe80::1%en0,see-the-runbook-at-step-4' -> 'gw [REDACTED_IP]'                      （删掉 24 字符正文）
'gw fe80::1%en0;rollback-plan-v2'          -> 'gw [REDACTED_IP]'                      （删掉正文）
```

方向安全（过度脱敏而非泄漏），但会静默吃掉最多 64 字符的相邻笔记内容。

### R3-P3-2（P3）：`::` 结尾 + 孤立 `:` 不脱敏

`2001:db8:::`、`1:::`、`:::` 不脱敏。这是修复 ② 中 `(?<![^:]:)` 那个"前面不是另一个冒号"限定词的直接代价，而该限定词正是为了保住 `2001:db8::` 这类合法写法。文本本身不是合法地址，现实中几乎不会出现，接受即可。

### R3-P3-3（P3，非本轮新增，且本轮变好）：全 hex 标识符误判

`abc::def`、`a::b`、`Dec::Add`、`Cafe::Beef`、`ace::bed` 被整体替换为 `[REDACTED_IP]`——它们确实是语法合法的 IPv6，与真实地址无法区分。基线行为完全相同，**且本轮把基线更严重的 `std::vector`→`st[REDACTED_IP]vector` 破坏修好了**，所以这一项是净改善，仅作记录。

### 其他回归面检查（均未发现问题）

- `_jsonl_lines` 签名由 `str` 改 `bytes`：调用方只有 `_find_json_field` 与 `_find_relocated_cwd`，两处均已同步；`split_blocks` 走的是自己的 `document.text.split("\n")`，未受影响。
- 尾部前瞻由 `(?![0-9A-Za-z_:%])` 改为 `(?![0-9A-Za-z_%])` + `(?!:[0-9a-fA-F:])` 属于放宽：我用 12 组对抗性冒号串（`1:2:3:4:5:6:7:8:9`、`::::`、`2001:db8::1::2`、`::1::`、`12:34:56`、`cost 3:1 ratio` 等）对照基线逐条比对，**只有一处差异，且是修复方向**（`he said: 2001:db8::1: ok`）。
- 在一份贴近真实的 MEMORY.md 上做整体 `redact()` 差分，目标相对基线的每一处差异**都是改善**（`2001:db8::1.` 与 `10.0.0.1.` 现在脱敏；`password="hunter2"` 保留合法引号；`?token=…&next=1` 不再吞掉后续参数；`std::vector` 不再被破坏）。
- 修复 ① 的 `checked` 计数只对 `.jsonl` 递增，`memory/` 子目录被正确跳过且不消耗预算（我最初的 T5 断言误把 `memory/` 当成最新条目，是我自己测试的 bug，非产品缺陷）。

---

## (e) 测试套件

```
cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
Ran 43 tests in 0.230s
OK
```

Python 3.9.6。**真实 43/43 全绿**（36 个 hook 测试 + 7 个 install_bridge 测试），无 skip、无 xfail。逐条测试名已核对，包含本轮新增的 `test_two_genuinely_colliding_cwds_are_both_refused`、`test_invalid_utf8_in_a_transcript_line_never_synthesizes_a_different_cwd`、`test_addresses_redact_even_with_no_space_before_a_sentence_period`。

**但测试覆盖有缺口**：`test_two_genuinely_colliding_cwds_are_both_refused` 只覆盖了 transcript 数量在上限以内的情形，没有覆盖 R3-P1-1（>16 条）。修复 R3-P1-1 时应同时补上这条回归测试。

---

## 收敛所需（建议）

1. **必须（阻断）**：修复 R3-P1-1——让"发现第二个 cwd 即拒绝"不受 16 条扫描窗口约束，或在扫描超限而无法证明唯一性时 fail closed。补对应回归测试。
2. **应当**：就 R3-P2-1 做出明确决策（接受则写进 README 与测试；不接受则把 relocated 目标排除在"不同 cwd"判定之外）。
3. **应当**：同步 README 与 `read_memory_documents` 注释到修复 ① 之后的真实语义（R3-P2-2）。
4. 可选：R3-P3-1 把 zone-id 字符类收紧到不吞尾部标点。

修复后需对**新候选**重新走双路复核（本轮结论不可继承到修改后的候选）。

---

## 附：本轮未做的事（诚实边界）

- 未反汇编 Claude Code 二进制，因此不为 docstring 中关于 `j3`/`hJc`/`uEo`/`XTt`/`fWe` 等函数**行为语义**的描述背书（只核实了符号真实存在）。
- 未审查 `install_bridge.py` 的完整安装/回滚事务逻辑（本轮增量未触及该文件；其 7 个测试通过）。
- 未在真实 Codex 会话中激活该 hook——按任务要求为只读复核，未安装、未激活。
- 对本机真实 `~/.claude/projects/` 的普查是**只读**的（仅 `lstat` 与读取 transcript 的 head/tail 窗口），未修改任何真实文件。
