# 调研：holaOS（holaboss-ai/holaOS）记忆架构，对照 Orca 现有记忆系统的可迁移优化点

来源：<https://github.com/holaboss-ai/holaOS>（开源，Modified Apache 2.0），只读调研，未 clone/安装/执行任何代码。
调研范围：`apps/docs/content/docs/concepts/memory-and-continuity/*.mdx`（4 篇概念文档）+ 仓库文件树（2184 个文件路径，
grep 出 ~100 个 memory 相关源文件路径，未逐一读源码，仅读概念文档与文件树结构）。

## 一句话结论

holaOS 的记忆设计比 Orca 现在的记忆系统（`orca-agent-memory` 守护进程 + `claude-codex-memory-bridge` 单向注入
hook）成熟至少一个世代：它把"策略/续跑/可回忆记忆"三件事显式拆开，把 markdown 定为唯一真相源、检索索引全部
可从 markdown 确定性重建，并且检索走的是"规划→向量粗筛→词法精筛→合并重排→带证据的检索包"而不是 Orca 当前
的"截断字节数直接塞进 prompt"。下面把每个可迁移点对应到 `sol-memory-strengthening-audit` 已经点名的 Orca 自身
P0 缺口。

## holaOS 的核心设计（原文摘要）

### 1. 三层分离：策略 / 续跑 / 可回忆记忆

| 层 | 存储位置 | 是否持久 |
|---|---|---|
| 标准工作区策略 | `AGENTS.md`（人工撰写） | 是（人工所有） |
| 运行态续跑 | `turn_results`、`.holaboss/memory/runtime/`、`session-memory/` | **否**（明确标注"非持久知识"） |
| 可回忆持久记忆 | 工作区本地 `.holaboss/memory/interaction/`、`.holaboss/memory/semantic/interaction/trees/`；
全局 `memory/integration/trees/`、`memory/semantic/integration/trees/`、`memory/preference/`、`memory/identity/` | 是 |

**关键纪律**：运行态续跑文件明确"仅供检查/恢复，不当作持久知识"——即使技术上可以从里面提炼记忆，也不会被
自动当成真相。

### 2. markdown 是唯一真相源，索引永远可从它确定性重建

> "If the markdown and the index disagree, the markdown is the durable source of truth and the runtime rebuilds
> the derived state from it."

语义节点、搜索文档、向量嵌入全部是**派生索引**，从来不是权威内容本身。

### 3. 持久记忆的类型分类法

`preference`（稳定用户偏好）/ `identity`（身份类事实）/ `fact`（工作区或业务事实）/ `procedure`（可复用操作
流程）/ `blocker`（反复出现的权限/执行阻塞）/ `reference`（需要在使用前重新确认的外部引用）。

后台自动 writeback **只**处理 `fact`/`procedure`/反复出现的 `blocker`；`preference`/`identity`/profile 更新必须
经过显式接受的用户提案路径，绝不由后台流程静默写入。

### 4. 命名空间路径安全（allowlist + 拒绝穿越）

只允许 `MEMORY.md`（兼容索引）、`workspace/<workspace-id>/*`、`preference/*`、`identity/*`；拒绝绝对路径、`..`
穿越、任何逃出记忆根的路径；跨工作区访问被显式阻断。

### 5. 混合检索管线 `memory_retrieve`

规划该查询搜交互记忆/集成记忆/两者都搜 → 配置了嵌入时先跑一次向量粗筛 → 在最强的几棵树内跑词法精筛 → 合并
重排 → 输出**有界证据包**（不是全量转储）。返回结构包含：证据项（标题/摘要/新鲜度/分数/理由）、按"已知事实/
近期高信号/约束/阻塞/待澄清问题"分组的检索包、**覆盖信号**（用了词法/向量/邻接中的哪些）、**缺口信号**（是否
还需要实时核实或更窄的外部信源）。

### 6. 保守、批量化的后台 writeback

只在有可用的后台记忆模型时才跑；按 3 条已完成 turn 一批处理；只写"持久叶子"不写原始 turn 日志；只重建被
触碰到的交互实体树；不自动提升偏好/画像记忆。

## 对照 Orca 现有记忆系统的具体缺口（引用 `sol-memory-strengthening-audit` 已确认的 P0）

| holaOS 模式 | 对应的 Orca 已知缺口 | 建议动作 |
|---|---|---|
| markdown 唯一真相源 + 索引确定性可重建 | **已确认 P0**：正在服务真实会话的已装 `orca_memory.py` 哈希与任何源码分支都不一致，"是一份不在任何 git 分支上的构建"（`UNFINISHED-WORK-CROSS-REVIEWED-2026-08-12.md` 第 1 节） | 把"已装 daemon 必须能从某个具体 git commit 确定性重建"定为新的验收门禁项，仿照 holaOS "markdown 与索引不一致时以 markdown 为准、运行时重建索引"的纪律 |
| `memory_retrieve` 规划→向量→词法→合并重排→有界证据包（含覆盖/缺口信号） | **已确认 P0**：live `context_pack()` 未接入本地优先/Sol 兜底准入合约；当前 `ORCA_AGENT_MEMORY_CONTEXT_PACK_V1` 的实现看起来是"按 `max_items`/`fact_limit`/`max_utf8_bytes` 直接截断"，不是查询感知的检索 | 把 context-pack hook 的目标形态定为"规划→检索→合并重排→有界证据包+覆盖/缺口信号"，而不是当前的固定字节截断；这是一个真正的架构升级，需要单独立项、走候选→双复核流程，不能当场改 |
| 持久记忆类型分类法（preference/identity/fact/procedure/blocker/reference） | Orca 现有 L0-L3 只是"复核置信度"维度，缺"记忆种类"维度 | 可以低成本先做：给现有 L1-L3 记忆条目加一个 `kind` 字段（复用这 6 类），不改变现有信任层级语义，是纯 additive 的 schema 扩展 |
| 命名空间路径安全 allowlist（拒绝 `..`、绝对路径、跨工作区） | `claude-codex-memory-bridge` 当前只做字节数上限与"不可信历史参考"标注，README 未提及路径穿越防护 | 给 `claude-codex-memory-bridge` 补一条相同形状的 allowlist 校验，作为该候选下一版的具体加固项，随其本身的双复核流程一起走，不单独绕过 |
| 后台 writeback 按 3 条批处理、只写持久叶子不写原始 turn 日志 | 与 Orca 现有"只召回已审批、未撤销的 L1-L3"哲学方向一致，无需改动 | 无动作，已对齐 |
| 三层分离（策略/续跑/可回忆记忆），运行态续跑文件明确"非持久知识" | Orca 的 `.orca/context/`、session 快照、`memory.sqlite3` 目前边界不够清晰（本次普查也发现 `.orca/context/` 被注入器写进了本该只读的 git 树，导致 freshness 闸门自触发） | 值得作为一个独立设计任务：显式画出 Orca 自己的"策略 / 续跑 / 可回忆记忆"三个存储面，对应到具体文件路径，参照 holaOS 的表格形式产出一份 Orca 版本 |

## 明确不建议做的事

- **不建议直接搬运/依赖 holaOS 代码或依赖它的具体实现**：本次调研只读了概念文档和文件树，没有读它的实际源码逻辑，不能验证其正确性/安全性；抄代码等于把未经复核的外部代码引入一个本来就在强调"双模型独立复核"的系统，不划算。
- **不建议现在就动手实现混合检索管线**：这是架构级改动，涉及 `context_pack()` 这个已经被点名 P0 的活跃热路径，必须先立项、出候选、再走 Claude opus+max + Codex sol+max 双复核，不能在本轮直接改。

## 已转入执行台账

以上 4 条"建议动作"已加入 `reports/COMPLETION-BACKLOG-2026-08-16.md` 第 6 节（记忆 / 知识图谱 / 协作）追加
子项，标记 `BLOCKED_HUMAN_DECISION`（是否采纳该设计方向）或 `BLOCKED_DUAL_REVIEW`（涉及热路径的实现改动）。

—— 只读调研，未修改任何现有代码/候选/测试文件。
