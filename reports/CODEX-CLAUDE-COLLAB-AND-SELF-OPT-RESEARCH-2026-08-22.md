# Codex×Claude 协作 与 AI/Orca 本体优化 —— 全平台调研（2026-08-22）

## 执行摘要 TL;DR

本报告综合约 58 条 B 站真实视频数据（三轮扩大搜索）与两场 Workflow 检索到的 73 条国际/中文技术社区及官方渠道真实材料（涵盖 X/Twitter、Hacker News、GitHub Issues、arXiv、Anthropic/OpenAI 官方博客、掘金、少数派、V2EX 等），总计 **127 条不重复真实来源**（URL 去重后统计）。最值得关注的四点结论：

1. **Codex 与 Claude Code 的协作已从民间土法拼接升级为官方支持**——OpenAI 于 2026-03-30 正式开源发布 Claude Code 插件 `codex-plugin-cc`，提供 `/codex:review`、`/codex:adversarial-review`、`/codex:rescue` 等原生命令，是 OpenAI 主动把自家工具嵌入竞品 Anthropic 生态的标志性事件；随后开发者与中文社区各自独立涌现出功能相近的桥接工具，说明"双模型互相审查"已是真实、可复制的工程实践而非孤例。但 **Anthropic 官方插件市场并未收录这个插件**，是 OpenAI 单方面适配，不是双向官方整合。
2. **"Harness Engineering"（脚手架工程）正成为独立于模型本身的关键变量**——真实数据显示同一模型仅切换 harness、成功任务成本可相差 5 到 30 倍；"实验室拥有模型、你拥有 harness"的分工论获多家基础设施厂商呼应，但该概念本身也遭部分从业者质疑为旧词新造，需辩证看待。
3. **多 agent 编排的可靠性问题是贯穿学术界与产业界的共识性痛点**——从 2025 年论文《Why Do Multi-Agent LLM Systems Fail?》到 Redis、Ably 等厂商 2026 年的公开表态，反复印证"项目失败常源于编排层被低估、orchestrator 自身成为瓶颈"；三份来自 `anthropics/claude-code` 官方仓库的真实用户 issue（12 小时会话挂起、361 个失控子 agent 耗尽配额、单次过夜自主周期暴露 12 个协调 bug）是具体的一手证据。
4. **已有真实独立项目在做与"本体自我优化"几乎同构的事**——agent-improvement-loop 每日挖掘 Claude Code 与 Codex 会话定位反复摩擦点、Claude Code 内部被曝光的"Magic Docs"自更新文档机制、15k+ star 的自我改进型 Prime Agent 项目，说明这一方向正被业界同步验证而非闭门造车。

抖音/小红书/快手/微博四个中文短视频/社交平台的 MoreAPI 接口今天没能打通（已列 6 条问题移交给持有该服务标准授权的会话处理）；本轮对报告链接做了 20 条抽样核实，**未发现任何虚构或域名不存在的链接**，但约一半样本存在主题匹配度偏弱或抓取工具反爬拦截的问题，已在文末"链接核实说明"逐条列出。

## 调研方法

- **中文社交/短视频平台**：本会话按 `CLAUDE.md` 规则先读 `SERVER_CONNECTION_RULES.md`、跑 `verify-ssh-routes.sh`（静态检查全过），再 SSH 到 `hgcloud`，直接调用用户自建的 `moreapi.service`（APIServer v5.3.6，本机 `127.0.0.1:8001`，真实生产服务，非第三方黑产接口）。只做只读搜索调用，未做任何写操作、未修改服务配置。
- **补充调用方式（用户要求"用代理的方式"）**：该服务同时挂了 `cloudflared-moreapi.service`，经 Cloudflare Tunnel 对外暴露在 `https://moreapi.aistartools.com`，无需 SSH 隧道即可直接 HTTPS 调用（鉴权仍是同一个 `pwd` 字段，走公网但仍受 Cloudflare 保护）。验证：同一个 `__ac_nonce` 报错在公网域名下原样复现，证明抖音那个 bug 是后端本身的问题，不是 SSH 路径的假象。踩坑记录：Python `urllib` 默认 User-Agent 会被 Cloudflare 拦成 403，换成普通浏览器 UA 后恢复正常——留给以后调用的人。
- **国际/中文技术社区 + 官方渠道**：用 `Workflow` 并行调度 6 个只读调研 agent（2 主题 × 3 渠道），各自用 `WebSearch`/`WebFetch` 检索并核实真实链接，最后由一个合并 agent 去重整合。
- 两条腿的结果都要求"只报真实检索到的内容，不编造标题/链接"。

## 已知限制（如实记录，不遮掩）

调研当天（2026-08-22）本会话遇到的失败已转交 `自媒体接口分析/接口-代理` 会话处理，该会话随后完成了一轮真实修复，最新状态更新如下：

| 平台/接口 | 调研当天状态 | 最新状态（`接口-代理` 会话反馈） |
|---|---|---|
| 抖音 `search_video` / `get_anonymous_cookie` | ❌ `__ac_nonce` 500 报错 | ✅ **已修复**——接入快代理隧道解决了根因；视频搜索**务必用 `search_video_v2`，不要用 v1** |
| 抖音热点榜单 | 未测 | ✅ 可用 |
| B 站 `search` | ✅ 成功 | ✅ 仍可用 |
| 微博热搜 `hot_search` | 未测（当天只测了 `search_data`） | ✅ 可用（`search_data` 完整关键词搜索仍需真实登录 cookie，用户已明确表示"暂时不用处理"） |
| 小红书 `search_suggestion`（搜索联想词） | 未测 | ✅ 可用（完整关键词搜索 `search_note` 仍需真实登录 cookie，同上，暂不处理） |
| 快手全部搜索 | ❌ 缺登录态 | ⚠️ 仍卡住，需要真实登录 cookie，用户已明确表示暂不处理 |
| YouTube `search_data` | ❌ 缺 Google API Key | ⚠️ 仍卡住，缺 `GOOGLE_API_KEY`（国际平台改由 Workflow 的 WebSearch 覆盖，未受影响） |
| TikTok | 未测 | ⚠️ 缺 `OVERSEAS_PROXY_URL`——已接入的快代理是大陆代理，解决不了海外平台的访问需求 |
| 今日头条/西瓜视频/Lemon8 | 未测 | ⚠️ 仍未测试——全部接口都要求真实内容 ID，没有可空测的公开接口 |

另外，`接口-代理` 会话修复过程中还发现并修了 **3 个接口重试次数被误写成 0 次**的 bug（本报告调研当天未触发到，属于新发现），以及一个**尚未修复、需要用户决定**的问题：响应 JSON 里的 `proxy` 字段会**明文回显代理账号密码**，这是一个真实的凭据泄露面，本报告如实转达，不代为决定要不要处理。

因此"中文社交/短视频平台"这一路，截至本报告调研当天实际只有 **B站** 出了真实数据；上表"最新状态"栏反映的是后续修复后的能力，本报告正文的实际检索结果仍以调研当天的一手数据为准（正文里的 B 站条目），后续如需要用新打通的平台补充检索，应另起一轮。

---

## 一、Codex 与 Claude 协作

### 1.1 中文社交/短视频平台（MoreAPI → 哔哩哔哩，真实数据）

**搜索词「Codex Claude 协作」：**

- **codex与claude code协作教程** — UP主：铅笔头tech，播放量：4050
  https://www.bilibili.com/video/BV1P1L96KE6Z
  > Claude 规划审核Codex编程可以极大提高代码质量
- **别再二选一：Claude Code + Codex 联用才是最强姿势** — UP主：星小脉，播放量：20445
  https://www.bilibili.com/video/BV1zjd3BiEzo
  > Codex 已悄然追上 Claude Code，GPT 5.5 比肩 Opus 4.7、OpenAI Pro 额度更大方。但作者 Chase 想说：别再纠结谁更好，最佳姿势是把两者一起用——Codex 桌面应用直接跑 Claude Code
- **20秒看懂，一招在Codex里用上Claude Code** — UP主：卡尔的AI沃茨，播放量：24441
  https://www.bilibili.com/video/BV1LY7K6dEnd
- **你怎么知道Codex和Claude Code可以合作干活了一起用了？** — UP主：卡尔的AI沃茨，播放量：7544
  https://www.bilibili.com/video/BV1vZ7r6xEjn
- **5分钟演示：codex和claude协作干活** — UP主：Miss发行人，播放量：198
  https://www.bilibili.com/video/BV1yi876iExa
  > 很多人知道多Agent能协作，却不知道具体怎么开。这期直接实操：只在一个窗口输入任务，让主Agent自动组队、分工、推进并汇报，以"如何写好提示词"为例分工调研/查资料/写作/审核
- **完美复刻OpenAI官方Codex Micro 宏键盘，支持二次开发，可同时控制多款AI Agent** — UP主：虾米实验室，播放量：686
  https://www.bilibili.com/video/BV1678A6pEzw
  > 支持Codex、Claude Code等多种AI Agent以及常规宏键盘，可以进行二次开发
- **🚀Claude Code重大突破：Workflow功能完整实战教程！ultrawork召唤无数个Agent协同！** — UP主：AI超元域，播放量：117350
  https://www.bilibili.com/video/BV1KoGE6cE53
  > 全球首测！Anthropic未官宣的Claude Code Workflow隐藏功能完整使用指南，三大阶段六种形态精准解析

**搜索词「Codex Claude Code」：**

- **9分钟搞定！Claude Code 保姆级安装+原理+真实用法（国内直连）** — UP主：人工大黑，播放量：1737109
  https://www.bilibili.com/video/BV1KjoxBoEQJ
- **Pi 大道至简，超越Codex和Claude Code的极简Agent，保姆级全攻略** — UP主：技术爬爬虾，播放量：152987
  https://www.bilibili.com/video/BV139bD6gEa8
  > Pi 只有四个默认工具（读/写/改文件、运行命令），系统提示词仅约一千 Token，多项基准测试代码质量/速度对标 Codex 和 Claude Code
- **分享一下我对于 Codex 和 Claude 的一些看法** — UP主：oil欧呦，播放量：75664
  https://www.bilibili.com/video/BV1677s6UErR
  > 深度用过 Codex 和 Claude 之后，分享桌面端产品设计、模型体验和日常工作流的真实感受
- **【2026最新Codex】Codex保姆级完整教程** — UP主：编程大佬陈悠秀，播放量：2162940
  https://www.bilibili.com/video/BV1BVEs6LENZ
  > Codex APP 比起 Claude Code 额度更高、功能更全，免费账户也能用

**搜索词「Claude Code 多agent」：**

- **Claude Code多Agent模式实战分享** — UP主：Simon林_，播放量：24896
  https://www.bilibili.com/video/BV1WtoTBiEuR
  > Claude Code有2种多Agent模式：多个subagents模式和多个独立agent模式
- **Claude 4.6最新功能，Claude Agent Teams 保姆级入门及使用教程** — UP主：AI随风随风，播放量：48494
  https://www.bilibili.com/video/BV1fjcgzLE43
- **🧲 Claude Code 团队：Agent Teams 的理想和现实** — UP主：沧海九粟，播放量：6559
  https://www.bilibili.com/video/BV1c3Tw6FEQs
  > 官方文档：code.claude.com/docs/en/agent-teams
- **Claude Code 多 agent 并行又升级：agent view 一屏管所有会话** — UP主：星小脉，播放量：5020
  https://www.bilibili.com/video/BV1ty5K6qERZ
  > Anthropic 给 Claude Code 加了 agent view——一个终端 tab 里同时看所有并行 session 的状态（待输入/进行中/已完成）
- **Claude Code: 从零搭建你的 AI 工作团队（Skills + Agents）** — UP主：回到Axton，播放量：105040
  https://www.bilibili.com/video/BV1eADaBME5z

**补充搜索词「Claude Codex 双复核」（经 `moreapi.aistartools.com` 代理域名检索）：**

- **如何解决Codex、Claude Code的 /goal 跑一会就停的问题？** — UP主：Next蔡蔡，播放量：6456
  https://www.bilibili.com/video/BV1wkV26MEfu
- **硬核分享TikTok如何结合Ai(Claude ,Codex)落地运营** — UP主：海外跨境Kevin，播放量：3393
  https://www.bilibili.com/video/BV1QeG26UEqF
- **一行命令让 Claude、Codex、Gemini 组队干活 ｜ 我开源了这套多 AI 协作 SKILL** — UP主：回到Axton，播放量：9218
  https://www.bilibili.com/video/BV1D6cZzNERN

### 1.3 第三轮扩大搜索（用户要求"越多越好"，10 组新搜索词，经代理域名检索）

10 组新词共拿到 42 条新增不重复结果；以下按子主题归类展示，过滤掉纯广告/无关噪音后保留：

**A. Claude Code / Codex 横向测评对比：**

- **100 小时测试 Claude Code vs Codex（真实结果）** — 设计之道，play=38525 — https://www.bilibili.com/video/BV1x6Vt6dEef
- **别被参数骗了！Codex和Claude Code实测对比，$20档位差距比你想象大得多！** — 阿喵讲AI，play=26134 — https://www.bilibili.com/video/BV1yHVK6bE3f
- **为什么大家都放弃Claude Code，转投CodeX了...CodeX强在哪？** — 赛博学妹科技社，play=37633 — https://www.bilibili.com/video/BV1zY7k6aE9i
- **我让 Codex 和 Claude Code 做同一款应用，一方明显胜出** — 黑纹白斑马，play=2640 — https://www.bilibili.com/video/BV15Lba6WE2q
- **Claude Code、Codex (ChatGPT)、Cursor该怎么选？Max/Pro/Ultra Plan亲身经验分享** — HexUp，play=195699 — https://www.bilibili.com/video/BV1h4DkBaEu1
- **DeepSeek Harness 实测 Claude Code 对比后，梁神我错了** — 程序员晓刘，play=14144 — https://www.bilibili.com/video/BV1hmb26ZEws

**B. 两者结合使用 / 工具链集成：**

- **国内爽用 Claude Code + Codex，2分钟搞定！** — 程序员鱼皮，play=237517 — https://www.bilibili.com/video/BV14JEj6uEdG
- **为什么我建议你同時用 Claude Code＋Codex？双Agent 实战分析** — Fankoai范式，play=4995 — https://www.bilibili.com/video/BV1oWNc6VECK
- **电商人狂喜！Codex+skills一个人成一个团队** — 不吃辣的Chris，play=27731 — https://www.bilibili.com/video/BV1vaN56LEhf
- **IDEA中集成claudecode和codex，全程无废话，提升编程效率** — 阿秀_love，play=9205 — https://www.bilibili.com/video/BV1Uwj863EPS
- **pycharm安装+集成claude+codex，一键代码生成神器** — 阿秀_love，play=3169 — https://www.bilibili.com/video/BV1bU3d6TEPW
- **Zotero MCP 接入 Claude + Codex 完整教程** — 旭光升，play=8360 — https://www.bilibili.com/video/BV1W2E46hEuv
- **Opencode go通过CCswicth 接入Claude Codex Gemini OpenClaw** — Ineedbrain，play=9668 — https://www.bilibili.com/video/BV1BbNN64EcS
- **把Claude code丨Codex接入Rstudio的工具ClaudeR体验分享** — 外科小小硕，play=6101 — https://www.bilibili.com/video/BV1E6Vh6WEDa
- **用ccswitch给Claude code和codex接入Agnes** — 摸鱼都尉，play=4931 — https://www.bilibili.com/video/BV1c4EZ61E7t

**C. 互相审查代码 / 双复核模式（与 Orca 的"双模型复核"理念最接近）：**

- **让Claude Code和Codex互相审查代码,光靠计划模式远远不够** — 星小脉，play=1815 — https://www.bilibili.com/video/BV1Fu7r6FEac
- **40、Claude Code 整合Codex插件审查代码详解** — 神秘的葱，play=41 — https://www.bilibili.com/video/BV11MT46TEcJ
- **如何解决Codex、Claude Code的 /goal 跑一会就停的问题？** — Next蔡蔡，play=6456（已在 1.1 记录）
- **谁来 review AI 代码？为 AI 打造的代码质检工具** — no-mistakes | x-cmd，play=1249 — https://www.bilibili.com/video/BV1jQug6DEBb
- **怎么让claude长期运行，不要人工点击确认** — 阿杰学ai编程，play=15283 — https://www.bilibili.com/video/BV1eUxCzfEa5

**D. Anthropic vs OpenAI 行业动态（背景/竞争视角，非教程）：**

- **别搞混：OpenAI与Anthropic接口协议区别** — 吃人的代码，play=1873 — https://www.bilibili.com/video/BV1KcJW6bEum
- **OpenAI和Anthropic x上开撕！封号挖墙脚互怼三天** — 新智元AIEra，play=15839 — https://www.bilibili.com/video/BV1e4uq6QEk5
- **【2026超级碗广告】Anthropic讽刺OpenAI计划将广告植入AI** — 全球好看广告精选，play=16755 — https://www.bilibili.com/video/BV1WbFxzbEH7
- **OpenAI 阴阳 Anthropic，Anthropic 反手挖人** — 老高AI观察，play=327 — https://www.bilibili.com/video/BV1keuS6vEGm

### 1.4 国际/中文技术社区 + 官方渠道（Workflow → 6 agent 并行 WebSearch/WebFetch，共 189 次工具调用、48 万 token）

> 以下内容由 Workflow 多路检索后经去重合并 agent 整理，同一篇内容在不同检索路径重复出现的已合并（发现 3 处：OpenAI 官方 `codex-plugin-cc` 仓库、Anthropic 官方博客《Building multi-agent systems》、Claude Platform 官方文档《Multiagent orchestration》）。所有标题/URL/来源/日期均为真实检索结果，未编造。

#### 1.4.1 国际技术社区

**[FEATURE] Claude Code Channels (MCP) → OpenAI Codex App Server in the same live session (first bridge)**
https://github.com/anthropics/claude-code/issues/36871
来源：GitHub (anthropics/claude-code issues) ｜ 日期：2026-03-20
开发者 raysonmeng 提出的功能请求：希望 Claude Code 通过 MCP 通知系统与 OpenAI Codex App Server 建立同一会话内的实时双向通信，避免当前只能靠人工在多个终端间复制粘贴的跨模型协作方式。他给出了一个开源 PoC（agent-bridge，纯本地 MCP 服务器 + JSON-RPC），演示 Claude 与 Codex 在同一会话里互相问候并共同写代码，场景是 Codex 优化后端、Claude 处理前端，双方通过 MCP 实时注入代码建议并互相审查。该 issue 最终被官方标记为 closed/not planned，说明 Claude Code 官方尚未原生支持跨厂商 agent 的实时会话内互连，这类协作仍依赖社区自建桥接方案。

**Cross-Model LLM Code Review: Should you use Claude to review Codex or vice versa?**
https://arxiv.org/abs/2607.21656
来源：arXiv ｜ 日期：2026-07-22
对照实验论文（116 个编码任务），直接检验"跨模型互相代码审查"这一双复核模式的实际收益是否对称。结果：让 Claude 审查 Codex 生成的代码，通过率从 71.6% 提升到 89.7%；但反过来让 Codex 审查 Claude 的代码，通过率反而从 91.4% 降到 82.8%。结论存在明显不对称性，建议"用 Claude 审 Codex，而非反过来"，为社区流行的双模型互审工作流提供了实证依据。

**Claude Code and Codex can have real-time conversation via Git**
https://news.ycombinator.com/item?id=48345837
来源：Hacker News ｜ 日期：约 2026 年 6 月上旬（获 116 赞、79 条评论）
以 Git 仓库作中介让 Claude Code 与 Codex 近实时"对话式"协作。评论区分化明显：不少人质疑其价值，认为只是"手工设定的提示词工作流"被包装成新颖设计；也有人指出真正瓶颈不是模型间通信本身，而是让人类审阅者看懂/信任已达成的"协议内容"——国际社区对跨模型实时协作可行性持怀疑态度的一个典型样本。

**A Two-Agent PR Workflow: Claude Writes, Codex Reviews**
https://salmanalibanani.com/2026/07/04/a-two-agent-pr-workflow-claude-writes-codex-reviews/
来源：个人技术博客 ｜ 日期：2026-07-04
基于 GitHub Actions 的"Claude 写代码、Codex 审代码"两 agent 流水线：Claude 在分支实现变更并开 PR；GitHub Actions 监听 PR 事件自动触发 Codex（通过 OpenAI API）评审并发布批注；Claude 基于反馈做一次修复；最后合并并打上 `reviewed_by_codex` 标签，刻意限定单轮修复避免无限评审-修改循环。

**Claude Code + Codex Dual Review (Gist)**
https://gist.github.com/mlshv/f23fe487c6282009120248388682f5b6
来源：GitHub Gist ｜ 日期：2026-02-17
"双复核"工作流：先由 Claude 探索代码库、起草实现计划，再发给 Codex 评审并返回结构化 JSON（APPROVED/NEEDS_REVISION 裁定 + 问题严重级别）；Claude 针对每条意见决定接受改计划或拒绝并说明理由，最多迭代 3 轮。适用于复杂任务/架构决策，是在正式编码前引入跨模型复核（而非仅事后审代码）的具体范式。

#### 1.4.2 中文技术社区

**OpenAI 官方出手：把 Codex 接进 Claude Code** — 掘金 ｜ 2026-04-01
https://juejin.cn/post/7623251356007186468
详细介绍 OpenAI 官方插件 codex-plugin-cc：`/codex:review`（只读审查）、`/codex:adversarial-review`（对抗式审查，专门质疑设计取舍）、`/codex:rescue`（任务移交 Codex 调试/续接）等命令，体现"Claude 负责主工作流、Codex 作第二视角审查者/后台执行者"的官方实现。

**让 Agent 指挥 Claude Code、Codex、Gemini 同时给你打工——一条命令搞定多 Agent 并行** — 掘金 ｜ 2026-02-27
https://juejin.cn/post/7611095964389687342
开源 MCO（Multi-CLI Orchestrator），一条命令并行调度 Claude Code、Codex、Gemini CLI 等五个编程 Agent 各自独立执行同一任务后汇总结果做交叉验证。实测：5 个 Agent 分析同一段 retry.py 找 Bug，只有 Qwen 发现负数退避倍数导致的延迟问题、只有 Claude 给出正向设计评价——直接体现跨模型多复核能显著提高问题发现覆盖率。

**告别人肉复制粘贴：我的 Claude + Codex 自动化协作工作流** — 少数派 ｜ 2026-03-24
https://sspai.com/post/107162
开源三个 Claude Code Skill 实现自动协作：Codex Skill（把 Codex 封装为可调用 Sub-Agent）、Plan Review Skill（Claude 主导、调用 Codex 对方案打分/挑刺，自动迭代至双方一致）、Plan Execute Skill（Claude 当"架构师"做代码审计，Codex 负责编写代码/修 bug）。

**分享一个基于 Codex app-server 协议做的 Claude Code 与 Codex 协作 skill** — V2EX ｜ 2026-02-28
https://www.v2ex.com/t/1194858
codex-collab Skill：直接对接 Codex App Server 协议（JSON-RPC 2.0）实现两者通信，当用户提及 Codex 或系统判断需要"第三方意见"时自动触发。

**双开 Claude Code + Codex 好几个月，受不了人肉传话，写了个桥让它俩自己聊** — V2EX ｜ 2026-07-04
https://www.v2ex.com/t/1224964
AgentBridge（github.com/raysonmeng/agent-bridge）："双向常驻对等体"架构，两个 Agent 在同一会话中实时双向协作、支持中途打断反馈；典型分工是 Claude 负责规划分工、Codex 执行任务并互相审查。底层用 MCP channel + 逆向实现的 Codex 协议代理，MIT 协议本地运行，仅支持 macOS/Linux。

**分享一个让 Codex 和 Claude 相互调用的小工具** — V2EX ｜ 2026-08-13
https://www.v2ex.com/t/1234158
Desktop Agent Bridge（DAB）：Codex 对话中输入 `$peer-review` 自动新建 Claude session 做代码审查并传回，反向在 Claude 中输入 `/peer-review` 调用 Codex 审查，全程保留两个官方桌面 App 原本体验。

**搭了一个Claude, Codex, Gemini 协作的 Agent 工作流** — 知乎 ｜ 2025-11-27 前后
https://zhuanlan.zhihu.com/p/1977462207890626511
开源 Claude-Team：Claude 为"总控"统筹调度，Codex 当"工程师"负责代码实现与 debug，Gemini 当"分析师"利用超长上下文做分析。获 175 赞。

**你们都是怎么实现两个不同的大模型互相协作 coding 呢？** — V2EX ｜ 2026-08-02
https://www.v2ex.com/t/1231430
提问如何让 Claude Code 与 Codex 协作编程，回复给出 opencode 的 omo 插件、multica-ai 等现成工具方案。

#### 1.4.3 官方渠道

**openai/codex-plugin-cc — Codex Plugin for Claude Code** — GitHub（openai 官方组织仓库）
https://github.com/openai/codex-plugin-cc
首个正式版 v1.0.0（2026-03-30），最新 v1.0.6（2026-07-08）。OpenAI 官方开发维护的 Claude Code 插件：`/codex:review`、`/codex:adversarial-review`、`/codex:rescue`、`/codex:transfer`（把 Claude Code 会话交接为持久化 Codex 线程）。**关键限定**：经核查 Anthropic 官方 `claude-plugins-official` 市场目录中并未收录该插件——这是 OpenAI 单方面自托管分发，不是 Anthropic 官方审核收录的整合。

**Connect to MCP servers** — Claude Code 官方文档
https://code.claude.com/docs/en/mcp-quickstart
讲解 `claude mcp add` 如何把外部 MCP server 接入 Claude Code——是 Claude Code 能"反向调用 Codex CLI"所依赖的底层协议基础设施。

**Discover and install prebuilt plugins through marketplaces** — Claude Code 官方文档
https://code.claude.com/docs/en/discover-plugins
明确提示"Anthropic 不审核第三方插件所含的 MCP server、文件或代码，无法验证其按预期工作"——印证 codex-plugin-cc 是完全自托管分发。

**Use Codex with the Agents SDK** — OpenAI 官方开发者文档
https://learn.chatgpt.com/docs/mcp-server
说明如何用 `codex mcp-server` 命令把 Codex CLI 自身暴露成 MCP server，配合 OpenAI 自家 Agents SDK 搭建多 agent 工作流。理论上任何 MCP client（含 Claude Code）都可接入。

---

#### 1.4.4 Twitter/X 补充检索（第二场 Workflow，补齐平台缺口）

**OpenAI 官方宣布开源 Claude Code 插件，内置 Codex 集成** — @romainhuet（OpenAI Head of Developer Experience）｜ 2026-03-30
https://x.com/romainhuet/status/2038677236304245087
观察到很多 Claude Code 用户会额外调用 Codex 做代码审查、用 GPT-5.4 处理更复杂任务，因此 OpenAI 开源了这个插件，让用户可以用自己的 ChatGPT 订阅在 Claude Code 里调用 Codex——这是 OpenAI 主动为竞品 Claude Code 打造官方集成插件的公开宣布贴。

**插件基于开源 Codex app server / 同一套 harness** — @romainhuet ｜ 2026-03-30
https://x.com/romainhuet/status/2038681887959380434
补充说明该插件基于开源的 Codex app server 与同一套开源 Codex harness 构建，因此能获得与原生 Codex 相同的模型、并行任务处理与代码审查流程。

**插件实际开发者发布上线公告** — @dkundel（OpenAI DevRel，插件作者）｜ 2026-03-30
https://x.com/dkundel/status/2038670330257109461
可以在 Claude Code 里直接触发 Codex，把任务委派给 Codex，或用自己的 ChatGPT 订阅让 Codex 审查 Claude Code 生成的改动。

**社区实测演示：三个核心命令** — @reach_vb（Hugging Face）｜ 2026-03-30
https://x.com/reach_vb/status/2038671858862583967
演示插件安装与用法，列出三个核心命令：`/codex:review`（只读审查）、`/codex:adversarial-review`（对抗式挑战审查）、`/codex:rescue`（让 Codex 接管救场）。

**真实用法分享：用 Codex 修复 Claude Code 的错误** — @PaulSolt ｜ 2026 年 4 月上旬
https://x.com/PaulSolt/status/2043434962477539412
用 Codex 插件发现并修复 Claude Code 产出代码里的错误，甚至用 Rescue 功能让 Codex 子代理修复应用特定部分，建议两个 agent"接力"（tag team）同一份代码。

**开发者自建"claudex"双模型代理方案** — @theo（Theo Browne，t3.gg）
https://x.com/theo/status/2076114415368482854
用 CLIProxyAPI 同时接入 Claude 与 Codex 鉴权，连到 Claude Code，再建一个设置若干环境变量的 claudex 别名，实现两个模型在同一套工具里协作。

**长文对比 Claude Code 与 Codex，数月实测后选择切回 Claude** — @Hesamation（知名 AI/ML 开发者与技术写作者）
https://x.com/Hesamation/status/2031494290531131762
基于数月实际使用经验，从任务模式、生态、长期可靠性等角度对比 Opus 4.6（Claude Code）与 gpt-5.3-codex，最终选择切回 Claude 生态；该文在中文圈被多个账号转发讨论。

**中文社区工具推介：Claude Code Bridge 可视化多模型协作** — @GitHub_Daily
https://x.com/GitHub_Daily/status/2013235079087350272
开源项目 Claude Code Bridge：基于 WezTerm 或 tmux 构建分屏终端，把 Claude、Codex、Gemini 等多个模型的交互过程并排可视化。

（Reddit 专项检索未找到相关真实讨论，如实标注未检索到，不编造）

### 1.5 第二轮更仔细的 MoreAPI 抓取（抖音 `search_video_v2` 已修复，含深化详情）

> 此前抖音 `search_video`（v1）因后端 `__ac_nonce` 报错一直不可用；`接口-代理` 会话接入快代理隧道后修复，本轮改用 `search_video_v2` 并对最相关结果额外拉取 `aweme_detail_v4` 完整详情（不只是搜索摘要）。

**一个工具让Codex和ClaudeCode协同工作** — 廖定强AI笔记 ｜ 2026-08-05
https://www.iesdouyin.com/share/video/7670476214083013926/
围绕一款开源工具（字幕中出现的名称为"Orco"）讲解如何让 Codex 与 Claude Code 协同工作：为每个 Agent 建立独立工作区避免代码互相覆盖、用统一工作台协调并展示各 Agent 任务状态、支持把 Codex 会话转移给 Claude Code 继续处理，可在手机端查看进度/回答 Agent 提问/审查代码改动，同时提醒需设置好 Agent 权限避免接触敏感信息。互动量不高（赞44/藏30），但"独立工作区+协调工作台+跨模型会话转移+权限隔离"几点与多 Agent 协同编码的核心问题直接对应。

**ClaudeCode配Codex的6种接线方式** — 阿森编程日记（AI自动化）｜ 2026-06-24
https://www.iesdouyin.com/share/video/7654827698430201115/
系统梳理 Claude Code 与 Codex 组合使用的六种机制：Sub-Agent、MCP、Worktree（Git工作树并行）、Hooks、互审（双模型交叉复核）、过夜（无人值守长跑）。获赞7829/藏7868/评255/转1911，是本轮同类内容互动量最高的一条。这六种机制与 Orca 项目实际采用的跨模型 Sub-Agent 派发、MCP 配置审查、Worktree 隔离、opus+sol/max 双模型独立复核逐一对应，是外部同类实践的独立印证。

**Agent这么多，普通人到底该怎么选？** — 秋实｜AI ｜ 2026-07-24
https://www.iesdouyin.com/share/video/7665860214725332278/
本轮抖音结果中唯一标签同时带 #Codex 与 #ClaudeCode 的视频，逐一评测 Workbuddy、Trae、Zcode/Kimicode、Claude Code、Codex/ChatGPT 六款工具：Claude Code"适合有开发经验的人，需解决账号/网络/支付/环境配置问题"；Codex/ChatGPT"模型能力强、性价比高、插件生态丰富，适合长期使用"。

（本轮 B 站补充检索未命中与"Codex/Claude协作"精确匹配的新结果，如实标注，未编造凑数。）

## 二、AI / Orca 本体优化

### 2.1 中文社交/短视频平台（MoreAPI → 哔哩哔哩，真实数据）

**搜索词「大模型自我优化」：**

- **不改变参数，让模型自我进化变得更聪明！** — UP主：宽哥琢磨AI，播放量：2917
  https://www.bilibili.com/video/BV1p9WRzMEjF
- **【手撕LLM面试题系列】大模型推理优化** — UP主：丁师兄大模型，播放量：9736
  https://www.bilibili.com/video/BV1RC411B7Tf
- **大模型优化手段** — UP主：福建省科技馆，播放量：1124
  https://www.bilibili.com/video/BV1NJtgztEdQ

**搜索词「AI agent 自我进化」：**

- **自进化 AI Agent 是怎么实现的？拆解 Claude Code、OpenClaw、Hermes Agent 的记忆系统** — UP主：AIJasonZ，播放量：3703
  https://www.bilibili.com/video/BV1tJLF6WEhb
  > 围绕「self-evolving agent」的项目综述：Auto Agent、Auto Research、Claude Code 的 Auto Dream、OpenClaw、Hermes Agent 等
- **🚀AI编程助手自我进化！Prime Agent颠覆传统AI编程** — UP主：AI超元域，播放量：10152
  https://www.bilibili.com/video/BV1Eugj6qE7h
  > 动态工作流、多Agent并行、心跳机制、长期自主执行任务、Token消耗大幅下降；RLM、Continual Harness 等关键词
- **Prime Agent：让 AI 长任务接着做，15.4k stars 的自我改进 Agent** — UP主：Charaplay，播放量：78
  https://www.bilibili.com/video/BV19ggc6DEkL
  > 项目地址：github.com/PrimeIntellect-ai/prime-agent

**搜索词「本体优化 AI」：** 命中的多是无关噪音（硬件配置/通用AI教程/模型跑分/广告课程/低俗内容等，已过滤），"本体优化"这个中文短语本身不是 B 站上的常见搜索用语，语义匹配效果差，仅以下两条勉强相关：

- **《我们优化并且削弱了AI》** — UP主：缺赞，播放量：53837
  https://www.bilibili.com/video/BV1Qxuc62EF1
- **我十分建议在你的AI中加上这么一句.....** — UP主：你最爱的鼠子哥，播放量：916966
  https://www.bilibili.com/video/BV1ToV76NECH
  > 系统提示词优化建议：避免过分夸赞、反复推敲、优先准确性、结构化输出——本质是对 AI 输出行为的"本体"调优

**补充搜索词「AI自我迭代」「多agent编排系统优化」（经 `moreapi.aistartools.com` 代理域名检索）：**

- **我做了一個會自我迭代的 AI 系統，它讓自己越用越強** — UP主：院长G大，播放量：378
  https://www.bilibili.com/video/BV1fw386kEuf
- **2分钟教你做一个可以自我迭代的 Agent** — UP主：karminski-牙医，播放量：3951
  https://www.bilibili.com/video/BV1v7j26NEyP
- **AI 最危险的想法：让它自己改进自己** — UP主：第四种黑猩猩CHIMP，播放量：13237
  https://www.bilibili.com/video/BV17JEb6ZE1L
- **AI开始造AI：2028年前或实现递归自我迭代！** — UP主：人工智能产业链union，播放量：142
  https://www.bilibili.com/video/BV1LEbe6FExv
- **多Agent无人值守跑了4天，14万行代码，怎么编排的？【B站AI创造公开赛】** — UP主：小天fotos，播放量：30718
  https://www.bilibili.com/video/BV1cuJH6LEvU
- **Agent 编排的四种模式：到底谁说了算？｜ 7 分钟看懂工业级多 Agent 系统** — UP主：Web3布道师Noah，播放量：10514
  https://www.bilibili.com/video/BV19D9eB9Etg
- **Pi Agent 扩展实战系列教程之 pi-dynamic-workflows：让多个 AI Agent 动态编排、并行协作** — UP主：程序员暮闲，播放量：6582
  https://www.bilibili.com/video/BV11yM96nE45
- **hermes agent多智能体编排系统：kanban** — UP主：huanyigntianhe，播放量：3804
  https://www.bilibili.com/video/BV1rqRJBcEtn

### 2.2 国际/中文技术社区 + 官方渠道（Workflow → 6 agent 并行 WebSearch/WebFetch）

#### 2.2.1 国际技术社区

**Harness Engineering for Self-Improvement** — Lil'Log（Lilian Weng 个人博客）｜ 2026-07-04
https://lilianweng.github.io/posts/2026-07-04-harness/
OpenAI 前研究负责人 Lilian Weng 系统论述"harness 工程"（围绕基础模型、决定其如何思考/规划/调用工具/管理上下文/评估结果的系统层）如何驱动 AI 的递归自我改进。三大设计模式：工作流自动化、文件系统持久化记忆、子 agent 并行执行；核心观点：近期 self-improvement 的实际路径不是模型直接改写权重，而是优化"模型周围的系统"。

**PrimeIntellect-ai/prime-agent — A self-improving RLM agent for coding workflows and long-running autonomous tasks** — GitHub ｜ 2026-08
https://github.com/PrimeIntellect-ai/prime-agent
核心是两个抽象：Recursive Language Model（把上下文当变量、递归子 agent 当函数调用，运行在持久 IPython 内核中）与 Continual Harness（把提示词、记忆、技能描述作为可被证据驱动、小步更新的持久状态）。内置 `/refine` 可在会话间复盘轨迹并对 harness 做可回滚的小幅改进。（对应 1.1/1.3 里 B 站的"Prime Agent"系列视频，官方一手来源。）

**Self-Harness: Harnesses That Improve Themselves** — arXiv ｜ 2026-06-08 提交，2026-08-20 最新版
https://arxiv.org/abs/2606.09498
提出无需人类工程师或更强外部模型介入、agent 自主改进自身运行 harness 的三阶段循环：挖掘失败模式→提出最小化针对性修改→回归测试通过后再采纳。三基准×三模型组合测得相对收益最高达 132%，且改进完全来自 harness 而非模型权重变化。

**Time Horizon 1.1** — METR ｜ 2026-01-29
https://metr.org/blog/2026-1-29-time-horizon-1-1/
AI 自主任务时长评测框架更新版，任务套件从 170 扩至 228 个。核心结论：模型 50% 可靠度完成任务的时长翻倍周期自 2023 年以来从 165 天缩短到 131 天，2024 年以来进一步加快到 89 天。

**[Bug] Subagent delegation lacks timeout, monitoring, and abort controls—caused 12+ hour session hang** — GitHub (anthropics/claude-code issues) ｜ 2026-05-22（已关闭 not planned）
https://github.com/anthropics/claude-code/issues/61405
真实用户报告：本应 10 秒完成的任务，因子 agent 反复自我循环连续抓网页，导致会话挂起超过 12 小时。指出子 agent 委托机制缺乏用户可配置超时、周期性检查点、用户主动中止能力三项关键控制。

**Post-mortem 2026-04-28: 12 multi-agent coordination bugs surfaced across a single autonomous-overnight cycle** — GitHub (anthropics/claude-code issues) ｜ 2026-04-28
https://github.com/anthropics/claude-code/issues/54393
详尽的多 agent 协调事故复盘，单次过夜自主运行周期内暴露 12 个 bug：完整性检查只验形态不验内容、无"规则单一权威来源"强制机制、上下文压缩后丢失近期指令、hook 设计无超时/深度追踪等。

**Agent tool: uncontrolled recursive subagent spawning — 5 planned agents escalated to 361+ completed background agents, burned full 5h usage quota** — GitHub (anthropics/claude-code issues) ｜ 2026-06-30
https://github.com/anthropics/claude-code/issues/72566
预期 5 个并行子 agent，因默认继承完整 Agent/Task 工具权限、且无递归深度或总调用数硬上限，最终产生 361+ 个子 agent，2 天内两次几分钟耗尽 5 小时配额。**与 Orca 自身 `agent_capacity.py` 红黄绿灯限流的设计初衷高度对应，是同类真实工程风险的独立第三方印证。**

#### 2.2.2 中文技术社区

**自进化（Self-evolving／RSI），一篇就够了** — 知乎 ｜ 2026-07-30
https://zhuanlan.zhihu.com/p/2065227313973825752
提出 Artifacts（产出物）/Harness（脚手架）/Model（权重）三层分类框架，列举 OpenAI GPT-5.6 的"RSI Index"指标、Anthropic《When AI builds itself》、腾讯混元 Hyra-1.0、MiniMax M2.7 自主跑 100 余轮"分析失败→改代码→跑评测"将性能提升 30%、Sakana AI 的 RSI Lab 等具体案例。

**Harness Engineering for Self-Improvement（面向自我改进的 Harness 工程）译文** — 知乎 ｜ 2026-08-09
https://zhuanlan.zhihu.com/p/2057943837826348906
Lilian Weng 原文中文全文译本，将 harness 与操作系统设计做类比。

**Agent Harness：AI 自我改进真正发生的地方** — 掘金 ｜ 2026-07-10
https://juejin.cn/post/7660494716576989247
同一篇博文的另一中文解读版本，从掘金社区侧印证这一话题在中文技术圈的传播热度。

**多智能体协同从"概念验证"到"规模生产"：2026企业智能体部署全景指南** — 知乎 ｜ 2026-03-25
https://zhuanlan.zhihu.com/p/2020234672798442229
MCP 协议负责模型-工具调用、A2A 协议负责智能体间通信协作，梳理顺序管道/并行竞赛/分层指挥/联邦共识四种协同范式，给出"3-4 个智能体是协同黄金上限"等工程经验法则（**注意：此结论仅单一来源，未见交叉验证**）。

**深入解析Claude Code的Subagent机制** — 知乎 ｜ 2026-04-03
https://zhuanlan.zhihu.com/p/2023342730818930579
基于源码的深度技术拆解：独立 Agent Loop 与上下文隔离、Fork 模式共享父上下文、三级工具可见性过滤、fire-and-forget 无锁并行执行、嵌套深度限制、SendMessageTool 跨 agent 双向通信、Worktree 隔离、跨会话持久记忆。

**开源一个 AI Agent 协作平台：让 GitHub Copilot CLI、Claude Code 等 ACP Agent 在一个团队里协作** — V2EX ｜ 2026-07-24
https://www.v2ex.com/t/1229617
"Agents Chat"项目，基于 ACP（Agent Client Protocol）标准协议，让异构编程 agent 组成角色化团队协作，支持 DAG 工作流、条件判断、并行处理、人工介入循环。

**Agent的新思路：构建多agent系统** — 少数派 ｜ 2025-07-29
https://sspai.com/post/101365
解读 Anthropic 官方多智能体系统实践：主智能体分派任务、多个子智能体并行独立研究再汇总，实测搜索效果比单智能体提升 90.2%；总结复盘、兜底、迭代、渐进上线四大稳定性机制。

#### 2.2.3 官方渠道

**When AI builds itself** — Anthropic
https://www.anthropic.com/institute/recursive-self-improvement
阐述向"递归自我改进"推进的进展：Claude 已能自主完成开放式研究任务，成功率半年内从约 26% 升至 76%；超过 80% 的合入生产代码由 Claude 编写；讨论"dreaming"机制（agent 系统性回顾自身历史提取可复用知识）。

**Long-running Claude for scientific computing** — Anthropic ｜ 2026-03-23
https://www.anthropic.com/research/long-running-Claude
展示 Claude Code 如何在多天连续会话中自主完成复杂科研任务，用 CLAUDE.md 做持久化项目蓝图、CHANGELOG.md 记录进展与失败尝试。

**Effective harnesses for long-running agents** — Anthropic Engineering Blog ｜ 2025-11-26
https://anthropic.com/engineering/effective-harnesses-for-long-running-agents
"初始化 agent + 编码 agent"两阶段 harness 设计：初始化 agent 建立功能清单与进度追踪文件，编码 agent 每次会话仅推进一个功能并为下一会话留下清晰产物。

**Managing context on the Claude Developer Platform** — Claude by Anthropic ｜ 2025-09-29
https://claude.com/blog/context-management
上下文编辑（自动清除过期工具调用结果）+ 记忆工具（跨会话持久信息）。内测显示两者结合使 agent 性能提升 39%。

**New in Claude Managed Agents: dreaming, outcomes, and multiagent orchestration** — Claude by Anthropic ｜ 2026-05-19
https://claude.com/blog/new-in-claude-managed-agents
"Dreaming"定期回顾会话与记忆库提炼模式；"Outcomes"允许开发者写成功评分标准供 agent 自我校正，任务成功率最高提升 10 个百分点；"多 agent 编排"让主导 agent 委派工作给专家 agent 并行工作。

**Run long horizon tasks with Codex** — OpenAI Developers Blog
https://developers.openai.com/blog/run-long-horizon-tasks-with-codex
GPT-5.3-Codex 一次连续运行约 25 小时、消耗约 1300 万 token、生成约 3 万行代码从零构建设计工具的实践案例。

**Introducing GPT-5.3-Codex** — OpenAI ｜ 2026-02-05
https://openai.com/index/introducing-gpt-5-3-codex/
称其为"第一个在训练自己过程中发挥关键作用的模型"——Codex 团队用早期版本调试自身训练、管理自身部署、诊断测试与评估结果。

**How we built our multi-agent research system** — Anthropic Engineering Blog ｜ 2025-06-13
https://www.anthropic.com/engineering/built-multi-agent-research-system
orchestrator-worker 模式：主导 agent（LeadResearcher）规划并派生 3-5 个专职子 agent 检索/解读信息，独立 CitationAgent 做引用校验。比单 agent Opus 4 提升 90.2%，但 token 消耗约 15 倍。

**Building multi-agent systems: When and how to use them** — Claude by Anthropic ｜ 2026-01-23
https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them
建议默认用单 agent + 合适工具，只有在①上下文保护②可并行化的独立研究路径③需要不同专用工具集/系统提示的专业化场景下才引入多 agent；多 agent 方案通常比单 agent 多耗 3-10 倍 token。

**Claude Managed Agents: get to production 10x faster** — Claude by Anthropic ｜ 2026-04-08
https://claude.com/blog/claude-managed-agents

**Scaling Managed Agents: Decoupling the brain from the hands** — Anthropic Engineering Blog ｜ 2026-04-08
https://anthropic.com/engineering/managed-agents
将 agent 拆分为"大脑"（Claude 及其 harness）、"手"（执行动作的沙箱/工具）与"会话"（事件日志）三个解耦部分，首 token 延迟降低 60%。

**Multiagent orchestration** — Claude Platform 官方文档
https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration
所有 agent 共享同一沙箱、文件系统与 vault 凭证，但各自运行在独立 session thread 中；协调者可对此前调用过的 agent 发送后续消息且该 agent 保留历史。

**Orchestrate teams of Claude Code sessions (Agent Teams)** — Claude Code 官方文档
https://code.claude.com/docs/en/agent-teams
Team Lead 生成 teammate、协调工作；每个 teammate 独立上下文窗口，通过 JSON 邮箱系统直接互相通信；共享任务列表支持依赖追踪与文件锁防止竞态领取任务。文档明确列出多项已知局限（会话恢复不支持 in-process teammate 等）。

**Introducing AgentKit** — OpenAI ｜ 2025-10-06
https://openai.com/index/introducing-agentkit/
Agent Builder——可视化拖拽式画布编排多 agent 工作流，配套 Connector Registry、ChatKit、Evals 与强化微调。

**Orchestration and handoffs** — OpenAI API Docs
https://developers.openai.com/api/docs/guides/agents/orchestration
两种模式：LLM 驱动的自主决策交接（agents-as-orchestrator），以及以代码显式编排的确定性工作流；后者对速度/成本/性能更可控。

---

#### 2.2.4 Twitter/X 补充检索（第二场 Workflow，补齐平台缺口）

**Autogenesis：自演化 agent 协议** — @omarsar0（elvis）｜ 2026-04-17
https://x.com/omarsar0/status/2045241905227915498
论文提出的"Autogenesis"自演化 agent 协议：agent 自行识别能力缺口、生成候选改进方案并验证。

**SkillOpt：自演化 agent 技能库的执行策略** — @amitiitbhu ｜ 2026-05-25
https://x.com/amitiitbhu/status/2058946895951933825

**FORGE：不更新权重的 agent 记忆自我演化** — @SciFi ｜ 2026-05-19
https://x.com/SciFi/status/2056868912268984353
在不更新模型权重的前提下，通过"种群广播"机制实现 agent 记忆的自我演化。

**"Harness Engineering"一词的早期提出/命名帖** — @dexhorthy ｜ 2025-11-04
https://x.com/dexhorthy/status/1985699548153467120
把 context engineering 原则应用到"如何使用一个已有 agent"这件事本身上，被普遍视为该术语的早期命名帖。

**好 harness 配平庸模型胜过差 harness 配顶级模型** — @addyosmani（Addy Osmani）｜ 2026-05-09
https://x.com/addyosmani/status/2053231239721885918
harness 涵盖 prompt、工具、上下文策略、hooks、沙箱、子 agent、反馈回路与恢复路径。

**Qoder 开源 harness 质量打分工具，自测仅 58/100** — @qoder_ai_ide ｜ 2026-07-28
https://x.com/qoder_ai_ide/status/2082104538849272016
可对 Claude Code / Codex / Cursor 等 agent 的 harness 质量打分（MIT 开源），自测两个维度低于 50 分，强调"模型周围的环境比模型本身更重要"。

**同模型换 harness，成功任务成本可差 5-30 倍** — @omarsar0（elvis）｜ 2026-08-04
https://x.com/omarsar0/status/2084714744880173451
同一模型、同一任务、同一 prompt，仅切换不同 agent harness，单次成功任务成本可相差 5 到 30 倍。

**HarnessX：能自我编译/自我改进的 harness** — @akshay_pachaar ｜ 2026-06-16
https://x.com/akshay_pachaar/status/2066886861235315008
提到 Anthropic 在更强模型上线时会精简 Claude Code 规划步骤、Manus 半年内重构五次 agent 以持续降低复杂度，HarnessX 尝试把这种人工调优过程自动化。

**反面声音：质疑 harness engineering 只是旧词新造** — @GenAI_is_real ｜ 2026-03-24
https://x.com/GenAI_is_real/status/2036266930290696599
认为大量相关长文疑似 AI 代写，质疑该领域是否只是不断给旧概念造新词——重要的平衡性反面材料。

**编排器本身成为多 agent 系统瓶颈** — @ablyrealtime（Ably Realtime）｜ 2026-04-06
https://x.com/ablyrealtime/status/2041082955120967791
多 agent 系统中 orchestrator 常成为瓶颈：本应并行的工作被强行串行化，agent 已完成任务但更新卡在单一控制平面之后。

**项目失败常因编排被低估而非模型不行** — @Redisinc（Redis）｜ 2026-02-12
https://x.com/Redisinc/status/2022037799969681505

**对多 agent 失败模式研究论文的读后感** — @pinglin02 ｜ 2025-03-21
https://x.com/pinglin02/status/1902891717826675063
针对论文《Why Do Multi-Agent LLM Systems Fail?》的读后感，是较早引发广泛讨论的可靠性质疑贴。

**"Magic Docs"：Claude Code 内部构建的自更新文档机制** — @mattyp ｜ 2026-03-31
https://x.com/mattyp/status/2038988217102266669
披露 Claude Code 内部构建中的"Magic Docs"机制：员工创建带特殊标记的文档后，内部版本会在空闲时自动触发子 agent、由后台 agent 持续补全文档——agent self-improvement 基础设施的一手爆料。

**agent-improvement-loop：挖掘 Claude Code / Codex 会话找摩擦点** — @cathrynlavery ｜ 2026-07-17
https://x.com/cathrynlavery/status/2078153739740012809
独立开发者每日挖掘 Claude Code 与 Codex 的会话记录，找出反复出现的摩擦点，将修复建议路由到工具/技能/记忆/上下文各层，且严格要求人工审核批准——**与"Orca 本体优化"场景高度相似的真实实践案例**。

（Reddit 专项检索未找到相关真实讨论，如实标注未检索到，不编造）

### 2.3 第二轮更仔细的 MoreAPI 抓取（抖音 `search_video_v2`，含深化详情）

**多Agent无人值守跑了4天，14万行代码，用了什么编排？** — 小天fotos ｜ 2026-06-14
https://www.iesdouyin.com/share/video/7650813418760359203/
独立开发者用多 Agent 系统"Niuma"无人值守连续运行4天、产出14万行代码、"用自己重构了自己"的实战案例：长任务为什么难 → Agent 的图（DAG）编排 → 专职的"编排者Agent"角色 → 可复用的 YAML 编排模板 → 环境也是编排的一环。获赞6544/藏6192/评397/转1525，本轮抖音结果互动量最高。**其"多Agent图编排+编排者Agent+YAML可复用模板+环境即编排一环"的架构与 Orca 自身的 Run/Task/Dispatch 编排、codex-pool 多agent容量控制、per-workspace 环境 recipe 高度同构**，可作外部真实案例参照；内容为作者自述实战心得，未提供可验证技术细节，仅供思路参考。

**四种多 Agent 编排模式一次讲透** — AI技术投降派 ｜ 2026-08-12
https://www.iesdouyin.com/share/video/7672247433363000617/
以"谁掌控控制流"为标准，归纳四种编排模式：Workflow（人工写死路径，成本最低）、Supervisor（主管动态拆分派发，视频指出这是当下生产环境默认选择，Anthropic 自身研究系统即用此架构，准确率比单agent高近九成但token消耗是普通对话15倍）、Handoff（控制权在agent间真正接力转移，源自OpenAI Swarm）、Council（多agent独立作答再收敛裁决，只该用于高风险决策）。**Orca 现有机制可对号入座：orchestration 的任务派发/协调循环对应 Supervisor 模式，CLAUDE.md 要求的"能力不足/安全发布/最终验收时对同一候选并行要求opus+max与sol+max独立只读双复核"本质上是用在高风险决策上的 Council 模式**；视频给出的成本权衡可作为审视 Orca 编排设计是否"小任务上了过重双复核"的外部参照。

**DeepSeek Harness 到底有多大的野心** — 兆文AI流 ｜ 2026-08-15
https://www.iesdouyin.com/share/video/7674170236429438208/
讨论 DeepSeek Harness 的真正野心不是"更聪明的模型"，而是模型之外那套工作流、插件系统、工具调用、运行审计日志和多智能体分层调度组成的"自动化底座"，与偏个人效率的 OpenClaw、偏长期陪伴的 Hermes Agent 做对比，指出其瞄准企业级流程自动化这一更大市场。视频提到的插件化内核、代码级工具组合、全链路审计日志、多智能体分层调度，**正是 Orca 作为跨模型编排底座（Run/Task/Dispatch、完成收据、agent容量分级）所要解决的同一类问题**。

**DeepSeek Harness 实测：把 Agent 拆成很多 Agent** — 零号码农 ｜ 2026-08-16
https://www.iesdouyin.com/share/video/7674619634016073359/
用 DeepSeek Harness 实测演示把单体 Agent 拆解为"插件式 Agent"架构，核心论点是"Agent 项目难维护的根源在于模型、工具、Skills 与文件系统被焊死在同一套外壳里，而非模型能力不足"。互动量较小（赞8）但观点直接支撑 **Orca 当前采用的插件化 Skills 体系与多模型（Claude/Codex）解耦调度设计的合理性**。

**B站补充结果（10 条，本轮新增）：**

1. **最近爆火的 Harness Engineering 到底是啥？一期讲透！** — code秘密花园，播放 472,123 — https://www.bilibili.com/video/BV1Zk9FBwELs
2. **Harness Engineering企业级多Agent协同项目实战！Multi-Agent+SandBox+自我进化的Skill+人工介入** — 马士兵官方账号，播放 136,987 — https://www.bilibili.com/video/BV1Cs7h6MEsX
3. **手撕 Claude Code 源码：从零理解 Agent Harness** — 清华大学计算机系科协，播放 44,277 — https://www.bilibili.com/video/BV18Uu36rEbu
4. **AI编程助手自我进化！Prime Agent颠覆传统AI编程** — AI超元域，播放 10,207 — https://www.bilibili.com/video/BV1Eugj6qE7h（与前几轮重复，Prime Agent 反复出现印证其代表性）
5. **DeepSeek Harness 多 Agent 协作插件开源！一条指令拉起 Agent Teams** — 程序员阿江-Relakkes，播放 5,362 — https://www.bilibili.com/video/BV1Y1bv68Eq9
6. **多智能体是 Agent 的邪路** — 老周AgentBuilder，播放 3,938 — https://www.bilibili.com/video/BV1aYuz6xEri（反面观点，值得平衡参考）
7. **基于hermes自研企业多智能体平台** — 大雷2023，播放 751 — https://www.bilibili.com/video/BV1MP3968EVy
8. **【斯坦福大学】自改进智能体 | 2026** — 无为斋主，播放 451 — https://www.bilibili.com/video/BV1Q4uZ6GEhZ
9. **Multi-Agent（多智能体）系统 的核心架构与应用场景** — 老金独立开发，播放 269 — https://www.bilibili.com/video/BV1vAgY61E1P
10. **AgentRadio：多智能体被动感知协作** — 恒星sol，播放 52 — https://www.bilibili.com/video/BV1fRM66KEKJ

---

## 三、综合观察（Workflow 合并 agent 撰写）

**关于 Codex 与 Claude 协作模式的趋势**

三类渠道的材料相互印证地收敛到同一个分工范式：**"Claude 负责规划/架构/审核，Codex 负责具体编码执行"**，以及其变体**"一写一审"式跨模型互审（dual review）**。这一模式在国际社区（Gist dual-review 脚本、Two-Agent PR Workflow）、中文社区（少数派三层 Skill 方案、掘金 OpenAI 插件解读、V2EX 多篇自制桥接工具）、以及官方渠道（OpenAI 官方 codex-plugin-cc 插件的 `/codex:review`/`/codex:rescue` 命令设计）中反复独立出现，可以判断这是当前跨模型协作实践中最主流、认可度最高的具体范式。

但需要注意三点边界：第一，arXiv 论文（2607.21656）的实证数据表明这种互审存在**方向不对称性**——Claude 审 Codex 效果显著（71.6%→89.7%），反过来 Codex 审 Claude 效果反而下降（91.4%→82.8%），这与 CLAUDE.md 里"Claude opus+max 与 Codex sol+max 双路独立复核"的强制规则思路吻合，但也提示单向互审未必等价于双向互审。第二，"两个 agent 实时对话式协作"（如 AgentBridge、Git-mediated conversation）目前**只是社区自制方案，没有材料显示 Anthropic 或 OpenAI 官方原生支持这种会话内实时互连**——2026-03 的 Claude Code 官方 issue（36871）明确以 not planned 关闭了这类诉求。第三，OpenAI 官方虽主动为 Claude Code 生态开发了 codex-plugin-cc 插件，但该插件是**OpenAI 自托管分发，并未被 Anthropic 官方插件市场收录**，现有材料**证据不足以支撑"Anthropic 官方也在反向为 Codex 做深度整合"**这一结论。

**关于 AI 自我优化 / Orca 本体优化方向的新方法与工具**

"Harness 工程是 AI 自我改进真正发生的层面"这一命题正从 Lilian Weng 的单篇论述，扩散为被两个独立中文渠道（知乎译文、掘金解读）转译传播、并有早期开源实现（PrimeIntellect prime-agent 的 `/refine`）与学术验证（arXiv Self-Harness 论文，132% 相对收益）支撑的共识候选方向。不过这些仍以论述文章和小规模实验为主，**"harness 自我改进已成为工业界标准做法"这一判断证据尚不充分**。相比之下，官方渠道给出的是更扎实的**产品级证据**——Anthropic 的 Managed Agents（dreaming 机制、outcomes 自我校正、上下文编辑与记忆工具）、"初始化 agent+编码 agent"两阶段长时任务 harness 设计，以及 OpenAI GPT-5.3-Codex 官方宣称的"参与自身训练调试"——这些已经在真实产品中落地。

在多 agent 编排系统架构上，官方文档（Claude Managed Agents 的会话线程隔离+共享沙箱/凭据、Claude Code Agent Teams 的邮箱通信+文件锁任务领取）与中文社区的源码级架构解读、METR 的 Time Horizon 数据（任务时长可靠度翻倍周期从 165 天缩短到 89 天）共同指向：**agent 自主处理任务的时间跨度在稳定变长，配套编排基础设施也在同步变得更系统化**。但与此同时，三份来自 anthropics/claude-code 官方仓库的真实用户 issue（12 小时会话挂起、361 个失控子 agent 耗尽 5 小时配额、单次过夜自主周期暴露 12 个协调 bug）清楚表明，当前多 agent 编排在**超时控制、递归深度限制、权限继承边界、状态校验机制**这些工程细节上仍有明显缺口——**这与 Orca 自身 `agent_capacity.py` 红黄绿灯限流、以及本会话记忆中提到的 `agent_prompt_stalled` 竞态问题属于同一类现实工程挑战，可以作为 Orca 本体优化的直接参照依据，而非仅是行业趋势的旁证。**

另外，知乎文章提出的"3-4 个智能体是协同黄金上限"这类具体数字化经验法则**仅有单一来源、未见其他材料交叉验证，证据不足以当作普遍规律**，只能作为一种业界观察参考，不宜直接套用到 Orca 的容量策略设计中。

---

## 四、链接核实说明（抽样 20 条，独立 Workflow 复核）

对本报告全部 105 个不重复链接抽样核实 20 条（覆盖 B 站、GitHub、arXiv、Hacker News、少数派、V2EX、Anthropic/OpenAI 官方博客、moreapi 代理域名等各类来源）。**结论：未发现任何虚构链接或不存在的域名**，报告的真实性底线是稳的；但约一半抽样样本存在主题匹配度偏松或抓取工具本身局限的问题，标注如下，供后续引用时参考：

**完全相符、无问题（10 条）**：`BV1yi876iExa`、`BV1677s6UErR`、`BV1x6Vt6dEef`、`BV1Fu7r6FEac`、`news.ycombinator.com/item?id=48345837`、`sspai.com/post/107162`、`v2ex.com/t/1231430`、`BV19ggc6DEkL`、`anthropic.com/institute/recursive-self-improvement`、`developers.openai.com/blog/run-long-horizon-tasks-with-codex`。

**存在问题/需要注意（10 条）**：

1. `moreapi.aistartools.com` 根路径直接返回 401 鉴权 JSON，无可读正文，本报告中仅作为"MoreAPI 基础设施可公网访问"的技术佐证，不作为内容证据引用。
2. `BV1ty5K6qERZ` 内容偏 Claude Code 单侧 agent 编排（Agent View、`/goal`），未明确涉及"Codex/Claude 协作"，仅泛化归入本体优化话题。
3. `BV1hmb26ZEws` 实际对比对象是"Claude Code vs DeepSeek Harness"，并非 Codex/Claude 协作，严格说主题错配（仍属 harness 话题可用作旁证）。
4. `BV1bU3d6TEPW` 内容偏 IDE 集成教程，未明显涉及"自我优化/harness/多agent编排"这类更深层话题。
5. `BV1e4uq6QEk5` 内容是 OpenAI 与 Anthropic 商业纠纷/公关八卦报道，**不应作为技术性证据引用**，仅可用于"行业动态"背景参考。
6. `BV1p9WRzMEjF` 仅泛泛涉及"模型自我进化"，WebFetch 未能取得完整简介与评论区内容，判断依据不足。
7. `BV17JEb6ZE1L` 内容偏 AI 自我改进的宏观科普/观点解读，未涉及 harness engineering 或 Codex/Claude 协作的具体技术细节。
8. `BV1rqRJBcEtn` 是泛化的多智能体编排工具介绍（kanban 任务调度），未明确提及 Codex/Claude 协作。
9. `github.com/anthropics/claude-code/issues/61405` 内容本身真实，但首次 WebFetch 因子模型把"2026"误判为未来日期而误报页面不存在，需用更明确 prompt 才确认——**提示自动化链接核验对日期处理不可靠，人工复核优先于自动化首次结果**。
10. `juejin.cn/post/7660494716576989247` 内容本身真实，但直接 WebFetch/curl 被字节跳动风控 JS 挑战拦截，只返回占位页，需改用 ego-browser 实际渲染才拿到真实内容——**提示常规抓取工具对该域名不可靠**。

---

