# Codex sol/xhigh 独立复核（第四轮）— claude-codex-memory-bridge

- **复核日期**：2026-08-17
- **审查候选**：`1b93ad8c1caa5b1eace75cb0c041b81151213f6e`
- **对比基线**：`1003f5dd9630e5ef5a054fbfd3e4a2cfef2233c0`
- **审查范围**：`claude-codex-memory-bridge/`
- **候选目录 tree**：`65dc341718dcdd3c1bc2d804fd2ef148ad1dcc61`
- **性质**：只读独立复核；未修改候选、未提交、未合并、未安装、未激活。唯一持久写入是本报告；所有自建 transcript、策略和 runtime 都位于自动清理的临时目录。
- **启动降级状态**：收到 `ORCA_CONTEXT_NACK_V1`（central reviewed source freshness mismatch: wiki）。本报告不声称加载了 Orca 中央上下文，只依赖可见项目文件、精确 Git 对象和本轮实时只读输出。
- **独立性**：未读取并行第四轮复核者的过程或结论；只读取任务明确指定的第三轮历史报告，用它确定待关闭的 R3 编号，所有探针均重新独立构造。
- **运行中 HEAD 漂移**：开始冻结身份时 `HEAD=1b93ad8c...`；最终复核时外部并发提交已把 HEAD 推进到其直接子提交 `1db10738349af8b5a40a94f122f499a84f27e17d`。只读核对证明该提交仅修改 `reports/COMPLETION-BACKLOG-2026-08-16.md`，且两者的 `claude-codex-memory-bridge/` tree 均为 `65dc341718dcdd3c1bc2d804fd2ef148ad1dcc61`；因此本报告继续只对精确候选 commit/tree 给出结论，不把它冒充为当前 HEAD 的整仓验收。

## 结论：**GO**

在精确候选 `1b93ad8c1caa5b1eace75cb0c041b81151213f6e` 上，R3-P1-1、R3-P2-1、R3-P2-2 均已闭合；独立完整链路探针、精确基线差分和 46/46 测试未发现可复现 P0/P1，也未发现本轮新增功能回归。

本结论仅适用于上述 commit 与 `claude-codex-memory-bridge/` 目录，不授权安装、激活、合并或发布。

## 1. 候选身份与增量

复核开始时：

- `HEAD` 与请求候选完全一致：`1b93ad8c1caa5b1eace75cb0c041b81151213f6e`。
- 基线完整 SHA：`1003f5dd9630e5ef5a054fbfd3e4a2cfef2233c0`。
- `git diff 1003f5dd... 1b93ad8... -- claude-codex-memory-bridge/` 仅修改 3 个文件：
  - `README.md`：+34/-12（总 diff 统计口径为 46 行变化）；
  - `claude_memory_hook.py`：+86/-29；
  - `tests/test_claude_memory_hook.py`：+84/-0。
- 整体统计：3 files changed，202 insertions，43 deletions；`git diff --check` 无输出。
- 目标目录没有本地工作区修改；仓库存在大量任务外既有 dirty/untracked 项，本轮全部保留且未触碰。
- 运行中 HEAD 从候选推进到直接子提交 `1db10738349af8b5a40a94f122f499a84f27e17d`；该漂移只改一份任务外 backlog 报告，目标目录 tree 与候选仍逐字节一致。为避免身份混淆，本文的测试与裁决始终绑定 `1b93ad8...:claude-codex-memory-bridge` 的 tree `65dc3417...`，不扩大到新 HEAD 的整仓内容。

## 2. (a) R3-P1-1：扫描窗口外冲突与超限 fail closed

### 静态核对

`claude_memory_hook.py:727-759` 的现实现为：

1. 先一次性收集目录内所有 `.jsonl` 条目；
2. `len(jsonl_entries) > 256` 时在扫描前直接返回 `False`；
3. 数量在预算内时遍历完整列表；旧版 `checked >= cap -> break` 已不存在；
4. 任一可信 transcript 的 plain/relocated 候选均不含请求 cwd 时立即拒绝；只有完整扫描结束后才返回 `own_match_found`。

因此排序只影响“何时遇到冲突”，不再影响最终结论。

### 独立完整链路探针

我没有复用项目的 `HookFixture`，而是另建临时 rig，真实创建 `0700` 项目/记忆目录、`0600` UUID 命名 transcript、`0600` MEMORY.md、hash-pinned 脚本副本和策略，并驱动完整 `hook.run()`。

场景 A：两个真实存在、sanitize 后同名的 cwd；共享目录共 41 条 transcript，其中 40 条诚实记录 A，恰好 1 条诚实记录 B。

| 排序 | A 输出 | B 输出 | 私有标记 |
|---|---:|---:|---|
| B 冲突 transcript 为最旧 mtime | 0 B | 0 B | 双方均不可见 |
| B 冲突 transcript 为最新 mtime | 0 B | 0 B | 双方均不可见 |

两种排序下 `_session_recorded_cwd_matches(project, A/B)` 也均为 `(False, False)`。这直接覆盖旧的 16 条窗口之外位置，并确认双向拒绝。

场景 B：单一真实 cwd、所有 transcript 完全一致，无碰撞。

| transcript 数 | 直接 match | 完整输出 |
|---:|---|---|
| 256（精确上限） | `True` | 470 B，包含所有者标记 |
| 257（上限 + 1） | `False` | 0 B |

这确认实现是严格的 `> 256` fail closed，不是部分扫描后继续服务，也没有把恰好 256 错拒。

### 与精确基线的同 rig 差分

同一形状在 `1003f5dd...` 与 `1b93ad8...` 上分别装载并执行：

| 场景 | 基线 `1003f5dd...` | 候选 `1b93ad8...` |
|---|---|---|
| 20 条 A 更新 transcript + 1 条最旧 B 冲突，A 请求 | **470 B，含私有标记（复现 R3-P1-1）** | **0 B** |
| 257 条全部一致、所有者请求 | 469 B，仍服务 | **0 B，fail closed** |

探针在基线上确实复现旧问题，因此候选的 0 字节结果不是“空跑即通过”。

**判定：R3-P1-1 已修复。**

## 3. (b) R3-P2-1、真实碰撞与 T4

### relocated 不再制造所有者假碰撞

独立构造一个 transcript：头部 plain `cwd` 精确等于请求所有者，尾部同 sessionId 的 `type:"relocated"` 指向一个不相关第三方 cwd。候选 `_session_recorded_cwds()` 返回精确二元组 `(owner, third_party)`；完整 `hook.run(owner)` 返回 486 B，并包含所有者记忆标记。

同 rig 对比：

- 基线 `1003f5dd...`：0 B，误拒所有者；
- 候选 `1b93ad8...`：470 B，包含所有者标记。

### 真正的碰撞没有被放宽

另建两个真实、sanitize 后同名的 cwd，各有一条 UUID/sessionId 一致、plain cwd 分别诚实记录 A/B 的 transcript。候选完整链路结果为 A=0 B、B=0 B；双向拒绝仍成立。

### T4 原始 relocated 场景仍成立

构造 old cwd 与 new cwd sanitize 后同名，唯一 transcript 的 plain cwd 为 old、尾部 relocated 目标为 new，记忆位于共享派生目录。由 new cwd 请求时：

- 候选返回 474 B，并包含 `T4_RELOCATED_MARKER_WORKS`；
- 精确基线/候选同 rig 差分均返回 457 B 并含 T4 标记。

说明本次拆分 plain/relocated 没有破坏 T4；对两个诚实不同 cwd 的双向拒绝也没有放宽。

**判定：R3-P2-1 已修复，未发现关联新回归。**

## 4. (c) README 与 `read_memory_documents()` 注释

- `README.md:60-82` 已明确从旧的“共享 MEMORY.md”改为“真实碰撞时所有 cwd 全部拒绝”，并准确记录 256 上限、超限 fail closed、旧 16 窗口绕过。
- `README.md:83-93` 准确区分“不同派生目录的 relocation 仍不做反向查找”与“同一派生目录内 relocated target 可作为所有权证据，且不单独制造第二工作区冲突”。
- `claude_memory_hook.py:775-787` 的 `read_memory_documents()` 注释已删除第三轮指出的旧语义，现明确写为真实碰撞时对所有人拒绝，并指向 `_session_recorded_cwd_matches` 获取完整判定说明。
- 256/fail-closed 与 plain/relocated 二元组的细节分别在常量注释（约 47-66 行）和 enforcement 函数注释（约 644-759 行）完整说明；函数局部注释没有逐字重复，但不存在相反或过期陈述。
- 对整个目录搜索旧函数名、旧“only additionally requires”语句和旧部分扫描语义，未发现残留可执行调用或矛盾文档；“serves them the one shared memory”仅作为与 Claude Code 本身行为的对比出现。

**判定：R3-P2-2 已修复；未发现实质遗漏或语义矛盾。**

## 5. (d) 测试套件

执行指定命令：

```text
cd claude-codex-memory-bridge
/usr/bin/python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 46 tests in 0.163s
OK
```

- 46/46 通过；0 failure、0 error、0 skip、0 expected-failure。
- 包含新增的三个直接回归测试，以及此前 hook 与 installer 测试。

## 6. (e) 既有修复抽查

本轮增量没有触及 IPv4/IPv6/MAC、JSONL 严格解码、cwd dirname 变换或 installer 主逻辑。除全套测试外，另做了小型直接抽查：

| 项 | 本轮证据 | 结果 |
|---|---|---|
| P1-R2-2 IPv6 句末 `.` / `:` | `2001:db8::1.` → `[REDACTED_IP].`；末尾 `:` 同理保留标点 | PASS |
| N9 zone id | `fe80::1%eth0` 整体变为 `[REDACTED_IP]` | PASS |
| R1 代码正文 | `std::vector<int> Foo::Bar` 原样保留 | PASS |
| R2 MAC | `00:1A:2B:3C:4D:5E` → `[REDACTED_IP]` | PASS |
| dirname UTF-16 语义 | `/tmp/emoji-😀-x` → `-tmp-emoji----x` | PASS |
| T2/T3/T5 | UUID+sessionId gate、非法 UTF-8 行 fail closed、mtime/多 transcript 路径的既有测试均通过 | PASS |
| T4 | 上述独立完整链路正向探针 | PASS |
| D1 | 相关 docstring 未被本轮增量改动，未见文字回退 | PASS（静态边界） |

D1 的 Claude 二进制符号行为语义本轮未重新反汇编验证；这里只确认候选增量没有改动该说明，且相关映射回归测试仍通过。

## 7. (f) 新回归检查

- `_session_recorded_cwd` → `_session_recorded_cwds` 的仓库 Python 调用点已全部同步；全仓 Python 搜索仅余新函数定义与唯一生产调用，没有旧函数残留。
- 精确候选差分没有触及脱敏、排序/输出构造、安装事务代码；对应既有测试全绿。
- 在 relocated 所有者、诚实双碰撞、T4、256/257 边界、冲突 mtime 首尾五组相互约束场景中，没有发现安全规则被意外放宽或正常所有者被新增误拒。
- 未发现新增 P0/P1/P2 功能回归。
- 第三轮报告记录的既有 P3（zone-id 可能过度吞相邻正文、`:::` 形状残留、全 hex 标识符歧义）不在本轮增量内，代码也未触及；本 GO 不把它们表述为已修复。

## 8. 诚实边界与未执行事项

- 未安装、未激活、未运行真实 Codex hook 接入，因此不把临时 rig 结果表述为生产账号激活验证。
- 未读取真实 `~/.claude/projects/` 内容，也未实时复核 README 中“本机四组碰撞”等历史数量；这些是可漂移的环境事实，不影响本次精确候选代码结论。
- 未重新反汇编 Claude Code 二进制，不为 D1 中外部二进制符号的行为语义新增背书。
- 独立 rig 最初两次分别在自建上下文管理器和 Python 3.9 临时模块登记处启动失败；两次都在进入候选判断前失败。修正 harness 后原样重跑并通过，失败不计入候选结果。
- 运行中出现的 HEAD 漂移已如实记录；虽然目标目录 tree 未变，协调者若要验收“当前 HEAD 整体”仍需另行冻结并复核那个整仓身份，本报告不能代替。

## 最终裁决

**GO — commit `1b93ad8c1caa5b1eace75cb0c041b81151213f6e` 的 `claude-codex-memory-bridge/` 第四轮 Codex 独立复核通过。**

未发现可复现 P0/P1；R3-P1-1、R3-P2-1、R3-P2-2 的直接回归、精确基线差分、完整 46 项测试与既有修复抽查均通过。
