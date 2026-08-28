# OPUS5 独立只读复核 — install_bridge.py 第 10 轮（复核路 B，dispatch `ctx_bc12524eb5cd`）

> **文件名说明**：本轮两路并行复核被指派了同一个报告文件名
> `OPUS5-INDEPENDENT-REVIEW-install-bridge-ROUND10-2026-08-17.md`。另一路（其报告
> 自述 dispatch `ctx_68ce719674e3`、capability 已被回收、无法发送心跳与 worker_done、
> 结论 **GO**）于 2026-08-18 03:52 覆盖了我先写入的同名文件。我**没有**改动或删除
> 那份文件，改把本报告写在这个 dispatch 专属的文件名下。两份报告都应被视为有效
> 且互相独立——它们的结论不同（对方 GO，本路 NO-GO），分歧点见文末"与另一路的
> 分歧"一节。

- **候选**：`6fb376c671` (`fix(install_bridge): guard the install-rollback commit and stop treating unreadable as unowned`)
- **基线**：`cb1c532459`（第 9 轮候选）；另用 `37d2433a77`（第 8 轮候选）做测试非空转对照
- **复核者**：Claude opus5 / max，独立复核，未与并行派出的另一路交换任何信息
- **日期**：2026-08-17（会话日期 2026-08-18）
- **范围**：`claude-codex-memory-bridge/install_bridge.py` 与 `claude-codex-memory-bridge/tests/test_install_bridge.py`
- **解释器**：`/usr/bin/python3` 3.9.6 与 PATH `python3` 3.14.6
- **只读约束遵守情况**：未修改、提交、合并、安装或激活候选（复核结束时
  `git status --porcelain -- claude-codex-memory-bridge/` 为空，两个文件的 sha256
  与 `6fb376c671` 一致）。所有复现均在
  `…/scratchpad/r10/` 下的一次性假 SSD 根目录中、由独立子进程完成。对本机真实
  `/Volumes/Extreme SSD/Orca/local-homes` **只做过读取**（`os.walk` + `os.lstat` +
  一次只读调用 `_find_untracked_owned_configs()`），从未对真实 Codex `hooks.json`
  运行 install/uninstall/recover。

---

## 结论：**NO-GO**

第 9 轮的两个阻断项 **R9-P1-A 与 R9-P1-B 都真正修好了**，我用自己独立构造的场景在
两个解释器上都验证过；82/82 测试双解释器真实全绿；第 1–9 轮已确认的修复我另做了
17 项端到端抽查，**无任何回归**；候选自带的 3 个新测试我也全部做了非空转验证，
都名副其实。

但本轮**新引入了一个可确定性复现的阻断级缺陷**：把"未追踪 owned handler"扫描
挪到 `recover_pending_install()` 的共享位置之后，**install 侧的这次扫描只可能在
install 已经把一切都写完、提交完之后才发生**——所以它在 install 方向上并不能
"提前阻止"任何事，只能把一次**实际已经成功的安装**变成一句谎报的
`"install failed …"` 外加一个卡住的 pending journal。而新的
`_read_for_detection()` 又把"读不出来"升级成了硬失败，于是 local-homes 下
**任意一个与本安装器完全无关的、当前 uid 读不了的 `hooks.json`**（或超过 4 MiB 的）
就足以触发它——这恰恰是本轮设计注释里明文声称已经避开的那种拒绝服务。

- **R10-P1-A（阻断）**：见下文。确定性复现，无竞态、无特权要求，基线上完全无害。

> 公平起见先说清楚三点：①本轮在"不再静默抛弃活着的 handler"这条主轴上是真实
> 进步，比第 9 轮严格更安全；②R10-P1-A 的触发前提**今天在真实目标机上并不成立**
> （我实测 local-homes 下 49145 个文件全部属主为 uid 501，无不可读、无超大
> `hooks.json`）；③如果协调方把"可用性/谎报类回归"统一定级为 P2，那么本轮在
> "抛弃活 handler"这条轴上是可以放行的。但按本项目九轮以来一贯的定级习惯——
> "工具报告的状态与磁盘真实状态背离"每一次都被判为 P1（P1-1、P2-3、R4-P1-A、
> R6-P1-A、R7-P1-A、R8-P1-A、R9-P1-A/B 全是这个签名）——我给 P1，判 NO-GO。

---

## (a) R9-P1-A 是否真正修复 —— **是，已确认修复**

我没有复用候选测试的 kill 点、账户名或字段。我的构造：

- **三个**池账户 `pool-alpha` / `pool-bravo` / `pool-charlie`（候选测试用一个 `acct-one`）
- 每份 pristine 配置里预置**两个**互不相关的既有 handler（其中一个 command 里
  还故意包含 `--bridge-id other-tool`，用来同时压测精确匹配）
- **kill 点按目标表达而不是按调用序号**：patch `atomic_write`，当且仅当
  `Path(path).name == "latest-receipt.json"` 时 `os.kill(os.getpid(), SIGKILL)`
  ——即"所有 live 配置全部写完并 fsync，但安装尚未提交"这一刻，
  与候选测试数出来的 `#11` 是完全独立的表达方式
- 全过程走独立子进程（`kill_install.py` / `cli.py`），每个动作一个新进程，模拟真实 CLI

### 基线 `cb1c532459` 上确实复现 R9-P1-A

```
sigkill_install        returncode=-9 (真 SIGKILL)
post_kill_state        pending=True  latest=False  owned={alpha:1,bravo:1,charlie:1,main:1}
relocated              codex-accounts/pool-bravo -> pool-bravo-RETIRED  (owned=1，handler 仍活着)
after_relocate::recover  rc=0  ->  {"ok": true, "state": "rolled_back"}      ← 谎报成功
after_relocate::…::disk  pending=False  latest=False  owned_at_moved=1        ← journal 被删，
                                                                                receipt 从未存在过
final_state            owned={alpha:0, bravo:1, charlie:0, main:0}            ← 永久孤儿
```

完全符合报告描述：**"回滚成功"、pending 被删、改名账户的 handler 仍活着、
而且从头到尾没有任何 receipt 存在过来揭穿它。**

### 候选 `6fb376c671` 上已被挡住

```
after_relocate::recover    rc=1  "refusing to finish pending install/uninstall: found an owned hook
                                  handler at a path the current receipt does not track … pool-bravo-RETIRED/home/hooks.json"
after_relocate::uninstall  rc=1  同一条拒绝
after_relocate::install    rc=1  同一条拒绝
after_relocate::verify     rc=1  "pending install journal must be recovered first"
每一步之后 disk: pending=True  latest=False  owned_at_moved=1   ← 什么都没提交，配置未被动过
after_restore::recover     rc=0  {"ok": true, "state": "rolled_back"}
final_state                owned={alpha:0, bravo:0, charlie:0, main:0}  pending=False
```

**四条恢复路径全部拒绝；journal 存活；把目录改回原名后回滚正常完成，全部账户
回到 0 个 owned handler，没有永久锁死。** ✅

### "挪到更深位置"的隐蔽变体

任务里点名要验的那个变体（挪到扫描原本看不到的更深位置、之后再 install 会不会
意外成功并重建孤儿账户的 release 目录）：

| 迁移形态 | 候选行为 |
|---|---|
| 同级改名 `codex-accounts/pool-bravo-RETIRED` | **拒绝** ✅ |
| 更深层 `codex-accounts/attic/nested/pool-bravo` | **拒绝** ✅（`os.walk` 任意深度） |
| 移出 local-homes（仍在同一 SSD）`Orca/retired-accounts/` | 未挡住 ⚠️ = 已披露的 **R9-P2-A** |
| 移到 local-homes 外 + 在 local-homes 里放符号链接指过去 | 未挡住 ⚠️ 同上（见 (f)③） |

后两种我把完整链路跑到底，确认了 R9-P2-A 的实际危害程度比"扫描看不见"这句话
更重：

```
after_relocate::recover   rc=0  state=rolled_back        ← 提交
after_relocate::install   rc=0  安装成功                 ← 重新建出 release 目录
final_state  owned={alpha:1, bravo:1, charlie:1, main:1} latest=True
```

即**孤儿账户与新装的一整棵树同时活着，两边都在每次 Codex prompt 上执行**，而
receipt 只认识其中一棵。这是本轮刻意未处理的已知边界，不算新发现，但它的
后果值得在下一轮定级时如实计入。

---

## (b) R9-P1-B 是否真正修复 —— **是，检测侧已修复；但反向引入了新问题**

### 检测侧：改名账户 + 异常权限/大小 → 仍然正确拒绝 ✅

我没有复用候选测试的 `0o664`，改用四组不同的位组合加一组超大文件：

| 改名账户配置的状态 | 基线 `cb1c532459` | 候选 `6fb376c671` |
|---|---|---|
| `chmod 0606`（o+w，无 g+w） | **rc=0 静默卸载**，receipt 删除，handler 仍活 ❌ | **拒绝** ✅ |
| `chmod 0777` | **rc=0 静默卸载** ❌ | **拒绝** ✅ |
| `chmod 0400`（只读，仍可读） | 拒绝（`validate_owned_file` 本就放行 0400） | 拒绝 ✅ |
| `chmod 0200`（只写，读不出来） | **rc=0 静默卸载** ❌ | **拒绝** ✅ |
| 内容撑到 5 MiB（> `MAX_MANAGED_FILE_BYTES`） | **rc=0 静默卸载** ❌ | **拒绝** ✅ |

候选每一例都保留了 `latest-receipt.json`、没有落任何 pending journal、
改名账户的 handler 一个不少。R9-P1-B 的检测侧确实堵上了。

> 属主变更（`sudo` 还原造成的 root:0600）无法在无 sudo 的复核环境里直接构造。
> 但它命中的是 `_read_for_detection()` 里**完全相同**的 `os.open → EACCES` 分支，
> 与我用 `chmod 0000` / `0200` 验证的路径一致，因此结论可外推。

### 假阳性侧：**这里出了新问题**（见 R10-P1-A）

| local-homes 下一个与本安装器无关的 `hooks.json` | 基线 `cb1c532459` | 候选 `6fb376c671` |
|---|---|---|
| 6 份真实形状的插件市场 hook 配置，`0644` | uninstall rc=0 ✅ | uninstall rc=0 ✅ |
| 单份 `0666`（组+其它可写） | uninstall rc=0 ✅ | uninstall rc=0 ✅ |
| 单份 `0000`（读不出来） | **uninstall rc=0 ✅** | **uninstall rc=1 ❌ `cannot read <无关文件>`** |
| 单份 5 MiB（`0644`，完全可读） | **uninstall rc=0 ✅** | **uninstall rc=1 ❌ `cannot safely inspect …: too large`** |

`_read_for_detection()` 的注释写着：

> …giving any one of them a permanent veto over uninstall the moment its mode or
> owner looks unusual would trade R9-P1-B's silent-abandonment risk for an equally
> real denial-of-service risk — **so only actual read failures escalate, not
> "not owned by us" or "not private"**.

这个区分在现实里不成立：一个 root 属主 `0600` 的无关文件"不属于我们"**同时就是**
一次真实的读取失败；一个 `0000` 的无关文件"权限不够私密"**同时就是**一次真实的
读取失败。这两类根本无法用"是不是真的读不出来"分开——它们是同一件事。
并且"超过 4 MiB"既不是"不属于我们"也不是读取失败，文件完全可读，却照样升级成
永久否决。

---

## R10-P1-A（阻断，本轮新引入）：无关文件可以让 install 谎报失败并卡住事务日志

**严重级**：P1（可复现，无竞态、无特权；基线上完全无害）
**位置**：`install_bridge.py:916-939`（共享扫描的落点）、`:195-250`
（`_read_for_detection()`）、`:1310` 与 `:1313-1322`（install 的提交调用与
异常处理）

### 复现步骤（确定性，我已在两个解释器上跑通）

1. 一台**全新、从未安装过**的机器（fake SSD，三个池账户）。
2. 在 `local-homes` 下放一个与本安装器毫无关系的第三方 hook 配置——形状取自本机
   真实存在的那 6 份：
   `local-homes/.claude/plugins/marketplaces/vendor-x/plugins/guard/hooks/hooks.json`，
   `chmod 0000`（等价触发：root 属主 `0600`，或任何 >4 MiB 的同名文件）。
3. 运行 `install`。

### 观察到的结果（候选）

```
install                rc=1  {"ok": false, "error": "install failed and the durable recovery journal remains pending"}
disk_after_install     pending=True   latest=True
                       owned_handlers_live = {pool-alpha:1, pool-bravo:1, pool-charlie:1, <main>:1}
                                                        ↑↑↑ 安装其实完整成功了，桥接全部生效
wedged::recover        rc=1  "cannot read …/vendor-x/plugins/guard/hooks/hooks.json"
wedged::verify         rc=1  "pending install journal must be recovered first"
wedged::uninstall      rc=1  "cannot read …"
wedged::install        rc=1  "cannot read …"
wedged::plan           rc=0  （只剩 plan 还能答话）
--- 把那个无关文件 chmod 0644 之后 ---
recover                rc=0  {"ok": true, "state": "committed"}
```

同一输入在基线 `cb1c532459` 上：`install` rc=0，`verify` rc=0，`uninstall` rc=0，
`recover` rc=0 —— **完全无害**。

### 为什么这是 P1 而不只是"更保守了"

1. **工具报告的状态与磁盘真实状态背离。** 操作员看到的是"install failed"，
   而四份 Codex 配置已经全部桥接、`latest-receipt.json` 已经写下、hook 会在
   之后**每一次 Codex prompt 上真实执行**。操作员合理的下一步是"再装一次"
   （又失败）→ 得出"没装上"的结论 → 桥接在背后一直跑。这正是 P1-1 / P2-3 /
   R6-P1-A / R7-P1-A 这一串被判 P1 的同一个签名。
2. **在 install 方向上，这次"fail closed"什么都没能阻止。** 共享扫描的落点在
   `recover_pending_install()` 里，而 install 只在 `install_bridge.py:1310`——
   即所有 live 配置写完、`latest-receipt.json` 也写完之后——才第一次触到它。
   对比 `uninstall()`：它在 `:1444-1451` 于**写 journal 之前**跑同一个扫描，
   所以拒绝就是干净的"什么都没发生"。install 侧缺的正是这个对称落点。
3. **触发源与本工具无关。** 被否决的文件不是本安装器写的、不归它管、里面也没有
   它的 handler。本轮注释明文声称避开了这一点，实际没有。
4. **错误信息丢失了原因。** `install()` 的 `except BaseException`
   （`:1313-1322`）把真实原因换成
   `"install failed and the durable recovery journal remains pending"`，
   `main()` 只打印外层 `str(exc)`，`__cause__` 被丢弃。操作员必须再跑一次
   `recover` 才能看到"到底是哪个文件"。
5. **同一个楔子还有另外两个更普通的触发源**（我都实测到了）：
   - 一个用**硬链接**做镜像的备份工具在 local-homes 下留下一份 `hooks.json`
     （同 inode！）→ `install` 同样 rc=1 + pending journal 卡住；
   - 一个**真正被改名**的受管账户 → `install` 同样只在提交后才拒绝，
     报同一句无信息量的错（详见 (f)① 的 scenario_d）。

### 真实机相关性

今天不会触发：我实测真实 `local-homes` 下 49145 个文件**全部**属主为 uid 501，
9 份 `hooks.json` 全部可读、全部 < 6 KB，`_find_untracked_owned_configs()`
只读调用返回 `[]`。但那 6 份无关 `hooks.json` 位于
`.claude/plugins/marketplaces/claude-plugins-official/` —— 一棵由 **Claude Code
插件更新器另行刷新**的 git 树；再加上任务书自己点名的"sudo 驱动的还原"，
把前提变成真只需要一次 `chmod` / 一次 sudo 拷贝 / 一次归档还原。

### 建议修法（仅供参考，本次未实施）

1. 在 `install()` 里、`write_runtime()` 与 journal 之前**也**跑一次扫描
   （对齐 `uninstall()` 在 `:1444` 的落点）。`recover_pending_install()` 里
   共享的那一次保留不动——那部分是对的，是 R9-P1-A 的正解。
2. `install()` 的 `except BaseException` 把 `str(recovery_exc)` 拼进对外消息，
   不要丢掉原因。
3. `_read_for_detection()` 里把 ENOENT 归回"不存在"（见 P3-A），并对
   EACCES / 超大做一次明确取舍：要么承认这是有意的拒绝服务并在消息里说清
   （"因为无法检查某个无关文件而拒绝"），要么把升级范围收窄到
   `codex-accounts/` 子树——受管账户唯一可能出现的地方——其余位置读不出来就跳过。

---

## (c) 顺手修的测试非空转问题 —— **属实，且新测试确实非空转** ✅

我把**第 10 轮的测试文件**放到**第 8 轮的 `install_bridge.py`(`37d2433a77`)** 旁边直接跑：

```
test_uninstall_ignores_an_untracked_config_with_malformed_content_at_the_one_level_shape ... ERROR
  File ".../hybrid-r8/install_bridge.py", line 1254, in uninstall
    candidate_payload.get("hooks", {}).get("UserPromptSubmit", [])
AttributeError: 'list' object has no attribute 'get'
```

**裸 `AttributeError` 精确复现** ✅ ，而它在候选上属于 82/82 全绿的一部分。

同时确认了"旧测试确实空转"这个自述：

```
test_uninstall_ignores_untracked_configs_with_malformed_or_unexpected_content ... ok   ← 对着第 8 轮也过
```

顺带把候选另外两个新测试也做了非空转验证（对着本轮基线 `cb1c532459`）：

```
InstallCrashRecoveryTests.test_sigkill_mid_install_then_relocating_… ... FAIL  (AssertionError: InstallError not raised)
test_uninstall_refuses_a_relocated_account_even_after_its_mode_or_owner_looks_unusual ... FAIL  (同上)
```

**候选自述的三条非空转声明全部属实。**

---

## (d) 测试套件 —— **82/82，双解释器真实全绿** ✅

```
### /usr/bin/python3 Python 3.9.6 ###
Ran 82 tests in 2.680s
OK
### PATH python3 Python 3.14.6 ###
Ran 82 tests in 2.557s
OK
```

`grep -c "    def test_"` = **43** 个测试方法（第 9 轮 40 → 本轮 43，与自述一致）。
两个解释器上 `_read_for_detection()` 的逐条行为也完全一致（见 (f)②）。

---

## (e) 第 1–9 轮已确认修复的抽查 —— **17/17 无回归** ✅

除 82 个自带测试外，我另写了一组**不走候选 unittest fixture** 的端到端抽查
（每个动作一个独立子进程，模拟真实 CLI）：

| 抽查项 | 结果 |
|---|---|
| P1-1 连装三次后 uninstall 仍还原到真正 pristine（字节 + mode） | PASS |
| P1-2 uninstall 清除 `latest-receipt.json`；之后 verify 报 not installed | PASS |
| `owned_handler` 精确匹配：`# --bridge-id <ID>` 注释形态 / `<ID>-suffix` 形态均未被误删 | PASS |
| R3-P1-A 永久删除的账户：verify / uninstall 都容忍并报 unreachable | PASS |
| R6-P1-A install 拒绝把已装配置当 pristine 收养 | PASS |
| R7-P1-A uninstall 侧未追踪扫描同样拒绝 | PASS |
| R5-P1-A 备份目录被 `rm -rf`：uninstall 干净拒绝，无 journal、配置一字未动 | PASS |
| R4-P1-A 账户目录 `chmod 000`：fail closed，不谎报成功；恢复权限后正常完成 | PASS |
| R2-P1-B 全新机器（无 `.shared-runtime`）首次 install 成功且该目录为 `0700` | PASS |
| R3-P2-A / R4-P2-A 暂时不可见账户结转 + 结转行旧备份仍可还原 | PASS |
| 5+2 处 `stat()` 保护（无一处裸 traceback，全部 `{"ok": false, …}`） | PASS（全程未见任何 CRASH） |
| R2-P1-A `prev_*` / `before_*` 分离（连装三次仍能还原到真 pristine 即其证据） | PASS |
| R8/R9 uninstall 侧未追踪扫描的演进 | PASS（见 (a)(b)） |

**第 1–9 轮的修复本轮无一回归。**

---

## (f) 本轮新引入 / 未提及的问题

### ① 把扫描挪到共享位置之后的执行顺序 —— 无 fail-open，但产生了 install 侧的楔子

先说安全性方向：扫描**只会抛异常、不会放行**，因此把它提前到
`latest_matches_this_receipt` 计算之前**不可能**把一次拒绝变成通过。
`kind == "install"` 分支自己的 `latest_matches_this_receipt` 判定与 drift 检查
逻辑未受影响，我在 (a) 的"改回原名后 recover"一步里验证了 committed 与
rolled_back 两条路径都仍然正确收尾。**顺带还修好了一件事**：R9-P3-A 描述的
`atomic_write()` 的 `mkdir(parents=True)` 在回滚中重建已被挪走的目录、
反过来堵死"把账户挪回去"这条补救——在 install 方向上现在够不着了，因为扫描
在回滚循环之前就拒绝，一次 `atomic_write` 都不会发生。

**但**：这个落点在 install 方向上是"提交之后"。第二次 install 的完整链路
（无崩溃、无竞态、无特权）：

```
first_install                       rc=0
relocate pool-bravo -> codex-accounts/retired/2026/pool-bravo   （普通发现看不到，扫描看得到）
second_install (基线 cb1c532459)    rc=0  ok:true  ← 谎报成功并抛弃了活 handler
second_install (候选 6fb376c671)    rc=1  {"ok": false, "error": "install failed and the durable
                                            recovery journal remains pending"}
disk                                pending=True  latest=True  四份配置全部已桥接
then::recover                       rc=1  （这时才第一次看到真正的原因）
then::verify                        rc=1  "pending install journal must be recovered first"
then::uninstall                     rc=1
--- 把账户挪回去 ---
after_restore::recover              rc=0  state=committed
after_restore::uninstall            rc=0  全部回到 0
```

拒绝本身是**对的**（比基线安全），可恢复、不永久锁死；问题只在于**落点**和
**错误信息**。这一条本身我定级 **P2**；它与 R10-P1-A 是同一个结构缺陷的两种触发源，
所以我把 P1 记在 R10-P1-A 名下、这里不重复计分。

### ② `_read_for_detection()` 的 TOCTOU —— 无可利用窗口 ✅，但有两处弱化

我在两个解释器上逐条探测了这个新函数，结果完全一致：

| 输入 | 3.9.6 | 3.14.6 |
|---|---|---|
| 普通可读文件 | 返回内容 | 返回内容 |
| **路径不存在（ENOENT）** | **InstallError** | **InstallError** |
| 目录 / FIFO / 符号链接 / 悬空链接 | `None` | `None` |
| `chmod 0000`（EACCES） | InstallError | InstallError |
| 父目录 `chmod 0000` | InstallError | InstallError |
| 超过 `MAX_MANAGED_FILE_BYTES` | InstallError | InstallError |
| 零字节文件 | `b""` | `b""` |

**TOCTOU 结论：没有可利用窗口。** `_is_regular_file()`（跟随符号链接的 `stat`）→
`resolve_ssd_path()`（完全解析）→ `_read_for_detection()` 三次调用之间，即使
文件被换成符号链接：`lstat` 的 `S_ISLNK` 会返回 `None` 跳过；若在 `lstat` 与
`open` 之间被换，`O_NOFOLLOW` 会给出 `ELOOP` → InstallError；若被 rename 换成
另一个真实文件，`os.fstat` 的 `(st_dev, st_ino)` 比对会抓到。**最坏结果只是一次
虚假的 InstallError，读不到 SSD 之外的任何东西。**

不过和 `validate_owned_file()` 相比，新函数**丢掉了两道护栏**（下文 P3-B、P3-C）。

### ③ 新的"搜索盲区" —— 我找到 1 个新的 fail-open 残留 + 3 个新的假阳性形态

先如实说明：任务鼓励我找报告里没点出的**新盲区**。我按"活着的 owned handler 能
以哪些形态存在而扫描看不见"逐一枚举并实测，结论如下。

**真正的 fail-open（会静默漏掉）**——只找到一个报告未提及的，而且它就在本轮修好的
那一行**正上方**：

- **P3-D（新，非阻断）**：`install_bridge.py:470-473`
  ```python
  try:
      resolved = resolve_ssd_path(candidate)
  except InstallError:
      continue          # ← 与本轮刚修掉的 except InstallError: continue 完全同款
  ```
  本轮的正确论断是"`validate_owned_file()` 问的是能不能安全重写、不是这里面有没有
  活 handler"。**同一句批评一字不改地适用于 `resolve_ssd_path()`**：它问的是
  "这条路径是不是在 SSD 上、是不是同一个设备"，与"里面是不是活着一个 handler"
  同样无关，而失败被静默吞掉。可达触发：①指向 SSD 之外的符号链接（= 已披露的
  R9-P3-B）；②**`resolved.stat().st_dev != root.stat().st_dev`——即 local-homes 下
  挂载了另一个卷/磁盘映像**（报告未提及；我没有 sudo 无法真实挂载来实证，
  仅从代码路径确认，故只作 P3 记录）。修法与本轮一致：这一步也该只问"能不能确定
  它在 SSD 上"，不能确定就升级而不是 `continue`。

  同类但更边缘的还有 `:447-450`：`resolve_ssd_path(LOCAL_HOMES_ROOT)` 失败时
  整个扫描 `return []`（"没有未追踪 handler"），随后调用方照常删 receipt。
  实际很难单独触发（SSD 不在时别处也会先失败），一并记为 P3。

**假阳性（fail-closed，不会漏，但会给出无法照做的补救建议）**——三种，报告均未提及，
我全部实测复现：

| 形态 | 实测结果 |
|---|---|
| **符号链接祖先**：`mv pool-bravo pool-bravo.real && ln -s pool-bravo.real pool-bravo`。配置仍然可以在 receipt 记录的原路径读到，`verify` rc=0 一切正常 | `uninstall` rc=1 "restore it to its receipt-recorded path" —— 可它**已经在**那条路径上；`install` rc=1 R6-P1-A。两条路都拒绝 |
| **仅大小写改名**（APFS 默认大小写不敏感，本机 `/Volumes/Extreme SSD` 即 APFS）：`mv pool-bravo POOL-BRAVO`。`verify` rc=0 | `uninstall` rc=1 同一句无法照做的建议；`install` rc=1。两条路都拒绝 |
| **硬链接镜像**（`cp -al` / `rsync --link-dest` 类备份在 local-homes 下留下同 inode 的 `hooks.json`） | `uninstall` rc=1；**`install` rc=1 且留下 pending journal**（= R10-P1-A 的楔子） |

根因都一样：扫描用**解析后的路径字符串**与 receipt 里的**原始字符串**做集合比较，
而 `_receipt_rows()` 用的是解析后的 `Path` 对象——两者对"同一个文件"的判定标准
不一致。这三种都是 fail-closed（不会抛弃活 handler），且前两种自第 7 轮起就存在、
不是本轮引入，故定 P3；第三种因为会触发 install 楔子，已并入 R10-P1-A。

**我逐一排除掉的（确认不是盲区）**：`os.walk(followlinks=False)` 不进入符号链接
目录——但只要真实内容还在 local-homes 内的某个真实路径上，遍历就会在那里看到它，
所以这种形态最终塌缩成"内容在 local-homes 之外"（R9-P2-A），不是独立盲区，
我用 `behind-symlink` 实测确认了这一点；文件名假设（只看 `hooks.json`）与本工具
唯一会写的路径形状一致；`atomic_write` 崩溃残留的 `.hooks.json.XXXX` 临时文件
不叫 `hooks.json`，Codex 也不会读，跳过是对的；硬链接/账户挪进 RUNTIME_BASE
分别对应上表与已披露的 R9-P3-B（我实测 `runtime-hidden`：`uninstall` rc=0 谎报
成功、抛弃活 handler，之后 `install` rc=0 —— R9-P3-B 危害与 R9-P2-A 同级）。

### 关于报告里的 "item 3"（结构性方向）

本轮确实没有实现它，所以本轮仍然是"头痛医头"——这一点候选自己也承认了。
我的独立判断是：**"item 3" 能一次性解决的是上面这一整类 fail-open（R9-P2-A、
R9-P3-B、P3-D、以及任何未来的新盲区），但解决不了本轮新引入的这一整类 false
positive / 楔子**（符号链接祖先、大小写改名、硬链接镜像、无关不可读文件）——
因为后者的根因不是"搜索找不到证据"，而是"用错了判定标准 + 落点在提交之后"。
换句话说，即使下一轮实现了 item 3，R10-P1-A 也不会自动消失，仍需单独修。

---

## 其余非阻断发现

- **P3-A（新）**：`_read_for_detection()` 是这个文件里**唯一**把 ENOENT 当硬错误的
  地方。`_path_is_absent()`（`:774-780`）、`_is_regular_file()`（`:292-298`）、
  以及扫描自己的 `os.walk` `onerror`（`:456-459`）三处都明确把 ENOENT 当"不存在，
  正常"。而新函数的注释却写着 "matching this file's fail-closed discipline
  everywhere else stat-level ambiguity comes up (`_path_is_absent()`,
  `_is_regular_file()`)" —— 与那两处的实际行为相反。后果：local-homes 下任何一个
  `hooks.json` 在遍历的 `stat` 与读取的 `lstat` 之间被删除/替换（例如插件市场
  git 树刷新），整个 uninstall/recover 就变成硬失败。我用一个持续增删无关
  `hooks.json` 的并发进程做了 8 次尝试**未能命中**（窗口只有微秒级），故定 P3；
  但值得注意的是我实测真实 local-homes 上这次扫描要跑 **2.9–4.5 秒**
  （三次：4.457s / 3.881s / 2.907s；`os.walk` 单独也要 0.9–3.9 秒，外置 USB SSD
  冷热缓存差异很大），而 `uninstall` 会跑**两次**扫描（`:1445` 与 `:1509`），
  `install` 一次。这与第 9 轮报告写的 "0.24 秒" 差一个数量级——那多半是完全
  热缓存下的测量。窗口是秒级而不是毫秒级。

- **P3-B（新）**：`_read_for_detection()` 丢掉了 `validate_owned_file()` 读完之后的
  `after = os.fstat(descriptor)` 复核（`:186-189` 的
  `before_identity != after_identity or len(raw) != after.st_size` →
  `"file changed while reading"`）。因此一份**正在被并发改写**的配置可能被读成
  半截内容 → `_contains_owned_handler()` 解析失败 → 判为"这里没有 handler" ——
  与 R9-P1-B 同类的 fail-open，只是换了一扇门。加一行 `os.fstat` 复核即可。

- **P3-C（新）**：大小上限用**开 fd 之前**的 `before.st_size` 判定，之后按
  `opened.st_size` 读满整个文件，两次之间没有复检。文件在这个窗口里增长的话，
  4 MiB 上限不生效。`validate_owned_file()` 有同样的结构，所以这是继承而非新增，
  但新函数少了 P3-B 那道事后复核，因此比原函数更宽。

- **P3-E（观察，非缺陷）**：`_read_for_detection()` 里
  `if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)` 的后半句
  永远不可能为真（符号链接必然不是 S_ISREG，前半句已经返回）。从
  `validate_owned_file()` 抄来的冗余。函数注释里"a symlink — O_NOFOLLOW refuses it
  at open time"也不准确：实际是 `lstat` 那一步就返回 `None` 了，根本走不到 open。
  无行为影响。

---

## 与另一路复核的分歧（事后补记）

另一路（同名文件、结论 **GO**）把同一个现象记为 **R10-P2-A**，理由是它
"fail-closed、从不抛弃活 handler、下一条命令就会说出是哪个路径、一次
`chmod`/`mv`/`rm` 就能清掉"，因此不到阻断线。这三点我实测下来都属实，
分歧只在定级，不在事实。我仍判 P1，理由是那份报告没有单独称量的第 1 点：
**`install` 在磁盘上已经完整成功（四份配置全部桥接、`latest-receipt.json` 已写）
的前提下对外报 `{"ok": false, "error": "install failed …"}`**，而这句话既不说
原因也不说是哪个文件——操作员据此得出"没装上"的结论，桥接却在每次 Codex prompt
上真实执行。"报告状态与磁盘状态背离"在这个文件的前九轮里每一次都被判 P1
（P1-1、P2-3、R4-P1-A、R6-P1-A、R7-P1-A、R8-P1-A、R9-P1-A/B）。请协调方就这一点
自行裁定；若采用"可用性类一律 P2"的口径，则本轮为 GO。

---

## 复现材料

全部脚本与一次性目录在
`/Volumes/Extreme SSD/Orca/tmp/claude-code-runtime/claude-501/-Volumes-Extreme-SSD-Orca-workspaces-orca---orca/eb76c191-75c0-4435-a1dd-70c57d2e5c7d/scratchpad/r10/`：

| 文件 | 用途 |
|---|---|
| `fixture.py` | 独立 fake-SSD 夹具（3 个池账户、2 个既有 handler、按目标表达的 kill 点） |
| `cli.py` | 把某个 revision 的 `install_bridge.py` 绑到 fake SSD 上、按 CLI 形状单进程执行一个动作 |
| `kill_install.py` | 首次安装途中的真 SIGKILL（kill 在写 `latest-receipt.json` 那一刻） |
| `scenario_a.py` | (a) R9-P1-A，四种迁移形态 |
| `scenario_b.py` | (b) R9-P1-B，改名账户异常权限/大小 + 无关文件假阳性 |
| `scenario_c.py` | R10-P1-A：无关不可读文件导致的 install 楔子 |
| `scenario_d.py` | (f)① 第二次 install 的提交期扫描顺序 |
| `scenario_e.py` | (e) 第 1–9 轮修复的 17 项端到端抽查 |
| `scenario_f.py` | (f)③ 盲区/假阳性猎取（符号链接祖先、大小写改名、硬链接、RUNTIME_BASE 隐藏） |
| `probe_read.py` | `_read_for_detection()` 逐条单元探测（双解释器） |
| `race_enoent.py` | P3-A 的并发可达性尝试（8 次未命中） |
| `rev-<sha>/`, `hybrid-r8/`, `hybrid-r9/` | 各 revision 的 `git archive` 提取 + 跨版本混合运行目录 |

---

## 一句话总结

第 9 轮的两个阻断项确已修复、82/82 双解释器全绿、九轮既有修复零回归、
候选自带新测试全部非空转——本轮在"不再抛弃活着的 handler"这条主轴上是真实进步。
**但共享扫描被放在了 install 已经提交完毕之后的位置，加上 `_read_for_detection()`
把"读不出来"一律升级成硬失败，使得 local-homes 下任意一个与本安装器无关的不可读
或超大 `hooks.json` 就能把一次实际成功的安装变成一句谎报的 "install failed" 外加
一个卡住的事务日志——而这正是本轮设计注释明文声称已经避开的拒绝服务。
可确定性复现、基线上完全无害。判 NO-GO。**
