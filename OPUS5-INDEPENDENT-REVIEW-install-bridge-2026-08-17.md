# OPUS5 独立复核（第一轮）— claude-codex-memory-bridge / install_bridge.py

- 复核者：Claude opus5 / max effort，只读独立复核，与并行派出的另一路复核互不知情
- 日期：2026-08-17
- 候选：`29bb896012e05d4d6e3c21f461fc42163c73b747`
- 范围：`claude-codex-memory-bridge/install_bridge.py`（752 行，全文通读）
  + `claude-codex-memory-bridge/tests/test_install_bridge.py`（383 行）
  候选 diffstat：2 文件，+262 / −9
- 目标完整性：复核期间仓库 HEAD 由 `29bb896012` 前进到 `6305d9d9e4`。
  已核对 `git diff 29bb896012 -- claude-codex-memory-bridge/` **为空**，
  `git status --porcelain -- claude-codex-memory-bridge/` 全程为空
  → 复核对象目录逐字节未变，本报告结论对候选成立。
- 本次复核全程只读：未修改、未提交、未合并、未安装、未激活候选。
  **未对本机 `/Volumes/Extreme SSD` 上的真实 Codex `hooks.json` 执行任何写操作**；
  所有复现均在隔离临时目录中的假 "Extreme SSD" 上进行（见「复核方法」）。
  对真实 `hooks.json` 只做了一次**只读结构探测**（键名/计数/标志位，不打印命令内容），结果见第 §7 节。

---

## 结论：**NO-GO**

**存在 2 项可复现的 P1，均已端到端复现，均不需要攻击者、不需要竞态、不需要特殊权限。**
两项同源：`install()` 把**当前磁盘上的内容**当作"卸载还原基线"，而没有区分"这份内容里已经有我自己装的 handler"。

| 编号 | 级别 | 摘要 | 是否本轮新引入 |
|---|---|---|---|
| **P1-1** | **P1（阻断）** | **连续两次 `install` 会永久毒化回滚基线**：第二次 install 把"已安装态"备份成 before。随后 `uninstall` 返回 `{"ok": true, "restored": [...]}`，但 bridge handler **原封不动留在每一份 hooks.json 里继续执行**；之后 `verify()` 依然返回 `ok: true`。整条工具链没有任何一处报告异常 | 否，既有；但本轮新测试 `test_install_is_idempotent_replan_shows_no_change` **正式认可了触发该问题的操作序列**，却没有检查其卸载后果 |
| **P1-2** | **P1（阻断）** | **`uninstall()` 完全没有事务日志**（install 有）。中途被 SIGKILL → 多账户永久停在"一半卸了一半没卸"，`recover` 看不见（返回 `state: none`），`verify`/`uninstall` 双双报错，唯一修复路径 `install` 又会触发 P1-1，最终 bridge 在未还原的账户里**永久保持激活** | 否，既有 |

另报出 **4 项 P2 + 7 项 P3**，不阻断本轮，但需进入待办（详见 §5、§6）。

**同时明确：本轮 3 项修复本身的质量是好的。** ①、② 我都独立复现了"修好了"，且没找到修复引入的新假阳性；
install 侧的事务/回滚机制在**真实 SIGKILL** 下 4 个中断点全部正确（逐字节 + 原始权限位精确还原）；56/56 测试真实全绿。
NO-GO 来自 uninstall 侧——它从来没被事务化，本轮也没有触及。

---

## 复核方法

不复用仓库自带的测试脚手架，另写了一套独立 harness（全部在会话 scratchpad，未落入仓库）：

```
fixture.py                 独立搭建假 "Extreme SSD"：patch SSD_ROOT / LOCAL_HOMES_ROOT /
                           RUNTIME_BASE / PENDING_PATH / SOURCE_SCRIPT / Path.home() /
                           volume_uuid（避免真的调用 /usr/sbin/diskutil）
crash_child.py             子进程驱动器，可在事务的任意点对自己发 SIGKILL（真杀，不是抛异常）
exp1_reinstall_uninstall.py  install→install→uninstall（同 release / 升级 release 两种）
exp2_aftermath.py            P1-1 之后 verify/uninstall/recover/plan 各说什么、真原始备份还在不在
exp3_crash.py                真实 SIGKILL × 4 个中断点 → 真实 recover
exp4_uninstall_crash.py      uninstall 中途 SIGKILL → 多账户中间态 → 能否修回干净
exp5_owned_handler.py        20 条 owned_handler 向量（12 拒 / 5 收 / 3 判断题）+ 破坏性后果检查
exp6_traceback.py            main() 级：还有没有裸 traceback（对照正常路径基线）
exp7_more.py                 atomic_write 的裸 OSError / install 进程内回滚 / _mode_bits 假阳性
exp8_concurrent.py           无锁并发两个 installer
```

关键点：**跑的是真实、未被 mock 的顶层函数**（`plan`/`install`/`verify`/`uninstall`/
`recover_pending_install`/`main`），对真实文件做真实权限检查；崩溃场景用**真 SIGKILL**
（`rc == -9`），而不是在 Python 里抛异常——后者会走 `except BaseException` 的进程内回滚，
测不到"进程直接没了、只剩磁盘上的日志"这个真正需要 `recover` 的情形。

---

## §1 P1-1（阻断）— 二次 install 永久毒化回滚基线，uninstall 静默失效

### 位置
- `install_bridge.py:539-543` — 无条件把**当前磁盘内容**读成 `originals[path]`
- `install_bridge.py:550-554` — 把 `originals[path]` 逐字节写成备份
- `install_bridge.py:566-576` — receipt 的 `before_sha256` / `before_mode` 取自 `originals`
- `install_bridge.py:664-697` — `uninstall()` 忠实地把该备份还原回去
- `install_bridge.py:532-534` — `install()` 开头只调 `recover_pending_install()`，**不检查是否已存在 `latest-receipt.json`**，因此不知道"我已经装过一次了"

### 机制
`update_hook_config()` 的语义是"先摘掉 owned handler，再追加一个新的"，所以它**幂等**——这本身没错。
问题在于 `install()` 取 before 基线的时刻：第二次 install 时，磁盘上的 hooks.json **已经含有第一次装进去的 handler**，
于是这份"已安装态"被当作"安装前的原始状态"写进备份和 receipt。
`uninstall()` 只做一件事：把 receipt 指向的备份还原回去。还原的正是"已安装态"。

### 复现（exp1，隔离环境，无崩溃、无竞态）

```
[same-release] install#1 -> release 468817b4903a
    hooks.json: handlers=2 owned=1 mode=0o600      (两份 config 都装上了)
[same-release] install#2 -> release 468817b4903a   (同一个 release，纯幂等重装)
    receipt#2 backup for hooks.json: owned handlers inside backup = 1   <-- 备份里已经有我们的 handler
[same-release] uninstall -> ok=True restored=['hooks.json', 'hooks.json']
    AFTER UNINSTALL hooks.json: byte-identical-to-pristine=False owned_handlers_left=1 mode=0o600 (pristine mode 0o644)
[same-release] RESULT: *** BUG: uninstall did NOT restore the original ***
```

升级路径（改了 `claude_memory_hook.py` → 新 release_id → 新命令行）结果相同，且更糟：
卸载后残留的是**指向 release-1 的旧 handler**，而 `uninstall()` 返回 `runtime_retained: True`
（release 目录被保留），所以那条旧命令行**仍然可执行、仍然在每次 Codex prompt 时运行**。

### 为什么是 P1 而不是 P2

1. **静默**：`uninstall` 返回 `ok: true` 且列出了 restored 路径，退出码 0。
2. **事后自查全部说"一切正常"**（exp2，同一环境接着跑）：

```
state after install;install;uninstall:  owned handlers still live in main config: 1
    verify():          OK -> {'ok': True, 'release_id': 'f6944a68...', ...}   <-- 卸载后 verify 说"装好了、校验通过"
    uninstall() again: OK -> {'ok': True, 'restored': [...]}                  <-- 再卸一次，依然 ok，依然没卸掉
    recover():         OK -> {'ok': True, 'state': 'none'}
    plan():            OK -> will_change=[False, False]                       <-- "没什么要改的"
```
   工具**没有任何一个出口**会告诉操作者"卸载其实没生效"。

3. **触发序列是被设计支持、且被本轮新测试正式认可的**：release_id 由脚本 sha 推导，重装就是既定的升级路径；
   `test_install_is_idempotent_replan_shows_no_change` 明确断言"在已安装态上再装一次不得重复插入 handler"。
   也就是说，**测试认可了这个序列，但没有检查它对卸载的破坏**。
4. **真原始备份还在，但工具够不着**：`backups/<install_id_1>/` 里那份真原始内容仍在磁盘上
   （exp2 已确认字节匹配），但 `latest-receipt.json` 指向的是第二代备份，任何工具动作都不会回到第一代。只能人工修。
5. **安全含义**：这个 hook 的作用是把 Claude 私有记忆内容喂给 Codex。"卸载成功"却仍在每个 Codex 账户的
   每次 prompt 上执行，是一次真实的隔离/收敛失败，而不只是清理不干净。

### 附带
`mode` 也一并丢失：原始 0o644 → 卸载后停在 0o600（见上面复现输出）。

---

## §2 P1-2（阻断）— `uninstall()` 没有事务日志，中断即永久半卸载

### 位置
`install_bridge.py:682-697`（uninstall 的写循环）对比 `install_bridge.py:580-609`（install 的 pending journal + 提交点）。

`install()` 写 `PENDING_PATH` 事务日志、逐字节备份、fsync、并以 `latest-receipt.json` 作为提交点；
`uninstall()` **一条日志都不写**，只有一个进程内 `except BaseException` 回滚——进程被 SIGKILL 时该回滚根本不会执行。

### 复现（exp4，真 SIGKILL）

```
installed    : main{owned:1,0o600}  acct{owned:1,0o600}  pending:False latest:True
uninstall child rc=-9 (SIGKILLed)
post-kill    : main{owned:0,0o644}  acct{owned:1,0o600}   <-- 一个账户卸了，另一个还挂着

  recover  : ok  {'state': 'none'}                        <-- recover 看不见这个中间态
  verify   : InstallError -> file is not private: .../.codex/hooks.json
  uninstall: InstallError -> file is not private: .../.codex/hooks.json
  plan     : ok  will_change=[True, False]
final        : main{owned:0}  acct{owned:1}               <-- 卡死，工具无法前进也无法后退
```

唯一能推进的动作是重新 `install`，而那会立刻触发 P1-1：

```
--- can the operator get back to a clean, fully-uninstalled state? ---
  repair install: ok      -> main{owned:1} acct{owned:1}
  uninstall     : ok      -> main{owned:0} acct{owned:1}
  RESULT: *** BRIDGE STILL ACTIVE AFTER SUCCESSFUL UNINSTALL ***
```

即：**一次被打断的 uninstall 之后，这台机器上的 bridge 再也无法通过工具彻底卸载**，
而工具每一步都报 `ok: true`。多账户场景（本机真实有 2 个隔离账户 + 1 个主 home，见 §7）正是最容易命中的形态。

### 为什么是 P1
这个文件的核心承诺就是"事务性安装器：中途失败可回滚、可恢复"。install 侧确实做到了（§4 已验证），
uninstall 侧则完全没有该保护，且失败后果是**bridge 持续激活**而非"清理不干净"。
即使单独修好 P1-1，这条路径仍会留下一个 `recover` 无法识别的中间态。

---

## §3 修复①（stat 保护）独立验证：**正确，但只堵住了 stat 这一类**

### ①-a 已修部分：正确、完整、无假阳性

逐点核对 5 处替换 + `resolve_ssd_path` 设备号比较：

| 行号 | 调用点 | 状态 |
|---|---|---|
| 122 | `resolve_ssd_path` 设备号比较 | 已包在自己的 `try/except OSError` 里（121-124） |
| 456 | `_receipt_rows` | → `_mode_bits` |
| 525 | `recover_pending_install` 回滚校验 | → `_mode_bits` |
| 542 | `install` 记录 `original_modes` | → `_mode_bits` |
| 589 | `install` 写前重查 | → `_mode_bits` |
| 712 | `plan` 的 `will_change` | → `_mode_bits` |

`grep` 全文确认：文件中已不存在任何**未被 `try/except OSError` 包住**的 `.stat()` / `.lstat()` / `os.fstat()`
（145/185/220 三处 `lstat` 各自在自己的 try 里，155/167 的 `fstat` 在 `validate_owned_file` 的 try 里）。

假阳性检查（exp7c）：`_mode_bits` 对 0o600 / 0o644 / 0o400 / 0o700 / 0o755 / 0o666 与目录全部返回正确值，
文件缺失时干净抛 `InstallError`。**没有把本该成功的正常路径误判成失败**——
这一点也被 56/56 测试（`plan`/`install` 每次都会走 542/589/712）间接佐证。

### ①-b 未覆盖部分 → **P2-1**：`atomic_write()` 仍会漏出裸 OSError

①的目标表述是"让所有失败都产出干净的 `{"ok": false, "error": ...}`，而不是 traceback，因为 `main()` 只捕获 `InstallError`"。
`atomic_write()`（297-323）内部的 `path.parent.mkdir()`、`tempfile.mkstemp()`、`os.open()` 全部**没有包装**，
仍会把裸 `OSError` 抛穿 `main()`。exp6/exp7 实测复现（对照组：正常 plan/install/verify/uninstall 全部干净输出）：

```
=== B. unwritable directory mid-transaction (bare OSError from atomic_write) ===
  recover rollback into unwritable dir  -> *** RAW TRACEBACK ***  PermissionError: [Errno 13] ... /.codex/.hooks.json.7ixboz87
=== (a) re-install while RUNTIME_BASE is read-only ===
  install -> *** RAW TRACEBACK ***  PermissionError: [Errno 13] ... /.pending-install.json.gpsmtszw
```

两条都真实可达：
- `install_bridge.py:580` 的 `atomic_write(PENDING_PATH, ...)` **在 `install()` 的 try 块之外**（try 从 585 才开始），失败即 traceback；
- `install_bridge.py:523` 的回滚写入是**最危险的一条**：一个无法回滚的 pending 事务会以 traceback 收场，
  日志和半改过的 config 都留在原地，操作者拿不到任何结构化错误。

不构成 P1（`atomic_write` 本身是原子的，没有观测到状态损坏；且 549-554 的备份写入失败发生在任何 config 被改动之前），
但它与①声称建立的契约直接冲突，属于同一类问题的遗漏。

---

## §4 install 侧事务机制独立验证：**4 个中断点全部正确**（本轮未改动，但这是 GO/NO-GO 的关键前提，故实测）

exp3，真 SIGKILL（`rc == -9`），每个场景独立环境，杀完由**新进程**跑真实 `recover`：

| 中断点 | 杀掉时磁盘状态 | recover 判定 | 结果 |
|---|---|---|---|
| 写完 journal、还没碰任何 config | 2 份 config 均原样，pending=True | `rolled_back` | **PASS** 逐字节 + 权限位（0o644）精确还原，journal 清除 |
| 写完 config#1、还没写 config#2 | main 已装(0o600)，acct 原样(0o644) | `rolled_back` | **PASS** main 精确还原 |
| 两份 config 都写完、还没写 latest-receipt | 两份都已装 | `rolled_back` | **PASS** 两份都精确还原 |
| 写完 latest-receipt、还没删 journal | 两份都已装，latest=True | `committed` | **PASS** 保持安装态，journal 清除 |

四种情形恢复后 `plan()` 均可正常运行（前三种 `will_change=[True,True]`，第四种 `[False,False]`），系统可继续使用。
提交点选择（`latest-receipt.json` 的原子写入）与 `_receipt_rows()` 的 before/after/both/drift 四态判定都是正确的。

另外 exp7b 验证了 `install()` 的**进程内**回滚路径（第二份 config 所在目录只读 → 写第二份时失败）：

```
  install -> clean JSON: ok=False install failed; prior hook configs were restored
  CHECK full rollback to pristine, no pending journal left: PASS
```
这条路径**测试套件完全没有覆盖**，但实测正确。

**结论：install 侧的事务性名副其实。NO-GO 完全来自 uninstall 侧的不对称。**

---

## §5 修复②（owned_handler 精确匹配）独立验证：**实质性改进，20 条向量已跑**

exp5 自行构造了 20 条报告里没给过的向量。**12 条应拒绝的全部正确拒绝，5 条合法写法全部正确识别，0 硬失败：**

正确拒绝（`owned=False`）：
```
--tag <ID>                                      id 作为别的 flag 的值
--bridge-id-legacy <ID>                         相似但不同的 flag 名
--bridge-id <ID>-v2 / --bridge-id x<ID>         id 是值的前缀 / 后缀
echo '--bridge-id <ID>' / echo "--bridge-id <ID>"   整体被引号包成单个 argv token
--bridge-id                                     flag 在、值被截断
<ID> --bridge-id other                          id 在，但 flag 指向别处
/bin/sh -c 'notify --bridge-id <ID>'            id 在 sh -c 的字符串里
--json '{"bridge-id":"<ID>"}'                   id 在 JSON 参数里
--bridge-id <ID大写>                            大小写不同
--bridge-id 'unclosed                           shlex 解析失败（ValueError 被吃掉）
```
正确识别（`owned=True`）：真实生成形态、值单引号、值双引号、flag 加引号、多余空白。

**并且确认真实 `make_release()` 命令行能被识别**——注意真实路径含空格（`/Volumes/Extreme SSD/...`），
`shlex.quote` 会加引号，`shlex.split` 再还原，`--bridge-id` 与 `<ID>` 仍是相邻 token：

```
/usr/bin/python3 '/Volumes/Extreme SSD/.../claude_memory_hook.py' --bridge-id orca-claude-native-memory-v1 \
  --policy '/Volumes/Extreme SSD/x/policy.json' --expected-policy-sha256 000... --expected-script-sha256 111...
  owned_handler -> True
```

破坏性后果检查：对全部 20 条向量跑 `update_hook_config`，**除应被替换的自有 handler 外，没有任何外来 hook 被删除**。

### 但②的"注释"说法不成立 → **P3-1**

`shlex.split()` 默认 `comments=False`，`#` 只是一个普通 token。因此：

```
/usr/bin/true # --bridge-id orca-claude-native-memory-v1        -> owned=True  -> 被 update_hook_config 删除
/usr/bin/true; echo done # --bridge-id orca-claude-native-memory-v1 -> owned=True -> 被删除
```

这条命令交给 shell 执行时，`#` 之后**确实是注释、确实不会执行**，所以它并不是我们的 handler，删掉它是错的——
正是②要消灭的那一类假阳性。而 commit message 与测试注释都明确把"在注释里"列为已修复的例子；
现有测试 `test_owned_handler_rejects_bridge_id_as_a_mere_substring` 用的是
`echo # mentions <ID> in a comment only`——它通过只是因为 `#` 与 `<ID>` 之间隔着 `mentions`，
**并没有真正覆盖注释场景**。修法：`shlex.split(command, comments=True)`。

影响面窄（需要 hooks.json 里有一条带 shell 注释、且注释里恰好写着 `--bridge-id <精确ID>` 的命令），故列 P3；
但"注释已修复"这一表述目前**没有证据支撑**，建议同时更正 commit message 引用与测试注释。

---

## §6 新增 `InstallEndToEndTests` 的独立评估（(d)）

我逐条读了 8 个新测试并核对其断言。**没有发现空转测试**——它们确实构造隔离环境、
调用真实未 mock 的顶层函数、对真实文件做真实权限检查，断言也都指向声称的行为：

| 测试 | 是否真的测到 | 评价 |
|---|---|---|
| `test_plan_reports_pending_changes_before_install` | 是 | 真调 `plan()`，断言 2 份 config 且都 `will_change` |
| `test_install_verify_round_trip` | 是 | 最强的一个：断言 handler 落位、原有 handler 保留、mode=0o600，再真跑 `verify()` |
| `test_install_is_idempotent_replan_shows_no_change` | 是，但**认可了 P1-1 的触发序列** | 只查"没重复插入"，没查这次 install 对卸载基线做了什么。见 §1 |
| `test_verify_detects_installed_script_tampering` | 是 | 真篡改已安装脚本，断言 verify 抛错 |
| `test_verify_detects_hook_config_drift` | 是 | 真外部改 config，断言 verify 抛错 |
| `test_uninstall_restores_original_configs_exactly` | 是 | 逐字节 + mode 比对。**但只覆盖"装一次"**，正是 P1-1 的盲区 |
| `test_uninstall_refuses_when_config_changed_since_install` | 是 | 并额外断言拒绝后没有污染漂移态，很好 |
| `test_recover_pending_install_is_a_noop_when_nothing_pending` | 是，但最弱 | 只断言 `state == "none"`。**全套没有任何测试用真实中断产生过 pending journal**——另两个 recover 测试的 journal 是手工拼的 |

### "零测试覆盖"问题：**大幅缓解，但远未补齐**

仍然完全无覆盖的路径（`grep` 确认测试文件中根本没出现这些符号）：

- **`main()` 本身**：argv 分发、退出码、以及①专门要保障的"干净 JSON 而非 traceback"契约——**一个断言都没有**。
  （我在 exp6/exp7 里补跑了，并因此发现 P2-1。）
- **`resolve_ssd_path()` 的越界/跨设备拒绝**：这是整个文件最核心的安全控制（"一切必须落在 Extreme SSD 上"），
  **没有任何测试断言 SSD 之外的路径会被拒绝**。
- **`validate_owned_file()` 的各类拒绝**：符号链接、组/他人可写、非本人 uid、超 4MiB、读取期间被改。
- **`discover_hook_configs()`**：`< 2` 份配置报错、账户目录是符号链接、非目录条目、去重。
- **`make_release()` / `write_runtime()`** 的 immutable collision 分支。
- **`read_receipt()` 的"必须先 recover pending"守卫。**
- **`install()` / `uninstall()` 的进程内回滚分支**（前者我实测正确，后者未测）。
- **`install`→`install`→`uninstall`**（= P1-1）、**uninstall 中途崩溃**（= P1-2）、**并发两个 installer**（= P2-3）。

---

## §7 其余 P2 / P3

| 编号 | 级别 | 位置 | 摘要 |
|---|---|---|---|
| **P2-1** | P2 | `atomic_write` 297-323；调用点 523 / 553 / 579 / 580 | ①未覆盖：裸 `OSError` 抛穿 `main()`，已复现两条 traceback。详见 §3-b |
| **P2-2** | P2 | `verify` 633、`uninstall` 670/674、`read_receipt` 613-626 | receipt 被损坏（但仍属本人、0o600，能通过所有文件完整性检查）时抛裸 `KeyError`/`TypeError`。实测：`verify` 缺 `release_dir` → `KeyError`；`release_dir` 是 int → `TypeError`；`uninstall` 行缺 `path` → `KeyError`；`backup` 为 null → `TypeError`。对比 `_receipt_rows()`（425-477）对每个字段都做了严格校验——**recover 严、verify/uninstall 松**，不对称。注：`uninstall` 的校验循环在任何写入之前完成，故不会留下半改状态 |
| **P2-3** | P2 | 全文无任何锁 | 无互斥。并发两个 installer 时，B 的 `install()` 开头的 `recover_pending_install()` 会吃掉 A 正在进行的 journal。已复现**报告反转**：`install` 退出码 1、输出 `{"ok": false, "error": "install failed; prior hook configs were restored"}`，而磁盘上**两份 config 都已装好、latest-receipt 已提交**。照此 JSON 判断的操作者/自动化会认为什么都没装，从而不会去卸载，bridge 静默存活。多 agent 环境下值得注意 |
| **P2-4** | P2 | `verify` 645、`uninstall` 671 用 `private=True`；`install` 540、`plan` 705 不用 | 对**活的 hooks.json** 的权限要求不一致：install/plan 接受 0o644，verify/uninstall 要求 0o600。后果：一次**正确**的单次 install→uninstall（把 config 还原成 0o644）之后，`verify()` 报的是 `file is not private: .../hooks.json` 而不是"未安装"；`uninstall()` 不幂等，重复调用报同样的误导性错误；`latest-receipt.json` 卸载后**从不清除**——工具压根没有"已卸载"这个状态 |
| **P3-1** | P3 | `owned_handler` 261 | shell 注释向量仍被判为 owned（`shlex.split` 默认 `comments=False`）。commit message 与测试注释关于"注释"的说法无证据支撑。详见 §5 |
| **P3-2** | P3 | `owned_handler` 265 | 不识别 GNU 等号写法 `--bridge-id=<ID>`。若历史上手工写过该形态，install 会追加出第二个 handler，而 `verify()` 的 `len(matches) != 1` 只数"新形态"，恰好等于 1 → 检测不到重复 |
| **P3-3** | P3 | `owned_handler` 254-267 + `update_hook_config` 290 | 判定粒度是整个 handler **组**：只要组内任一 hook 命中，整组（含用户自己的其他 hook）都会被删。`make_handler()` 自己只产单 hook 组，故仅在用户手工把我们的 hook 并入自有组时才会中招 |
| **P3-4** | P3 | `_mode_bits` 137 | 用 `stat()`（跟随符号链接），而 `validate_owned_file` 用 `lstat` + `O_NOFOLLOW`。两次调用之间存在极小 TOCTOU 窗口，可让记录到的 `before_mode` 来自链接目标。**与修复前行为完全一致，非本轮回归**；且所有调用点传入的都是 `resolve_ssd_path` 已解析、且刚被 `validate_owned_file` 验证过的路径 |
| **P3-5** | P3 | `install` 589→592 | 写前重查（588-591）与 `atomic_write`（592）之间仍有微秒级窗口；落在该窗口内的并发编辑会被静默覆盖，且不会进入备份。需同 uid，实际难利用 |
| **P3-6** | P3 | `update_hook_config` 283/291 | `remove=True` 分支在生产代码中**从未被调用**（只有测试用）；`uninstall` 走的是备份还原。死分支易误导后续维护 |
| **P3-7** | P3 | `resolve_ssd_path` 103-111 | `must_exist=False` 从未被传入。该分支会**跳过跨设备检查**，若将来有人使用会静默弱化一层保护 |

### 未发现问题的方向（已查、结论为「无 P0/P1」）

- **路径穿越 / 符号链接**：`resolve_ssd_path()` 严格 resolve + `relative_to` 边界 + 同设备号三重校验；
  `validate_owned_file()` 用 `lstat` 预检 + `O_NOFOLLOW` 打开 + `(st_dev, st_ino)` 打开前后一致性 + 读完再校验
  `(dev, ino, size, mtime_ns)` 与长度——这是一套写得相当扎实的防换链实现，我没能构造出绕过。
  `discover_hook_configs()` 对账户目录做了 `lstat` + `S_ISLNK` 拒绝；账户内的 `hooks.json` 若是符号链接会被 resolve，
  但目标仍须在 SSD 内且能通过 `update_hook_config` 的结构校验，去重由 `dict.fromkeys` 兜住。
- **回滚保真度**：exp3 四个中断点均验证了**逐字节 + 原始权限位**精确还原（0o644 而非 0o600），非仅内容相等。
- **多账户部分成功**：install 侧正确（§4 第二行）；**uninstall 侧即 P1-2**。
- **`recover` 的提交/回滚判定**：四个中断点全部判定正确（§4）。

### 本机真实环境只读探测（供是否执行安装的决策参考，未做任何写操作）

```
.codex     1734B  top_level_keys=['hooks']  events=[SessionStart, UserPromptSubmit]  UPS handlers=1  mode 0o600
0b4cd443   5697B  top_level_keys=['hooks']  events=[PermissionRequest, PostToolUse, PreToolUse, SessionStart,
                                                    Stop, SubagentStart, SubagentStop, UserPromptSubmit]  UPS handlers=2  mode 0o644
b9f32a51   5697B  （同上）                                                                                UPS handlers=2  mode 0o644
所有三份：bridge_id_substring_hits=0  exact_token_hits=0  unshlexable_cmds=0
install() 前置条件 set(payload)=={'hooks'} -> 三份全部 True
```

结论三点：
1. `discover_hook_configs()` 的 `>= 2` 与 `update_hook_config()` 的顶层键前置条件在本机**都满足**，安装不会因结构被拒。
2. 现存命令里**没有任何一条**含 `BRIDGE_ID` 子串 → **②修的是潜在风险，今天这台机器上没有正在发生的破坏**。
3. 两个隔离账户的 hooks.json 各有 8 类事件、5697 字节的真实 handler，且是 0o644。安装会把整份文件按
   `canonical_json`（sort_keys + indent=2）重排并改成 0o600 —— 这本身由逐字节备份兜底，
   **但兜底的前提正是备份可信，而这恰好是 P1-1 打破的东西**。

---

## §8 测试套件真实性验证（(e)）

```
$ cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
...
Ran 56 tests in 0.484s
OK
```
真实全绿，56 = 原 46（含 `claude_memory_hook` 40 项）+ 新 10。测试名与实际断言一致，未发现被跳过或空断言的用例。

---

## §9 建议（不在本轮执行，只读复核）

阻断项必须先修再重审：

1. **P1-1**：`install()` 需要区分"真原始态"与"已安装态"。可选：
   (a) 存在 `latest-receipt.json` 时拒绝重复 install，要求先 uninstall；
   (b) 重装时把前一份 receipt 的 `before_sha256`/`before_mode`/备份**继承**下来作为真原始基线；
   (c) 备份 `update_hook_config(raw, cmd, remove=True)` 的结果而不是磁盘原文。
   (b) 最贴合"可升级"的设计意图。
2. **P1-2**：uninstall 复用 install 的 pending journal 机制（同一套 `_receipt_rows` 四态判定即可覆盖反向操作），
   并让 `recover_pending_install()` 能识别 uninstall 事务。
3. 顺带修 P2-1（`atomic_write` 包 `InstallError`）、P2-2（`read_receipt` 复用 `_receipt_rows` 级别的字段校验）、
   P2-4（统一活配置的权限期望；uninstall 成功后清除或改写 `latest-receipt.json`，让工具具备"已卸载"状态）。
4. P2-3 建议加一个 `RUNTIME_BASE` 下的 flock 单飞锁——多 agent 环境里代价很低、收益明确。
5. 补测试：`main()` 的干净-JSON 契约、`resolve_ssd_path` 越界拒绝、`validate_owned_file` 各拒绝分支、
   install→install→uninstall、uninstall 中断恢复、真实中断产生的 pending journal（而非手工拼）。

**在 P1-1 与 P1-2 修复并重审通过之前，不建议对本机真实 Codex hooks.json 执行 `install`。**
理由不是"装不上"——装是能装上的，且 install 侧崩溃恢复可靠；
理由是**一旦装上，就没有一条可靠的路径能干净地卸下来**，而工具在卸不干净时会报告成功。
