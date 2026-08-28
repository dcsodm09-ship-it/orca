# `install_bridge.py` Round 3 独立只读复核

- 日期：2026-08-17（Asia/Taipei）
- 复核者：Codex `gpt-5.6-sol` / xhigh dispatched worker
- 仓库：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`
- 基线：`09459cf6a0211fde84d16ae7f040ac8d9351c973`
- 候选：`870810d468a4727a357448e4986d64975826334c`
- 候选 production blob：`install_bridge.py` = `23a819040937b293b42a540de60a6979bb8d12f1`
- 候选 test blob：`tests/test_install_bridge.py` = `40f9a067192c05c8775a893c8a2cd26ca6cf22e8`
- 范围：上述两文件及 `09459cf6a0..870810d468` 增量
- 结论：**NO-GO**

## 结论摘要

本轮声称修复的 R2-P1-A、Codex P1-R2-1 的“临时缺席”直接场景、R2-P1-B、lock 的裸 `OSError`、`release_id` 缺失裸 `KeyError`，在精确候选 Git 导出中都通过独立探针。首次安装的四个真实 SIGKILL 边界也保持原有语义：pending/首份 live/全部 live 后均回滚到 pristine，latest 写完后提交成功。

但候选新引入一个可确定复现的 **P1**：一个 latest receipt 已管理的账户若不是临时改名、而是被永久退役/重建到新路径，`install()` 会因 missing path 永久拒绝；`verify()` 与 `uninstall()` 又因同一路径不存在而永久拒绝；`recover` 只返回 `state=none`，`plan` 仍返回 `ok:true`。其他仍存在账户中的 bridge handler 持续活跃，且新增账户也不能解除这个锁死状态。候选没有工具内恢复路径。

另有两个非阻断但必须修复的证据质量/兼容性问题：新增的升级中断测试实际在 pending journal 写入前失败，未覆盖它声称的恢复分支；receipt row 增加必填字段但 `RECEIPT_SCHEMA` 仍为 v1，导致 `09459cf6a0` 写出的同 schema receipt 被本候选的 install/verify/uninstall 全部拒绝。

## 启动、身份与只读边界

1. 启动为降级态：`ORCA_CONTEXT_NACK_V1`，原因是 `central reviewed source freshness mismatch: wiki`。本报告不声称加载了 Orca 中央 reviewed context，只依赖当前可见项目文件、Git 对象和本轮现场输出。
2. 当前分支为 `完善orca`，初始 HEAD 为后续 commit `33715915e8292072a8525aa260c835545a84dbec`，不是受审 candidate；但复核开始时两份目标工作树文件分别精确等于上述 candidate blobs，且 `870810d468..HEAD` 对这两文件没有提交差异。
3. 复核后半段，共享工作树中的 `install_bridge.py` 被另一并行活动改为未提交 blob `5a4eb1b1d9fb55b616e566126d805ce1b410528d`。从发现漂移起，本复核停止使用工作树 production 文件，改从 `git archive 870810d468` 导出到新的 OS 临时目录；导出后再次确认两 blobs 精确为 `23a819...` / `40f9a0...`，并重跑套件与关键探针。
4. 共享工作树的漂移 diff 中包含并行复核意见文字，因此本 lane 在该时点之后不能诚实声称与另一路仍然“互不知情”。本报告的 P1 已在精确 `870810d468` Git 导出上以独立 fixture、真实 `main()` 动作和确定性盘面断言重现，但严格的 mutually-unaware provenance 已受共享写集污染，协调者应保留这一事实。
5. 未对真实 `/Volumes/Extreme SSD/Orca/local-homes` 或任何真实 Codex `hooks.json` 运行 install/verify/uninstall；未安装、激活、提交、合并或修改候选。所有会改变配置盘面的复现均位于 `TemporaryDirectory` 或只含精确 Git 导出的 OS 临时目录中。
6. 仓库内唯一由本 worker 刻意创建的文件是本报告；没有修改两份候选 tracked 文件。按 Orca 全局规则仅重命名了本 worker 自己的终端，并发送了心跳/完成消息。

## 现场结果总览

| 检查 | 结果 |
|---|---|
| 指定官方套件（漂移前工作树） | **66/66 PASS**，0 failures / 0 errors，1.113s |
| 精确 `870810d468` Git 导出套件 | **66/66 PASS**，0 failures / 0 errors，0.624s |
| R2-P1-A：升级时第一份 live 已写、第二份 live 写失败 | PASS；自动恢复到 byte-exact v1，不是 pristine；pending 清；旧 latest 原样保留；verify/retry-upgrade/uninstall 均可用 |
| R2-P1-A：首次安装 4 个真实 SIGKILL 边界 | PASS；`pending/first/last -> rolled_back`，`latest -> committed`，24 项盘面断言通过 |
| P1-R2-1：历史路径临时缺席后恢复 | PASS；install fail closed，列出精确路径，latest 和其他配置不变；恢复路径后可正常 install/uninstall |
| P1-R2-1：只增加新账户 | PASS；新 receipt 单调增加 row，所有旧/新配置最终 byte+mode 精确卸载 |
| R2-P1-B：无 `.shared-runtime` 的真实 `main("install")` | PASS；main rc=0，`.shared-runtime` 与 runtime base 均 0700，lock 0600 |
| lock mkdir / `os.open()` 真实错误 | PASS；两者均为 rc=1 结构化 JSON，未泄漏裸 `OSError` |
| receipt 仅缺 `release_id` | PASS；`main verify` 精确返回 `{"ok": false, "error": "invalid receipt"}`，无 `KeyError` |
| 第一轮 uninstall 日志 / owned_handler / stat guard 抽查 | PASS；见下文 |
| 永久退役一个历史受管账户 | **FAIL / P1**；install/verify/uninstall 无出口，recover no-op，plan 绿，其他账户 bridge 仍活跃 |
| 新升级中断测试的真实触发点 | **FAIL / 空转覆盖**；第 7 次写是 `backup/.../receipt.json`，pending 尚不存在 |

## 阻断问题

### P1-R3-1：永久退役一个受管账户会把所有工具动作锁死，其他账户 bridge 无法卸载

#### 位置与根因

- `install_bridge.py:719-747`：install 只要发现 latest receipt 的任一路径未被本次 discovery 找到，就无条件拒绝；没有区分“临时改名”与“账户已永久退役/换新路径”。错误提示要求 `run uninstall first`。
- `install_bridge.py:536-555`：`_receipt_rows()` 对 receipt 中每个 config path 使用默认 `must_exist=True` 的 `resolve_ssd_path()`；缺席路径在建立 rows 之前就报 `path unavailable`。
- `install_bridge.py:919-943`：verify 同样无条件要求每个历史 row 的 path 存在。
- `install_bridge.py:954-968`：uninstall 先调用 `_receipt_rows()`，因此错误提示建议的 uninstall 自己也必然因相同缺席路径失败。

修复 Codex P1-R2-1 需要不丢失历史 baseline，但“只要缺席就永远拒绝 install”并不等价于安全保留。它把普通账户生命周期事件变成全局锁：剩余受管账户仍含 bridge，receipt 仍存在，却没有一个 modifying action 能清理它们。

#### 确定性复现

精确从 `870810d468` Git 对象导入 production module；fixture 使用与候选测试不同的账户名 `moss-r3` / `onyx-r3`，共 main + 2 accounts，权限为 0640 / 0604 / 0600：

1. 通过真实 `main("install")` 完成第一次安装，三份配置各有且只有一个 owned bridge handler。
2. 将 `moss-r3/home/hooks.json` 移出 discovery 名称，模拟该账户被永久退役；保留 main + `onyx-r3` 两份可发现配置，明确不触发 discovery 自身“至少 2 份配置”的既有下限。
3. 依次通过真实 `main()` 调用 install / verify / uninstall / recover / plan。
4. 再创建一个全新 `pearl-r3` 账户，重试 install。

实际结果：

```text
install   rc=1  ok=false  previously-managed ... no longer discoverable ... (run uninstall first)
verify    rc=1  ok=false  path unavailable: .../moss-r3/home/hooks.json
uninstall rc=1  ok=false  path unavailable: .../moss-r3/home/hooks.json
recover   rc=0  ok=true   state=none
plan      rc=0  ok=true
```

- `latest-receipt.json` 与退役前逐字节相同；没有 pending journal 可恢复。
- main 与 `onyx-r3` 中 owned handler count 仍各为 1，bridge 持续在 prompt 上执行。
- 新建 `pearl-r3` 后 install 仍因旧 `moss-r3` 路径拒绝；新账户保持 pristine，无法推动状态。
- 错误建议 `run uninstall first` 不可执行，因为 uninstall 依赖同一个已经不存在的 path。

相同永久退役序列在精确基线 `09459cf6a0` 上（直接 action，避免该基线自己的 fresh-main lock P1）可继续 upgrade install，receipt 变为两行，并正常 uninstall、逐字节+权限恢复剩余两份配置。基线对“临时恢复”不安全是已知 P1-R2-1，但候选当前策略把修复变成另一个日常账户生命周期 P1。

#### 影响与修复方向

这是 P1 而不是单纯提示问题：工具不能再卸载仍在执行的 bridge，且没有 recovery journal、没有可用的推荐动作；只有手工重建缺席路径或手工编辑私有 receipt/config 才能退出。账户删除、池化账户重建到新 UUID/路径、磁盘清理都能触发，不需要崩溃、竞态、特权或内容漂移。

修复必须同时满足：

1. 历史 path/baseline 单调保留，临时恢复时不得把已安装内容重采样为 pristine。
2. 缺席必须是显式状态，不得使所有存在路径都不可卸载；uninstall 应能恢复当前存在的 rows。
3. 对缺席 row 不能在清除所有证据后谎报完全卸载；应保留可恢复 tombstone/未完成状态，直到路径恢复后能清理或有明确、审计化的账户退役确认。
4. 新回归至少覆盖：install -> 永久退役 1/3 paths -> upgrade/install/verify/uninstall/recover/plan -> 新账户出现，以及缺席旧路径随后重新出现的清理语义。

## 指定修复的独立验证

### (a) R2-P1-A：修复本身通过

独立 ordinary-write-failure 探针没有采用候选测试的固定 call #7。三份配置完成 v1 install 后改变 source 产生新 release id；v2 install 真实写完第一份 live main config 后，在第二份 live config 写入前按精确 path 抛 `InstallError`。

结果：

- install 返回 `install failed; prior hook configs were restored`。
- 三份配置 byte-exact 回到 v1 installed bytes，均不等于真正 pristine；未发生过度卸载。
- pending 清除，旧 `latest-receipt.json` 逐字节保留。
- verify 返回 v1 release；随后无故障重试成功安装不同 v2 release；最终 uninstall 把三份配置 byte+mode 精确恢复到真正 pristine。

首次安装假阳性回归在精确 candidate export 中用 path-addressed 真实 SIGKILL 完成，不依赖固定写次数：

| 杀点 | 恢复前关键盘面 | 新进程 recover | 最终盘面 |
|---|---|---|---|
| pending durable 后 | 全 pristine，pending=yes | `rolled_back` | byte+mode pristine，pending/latest=no |
| 第一份 live 写后 | 1 installed / 1 pristine | `rolled_back` | byte+mode pristine，pending/latest=no |
| 最后一份 live 写后、latest 前 | 全 installed，latest=no | `rolled_back` | byte+mode pristine，pending/latest=no |
| latest 写后、journal 清理前 | 全 installed，latest=yes | `committed` | 全 0600、各 1 owned handler，pending=no |

结论：`prev_*` 与 install_state 分离在直接行为上正确，没有破坏首次安装的四个已知恢复边界。

### (b) P1-R2-1：临时缺席与纯新增通过；永久退役仍阻断

独立 fixture 为 main + `willow-r3` + `xenia-r3`：

- v1 install 后临时移走 `willow-r3/hooks.json`，install 明确拒绝并列出精确路径；latest 与仍可见配置逐字节不变。
- 路径恢复后新增 `yarrow-r3`，install 成功把 receipt 从 3 rows 增至 4 rows。
- uninstall 后旧三份配置及新账户各自 byte+mode 精确回到首次被管理前状态。

所以任务要求的“临时缺席 fail closed”和“只新增不误伤”均通过；但同一策略在永久退役场景产生 P1-R3-1，不能据此前两项 PASS 给候选 GO。

### (c) R2-P1-B：fresh `main()` 通过

精确 candidate export、无 `.shared-runtime` 的独立 fixture 中真实执行 `main("install")`：rc=0；`.shared-runtime` = 0700，bridge runtime base = 0700，`installer.lock` = 0600；live configs 安装完成。该结果不是直接调用 `install()`，确实覆盖 `_acquire_exclusive_lock()`。

### (d) 两个 P2 修复通过

- mkdir：将 fixture `local-homes` 临时设为 0500，真实 `main("recover")` 触发创建 `.shared-runtime` 的 PermissionError；结果 rc=1、结构化 `cannot create runtime directory`，无裸异常。
- `os.open`：预建 0700 runtime，并将 `installer.lock` 建为目录，真实触发 EISDIR；结果 rc=1、结构化 `cannot open lock file`，无裸异常。
- `release_id`：真实 install 后仅删除私有 latest receipt 的顶层 `release_id`；`main("verify")` 精确返回 `{"ok": false, "error": "invalid receipt"}`，无 `KeyError`。

## 测试套件、计数与非空转审计

指定命令现场结果：

```text
cd claude-codex-memory-bridge
/usr/bin/python3 -m unittest discover -s tests -v

Ran 66 tests in 1.113s
OK
```

共享工作树漂移后，又从精确 `870810d468` Git 导出执行（额外设置 `PYTHONDONTWRITEBYTECODE=1` 不改变测试逻辑）：

```text
Ran 66 tests in 0.624s
OK
```

### 数量漂移

静态计数为：

- `09459cf6a0`：install_bridge tests 24，hook tests 39，总计 63。
- `870810d468`：install_bridge tests 27，hook tests 39，总计 66。
- 增量只新增 3 个 `def test_*`，不是背景所称“61+5”；另有一个既有 corruption test 被换成 `release_id` 向量，两个既有 pending receipt fixture 仅适配新 schema。

66/66 是真实绿灯，但“新增 5 个测试”不是这个精确 diff 的事实。

### 基线反证

把 candidate 的测试文件放入精确 `09459cf6a0` 临时导出，单跑三项新增测试和修订后的 `release_id` 测试：2 failures + 2 errors，四项都不会在旧 production code 上绿灯。具体为：临时缺席没有抛错、fresh main rc=1、升级中断后 pending 阻止 verify、缺 release_id 裸 `KeyError`。

但“旧代码失败”不自动证明候选测试测到了预定边界。

### P2-R3-TEST：升级中断测试在候选上没有产生 pending journal

`tests/test_install_bridge.py:461-508` 固定在第 7 次 `atomic_write` 抛错，并声称 receipt/pending 都已 durable、尚未写 live config。精确 candidate 的真实写入序列是：

```text
1 release/claude_memory_hook.py
2 release/policy.json
3 config-A pristine backup
4 config-A after backup
5 config-B pristine backup
6 config-B after backup
7 backup/<install_id>/receipt.json   <-- 实际失败点
8 pending-install.json
```

现场 instrument 结果：call #7 = `backup/.../receipt.json`，`pending_exists=false`，旧 latest 仍存在。异常发生在 `install()` 的 journal/try block 之前，v1 当然保持可验证；测试 PASS 与 `prev_*` recovery fix 无关。它在 `09459cf6a0` 失败，是因为旧代码写入次数不同、call #7 恰好落到 live write，而不是一个跨版本稳定的语义触发点。

本报告的 path-addressed partial-write probe 与真实 SIGKILL matrix 证明 production 修复确实有效，但候选官方回归必须改成按目标 path/phase 触发，并明确断言 pending 曾 durable、live 形成混合 v1/v2 状态、recover 实际执行。

## 第一轮/第二轮已确认修复的回归抽查

- uninstall 事务日志：精确 66-test suite 的真实 SIGKILL mixed-state 测试通过；另在 candidate blob 仍冻结时独立杀在 latest 已删除、pending 未清边界，新进程完成 `state=uninstalled`，pending 清，配置 pristine。
- `owned_handler`：exact adjacent/generated 与 quoted value 为 true；unquoted shell comment、substring、other flag value、non-adjacent pair、`--bridge-id=<ID>` 为 false，7/7 与既定精确 token 语义一致。
- stat guards：精确 candidate production 的实际 `.stat()` 调用只剩 `resolve_ssd_path()` 内受 try/except 包装的 device 比较（line 123）与 `_mode_bits()` 内受包装的 mode 读取（line 138）；suite 的 missing-path probe 返回 `InstallError`，无裸 OSError。
- 第一轮 install-side 四 SIGKILL 边界见上文，全部通过。
- 同 release / 跨 release baseline inheritance、uninstall 清 latest、并发 flock 的既有测试均在精确 66/66 中继续通过。

## 其他非阻断问题

### P2-R3-COMPAT：同名 v1 receipt schema 的向后兼容被无版本变更地打断

`RECEIPT_SCHEMA` 在基线与候选均为 `orca.claude-native-memory-bridge-receipt.v1`，但候选 `_validate_receipt_shape()` 新要求 `prev_backup` / `prev_sha256` / `prev_mode` / `after_backup`。在隔离目录中用精确 `09459cf6a0` 真实 install 生成 receipt，再用 `870810d468` 调用：

```text
verify    InstallError: invalid receipt config row
install   InstallError: invalid receipt config row
uninstall InstallError: invalid receipt config row
```

当前目标机器据 Round 2 现场证据尚无 `.shared-runtime`，因此这不是本轮 NO-GO 的主 P1；但同 schema 版本不应无迁移地改变必填字段。若任何机器运行过前一 candidate/版本，本候选无法升级或卸载。应 bump schema 并明确迁移/拒绝策略，或对旧 v1 receipt 安全补建 prev/after 持久证据。

### 既有 P3 保持原级别

`--bridge-id=<ID>` 不被识别、handler group 粒度删除、path validation/stat 的 TOCTOU、写前重查与 replace 间窗口、`remove=True`/`must_exist=False` 死分支等既有 P3 未因本轮直接变化而单独升级。它们不抵消上述 P1。

## 最终结论

**NO-GO for `870810d468a4727a357448e4986d64975826334c`.**

R2-P1-A、临时缺席 fail-closed、纯新增账户、fresh-main permissions 和两个裸异常修复本身都通过；但永久退役一份历史受管配置会让 install/verify/uninstall 无工具内出口，recover no-op、plan 仍绿，而其他账户 bridge 保持活跃。这是普通账户生命周期即可触发的 P1，必须在新的精确候选上修复，并对“临时消失、永久退役、换新路径、旧路径重新出现”四种状态做成一组不可空转的回归。

此外，升级中断官方测试目前没有进入 pending recovery 分支；即使生产修复经独立探针通过，也必须修正测试触发点，不能把当前 66/66 当作该恢复边界已有可靠回归覆盖。
