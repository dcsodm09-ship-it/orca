# AI超元域全量视频调研

状态：执行中；307 条元数据/仓库/Skill 证据已覆盖，旧候选已完成同包临时双审并被拒绝，完整视听学习与最终双模型验收尚未完成。

- Orca Run：`run_cee9d4cdf9be`
- 频道：`@AIsuperdomain`
- canonical channel：`UCIomFkAj4Vq_rGX2Jot7D8A`
- 首次清单观测：`2026-08-14T16:25:43.937Z`
- Videos 页：307 条，已提取 307 个有序 video ID 与 307 个非空标题，ID 顺序完全一致
- 视频总时长基线：189193 秒（约 52.55 小时），重复 video ID 为 0
- YouTube 证据包：307/307；304 条发布者简介非空，3 条发布者字段确认为空；共提取 3234 个描述章节和 812 个描述外链
- 播放器精确时长：189039 秒；卡片显示总时长多 154 秒，对应 154 条视频各向上显示 1 秒
- 字幕：Android InnerTube 全量探测 307/307；仅第 246 条有可用字幕，已保存 354 个片段和原始 XML；其余 306 条无字幕轨
- 逐视频介绍索引：307/307；304 条从完整发布者描述生成可读摘要，3 条明确保留 `publisher_empty`
- GitHub：验证 121 个唯一仓库；29 条有发布者项目专属直链，215 条为官方/精确/关键词主仓库候选，32 条仅链接频道通用仓库，2 条弱关联，15 条 Aila 产品谱系推断，14 条无可信公开仓库
- Skills：skills.sh 进行 9 组查询，共 80 个发现命中；按视频主题完成候选路由，但未安装、未执行，也未把热度当作批准
- GitHub README 深读：15 个 Orca 高相关主仓库，均记录 README blob SHA 和风险边界
- 独立审阅包：307/307；旧候选根 `5c966265…a309` 已由 Opus/max 与 Sol/max 独立同包复核，两个 reviewer 都拒绝放行；当前修订会产生新 packet SHA，最终双审仍需在 P0 与内容证据门禁关闭后重跑
- 独立 Shorts/Live 标签：未发现；对应 URL 回落到频道首页，不据此推断视频类型
- 研究要求：每条视频学习完整介绍、章节、字幕/播放演示、外链及相关 GitHub/Skills
- 审查要求：每条冻结证据包分别由 Claude Opus 5/max 与 Codex gpt-5.6-sol/max 同哈希独立复核

当前启动上下文为 degraded（`central reviewed source freshness mismatch: wiki`），不声称 Orca 上下文已加载。正式只读预检为 yellow，最多一次启动一个隔离 worker；因此本轮只做了串行的临时 reviewer 审阅，yellow 容量不关闭启动权威 P0，也不构成最终验收。此目录只保存协调器依据当前可见文件取得的公开只读证据和审查回执；不安装 Skill，不执行第三方仓库代码，不重建 Orca。

边界：发布者简介字段、时间戳、外链与播放器元数据已经覆盖全量 307 条，但它们不等同于完整视频内容学习。第 246 条只完成字幕所承载的口述内容分析，画面未验证；其余 306 条仍需语音与画面补证。旧候选的双模型审阅是有效缺陷证据，但最终双审仍被启动权威与内容完整性门禁阻塞。

## 主要产物

- [Orca 改进报告](ORCA-IMPROVEMENT-REPORT.md)
- [未完成门禁与恢复顺序](BLOCKERS.md)
- [逐视频介绍索引](inventory/video-intro-index.tsv)
- [逐视频覆盖状态](inventory/coverage.tsv)
- [逐视频 GitHub 路由](inventory/github-video-map.jsonl)
- [GitHub 覆盖摘要](inventory/github-coverage-summary.json)
- [GitHub README 深读](inventory/github-readme-research.json)
- [Skill 发现结果](inventory/skills-discovery.json)
- [按主题路由的 Skill 候选](inventory/skill-topic-map.json)
- [逐视频冻结审阅包](inventory/review-packets.jsonl)
- [审阅包 SHA 清单](inventory/review-packet-manifest.tsv)
- [Opus/Sol 同包双审协议](reviews/REVIEW-PROTOCOL.md)
- [旧候选临时双审回执](reviews/INTERIM-DUAL-REVIEW-RECEIPT.json)
- [本轮治理/容量预检回执](inventory/governance-preflight-20260815.json)
- [当前 Orca 基线快照清单](inventory/orca-baseline-source-manifest.json)
- [全目录内容清单](MANIFEST.json)
- [离线内部一致性回执](VALIDATION.json)

`scripts/build-derived-report.py` 仅从本目录冻结证据机械重建上述逐视频派生产物，不联网、不执行第三方仓库代码。`scripts/validate-report.py` 重算 307 个逐视频包和全目录 manifest；它的 PASS 不代表视听内容、双 reviewer 或 installed runtime 已验收。
