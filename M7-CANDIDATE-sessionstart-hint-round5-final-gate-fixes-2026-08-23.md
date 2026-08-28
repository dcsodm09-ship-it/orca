# M7 候选 round-5:终审 NO-GO 的修复(2026-08-23)

补充说明,不覆盖此前各轮报告。

## 终审结果

针对提交 `3f149df9b2` 派出的 Codex gpt-5.6-sol(design 档,池上限 xhigh)`[强制双复核]` 终审,结论是 **NO-GO**:0 P0、**2 P1**、1 P2、2 P3、2 P4。这不是像上一次那样的假阳性(那次是我给的命令缺 `--cwd` 导致的环境误报)——这次每一条都给了明确复现步骤,我逐条独立复现验证,全部真实。

## 逐条核实与处理

### P1-1:`install_shared.py` 的整目录部署会覆盖受保护脚本、使现网 NACK

**核实**:`install_shared.py` 用 `shutil.copytree()` 整个复制 `orca-context-bridge/`,只忽略 `__pycache__`/`*.pyc`,不区分 git 跟踪状态。仓库里未跟踪的 `build_startup_bundle.py`(哈希 `07b50b53...`)、`startup_context.py`(哈希 `cc6148d8...`)和现网已部署、受保护的版本(`50721e76...`、`2095d1de...`)完全不同——这是本会话早前 M7/M8 三模型规划阶段就已经发现并记录的既有分叉(工作区那两份是过时草稿,从未部署),但 Codex 这次把它和"一旦用这个工具部署 M7 会发生什么"具体联系了起来,精确复现了替换后现网变成 `installed startup generator digest mismatch` 的 NACK。

**处理**:这不是 `catalog_session_hint.py` 自己代码的问题,是另一个既有、共享部署工具的问题——修 `install_shared.py` 本身超出 M7 这次终审的范围(触碰一个未经本轮复核的共享工具,风险更大)。改为**流程约束**:M7 将来部署到 `~/.agents/skills/orca-context-bridge/` 时,**绝对不能用 `install_shared.py` 的整目录 `copytree`**,必须沿用 M0-M6 一直在用的、逐文件 `cp -p` + 哈希核对的精确部署方式。已写入 commit message 和本报告,作为部署前的强制检查项。

### P1-2:全局钩子命令未隔离环境,可被 `PYTHONPATH` 劫持

**核实**:`registration_entry()` 生成的命令是 `/usr/bin/python3 <script> hook`,没有 `-I`/`-E`。实测复现:

```bash
tmp=$(mktemp -d)
printf 'raise RuntimeError("PYTHONPATH_HIJACK")\n' > "$tmp/argparse.py"
PYTHONPATH="$tmp" /usr/bin/python3 catalog_session_hint.py hook --no-spawn --catalog /nonexistent
# Traceback ... RuntimeError: PYTHONPATH_HIJACK
```

这个钩子的核心设计承诺是"沉默是唯一的失败模式"(missing/corrupt/deadline 都不产生 traceback),但一个项目环境里设置的 `PYTHONPATH` 只要有同名模块(如 `argparse.py`),就能在这个脚本自己的异常处理建立**之前**执行任意代码——对一个"每个项目默认生效"的全局钩子,这是不可接受的本地代码执行边界。

**处理**:`registration_entry()` 生成的命令加上 `-I`(隔离模式,隐含 `-E`/`-s`),同时顺手修了同一行里另一个已知的既有 P4(`args.python` 之前没有 `shlex.quote()`)。用同一个复现脚本验证:加 `-I` 后返回正常的空信封,不再执行注入的代码。新增 2 条回归测试(`-I` 存在性、`--python` 路径带空格时的引用)。

### P2:完整测试套件的一个测试会启动真实生产 aggregator

**核实**:`test_no_write_flag_is_ever_requested_at_runtime` 用一个"很久以前"的过期 fixture,并且 `no_spawn=False`——这意味着它真的会触发 `spawn_rebuild()`,而 `spawn_rebuild()` 调用 `subprocess.Popen(...)` 启动一个真实、独立的子进程 `build_cross_project_catalog.py build --quiet`。这个子进程**不受**测试里对当前进程 `os.open`/`builtins.open` 的 mock 影响(mock 只作用于当前进程),而且 `spawn_rebuild()` 从不传 `--catalog` 参数,所以子进程永远指向真实生产路径的 `catalog.json`,和测试自己用的 tmp fixture 路径无关。也就是说,这个测试**之前每次跑,都会真的后台重建一次现网共享的 `catalog.json`**——即便内容大概率不变,这仍然是测试污染共享生产状态的真实问题。

**处理**:给这个测试加上 `mock.patch("catalog_session_hint.subprocess.Popen")`,既保留了"决定要不要 spawn 的这段代码本身不写文件"这个测试原意,又消除了真实子进程副作用。

### P3(文档):"沉默是唯一失败模式"这段文档已经过时

自 round-3 起,"未知项目"或"catalog 读出后才撞上的 deadline"这两种情况其实会**保留第一行**,不是完全空。这段模块级文档字符串还停留在 round-2 之前的说法。已改写为准确描述"两档沉默"(catalog 读不出来才是完全空;catalog 读出来之后的任何失败都只丢第二行)。

### P3(bidi,记录但本轮不修):`_flatten_for_terminal` 漏了 U+061C/U+200E/U+200F

这个函数是本文件"约 40 行为反漂移故意和 `query_catalog.py` 保持字节级一致"的一部分,被 `test_terminal_flattening_agrees_over_a_corpus` 正式绑定;`query_catalog.py` 自己的独立副本也一样漏了这三个字符——只改一边会打破绑定,改两边又超出 M7 这次终审的范围(触碰一个 M6 已经复核通过并部署的文件)。已如实记录为**已知的、跨两个文件共享的既有限制**,不在本轮修复。Codex 自己也判定这条"不能伪造新行或 sentinel",严重度定为 P3。

### P4:`catalog_age_seconds()` 对未来时间戳的截断方向错误

**核实**:用 `int()` 截断,向零取整而不是向下取整。如果 `verified_at` 恰好比一个带微秒的 `now`领先不到 1 秒,`(now - verified).total_seconds()` 可能是 `-0.5` 这样的小数,`int(-0.5)` 得到 `0`(不是负数),会被误判为"刚刚校验过、完全新鲜",而不是按文档承诺的"任何未来时间戳都算 stale"正确处理。

**处理**:改用 `math.floor()`。新增回归测试(构造一个带微秒的 `now` 和恰好领先 1 整秒的 `verified_at`,断言结果保持负数、被 `is_stale()` 正确判定)。`query_catalog.py` 里有完全相同的独立实现,同样的问题——没有正式的反漂移绑定约束这两处必须一致,判断为该文件自己的既有问题,本轮不动。

### 已确认的其它 P4(`--python` 路径引用)

在修 P1-2 时顺手一起修了。

## 验证

- `python3 -m py_compile catalog_session_hint.py test_catalog_session_hint.py`:通过。
- 完整测试套件:**139 passed(新增 3 条),101 subtests passed,0 failed,0 skipped**。
- 真实复现:`-I` 修复后 PYTHONPATH 劫持被阻止(空信封,exit 0)。
- 现网钩子:重新提交+重签后恢复 `ORCA_CONTEXT_DELIVERY_V1`(连跑 2 次)。
- 受保护文件哈希不变;`~/.claude/settings.json` 未写入。

## 提交与重签

- 修复提交为 `b57dd68ca8`(commit message 原本因 shell 反引号误把 `` `now` `` 当命令替换吞掉了两个字,已用 `git commit --amend -F <文件>` 从文件读取修正,不影响代码本身)。
- manifest 已针对 `b57dd68ca8` 重签(`authority_git.head`/`project_git.head` → `b57dd68ca8c6585bed735e4751deb820f4b45ba8`)。

## 下一步

按 CLAUDE.md 规则 4,这次终审发现了真实 P1,修复后必须对新候选(`b57dd68ca8`)重新走一轮 `[强制双复核]` 终审,不能用这次的 NO-GO 结果或之前任何一轮的 GO 顶替。已重新派发。
