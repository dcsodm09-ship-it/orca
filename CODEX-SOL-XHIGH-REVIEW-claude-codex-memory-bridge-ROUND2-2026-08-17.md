# Codex 独立只读复核：claude-codex-memory-bridge 第二轮

- 日期：2026-08-17（Asia/Taipei）
- 复核者：Codex `gpt-5.6-sol` / `xhigh`，独立 reviewer
- 精确候选：`01e65fd7f40d00feabdb66d78880fd8ba6297816`
- 增量基线：`b9ce3e1e62259cb99112d50f972f94fc826c7e9c`
- 范围：`claude-codex-memory-bridge/`
- 最终结论：**NO-GO**

## 0. 结论先行

第二轮候选完整修复了第一轮 P1-2 的命名算法偏差：我从本机 Claude Code
2.1.233 可执行文件独立提取 `Zu`、`Iot`、`fEo`、`WT`、`bN`，再用原始
JavaScript 语义对 208 个包含 NFC、非 BMP、200/201 边界和长路径的向量做
交叉验证，候选 **208/208 一致**。

但仍有两个可稳定复现的 P1，因此不能安装或激活：

1. **P1-R2-1：原 P1-1 只被部分修复。** 两个真实存在、sanitizer-collision
   的 cwd 在第二个 cwd 没有 transcript 时确实 fail closed；一旦两个 cwd 都
   真实跑过 Claude、共享派生目录中各有自己的 transcript，第二个 cwd 仍会收到
   该共享目录唯一 `MEMORY.md` 中第一个 cwd 的标记。候选测试只覆盖“第二个 cwd
   从未记录于 transcript”的较弱情形。
2. **P1-R2-2：原 P1-3 只被部分修复。** 裸 `2001:db8::1`、`fe80::1234`、
   `::1` 会脱敏，但同一个合法压缩地址后紧跟普通句号时，例如
   `2001:db8::1.`，候选正则完全不匹配，地址在完整 `hook.run()` 输出中原样
   注入 Codex。

此外，本轮 IPv6 改动新引入了普通 C++/命名空间文本被误删、MAC 地址不再脱敏；
transcript 校验还存在任意 `.jsonl` 一行伪造、非法 UTF-8 忽略后合成匹配、跨行
拼接 `relocated` 语义、任意 UUID 名排序后只扫前 8 个等较低级问题。

## 1. 启动态、只读边界与候选身份

启动时收到 `ORCA_CONTEXT_NACK_V1`，原因是中央 reviewed source 的 wiki
freshness mismatch。本复核没有声称加载 Orca 中央上下文，只使用明确可见的 Git
对象、仓库文件、本机已安装 Claude 二进制、OpenAI 官方 hooks 文档，以及
`TemporaryDirectory`/`/tmp` 中的隔离合成探针。

未修改候选目录，未提交、合并、安装或激活 bridge，未连接任何服务器，也未读取
凭据。仓库内唯一由本复核保留的新文件是任务明确要求的本报告。

复核开始时工作树 HEAD 为 `06c04bac34334364ccca894c8d77286040b9d98f`，
已经晚于目标候选；写完报告后的最终交叉检查发现共享工作树又被并发任务推进到
`b59ace0b922083ca0b7f61e279a2ee9766c74d01`（新增提交与本 bridge 无关）。
目标 `01e65fd7…` 始终是二者祖先，但当前 `claude-codex-memory-bridge/` 含候选之后
的提交。因此没有把任一 live HEAD 的目录测试误归因给候选，而是通过：

```sh
git archive 01e65fd7f40d00feabdb66d78880fd8ba6297816 claude-codex-memory-bridge
```

导出到隔离临时目录后运行所有测试和探针。导出文件 SHA-256 与直接
`git show <commit>:<path>` 的 SHA-256 一致；HEAD 漂移后又重新核对一次，目标
tree OID 与五个 blob 内容均未变化。

| 身份检查 | 结果 |
|---|---|
| 目标 commit | `01e65fd7f40d00feabdb66d78880fd8ba6297816` |
| 目标 parent | `f2b53dc093d994b1f6b6b5c58c3b7f6a53f2cfc4` |
| 目标目录 tree | `c9a5feb528971455f3f9c379bc35611c7080302c` |
| 相对 `b9ce3e1e…` | 3 文件，`+487/-48`；README、hook、hook tests |
| `git diff --check` | 通过 |

精确候选文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `README.md` | `964082aca42a66026e7808fb55726a7595e52edfc36988697681092a788a0fe6` |
| `claude_memory_hook.py` | `6a7c205e10eb6415a29d230df3f5de8d4855c67972759a139b52dd5ecd060ddd` |
| `install_bridge.py` | `08add5c165d655e02d4f5687e16203bd946d5fb46588408a6ae3c618ed62db54` |
| `tests/test_claude_memory_hook.py` | `d91fed6aa53338a8e7da7e7af1d90f659e70abd20105041d673b7668554545d2` |
| `tests/test_install_bridge.py` | `1e1041711703d8af2ea2c291dcc2be7761b93c16ca5a70d2ef8e0c9be853bf4f` |

## 2. 三个第一轮 P1 的复核结果

### P1-1：**未完全修复，仍是 P1**

相关位置：`claude_memory_hook.py:401-418`、`:481-530`、`:533-604`；覆盖缺口：
`tests/test_claude_memory_hook.py:308-328`。

候选新增的 `_session_recorded_cwd_matches()` 对“碰撞目录只属于 A，B 从未运行过
Claude、没有任何 B transcript”的情况有效。候选自己的测试恰好只构造这一情形：
A 有 `MEMORY.md` 和 A transcript，B 没有 transcript，因此 B 得到空输出。

真实边界要再走一步：在 `TemporaryDirectory` 中创建两个真实目录：

```text
<tmp>/collision/team/app
<tmp>/collision/team-app
```

二者都存在且互不相同，但 `claude_project_dirname()` 完全相同。在共享派生项目目录
中写入 A 的 `MEMORY.md` 与 A transcript：

```text
before_b_transcript_blank = true
```

随后模拟 B 也真实运行过 Claude，在同一派生目录增加一份记录 B 精确 cwd 的普通
`.jsonl` transcript，再以 B 的 cwd 调用完整 `hook.run()`：

```text
dirname_equal = true
after_b_transcript_leaks = true
marker = VICTIM_PRIVATE_91c2f0
output_bytes = 476
```

这不是路径穿越、symlink、权限漂移或不存在的 cwd：两个 cwd 都是实际 `mkdir`
出的目录，project/memory 目录为 `0700`，文件为 `0600`。检查只证明“请求 cwd
曾在这个共享目录出现过”，不能证明共享的唯一 `MEMORY.md` 只属于它。若两个真实
cwd 都产生 transcript，这个 lossy key 的歧义重新变成确定性串读。

同一机制还可被一行任意 `.jsonl` 绕过。候选既不校验 UUID 文件名，也不校验记录
的 `sessionId` 或 transcript 来源；向碰撞目录加入：

```json
{"cwd":"/tmp/forge/team-app"}
```

完整 `hook.run()` 就把 `FORGED_GATE_LEAK_7e4a` 返回给请求 cwd。对“同 UID
恶意代码本来就能直接读文件”的威胁模型，这可降级看作纵深不足；但对本修复宣称的
cwd namespace 绑定，它说明 transcript 并不是可信身份凭据。

README `:33-41` 同时承认转换有损，却断言这些限制“neither ever serves a
different workspace's content, only nothing”；现场双 transcript 结果直接否定这句
绝对声明。代码注释 `:543-554` 反而更接近实际行为：共享目录会被当作一个 project。

**修复要求**：如果验收边界仍是“任意不同真实 cwd 不串读”，必须对派生目录的
歧义 fail closed，而不是在“找到任一精确 cwd transcript”后继续读取共享
`MEMORY.md`。至少加入两个碰撞 cwd **各有真实 transcript** 的回归；单边 transcript
测试不足以关闭 P1。

### P1-2：**算法修复通过**

相关位置：`claude_memory_hook.py:327-418`。

本机证据：

```text
binary  : ~/.local/share/claude/versions/2.1.233
version : 2.1.233 (Claude Code)
SHA-256 : bc466b6cde63edafc773f471a1fb98787fabb31f52240c8616ce7e1f587b212d
format  : Mach-O 64-bit executable arm64
```

从 Bun 单文件可执行中的 JS 字节窗口独立找到：

| 函数/常量 | 二进制字节偏移附近的实际语义 |
|---|---|
| `Zu` | `e.normalize("NFC")` |
| `Iot` | 逐 `charCodeAt` 的 32-bit `(t<<5)-t+code|0` |
| `fEo` | `e.replace(/[^a-zA-Z0-9]/g,"-")` |
| `WT` | `<=Yre` 原样；否则 `slice(0,Yre)-abs(Iot(e)).toString(36)` |
| `bN` | `join(projectsRoot, WT(e))` |
| `Yre` | `200` |

主字节窗口位于约 `269,363,200`，另一个 `Iot`/同算法副本位于约
`268,725,900`。候选的 UTF-16 code-unit 替换、signed 32-bit wrap、绝对值、
base36 和 200 字符判断都与之吻合。

独立 JavaScript reference 直接使用上述二进制源码语义，和候选 Python 实现比较
208 个确定性向量，结果：

```json
{"vectors":208,"mismatches":0}
```

覆盖包括：

- NFC 分解/合成（`e + U+0301`）；
- emoji 和其他非 BMP 字符的双 UTF-16 code unit；
- 恰好 200 与 201 code units；
- 长 ASCII、长 CJK、长 emoji 混合路径；
- `Iot()` 正、负结果及精确 base36 后缀。

例如：

```text
/tmp/emoji-😀-x -> -tmp-emoji----x
201-unit ASCII  -> <first 200>-b6ymvl
220-unit ASCII  -> <first 200>-y46xwy
```

所以第一轮 P1-2 本身关闭。现有长路径测试 `:293-306` 只检查后缀形状而非精确
hash，建议把至少一个独立固定 suffix 作为回归，但本次独立 208 向量没有发现实现
偏差。

#### 二进制校验机制的注释并不准确

候选 `claude_memory_hook.py:403-410` 说 Claude Code 自身不会只信任派生名，会先
用 `hJc/uEo/XTt` transcript cross-check。实际 2.1.233 的 `j3()` 是：先对
`bN(e)` 直接 `readdir()`，存在就加入结果；普通短路径随后立即返回。只有长路径
hash sibling 搜索及另一个跨 worktree 路径会进入 `fWe()`/`hJc()`。

而且真实 `hJc()` 不是比较 raw cwd；它对 transcript 的 cwd 做
`fEo(Zu(n))` 后与已经 sanitized 的请求 key 比较，所以两个 sanitizer-collision
cwd 在 `hJc()` 中仍相等。候选自己的 raw NFC 字符串比较更严格，这是正向纵深，
但不能宣称等同于 Claude 主查找的身份验证。此文档/注释问题定为 P2，不改变算法
本身通过的结论。

### P1-3：**只修复裸形式；普通句末形式仍是 P1**

相关位置：`claude_memory_hook.py:623-660`；测试：
`tests/test_claude_memory_hook.py:175-193`。

正向矩阵通过：

| 输入 | 候选输出 |
|---|---|
| `2001:0db8:0000:0000:0000:ff00:0042:8329` | `[REDACTED_IP]` |
| `2001:db8::1` | `[REDACTED_IP]` |
| `::1` | `[REDACTED_IP]` |
| `::` | `[REDACTED_IP]` |
| `[2001:db8::1]:443` | `[[REDACTED_IP]]:443` |
| `2001:db8::/64` | `[REDACTED_IP]/64` |

但 `_IPV6_CANDIDATE_RE` 把 `.` 既列为候选字符又列入右边界禁止集合。
合法地址后紧跟句号时，没有任何可结束匹配的位置：

| 输入 | 候选输出 |
|---|---|
| `2001:db8::1.` | `2001:db8::1.` |
| `fe80::1234.` | `fe80::1234.` |
| `2001:db8::1:` | `2001:db8::1:` |
| `2001:db8::1,` | `[REDACTED_IP],` |

这不是只调用 helper 的假阳性。把三条地址写进 `0600 MEMORY.md`，经完整
`hook.run()` 选块、脱敏、构建上下文后得到：

```json
{"primary_leaked":true,"backup_leaked":true,"control_redacted":true}
```

输出中的 `quoted_text` 原样包含 `primary 2001:db8::1.` 和
`backup fe80::1234.`。句末句号是历史说明文字的普通写法，直接落在本 bridge 的
“常见服务器地址脱敏”核心边界，因此是第一轮 P1-3 的未关闭分支，而不是纯格式
问题。

**修复要求**：先可靠分词/候选截取，再用 `ipaddress.IPv6Address` 验证核心地址；
不能让支持 IPv4-mapped 地址所需的 `.` 把普通句末句点吞进候选或边界判断。回归需
包含句号、冒号、逗号、括号、CIDR、zone id、IPv4-mapped 和相邻代码文本。

## 3. `_session_recorded_cwd_matches()` 独立边界评估

### 3.1 成立的部分

- source/project/transcript 仍要求 owner、非 symlink、非 group/world writable；
- transcript 本体 `open(..., O_NOFOLLOW)` 并复核 open 前后的 dev/inode；
- 请求 cwd 与记录 cwd 做 NFC 后的 raw 字符串精确比较，比真实 `hJc()` 的 lossy
  sanitized 比较更严格；
- 1 GiB sparse transcript 实测只读取头尾共 `131072` bytes，耗时约
  `0.061 ms`；单 project 最多读取 8 份，即 transcript 内容读取上限约 1 MiB，
  大文件不会按文件大小线性扫描。

### 3.2 发现的边界问题

| ID | 级别 | 复现与影响 |
|---|---|---|
| T1 | P1（同 §2 P1-R2-1） | 两个碰撞 cwd 都有正常 transcript 时，两边都通过，仍共享唯一 `MEMORY.md` |
| T2 | P2 | 任意 owner-only `.jsonl` 的一行 `{"cwd":"target"}` 即可通过；不校验 UUID 名、`sessionId` 或来源 |
| T3 | P2 | 非法 UTF-8用 `decode(...,"ignore")` 删除坏字节，可合成另一个精确 cwd；`/tmp/invalid/team-\xffapp` 被当成 `/tmp/invalid/team-app`，完整 pipeline 串读成功 |
| T4 | P2 | `relocatedCwd` 的值和 `type:"relocated"` 可来自两条不同记录；`relocated=/tmp/legit` + 较新的 `type:other, relocatedCwd=/tmp/attack` 返回 `/tmp/attack`。真实 `XTt()` 要求 type/value 同一行 |
| T5 | P2 | 对文件名排序后只扫前 8 个。8 个 decoy + 第 9 个真实 target transcript 得到 `False`；UUID 名随机，行为和最近会话无关。真实本地 `fWe()` 没有该 8 文件 cap |
| T6 | P3 | `sorted(project_dir.iterdir())` 先枚举并排序全部目录项，读取 cap 不约束目录枚举的内存/时间 |
| T7 | P3 | 头尾窗口与真实二进制同为 64 KiB；落在中段的 relocation 会被忽略并回退到 head cwd，属于覆盖率/陈旧身份风险 |

非法 UTF-8 的另一方向是安全的：`MEMORY.md` 本身含非法 UTF-8 时
`_read_memory_file()` 严格解码并返回空，现场 `invalid_memory_output_blank=true`。
问题只在 transcript gate 使用 `ignore`，导致“坏输入 fail closed”的保证不成立。

## 4. 新回归

### R1（P2）：IPv6 候选正则误删普通代码/文字

候选从基线的窄 `_IPV6_RE` 改成宽候选后，以下全部是第二轮新增行为：

| 原文 | 基线 `b9ce…` | 候选 `01e65…` |
|---|---|---|
| `std::vector<int>` | 原样 | `st[REDACTED_IP]vector<int>` |
| `Foo::bar()` | 原样 | `Foo[REDACTED_IP]r()` |
| `namespace::fn` | 原样 | `namesp[REDACTED_IP]n` |
| `hello::world` | 原样 | `hello[REDACTED_IP]world` |
| `::before` | 原样 | `[REDACTED_IP]ore` |

原因是正则允许从普通单词内部的十六进制字母开始；`d::`、`e::f`、`::bef`
等片段恰好能被 `ipaddress` 当成合法压缩 IPv6。bridge 会静默篡改历史代码记忆，
现有正向 IPv6 测试没有任何 negative controls。

### R2（P2）：MAC 地址脱敏回归

基线会把 `de:ad:be:ef:00:11`、`AC:DE:48:00:11:22` 替换成
`[REDACTED_IP]`；候选的 `ipaddress` 正确判定它们不是 IPv6，于是原样保留。
无论原基线是否“碰巧”覆盖 MAC，这都是送审增量可观察到的脱敏能力回退，应明确
决定是否仍属“server-address forms”契约并加测试。

### R3：IPv4-mapped 与 zone-id 只部分处理

- `::ffff:192.0.2.128` 先被 IPv4 规则改成 `::ffff:[REDACTED_IP]`，不会以完整
  IPv6 候选进入 `ipaddress`；endpoint 数字被遮住，但输出不是统一的整地址脱敏。
- `fe80::1%en0` 变成 `[REDACTED_IP]%en0`；URL zone
  `[fe80::1%25eth0]:443` 仍保留 `%25eth0`。地址主体已脱敏，故本项定 P3，但
  interface/scope 标识仍可能敏感。

## 5. F3 / F4 / F5 文档修复

### F3：核心风险已写明；“静默、无错误”措辞过度绝对

README `:91-101` 已明确安装会触发 hooks trust gate、人工 re-trust 前 hooks
可能停摆；这成功把第一轮遗漏的安装风险放进 acceptance boundary。

当前 [OpenAI 官方 Codex hooks 文档](https://developers.openai.com/codex/hooks)
确认：trust 绑定当前 hook definition 的 hash；new/changed hooks 会被标记 review
并在 trusted 前 skip。官方文档同时说启动时若 hooks 需要 review，Codex 会打印
提示。因此 README `:95` 的“silently, with no error”过度绝对；更准确是“hook
本身不运行，非交互流程不能依赖它失败；客户端版本/入口可能另有 startup warning”。

此外，官方当前措辞是 new/changed **hook definition**，并不单独证明“文件任意
变化会让所有未改动 handler 一并失效”。第一轮实证在当时本机环境观察到两个现有
hook 都未运行，所以风险本身保留；文档应区分“本机实证”与“官方通用契约”。定 P3。

### F4：限制描述基本准确；验证建议不充分

代码 `:533-565` 明确只检查一个 cwd-derived 目录，没有 reverse/cross-directory
lookup。只检查文件元数据、未读取内容的本机验证也重现了 README 所述反例：

```text
derived -Volumes-Extreme-SSD-Orca-workspaces-orca---orca : dir=yes, memory=no
actual  -Volumes-Extreme-SSD-Orca-projects-orca          : dir=yes, memory=yes
```

因此“本仓库可能取不到记忆”的警示准确，且方向是少读/不读。小问题是 README
`:106` 的“confirm that with claude_project_dirname(cwd)”只会计算名字，不会证明
该目录存在、含 `memory/MEMORY.md` 或 transcript 绑定有效；应要求检查实际派生
路径与记忆位置。定 P3。

### F5：修复准确

`claude_memory_hook.py` 对依据的说明已改为 Codex 官方 hooks 文档。官方 common
input fields 表列出 `cwd | string | Working directory for the session`，而
`UserPromptSubmit` 继承 common fields 并增加 `turn_id`、`prompt`。因此把 cwd 当
必填字符串的结论与引用来源都准确。

## 6. 测试套件

在精确候选 Git tree 的隔离导出目录运行任务指定命令：

```sh
cd claude-codex-memory-bridge
/usr/bin/python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 26 tests in 0.056s
OK
```

- hook tests：19
- installer tests：7
- failure：0
- error：0
- skip：0

作为增量对照，精确基线 `b9ce3e1e…` 的隔离 tree 为 `19/19` 通过，耗时
`0.033s`。候选新增 7 个 hook tests，但没有覆盖：双边 collision transcript、
句末 IPv6、普通 `::` 代码 negative controls、MAC 回归、非法 UTF-8 transcript、
跨行 relocated 或第 9 个 transcript。

## 7. 发现清单与最终判定

| ID | 级别 | 结论 |
|---|---|---|
| P1-R2-1 | **P1** | 两个真实 collision cwd 都有 transcript 时仍确定性共享/串读唯一 `MEMORY.md`；原 P1-1 未完整关闭 |
| P1-R2-2 | **P1** | 合法压缩 IPv6 后跟普通句号/冒号时原样泄漏；原 P1-3 未完整关闭 |
| A1 | 通过 | NFC、UTF-16、200 cap、DJB2/base36 与本机 Claude Code 2.1.233 一致，208/208 |
| R1 | P2 | 新 IPv6 候选会误删 C++、namespace、CSS pseudo-element 等普通文本 |
| R2 | P2 | MAC 地址相对基线不再脱敏 |
| T2-T5 | P2 | transcript authenticity、非法 UTF-8、跨行 relocated、任意前 8 个扫描问题 |
| D1 | P2 | 对真实 `j3/fWe/hJc` 的调用位置和比较语义注释不准确 |
| D2 | P3 | F3/F4 警示核心正确，但仍有过度绝对或不可执行的验证措辞 |
| F5 | 通过 | `cwd` 官方字段引用准确 |

**最终判定：NO-GO。不得安装或激活 commit
`01e65fd7f40d00feabdb66d78880fd8ba6297816`。**

重新送审前至少需要：

1. 明确产品边界。如果要求任意真实 cwd 隔离，collision 目录必须在歧义时拒绝，
   并加入“双 cwd、双 transcript、单 MEMORY.md”的完整 pipeline 回归。
2. 重做 IPv6 候选边界，加入句号/冒号、mapped、zone、CIDR、bracket positive
   cases 与 C++/namespace/CSS negative cases；明确 MAC 是否继续脱敏。
3. transcript parser 使用严格 UTF-8 fail-closed；`relocated` type/value 同行解析；
   取消任意 UUID-name 前 8 个选择，或采用可证明不会随机漏掉 owner transcript 的
   有界策略。
4. 修正 Claude binary parity 与 README 的绝对化措辞。
5. 对新 exact candidate 重跑完整测试和两份独立 max-effort 复核。
