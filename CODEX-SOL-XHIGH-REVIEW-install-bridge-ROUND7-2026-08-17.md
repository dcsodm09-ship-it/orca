# install_bridge.py 第七轮独立只读复核（Codex sol/xhigh）

## 结论

**NO-GO。候选 `8fe75b209e5b90aa4c84cece06590d086c729b86` 有 1 个可确定复现的 P1（R7-P1-A）。**

本轮新增的 owned-handler 预检本身确实挡住了 R6-P1-A 的“把改名后、已安装的内容收养为 pristine baseline”步骤；同 release、跨 release、父目录改名后软链接回接三种变体都在任何持久化写入之前干净拒绝，恢复原路径后的正常 install/uninstall 也能 byte/mode 精确回到真正原始态。但新增错误信息明确建议 `run uninstall first`，而在路径仍保持改名状态时照做，会让 `uninstall()` 返回 `ok:true`、删除 `latest-receipt.json`，同时在新路径留下仍会执行的 owned handler；之后 `verify()` 报 `not installed`，即使再移回旧路径，`install()` 也因没有 receipt、但文件已带 owned handler 而拒绝。该建议把既有的 absent-row 卸载语义变成了修复所推荐的正常操作路径，确定性地产生了与 R6-P1-A 相同的安全后果，因此是候选级 P1 release blocker。

## 启动与候选身份

- 启动收到 `ORCA_CONTEXT_NACK_V1`，原因为 `central reviewed source freshness mismatch: wiki`。本报告不声称加载了 Orca 中央上下文；证据只来自当前可见 Git 对象、隔离 archive 与实时只读命令。
- 仓库：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`。
- 当前工作树 `HEAD` 是 `45a027245cb06dc5c3d630166057ec228ef724fa`，不是候选；工作树原本已有大量无关修改/未跟踪文件，均未触碰。
- 基线：`f8abefc9f0445a69d2e21f23b1db86ff0e7194be`。
- 候选：`8fe75b209e5b90aa4c84cece06590d086c729b86`；基线是候选祖先，中间只有一个与本目标目录无关的 docs commit `d7c73e8b71`。
- 按要求执行 `git diff f8abefc9f0 8fe75b209e -- claude-codex-memory-bridge/`：目标增量仅两文件、76 行新增：`install_bridge.py` +32，`tests/test_install_bridge.py` +44；`git diff --check` 通过。
- 未从后来 `HEAD` 运行被审代码。用 `git archive 8fe75b209e` 与 `git archive f8abefc9f0` 解到独立 `/tmp` 树；候选 archive 文件与 Git blob 字节核对一致：
  - `install_bridge.py` SHA-256 `a2ba6cc630136e2f530dd7b5bf20952a98a6c3bf6379011aee5b59a8f719adcc`
  - `tests/test_install_bridge.py` SHA-256 `870edf6e3d8cea1557e6b3463eeed50d32c87034b88c4c36ea81ac3ecdffb6dd`

## P1：新错误信息推荐的卸载顺序会删除唯一 receipt 并留下 live handler

### R7-P1-A — `run uninstall first` 是确定性危险的恢复指引

位置：候选 `claude-codex-memory-bridge/install_bridge.py:937-947`，尤其错误文本 `:945-946`；与 `uninstall()` 对 absent 行的跳过逻辑 `:1172-1180`、receipt 删除 `:1220-1222` 组合触发。

隔离实文件复现（没有调用本机真实 hooks；installer 行为没有 mock，只有临时 SSD 根、`Path.home()` 与 volume UUID 注入）：

1. 在临时 SSD 建主配置与池化账户 `pool-bronze-36/home/hooks.json`，保存 pristine bytes/mode。
2. 调用候选 `install()`，确认两份配置各有一个 owned handler 和有效 `latest-receipt.json`。
3. 将账户目录改名为 `pool-silver-84`，内容不变。
4. 第二次 `install()` 正确拒绝，错误为：

   ```text
   refusing to record an already-bridged config as a pristine baseline (did this path move since the last install? run uninstall first): .../pool-silver-84/home/hooks.json
   ```

5. 按错误信息，在目录仍叫 `pool-silver-84` 时立即调用 `uninstall()`。
6. 实际结果：
   - `uninstall()` 返回 `ok:true`；
   - receipt 中旧路径 `pool-bronze-36/.../hooks.json` 被列入 `unreachable`；
   - 新路径 `pool-silver-84/.../hooks.json` 不在 receipt 中，因此完全未处理，owned handler 数仍为 1；
   - `latest-receipt.json` 与 pending journal 均不存在；
   - 随后 `verify()` 报 `not installed: no receipt found`；
   - 再把目录移回 `pool-bronze-36` 后，`install()` 仍拒绝 `already-bridged`，因为 receipt 已经删除，工具级恢复链断开。

该复现在 `/usr/bin/python3` 3.9.6 与 PATH `python3` 3.14.6 上结果相同。配置仍位于 Codex 会使用的新账户目录，handler 继续执行；操作却报告成功并删除当前 receipt，符合 P1。

### 必需修复方向

1. 至少删除危险的 `run uninstall first` 指引，明确要求：**先把文件恢复到 receipt 记录的原路径，再运行 verify/uninstall；绝不能在路径仍移动时卸载。**
2. 更稳妥的 fail-closed 修复是在 `uninstall()` 写 journal/配置/receipt 前，扫描当前 discover 到、但不在 receipt 的配置；若其中存在 owned handler，拒绝卸载并保持 receipt、pending 与所有配置不变。仅修文案不能阻止脚本调用方在改名状态直接卸载。
3. 增加回归：install → 账户目录永久改名 → install 拒绝 → 在仍改名时调用 uninstall；必须断言 uninstall 拒绝、receipt byte-exact 不变、无 pending、所有配置 byte/mode 不变。随后恢复原路径，verify/uninstall 必须精确恢复 pristine。
4. 同一回归至少覆盖跨 release 变体；父目录改名/软链接回接也宜覆盖。

当前新增测试 `test_install_refuses_to_adopt_an_already_bridged_config_as_pristine_after_a_rename`（候选测试 `:876-918`）只走“拒绝后先改回原名，再 install/uninstall”的安全路径，没有执行错误信息推荐的顺序；它也只断言 receipt 仍存在，没有断言 receipt byte-exact 不变或所有配置/runtime tree 不变，因此漏掉此 P1。

## (a) R6-P1-A 主修复独立验证

我没有复用候选测试的账户名，分别使用不同临时账户并对拒绝前后的整个临时 SSD 树做相对路径、类型、mode、文件 SHA-256 快照比较。

### 同 release 改名

- `pool-saffron-28` 安装后改名为 `pool-indigo-93`。
- 第二次同 release `install()` 报 `already-bridged`。
- 整棵临时 SSD 快照完全相等：main config、改名后 config、receipt、所有备份、release 文件与目录 mode 均未改变；pending 不存在。
- 改回原名后，正常 reinstall + uninstall 成功；两份配置 bytes 与原 mode 均精确恢复。

### 跨 release 改名

- `pool-umber-44` 安装 v1 后改名为 `pool-cobalt-61`，再修改隔离 source script 形成 v2 release。
- v2 `install()` 在写 runtime 前拒绝；整树快照、旧 receipt 完全不变，release 目录数量不变（v2 runtime 未落盘），pending 不存在。
- 改回原名后 v2 install 成功，`release_id != v1 release_id`；uninstall 把 bytes/mode 精确恢复到真正 pristine。
- 同一探针对基线 `f8abefc9f0` 非空转：基线 v2 install 成功，把 v1-installed bytes 记为新路径 `before_sha256`；随后 uninstall 返回 `ok:true`，新路径仍是 v1-installed bytes、owned handler 仍为 1，receipt 已删除。

### 整个 `codex-accounts/` 改名并软链接回接

- 安装两个池化账户后，将父目录改名为 `codex-vault-2026`，再创建 `codex-accounts -> codex-vault-2026` 目录软链接。
- `discover_hook_configs()` 解析到新的真实路径；第二次 install 在第一个新路径的 owned-handler 预检处拒绝。
- 拒绝前后整树快照与 receipt 完全不变；去掉软链接、恢复父目录后 install/uninstall 精确还原全部配置。

所以：**R6-P1-A 的“禁止把已带 handler 的新路径收养为 pristine”这个局部目标已真正修复；NO-GO 来自修复给出的下一步会重新造成同一 P1 后果。**

## (b) 临时不可发现后恢复的结转路径

- 使用 `pool-mint-22`、`pool-ruby-78`，首次安装后把 `pool-mint-22` 整个目录暂移到 discovery 根之外；第二次 install 仍有 main + 另一个账户可发现。
- 第二次 receipt 中目标旧路径 row 与首次 receipt **逐字段完全相等**；verify 将旧路径列入 `unreachable`，其余配置正常校验。
- 把目录移回原路径后，下一次 install 没有被新增检查误判：该路径命中 `previous_row`，走既有 after-digest/mode 漂移校验。
- 最终 uninstall 后三份配置 bytes/mode 均精确回到原始态。

结论：第三轮已修的 carry-forward/rediscovery 路径未回归。

## (c) 测试套件与计数

候选 archive 内运行用户指定命令，两个解释器均真实全绿：

| 解释器 | 版本 | 结果 |
|---|---:|---:|
| `/Applications/Xcode.app/Contents/Developer/usr/bin/python3` | 3.9.6 | `Ran 73 tests in 0.645s`，OK |
| `/opt/homebrew/opt/python@3.14/bin/python3.14`（PATH `python3`） | 3.14.6 | `Ran 73 tests in 0.582s`，OK |

AST 独立计数：`test_install_bridge.py` 从基线 33 个 test methods 增至候选 34 个（9 helper tests + 24 end-to-end tests + 1 SIGKILL recovery test）。全套另有 39 个 `test_claude_memory_hook.py` 测试，合计 73。

绿色套件没有覆盖 R7-P1-A 的“照错误提示继续卸载”链路，因此不能推翻 NO-GO。

## (d) 第一至第六轮修复抽查

目标 diff 只新增预检和一个测试，没有删除或改写这些修复；双解释器全套测试均通过，并做了以下定向代码/边界核对：

| 项目 | 实时证据 | 结论 |
|---|---|---|
| P1-2 uninstall 事务日志 | `test_sigkill_mid_uninstall_is_fully_completed_by_recovery` 真实子进程 SIGKILL；journal kind-aware 路径 `install_bridge.py:687-804,1152-1245` | 未回归 |
| `owned_handler` 精确匹配 | 结构化 `shlex.split(..., comments=True)` + 相邻精确 token `:247-288`；substring/comment 三变体自建探针均保留 | 未回归 |
| 5+2 处 stat 保护 | 7 个业务 call site 全部仍经 `_mode_bits()`：`642,766,798,909,1055,1217,1260`，helper 在 `:143-152` 捕获 OSError | 未回归 |
| R2-P1-A prev/before 分离 | upgrade 中途失败回到 v1 working state 的测试通过；row shape 与 recovery 分别检查 `:504-544,727-770` | 未回归 |
| R2-P1-B 并发锁目录创建 | fresh runtime 的真实 `main()` + lock contention 测试通过；逐级 private dir `:1312-1325` | 未回归 |
| R3-P1-A 永久退役结转 | `test_permanently_retired_account_does_not_lock_out_other_accounts` 通过 | 未回归 |
| R3-P2-A / R4-P2-A 备份清理容错 | prior backup prune 与 carried-forward backup prune 两测试通过；`_receipt_rows()` 仍只 resolve、不无条件读 backup `:583-684` | 未回归 |
| R4-P1-A 不可读 fail closed | unreadable managed config 的 verify/uninstall 测试通过；`_path_is_absent()` 只把 ENOENT 当 absent `:555-580` | 未回归 |
| R5-P1-A 提前加载 | uninstall 在 durable journal 前加载所有实际写入 backup `:1181-1209`；对应 missing-backup 测试通过 | 未回归 |
| R5-P2-A 提前判定 | carried-forward row 在任何 transaction 写之前经 `_path_is_absent()` `:864-889`；对应 indeterminate 测试通过 | 未回归 |

任务背景中刻意未处理的 R6-P2-A/B/C 与三个 P3 本轮没有代码声称修复；本报告不把它们误报为候选回归，也不把全绿套件解读成这些项已关闭。

## (e) 新检查的误报与漂移交互

### 精确 marker 碰撞

在一个从未有 receipt 的新账户中手写无关命令：

```text
/usr/bin/printf unrelated --bridge-id orca-claude-native-memory-v1 --mode demo
```

候选把它识别为 owned 并干净拒绝，整树不变；错误仍建议 uninstall，但此时 uninstall 只会报 `not installed: no receipt found`。这说明错误文案对“真正移动”和“保留字碰撞”两类原因都不准确。

不过这不是另一个 P1：精确的 `--bridge-id <BRIDGE_ID>` 相邻 argv pair 本来就是现有 `owned_handler()` 的唯一所有权标志；本补丁之前，`update_hook_config()` 会把这种 handler 当 owned 删除/替换。本轮从“替换”改为“拒绝且不写”反而更安全。自建的三种相似但不精确情形（BRIDGE_ID 仅为 substring、精确 pair 只在 shell comment、值带额外 suffix）均正常安装、原 handler 全保留、uninstall 后 byte/mode 精确恢复。

### 与既有 drift 检测的优先级

对 receipt 已管理的原路径追加外部 handler，同时保留 owned handler，再调用 install：候选仍进入 `previous_row is not None` 分支，报 `hook config no longer matches the last known installed state`，没有被新 `already-bridged` 检查覆盖；receipt、pending 和配置均不变。新检查只作用于没有 path-keyed receipt row 的路径，分支优先级正确。

## 只读边界

- 没有对本机 `/Volumes/Extreme SSD` 上真实 Codex hooks、真实 bridge runtime、账户目录或凭据运行 plan/install/verify/uninstall/recover。
- 没有安装、激活、提交、合并、stage 或修改候选源代码/测试。
- 所有行为复现都在独立 `/tmp` 根中，使用 archive 的精确候选字节和真实文件系统操作；仅隔离环境常量、`Path.home()`、volume UUID 被注入。
- 仓库内唯一新增文件是用户指定的本报告。

## 最终判定

**NO-GO：1 个 P1（R7-P1-A），0 个 P0。**

候选局部检测是正确且无部分写入的，但 release 不能带着一个会把用户引导到 `ok:true`、handler 留存、receipt 删除状态的恢复指令。修复并增加“路径仍改名时 uninstall 必须拒绝且 receipt 不变”的回归后，应对新精确候选重新做同 release、跨 release、父目录与双解释器复核。
