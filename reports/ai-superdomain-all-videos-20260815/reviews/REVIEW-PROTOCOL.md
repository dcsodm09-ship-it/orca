# 逐视频同包双审协议

每个 reviewer 先验证候选 manifest、`inventory/review-packets.jsonl` 和目标行的 `packet_sha256`，再读取该行显式列出、且具有 SHA-256 的报告内证据路径。允许的证据只包括包内字段以及 `youtube_evidence` 指向的发布者原始描述、字幕、原始 timed-text 和内容分析；路径缺失、越过报告根目录、SHA 缺失或不匹配时必须拒绝该包。

Claude Opus/max 与 Codex gpt-5.6-sol/max 必须绑定完全相同的候选 manifest、packet SHA 和证据文件 SHA。两位 reviewer 互相不可见：不得读取 `reviews/INTERIM-DUAL-REVIEW-RECEIPT.json` 指向的历史完整报告、另一个 reviewer 的 scratch/output、全局编排 inbox 或旧 adjudication。独立输出先写到不同的 task-private scratch 路径；协调器只能在两者都结束后归档。

## 独立输出

每位 reviewer 必须为 307 个视频各输出一条结论。可使用一个包含 `video_assessments[307]` 的机器 JSON，也可使用 307 个独立 JSON；两种形式都必须逐条包含：

```json
{
  "schema_version": 1,
  "video_id": "...",
  "packet_sha256": "...",
  "reviewer": {
    "provider": "claude-or-codex",
    "model": "opus-or-gpt-5.6-sol",
    "effort": "max"
  },
  "evidence_level_accepted": "caption-transcript-for-spoken-content-visuals-unverified-or-publisher-metadata-only",
  "introduction_assessment": {
    "publisher_claims": [],
    "repo_supported_claims": [],
    "unsupported_or_unverified_claims": []
  },
  "content_assessment": {
    "observed_claims": [],
    "missing_evidence": [],
    "must_not_claim": []
  },
  "orca_lessons": [],
  "findings": [
    {
      "severity": "P0-or-P1-or-P2-or-note",
      "code": "STABLE_CODE",
      "summary": "...",
      "evidence_pointer": "...",
      "reproducible": true
    }
  ],
  "verdict": "accept-evidence-boundary-or-reject-packet",
  "reviewed_at": "ISO-8601"
}
```

## 强制判断规则

- 发布者描述中的“最强、超越、生产可用、全自动、安全、低成本”等只能记为 publisher claim，除非仓库或完整内容证据独立支持。
- GitHub README 是上游自述，不是 benchmark、security audit 或 Orca compatibility proof。
- `generic_channel_repository_link` 仅说明描述链接了频道总仓库，不是视频项目身份；关键词候选、前代模型、同名仓库和 Aila 产品谱系也必须保留弱证据标签。
- 无字幕视频不得把描述摘要提升为“已观看/已验证完整视频内容”。
- 第 246 条只能依据完整字幕分析口述内容；截图、UI 状态、输出图像、医学质量、数据合规及“超越 GPT-4o”仍需要独立证据。
- reviewer 不得建议绕过用户授权、安装未知 Skill、运行上游脚本、导出 cookie/凭据或重建 Orca。
- `worker_done` 成功只表示审阅工作完成；候选 verdict、P0/P1 和启动/容量门禁必须单独判断。

## 模型与启动回执

- Opus 必须有 Orca receipt 显示 requested/effective 均为 `claude / opus / max`，或同等的受跟踪自定义终端启动证据。
- Sol 必须有 Orca receipt 或受跟踪终端横幅显示 `gpt-5.6-sol max`；组合启动器拒绝 provider CLI 支持的 max 时不得降级。
- 若注入回执声称 accepted、但 TUI 仍 idle 且任务文本尚未提交，可对该精确终端补发一次 Enter；不得重发任务或猜测另一个句柄。

## 仲裁

`adjudication/<video_id>.json` 必须同时绑定两个 reviewer 文件 SHA、原 packet SHA、候选 manifest SHA 和模型/effort 回执。任一 reviewer 的可复现 P0/P1 不得静默忽略；修复后生成新的 packet SHA 并重新双审。启动权威 degraded、视听证据不完整或候选自声明 blocked 时，历史 interim reviewer 只能作为缺陷证据，不能关闭最终双审门禁。
