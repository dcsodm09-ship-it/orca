# Claude Code Agent Teams 技术调研报告

## 执行摘要

Claude Code 的 **Agent Teams** 是一个**实验性功能**（v2.1.178 起），通过 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 环境变量启用。它允许一个主会话（Team Lead）协调多个独立的 Claude Code 实例（Teammates）进行并行协作，通过共享任务列表、消息收件箱和直接的点对点通信来协调工作。与普通 subagent（子代理）根本不同：子代理运行在单个会话的上下文内并向调用者返回结果，而 Agent Teams 中的队友是完全独立的会话实例，具有自己的上下文窗口、生命周期和跨进程通信能力。

---

## 1. 功能定义与触发方式

### 1.1 官方定义

Agent Teams 允许用户协调多个 Claude Code 会话（称为 **teammates**）共同完成任务。一个会话担任 **Team Lead**（团队领导），其他会话作为独立的队友（Teammates），各自在独立的上下文窗口中工作，通过共享的任务列表和消息系统进行协调。

### 1.2 触发/启用方式

**环境变量启用**（唯一方式）：
```json
// ~/.claude/settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

或在 shell 中：
```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

**启用后的行为**：

- 启用交互式会话（不适用于 `-p` 非交互模式）
- Claude 在调用 Agent tool 时指定 `name` 参数，会自动创建队友而非普通子代理
- 每个会话会自动拥有一个**隐式团队**（无需手动创建）
- 可通过自然语言或明确命令启动队友，例如：
  ```
  我想要三个队友从不同角度审查这个 PR：
  一个专注于安全，一个检查性能，一个验证测试覆盖
  ```

**不适用的环境**：
- 非交互模式（`--print`、`-p` 标志）：即使启用了 Agent Teams，子代理仍作为普通子代理运行
- SDK 模式（Claude Agent SDK）：不支持队友生成
- 条件禁用：设置 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0` 可覆盖较低优先级设置，强制禁用

---

## 2. 实际机制

### 2.1 架构组件

| 组件 | 角色 |
|------|------|
| **Team Lead** | 主会话，协调工作、分配任务、综合结果 |
| **Teammates** | 独立 Claude Code 实例，各自在独立进程中运行 |
| **Task List** | 共享任务列表（位于 `~/.claude/tasks/{team-name}/`） |
| **Mailbox System** | 队友间点对点消息传递（JSON 文件 `~/.claude/teams/{team-name}/inboxes/{agent-name}.json`） |

### 2.2 工作流程

```
主会话（Lead）
  ├─→ 生成任务或直接指示
  ├─→ 队友 A（独立会话进程）
  │   ├─→ 认领任务或接收任务分配
  │   ├─→ 执行工作（Read、Edit、Bash 等）
  │   ├─→ 可直接与队友 B/C 通信
  │   └─→ 完成后发送空闲通知给 Lead
  ├─→ 队友 B（独立会话进程）
  │   └─→ [同上工作流程]
  └─→ 队友 C（独立会话进程）
      └─→ [同上工作流程]
```

### 2.3 通信机制

#### 队友间消息传递
- **Mailbox 文件**：每个队友的收件箱是一个 JSON 文件
- **验证机制**：Claude Code 读取时会逐项验证格式；格式错误的条目会被清除（v2.1.207+ 后不再引起重复错误）
- **可靠性**：消息只在写入成功后才标记为"已发送"；写入失败时发送方收到错误

#### Team Lead 与队友的交互
- **消息**：使用 `SendMessage` 工具（自动添加到队友工具集）
- **任务列表**：在具有 Task tools 的会话中，队友可查看、认领、更新任务
- **空闲通知**：队友转入空闲时自动通知 Lead（无需 Lead 主动轮询）

### 2.4 角色分工与能力

**Team Lead 的责任**：
- 决定是否生成队友（基于任务复杂度）
- 创建任务或分配工作给队友
- 审批队友的计划（如启用了计划模式）
- 综合来自多个队友的结果

**Teammates 的能力**：
- 完全独立的 Claude 会话，拥有自己的上下文窗口
- 可认领任务、请求任务依赖、报告完成
- 可直接与其他队友通信（通过 `SendMessage`）
- 不可生成嵌套的队友（只有 Lead 可管理团队成员）

### 2.5 与背景 Agent 的关系

**背景 Agent（Background Agents）** 与 **Agent Teams** 是两个不同的概念：

| 特性 | 背景 Agent | Agent Teams 队友 |
|------|-----------|-----------------|
| **启动方式** | `claude --bg` 或 `claude agents` 管理 | 通过 Agent tool + `name` 参数自动创建 |
| **生命周期** | 由用户通过 `claude agents` 子命令管理，持续运行直到手动停止 | 与 Lead 会话一起启动/停止 |
| **通信** | 消息系统 (`ListAgents` / `SendMessage`) | 同样的消息系统 + 共享任务列表 |
| **协调** | 无自动协调机制 | Task list + 隐式团队管理 |
| **所属关系** | 全局、独立会话 | 属于某个 Lead 会话的隐式团队 |
| **是否属于 "Agent Teams"** | 否 | 是 |

**关键区别**：背景 Agent 是面向自由启动和管理的独立会话；Agent Teams 中的队友从属于某个 Lead 会话，形成显式的协作结构。

---

## 3. 与普通 Subagent 的详细对比

### 3.1 对比表

| 维度 | Subagent（子代理） | Agent Teams 队友 |
|------|-------|------------|
| **上下文** | 可选共享主会话上下文或独立上下文 | 完全独立上下文窗口 |
| **运行位置** | 同一进程内（当作后台任务运行） | 独立子进程/系统进程 |
| **会话数** | 单个主会话内 | 多个独立会话 |
| **生命周期** | 由主会话生成→执行→返回结果→销毁 | 与 Lead 同生命周期，可跨越多轮 |
| **通信方式** | 返回结果给主会话的 Claude 模型 | 点对点消息、共享任务列表、空闲通知 |
| **互相通信** | 命名子代理可通过 `SendMessage` 相互通信 | 队友可直接相互通信（无需 Lead 中介） |
| **工作协调** | 由 Lead 的 Claude 模型指挥 | 自我协调（通过任务认领、消息交互） |
| **成本** | 相对较低（结果摘要回到主上下文） | 较高（每个队友是独立的 Claude 实例） |
| **最佳用途** | 快速委派特定任务、隔离输出 | 复杂协作、持续讨论、并行独立工作 |

### 3.2 实际差异说明

**启动方式**：
- Subagent: Claude 在主会话中调用 Agent tool，传递 `type` 参数
- Agent Teams: Claude 在 Agent tool 中指定 `name` 参数（同时需要 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`）

**上下文共享**：
- Subagent: 可选择继承或不继承主会话对话历史（取决于配置）
- Agent Teams: 队友在启动时获得 spawn prompt，**不继承** Lead 的对话历史

**交互模式**：
- Subagent: 用户→主会话 Claude→子代理；子代理的输出作为"工具结果"流向主会话
- Agent Teams: 用户↔Lead 会话，Lead↔队友（通过消息系统），队友↔队友（直接通信）

---

## 4. 典型应用场景与限制

### 4.1 推荐应用场景

**最优用例**（官方列举）：

1. **并行代码审查**
   - 三个队友各专注一个方面（安全、性能、测试）
   - 同时审查，互不阻挡，最后综合

2. **竞争假设调查**
   - 五个队友各假设一个根本原因
   - 相互辩论驳斥，通过科学辩论法快速收敛

3. **新模块/特性开发**
   - 队友各自负责不同的文件/模块
   - 完全独立工作，无文件冲突

4. **跨层协调**
   - 前端、后端、测试各一个队友
   - 同步推进，避免串行等待

### 4.2 不适合的场景

❌ **序列任务**：最好用单个会话或子代理
❌ **同一文件多处修改**：容易产生覆盖冲突
❌ **任务间依赖多**：协调成本高，收益低
❌ **需要实时用户输入**：消息延迟和异步性不理想

### 4.3 已知限制

| 限制 | 影响 |
|------|------|
| **会话恢复限制** | `/resume` / `/rewind` 不能恢复进程内队友；需重新生成 |
| **任务状态滞后** | 队友有时未标记任务完成，需手动更新 |
| **关闭缓慢** | 队友需完成当前 API 请求才能关闭 |
| **单队一组** | 每个会话仅一个隐式团队，无法共享或创建多个 |
| **无嵌套队友** | 队友不能生成自己的队友 |
| **后台子代理限制** | 进程内队友的子代理不能后台运行（Lead 的后台子代理正常） |
| **无双向角色切换** | Lead 固定，不能晋升队友或转移领导权 |
| **显示模式限制** | 分窗模式仅支持 tmux / iTerm2；不支持 VS Code、Windows Terminal |

### 4.4 版本/权限要求

| 要求 | 说明 |
|------|------|
| **最低版本** | v2.1.178（移除 `TeamCreate`/`TeamDelete` tools，启用隐式团队） |
| **启用条件** | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 环境变量 |
| **功能状态** | **实验性（研究预览）**，API/架构仍可能改变 |
| **成本** | 高于单会话；每个队友占用独立的上下文窗口和 token 配额 |
| **权限** | 队友继承 Lead 的权限设置；启用后可单独修改 |
| **环境** | 仅限交互式会话；`-p` / 非交互模式中禁用 |

---

## 5. 实测材料与原始证据

### 5.1 命令行证据

**命令检测结果**：
```bash
$ claude agents --help
Usage: claude agents [options]
Manage background agents
```

**发现**：`claude agents` 子命令存在，用于管理后台 agent，与 Agent Teams 是分开的概念。

**不存在的命令**：
```bash
$ claude teams --help
# 返回错误：teams 子命令不存在（Claude 主命令帮助中无 teams 子命令）
```

**Agent 参数检测**：
```bash
$ claude --help | grep -i agent
--agent <agent>           Agent for the current session...
--agents <json>           JSON object defining custom agents...
```

### 5.2 配置文件证据

**Agent 定义目录**（本地实测）：
```
~/.claude/agents/
  ├── codex-bulk.md
  ├── codex-design.md
  ├── codex-fast.md
  └── codex-work.md
```

这些都是 **subagent 定义**，而非 Agent Teams 配置。

**Settings 中的团队相关配置**：
```json
"env": {
  "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "8",
  // 注：无 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS 环境变量
}
```

**Hook 中出现的 Teammate 相关事件**：
```json
"TeammateIdle": [
  { "hooks": [...] }
],
"SubagentStart": [...],
"SubagentStop": [...]
```

**发现**：配置中已存在 `TeammateIdle` hook（用于处理队友空闲事件），但在本次设置中未启用 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`。

### 5.3 Changelog 原始证据

**关键版本变更**（从 `~/.claude/cache/changelog.md`）：

v2.1.232（最近）：
```
- Subagent forking is now on by default: a subagent_type: "fork" subagent inherits 
  the full conversation and prompt cache, and non-teammate agent spawns in interactive 
  sessions now run in the background by default
```

v2.1.178（Agent Teams 重构）：
```
- Agent teams: removed the TeamCreate and TeamDelete tools. With 
  CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 set, every session now has one implicit team — 
  spawn teammates directly with the Agent tool's name parameter, no setup step needed. 
  The team_name parameter on the Agent tool is still accepted but ignored.
```

v2.1.101 及之前：
```
- Added /team-onboarding command to generate a teammate ramp-up guide from your local 
  Claude Code usage
```

**修复记录（表明稳定性改进）**：
- v2.1.207：修复了格式错误的队友邮箱导致重复错误的问题
- v2.1.199：修复了队友停止时发送重复空闲通知的问题
- v2.1.186：修复了队友在分窗模式下无法继承 Lead effort 级别的问题
- v2.1.180：修复了 tmux 队友窗格启动时壳配置初始化慢的问题

### 5.4 官方文档链接

**主文档**：https://code.claude.com/docs/en/agent-teams.md
- 完整的启用、控制、最佳实践指南
- 架构说明和故障排除

**相关文档**：
- Subagent 文档：https://code.claude.com/docs/en/sub-agents.md
- 跨会话消息：https://code.claude.com/docs/en/cross-session-messaging.md
- Hooks 参考：https://code.claude.com/docs/en/hooks.md

---

## 6. 结论速览：Agent Teams 的定位

### 6.1 相对于其他多 Agent 机制的位置

```
┌─────────────────────────────────────────────────────────────────┐
│                   多 Agent 协作方案对比                          │
├──────────────┬──────────┬────────────┬──────────┬────────────────┤
│ 机制         │ 生命周期 │ 上下文独立 │ 协调机制 │ 最佳场景       │
├──────────────┼──────────┼────────────┼──────────┼────────────────┤
│ Subagent     │ 临时     │ 可选       │ Lead指挥 │ 快速委派任务   │
│ Agent Teams  │ 持久     │ 完全独立   │ 自协调   │ 复杂协作       │
│ Background   │ 持久     │ 完全独立   │ 手动/消息│ 长期后台任务   │
│ Agent        │          │            │          │                │
│ Orca Run/    │ 持久     │ 完全独立   │ 编程     │ 工程级编排     │
│ Task/Dispatch│          │            │ 驱动     │ 和变更管理     │
└──────────────┴──────────┴────────────┴──────────┴────────────────┘
```

### 6.2 功能边界

**Agent Teams 能做什么**：
- ✅ 并行执行多个独立的子任务
- ✅ 队友之间点对点通信和讨论
- ✅ 自动的任务依赖管理（如已启用 Task tools）
- ✅ 实时的空闲通知（无需轮询）
- ✅ 用户可与任意队友直接对话
- ✅ 跨窗格/分窗显示模式

**Agent Teams 不能做什么**：
- ❌ 序列化的复杂工作流（Orca 编排更适合）
- ❌ 跨会话持久的团队（每个会话一个隐式团队）
- ❌ 队友生成子队友（嵌套）
- ❌ 后台子代理的长时间运行（会随 Lead 进程结束而死亡）
- ❌ 编程驱动的自定义协调逻辑（需自己在 Lead 的 prompt 中写入）

### 6.3 对比 Orca 编排层（Orchestration）

| 维度 | Agent Teams | Orca Run/Task/Dispatch |
|------|------------|----------------------|
| **托管位置** | Claude Code 内置 | Orca 编排工具 |
| **代码集成** | 自然语言 prompt | Python/YAML/编程 |
| **状态管理** | 隐式的会话关联 | 显式的 Run/Task 对象 |
| **错误恢复** | 基础重试 | 精细的错误处理和重试 |
| **多项目** | 无跨项目支持 | 支持 |
| **审计日志** | 会话记录 | 显式的 Run 历史 |
| **学习曲线** | 低（自然语言） | 高（编程模型） |

**选择原则**：
- **Agent Teams**：快速、轻量、基于对话的多 Agent 协作
- **Orca Orchestration**：复杂工作流、生产级别、完整的审计/恢复机制

### 6.4 总体定位

**Agent Teams 是什么**：
Claude Code 内的**轻量级、对话驱动的多 Agent 协作框架**，适合需要多个 Claude 实例进行实时讨论和协调的任务（研究、审查、调试）。

**与其他机制的关系**：
- **相比 Subagent**：交换"响应速度"和"低成本"换取"深度协作"和"持久性"
- **相比 Background Agents**：提供显式的团队结构和自动协调机制
- **相比 Orca 编排**：更轻量，但功能和可靠性更弱

**风险和特点**：
- 🔬 **实验性**：架构仍可能改变（v2.1.178 已有重大变更）
- 📈 **成本高**：每个队友是独立的 Claude 实例
- ⚠️ **已知缺陷**：会话恢复困难、任务状态滞后、关闭缓慢
- 🎯 **最佳用途**：平行探索型工作（审查、调查、假设对比）

---

## 附录：快速启用步骤

1. **启用 Agent Teams**：
   ```json
   // ~/.claude/settings.json
   {
     "env": {
       "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
     }
   }
   ```

2. **重启或刷新会话**（settings.json 自动重新加载）

3. **请求队友**：
   ```
   我要三个队友分别从安全、性能、测试三个角度审查这个 PR
   ```

4. **交互**：
   - 在进程内模式下，用上下箭头选择队友，Enter 查看/沟通
   - 设置 `"teammateMode": "auto"` 或 `"tmux"` 获得分窗模式

---

## 参考资源

- **官方文档**：https://code.claude.com/docs/en/agent-teams.md
- **Changelog 信息**：`~/.claude/cache/changelog.md`
- **本地配置**：`~/.claude/agents/` 和 `~/.claude/settings.json`
- **团队文件位置**：`~/.claude/teams/{team-name}/`，`~/.claude/tasks/{team-name}/`

