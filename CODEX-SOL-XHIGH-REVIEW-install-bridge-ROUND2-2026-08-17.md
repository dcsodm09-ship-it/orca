# `install_bridge.py` Round 2 独立只读复核

- 日期：2026-08-17（Asia/Taipei）
- 复核者：Codex `gpt-5.6-sol` / xhigh dispatch worker
- 仓库：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`
- 基线：`29bb896012e05d4d6e3c21f461fc42163c73b747`
- 候选：`09459cf6a0211fde84d16ae7f040ac8d9351c973`
- 范围：`claude-codex-memory-bridge/install_bridge.py` 与 `claude-codex-memory-bridge/tests/test_install_bridge.py`
- 结论：**NO-GO**

## 执行边界与候选身份

1. `git rev-parse 09459cf6a0^{commit}` 解析为上述完整候选 hash；`29bb896012` 是其祖先。
2. 本轮按指定命令审查 `git diff 29bb896012 09459cf6a0 -- claude-codex-memory-bridge/`：2 个文件，513 insertions / 79 deletions。
3. 当前工作树 HEAD 是 `5468a5214a46c5f3c5f740881546c0592f943ab7`，不是受审 commit；但目标目录相对 `09459cf6a0` 无 diff。所有复现与测试仍从精确 Git 对象导出到临时目录后运行，不以 HEAD 身份代替候选身份。
4. 工作树审查前已有大量无关未跟踪/已修改内容，本轮未触碰。唯一写入是本报告。
5. 未在真实 `/Volumes/Extreme SSD` Codex homes/hooks 上运行 install；未安装、激活、提交、合并或修改候选。所有文件系统探针位于 `TemporaryDirectory` 中，且真实使用普通文件、权限位、`fsync`/`os.replace`、`flock` 与 OS `SIGKILL`。
6. 启动为降级态：`ORCA_CONTEXT_NACK_V1` / `central reviewed source freshness mismatch: wiki`。本报告不声称加载了中央 reviewed context，仅依赖可见项目文件、Git 对象和本轮现场输出。

## 执行摘要

| 检查 | 结果 |
|---|---|
| 候选官方套件 | **63/63 PASS**，0 failures / 0 errors，0.347s |
| 基线原套件 | **56/56 PASS**，0 failures / 0 errors，0.245s |
| 同 release `install→install→uninstall` | PASS，3 份配置逐字节+原权限位精确恢复，owned handler=0 |
| 跨 release `install→upgrade→install→uninstall` | PASS，release id 确实改变，3 份配置逐字节+原权限位精确恢复 |
| uninstall 真实 SIGKILL | PASS，4 个阶段均由全新进程完成卸载，latest/pending 均清除 |
| install 真实 SIGKILL 回归抽查 | PASS，4 个阶段均正确回滚或提交 |
| `atomic_write()` 真实 OSError | PASS，包装为 `InstallError` |
| 真实跨进程 flock 争用 | PASS，第二进程结构化失败；释放后正常成功 |
| shell comment / non-owned 假阳性 | PASS，未引号 `#` 后 ID、substring、other flag 均不被删除 |
| 临时缺席账户跨重装 | **FAIL / P1**，卸载报成功但该账户 bridge 仍活跃，且 receipt 已清除 |

## 阻断问题

### P1-R2-1：重装只继承 latest receipt，临时未被发现的账户会从可卸载集合永久丢失

位置：

- `install_bridge.py:637-642`：仅从 `latest-receipt.json` 建立 `previous_rows_by_path`，不扫描旧 receipts。
- `install_bridge.py:650-676`：当本次 discovery 的 path 不在 latest rows 里时，将当前盘面内容视为 pristine baseline。
- `install_bridge.py:700-710`：新 receipt 的 `configs` 只包含本次 discovery 结果；旧 receipt 中暂时缺席的 path 不会继承。
- `install_bridge.py:815-857`：uninstall 仅遍历当前 receipt rows，然后清除 latest receipt 并报成功。

确定性复现（无崩溃、无竞态、无特权）：

1. 在隔离 volume 中创建 main + `quartz` + `sable` 三份不同原始 hooks，权限分别为 0640 / 0600 / 0644。
2. 执行第一次真实 `install()`；三份配置均有且只有一个 owned bridge handler。
3. 将 `quartz/home/hooks.json` 重命名为同目录下的临时名，模拟该账户 hooks 在一次升级发现时暂时不存在。其他两份配置仍满足 `discover_hook_configs()` 的最小数量。
4. 改变 source script 字节，执行第二次真实 `install()`。新 latest receipt 只有 main + `sable` 两行，`quartz` 行被丢弃。
5. 将 `quartz` 的已安装 hooks 原样改回 `hooks.json`，然后执行 `uninstall()`。
6. 实际结果：`uninstall()` 返回 `ok: true`；main 和 `sable` 精确恢复；`quartz` 仍有 1 个 owned bridge handler，且字节不等于原始内容；`latest-receipt.json` 与 `pending-install.json` 均不存在。
7. 此后 `verify()` 和再次 `uninstall()` 都只返回 `InstallError: not installed: no receipt found`，工具已没有任何出口告知或清理仍在执行的 `quartz` bridge。

影响：这与第一轮 P1-1 的核心安全后果相同：工具声称卸载成功，但 bridge 继续在某账户每次 Codex prompt 上执行，并且提交信号/receipt 已被清除。这是普通文件出现/缺席序列，不需要越权或时序攻击。

回归/修复方向：必须保持单调的 per-path 原始基线清单，不能用每次 discovery 结果直接覆盖 latest 可卸载集合。最少应在新 install 时拒绝丢失旧 latest rows；更完整的方案是保留历史受管 path/baseline，在卸载时不得清除总 receipt，除非所有曾受管且现又可达的 path 都已恢复，不可达 path 也必须有明确未完成状态，不能静默遗忘。回归测试应固定 main + 2 accounts 的“install→一账户临时缺席→upgrade install→账户恢复→uninstall”。

## 非阻断问题

### P2-1：新增 lock 的准备阶段仍可将裸 `OSError` 穿透 `main()`

- 位置：`install_bridge.py:904-906` 的 `mkdir()` / `os.open()` 不在 OSError 包装中；`907-911` 只包装 `flock()`；`main()` 在 `933` 只捕获 `InstallError`。
- 复现：在隔离目录中将 `RUNTIME_BASE` 预先创建为普通文件，调用 `main()` 的 `recover` action。
- 实际：裸 `FileExistsError: [Errno 17] File exists` 逃出，无结构化 JSON，且不是 `InstallError`。
- 评级：P2。这不导致静默误安装/误卸载，但它是本轮为修复裸异常而新增的另一个裸异常边界。
- 方向：将 parent preparation 和 `os.open()` 也包装为 `InstallError`，并对现有 lock dir/file 做 owner/mode/regular/no-follow 校验。

### P2-2：`_validate_receipt_shape()` 并未完整验证顶层字段，仍可在 `verify()` 裸抛 `KeyError`

- 位置：`install_bridge.py:456-503` 未要求 `release_id`；`verify()` 在 `793-799` 直接索引 `receipt["release_id"]`。
- 复现：在隔离环境真实 install 后，仅删除 owned + 0600 `latest-receipt.json` 的 `release_id`，其他字节、backups、runtime 与 hooks 均保持有效。
- 实际：`read_receipt()` 接受该 receipt；`verify()` 执行完 runtime/config 验证后裸抛 `KeyError: 'release_id'`，不是 `InstallError`。
- 评级：P2。本轮对 row 的缺字段修复有效，但“顶层字段完整校验”的声称不成立。
- 方向：校验所有生产路径会直接索引或输出的顶层字段，至少包含非空字符串 `release_id`；对 `installed_at` / `command` 等也应明确 schema 约束。

### P3-R2-1：已安装配置的合法无关 hook 变更会被安全拒绝，但报错指引不可操作

- 位置：`install_bridge.py:665-669`。
- 复现：install 后向一份 hooks 追加一个与 bridge 无关的合法 handler，保持 0600，然后重新 install。
- 实际：修复在写 runtime/backup/pending 之前 fail closed；latest receipt、全部 configs 与 pending 均未被该拒绝改动。这对“整份文件精确恢复”模型是合理的安全选择，不是 P1。
- 但它确实会误伤正常的无关 hook 演进；错误文案要求“run verify or uninstall first”，而 verify 只会确认 drift，uninstall 也会拒绝同一 drift。操作者只能先在工具外手动恢复精确 after state。
- 评级：P3（可用性/诊断指引）。

## 对指定修复的独立验证

### (a) 第一轮 P1-1 的直接主路径

两个独立 sandbox 使用与候选测试不同的账户名和不同原始 handler 内容：

- 同 release：两次 receipt 的 release id 相同；卸载后三份配置字节与 0640/0600/0644 权限均与最初完全相同，owned handler 计数全为 0，latest/pending 不存在。
- 跨 release：修改 source script 字节后 release id 确实改变；结果同样逐字节+权限精确恢复。
- 结论：**固定 config path 集合的直接 P1-1 已修复**；但本报告 P1-R2-1 说明 path 集合变动时修复不完整。

### (b) 第一轮 P1-2 的 uninstall 事务恢复

在每个全新 sandbox 先完成真实 install，再用 child process 在真实文件写完后 `os.kill(getpid(), SIGKILL)`，父进程确认中间盘面，再 fork 另一个全新进程调用 `recover_pending_install()`：

| 实际中断点 | 恢复前 | 新进程恢复后 |
|---|---|---|
| pending journal 已 durable，未恢复任何 config | owned=[1,1], latest=yes, pending=yes | `state=uninstalled`，两文件字节/权限精确原始，latest=no, pending=no |
| 首份 config 已恢复，第二份未恢复 | owned=[0,1], latest=yes, pending=yes | 同上（明确覆盖要求的混合态） |
| 两份 config 均已恢复，latest 未清 | owned=[0,0], latest=yes, pending=yes | 同上 |
| latest 已清，pending 未清 | owned=[0,0], latest=no, pending=yes | 同上 |

四个 child 都以 signal 9 死亡，不是 Python 模拟异常。**P1-2 修复本身通过**。

### (c) P2/P3 修复与假阳性

- `atomic_write`：用“普通文件当 parent directory”稳定触发真实 `FileExistsError`，结果为 `InstallError: cannot prepare write ...`，通过。
- receipt row：官方“删 path”回归通过；独立探针发现顶层 `release_id` 缺口，见 P2-2。
- flock：父进程持有 lock 时，全新 subprocess 运行 `main(recover)` 退出码 1，stdout 为 `{"ok": false, "error": "another install_bridge.py invocation is already running"}`，无 stderr；释放后新 subprocess 退出码 0 且 `state=none`，通过。lock 准备边界另有 P2-1。
- uninstall 后 latest：正常卸载和所有 SIGKILL 恢复路径都清除 latest；之后 verify/uninstall 清楚报 `not installed`，通过。
- `owned_handler`：真实生成形态为 true；未引号 `# --bridge-id ID`、ID substring、ID 作为其他 flag 值、不配对引号均为 false。包含这些形态的四个合法 non-owned handlers 经 `update_hook_config()` 全部保留，仅新增一个 owned handler，未发现新假阳性。引号内 `'#'` 后的精确 flag/value 仍为 true，符合 shell 实际 argv 语义。

### (d) 测试套件与非空转对照

精确候选导出目录中执行：

```text
cd claude-codex-memory-bridge
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -v

Ran 63 tests in 0.347s
OK
```

另将候选中两个 P1-1 关键测试原样放到精确旧基线导出目录，让它们 import 旧 `install_bridge.py`：

- `test_reinstall_does_not_poison_the_uninstall_baseline`：**FAIL**，卸载后 main config 仍包含 bridge handler。
- `test_upgrade_reinstall_does_not_poison_the_uninstall_baseline`：**FAIL**，同样未恢复 pristine main config。

两测试在候选均 PASS、在基线均 FAIL，因此不是空转测试。旧基线原生 56 项另行仍 56/56 PASS。

### (e) 第一轮其余修复回归

- stat 保护：`resolve_ssd_path()` 的 device stat 在 `122-125` 包装 OSError；所有目标 mode stat 均收敛到 `131-140` 的 `_mode_bits()`。候选生产文件的 `.stat()` 只剩这两个 guarded 实现，调用点均经 wrapper；missing path 现场探针得到 `InstallError`而非裸 OSError。无回归。
- 精确 token：前述 ownership matrix 与 non-owned 保留探针通过。无回归。
- install 事务真实 SIGKILL 四点：在 atomic write #6（pending 后）、#7（首 config 后）、#8（两 config 后）、#9（latest 后）杀死 child。新进程恢复分别得到 `rolled_back, rolled_back, rolled_back, committed`；前三者字节+原 0640/0644 权限精确恢复且 owned=0，最后一者两文件完整安装且 owned=1。所有 pending 均清除。无回归。

### (f) 刻意未处理 P3 的当前级别

| 项目 | 现场状态 | 级别判断 |
|---|---|---|
| P3-2 `--bridge-id=<ID>` | `owned_handler()` 仍返回 false | 仍 P3。生成器从不生成等号形态；手工等价改写会被 digest/drift 拒绝，未发现静默误卸载主路径。 |
| P3-3 handler 组粒度 | 重现：一组中同时有 owned command + unrelated sibling 时，整组被删，sibling 不保留 | 仍 P3。生产生成器创建的是单 hook 组；正常受 receipt 覆盖的重装若有外部合组修改会先在 after digest 校验失败。仍应有精确 element 级回归。 |
| P3-4 `_mode_bits()` 跟随 symlink 的 TOCTOU | 代码仍为分离的 validate/open 与后续 path stat | 仍 P3，需要微小竞态窗口，且为本轮前存量。 |
| P3-5 install 写前重查与 `atomic_write` 间窗口 | `722-726` 仍存在 | 仍 P3，需要外部竞态，进程 flock 不能阻止任意外部写者；非本轮回归。 |
| P3-6 `remove=True` 死分支 | Git grep 只命中 test，无生产调用 | 仍 P3/清理项，无当前运行后果。 |
| P3-7 `must_exist=False` 未使用 | Git grep 只命中定义的默认参数，无 false 调用 | 仍 P3/清理项，无当前运行后果。 |

结论：这 6 项是除本轮已修复 P3-1 以外的刻意未处理项，均没有单独升为 P0/P1。P3-1 的 shell-comment 修复已在上文独立验证通过。P3-3 有真实数据粒度问题，建议保留回归，但本轮的 release blocker 是上述 P1-R2-1。

## 最终结论

**NO-GO for `09459cf6a0211fde84d16ae7f040ac8d9351c973`.**

修复对“config path 集合恒定”的同 release/跨 release P1-1 主路径有效，uninstall 方向事务恢复也在四个真实 SIGKILL 边界上通过。但候选没有保持跨 discovery 变动的单调受管 path/baseline 集合：一个在重装时临时缺席、稍后恢复的账户可在卸载“成功”与 receipt 清除后继续执行 bridge。该可复现 P1 必须在新的精确候选上修复并重审；另建议同时关闭两个裸异常 P2。
