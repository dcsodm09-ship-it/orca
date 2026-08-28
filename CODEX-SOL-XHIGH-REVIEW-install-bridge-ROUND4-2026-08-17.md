# Codex sol/xhigh 独立复核：install_bridge 第四轮（2026-08-17）

## 结论

**NO-GO（精确候选 `d80d14c5174ae0936941b72aa6ee823b6d2fc4d6`）。**

本轮声明修复的永久退役账户、旧事务备份目录裁剪、升级中断自愈和 receipt schema 升级，在各自独立隔离复现中均通过；候选全套也确实是 68/68 通过。但 `absent` 的新卸载语义引入了一个普通临时改名即可触发的 P1：`uninstall()` 在路径缺席时跳过该行、仍删除唯一 receipt；路径稍后恢复后，handler 仍活跃且真正原始基线已失去管理入口，甚至再执行一次 install→uninstall 也只会把已安装态重新当成原始态。本候选不能安装、激活、合并或发布。

## 复核边界与候选身份

- 请求候选：`d80d14c5174ae0936941b72aa6ee823b6d2fc4d6`。
- 第三轮基线：`870810d468a4727a357448e4986d64975826334c`；`git merge-base --is-ancestor` 返回 0。
- 本轮开始时共享工作树 `HEAD=652e19cb775fd42641f3ffa66e176b5b8f9769e7`，晚于请求候选；因此未把 `HEAD` 当成候选，而是分别用 `git archive` 精确导出 `d80d14c517` 与 `870810d468` 的 `claude-codex-memory-bridge/` 子树，所有测试和动态探针都在临时隔离目录或独立临时子进程内运行。
- 共享工作树中的两个受审文件在开始、中途和报告前都与 `d80d14c517` 精确一致：
  - `claude-codex-memory-bridge/install_bridge.py`：SHA-256 `fad1831788cacffa5b8c314ac233373350dbb1b0e406d9cabbd6349f82a8a12c`
  - `claude-codex-memory-bridge/tests/test_install_bridge.py`：SHA-256 `708ec639cac00ea2bc801228bd69b95b7dd3110ea7216079b3c79f9efd82dc6f`
- `870810d468..d80d14c517` 在受审范围内仅改这两个文件：生产代码 +148/-34，测试 +165/-26。
- 工作树原本已有大量与本任务无关的已修改/未跟踪文件；未触碰它们。没有打开或依赖并行第四轮复核报告，也没有把并行路线的结论当证据。
- 启动处于降级态：收到 `ORCA_CONTEXT_NACK_V1`，原因为 central reviewed source freshness mismatch: wiki。本报告不声称加载了 Orca 中央上下文，只使用显式可见仓库文件、精确 Git 对象和本轮实时输出。
- 未对真实 `/Volumes/Extreme SSD` Codex `hooks.json` 执行 install/verify/uninstall/recover/plan；没有安装、激活、提交、暂存、合并或修改候选。唯一仓库写入是用户指定的本报告；另按全局 Orca 指令把本终端 UI 标签改为 `Codex · Install Bridge R4 Review`。

## 阻断项

### P1-R4-1：临时缺席期间卸载会删除唯一 receipt；路径恢复后 handler 永久失管，重装再卸载也无法清除

**位置**

- `claude-codex-memory-bridge/install_bridge.py:583-607`：不存在的路径被归类为 `state=absent` / `install_state=absent`。
- `claude-codex-memory-bridge/install_bridge.py:1081-1091`：`uninstall()` 把 absent 行加入 `unreachable`，随后在恢复循环中跳过。
- `claude-codex-memory-bridge/install_bridge.py:1097-1099`：即使有 absent 行，仍删除 `latest-receipt.json` 并完成 uninstall journal。
- `claude-codex-memory-bridge/install_bridge.py:744-761`：崩溃恢复的 uninstall 方向同样跳过 absent 行并清除 latest receipt / pending journal。
- `claude-codex-memory-bridge/install_bridge.py:851-862`：receipt 已被删除后，恢复出现的路径没有 previous row，下一次 install 会把当前（其实已经带 bridge）的字节和 mode 当成新的 pristine baseline。

**独立确定性复现**

隔离假 SSD 中创建主配置和两个自定义账户（探针使用 `temporary-gull` / `steady-kite`，没有复用候选测试的账户名或目录序列），保存每份 pristine byte+mode：

1. 执行真实 `install()`，确认 `temporary-gull` 的配置已有候选 owned handler。
2. 把该受管 `hooks.json` 普通 rename 到同目录临时文件。
3. 在路径仍缺席时执行真实 `uninstall()`。
4. 结果返回 `ok:true`，该路径出现在 `unreachable`，同时 `latest-receipt.json` 已删除。
5. 把文件 rename 回原路径；owned handler 仍存在。
6. 此时 `verify()` 与 `uninstall()` 都只报 `not installed: no receipt found`，没有任何动作再持有其真正 pristine baseline。
7. 再执行一次 `install()` → `uninstall()`；两步都报告成功，但恢复后的账户仍有 owned handler，且 byte+mode 精确停留在第一次的 installed state，而非真正 pristine state。

对同一场景运行精确基线 `870810d468`：缺席期间的 uninstall 会以干净 `InstallError(path unavailable)` 拒绝并保留 receipt；路径恢复后 uninstall 成功，byte+mode 精确回 pristine。故该问题由本轮 absent/skip/clear 组合新引入。

**影响**

这是无崩溃、无竞态、无特权的普通账户临时改名边界。永久退役与临时缺席仅凭“路径当前不存在”不可区分；候选在宣告 uninstall 完成时销毁了唯一恢复凭据，复现了 P1-R2-1 的最终失败形态：工具持续报告成功或“未安装”，但 bridge handler 仍在每次 Codex prompt 上执行，完整重装/卸载也无法去掉。

**必需修复方向**

不能把瞬时 absence 当作足以销毁管理状态的永久退役证明。卸载可先恢复所有当前存在的行，但只要仍有 absent 行，就必须持久保留这些行及其真正 `before_*`/backup（例如残余 receipt / tombstone / partial-uninstall 状态），并在路径重新出现时继续完成 pristine 恢复；只有受信任、显式、持久的账户生命周期身份/退役信号或等价安全确认，才可最终丢弃该行。不得在无 previous row 的情况下把仍含 owned handler 的字节重捕获为 pristine。

**缺失回归覆盖**

候选的临时缺席测试 `test_install_carries_forward_a_temporarily_undiscovered_config_without_losing_baseline`（测试文件 549-594）在 uninstall 前先恢复路径；永久退役测试（596-651）则永不恢复路径。二者没有覆盖关键交叉窗口：`install → rename-away → uninstall while absent → rename-back → verify/uninstall → reinstall/uninstall`。新测试必须断言恢复后的路径最终 byte+mode 精确回真正 pristine，且任何时刻都不会丢失可继续卸载的持久基线。

### P2-R4-2：`absent` 判定对“暂时不可达”裸抛 `PermissionError`，本轮重新引入未结构化异常

**位置**

- `claude-codex-memory-bridge/install_bridge.py:583` 与 `:1030`：`path.exists()` / `path.is_symlink()` 在 `resolve_ssd_path(..., must_exist=False)` 之后无异常保护。
- `claude-codex-memory-bridge/install_bridge.py:1224-1226`：`main()` 只捕获 `InstallError`，因此该 `PermissionError` 会形成裸 traceback。

**复现与基线对照**

隔离安装后，把一个受管账户的 `home/` 暂时 chmod 为 `0o000`，分别调用 `verify()` / `uninstall()`：候选两者都裸抛 `[Errno 13] Permission denied ... hooks.json`，异常类型为 `PermissionError`。完全相同的精确基线复现两者都返回干净的 `InstallError: path unavailable: ...`。这与刻意未处理的既有 R3-P3-B（`.shared-runtime` 自身为 `0o000`）不是同一位置；这是本轮新增的受管配置 presence probe 引入的回归。

**修复与覆盖**

用受保护的 `lstat`/等价探测只把 `ENOENT` / `ENOTDIR` 归为 absent；权限、I/O、循环等其它 `OSError` 必须包装为 `InstallError`，不能当 absent，也不能裸抛。新增受管路径父目录暂时不可达的 verify/uninstall/recover 覆盖，并经 `main()` 断言结构化 JSON 错误、无 traceback。

## (a) R3-P1-A / P1-R3-1 正向验证

在另一套隔离假 SSD 中使用自定义账户 `retired-nebula`、`steady-comet`：首次 install 后永久删除 `retired-nebula/hooks.json`，逐一执行 recover、plan、verify、install，再新增 `new-pulsar` 并再次 install、verify、uninstall。

- `recover` 返回 `state:none`；`plan.ok=true`。
- verify 返回 `ok:true`，退役路径准确进入 `unreachable`，仍存在的主配置与 `steady-comet` 进入 `configs`。
- install 原样结转退役行；新增 `new-pulsar` 被 receipt 收录且 verify 检查通过。
- uninstall 返回 `ok:true` 并上报一个 unreachable；所有仍存在配置均 byte+mode 精确恢复各自真正原始态。

永久退役主场景本身确实修复；但 P1-R4-1 证明候选把“永久”和“暂时”合并为同一不可逆卸载语义，因此总体仍 NO-GO。

另用 `orbit-lynx` / `stable-ibis` 独立验证 P1-R2-1 原始临时改名路径：install 后临时移走一行、在缺席期间再次 install、再恢复路径，verify 通过，随后 uninstall；所有配置 byte+mode 精确回真正 pristine。这个“恢复后再卸载”的顺序通过，不能覆盖“缺席期间先卸载”的阻断顺序。

## (b) R3-P2-A 正向验证

没有复用候选的两事务序列：独立执行三次不同 source release 的 install；确认第三份 receipt 的所有 `prev_backup` 都指向第二事务目录，然后删除第二事务的整个备份目录。真实 `uninstall()` 仍返回 `ok:true`，所有存在配置 byte+mode 精确回首次安装前的原始态。说明 `_receipt_rows()` 不再为 uninstall 无谓加载 `prev_backup`/`after_backup` 的目标修复有效。

## (c) 升级中断与同步自愈

使用三份配置和路径+阶段定位注入：先完整安装 v1，再修改 source 形成 v2；在 pending journal 已落盘后，让第二份 live config 的精确 `atomic_write(path)` 失败。失败回调内部同时观测到：

- pending journal 已存在；
- 第一份 live config 已是 v2 command；
- 第二份目标和第三份后续配置仍是 v1 command；
- 因而磁盘确实处于一新两旧的可观测混合态。

`install()` 返回错误 `install failed; prior hook configs were restored` 时，三份配置已全部 byte+mode 精确回到该事务开始前的 v1 installed state，pending 已同步清除；没有额外手调 recover。随后 verify、plan、uninstall 全部成功，最终所有配置 byte+mode 精确回真正 pristine。生产路径修复有效。

候选重写测试也不是另一种固定计数空转：测试文件 494-520 按 live path 注入，并显式断言 pending durable、第一份配置 command 已不同于 v1；530-535 再断言两份配置同步回 v1。把候选测试套到 `870810d468` 生产代码上时该测试仍通过，这是预期结果——生产自愈在该基线已正确，第三轮问题是测试注入点本身；本轮测试现在确实观测到了所声称的混合窗口。本轮独立三配置探针又额外断言目标和后续配置在回调内仍为 v1。

## (d) P2-R3-COMPAT

在隔离环境中先加载精确 `870810d468` 生产模块并运行真实 `install()`，生成 schema `orca.claude-native-memory-bridge-receipt.v1` 的真实 receipt；保持同一磁盘状态，切换到 `d80d14c517` 生产模块：

- `verify()` → `InstallError: invalid receipt`
- `install()` → `InstallError: invalid receipt`
- `uninstall()` → `InstallError: invalid receipt`

三者均在顶层 schema 检查清晰拒绝，没有深入到 config-row 错误；v2 升级目标达成。本轮不要求迁移 v1 receipt，且没有对真实目标机作任何动作。

## (e) 测试套件与非空转检查

精确 archive 候选中执行指定命令：

```text
cd claude-codex-memory-bridge
/usr/bin/python3 -m unittest discover -s tests -v
```

结果：**Ran 68 tests in 0.508s — OK（68/68）**。AST 计数与运行一致：`test_install_bridge.py` 29 个测试方法，`test_claude_memory_hook.py` 39 个，总计 68。

把第四轮候选的四个关键测试原样加载到精确 `870810d468` 生产模块：

- 临时缺席结转测试：旧生产代码报“previously-managed ... no longer discoverable”——非空转。
- 永久退役测试：旧生产代码报 `path unavailable`——非空转。
- 裁剪旧事务目录测试：旧生产代码报被删 `...-after.json` 的 `path unavailable`——非空转。
- 升级中断重写测试：旧生产代码通过——符合预期，因为它验证的是已存在生产修复上的注入/断言质量；本轮源码断言和独立混合态探针共同证明它不再空转。

全绿不包含 P1-R4-1 的交叉顺序，也不包含 P2-R4-2 的暂时不可达路径。

## (f) 第一、二、三轮保护抽查

额外定向运行 8 项：

1. 真实 SIGKILL 中断 uninstall 后恢复（P1-2）；
2. `owned_handler` 拒绝 bridge id 子串/注释误匹配；
3. `_mode_bits` 把 stat 失败包装为 `InstallError`；
4. prev/before 分离的升级中断恢复（R2-P1-A）；
5. 首次缺少 `.shared-runtime` 时的真实 `main install`（R2-P1-B）；
6. 并发锁互斥；
7. 损坏 receipt row 不裸抛；
8. `atomic_write` 的 OSError 包装。

结果 **8/8 OK**。静态抽查确认生产文件直接 `.stat()` 仅剩 `resolve_ssd_path()` 的受保护 try 和 `_mode_bits()` 的受保护 try；旧保护没有被删除。必须同时指出：P2-R4-2 在新 `exists()/is_symlink()` presence probe 上重新引入了同一类裸异常，所以不能笼统声称“所有裸异常边界无回归”。

## (g) absent / unreachable 调用路径一致性审计

- install 对未发现旧行的 verbatim carry-forward、verify 的 unreachable 报告、install recovery 的 committed/rollback 放行和永久退役 uninstall 主路径彼此一致，任务列出的正向场景通过。
- recover 的 uninstall 方向与普通 uninstall 都把 absent 当作无操作并最终清 receipt；这正是 P1-R4-1 的共同根因，不只是一个返回字段遗漏。
- `unreachable` 实际由“当前 existence probe 未找到”得出，并不构成“账户已永久退役”的所有权/生命周期证明；候选把暂时 rename 与永久删除压成同一不可逆状态。
- presence probe 没有区分真正 `ENOENT` 与权限不可达，产生 P2-R4-2。
- 没有发现其它可复现 P0/P1；上述一个确定性 P1 已足以阻断候选。

## 最终判定

**NO-GO for `d80d14c5174ae0936941b72aa6ee823b6d2fc4d6`.**

进入下一轮前至少必须：修复 P1-R4-1，增加缺席期间卸载后恢复路径的 byte+mode 精确回滚回归；修复 P2-R4-2 的受保护 presence probe；在新的精确候选上重跑隔离复现、定向回归和完整 68+ 测试，并重新接受独立双复核。当前候选不得安装、激活、合并或发布。
