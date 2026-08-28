# OPUS5 独立复核（第四轮）— claude-codex-memory-bridge

- 复核者：Claude opus5 / max effort，只读独立复核，与并行派出的另一路复核互不知情
- 日期：2026-08-17
- 候选：`1b93ad8c1caa5b1eace75cb0c041b81151213f6e`
- 基线：`1003f5dd9630e5ef5a054fbfd3e4a2cfef2233c0`（第三轮复核对象）
- 增量范围：`git diff 1003f5dd96 1b93ad8c1c -- claude-codex-memory-bridge/`
  → 3 个文件，+202 / −43：`claude_memory_hook.py`、`README.md`、`tests/test_claude_memory_hook.py`
- 目标完整性：复核期间仓库 HEAD 由 `1b93ad8c1c` 前进到 `1db1073834`（`docs(backlog)`）。
  已核对 `git diff 1b93ad8c1c HEAD -- claude-codex-memory-bridge/` 与
  `git diff 1b93ad8c1c -- claude-codex-memory-bridge/` **均为空** —— 复核对象目录逐字节未变，
  本报告结论对候选仍然成立。
- 本次复核全程只读：未修改、未提交、未合并、未安装、未激活候选（`git status --porcelain -- claude-codex-memory-bridge/` 全程为空）。

---

## 结论：**GO**

**没有可复现的 P0/P1。** 第三轮的 3 项发现（R3-P1-1 / R3-P2-1 / R3-P2-2）我都独立复现了"修复前确实坏"
并独立验证了"修复后确实好"；46/46 测试真实全绿；此前各轮已确认修复项抽查无回归；对候选新代码的
对抗性探测没有发现可被利用的新洞。

同时报出 **1 项 P2 + 3 项 P3**，均不阻塞本轮，但建议进入待办：

| 编号 | 级别 | 摘要 | 是否本轮新引入 |
|---|---|---|---|
| R4-P2-1 | P2 | 修复②收窄了 round-3 的碰撞拒绝：当"第二占用者"的唯一证据是**同一条 transcript 上的 relocated 标记**（且该 transcript 的 plain cwd 正是请求方）时，拒绝不再触发。已端到端复现：round-3 拒绝、round-4 放行 | **是，本轮新引入**（修复②的副作用） |
| R4-P3-1 | P3 | README 与代码注释把 round-2 旧行为描述成"找到自己的 match 就提前停止"，实际旧代码是**扫到上限才 break**（与是否已 match 无关）。历史描述失真 | 是（本轮新写的文字） |
| R4-P3-2 | P3 | README "这个拒绝只有在该共享目录的**每一条** session transcript 都真正被检查过之后才会触发" 说法过强：读不到 / 取不到证据的 transcript 是**被跳过**而不是 fail closed。已端到端复现两种反例 | 文字是本轮新写；底层行为是既有残留（round-3 完全相同） |
| R4-P3-3 | P3 | README 称"本 bridge 只在 relocated 目标与改名前 cwd derive 到**同一个**目录名时才识别它"——不准确。本机真实存在的 relocated 目录（两侧 derive 结果不同）就是反例，round-3/round-4 都会识别 | 文字本轮重写时沿用了旧表述 |
| O1（观察） | — | transcript 保留期（`cleanupPeriodDays` 未设置 → Claude Code 默认 30 天）会让碰撞证据自然消失，从而使 P1-R2-1 的拒绝在长期不活跃的一侧失效 | 否，既有 |

---

## 复核方法

不复用仓库自带的 `HookFixture`，另写了一套独立 harness
（`independent_probe_lib.py` / `independent_probe.py` / `independent_probe_b.py`，均在会话 scratchpad，
未落入仓库），自行搭建 policy / runtime / source 布局，并**全部走完整的 `hook.run()` 管道**
（policy sha256 + script sha256 + 卷 UUID + 输入校验 + 目录解析 + transcript 校验 + 记忆读取 +
脱敏 + 相关性筛选 + 输出封装），不做单元级取巧。

每个场景**同时对 round-3 基线与 round-4 候选各跑一遍**，这样"修好了"是差分观察，不是单点断言：

```
git show 1003f5dd96:claude-codex-memory-bridge/claude_memory_hook.py  → hook_r3.py
git show 1b93ad8c1c:claude-codex-memory-bridge/claude_memory_hook.py  → hook_r4.py
```

另外对本机真实 `~/.claude/projects/`（76 个项目目录、254 条真实 transcript）做了**只读**实测对照。

---

## (a) R3-P1-1 的独立验证：**通过**

### (a-1) 冲突 transcript 落在旧扫描窗口之外

自建共享目录：`/tmp/r4probe/deep/dir` 与 `/tmp/r4probe/deep-dir` 真实 sanitize 到同一目录名；
N 条所有者自己的诚实 transcript + **恰好 1 条**记录另一个真实 cwd 的诚实 transcript，
给它**最旧的 mtime**（在旧的 newest-first + cap 16 下必然排在扫描窗口之外）。

| 场景 | round-3 基线 | round-4 候选 |
|---|---|---|
| 20 条所有者 + 冲突(最旧 mtime) | 所有者**被服务**，输出 508 字节，**含 `OWNER_PRIVATE_MARKER`** | 双向拒绝 |
| 40 条所有者 + 冲突(最旧 mtime) | 所有者**被服务**，508 字节，泄漏 | 双向拒绝 |
| 100 条所有者 + 冲突(最旧 mtime) | 所有者**被服务**，508 字节，泄漏 | 双向拒绝 |
| 20 / 40 / 100 条 + 冲突(最新 mtime) | 拒绝 | 拒绝 |
| 20 条 + 冲突(中间 mtime) | 拒绝（恰好落在前 16 内） | 拒绝 |
| 40 / 100 条 + 冲突(中间 mtime) | **被服务，泄漏** | 拒绝 |

**排序无关性**：另跑 25 组随机 mtime（60 条所有者 + 1 条冲突，种子 0–24），round-4 **25/25 全部拒绝**。
这正是修复的关键性质——移除提前 `break` 之后，扫描顺序对结论不再有任何影响。

对照组有效：round-3 在同一 rig 上真实泄漏（508 字节、含私有标记），说明 rig 不是空跑。
**R3-P1-1 已真正修复。**

### (a-2) 超过上限时 fail closed

| 目录内 .jsonl 数 | 预期 | round-4 实测 |
|---|---|---|
| 255（全部一致、零冲突） | 服务 | 服务 ✓ |
| 256（= 上限，全部一致） | 服务 | 服务 ✓ |
| **257（全部一致、毫无冲突）** | **拒绝** | **拒绝 ✓** |
| 456（全部一致） | 拒绝 | 拒绝 ✓ |
| 256 所有者 + 1 冲突（= 257） | 拒绝 | 拒绝 ✓ |
| 256 条 + 50 个非 `.jsonl` 兄弟文件 | 服务（非 transcript 不计入上限） | 服务 ✓ |

round-3 在 257 条、零冲突时是**服务**（它没有 fail-closed 规则，只扫前 16 条）。
边界无 off-by-one：上限值本身仍可用，超一条即拒绝。**修复①的两半都成立。**

### (a-3) 上限抬高 16→256 的代价（真实测量）

- 本机真实最大目录 36 条 transcript（正是本仓库工作区）：完整 `hook.run()` **8.5 ms**（round-3 3.3 ms）。
- 合成最坏情形，256 条 × 每条约 200 KB（head/tail 窗口全满）：完整 `hook.run()` **63 ms**。
- 本机 76 个项目目录中，**0 个**超过 256 条，3 个超过旧的 16 条。

即：这个 hook 挂在 `UserPromptSubmit` 上，抬高上限后的真实开销仍在十毫秒级，最坏也只有几十毫秒；
同时旧上限 16 在本机已被 3 个目录突破——**旧上限确实是活的风险，新上限确实覆盖了现实分布。**

---

## (b) R3-P2-1 的独立验证：**通过**（并附一项 P2 残留，见 R4-P2-1）

### (b-1) 所有者不再被自己的 relocated 历史误伤

目录内：会话 1 `plain=owner`；会话 2 `plain=owner` + 尾部 `relocated → 第三方 cwd`。

| relocated 目标 | round-3 | round-4 |
|---|---|---|
| derive 到**别的**目录名（`/tmp/somewhere-else-entirely`） | 所有者**被拒绝** | **被服务，且拿到自己的 `OWNER_ONLY_NOTE`** ✓ |
| derive 到**同一个**目录名 | 所有者**被拒绝** | **被服务** ✓ |

**R3-P2-1 已真正修复**（两种子形态都修好，不只是测试覆盖的那一种）。

### (b-2) 真正的碰撞检测没有被放宽

| 场景 | round-3 | round-4 |
|---|---|---|
| 两个真实不同 cwd，各自诚实 transcript（P1-R2-1 原场景） | 双向拒绝 | **双向拒绝**，两侧都拿不到 `COLLISION_SECRET` ✓ |
| 第二占用者**只**由 relocated 标记证明（该 transcript 无 plain cwd） | — | **双向拒绝** ✓ |
| 真实 relocated 布局（26 条，其中 5 条 relocated）+ 额外掺入 1 个真正不同的第三方 cwd | — | **拒绝** ✓ |

也就是说：`(plain, relocated)` 拆分**没有**把 relocated 这一路的冲突证据整体丢掉——
一条 transcript 只要它的两个身份**都不是**请求方，仍然算冲突证据。

### (b-3) T4 仍然成立

`test_relocated_cwd_marker_is_honored` 覆盖的原始语义（改名后新 cwd 靠 relocated 标记找回旧记忆），
我用自己的 rig 独立重建：round-3 服务、round-4 服务，且内容 `SURVIVES_RELOCATION` 真实到达上下文。**T4 未被破坏。**

### (b-4) 本机真实 relocated 目录实测（只读）

`~/.claude/projects/-Users-www1adwawd-claudecode---`（26 条 transcript）：

- 5 条 transcript 的 `plain cwd` = `/Volumes/Extreme SSD/claudecode/本机`，尾部 `relocated → /Users/www1adwawd/claudecode/本机`
- 两个路径 derive 结果**不同**（`-Volumes-Extreme-SSD-claudecode---` vs `-Users-www1adwawd-claudecode---`）
- round-4 判定：新路径 = `True`（服务），旧路径 = `False`（该目录不是它的 derive 目标）
- **若没有本轮的 `(plain, relocated)` 拆分，这 5 条会被读成"第二个占用者"** —— 修复②在真实数据上有意义

### (b-5) round-3 vs round-4 全量真实数据对照

对本机 62 个"至少有一条 transcript 记录了 cwd"的真实项目目录，
枚举其中出现过的每一个真实 cwd（plain + relocated），逐个跑两版判定：

- **决策发生变化的：0 个。**
- 4 个真实碰撞目录（CJK 同长度兄弟目录，非构造攻击）两版都拒绝，与 README 的"本机四个碰撞目前一个都读不到"一致。
- 唯一的真实 relocated 目录两版都服务其当前所有者。

**结论：本轮改动在真实数据上零行为漂移**，全部差异只出现在合成的边界场景里——这正是想要的性质。

---

## (c) 文档同步核实：**基本准确，3 处措辞不精确（P3）**

先说结论：R3-P2-2 报的那两处**过期语义**（README + `read_memory_documents()` 注释里
"serves them the one shared MEMORY.md" 描述 bridge 自身行为）**已经改对了**——
现在两处都明确写成"**Claude Code 自己**这么做，而**本 bridge** 反过来对所有人拒绝"，与代码一致。
新的 fail-closed 上限（256）也在 README 与常量注释两处都写到了。已删除函数
`_session_recorded_cwd` 在全仓库无任何残留引用（只有新的 `_session_recorded_cwds` 与 `_session_recorded_cwd_matches`）。

以下 3 处是我实测出的措辞不准，均不影响代码正确性：

### R4-P3-1（P3）：对 round-2 旧行为的描述失真

README 第 79–80 行与 `MAX_TRANSCRIPTS_SCANNED_PER_PROJECT` 注释都说旧版
"stopped early once the requester's own cwd was found" / "A scan that stops early after finding its own match"。

但 round-3 的真实代码是：

```python
for entry in entries:
    if checked >= MAX_TRANSCRIPTS_SCANNED_PER_PROJECT:
        break          # ← 只在扫到上限时停，和是否已经 own_match 无关
```

它**从不**因为找到自己的 match 而提前停止；它一直扫到上限才停。
被描述的**后果**（排在上限之外的冲突 transcript 被静默漏掉）是对的，**机制**写错了。
建议改成 "capped this scan at 16 and trusted whatever it had seen when the cap was hit"。

### R4-P3-2（P3）：README "每一条都被检查过" 的说法过强

README 现在写："This refusal only fires once every one of the shared directory's session transcripts
has actually been examined"。实测两个反例（两版行为**完全相同**，是既有残留而非本轮回归）：

1. **权限降级**：把冲突 transcript 改成 `0o666` 后，`_read_head_tail` 拒绝读它 → 返回 `(None, None)`
   → 被当作"没有证据"跳过 → 所有者重新被服务，`PERM_SECRET` 真实进入上下文。
   round-3 与 round-4 结果一字不差。
2. **证据落在 head 窗口之外**：冲突 transcript 的第一条记录是 200 KB 的大 record，
   诚实的 `cwd` 行被挤到 64 KB head 窗口之后 → `(None, None)` → 跳过 → 泄漏。
   round-3 与 round-4 同样一字不差。

现实可达性很低：本机 254 条真实 transcript 中，**0 条**会退化成 `(None, None)`，
且全部 254 条都是 `0o600`。所以这不是当前可触发的漏洞，但
"每一条都被检查过"这句话字面上不成立——本轮把 cap 维度补成了 fail-closed，
**内容维度（读不到 / 取不到证据）仍然是 skip 而非 fail closed**，两者的口径不一致。
建议要么把措辞降级为"每一条都被**尝试**检查过"，要么在后续轮次把
`_read_head_tail` 返回 `None` 与"有 `.jsonl` 但取不到任何 cwd 证据"也改成 fail closed。

### R4-P3-3（P3）：relocated 识别条件描述不准

README 新段落称本 bridge "only recognizes a relocated *target* cwd when it derives to the *same*
directory name as the pre-relocation cwd"。实测反例（round-3 / round-4 都一样）：

单条 transcript，`plain = /Volumes/SSD/claudecode/proj`、`relocated = /Users/tester/claudecode/proj`，
两者 derive 结果**不同**，transcript 位于 `f(relocated)`，请求方 = relocated cwd → **被服务**。
也就是说 relocated 目标只要等于请求方 cwd 就会被采信，与两侧 derive 是否同名无关。

而且这正是**本机真实存在的布局**（见 (b-4)）：Claude Code 把 relocated 会话的 transcript 写在
`f(新 cwd)` 下、而记录里的 plain cwd 是旧路径。README 这句话把本机唯一一个真实 relocated 目录
排除在了它自己的描述之外。（这处措辞不是本轮新造的错，是重写该段时沿用了 T4 测试注释里的旧表述。）

---

## (d) 测试套件：**真实全绿 46/46**

```
$ cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
Ran 46 tests in 0.169s
OK
```

（复核开始与结束各跑一次，均 46/46。）新增 3 条测试我逐条读过，都是**真回归测试**，不是同义反复：

- `test_conflict_beyond_the_old_scan_cap_is_still_detected`：20 条所有者 + 1 条冲突（刻意最旧 mtime），
  总数 21 > 旧上限 16、< 新上限 256 —— 在 round-3 代码上会失败，是 R3-P1-1 的直接回归测试。**有效。**
- `test_scan_fails_closed_when_transcript_count_exceeds_the_cap`：cap+1 条、全部一致仍拒绝。**有效。**
- `test_relocated_marker_does_not_manufacture_a_false_collision_for_the_owner`：R3-P2-1 的直接回归测试，
  在 round-3 代码上会失败。**有效。**

一处覆盖缺口（非阻塞）：新增测试只覆盖了 relocated 目标 derive 到**别的**目录名的子形态，
没有覆盖 derive 到**同一个**目录名的子形态——而后者正是 R4-P2-1 所在的形态。

---

## (e) 前几轮已确认修复项抽查：**未发现回归**

全部走完整 `hook.run()` 管道，用我自己的样本（不是仓库测试里的样本）：

| 项 | 抽查方式 | 结果 |
|---|---|---|
| **T2**（transcript 真实性） | 同时放入"UUID 文件名但无 `sessionId`"与"`sessionId` 不匹配文件名"两种伪造 | 两种都不被采信 → 拒绝 ✓ |
| **T3**（非法 UTF-8 合成 cwd） | `cwd = "/tmp/invalid/team-\xffapp"` 的真实字节 transcript | 取到 `(None, None)`，未合成 `team-app`，拒绝 ✓ |
| **T5**（前 8 个 / 文件名排序） | 12 个按文件名排在前面的 decoy + 目标 transcript 文件名排最后 | 目标仍被找到，`T5_NOTE` 到达上下文 ✓ |
| **P1-R2-2**（IPv6 收尾） | `2001:db8::1.` / `fe80::1234:` / `, ; ( )` 结尾 / zone-id / 全长式 / `::ffff:v4` 映射 | 12 种敏感形态**零泄漏** ✓ |
| **R2**（MAC） | `aa:bb:cc:dd:ee:ff` | 已脱敏 ✓ |
| **R1**（IPv6 正则误伤普通文本） | `std::vector<int>` / `a::before` / `12:34:56` | 全部存活 ✓（`Cafe::Beef` 仍被误判，即 R3-P3-3，第三轮已记为既有 P3、非本轮引入） |
| 其他 | JWT、`$USER_HOME` 路径、`api_key = sk-live-…` 赋值 | 全部脱敏 ✓ |
| **D1**（对二进制符号的注释主张） | 有界核对：`~/.local/share/claude/versions/` 存在 `2.1.231/232/233` | 与第三轮同样的诚实边界：**我没有反汇编二进制**，不为注释里 `j3`/`hJc`/`uEo`/`XTt` 的**行为语义**主张背书 |
| 零 transcript 目录 | 有 memory、无任何 transcript | 拒绝 ✓ |
| 非 UUID 名 / 目录 / 符号链接命名为 `*.jsonl` | 三种垃圾条目 + 一条真冲突 | 不崩溃；只有垃圾时正常服务，掺入真冲突后拒绝 ✓ |

---

## (f) 新引入的回归

### R4-P2-1（P2，本轮新引入）：relocated 标记可以单方面压制"第二占用者"证据

**机制**：新的冲突判据是"这条 transcript 的 `plain` 与 `relocated` **都不等于**请求方才算冲突"。
于是当第二个占用者的**唯一**证据是"某条 transcript 的 relocated 目标"、而这条 transcript 的
`plain` 恰好就是请求方时，冲突不再成立。round-4 的冲突集合是 round-3 冲突集合的**真子集**
（可以证明：round-4 判冲突 ⇒ 两者都 ≠ 请求方 ⇒ round-3 的折叠值也 ≠ 请求方 ⇒ round-3 也判冲突），
所以修复②在 relocated 这一维上是**纯放宽**。

**端到端复现**（`R = /tmp/W/proj/x`，`X = /tmp/W/proj-x`，两者 sanitize 到同一目录 D）：

| 目录内容 | round-3 | round-4 |
|---|---|---|
| 1 条 transcript：`plain=R`，`relocated=X`（会话从 R 改名到 X） | 请求方 R：**拒绝** | 请求方 R：**被服务，拿到 `X_WORKSPACE_PRIVATE_NOTE`** |
| 同上 + 后来 X 自己又跑过一次会话（出现 `plain=X` 记录） | 请求方 R：拒绝 | 请求方 R：**拒绝**（证据恢复，自动收敛） |

**为什么判 P2 而不是 P1**：

1. **无法被对抗性放大。** 要让攻击者 cwd `A` 拿到受害者 `V` 的记忆，必须让 D 内**每一条** transcript 的
   两个身份里都含 `A`。`V` 自己的诚实 transcript（`plain=V`、无 relocated）永远会触发冲突。
   我实测了两种针对性伪造（`plain=A`+`relocated=V`、`plain=V`+`relocated=A`），
   在受害者存在诚实 transcript 的前提下**都拿不到 `VICTIM_SECRET`**。
2. **本机零实例。** 唯一的真实 relocated 目录两侧 derive 结果不同，不落在这个形态里；
   全量 62 目录对照下 round-3 与 round-4 决策差异为 0。
3. **需要一串不自然的前置条件**：同名 sanitize 的改名 + 旧路径被另一个无关项目复用 + 新路径此后再没跑过会话。
   任何一次新会话都会让证据恢复、拒绝重新生效。
4. **已被披露**：README 明确写了"不把这种识别当作第二个真实工作区共享该目录的证据"。
5. round-3 在这个形态下的"拒绝"本身是折叠 bug 的副产物——同一个 bug 也在别的形态下拒绝了诚实所有者
   （即 R3-P2-1），所以不能简单说 round-3 更正确。

**建议（后续轮次，不阻塞本轮）**：把 relocated 目标再分一次情况——
若 `f(relocated) == 当前目录`，说明改名前后两条路径**确实**共享同一个记忆目录，应仍按第二占用者处理；
若 `f(relocated) != 当前目录`，说明该会话的记忆已经跟着搬走，才是本轮想豁免的情形。
这样既保住 R3-P2-1 的修复，又不收窄 round-3 的碰撞拒绝。同时补上对应的同名 sanitize 子形态测试。

### 其他回归面检查（均未发现问题）

- 上限判定的 off-by-one：255/256 服务、257/456 拒绝，边界正确。
- 非 `.jsonl` 兄弟文件不计入上限（50 个 `.txt` + 256 条 transcript 仍服务）。
- 命名为 `*.jsonl` 的**目录**、**符号链接**、**非 UUID 名文件**：不崩溃、不被采信、也不会吞掉真冲突。
- `_transcripts_newest_first` 的 mtime 排序在移除提前 `break` 后已与正确性无关（25 组随机顺序全部一致），
  仍保留是合理的（决定"先看到谁"的成本顺序，不影响结论）。
- 性能：真实最大目录 8.5 ms，合成最坏 63 ms（见 (a-3)）。
- 全仓库无对已删除函数名的残留引用。

### O1（观察，非本轮引入）：transcript 保留期会让碰撞证据自然过期

本机 `~/.claude/settings.json` 未设置 `cleanupPeriodDays`（Claude Code 默认 30 天清理）。
`_session_recorded_cwd_matches` 的整套保证建立在"冲突方的 transcript 还在"之上，
而 `memory/` 不会被这套清理带走。因此一个长期不活跃的工作区，其 transcript 过期消失后，
与它碰撞的另一个 cwd 会重新通过校验、读到它遗留的 `MEMORY.md`——
即 P1-R2-1 的原始泄漏会随时间自然复活。这不是本轮引入，round-3 完全相同，
也超出本轮"修 R3-P1-1/P2-1/P2-2"的范围，但值得进入待办（本机 4 个真实碰撞目录都在这个风险面上）。

---

## 逐项裁决

| 第三轮发现 | 本轮修复 | 独立裁决 |
|---|---|---|
| R3-P1-1（P1）扫描上限使碰撞检测失效 | 上限 16→256 + 超限 fail closed + 移除提前 break | **已修复。** 21/41/101 条目录、最旧/最新/中间/25 组随机 mtime 全部拒绝；对照 round-3 在同一 rig 上真实泄漏 508 字节 |
| R3-P2-1（P2）relocated 误判成第二占用者 | `_session_recorded_cwds` 返回 `(plain, relocated)`，匹配取并、冲突取交 | **已修复**（两种子形态都修好）。同时引入 R4-P2-1（P2 残留） |
| R3-P2-2（P2）README/注释过期 | 同步改写 README "Known limits" 与 `read_memory_documents()` 注释 | **已修复。** 另发现 3 处措辞不精确（R4-P3-1/2/3，P3） |

---

## GO/NO-GO

**GO。**

- (a) R3-P1-1 独立验证通过（含超上限 fail closed、边界、排序无关性）
- (b) R3-P2-1 独立验证通过；真实碰撞双向拒绝未被放宽；T4 仍成立
- (c) 文档已同步到真实行为；3 处措辞不精确记为 P3
- (d) 46/46 真实全绿，新增 3 条测试都是有效回归测试
- (e) 前几轮修复项抽查无回归
- (f) 新引入 1 项 P2（R4-P2-1），无 P0/P1

按本项目的门禁规则（任一路存在**可复现 P0/P1** 即不得完成/合并/发布/安装/部署），
本路复核**没有**可复现的 P0/P1，故给出 GO。R4-P2-1 与三项 P3 建议进入下一轮待办，
其中 R4-P2-1 的收敛方案已在上文给出。

**诚实边界**：与第三轮一致，我**没有**反汇编 Claude Code 二进制，
因此不为源码注释中关于 `j3`/`hJc`/`uEo`/`XTt`/`fWe` **行为语义**的主张背书；
我能核实的是这些符号确实存在于安装的 2.1.233 中，以及 `relocatedCwd` 在真实 transcript 中的
落盘形态（`{"type":"relocated","relocatedCwd":…,"sessionId":…}`，位于 `f(新 cwd)` 目录下），
后者是我本轮实测得到、并据以评估 (b) 与 R4-P2-1 的事实基础。
