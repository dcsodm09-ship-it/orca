# Agent CLI 清理审计 — 2026-08-28

审计范围：Orca 识别的全部智能体 CLI 中，除 Claude/Codex/opencode(Grok)/Gemini 这几个明确在用的核心工具外，剩余 31 个逐一核查安装状态、真实占用空间、使用证据（日志/会话/进程/mtime），只读调查，未做任何删除或移动。

## 结论汇总

| 分类 | 数量 | 空间 |
|---|---|---|
| **可直接安全删除**（零使用证据） | 24 个 | **≈ 5.1 GB** |
| **建议移到 SSD，不删**（有真实但低频使用记录） | 6 个 | **≈ 2.7 GB** |
| **保留，不动**（近期活跃） | 1 个（gemini） | — |
| **未安装**（无需处理） | 2 个（claude-agent-teams, trae） | — |

两项合计，全部处理完可从内置盘释放 **≈ 7.8 GB**。

---

## 可安全删除（24 个，零使用证据）

| Agent | 真实路径 | 占用 |
|---|---|---|
| command-code | `~/.local/lib/node_modules/command-code` | 428 MB |
| kiro (Kiro CLI.app) | `/Applications/Kiro CLI.app` | 1.8 GB |
| aider | `~/.local/share/uv/tools/aider-chat` | 659 MB |
| cline | `~/.local/lib/node_modules/cline` | 321 MB |
| kilo | `~/.local/lib/node_modules/@kilocode/cli` | 251 MB |
| goose | `~/.local/libexec/orca-siliconflow-upstream/goose` | 250 MB |
| antigravity (`agy`) | `~/.local/bin/agy` | 158 MB |
| devin | `~/.local/share/devin` | 137 MB |
| crush | `~/.local/lib/node_modules/@charmland/crush` | 122 MB |
| kimi | `~/.local/lib/node_modules/@moonshot-ai/kimi-code` | 120 MB |
| omp | `~/.local/bin/omp`（独立二进制，共享目录内） | 125 MB |
| qwen-code (`qwen`) | `~/.local/lib/node_modules/@qwen-code/qwen-code` | 115 MB |
| droid | `~/.local/bin/droid` | 113 MB |
| mimo-code (`mimo`) | `~/.mimocode/bin/mimo` | 95 MB |
| mistral-vibe (`vibe`) | `~/.local/share/uv/tools/mistral-vibe` | 89 MB |
| continue (`cn`) | `~/.local/lib/node_modules/@continuedev/cli` | 79 MB |
| autohand | `~/.local/bin/autohand` | 67 MB |
| amp | `~/.amp/bin/amp` | 69 MB |
| aug (`auggie`) | `~/.local/lib/node_modules/@augmentcode/auggie` | 39 MB |
| ante | `~/.ante/bin/ante` | 28 MB |
| codebuff | `~/.local/lib/node_modules/codebuff` | 3.7 MB |
| rovo (`acli rovodev`) | `~/.local/bin/acli` | 15 MB |
| grok 旧版孤儿二进制 | `~/.grok/downloads/grok-macos-aarch64`（已被 1.0.5 取代，无引用） | 131 MB |

小计 ≈ **5.09 GB**。

**注意事项：**
- `omp` 与 `goose` 共享 `~/.local/libexec/orca-siliconflow-upstream/` 目录——只能删 `omp` 二进制本身和 `~/.omp/`，**不能整目录删**，也不能动里面指向其它在用工具（aider/cline/openclaw/opencode/pi/qwen）的 `*-original-link` 符号链接。
- `antigravity`/`aug`/`kiro`/`mimo-code`/`omp`/`qwen-code`/`mistral-vibe`/`rovo` 都是通过 Orca 内部命令覆写表（`app.asar` 里的 `detectCmd`/`launchCmd`/`cmd` 映射）才装上的，字面命令名本身在 PATH 里查不到，卸载时要按上表"真实路径"来。

## 建议移到 SSD（6 个，真实但低频使用）

| Agent | 真实路径 | 占用 | 最近一次真实使用 |
|---|---|---|---|
| hermes | `~/.hermes/`（含 857MB 的 state.db） | 1.4 GB | Aug 11（17 天前） |
| grok 独立 CLI（当前版） | `~/.grok/`（今日之前刚被 opencode-go 取代默认路由） | 636 MB | Aug 27 19:00（调研发起前 20 分钟内，非常新） |
| copilot | `~/.local/lib/node_modules/@github/copilot` | 324 MB | Aug 20 |
| cursor (`cursor-agent`) | `~/.local/share/cursor-agent` | 228 MB | Aug 22（就在本项目里跑过） |
| pi | `~/.local/lib/node_modules/@earendil-works/pi-coding-agent` | 168 MB | Aug 22（也在本项目里跑过） |
| openclaude | `~/.local/lib/node_modules/@gitlawb/openclaude` | 40 MB | Aug 21（配置被改过，但无实际会话证据，信号偏弱） |

小计 ≈ **2.73 GB**。这几个都没有原生的"改安装位置"环境变量（不像 Android SDK/pnpm/Homebrew 那样），只能用"整目录搬到 SSD + 原地放符号链接"的通用手法。

## 保留不动

- **gemini** — 3 分钟前刚有真实 OAuth 刷新和会话活动，明确在用。
- **claude / codex / opencode(grok)** — 核心工具，未纳入本次审计范围。

## 未安装（无需处理）

- claude-agent-teams（只有 Orca 自己的 tmux 集成垫片，4KB，不是第三方工具）
- trae（本机完全没有痕迹）

---

*方法说明：每个工具都核查了 `which`/覆写映射解出的真实二进制路径、`du -sh` 真实占用、`ps aux` 是否在跑、配置/日志/会话目录的 mtime、`orca-<name>-usage.json` 是否存在、shell rc 里是否有专门的 PATH/别名设置。凡是几个工具共用的"skills/ 自动同步"痕迹（每天都会无差别 touch 所有已配置 agent 的 skills 目录）一律不算使用证据，只有真实、工具专属的会话/日志内容才算。全程只读，未做任何删除或移动。*
