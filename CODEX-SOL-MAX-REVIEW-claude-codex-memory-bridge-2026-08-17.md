# Codex 独立复核：claude-codex-memory-bridge

- 日期：2026-08-17（Asia/Taipei）
- 复核者：Codex（独立只读 reviewer，Dispatch `task_54101e1a0a09`）
- 目标 commit：`b9ce3e1e62259cb99112d50f972f94fc826c7e9c`
- 目标目录：`claude-codex-memory-bridge/`
- 最终结论：**NO-GO**

## 0. 启动态与只读边界

启动时收到 `ORCA_CONTEXT_NACK_V1`，原因是中央 reviewed source 的 wiki 新鲜度不匹配。因此本复核没有声称加载 Orca 中央上下文，只使用明确可见的仓库文件、本机只读 Git/文件系统证据，以及为任务要求而在 `TemporaryDirectory` 中创建并自动清理的合成探针。

未修改候选目录，未提交、合并、安装或激活 bridge，也未连接任何服务器。仓库在复核前已有大量与本候选无关的 dirty/untracked 内容，均保持不动；本次唯一保留的新文件是任务明确要求的本报告。

## 1. 候选身份冻结

| 检查 | 结果 |
|---|---|
| 当前 HEAD | `492d9b1482b6b2148c51358d81aa1fda6156bc64` |
| 目标 commit | `b9ce3e1e62259cb99112d50f972f94fc826c7e9c` |
| 目标 parent | `76e7d88ccbcb8f91ce89fed4fa9c1605dfcf285b` |
| 目录首次提交 | 是；parent 中没有该目录，目标提交新增 5 文件、1,866 行 |
| 目标 commit 目录 tree OID | `2d90e2f9dff134bace3685e29533d3fa20e28166` |
| 当前 HEAD 目录 tree OID | `2d90e2f9dff134bace3685e29533d3fa20e28166` |
| `git diff b9ce… HEAD -- claude-codex-memory-bridge` | 空 |
| `git status --short -- claude-codex-memory-bridge` | 空 |

现场目录因此与目标 commit 的候选字节完全一致。文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `README.md` | `8d58261ff3edc2ab4582f8ff9b80f5e1b4139d886906098a3b54ab447201ecaa` |
| `claude_memory_hook.py` | `8eee65483c0f057974374da2aa27d84dd4cc211f48574b5e42b907262bc1518a` |
| `install_bridge.py` | `08add5c165d655e02d4f5687e16203bd946d5fb46588408a6ae3c618ed62db54` |
| `tests/test_claude_memory_hook.py` | `5f40952bd96c65ff57d55c0a030dd64e1a1b83cc3da56685da88d56b033a190b` |
| `tests/test_install_bridge.py` | `1e1041711703d8af2ea2c291dcc2be7761b93c16ca5a70d2ef8e0c9be853bf4f` |

## 2. Commit-blocking findings

### P1-1：cwd 转换非单射，仍可确定性跨工作区读取记忆

位置：`claude-codex-memory-bridge/claude_memory_hook.py:313`、`:332`、`:335`、`:346-374`；缺失覆盖位于 `tests/test_claude_memory_hook.py:219-232`。

`claude_project_dirname()` 把 `/`、`-`、空格、`.`、CJK 等所有非 ASCII 字母数字字符都映射成同一个 `-`。这保证了没有原始路径分隔符，却不能建立一一对应的 cwd 命名空间。两个不同且真实存在的 cwd 可以映射到同一个 Claude project key：

```text
<tmp>/collision/team/app  -> <same-sanitized-name>
<tmp>/collision/team-app  -> <same-sanitized-name>
```

确定性复现使用真实临时目录、`0700` project/memory 目录、`0600` 普通 `MEMORY.md`，没有符号链接或不安全 mode：

```json
{"cwd_distinct":true,"dirname_equal":true,"attacker_received_victim_marker":true,"returned_project_ref":"ef52dbc8e735"}
```

也就是说，只为第一个 cwd 写入 `VICTIM_PRIVATE_91c2f0` 后，以第二个 cwd 构造完整 `hook.run()` 请求，输出仍包含 victim marker 和该 project 身份哈希。攻击者只需选择一个与目标路径 sanitizer-collision 的工作区名；不需要 `..`、绝对路径拼接、符号链接或权限漂移。对可能受 sandbox 限制的 Codex 会话而言，hook 主动注入了它本不应获得的另一工作区内容，重现了本候选声称已封闭的隐私边界问题。

现有 `test_only_returns_memory_for_the_requesting_workspace` 只使用 sanitizer 输出不同的 `alpha/beta` 型路径，所以无法发现该问题。

修复方向：不能只以 lossy 的 `<sanitized-cwd>` 作为授权边界。至少必须把可信、规范化的原始 cwd 与项目 key 做可验证绑定，并在绑定缺失或歧义时 fail closed；加入 `/x/y` vs `/x-y`、点/空格/CJK 等碰撞对的双向回归测试。若 Claude 自身对碰撞 key 也不区分，则 bridge 必须明确拒绝歧义，而不是把 Claude 的 lossy 存储 key 当成 cwd 身份证明。

### P1-2：`claude_project_dirname()` 与本机 Claude Code 2.1.233 的真实规则并不精确一致

位置：`claude-codex-memory-bridge/claude_memory_hook.py:313-332`；声明位于 `README.md:23-29` 与 `tests/test_claude_memory_hook.py:200-217`。

本机现场证据：

- `claude --version` 为 `2.1.233 (Claude Code)`。
- 当前真实目录 `~/.claude/projects/-Volumes-Extreme-SSD-Orca-workspaces-orca---orca` 存在，说明候选对当前短 BMP/CJK 路径的已知样例确实匹配。
- 但从同一已安装 Claude 二进制提取到实际 project-key 逻辑：先用 JavaScript `/[^a-zA-Z0-9]/g` 替换；常量上限为 200；超过上限时使用前 200 字符并追加基于原 cwd 的哈希后缀。候选没有 200 字符截断/哈希分支。
- JavaScript 正则没有 Unicode `u` 标志，非 BMP 字符按两个 UTF-16 surrogate code unit 替换；Python 逐 Unicode code point 遍历，只替换一次。

短非 BMP 复现：

```text
cwd=/tmp/emoji-😀-x
candidate: -tmp-emoji---x   (length 14)
Claude JS: -tmp-emoji----x  (length 15)
```

长路径复现（219 字符 cwd）：

```json
{"candidate_length":219,"claude_length":207,"equal":false,"claude_prefix_length":200,"claude_suffix":"-433otj"}
```

因此“逐字符替换且与真实规则精确一致”的核心前提只对已列出的短 BMP 样例成立。含 emoji 等非 BMP 字符或 sanitizer 后超过 200 字符的有效 workspace 会静默找不到 Claude 实际使用的目录；长于文件系统单组件上限时还会落入 `lstat()` 的 `ENAMETOOLONG`，最终虽 fail closed，但 bridge 的核心功能失效。

修复方向：以已安装、明确版本的 Claude 实现为规范，完整复刻 UTF-16 替换、200 字符上限与原 cwd 哈希算法，并增加非 BMP、恰好 200/201 字符及长中文路径测试。算法仍可能随 Claude 版本漂移，需有版本/契约漂移检测。

### P1-3：常见压缩 IPv6 不会被脱敏，会原样注入 Codex

位置：`claude-codex-memory-bridge/claude_memory_hook.py:393-414`。

`_IPV6_RE` 只能处理完整展开形式，不能处理最常见的 `::` 压缩。独立直接复现：

```json
{
  "expanded 2001:0db8:0000:0000:0000:ff00:0042:8329": "expanded [REDACTED_IP]",
  "compressed 2001:db8::1": "compressed 2001:db8::1",
  "linklocal fe80::1234": "linklocal fe80::1234",
  "loopback ::1": "loopback ::1"
}
```

这里使用的均为文档/本机保留形式，不是实际服务器地址。该 bridge 的用途正是把历史记忆跨 provider 注入，README 又声明会脱敏常见服务器地址；压缩 IPv6 原样输出会泄漏真实记忆中的服务器标识。现有测试只覆盖 IPv4。

修复方向：使用经验证的 `ipaddress` 解析/替换策略或覆盖所有合法 IPv6 压缩形态的边界实现，并加入压缩、IPv4-mapped、带 zone-id、括号 URL host 等测试。此项从 Git 无法判定是不是 cwd 修复“新引入”的回归，因为该目录在目标 commit 才首次进入历史；但它存在于送审的最终候选，仍属于安全阻断项。

## 3. 两个非碰撞真实 cwd 的隔离探针

为了单独确认这次修复的正向效果，在同一个真实临时 `source_root` 下创建两个真实 cwd、两个不同 Claude project 目录和两个 `0600` 记忆文件，并双向调用完整 `hook.run()`：

```json
{
  "names_distinct": true,
  "a_sees_a": true,
  "a_sees_b": false,
  "a_contains_own_ref": true,
  "a_contains_other_ref": false,
  "b_sees_b": true,
  "b_sees_a": false,
  "b_contains_own_ref": true,
  "b_contains_other_ref": false
}
```

结论：对 sanitizer 输出不碰撞的 cwd，本次从全目录遍历改为单目录查找确实消除了原始的无条件跨项目聚合；但 P1-1 表明它没有建立“任意两个不同 cwd 永不串读”的完整边界。

## 4. 路径穿越、绝对路径与符号链接评审

### 4.1 原始文件系统穿越

以下部分成立：

- `parse_hook_input()` 要求 cwd 是字符串、以 `/` 开头且不超过 4,096 字符（`:237-257`）。
- sanitizer 输出字符集只有 `[A-Za-z0-9-]`；`/`、`.`、NUL、换行、反斜线等都不会作为路径语法保留（`:313-332`）。
- lookup 还要求非空、无 `/`、无 `..`、`project.parent == source_root` 且 lexical relative-to 成立（`:346-359`）。
- 因此 `/../../target`、重复 `/`、绝对路径注入等不能让 `Path` 拼接逃出 `source_root`。

但这只是“不能越出目录”的保证，不是“cwd 身份不可冒充”的保证；P1-1 的 sanitizer collision 是命名空间层逃逸。

### 4.2 符号链接与 mode

- `source_root`、project、memory 目录最终都经过 owner、目录类型、非 symlink、非 group/world-writable 检查（`:260-270`、`:344-364`）。
- `MEMORY.md` 通过 `lstat` 拒绝 symlink/非普通文件/错误 owner/group-world-write/超限；open 使用 `O_NOFOLLOW`，并比较 open 前后的 dev/inode/size/mtime（`:273-310`）。
- 直接 project symlink、memory-dir symlink、file symlink 探针都得到空输出。

仍有典型的同 UID parent-directory swap TOCTOU 窗口：目录分别 `lstat()` 后，最终 file open 不是基于固定的目录 fd 与逐层 `openat(O_NOFOLLOW)`。本次没有把它单列为额外 P1，因为能执行精确同 UID race 的进程本就能直接读这些同 UID 文件；但若威胁模型要求抵抗同 UID 恶意并发，需改为目录 fd 链式打开并复核身份。

## 5. Fail-closed 与“不阻塞 Codex”现场验证

使用候选脚本的真实 CLI `main()`、真实 `diskutil` volume gate、真实临时文件系统和每例 5 秒 timeout。有效控制例 `rc=0` 且有输出；以下所有异常例均 `rc=0`、stdout/stderr 为空，耗时约 152–252 ms：

| 场景 | 结果 |
|---|---|
| cwd 缺失 | fail closed，空输出 |
| cwd 相对路径 | fail closed，空输出 |
| cwd 错误类型 | fail closed，空输出 |
| 找不到对应 Claude project | fail closed，空输出 |
| project 是 symlink | fail closed，空输出 |
| memory 目录是 symlink | fail closed，空输出 |
| `MEMORY.md` 是 symlink | fail closed，空输出 |
| project group-writable | fail closed，空输出 |
| memory 目录 group-writable | fail closed，空输出 |
| `MEMORY.md` group-writable | fail closed，空输出 |
| `MEMORY.md` 是 FIFO | fail closed，空输出且未阻塞 |
| `MEMORY.md` 非法 UTF-8 | fail closed，空输出 |

因此任务 (c) 所列的常规缺失/非法/不安全 steady-state 行为成立。注意：`run()` 内部会抛 `BridgeError`，真正“不阻塞、空输出”由 CLI `main()` 的 catch-and-return-0 实现（`:599-629`）；本探针验证的是最终 CLI 行为。

## 6. 官方测试套件

在候选目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -v
```

结果：**19/19 通过，0 failure，0 error，耗时 0.034s**。

测试明细为 hook 12 项、installer 7 项。使用 `PYTHONDONTWRITEBYTECODE=1` 是为了不在只读候选中生成新缓存；目录内已有的 4 个 `.pyc` mtime 均早于本 Dispatch（其中本日文件为 10:43:37，本 Dispatch 约 10:47 后开始），不是本次测试生成。

全绿不改变 NO-GO：测试没有 collision、Claude 200 字符/hash、非 BMP UTF-16 或压缩 IPv6 覆盖。

## 7. redact / rank / build_context 回归检查

由于该目录在目标 commit 才首次提交，Git 中不存在可与“修复前版本”做字节 diff 的基线；本节评审的是送审最终实现本身。

已确认：

- query token overlap 仍只选择相关 block，并有确定性排序（`:468-493`）。
- Markdown 历史文本被包入单行 JSON record；独立恶意 Markdown 探针没有产生裸 `SYSTEM OVERRIDE` 顶层行（`:496-558`）。
- 代表性 token/password/IPv4/email/home path 脱敏测试全绿；独立 token 探针也成功移除 `ghp_` marker。
- UTF-8 output cap 测试全绿；`max_blocks` 与最终输出字节数仍受限。

发现的回归/契约缺口：

- P1-3：压缩 IPv6 未脱敏。
- 较低严重度：`Limits.max_total_bytes` 与 `max_files` 在改成单文件 lookup 后不再参与 `read_memory_documents()`；当前只把 `max_file_bytes` 传给 `_read_memory_file()`（`:335-374`）。单文件内容可大于策略的 `max_total_bytes`（但仍受 `max_file_bytes <= 262,144` 和最终输出 7,000 bytes 硬上限），建议清理失效策略字段或执行 `min(max_file_bytes, max_total_bytes)`。

## 8. 最终判定

**NO-GO：不得安装或激活 commit `b9ce3e1e62259cb99112d50f972f94fc826c7e9c`。**

正向修复确实把“遍历所有 Claude projects”收缩成“查一个 key”，且常规错误/不安全文件均快速 fail closed；但 P1-1 给出了无 symlink、权限安全、双真实 cwd 的确定性跨工作区泄漏复现，直接否定“任意不同 cwd 永不读取对方记忆”的验收目标。P1-2 又证明核心命名规则并未完整复刻本机 Claude Code 2.1.233，P1-3 则会把常见压缩 IPv6 原样跨 provider 注入。三项修复后必须针对新 exact candidate 重跑测试与独立双复核。
