# M7 候选 round-3 修复报告(2026-08-23)

补充说明,不覆盖 round-1/round-2 报告(它们各自保留自己当时的错误说法作为历史记录)。

## 触发原因

Claude opus + max 对 round-2 候选(sha256 `7bc61bf3c9037790cdc67c11078df37fd425f5c62bd0b8f0154471df12e496f5`)的独立只读复核结论是 **GO,0 P0 / 0 P1**,round-2 的 6 处修复全部独立复核为真实有效(含字节码异常表级别验证告警竞态修复的结构完整性、真实 subprocess 复现确认 stdin-deadline 修复对其原始目标场景有效)。但精确指出 round-2 自己的一处修复(stdin 卡住丢弃第一行)本身还留了两个缺口,均给出可复现步骤:

1. **P3-1**:`build_hook_text()` 里保护性 `try` 起始位置太晚——如果 deadline 卡在 catalog **已经成功读出之后**、`summary_line` 算出**之前**这个窗口(例如 `load_catalog` 本身耗时较长但成功返回、或 `lock_appears_free`/`spawn_rebuild` 阶段卡住),仍然会把已经可以真实算出的第一行丢弃,即使此时 `catalog`/`age_seconds`/`stale`/`spawned` 全部已知。给出三个具体计时复现。
2. **P3-2**:同一段 `try` 只捕获 `_HookDeadline`,没有捕获其它异常类型(例如某条目 `global_id` 混入孤立代理项,导致 `fit_budget` 自己的 `.encode("utf-8")` 抛 `UnicodeEncodeError`),同样会逃逸、丢弃第一行(虽然仍会安全降级为空信封/exit 0/0 字节 stderr,但白白损失了本可保留的价值)。

## 本轮修复

重构 `build_hook_text()`(唯一改动点):

- 把 `age_seconds`/`stale`/`spawn` 判断整体挪到 `catalog is None` 判断**之前**——这是恢复原有顺序保证("catalog 缺失/损坏时仍应触发后台重建"),不是新逻辑。
- 修复过程中我自己第一版重构**不小心把这条既有行为改丢了**(把 `if catalog is None: return ""` 挪到了 spawn 判断之前),被测试套件里已有的两个回归测试立刻抓住(`test_corrupt_catalog_still_spawns_a_rebuild`、`test_missing_catalog_still_spawns_a_rebuild_but_says_nothing`)——如实记录这个自产自销的错误,不掩盖。
- `lock_appears_free`/`spawn_rebuild` 单独包一层 `try/except BaseException: pass`,失败只降级为"不 spawn",不影响后续。
- `catalog` 确认非 `None` 之后,到 `summary_line` 构造完成为止,**不再有任何 `_check_deadline()` 调用**——这段全是内存里的字符串格式化,不做任何 I/O,不需要 deadline 保护,这正是 P3-1 复现的根因(慢的是 `load_catalog` 这一步本身,不是之后的纯计算)。
- 项目识别 + 第二行构造那段 `try` 的 `except` 从 `except _HookDeadline:` 放宽到 `except BaseException:`,失败时 fallback 到 `fit_budget(summary_line, [])`(只保第一行)。

## 验证

- `python3 -m py_compile catalog_session_hint.py`:通过。
- 完整测试套件(`test_catalog_session_hint.py`,同目录符号链接 `build_cross_project_catalog.py`/`query_catalog.py` 指向现网部署文件):**135 passed, 101 subtests passed, 0 failed, 0 skipped**(先跑出 2 个失败,即上面提到的自产回归,修复后清零)。
- 真实 subprocess/monkeypatch 复现 Claude 给出的原始场景,确认修复生效:
  - 场景 A(`load_catalog` 耗时 0.3s 但成功返回、deadline 只有 0.1s):修复前会丢第一行,修复后 `build_hook_text()` 返回完整正确的 `ORCA_CATALOG_V1 ...` 行。
  - 场景 B(`lock_appears_free` 在 spawn 判断阶段抛异常):第一行仍然保留,`spawned` 安全降级为 `False`。
  - 场景 C(`freshness_hits` 返回值里混入孤立代理项,触发 `fit_budget` 编码异常):第一行仍然保留,第二行被丢弃,`fit_budget(summary_line, [])` 走通。
- 现网 SessionStart 钩子重新核对:仍返回 `ORCA_CONTEXT_DELIVERY_V1`。
- 受保护文件(`build_startup_bundle.py`、`startup_context.py`)哈希核对:逐字节不变。
- `git status --porcelain=v1 -- orca-context-bridge/ wiki/`:M7 候选相关文件均未出现(候选仍在仓库根暂存目录)。
- `~/.claude/settings.json`:未出现 `catalog_session_hint`。

## 新 sha256

```
80e78d9421b4c28188cc286cac9e5d1e32b374caa6372e0b553029eda5a8eef0  scripts/catalog_session_hint.py
```
`test_catalog_session_hint.py`(`64a3d4b719b0f4630a11ae3f1c8b5c910bcf5626d732cbaaa9cb269a99340d92`)本轮未改动。

## 后续

- Grok 对 round-2 候选的独立复核仍在跑(高推理档,dispatch 于 round-2 阶段);其结论覆盖的是本轮修复**之前**的文件状态,针对 `build_hook_text()` 之外的部分仍然有效,针对这个函数本身的部分需要单独核对是否命中同一批已修复的点。
- Codex 对本轮(round-3)候选的独立复核 prompt 已写好(`codex_m7_round3_review_prompt.txt`),因 `agent_capacity.py` 的 `advisory_true_recommendation` 当前为 red,按既定原则暂缓并发派发,等 Grok 这一轮跑完释放并发余量后再派发,不是遗漏。
- 三路(Claude opus+max / Codex / Grok)对同一个 round-3 精确候选的复核全部拿到干净结论(0 P0/P1)之后,才进入:挪回 `orca-context-bridge/scripts/` → 提交 → 重签 manifest → 端到端复验 → 按 CLAUDE.md 规则 4 补一轮 Codex sol+max `[强制双复核]` 终审 → 才可考虑部署到 `~/.agents/skills/` 与(远晚于此、需另行授权)接入 `~/.claude/settings.json`。
