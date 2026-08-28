# M7 候选 round-4 修复报告(2026-08-23)

补充说明,不覆盖 round-1/2/3 报告。

## 重要的流程事故(先如实记录,再说代码)

Grok 对 round-2 候选(sha256 `7bc61bf3...96f5`)的独立复核**跑到一半时**,我(未意识到它正在复核这份文件)对同一个文件做了 round-3 的代码修复,导致磁盘上的文件在复核过程中从 `7bc61bf3...` 变成了 `80e78d94...`。Grok 正确地把这个当作复核的**阻断项**,给出 **NO-GO**——理由是"提示里点名的精确候选已经不在磁盘上了,对被换掉的目标做'能不能进下一步'的判断等于没审",而不是代码本身有阻断性问题。

**这是我的流程失误,不是 Grok 的误判**:一份候选文件只要正被外部独立复核,在那个复核结束前就不能再编辑它——这条纪律之前只在"暂存目录本身的位置"上强调过(不能动 `orca-context-bridge/`),现在必须扩展到"复核进行中的具体文件"本身。已经记入独立记忆,后续任何复核都要先确认没有其它复核正在读同一份文件再动手改。

副作用:复核中途新增的两个测试用符号链接(`query_catalog.py`/`build_cross_project_catalog.py`)让 Grok 的一次 in-process 测试意外触发了真实的 `spawn_rebuild()`,把现网 `manifests/cross-project-catalog/catalog.json` 重写了一次。已核对:重写后的文件结构完整、`generated_at`/`content_fingerprint` 与之前已知正确的构建一致,判断是"内容未变、增量判断后没有实质重写"的空转,没有造成数据损坏。这不在 `AUTHORITY_TRACKED_PATHS` 范围内,现网钩子全程仍是 `ORCA_CONTEXT_DELIVERY_V1`。

## Grok 复核挑出的真实代码发现(基于它中途看到的、后来变成 round-3 状态的文件)

尽管候选身份被换导致 NO-GO,Grok 仍然独立验证并列出了几个真实、具体的发现,已逐条核实并修复:

1. **P2(残余)**:`_arm_backstop()` 在 `cmd_hook` 的 `try` **外面**调用,且 except 子句仍是收窄的 `(ValueError, OSError, AttributeError)`——如果一个"待处理的" SIGALRM 恰好打在它自己的两条系统调用上,`_HookDeadline` 会直接飞出 `cmd_hook`(脚本路径靠 `__main__` 兜住,但直接 import 调用 `cmd_hook()` 的场景没有这层保护)。`_disarm_backstop()` 的顺序是先 `setitimer(0)` 再 `SIG_DFL`,异常被吞掉后不保证真的完成了重置。
2. **P3(测试覆盖缺口)**:round-2 的"stdin 卡住不丢第一行"修复完全没有自动化回归测试——套件里唯一涉及 stdin 的 helper(`run_subprocess`)用 `subprocess.run(..., input=...)`,该调用方式**总会**关闭 stdin,不管 `input` 是不是空字符串,和"harness 一直不关闭 stdin"完全是两回事(Grok 特别指出 `Popen.communicate()` 同理,也总会关闭 stdin,不能用来测这个场景)。这正是 round 1 事故"测错轴"这个盲区的同类重演,只是严重程度低一档。
3. **P4**:`test_zero_reminders_fire_against_the_real_catalog_today` 的 docstring 仍写"12 of 17"(SKILL 草稿早改成了 11/17,这条测试 docstring 漏改)。
4. **P4**:`test_the_alarm_backstop_produces_the_empty_envelope` 对 `ALARM_SECONDS` 的 `mock.patch.object` 实际不生效——`_arm_backstop(seconds: float = ALARM_SECONDS)` 的默认值在函数**定义时**就绑定好了,之后 patch 模块级属性改不到已经生成的默认值;测试仍能通过,只是因为真实的 0.75s 默认闹钟本身也能在断言的 2s 窗口内打断 5s 的 sleep,掩盖了 patch 无效这件事。

## 本轮修复

- `_arm_backstop(seconds: "float | None" = None)`:改成调用时才读取模块级 `ALARM_SECONDS`(`if seconds is None: seconds = ALARM_SECONDS`),测试 patch 现在真正生效(已用真实闹钟计时验证:patch 到 0.15s 后闹钟确实在 0.159s 触发,不再是真实默认的 0.75s)。except 子句也放宽到 `BaseException`。
- `_disarm_backstop()`:顺序改成 `SIG_IGN` → `setitimer(0)` → `SIG_DFL`(原来是 `setitimer(0)` → `SIG_DFL`)。先切到 `SIG_IGN` 能让"重置过程本身执行期间"落下的任何信号被静默丢弃,而不是可能再次触发 `_alarm_handler` 并在异常被吞掉后跳过后面的 `SIG_DFL` 重置。
- `cmd_hook()`:把 `armed = _arm_backstop()` 从 `try` 外面挪到 `try` 里面第一行(`armed` 预先初始化为 `False`),这样"待处理信号恰好打在 arm 的系统调用上"这种情况现在也会被同一个 `except BaseException: text = ""` 兜住,不会直接飞出函数。
- 测试文件:改正"12 of 17"→"11 of 17"的 docstring;新增 `test_a_harness_that_never_closes_stdin_still_gets_line_one`,用真正不关闭的 `subprocess.Popen` 管道(不用 `communicate()`,因为它无论如何都会关闭 stdin)复现 round-2 那个原始场景,断言 exit 0、0 字节 stderr、第一行完整、且真实耗时 > 0.5s(证明确实走了 deadline/alarm 路径,不是碰巧提前返回)。

## 验证

- `python3 -m py_compile catalog_session_hint.py test_catalog_session_hint.py`:通过。
- 完整测试套件:**136 passed(新增 1 条),101 subtests passed,0 failed,0 skipped**。
- 手工验证 `ALARM_SECONDS` patch 现在真正生效(见上)。
- 现网 SessionStart 钩子:仍 `ORCA_CONTEXT_DELIVERY_V1`。
- 受保护文件(`build_startup_bundle.py`、`startup_context.py`):哈希逐字节不变。

## 新哈希(本轮冻结,后续复核针对这一对文件,复核期间不再编辑)

```
5db566b83b254b7dda80bd5f7fbe1ef1519203793dd613dab59a762f670dd600  scripts/catalog_session_hint.py
a521cda6d9e7a0952e3ca4713cdd91ab73e47a4769ea2a7f63ceb9d7ce24be5f  scripts/test_catalog_session_hint.py
```

## 后续

- 派发 Codex 对这一对精确哈希做独立只读复核(它此前对 round-2 候选的复核因一过性 `storage gate blocked` 未拿到有效结论,需要重新派发)。**派发前会先确认没有其它复核正在读同一份文件**,派发后在结果回来之前不再编辑这两个文件——这是本轮事故换来的新纪律。
- 是否需要再派一路 Grok 对这个新哈希复核,视 Codex 结果而定;不会同时对同一份文件并行派两路复核(避免重演本轮的换靶事故)。
- 三路(或视情况精简后的路数)对同一个精确哈希拿到干净结论(0 P0/P1)后,才进入:挪回 `orca-context-bridge/scripts/` → 提交 → 重签 manifest → 端到端复验 → CLAUDE.md 规则 4 的 Codex sol+max `[强制双复核]` 终审 → 才可考虑部署。
