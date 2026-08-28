# M7 候选已提交 + 重签(2026-08-23)

补充说明,不覆盖 round-1/2/3/4 报告。

## 前置条件(提交前已满足)

- Claude opus/max 独立复核:GO,0 P0/P1(round-2 候选,2 个 P3 已在 round-3 修复)。
- Codex gpt-5.6-sol(design 档,池上限 xhigh)独立复核:GO,0 P0/P1(round-4 精确哈希,逐项复核了 Claude 的 2 个 P3 修复和 Grok 的 4 个发现,确认哈希在复核前后未变)。第一次派发因验证命令缺少 `--cwd` 参数撞到假 NO-GO(`storage gate blocked`,报告的 `project` 路径是 Codex 自己的沙箱 scratch 目录,不是真实项目),已定位根因、补上 `--cwd`、重新派发拿到真实结论——详见 [[reference_storage_gate_transient_blocked_2026_08_22]](已更正)。
- Grok 独立复核(round-2→round-3 过渡期,因我自己的流程失误在复核进行中编辑了候选文件)给出的 NO-GO 是基于"候选身份被换掉"这个流程问题,不是代码缺陷;它挑出的 4 个真实发现已全部修复(round-4)并被 Codex 独立重新核实。

三路复核合起来:Claude opus GO、Codex sol/xhigh GO、Grok 的真实发现全部吸收并被交叉验证——这不是 CLAUDE.md 规则 4 的正式终审(两次派发都没有加 `[强制双复核]` 标记),但已经是相当扎实的多模型交叉确认。

## 执行步骤(严格按已有的 M0-M6 机制式流程,不重新发明)

1. 把 round-4 冻结的两个文件(`catalog_session_hint.py` sha256 `5db566b8...`、`test_catalog_session_hint.py` sha256 `a521cda6...`)用 `cp -p` 拷贝进 `orca-context-bridge/scripts/`,逐字节哈希核对一致。
2. 在真实位置(不再需要符号链接,同目录已有真实的 `build_cross_project_catalog.py`/`query_catalog.py`)重新编译 + 跑测试:**136/136 通过,0 跳过**。
3. 确认此时现网钩子按预期变成 `ORCA_CONTEXT_NACK_V1`(新文件落进 `orca-context-bridge/` 触发 `AUTHORITY_TRACKED_PATHS` 指纹不匹配)——这是提交前的正常过渡窗口,不是事故,但必须尽快闭合,不能像 M1/M7 round-1 那样放置几十分钟。
4. 把 `SKILL.md.snippet.fixed.md` 的内容(标题从"(candidate — not deployed)"改成正式小节标题)拼进 `orca-context-bridge/SKILL.md`,作为"Cross-project capability catalog"下的新小节"Announcing the catalog at session start"。
5. 精确圈定 `git add` 范围为这 3 个文件(`SKILL.md`、`catalog_session_hint.py`、`test_catalog_session_hint.py`),明确排除 `orca-context-bridge/` 下另外 20 个既有的、与本次工作无关的未跟踪文件/目录(`build_startup_bundle.py`/`startup_context.py` 工作区分叉、`auto_index.py`、`install_hook.py` 等)——和 M1-M6 基线提交用的是同一条纪律。
6. 提交:`3f149df9b2`。
7. 用部署好的 `build_startup_bundle.py` 自带的 `summarize_git(project, paths=AUTHORITY_TRACKED_PATHS, untracked_all=True)` + `git_authority_state(...)`(直接 import 真实模块调用,不重新实现)重算 `authority_git`/`project_git`,写入新的 `authority_signed_at`/`signed_by`/`signature_nonce`,`pack`/`shared_source_sha256s`/`shared_source_policy` 均未改动。重签前已备份原 manifest。
8. 端到端复验:现网钩子恢复 `ORCA_CONTEXT_DELIVERY_V1`(连跑 2 次)。
9. 受保护文件(`build_startup_bundle.py`、`startup_context.py`)哈希核对逐字节不变;`~/.claude/settings.json` 未出现 `catalog_session_hint`(未注册,按计划这是单独一步,需要另行明确授权)。
10. 删除仓库根的暂存目录 `m7-sessionstart-candidate-STAGED-review-only/`(候选已经提交进真实位置,暂存副本不再需要,和 M1 候选转正后的清理方式一致)。
11. 再跑一次完整测试套件确认清理后无回归:**136/136 通过**。

## 当前状态

已提交(`3f149df9b2`)、已重签、已端到端复验、已部署到工作区仓库,但**尚未部署到 `~/.agents/skills/orca-context-bridge/`(现网技能目录)、尚未注册进 `~/.claude/settings.json`**。按计划,这两步都是后续单独的、需要明确授权的部署动作,不在本次范围内。

**下一步**:按 CLAUDE.md 规则 4,这项工作即将被判定为"全部完成、准备进入最终验收/合并/发布"——需要补一轮 Codex gpt-5.6-sol + max(池上限 xhigh)独立只读复核作为终审,prompt 第一行加 `[强制双复核]` 标记。终审对象是这次的精确提交 `3f149df9b2`。终审干净(0 P0/P1)后,才可以考虑部署到 `~/.agents/skills/` 和(远晚于此、需另行授权)注册进 `settings.json`。
