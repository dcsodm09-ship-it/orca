# 从 AI超元域 307 条视频提炼的 Orca 改进报告

状态：`INTERIM / CURRENT_CANDIDATE_REJECTED / P0_STARTUP_AUTHORITY_OPEN / AUDIOVISUAL_AND_FINAL_DUAL_REVIEW_BLOCKED`

本报告已经覆盖频道全量清单、发布者简介字段、章节、外链、播放器元数据、GitHub 与 Skill 候选研究，并把 307 条视频分别冻结成可独立审阅的数据包。它不是最终验收报告：只有第 246 条具备完整字幕，字幕只支持口述内容分析，不能证明画面；其余 306 条尚无可用字幕或已授权 ASR，因此不能声称已学习完整视听内容。Claude Opus/max 与 Codex gpt-5.6-sol/max 已对旧候选根哈希 `5c966265…a309` 做同包、独立、临时复核并共同拒绝候选；启动权威 P0 未关闭，所以这些回执不能冒充最终双审放行。

## 一、可确认的结论

### 全量证据覆盖

| 项目 | 结果 | 证据边界 |
|---|---:|---|
| 频道 Videos 清单 | 307/307 | 顺序、ID、标题一致；重复 ID 为 0 |
| 发布者描述 | 304 非空，3 为空 | 保存完整原文；空字段不补写作者未提供的介绍 |
| 描述章节 | 3,234 | 仅代表发布者时间戳，不等于逐段观看 |
| 描述外链 | 812 | 仅用于来源路由，不自动信任 |
| 精确播放时长 | 189,039 秒 | 卡片显示多 154 秒，来自 154 条逐秒向上取整 |
| 字幕轨探测 | 307/307 | 仅第 246 条有字幕轨 |
| 口述内容字幕学习 | 1/307 | 354 段，约 700 秒；画面仍未验证，其余 306 条未补证 |
| GitHub 仓库目录 | 121 个唯一仓库 | 仅验证仓库和元数据；未 clone、安装或执行 |
| 发布者项目专属 GitHub 直链 | 29 | 链接身份较强，但仍不证明视频主张或 Orca 兼容性 |
| 官方/精确/关键词主仓库候选 | 215 | 确定性路由候选，不称为视频源码或“强关联” |
| 仅频道通用仓库链接 | 32 | 只链接 `win4r/AISuperDomain`，不是当前视频项目身份 |
| GitHub 弱关联 | 2 | 前代模型、官方 SDK/示例或同名未确认关系 |
| Aila 产品谱系推断 | 15 | 标题与频道产品线推断，明确不是发布者直链 |
| 无可信公开仓库 | 14 | 保持空缺，不为闭源产品伪造关联 |
| Skill 搜索 | 9 个查询、80 个命中 | 发现与路由；没有安装或执行 |
| 独立审阅包 | 307/307 | 每包有独立 SHA-256；两个 reviewer 必须使用同一包 |

逐视频介绍摘要见 [video-intro-index.tsv](inventory/video-intro-index.tsv)，完整发布者描述见 `evidence/<video_id>/youtube.json`，GitHub 路由见 [github-video-map.jsonl](inventory/github-video-map.jsonl)，冻结审阅包见 [review-packets.jsonl](inventory/review-packets.jsonl)。

### 主题信号

以下是对标题与发布者描述的确定性、可重叠关键词路由，不是完整视频语义分类：

| 主题 | 视频数 | 对 Orca 的含义 |
|---|---:|---|
| 模型评测、路由、成本 | 206 | 需要以任务类型和真实回执驱动模型选择，而非静态品牌偏好 |
| 多智能体编排 | 188 | Run/Task/Dispatch/DAG 是核心产品能力，但必须可观察、限额和可恢复 |
| 设计、媒体、多模态 | 162 | 产物生成需要统一的渲染、预览、批注和视觉验收合同 |
| 浏览器、桌面、移动自动化 | 126 | 录制回放很有价值，但必须绑定浏览器 profile、窗口状态和批准边界 |
| Agent IDE/编程工具 | 112 | Orca 应成为多种 agent runtime 的安全宿主，而不是复制每个 IDE |
| Skills、MCP、插件 | 100 | 发现、审计、暂存、激活和撤销必须是不同状态 |
| 微调、本地推理 | 95 | 需要数据、指标、许可证、隐私、资源和失败模式门禁 |
| 记忆、知识、RAG | 84 | 图和向量只是投影；时效、来源、撤销、冲突才是权威 |
| 安全、沙箱、治理 | 49 | 安全不能由 README、星数、单测或“本地运行”替代 |

第 246 条 PaliGemma 完整字幕还暴露了一个重要模式：字幕中的演示叙述不等于已观察画面，演示前后效果也不等于模型质量。标题“超越 GPT-4o”在 354 段字幕中没有对比基准支持。缺少视频帧、独立测试集、量化指标、数据许可证/隐私、失败样本和高风险场景验证时，Orca 应把结论标为 publisher/demo claim，而不是 acceptance。

## 二、当前可见 Orca 基线

本节只依据当前工作树可见文件的报告内字节快照，不代表已加载中央 reviewed authority，也不代表 installed runtime 已验收。源路径、源 SHA 与快照 SHA 见 [orca-baseline-source-manifest.json](inventory/orca-baseline-source-manifest.json)。

应保留的已有方向：

- [动态调度候选快照](evidence/orca-baseline/orchestration-dynamic-scheduler/ORCA-NATIVE-ARCHITECTURE.md) 已明确 Run/Task/Dispatch、`worker_done`、隔离 worktree、容量门禁和单写者边界；[scheduler.py 快照](evidence/orca-baseline/orchestration-dynamic-scheduler/scheduler.py) 还是无副作用 planner，并按风险/阶段选择模型与 effort。
- [知识图谱构建器快照](evidence/orca-baseline/orca-context-bridge/scripts/build_knowledge_graph.py) 对输入做大小、普通文件、非符号链接和 SHA-256 绑定；Graphify 仅作为目录元数据，reviewed route 还要求 exact-hash authority 绑定。这比“把所有聊天扔进图数据库”安全得多。
- [桌面桥快照](evidence/orca-baseline/desktop-mcp/README.md) 已有五秒单次状态租约、应用/窗口/坐标绑定、保护文本拒绝和批准边界；同用户 peer 身份仍未闭合，当前文档正确保持 production blocked。
- [Prime Agent 集成候选快照](evidence/orca-baseline/prime-agent-integration/README.md) 已将安装、启用、回滚、SSD 驻留、settings/extension 隔离和 exact release identity 分开，并明确 Prime Agent 不是安全沙箱。
- [记忆架构审计快照](evidence/orca-baseline/sol-memory-strengthening-audit/REPORT.md) 已识别 source/wheel/installed identity、typed relation、撤销、abstention 和 local-first 缺口；不需要因为视频热度再平行造一个未经治理的记忆系统。

本次实际运行同时复现了最重要的 P0：`central reviewed source freshness mismatch: wiki` 使启动上下文降级。正式预检为 yellow，只允许一次一个隔离 reviewer，因此两位 reviewer 是串行临时审阅；yellow 容量许可不等于中央启动权威已恢复，也不能放行最终验收。冻结回执见 [governance-preflight-20260815.json](inventory/governance-preflight-20260815.json)。

## 三、旧候选的临时同包双审结果

两位 reviewer 绑定同一个候选：`MANIFEST.json` SHA `98ee8f4d…c0b`、文件根 SHA `5c966265…a309`、307 行 packet 文件 SHA `48664fc8…58c`。完整历史输出保存在独立审阅归档，主报告只冻结 [双审回执](reviews/INTERIM-DUAL-REVIEW-RECEIPT.json)，防止未来 reviewer 在独立审阅前看到另一位结论。

| Reviewer | 模型回执 | 逐视频结果 | 发现 | 候选结论 |
|---|---|---:|---:|---|
| Claude | requested/effective `opus / max` | 307/307，306 接受有限边界、1 拒绝 | 0 P0 / 2 P1 / 5 P2 | reject |
| Codex | 终端横幅 `gpt-5.6-sol max` | 307/307，287 接受有限边界、20 拒绝 | 1 P0 / 5 P1 / 2 P2 | reject |

共同且可复现的阻断包括：启动权威 freshness mismatch；第 246 条把字幕错误提升为完整视听；row-only 协议未绑定字幕 SHA；GitHub “强关联”合并了通用频道仓库和关键词候选；五个 Orca 基线来源在旧候选之外。当前修订已修复报告内可控项并生成新 packet SHA，但还没有在 P0 关闭后重跑最终双审。

本轮还复现两个 Orca 编排兼容缺陷：组合启动器拒绝 provider CLI 可运行的 `gpt-5.6-sol/max`，必须走 tracked custom-terminal；Opus `worker-start` 与 Sol `dispatch --inject` 都曾报告 accepted/injected，但任务文本仍停在 idle TUI，需要对精确终端补发一次 Enter。这些是增量修复候选，不是重建 Orca 的理由。

## 四、优先级路线图

### P0：先恢复可信启动权威

#### P0-1 中央 reviewed authority 新鲜度闭环

目标：同一份启动包必须把 Git source、reviewed manifest、route catalog、Skill 引用和 installed runtime 身份绑定到可验证的新鲜度快照。

最小交付：

1. 生成一个只读 `startup_authority_receipt.v1`，包含 Git common-dir、commit/tree、dirty boundary、reviewed manifest SHA、route SHA、Skill SHA、installed capability catalog SHA 和观察时间。
2. 明确 `fresh`、`stale`、`mismatch`、`unavailable` 四态；任何被影响能力在后三态 fail closed。
3. 让 NACK 指向精确漂移对象，而不仅是总括性原因；修复后用新的 exact receipt 重跑，不复用历史绿色标记。

验收：干净基线通过；commit、route、Skill、manifest、installed catalog 任一字节漂移都可单独复现 NACK；回滚后用全新进程恢复。

### P1：补齐证据与审阅

#### P1-0A 视听证据摄取管线

307 条视频中 306 条没有字幕，说明“只读描述”无法支撑视频学习型任务。Orca 需要一个明确的媒体证据状态机：

```text
DISCOVERED
 -> PUBLISHER_METADATA_FROZEN
 -> CAPTION_PROBED
 -> AUDIO_ACQUISITION_AUTHORIZED
 -> ON_DEVICE_ASR_OR_APPROVED_PROVIDER
 -> TRANSCRIPT_SEGMENTS_HASHED
 -> FRAME_SAMPLE_PLAN
 -> CONTENT_ANALYSIS
 -> DUAL_REVIEW
```

要求：

- 默认先用现有字幕；无字幕时不得静默调用付费 API、导出 cookie 或请求系统权限。
- ASR 优先本机离线；权限、模型下载、磁盘预算、音频保留/删除策略分别出 receipt。
- 关键 UI/演示视频还需要按章节或变化点抽帧，字幕不能替代画面验证。
- transcript、frame、描述和作者外链必须保留不同 provenance；模型摘要不能回写成原始证据。

验收：至少覆盖无字幕、空描述、直播回放、语言切换、长视频、ASR 失败和撤销六类 fixture；不含凭据、cookie、私人浏览状态或隐藏推理。

#### P1-0B 同包双审与争议仲裁

本目录已经生成 307 个独立 `packet_sha256`。治理恢复后，Orca 应强制：

- Opus/max 与 Sol/max 收到完全相同的 packet SHA；
- reviewer 只读，不能修改证据包；
- 每个结论包含 `supported_claims`、`unsupported_claims`、`missing_evidence`、`orca_lessons` 和 severity；
- 两者结论差异进入显式 adjudication，不能由协调器静默挑选；
- 任一可复现 P0/P1 阻断发布或 Skill 激活，新候选需要重新双审。

验收：交换包、旧包、缺字段、伪造 reviewer/model/effort、只完成一位 reviewer 都必须拒绝完成态。

### P1：把视频中的高价值模式工程化

#### P1-1 `workflow.v1`：可编译、可观察、可复跑的工作流

Dynamic Workflows、Harness、OpenSpec、Spec Kit 和 BMAD 的共同价值不是“自动派更多 agent”，而是把临场提示变成可检查的执行合同。

建议字段：

```text
workflow_id / version / source_spec_sha256
stage_id / dependencies / concurrency_limit
agent / model / effort / token_budget / time_budget
read_set / write_set / network_policy / tool_allowlist
approval_class / stop_conditions / retry_policy
expected_artifacts / acceptance_predicates
```

执行前编译为现有 Orca Task DAG；执行后每阶段绑定 Dispatch、worker_done、artifact 和 acceptance SHA。工作流脚本不能直接越过 Orca authority 创建 worker、改 settings 或扩权。

#### P1-2 安全的 Record & Replay → Skill candidate

录制回放应只生成候选，不自动安装：

1. 录制时保存语义动作和必要状态选择器，不保存 cookie、密码、完整页面正文或未授权屏幕内容。
2. 浏览器动作绑定 Ego profile/task-space；桌面动作绑定 `desktop_state` 的应用、窗口和单次租约。
3. 对发送、购买、账号变更、删除、凭据输入插入不可省略的人工批准节点。
4. 回放先在 fixture/空白 profile/可恢复副本运行；差异转成新的候选版本，不原地改可信 Skill。
5. 通过 source review、权限清单、沙箱测试、双审和显式 activation 后才进入用户或项目 Skill。

#### P1-3 基于实测的模型路由与成本预算

当前 scheduler 能按风险和阶段选择模型，但视频主题显示还需要按工作负载实测：代码理解、UI、长上下文、规划、测试、图像、延迟、成本分别评分。

建议新增：

- 内容固定、可重复的任务套件与隐藏验收；
- 模型/版本/provider/account/effort 的 exact identity；
- input/output token、缓存、费用、墙钟、工具调用和失败码；
- 质量最低门、成本上限、上下文压力和降级策略；
- 路由只影响未来 Dispatch，运行中禁止热切换；
- 低成本模型负责可验证机械阶段，高风险最终判断仍走规定 reviewer。

#### P1-4 Graph-aware context plane，不改变权威层级

CodeGraph、GitNexus、codebase-memory-mcp、OpenWiki 和 Graphiti 都支持“先索引关系，再给 agent 小而完整的上下文”。Orca 可扩展当前 Graphify 目录，但必须保留：

```text
reviewed journal/truth registry
  > active/inactive closure
  > source-state-bound graph/index
  > query receipt
  > prompt context
```

每次查询应返回 source tree/hash、index version、coverage、staleness、query hash、选中节点/路径和 abstention reason。禁止上游工具自动修改 MCP 配置、hooks、AGENTS.md 或安装 Skill；这些必须拆成独立提案。

#### P1-5 Skill 供应链与作用域

本次 Skill 搜索得到 80 个命中；其中一个约 2K 安装候选来自仅 40 stars 的仓库，而最高命中约 542K 安装。两个 archived 信号来自独立的 GitHub 视频仓库目录，不属于 Skill 命中集合。星数、安装量和 archived 状态都只能作为分开记录的发现信号。

建议状态机：

```text
DISCOVERED
 -> SOURCE_FETCHED_AND_PINNED
 -> SKILL_MD_AND_REFERENCES_FULLY_READ
 -> PERMISSIONS_AND_NETWORK_DECLARED
 -> STATIC_REVIEWED
 -> DISPOSABLE_SANDBOX_EVALUATED
 -> DUAL_REVIEWED
 -> STAGED
 -> EXPLICITLY_ACTIVATED
 -> MONITORED / REVOKED
```

Skill 需要 personal/project/org scope、所有者、grant、版本、expiry、兼容 runtime、写集合、网络域和回滚 preimage。共享 promotion 必须 admin/human gated。

### P2：改善长期任务与产品体验

#### P2-1 Goal、heartbeat 与 checkpoint supervisor

Prime Agent 展示了长期 goal、heartbeat、后台 session 和恢复价值。Orca 应把它们接在现有 Run/Task/Dispatch 上，而不是另建无治理 daemon：

- heartbeat 只能检查已授权目标和已注册任务；
- 每次唤醒重跑容量、authority、依赖、账户和网络门；
- checkpoint 保存状态摘要和 artifact hash，不保存原始凭据/完整私密 transcript；
- 连续失败、预算耗尽、authority 变红或用户撤销时进入稳定终态；
- detached session 必须可见、可停止、可归属、可恢复。

#### P2-2 统一 Artifact Studio

设计/媒体主题占 162 条。可在现有 Orca 工作区加入统一 artifact contract：

- HTML/UI、图片、PDF、PPTX、视频、数据图表使用不同 renderer，但统一记录 source、tool/model、design system、render hash、尺寸和 QA；
- 原始文件、渲染预览、review comment 和修订版本并列；
- 视觉验收必须查看实际 render，不用源代码或生成成功代替；
- 导出、上传、发布仍是独立授权动作。

#### P2-3 面向操作者的证据仪表盘

在 worktree、diff、GitHub/Linear 和移动端旁边展示：

- Run DAG 与 ready/blocked/running/settled；
- 每个 Dispatch 的 agent/model/effort/account 绑定；
- token、费用、时间、重试和 stop reason；
- 写集合与 merge 顺序；
- artifact、测试、installed、real acceptance 的证据等级；
- Opus/Sol 同包审阅与争议状态；
- authority、freshness、capacity、storage、network 当前门禁。

## 五、明确不应照搬的模式

- 不执行上游 `curl | sh`、自动配置 MCP/hooks/AGENTS.md 或自更新命令。
- 不把“everything is a plugin”理解为插件可绕过 capability、scope、签名和兼容性检查。
- 不把持久 Python/REPL、后台 daemon 或模型生成命令当作 sandbox。
- 不把自愈浏览器操作当成可以跳过 profile、页面状态和人工批准。
- 不把 README 的性能数字、GitHub stars、安装量、单个 demo 或视频作者结论当作 Orca acceptance。
- 不让生成式 wiki、记忆、GraphRAG 或模型 judge 写入 truth authority。
- 不为闭源产品强配无关 GitHub 仓库，也不把同名项目视为同一产品。
- 不因本报告提出改进而重建、替换或重装 Orca；全部应以可逆候选、adapter、Skill、plugin、schema 和测试推进。

## 六、建议实施顺序

1. 先修复中央 reviewed authority 新鲜度并恢复同包双审能力。
2. 建立视听证据摄取，完成剩余 306 条内容学习；在此之前本报告只作为描述/仓库驱动的 interim 研究。
3. 以一个低风险本地 fixture 实现 `workflow.v1` 编译到现有 Task DAG，不启动新的执行后门。
4. 在只读代码索引 fixture 上比较现有 Graphify、CodeGraph/GitNexus 类方案；所有安装动作保持隔离。
5. 增加模型评测与 token/cost receipt，再接入动态路由。
6. 实现 Record & Replay 候选生成和 Skill 供应链，不自动激活。
7. 最后扩展 Artifact Studio、长期 supervisor 和操作仪表盘。

## 七、最终验收条件

本调研只有同时满足下列条件才可改为完成：

- 307 条视频均有完整视听证据，或逐条记录经用户接受的不可获得例外；
- 307 个冻结包分别收到 Opus/max 与 Sol/max 的同 SHA 独立回执；
- 所有 P0/P1 争议已对新候选复审并关闭；
- 报告 manifest、逐视频包、原始描述、字幕/ASR、GitHub/Skill 来源可重算；
- 任何 Orca 改进仍是独立、可逆、未激活候选，除非用户另行授权实施和运行态验收。
