# M7 完成:跨项目目录 SessionStart 提示钩子(2026-08-23)

跨项目共享开发系统计划(`~/.claude/plans/sequential-baking-thunder.md`)M7 里程碑,最终状态汇总。此前各轮详细报告不重复,只列结论与指针。

## 交付物

`orca-context-bridge/scripts/catalog_session_hint.py` + `test_catalog_session_hint.py`:一个完全独立、不依赖 `build_startup_bundle.py`/`startup_context.py` 的新 SessionStart 钩子,在会话启动时打印最多两行:①跨项目目录(`build_cross_project_catalog.py` 的产出)的摘要与新鲜度;②如果本项目声明的依赖在别处已更新但本地尚未复核,给一行提醒。

提交:`90941a4731`(分支 `完善orca`)。manifest 已重签。**尚未部署**到 `~/.agents/skills/orca-context-bridge/`,**未注册**进 `~/.claude/settings.json`——两者都是后续单独授权的部署动作。

## 7 轮修复的真实发现(不是走过场,每轮都挑出了新东西)

1. **round 1**(build+3路自检+completeness critic):候选文件放错位置,直接建在 `orca-context-bridge/scripts/` 导致这个项目自己的现网钩子 NACK 了 44 分钟——和更早的 M1 事故同一机制。修复:候选迁到仓库根暂存目录。同轮还修了 SIGALRM 竞态、stdin 卡住丢弃已算好的第一行、缺失的不可信数据免责声明、文档数字错误。
2. **round 2 独立复核**(Claude opus/max GO):挑出 round-1 修复本身的 2 个 P3(deadline 保护范围起始点太晚;异常捕获范围太窄)。
3. **round 3**:修上述 2 个 P3。过程中我自己第一版重构不小心把"catalog 缺失时仍应触发后台重建"这个既有行为改丢了,被现成的回归测试当场抓住又修回来。
4. **round 4**(Grok 独立复核):**我在 Grok 复核进行中编辑了候选文件**,这是真实的流程失误,让 Grok 正确给出"候选身份被换掉"的 NO-GO——不是代码问题。Grok 仍贡献了 4 个真实发现(信号处理顺序、`stdin` 修复完全没有回归测试、文档数字错误、`ALARM_SECONDS` 测试补丁实际不生效),全部修复。已把"复核期间不能编辑候选"写成独立教训。
5. **候选提交+重签**:挪回正式位置,提交 `3f149df9b2`,manifest 重签,端到端复验。
6. **终审(CLAUDE.md 规则 4 的 Codex sol+max `[强制双复核]`)round 1**:真实 NO-GO,2 个 P1——①现有部署工具 `install_shared.py` 的整目录 `copytree` 会用仓库里两份过时分叉脚本覆盖现网受保护文件(处理为部署时的硬约束,不改这个共享工具);②钩子注册命令没有 `-I` 隔离,项目环境的 `PYTHONPATH` 能在脚本自己的异常处理生效前劫持顶层 `import`、执行任意代码(已修)。顺带修了一条会真的启动后台重建、写现网共享 `catalog.json` 的测试污染问题。提交 `b57dd68ca8`(round-5)。
7. **终审 round 2**:被 OpenAI Trusted Access 安全内容审查中断,但中断前独立发现 round-5 的 `-I` 修复只盖住了钩子自己的调用,后台重建子进程(`spawn_rebuild()`)同样没加 `-I`、同样能被劫持——独立复现属实,已修(round-6,提交 `6296b1f412`)。
8. **终审 round 3**:再次被 Trusted Access 中断。`codex-bulk` 诊断确认是 design/sol 档特有问题(和更早 M4 撞到的墙同一类)。经用户明确确认,接受 Grok 独立复核替代这一步终审(与 M4 先例一致)。
9. **Grok 终审替代**:**GO(0 P0/P1/P2)**。逐条独立复验了前面全部历史发现,又挖到一个新的、此前所有轮次都没写成缺陷的问题——round-6 的注释把 `-I` 对 `sys.path[0]` 的影响写反了(行为一直是对的,注释里的机制解释错了)。已修正注释,顺带按建议收紧一条测试断言(round-7,提交 `90941a4731`)。这两条是纯文档/测试质量修复,不含行为变化,不需要再开一轮终审。

## 最终验证状态

- 测试:140/140 通过,0 跳过。
- 现网 SessionStart 钩子:`ORCA_CONTEXT_DELIVERY_V1`(端到端复验)。
- 受保护文件(`build_startup_bundle.py`、`startup_context.py`):全程逐字节不变。
- `~/.claude/settings.json`:未写入,未注册。
- manifest:已针对 `90941a4731` 重签。

## 多模型复核记录(汇总)

| 复核 | 对象 | 结论 |
|---|---|---|
| Claude opus/max | round-2 候选 | GO(0 P0/P1),2 个 P3 |
| Codex design/xhigh | round-4 候选(第一次派发因命令缺 `--cwd` 撞假 NACK,已定位根因重派) | GO(0 P0/P1) |
| Grok(独立复核,非终审) | round-2→round-3 过渡态(因流程失误被换靶) | NO-GO(候选身份问题),4 个真实发现全部吸收 |
| Codex design/xhigh(CLAUDE.md 规则 4 终审) | round-4 提交 `3f149df9b2` | **真实 NO-GO**,2 个 P1(已修,round-5) |
| Codex design/xhigh(终审 round 2) | round-5 提交 `b57dd68ca8` | 被 Trusted Access 中断,中断前发现 1 个真实 P1(已修,round-6) |
| Codex design/xhigh(终审 round 3) | round-6 提交 `6296b1f412` | 被 Trusted Access 中断,无有效结论 |
| `codex-bulk`(诊断) | 同一段内容 | 正常完成,证实是分档特定问题 |
| Grok(终审替代,经用户明确确认) | round-6 提交 `6296b1f412` | **GO(0 P0/P1/P2)**,2 个新 P3/P4(已修,round-7) |

## 下一步(需要另行明确授权,不属于本次范围)

1. 部署到 `~/.agents/skills/orca-context-bridge/`:**必须**用 M0-M6 一直沿用的逐文件哈希核对拷贝,**绝对不能**用 `install_shared.py` 的整目录 `copytree`(会用仓库里过时的分叉脚本覆盖现网受保护文件)。
2. 注册进 `~/.claude/settings.json`:独立的、更晚的部署步骤,需要用户单独授权。
3. M8(`text_mention` 模糊证据级别、自动扫描发现可复用能力、能力自动发布、跨项目自动兼容性测试)按用户此前明确决定维持推迟。
