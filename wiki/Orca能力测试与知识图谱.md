# Orca 能力测试与知识图谱 Wiki

更新：2026-08-10。本页记录本机 Orca 的**能力面**测试，而非将 235 个命令逐个对真实账号、生产工作树或设备盲目执行。每个条目均标明证据级别：`已实测`、`接口已发现`、`待隔离验收` 或 `需要用户目标/授权`。

## 实测边界

本次测试只使用无凭据、只读或可逆的本机操作。检查时主机有 8 个逻辑 CPU、16 GiB 内存，Orca diagnostics 观测到 1 分钟 load 53.7；因此没有启动新的编码 worker、创建/删除工作树、建立自动化、修改账户、连接远端环境、操作真实浏览器资料或驱动模拟器。这样不会干扰用户正在运行的 agent 和生产项目。

随后又以固定 argv 对 18 个只读能力入口进行了无输出探测：本轮 16 个成功，`account list` 与 `worktree current` 失败。失败项在能力目录中明确标为 `live_read_only_failed_current`，不会把 schema 存在误写为运行成功。探测不输出账户、终端、浏览器、编排或诊断资料内容。

本机 `agent-context` 的 235 条命令 schema 已完整提取为 [Orca CLI 能力目录](orca-cli-capability-inventory.json)。目录为每一条命令标记证据级别：本轮固定无 payload 探针成功/失败、只发现 schema 的只读命令、或必须先有隔离目标/明确授权的操作。它不把“发现命令”误写成“执行成功”。

| 能力面 | 状态 | 本次证据 | 尚未执行的高风险部分 |
| --- | --- | --- | --- |
| Runtime 与命令发现 | 部分已实测 | `orca status --json` 返回 app/runtime/graph ready；`orca agent-context --json` 返回 235 个命令 schema；`orca_readonly_probe.py` 本轮 16/18 通过，失败为账户列表和当前工作树；全量命令逐条记录在能力目录。 | 失败的两个只读入口需在 SSD 接管后重试；`open`/`serve` 不在现有 runtime 上重复启动。 |
| 容量预检 | 已实测 | `agent_capacity.py` 汇总 logical CPU、load、memory pressure 与 Orca 聚合计数，只输出建议、不操作 agent。 | 阈值是启动门槛，不会自动解决同一路径或同一业务资源的写冲突。 |
| Skills | 已实测 | `skills list`、`skills get orca-cli`、`skills get orchestration`。 | `skills install/update` 会写入全局技能，需单独授权。 |
| 工作树与仓库 | 已实测（只读） | `worktree ps/current`、repo/project 列表可读；自动索引已修正为同时识别主仓库的 `.git` 目录与 linked worktree 的 `.git` 文件。 | `repo add`、base ref、`worktree create/set/rm` 会改注册表、Git 或工作区，须在独立测试 repo 内验证。 |
| 终端与文件 | 已实测（只读） | 终端列表和当前工作树可读；runtime 宣告 terminal multiplex。 | `terminal create/send/split/stop/close` 及 `file open/diff` 的 UI 副作用，待负载恢复后在专用测试工作区测试。 |
| 受监督编排 | 已实测（只读与拒绝路径） | `run-list` 返回现有 Run；未绑定 Run 的 `task-create` 得到 `run_required` 且 `effectsApplied:false`，由 `orca_lifecycle_precondition_probe.py` 可重复验证。 | `run-create`、Task、Dispatch、worker 生命周期、ask/reply、gate 需一套独立 sandbox 和至少一名 worker。 |
| 自动化 | 已实测（只读） | `automations list` 成功，当前为空。 | create/edit/run/remove 会创建持久计划或启动 agent，需隔离项目和明确时间表。 |
| 环境与联邦 | 已实测（发现/列表） | runtime 声明 federation/control-mail；环境列表命令成功。 | pairing、remote worker、environment add/rm 需要明确远端和人工授权。 |
| 账户 | 已实测（只读列表） | `account list` 成功且未输出资料。 | 登录会写入受管账户并可能打开设备授权；不自动测试。 |
| 内置浏览器 | 已实测（只读） | browser tab 与 profile list 成功；runtime 具 browser screencast。 | 导航、登录、上传、storage、cookie、凭据或真实网页写入需要指定站点和资料；真实网页默认仍优先 Ego。 |
| Ego 全局网页路由 | 已实测 | 通用 Orca agent、Claude、Codex、Orca Codex runtime、OpenCode hooks 与 Hermes 已统一为 Ego 优先；router 单测、两路并发互斥、无认证本地夹具回归均通过。 | 不把 task space 当 Cookie/登录态隔离；展示所需的新空白资料因公开 API 无 `createProfile` 而安全拒绝。 |
| Computer Use | 已实测（能力查询） | `computer capabilities` 成功。 | 枚举/操作其他桌面 app 或权限设置会触及用户 UI，需指定 app 和动作。 |
| 实时桌面坐标控制 | 已实测 | `desktop-mcp` 通过 Hammerspoon 的 Accessibility/CGEvent 发送坐标输入；Codex 与 Claude Code 均已接入 MCP。 | 输入仍可能触发外部副作用；发送、购买、账号变更、删除及密钥输入须先取得用户批准。 |
| iOS/Android 模拟器 | 已实测（iOS 列表命令） | `emulator list` 命令可达。 | attach、tap、相机、权限、Android AVD 和安装 app 需指定测试设备/应用。 |
| Linear | 需要用户目标/授权 | 命令 schema 已发现。 | 读取或写入真实项目管理数据必须指定 issue 与允许的状态变更。 |
| 诊断与记忆 | 已实测 | `diagnostics memory` 成功返回主机总览。 | 诊断输出含其它工作区元数据；仅保存聚合健康数据，不能写入 wiki 原文。 |
| Agent hooks | 已实测（只读状态） | `agent hooks status` 成功。 | on/off 会写入本地 hook 配置，需作为独立配置变更审阅。 |

## 已验证的协作路由

```text
Codex 内部并行        -> 原生子 agent
Claude <-> Codex 协作 -> Orca Run / Task / Dispatch / worker-start
完整交接不监督        -> Orca worktree/terminal handoff
Orca 不可用           -> tmux 单独承载 CLI + 非注入 mailbox
```

这条路由避免把 tmux 当成编排器，也避免让 Claude 的普通子 agent 静默后台启动 Codex。详细并发门槛和回收规则参见 [本机多 Agent 操作手册](../本机Claude-Codex-Orca多Agent操作手册.md)。

## Ego 全局策略节点

`ego-global-policy` 覆盖所有 Orca 启动的 agent 入口：网页自动化使用 `ego-browser`，多 agent 浏览器轮次使用 `ego_profile_router.py start/run/finish` 的同一把互斥锁。完成后只持久化外网 hostname 标签；localhost、私网 IP 和内网域名不产生标签。

`ego-profile-isolation` 是安全边界节点。官网将 Space 描述为 BrowserContext，但本机运行态复测到跨 Space Cookie 可读，因此 Space 仅作为窗口与任务所有权隔离，不能替代独立资料。明确展示/show 请求由 `fresh-display` 门禁处理；未发现官方 `createProfile` 时返回 `EGO_FRESH_PROFILE_UNAVAILABLE`，不会改写 Chromium/Ego 存储或回退到已认证资料。

`ego-capability-regression` 对应 `scripts/verify_ego_capabilities.sh`：它在临时 `127.0.0.1` 夹具上验证导航、快照、截图、输入、上传、拖拽、滚动、等待、Fetch、`js`/`cdp` 与 router 并发互斥，结束时关闭测试 task space 和本地服务。当前已知能力缺口为 `dispatchKey`；默认使用 `pressKey` 或 `typeText` 并校验结果。完整矩阵见 [Ego 与 Cloudflare 域名操作手册](../Ego与Cloudflare域名操作手册.md)。

## 实时桌面坐标控制

`desktop-mcp` 是给 Codex 与 Claude Code 共用的本机 macOS 输入链路：`STDIO MCP → 本地单例 broker → Hammerspoon → CGEvent + Accessibility`。它只暴露固定、校验过的鼠标、键盘和安全 AX 命中测试动作；没有 TCP 监听、截图、Shell、AppleScript 或任意 Lua 执行入口。Hammerspoon 持有 macOS Accessibility 权限，Unix socket 位于 owner-only 目录。

已在 2026-08-08 实测：单 MCP 客户端以 60Hz 发送 240 次同坐标真实 `mouse_move`，p50 为 1.33ms、p95 为 8.98ms、p99 为 16.57ms，仅 2 次超过 16.67ms 单帧预算；两个独立 MCP 客户端并发时 p95 为 7.32ms，且 broker 保证顺序与响应归属。该测试不点击、不输入、不截图，也不产生可见鼠标位移。

`mouse_move` 的默认路径是瞬时定位；可选 `duration_ms`（0–2000）会让真实光标逐步经过中间坐标，以显示可见轨迹。已完成 500ms 右移和 500ms 返回原位测试，未点击，且最终恢复初始坐标。该方案刻意不采集像素画面；它验证的是低延迟坐标控制，而非实时视频流。

## 下一阶段：隔离验收清单

需要用户给出或确认以下测试边界后，才能把“待隔离验收”提升为“端到端已实测”：

1. 一个可删除的测试 Git 仓库/工作树，用于 worktree、terminal、file、automation 和 orchestration lifecycle。
2. 一个允许测试的登录资料与公开测试 URL，用于内置浏览器；涉及真实站点仍使用 Ego 默认流程。
3. 一个明确的模拟器/测试 app，用于移动操作。
4. 一个明确的已配对远端或授权新建配对，用于 federation。
5. 明确允许注册/测试的 Claude/Codex 账户；不使用生产账号做账户添加测试。

每一项完成后，更新本页的状态、证据命令、时间和回退结果，再执行 `auto_index.py run`。知识图谱会从同目录的 `orca-context-wiki.json` 自动加入本页节点及其关系。
