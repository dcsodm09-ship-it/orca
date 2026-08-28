# `install_bridge.py` 第五轮独立只读复核（Codex sol / xhigh）

## 结论：**GO**（以 P0/P1 阻断门为准；不是零缺陷）

精确候选 `9e2205dc4325b6689f4ccb8ee42c505257209973` 上：

- **R4-P1-A 已修复。** `verify()` 与 `uninstall()` 对“配置仍在、但祖先目录暂时不可遍历”的真实 `EACCES` 会失败关闭，`main()` 返回 `rc=1` 与干净 JSON `InstallError`；receipt 不变、没有 pending journal。权限恢复后 verify/uninstall 正常，所有存活配置都按各自原始 bytes 和 mode 精确恢复。
- **R4-P2-A 已修复。** 结转行继续指向已经被整个删除的旧事务备份目录时，后续 `install()` 的提交收尾不再卡住；`verify()` 与 `uninstall()` 都能完成，pending 被清除，存活配置精确恢复，缺席行报告 `unreachable`。
- **真正 ENOENT 没有被误伤。** 永久删除账户仍会被识别为 absent；verify/install/verify/uninstall 全链可用。
- **没有复现新的 P0/P1。** 轮次 1–4 的指定修复项也没有发现回归。
- 发现两个本轮惰性化引入的 **P2（非阻断）**，详见下文：
  1. 对 live 行也取消 eager backup 校验，会丢掉卸载写入前的完整 preflight，并允许提交收尾在当前 live 行的 pristine backup 已丢时仍报告安装成功；
  2. `backup` 改为 `resolve_ssd_path(..., must_exist=False)` 后，真正读取时没有重新执行 `must_exist=True` 的同设备检查。

这里的 GO 明确按本任务要求的阻断标准给出：我没有找到可复现 P0/P1。它不表示上述 P2 可以被遗忘，也不表示文件已经“第一次完全干净”。

## 1. 启动态、边界与候选身份

- 启动态为降级：收到 `ORCA_CONTEXT_NACK_V1`，原因是中央 reviewed source 的 wiki freshness mismatch。**没有声称 Orca 中央上下文已加载**；本报告仅使用当前可见仓库文件、Git 对象、指定历史报告和本轮本机只读/临时目录实测。
- 仓库：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`
- 指定基线：`d80d14c5174ae0936941b72aa6ee823b6d2fc4d6`
- 指定候选：`9e2205dc4325b6689f4ccb8ee42c505257209973`
- 候选 parent：`652e19cb775fd42641f3ffa66e176b5b8f9769e7`
- 候选 tree：`1258d01704b87750b32d11b8c809b3c7bb8e01b6`
- 复核开始时 `HEAD`：`9f3b23941621b1121626d8c77860384e99ef1645`，晚于指定候选；`git diff 9e2205dc43 HEAD -- claude-codex-memory-bridge/` 为空。因此我仍用 `git archive` 分别把候选与基线提取到独立临时目录，所有执行都针对精确归档，不把 later HEAD 当候选。
- 候选 blob：
  - `install_bridge.py`: Git blob `396c3a96c897a648888eaf18f52e37c1a798a9f3`，SHA-256 `2a5c6631cb9e164c475765e75cafe72898e3542fd84c07991e9793cf6c101851`
  - `tests/test_install_bridge.py`: Git blob `f55f787bb164e2fa1e52ae33a13c3dc566560f32`，SHA-256 `fcda822bfc77079b0269dc034d51a6cb5e31a15ee1f8e1bc3767ab4d45a4cfbe`
- `git diff --check d80d14c517 9e2205dc43 -- claude-codex-memory-bridge/` 无输出。
- 增量只有两文件：`install_bridge.py` `+63/-29`、`tests/test_install_bridge.py` `+89/-0`。

只读边界：没有修改候选源文件或测试，没有提交、合并、安装或激活；没有对真实 `/Volumes/Extreme SSD/Orca/local-homes` 的 hooks、runtime 或 receipt 执行任何 action。所有 install/verify/uninstall/recover 都在系统临时目录中的 fake SSD root，且模块常量、`Path.home()`、`volume_uuid()` 均指向该隔离夹具。仓库内唯一由本 worker 新建的文件是本报告。

## 2. 代码增量审计

### 2.1 `_path_is_absent()`

候选 `install_bridge.py:555-580`：

- `path.lstat()` 成功：返回 `False`；
- 仅捕获 `FileNotFoundError` 为 absent；真实 POSIX syscall 的 ENOENT 会映射到该异常；
- 其他 `OSError` 统一包装为 `InstallError("cannot determine whether ... exists")`，保留原异常为 `__cause__`。

两个受影响调用点都替换完成：

- `_receipt_rows()`：`install_bridge.py:592-613`
- `verify()`：`install_bridge.py:1050-1063`

没有发现剩余的第四轮 `not path.exists() and not path.is_symlink()` absent 判定。

### 2.2 `backup` 惰性加载

候选 `install_bridge.py:583-684` 不再产生 `backup_raw`；全文没有残留 `row["backup_raw"]`。真正写 pristine backup 的两个路径都有显式 `_load_backup()`：

- pending uninstall 恢复：`install_bridge.py:772-787`
- 正常 uninstall 主循环：`install_bridge.py:1121-1130`

`_receipt_rows()` 只有两个调用者：`recover_pending_install()`（`:714`）和 `uninstall()`（`:1099`）。不存在第三个调用者继续隐式依赖已删除的 `backup_raw` 键。

但“没有残留键访问”不等于“没有行为损失”；对 eager 校验的两个隐式依赖形成了本报告的 R5-P2-A，见 §6。

## 3. (a) R4-P1-A 独立复现：**FIXED**

### 3.1 解释器差异实测

用一个临时 `gate/deeper/hooks.json`，只把更高一层 `gate` 改为 `0o400`：

| 解释器 | `Path.exists()` | `Path.is_symlink()` | `Path.lstat()` |
|---|---:|---:|---:|
| `/usr/bin/python3` 3.9.6 | `PermissionError(errno=13)` | `PermissionError(13)` | `PermissionError(13)` |
| PATH `/opt/homebrew/bin/python3` 3.14.6 | `False` | `False` | `PermissionError(13)` |

权限恢复后文件内容仍为 `still here`。这独立确认了第四轮报告强调的解释器分歧：3.14 的 `exists()`/`is_symlink()` 会把 EACCES 吞成 `False`，而 3.9 会抛异常；`lstat()` 在两者都保留错误。

### 3.2 候选真实 EACCES 场景

我的夹具与新增候选测试不同：

- 4 份配置，内容均不同，原始 mode 分别为 `0644/0640/0604/0600`；
- 候选测试对 `.../home` 做 `chmod 0000`；本复现对更高一层账户目录 `.../codex-accounts/82bb-bravo` 做 `chmod 0600`（目录有读写但无执行/遍历位）；
- 在 PATH Python 3.14.6 和 `/usr/bin/python3` 3.9.6 上分别执行真实 `main()` 路径。

两种解释器结果一致：

```text
verify    -> rc=1, {"ok": false, "error": "cannot determine whether .../hooks.json exists"}
uninstall -> rc=1, {"ok": false, "error": "cannot determine whether .../hooks.json exists"}
```

直接调用 `_path_is_absent(target)` 得到 `InstallError`，其 cause 为 `PermissionError(errno=13)`；没有裸 traceback。失败后：

- `latest-receipt.json` SHA-256 与失败前完全相同；
- `pending-install.json` 不存在；
- 权限恢复后 `verify()["ok"] == True`；
- 随后 `uninstall()["ok"] == True`；
- 4/4 配置均 bytes+mode 精确回到各自 pristine，owned-handler 计数均为 0。

### 3.3 非空转基线对照

相同四配置、相同祖先目录 `chmod 0600` 场景在第四轮基线 `d80d14c517` + Python 3.14.6 上：

- `verify`：`rc=0, ok:true`，把不可读配置放进 `unreachable`；
- `uninstall`：`rc=0, ok:true`；
- `latest-receipt.json` 被删除；
- 恢复目录权限后，该配置仍有 1 个 owned handler。

这精确重现 R4-P1-A 的失败开放，不是候选测试或本复现空转。

### 3.4 EIO 与 ENOENT

- 对 `_path_is_absent()` 注入 `OSError(errno=5/EIO)`：得到带原 cause 的清晰 `InstallError`，没有被判 absent。
- 永久 ENOENT 实验：完成一次 install 后真正 unlink 某账户 hooks 并删除 `home` 与账户目录；随后 `verify -> install -> verify -> uninstall` 全部成功。该行在两次 verify 与 uninstall 均列入 `unreachable`，新 receipt 继续结转它；其余 3 份配置 bytes+mode 精确恢复，pending/latest 均在卸载后消失。

结论：本修复同时关闭 EACCES/EIO 假 absent，并保留真实 ENOENT 语义。

## 4. (b) R4-P2-A 独立复现：**FIXED**

独立夹具不是候选测试的 `acct-one/acct-two` 两/三配置形状：

1. 初始 5 份配置（main + `11-red/22-blue/33-green/44-gold` 四账户）完成 v1 install；
2. 真正删除 `33-green` 的 hooks、home 与账户目录；
3. v2 install，确认该行原样结转，`backup` 仍指向 v1；
4. 删除 **整个 v1 backup directory**；
5. 新增第六份 `55-silver` 配置；
6. v3 install；紧接着 verify 成功（因此 pending 不可能仍存在）；
7. uninstall 成功。

结果：

- v1→v2 结转行 `backup` 路径确实未变；
- v1 整目录确实不存在；
- v3 receipt 仍含退休行；
- `verify` 与 `uninstall` 都报告退休行为 `unreachable`；
- 5 份存活配置全都 bytes+mode 精确恢复，owned handler 均为 0；
- uninstall 后 latest 与 pending 都不存在。

非空转对照：在 `d80d14c517` 上做同类“整账户删除 → 结转 → 删除 v1 backup dir → 再 install”，第三次 install 报：

```text
InstallError: install failed and the durable recovery journal remains pending
pending-install.json == present
```

因此候选确实修复了第四轮报告的具体卡死路径。

## 5. (c) 测试套件与新增测试有效性

指定命令，在精确候选归档中：

```text
cd claude-codex-memory-bridge
/usr/bin/python3 -m unittest discover -s tests -v
Ran 70 tests in 0.548s
OK
```

PATH Python 3.14.6 也跑完同一套：

```text
Ran 70 tests in 0.441s
OK
```

AST 计数：

| 文件/类 | 基线 | 候选 |
|---|---:|---:|
| `InstallBridgeTests` | 9 | 9 |
| `InstallEndToEndTests` | 19 | 21 |
| `UninstallCrashRecoveryTests` | 1 | 1 |
| `test_install_bridge.py` 合计 | **29** | **31** |
| 全套（另含 hook 测试 39） | **68** | **70** |

新增两测试不是空转：

- unreadable 测试在基线上会走 PermissionError/失败开放分支；我的基线 counterfactual 直接重现了 `ok:true + receipt 删除 + handler 留存`。
- carried/pruned 测试对应边界在基线上会让第三次 install 失败并留下 pending；我的基线 counterfactual 精确重现。

覆盖备注（P3，不阻断）：

- unreadable 测试直接调用函数，只断言异常类型，没有断言 `main()` 的 `rc=1`、JSON 文案或原始 errno；本轮独立双解释器主路径实验补足了它。
- carried/pruned 测试在第三次 install 后没有立即断言 pending 不存在，随后调用的 `uninstall()` 自身会先 recover；不过我的独立实验在 v3 后立即调用 `verify()`，它在 pending 存在时必定拒绝，因而实证收尾已经完成。

## 6. (e) 本轮新发现

### R5-P2-A — 惰性 `backup` 取消了 live 行 preflight 与提交收尾完整性校验（**新，非阻断**）

根因：`_receipt_rows()`（`:583-684`）现在对所有行都不加载 `backup`，而调用方只在已经进入写入循环后按行加载：正常 uninstall `:1116-1126` 先写 pending，再逐行 `_load_backup()`；pending-uninstall 恢复也是 `:772-783` 逐行加载。install 提交收尾的 committed 分支 `:727-737` 则完全不验证 live 行的 pristine backup，直接删除 pending。

#### 实验 D：卸载前置校验丢失

4 份已安装配置，删除 receipt 最后一行的**当前、确实需要** pristine backup，然后 uninstall：

| 版本 | 返回 | 4 行磁盘状态 | pending |
|---|---|---|---|
| 候选 `9e2205dc43` | `InstallError: uninstall failed and ... journal remains pending` | `pristine, pristine, pristine, installed` | **存在** |
| 基线 `d80d14c517` | `InstallError: path unavailable: <backup>` | `installed, installed, installed, installed` | **不存在** |

也就是说，基线的 eager 校验虽然不能修复已丢 backup，却会在任何配置写入前拒绝；候选先部分卸载，再在末行失败并卡住 journal。

#### 实验 F：提交收尾不再检查 live 行回滚资产

在 install 写完 `latest-receipt.json` 后、进入 `recover_pending_install()` 前删除末行刚创建的 pristine backup：

| 版本 | install | verify | pending |
|---|---|---|---|
| 候选 | **success** | **true** | 不存在（已清除） |
| 基线 | `InstallError: install failed and ... journal remains pending` | 因 pending 拒绝 | 存在 |

候选因此可以对一个已经失去卸载回滚资产的 live install 报成功并清掉事务 journal。

**为什么定 P2 而不是 P1：** 两个复现都要求候选之外的主体在备份已被 durable 写入后删除/破坏当前事务真正需要的 backup；本文件没有清理 backup 的代码路径。上一轮对“外部清理备份目录”的 R3-P2-A/R4-P2-A 使用了相同的非阻断定级口径。没有该外部前提时，我没有复现错误成功、混合状态或不可恢复 journal。

**建议修复：** 保持 absent/结转行的 lazy 语义，但在写 pending 或任何配置之前，先为“本次确实将写 pristine”的所有 live 行完成 `_load_backup()` 并缓存 bytes；install committed 分支也应在清除 pending 前验证所有 live 行未来 uninstall 所需的 pristine backup。不要重新 eager 读取 absent 行，否则会复活 R4-P2-A。

### R5-P2-B — lazy `backup` 实际读取没有恢复同设备检查（**新，静态确认，非阻断**）

基线在 `_receipt_rows()` 用默认 `resolve_ssd_path(backup)`，即 `must_exist=True`，会执行 `resolved.stat().st_dev == root.stat().st_dev`。候选改为 `resolve_ssd_path(backup, must_exist=False)`（`:609`），实际写回前 `_load_backup()`（`:548-552`）只做 owned/private/digest 检查，不重新调用 strict `resolve_ssd_path()`。

因此若 receipt 路径词法上位于 SSD root 内、但该目录后来变成一个**不同设备的嵌套挂载点**，候选会读取它；基线会报 wrong device。这个问题需要外部挂载/替换备份路径，正常代码不会创建该状态；为遵守只读边界，本轮没有实际创建/挂载第二文件系统，结论来自直接控制流审计，故不升级为 P1。

**建议修复：** `_load_backup()` 在真正读取时先 `resolve_ssd_path(backup_path)`（`must_exist=True`）并使用其返回值，再做 `validate_owned_file()` 与 digest；最好保留 receipt 的原始路径到该时点，避免提前 canonicalize 后丢失原路径身份。

### 符号链接交互备注（P3 / 前存行为，不阻断）

直接对 dangling symlink 调 `_path_is_absent()` 返回 `False`（`lstat` 正确看见链接）；但调用点先 `resolve_ssd_path(..., must_exist=False)`，dangling link 被解析为缺失 target，随后 helper 返回 `True`。这一交互在第四轮调用顺序里已经存在，不是 `9e2205dc43` 新引入；dangling target 本身也没有可执行的 live handler，因此没有形成 R4-P1-A 式失败开放。对 EIO、可访问 symlink target、逃出 SSD root 的 symlink 仍会失败关闭或进入既有严格校验。

## 7. (d) 第一至第四轮修复抽查

| 项目 | 证据 | 结果 |
|---|---|---|
| P1-2 uninstall 事务日志 | 全套中的真实 SIGKILL mid-uninstall 测试通过；代码仍先写 uninstall journal，恢复朝 before 收敛 | 无回归 |
| `owned_handler` 精确匹配 | `shlex.split(..., comments=True)` 与精确相邻 `--bridge-id`, `BRIDGE_ID`；substring/comment/wrong-flag 测试通过 | 无回归 |
| 5+2 stat 保护 | 全文真正 `.stat()` 只剩 `resolve_ssd_path()` 受保护的设备比较和 `_mode_bits()` 受保护主体；7 个 mode 调用仍都经 `_mode_bits()` | 无回归 |
| R2-P1-A `prev_*`/`before_*` 分离 | interrupted upgrade 回到 PREV 的生产行为测试通过；本轮 diff 未改该分支 | 无回归 |
| R2-P1-B 并发锁目录创建 | fresh runtime 的真实 `main()` E2E 与 lock contention 测试均通过；逐级 `ensure_private_dir()` 仍在 | 无回归 |
| R3-P1-A 永久退役账户结转 | 本轮独立真实 ENOENT 全链通过 | 无回归 |
| R3-P2-A 旧 `prev_backup/after_backup` 清理容错 | 原测试通过；本轮五行结转/整目录删除实验通过 | 无回归 |
| P2-1 atomic-write 裸 OSError | 定向测试通过，仍包装 `InstallError` | 无回归 |
| P2-2 receipt row/top-level 裸异常 | `_validate_receipt_shape()` 仍覆盖必需字段；删除 `release_id` 的非空转测试通过 | 无回归 |
| R2-P2-A lock mkdir/open 裸 OSError | `_acquire_exclusive_lock()` 的 mkdir/open 保护未变，fresh-main 测试通过 | 无回归 |
| R2-P2-B `release_id` 裸 KeyError | `release_id` 仍在 shared shape validator 中，定向测试通过 | 无回归 |

`9e2205dc43` 相对 `d80d14c517` 没有改动 `owned_handler`、stat wrapper、prev/before 模型、锁目录创建或 receipt schema 校验主体；动态套件与上述独立边界实验没有显示间接回归。R5-P2-A 是唯一触及已有事务行为的实质新回归，已单列且未隐藏在“无回归”结论中。

## 8. 最终结论与未验证项

**GO on `9e2205dc4325b6689f4ccb8ee42c505257209973`，候选范围内未发现可复现 P0/P1。**

R4-P1-A 与 R4-P2-A 都有候选成功 + 基线失败的非空转证据；70/70 指定套件真实全绿，两个 Python 解释器均通过。不要把这个 GO 扩张为：

- 对 later commit 的结论（虽然本轮结束前目标子树与 later HEAD byte-identical）；
- 对真实机器 hooks 已安装/已激活的结论；本轮明确没有执行；
- 对嵌套不同设备挂载的运行时验证；R5-P2-B 仅做静态控制流确认；
- “没有任何新问题”；R5-P2-A/B 是真实的新 P2，建议在后续 hardening 中按上述方向修复并加回归测试。

