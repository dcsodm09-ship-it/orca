# 未完成门禁与恢复顺序

## B1：Orca 启动权威不新鲜（P0）

- 当前状态：`degraded / fail-closed`
- 精确原因：`central reviewed source freshness mismatch: wiki`
- 当前正式预检：`governance_gate=yellow`、`max_new_workers=1`；这只允许一次一个隔离 worker，不代表中央 reviewed context 已加载或 freshness P0 已关闭。
- 伴随警告：Orca 活动证据不可用，且 primary/installed `agent_capacity.py` 实现 SHA 漂移。
- 影响：不能完成、发布、安装、部署或把临时 reviewer 回执登记为最终验收。
- 恢复条件：修复精确 Git/wiki/manifest/route/installed identity 漂移后，重新运行批准的只读治理与容量预检，并取得新的 verified/fresh 回执。历史 yellow 或绿色回执不能复用。

本轮回执见 `inventory/governance-preflight-20260815.json`。

## B2：306 条视频无字幕轨，307 条都缺完整画面证据（P1）

- Android InnerTube 已探测 307/307，仅 `8WrfOGgGkck` 有字幕。
- 第 246 条具备 354 段完整字幕，可分析口述内容；没有冻结视频帧，因此截图、UI 状态、输出图像、医学质量和“超越 GPT-4o”仍未验证。
- 本机 Speech recognizer 可用且支持 on-device recognition，但授权状态为 `notDetermined`。
- 本轮没有触发系统权限 UI、没有安装 Whisper/第三方 ASR、没有下载 52.5 小时媒体、没有调用付费 API，也没有使用浏览器 cookie。
- 恢复条件：用户明确选择并授权一种内容补证路径，同时确认音频/转写保留策略、抽帧策略和磁盘预算：
  1. 本机 Speech 权限与离线识别；或
  2. 明确批准的本地 ASR runtime/model 下载；或
  3. 明确批准的外部转写服务、账户与费用。

## B3：旧候选双审已执行但被拒绝；新候选尚未最终双审（P0/P1）

- 旧候选：`MANIFEST.json` SHA `98ee8f4d78e161c5b308bf8a686ec1df6660fb57a5a7b68441483a95b40c8c0b`，文件根 SHA `5c9662658d901cde6e82bbb1cd94a30271bb93e9f2f006a4de34f2ef08eca309`。
- Claude Opus/max：307/307；报告 SHA `1a4ab2599fc8a3331dbb927e7ad84e40cdcfc992fa412f000aa5b775178efaaf`；`2 P1 / 5 P2`；候选 reject。
- Codex gpt-5.6-sol/max：307/307；报告 SHA `7e63a86459eedb9231d6eca3941abbe379840d9b971a9f59692f75c4b56c33bf`；`1 P0 / 5 P1 / 2 P2`；候选 reject。
- 两份完整报告已复制到独立历史审阅归档；主报告中的 `reviews/INTERIM-DUAL-REVIEW-RECEIPT.json` 绑定路径和 SHA。
- 当前修订修复旧候选中可控的协议、字幕边界、GitHub 分类、基线快照和状态标签问题，因此 packet SHA 将变化，旧 reviewer 不能放行新候选。
- 恢复条件：B1 关闭、B2 完成或逐条获得用户接受的不可获得例外后，对新候选重新运行独立 Opus/max + Sol/max 同包双审并完成 adjudication。

## B4：Orca launcher 兼容缺陷（P1 产品改进候选）

- 组合 `worker-start` 错误拒绝 `gpt-5.6-sol / max`，而 Codex CLI 0.147.0 的受跟踪自定义终端横幅确认该组合可运行；本轮未降级 effort。
- Opus `worker-start` 和 Sol `dispatch --inject` 都曾报告输入 accepted/injected，但任务文本仍停在 idle TUI；对各自精确终端补发一次 Enter 后才实际执行。
- 这些缺陷只登记为可逆的 Orca 改进候选；本轮没有重建、重装或替换 Orca。

## 恢复顺序

```text
B1 startup authority freshness
 -> approved capacity/governance precheck
 -> B2 audiovisual evidence acquisition or per-video accepted exception
 -> regenerate exact review packets
 -> B3 Opus/max + Sol/max independent review of the new hashes
 -> adjudication
 -> final report
```

在上述门禁关闭前，本目录的 `VALIDATION.json: PASS` 只表示本地报告结构、数量、哈希和回执绑定一致，不表示用户目标已全部完成，也不表示 installed/runtime/production acceptance。
