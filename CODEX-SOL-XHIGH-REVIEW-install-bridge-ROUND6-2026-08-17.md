# `install_bridge.py` 第六轮独立只读复核

## 结论

**GO（仅限精确候选 `f8abefc9f0445a69d2e21f23b1db86ff0e7194be`）。**

本轮没有发现可复现的 P0/P1。R5-P1-A 与 R5-P2-A 均被独立复现为已修复；第一至第五轮抽查项未见回归。下文记录两个非阻断问题：一个残余 TOCTOU 边界（P2），以及测试/前置条件仍偏局部修补、没有系统化编码（P2）。

## 独立性、启动降级与只读边界

- 本会话收到 `ORCA_CONTEXT_NACK_V1`，原因是中央 reviewed source 的 wiki freshness mismatch。因此本报告只使用当前可见仓库文件、精确 commit 对象和实时只读命令；**不声称已加载 Orca 中央上下文**。
- 没有打开并行第六轮复核，也没有打开第五轮的两份既有报告正文；已知历史 finding 仅取自 Dispatch 任务说明。
- 没有对真实 `/Volumes/Extreme SSD/Orca/local-homes/**/hooks.json` 执行 install/uninstall/recover，没有安装、激活、提交、合并或修改候选。所有行为复现均在 `tempfile.TemporaryDirectory()` 或 `mktemp -d` 隔离目录中完成。
- 仓库开始时已有大量与本任务无关的修改/未跟踪文件；目标两个文件当时没有本地修改。除任务要求的本报告外，本轮没有修改候选文件。

## 精确候选身份

- 基线：`9e2205dc4325b6689f4ccb8ee42c505257209973`
- 候选：`f8abefc9f0445a69d2e21f23b1db86ff0e7194be`
- 候选 tree：`e9fafd732775b872cb026826ccdf49bf6472be07`
- `install_bridge.py` blob：`b0405dd1494f37972e649a56f88534284263746a`
- `tests/test_install_bridge.py` blob：`ae762e8950be51e7ad0d45de2cbde5e90a4f4150`
- `git diff 9e2205dc43 f8abefc9f0 -- claude-codex-memory-bridge/`：只改上述两个文件，`162 insertions(+), 2 deletions(-)`；`git diff --check` 通过。

复核开始时 `HEAD` 正是 `f8abefc9…`。复核中途外部把 `HEAD` 推进为其直接子 commit `d7c73e8b710a3161ddab66a71529c2178d403feb`（仅 backlog 文档提交）；`git diff f8abefc9 HEAD -- claude-codex-memory-bridge/` 为空，两个工作树 blob 仍与 `f8abefc9` 完全相等。为避免把后来的 HEAD 冒充候选，最终测试又从 `git archive f8abefc9…` 的隔离副本重跑。

## 差异审计

候选新增的生产逻辑只有三块：

1. `recover_pending_install()` 的 uninstall 分支在任何恢复写入前，预先加载所有 `state not in (before, both, absent)` 行的 pristine backup（`install_bridge.py:776-795`）。
2. `install()` 在任何候选写入前，逐个用 errno-aware `_path_is_absent()` 判定 carried-forward 行是否可确定（`install_bridge.py:864-889`）。
3. `uninstall()` 在写 pending journal 前，预先加载所有本事务真正会写入的 pristine backup（`install_bridge.py:1148-1183`）。

两处 preload 的集合条件与后续写循环的 skip 条件逐字一致：

- preload：`state not in ("before", "both", "absent")`
- write loop skip：`state in ("before", "both", "absent")`

因此对候选正常生成、路径唯一的 receipt，不存在“预加载时跳过、后续却访问字典”的行。字典键使用同一个已经 resolve 的 `path_obj` 的 `os.fspath()`，生产 receipt 又由去重后的 discovery 集合加互斥的 carry-forward 集合生成，没有发现键遗漏。

## (a) R5-P1-A：永久丢失当前事务 backup

### 独立复现 1：删除整个当前 backup 目录

隔离 SSD 使用了与候选测试不同的账户名 `birch-node`、`maple-node`，共三个配置。一次真实 `install()` 后：

1. 保存三个 live config 的逐字节内容和 mode、`latest-receipt.json` 的逐字节内容。
2. 永久删除当前 receipt 自己的整个 `backups/<install_id>` 目录。
3. 调用 `uninstall()`。

结果：

- `uninstall()` 以 `InstallError: cannot read .../<backup>.json` 干净拒绝。
- `pending-install.json` **不存在**。
- `latest-receipt.json` 逐字节未变。
- 三个 live config 的内容和 mode 逐项完全未变；没有任何一行回滚。
- `verify()` 成功；`plan()` 返回 `ok:true` 且 `pending_transaction:false`；`recover_pending_install()` 返回 `{"ok": true, "state": "none"}`。
- 再次 `uninstall()` 仍是同样的干净拒绝；`install()` 因永久丢失 pristine baseline 也安全拒绝；两者都不落 pending journal、不改变 live config，之后 `verify()` 仍成功。

这里“工具仍可用”的正确含义是不会被 pending journal 永久锁死，并非在真实 pristine backup 已永久丢失后假装能够安全 uninstall/reinstall。需要该 backup 的动作必须继续 fail closed；不需要它的 verify/plan/recover 正常运行。

### 独立复现 2：只删除最后一个 live 行的 backup

另一个四配置场景只删除 receipt 最后一行的 `backup`，保留前三行的 backup 可读。这比候选新增测试“删除整个目录”更强地覆盖了 preload 的排序语义：旧惰性循环本会先回滚前三行再在最后一行失败。

候选结果是最后一行 load 失败前没有写 pending journal，且前三行和最后一行的内容/mode 全部逐字节不变。由此确认 preload 的确覆盖了后续会写入的所有行，而不只是碰巧在第一行失败。

### `recover_pending_install()` 的对应 preload

手工构造一个已经存在的合法 uninstall journal，删除最后一个 live 行的 backup：

- `recover_pending_install()` 在任何 config 写入前失败；所有 live config 逐字节/mode 未变，pending journal 保留。
- 把同一 backup 的已保存字节恢复后再次 recover，完整 uninstall 成功，pending 和 latest receipt 均清除，所有配置恢复到各自不同的 pristine 内容。

这证明 recovery 分支新增字典也覆盖了全部后续写行。它不能消除“journal 已经存在后 backup 又永久丢失”的根本不可恢复性，但不会再制造额外的半回滚状态。

**判定：R5-P1-A 已修复。**

## (b) R5-P2-A：结转行在写前为 indeterminate；ENOENT 不误伤

### indeterminate 前置拒绝

隔离 SSD 使用账户 `frost-node`、`harbor-node`。首次 install 后，把 `frost-node/home` 改为 `000`，并让 discovery 返回主配置和 `harbor-node`，从而把真实仍存在但不可判定的 `frost-node` 行置入 carry-forward 集合。

失败前保存了：

- 所有可读 live config 的逐字节内容和 mode；
- `latest-receipt.json` 的逐字节内容；
- 整个 runtime 目录的相对路径、类型、mode、SHA-256 清单。

候选 `install()` 在 `install_bridge.py:888-889` 抛出 `cannot determine whether ... exists`。失败后：

- 无 pending journal；
- runtime 清单完全不变，没有新 backup/release/receipt；
- latest receipt 逐字节不变；
- 其它可发现配置逐字节/mode 不变。

恢复目录权限后，被结转配置也逐字节/mode 不变，`verify()` 和 `uninstall()` 均正常完成。

### 真正 ENOENT

另一个隔离场景用 `iris-node`、`juniper-node`。首次 install 后永久删除 `iris-node` 的 hooks、home 和账户目录，再执行 install/verify/uninstall：

- 新 receipt 正确保留 `iris-node` 原行；
- verify 在 `unreachable` 中报告该路径；
- uninstall 成功恢复其余配置；
- 被删除账户没有被重新创建；latest receipt 和 pending journal 正常消失。

**判定：R5-P2-A 已修复；R3-P1-A 的永久退役路径没有回归。**

## (c) 测试套件与新增测试非空转性

从精确 `git archive f8abefc9…` 候选运行任务指定命令：

```text
/usr/bin/python3 3.9.6: Ran 72 tests in 0.598s — OK
PATH python3 3.14.6:  Ran 72 tests in 0.475s — OK
```

AST 计数确认 `test_install_bridge.py` 从基线 31 个 test method 增至候选 33 个；另有 `test_claude_memory_hook.py` 39 个，总计 72。

把候选的整个 `test_install_bridge.py` 放入 `9e2205dc43` 的隔离归档、让它加载基线 `install_bridge.py`，单独运行两项新增测试：

- `test_uninstall_leaves_no_pending_journal_when_a_live_rows_backup_is_unloadable`：失败，`PENDING_PATH.exists()` 实为 `True`。
- `test_install_refuses_before_writing_anything_when_a_carried_forward_row_is_indeterminate`：失败，`PENDING_PATH.exists()` 实为 `True`。

两项测试都不是对旧代码空转。

解释器差异仍按任务说明存在于刻意未修的 R5-P3-A：对 `000` 父目录下的 `Path.is_file()`，本机 3.9.6 实测裸抛 `PermissionError`，3.14.6 实测返回 `False`。两套 72/72 一致说明候选已有的 mock 回归在两解释器上行为一致；它不等于 R5-P3-A 已修复。磁盘安全方向一致（都在任何候选写入前停止），但 3.9 的错误规范化仍是已接受的 P3 差异。

## (d) 第一至第五轮既有修复抽查

| 项目 | 静态/动态证据 | 结果 |
|---|---|---|
| P1-2 uninstall 事务日志 | `uninstall()` 先写 kind=`uninstall` journal，再逐行恢复；真实 SIGKILL 测试通过；本轮又手工构造 pending journal 验证恢复 | 无回归 |
| `owned_handler` 精确匹配 | `shlex.split(comments=True)` 后要求精确相邻 `--bridge-id`/ID；substring、comment、错误 flag 测试通过 | 无回归 |
| 5+2 处 stat 保护 | `resolve_ssd_path()` 和 `_mode_bits()` 的 OSError→InstallError 包装未被本轮 diff 触及；权限 fail-closed 测试双解释器通过 | 无回归；`candidate.is_file()` 是已知 R5-P3-A 例外 |
| R2-P1-A `prev_*`/`before_*` 分离 | install rollback 仍按 `install_state` 与 `prev_backup` 回退；interrupted upgrade 测试通过 | 无回归 |
| R2-P1-B 并发锁目录创建 | `_acquire_exclusive_lock()` 仍逐层 `ensure_private_dir()` 并 `O_NOFOLLOW` 打开；fresh-runtime main() 与锁竞争测试通过 | 无回归 |
| R3-P1-A 永久退役账户结转 | 本轮独立 ENOENT install→verify→uninstall 全链路通过且不复活账户 | 无回归 |
| R3-P2-A/R4-P2-A backup 清理容错 | 既有两项 pruned-backup 测试通过；本轮 whole-dir/last-row 独立探针通过 | 无回归 |
| R4-P1-A 权限不可读 fail closed | 既有 verify/uninstall 测试双解释器通过；本轮 install preflight manifest 零变化探针通过 | 无回归 |

本轮源代码 diff 没有触及 `owned_handler`、stat helper、prev/before 建模或锁目录创建实现；相关结论同时由精确候选全套测试和上述边界探针支持，而不是仅凭“未改代码”推断。

## (e) 新问题与系统性审计

### R6-P2-A（非阻断）：carry-forward preflight 之后仍有残余 TOCTOU

候选在 `install_bridge.py:888-889` 只获得一个时点的可判定性；到 `write_runtime()`、backup/journal/live-config/latest-receipt 写入以及最终 recover 之间没有目录身份锁或稳定句柄。

确定性注入探针：首次 install 后修改 source 形成真实 upgrade，让一个 carry-forward 账户在 preflight 时可读；在随后进入 `write_runtime()` 时才把该账户 home 改为 `000`。结果：

- `install()` 报 `install failed and the durable recovery journal remains pending`；
- 其它两个可发现配置已经升级；
- pending journal 已落盘；
- 恢复权限后 `recover_pending_install()` 返回 `committed`，journal 清除，verify 成功。

这不是本轮新增的回归：此前窗口更大，本轮把“从一开始就 indeterminate”的常见确定性输入挡在任何写入前。剩余复现要求另一个 actor 恰好在 preflight 后改变状态；状态恢复后可恢复。因此定级 P2，不阻断 GO。但它说明注释里的“before anything this install does becomes durable”只对检查时点成立，不是一个事务级稳定快照。

建议后续：把 carried-forward 行允许的 precondition 明确为 `absent` 或与 receipt 的 installed state 精确匹配，记录目录/文件身份，并在写 journal 前再次验证；若要彻底关闭窗口，需要基于稳定 fd/目录身份或让 recovery 对该变化具有明确收敛语义，而不是仅增加另一次 `lstat()`。

### R6-P2-B（非阻断）：前置条件与回归测试仍主要是局部补丁

候选实现正确，但没有完全落实第五轮提出的“把 install/uninstall 前置条件系统化”的方向：

- uninstall 与 recovery 各自复制同一 dict comprehension 和 skip predicate，没有共享的“本次 write set + 已验证 restore bytes”构造器；当前两者一致，但未来仍可能漂移。
- 新 uninstall 测试删除整个 backup 目录，所以基线在第一行 load 就失败；它能证明旧代码会落 pending（因此非空转），但不能单独证明“最后一行缺失时前面行也绝不回滚”。本轮独立 last-row 探针补上了该证据。
- 新 install 测试是在相同 release 上的 idempotent reinstall；“其它配置仍有两个 handler”本身不能证明没有 rewrite。真正使测试对基线非空转的是 pending journal 断言。它没有比较 latest receipt、backup/release 目录或完整 runtime manifest。本轮独立探针确认候选确实零写入。
- `recover_pending_install()` 新增 preload 没有对应的候选回归测试；现有 SIGKILL 测试只覆盖 backup 全部存在。本轮手工 pending + 最后一行 backup 缺失探针补上了复核证据，但尚未进入仓库测试。
- 没有 failpoint 矩阵系统地断言 install/uninstall 在每个前置条件失败点的“pending 是否存在、receipt 是否变化、live config 是否逐字节/mode 不变”。

这些是未来防回归和维护风险，不是当前候选的 P0/P1。建议后续抽出共享 restore-input collector（最好返回有序行而非 path-keyed dict），同时为 receipt 路径唯一性、最后一行 backup 缺失、recovery preload、preflight 零 runtime-diff 和 post-preflight race 增加显式断言。

## 已知非阻断项

- R5-P3-A：`discover_hook_configs()` 的 `candidate.is_file()` 解释器差异仍在；本轮未声称修复。
- backup/prev_backup/after_backup 的 device-validation 不对称、ENOTDIR 分类、R4-P2-B、R4-P3-A~E、R3-P3-A/B 均未由本轮 diff 扩大；未发现把这些已接受项升级为 P0/P1 的新证据。
- 当前 receipt backup 永久丢失后，install/uninstall 会永久**安全拒绝**，因为 pristine baseline 已不可恢复；它们不会永久挂起或留下 pending journal。把“安全拒绝”与“工具被 pending journal 卡死”区分开是本轮验收的关键。

## 最终候选门禁

- P0：0
- P1：0
- 新/残余 P2：2（post-preflight TOCTOU；系统性前置条件/测试覆盖债务）
- 精确候选测试：72/72（Python 3.9.6），72/72（Python 3.14.6）
- R5-P1-A：PASS
- R5-P2-A：PASS
- 第一至第五轮抽查：PASS

**最终：GO for `f8abefc9f0445a69d2e21f23b1db86ff0e7194be`，不外推到后续改变 `claude-codex-memory-bridge/` 的 commit，也不代表已安装或已激活。**
