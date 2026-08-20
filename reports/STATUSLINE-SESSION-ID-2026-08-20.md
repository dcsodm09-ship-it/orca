# 状态栏对话标识：已实现部分 + 已确认不可行部分

用户请求（原话）："请设计完善claudecode对话底部加入对话id标识这样我要求@的时候就不用猜测了"

## 已实现（今天已生效，全部会话共享）

`~/.orca/agent-hooks/claude-statusline.sh`——Orca 给所有 Claude Code 会话注入的共享
statusLine 脚本——**改动前其实完全不往屏幕上打印任何文字**，纯粹是把 statusLine JSON
静默 POST 给 Orca 本地 hook 端点的后台上报脚本。已加入一段纯新增逻辑（改动前已备份到
`claude-statusline.sh.bak-pre-session-id-display-20260820`，diff 只有新增行，本地测试
过 4 种场景全部正常，完全没碰原有上报逻辑）：

- 有 `session_name`（会话被显式命名过）时显示 `@名字`——这才是真正能拿去 `@` 的地址。
- 没有时回退显示 `id:xxxxxxxx`（`session_id` 前 8 位，稳定不变，可用于和
  `orca orchestration`/`ListAgents` 输出对照，但**不是**可以直接拿去 `@` 的地址）。

## 已确认不可行：自动/脚本化给会话取一个可预测短名字

调查结论（对照 Claude Code 官方当前文档逐条核实）：**没有任何 hook、settings.json
字段或环境变量能在会话启动时或启动后自动设置会话名字。**

- `SessionStart` hook 只能接收信息（`session_id`/`cwd`/`permission_mode` 等）、往 stdout
  输出 `continue`/`systemMessage`/`terminalSequence`/纯文本给 Claude 看——没有任何字段能
  改会话名字，也没有机制能从 hook 里触发 `/rename`。
- `settings.json`（用户/项目/worktree 三个层级都查过）没有任何 `name`/`sessionName` 类的
  声明式字段。
- 没有 `CLAUDE_CODE_SESSION_NAME` 这类环境变量。
- 真正能设置会话名字的只有两条路，都需要人（或启动进程本身）主动触发：启动时的
  `claude -n <名字>`/`--name <名字>` 命令行参数，或者会话内手动 `/rename <名字>`。

**现实影响**：Orca 现在启动每个 Claude Code 会话时是裸 `claude --dangerously-skip-permissions`
（`ps aux` 现场确认过），没传 `-n`。要让状态栏显示的就是真正能 `@` 的名字，需要 Orca 自己
的启动逻辑加上 `-n <名字>`——这是 Orca.app 自身的编译产物，不是这个仓库能改的范围，也
落在用户全局规则"不得重建/替换 Orca"的边界内，本次没有触碰。真正解决需要：(a) 用户/
Orca 维护方给 Orca 的启动逻辑加 `-n`，或 (b) 通过 Claude Code 的 `/feedback` 反馈机制向
Anthropic 提需求（给 `SessionStart` 增加一个能设置名字的输出字段，或给 `settings.json`
加声明式 `name` 字段）——两条都不是这次能直接落地的范围，如实记录在此，不擅自去改
Orca 启动逻辑或以其他方式绕过。

## 现状小结

- 今天起，每个会话状态栏底部都会显示一个稳定、准确的短标识（`id:xxxxxxxx`），可以直接
  拿来和 `orca orchestration check`/`ListAgents` 的输出核对，不用再猜。
- 但这**不等于**解决了"直接拿去 `@`"——只要 Orca 不在启动时传 `-n`，真正的 `@` 地址仍然
  是那个不落在任何可读输出里的自动生成默认名字，仍然需要 `@` 输入时的自动补全或
  `ListAgents`/`/list-agents` 去查。
