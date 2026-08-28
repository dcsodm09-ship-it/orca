# 本机 Orca 与 Prime Agent 集成缺口审计（2026-08-15）

最新刷新：2026-08-14T20:23:54Z。此文件是待复审工件，不是安装、激活或发布回执。

## 证据边界

- 已安装运行态：`/Applications/Orca.app` 版本 `1.4.182`；本轮 `orca status --json` 显示 runtime 与 graph 均为 `ready`。
- 启动上下文：`ORCA_CONTEXT_NACK_V1`，原因为 central reviewed wiki freshness mismatch。下列结论只依赖本轮可见文件和只读命令，不能声称中心上下文已加载。
- Git：当前工作树 `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`，HEAD `b88eae79570dce5af6ff978411f9eeca2a50899e`，分支 `完善orca`。本轮观察到 64 个 dirty entries；本工件没有整理或覆盖无关改动。
- 运行态、源文件、历史 staging、测试和 reviewer 报告分别记账，任一项不替代另一项。

## 已确认的未完成项

### 1. Codex 迁移后的内部盘敏感回滚副本仍未退役（P0）

当前 live Codex home 与两个受管账户 home 均已通过软链接解析到 Extreme SSD；但 `/System/Volumes/Data` 上仍保留 2026-08-11 生成的两个 `home.rollback-backup-*`。本轮只读统计为 5.3 GiB 与 5.4 GiB，共约 10.7 GiB。未读取文件内容，仅按路径和元数据确认其中有 2 个 mode `0600` 的 `auth.json` 与 1903 个 session 文件。扩展到本机所有名称匹配 `*rollback-backup*` 的 15 个顶层目录，合计约 16.77 GiB；其他目录未逐一断言内容敏感性，但同样属于迁移后的保留/清理债务。

这不否定 live home 的 SSD 驻留，但证明“内部盘旧凭据/会话副本已经退役”仍不成立。删除、移动或归档均属高风险恢复边界：必须先确认 SSD 权威副本与回滚保留策略，再使用可恢复、精确目标操作；本轮没有修改这些副本。

### 2. 有效治理入口仍是 fail-open v1（P1）

有效脚本 `/Volumes/Extreme SSD/Orca/local-homes/.codex/skills/audit-orca-governance/scripts/verify_governance.py` 仍允许调用者提供 `--startup-context-state`、`--capacity-script`、`--precheck-script` 和 `--skip-cli`，并且只有显式传入 `--require-green` 时才会因非绿色退出非零。

本轮在真实容量红灯下复现：输出 `governance_gate=red`、`max_new_workers=0`，进程仍退出 `0`。这会让只检查退出码的调用者误把红灯当成功。

批准的 SSD 预检本轮退出 `1`；逐项跟踪确认卷 UUID、APFS、FileVault、解锁、Owners 与写入条件均通过，唯一失败点是末尾要求容量只能为 green/yellow，而实时容量为 red。治理 verifier 正确记录该子检查 FAIL，却仍因 v1 fail-open 以总退出码 `0` 返回。

历史 v2 位于 `/Volumes/Extreme SSD/Orca/projects/orca/.orca/audit/staging/orca-governance-v2`，manifest 仍为 `activation_state=not_activated`，并保留 startup freshness、capacity drift、activation 和 dual-review 门禁。其 verifier SHA-256 `bf32d86bb0c507a70fcd678f463859e9ef75bf2fa686c41cb134d10017f2aa61` 与当前有效 verifier SHA-256 `967352205f272088af8d79ed4e3a98ddd7011e03ee2da11c5f4d21a673420a83` 不同，不能直接把旧 staging 叫作当前已验收修复。

### 3. 容量脚本调用了 GUI 模式而非 CLI 模式（P1，候选已修复、未安装）

修复前，候选 `orca-context-bridge/scripts/agent_capacity.py` 用签名 Orca Electron 二进制直接执行 CLI JavaScript，却没有设置 `ELECTRON_RUN_AS_NODE=1`。可复现结果是 Electron 命中 single-instance guard 后退出 `3`；同一命令经 `/Applications/Orca.app/Contents/Resources/bin/orca` 正常退出 `0`。

该错误使容量脚本报告 `diagnostics_available=false` 并降级为黄灯，掩盖了真实 Orca agent 数量。本轮候选已在净化环境中加入 `ELECTRON_RUN_AS_NODE=1`，并移除 `_open_trusted_path()` 的 `finally` 内 `return`，避免清理错误吞掉控制流。

修复后真实只读探针显示：`diagnostics_available=true`、103 个 worktrees、3 个 working worktrees、8 个 reported working agents，最终 `gate=red`、`new_workers_max=0`。因此本轮不得再启动 Sol/Opus worker。

8 条 `state=working` 中另有 4 条元数据不一致的 stale candidates：对应 worktree 均为 inactive、`liveTerminalCount=0`、无 attached PTY，`orca terminal list` 没有对应 worktree terminal，进程表也没有匹配其完整 worktree 路径；但 agent row 仍停在 2026-08-11 的 `working`。当前容量脚本会保守地全部计入。不得仅凭年龄自动清理或打成 done；需要 Orca 用 terminal/process/ownership receipt 做显式 stale reconciliation。即使暂时只作分析性排除，仍有 4 条实时 working，超过当前总上限 3，所以本轮门禁结论仍是 red。

候选尚未复制到已安装 skill；primary SHA-256 与 installed SHA-256 仍不同，治理入口会继续报告 `capacity-version-drift`。

同一旧字节（SHA-256 `280b80f895521de7e9498f036a6d35ad5576c84ed2dc7da4f44f2fef32f413e7`）还存在于 `startup-reviewed-pack-schema3-fastfix/promotion-bypass-policy-20260815-v4` 至 `v7` 的 staged runtime。2026-08-15 已通过 Orca 消息 `msg_3398ac1155d9` 将复现、修复哈希和 11/11 测试证据通知正在处理 v7 的协调者；没有直接修改其已指纹化候选。

### 4. 启动权威仍未闭环（验收阻断）

本会话明确收到 `ORCA_CONTEXT_NACK_V1`。在 ACK freshness 恢复前，不能把 `.orca/context` 目录、历史 ACK、manifest 哈希或静态测试称为本次模型已加载的中心权威上下文。

### 5. 已安装运行态与本 checkout 缺少可证明的源码同源性（P1 evidence gap）

已安装并正在运行的是 Orca `1.4.182`；本地共同 Git 仓库可见的最高版本 tag 仅到 `v1.4.178-rc.2`。当前 HEAD `b88eae7…` 与本地 `origin/main` `b3c4ba9…` 的 `git merge-base` 返回空并退出 `1`。本轮没有 fetch，因此这里只陈述本机 refs，不声称远端最新状态。

结果是：当前 checkout 的源码测试不能单独证明已安装 1.4.182 的行为。Prime Agent 候选已通过读取安装包内的 command detection、process identity、resume、agent-directory 支持文件并记录各自哈希来补足目标功能证据，但这仍不是完整的源码到二进制可追溯链。按全局约束，本轮不会通过重建或替换 Orca 来“修复”该差距。

### 6. Orca worker 资源账本仍有待处置项（运行维护缺口）

本轮 `orca orchestration worker-list --json` 的不同语义必须分开：资源历史共 315 条，其中 retained 88、released 193、reclaimable 17、release_unknown 15、active 1；2026-08-14T20:20:56Z 的另一个运行态 worktree 汇总报告 3 个 working worktrees、8 个 `state=working` agents。未逐一证明进程、pane、terminal 和 ownership 前，禁止按年龄自动关闭或删除。

### 7. `/usr/local/bin/orca` 注册仍未修复（人工管理员步骤）

`/usr/local/bin/orca` 是 root:wheel 的悬空软链接，仍指向已移除的 Orca.app 备份。当前 shell 的 `orca` 可通过 PATH 解析到 `/Applications/Orca.app/Contents/Resources/bin/orca`，但标准路径注册仍需人工 `sudo`，不得绕过 macOS 管理员认证。

### 8. Codex 账户与记忆根的强绑定仍未闭环（P1）

本轮 `orca account list --json` 显示 Codex `activeAccountId=null`，只有 system default；当前 shell 的 `CODEX_HOME` 未设置。全局 hook 则把 memory root 固定到该 system-default 账户，而当前 `codex` 可执行文件解析到另一个受管账户 home 下的 standalone release。可执行文件位置本身不等同于认证身份，所以不能据此断言串号，但这四个信号没有形成单一、可验证的运行态身份绑定。

更强的 `--require-codex-home-memory` 路径当前也不能直接用于 SSD 布局：`startup_context.py` 要求 `CODEX_HOME` 存在，并明确拒绝受管 home 或 memory root 为 symlink；本机受管 home 正是到 Extreme SSD 的 symlink。当前 hook 因而使用显式 memory root，而不是从本次进程身份推导。若 system default 漂移，静态 hook 可能继续指向旧账户，需以账户选择事件驱动的原子更新或受审查的物理路径身份机制修复。

### 9. Claude 到 Codex 的记忆桥仍是受限交接（安全边界，不应改成原始合并）

现状只有指定 Claude session 的 0600、裁剪、脱敏 handoff 与显式 Codex 记忆说明。原始 transcript/tool output/credentials/hidden reasoning 未导入，自动 raw loader 未部署。后续完善方向应是带来源、freshness、保留期和失效原因的 reviewed summary，不是跨账户原始记忆合并。

## Prime Agent 集成状态

2026-08-14T20:04:32Z 通过 GitHub 官方 releases API 实时核对：`latest` 仍为 `v0.7.2`，发布时间 `2026-08-11T19:00:48Z`，目标提交仍为 `83a0f9f9566219551fcb6ffaf7f519a815749a58`（<https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/v0.7.2>）。官方 `SHA256SUMS` 的四个发布包哈希也与只读 `plan` 输出逐项一致。因此当前锁定版本没有上游漂移；这里只执行了只读元数据和校验和查询，没有直接运行仓库源码。

四个官方 tgz 的 package manifest 均声明 MIT，但包清单都不含 `LICENSE` 文件；固定提交的根 `LICENSE` SHA-256 为 `b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0`。当前候选已把该固定文件加入 digest-verified 下载、release tree、收据、plan 与 E2E，从而不依赖发布包漏掉的许可证副本。

已安装 Orca 1.4.182 的可见支持文件包含 Prime Agent command detection、launch、process identity、resume 与 agent-directory routing。`prime-agent-integration/install_prime_agent.py plan` 本轮返回：

- `ok=true`；
- `conflicts=[]`、`recovery_conflicts=[]`、`pending_transaction=false`；
- 固定 Prime Agent `0.7.2` / commit `83a0f9f9566219551fcb6ffaf7f519a815749a58`；
- 固定 upstream MIT `LICENSE` SHA-256 `b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0`；
- command 默认 disabled，credentials 未配置，daemon 未启动；
- runtime/state/session 目标均位于 Extreme SSD。

四文件精确候选哈希：

- `prime-agent-integration/README.md`: `37bb9ddb914f203490331833ee68ac00d0695adb042015e88a814388a257cd19`
- `prime-agent-integration/install_prime_agent.py`: `bdad7e86211bebd44120c9c5ee2d4638c44d6f43f45cab2b1e1f9bf4f7d1f9de`
- `prime-agent-integration/tests/test_install_prime_agent.py`: `35d5fab2bfcbf70a12d7a11a7e7d7aee604e11b5bfdeb394fe35732356a7b1db`
- `prime-agent-integration/tests/sandbox_e2e.py`: `2fe1c17f7dd2fe9de11d7fe615abebcbbbca048d33771892c89fd9e67c3767fa`

该精确四文件候选尚无匹配哈希的 Sol/max 或 Opus 5/max 最终复审。2026-08-14T20:20:07Z 本轮重跑 56/56 单元测试，并完成无 override 的 cold-download sandbox E2E：`ok=true`、version `0.7.2`、production lock SHA-256 `f537ad6d7061987cd56faf2a268c0322b34c433ef7d257eb8052d54d3d2d224c`、license SHA-256 `b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0`、release tree 26907 entries、`real_user_state_changed=false`。唯一受管 Claude 账户 weekly usage 仍为 100%，因此 Opus 5/max 尚不可运行。

本 Run 早期确有一次 Claude Opus/max 独立只读调研（`task_202280c012f0` / `ctx_58c1aaaff661`），它针对旧三文件哈希报告 P0=2、P1=2，并保持候选不变；后续修复改变了所有相关候选哈希。该回执证明 Opus 参与过缺口研究，但不能替代当前精确候选的最终 Opus/max 复审。

对应的早期 Sol/max 精确审查 `task_2bf68aabc85d` 针对另一组旧四文件哈希报告 P0=2、P1=4、P2=1，包含恢复、软链接、`--cwd`、tree identity、disable race 与 E2E 断言问题；这些结果推动了后续多轮修复。最近的 Sol/max PASS `task_8eac13b6b851` 只绑定许可证补齐之前的四文件哈希；当前四文件均已改变，因此该 PASS 已失效，必须对上列新哈希重审。

真实 `~/.prime`、`~/.local/bin/prime-agent` 与共享 Prime Agent tool root 仍不存在。不得称为已安装、已启用或已验收。

## 本轮可逆修复候选

- `orca-context-bridge/scripts/agent_capacity.py` SHA-256：`1c35bc7011adff2289908dbab494652e62b91b6e578575b8e2c51cf9f514d800`
- `tests/test_agent_capacity.py` SHA-256：`cc7563caaee7e814a9c3c0c84f72173a58259858ffd6af2071528edcd8b24a43`
- `python -W error::SyntaxWarning` 编译检查：PASS。
- `python -m unittest tests.test_agent_capacity -v`：11/11 PASS。
- 真实只读容量探针：Orca evidence available，红灯，0 个新 worker。

此修复仍是 source candidate；未修改 `/Users/www1adwawd/.codex/skills`、`/Volumes/Extreme SSD/Orca/local-homes/.codex/skills`、Orca.app 或任何旧 staging 副本。

## 剩余验收顺序

1. 等现有 working agents 降到容量门禁允许的范围；不得在红灯下启动 reviewer。
2. 固定 Prime Agent 四文件、容量脚本、容量测试与本报告的最终 SHA-256。
3. 对同一精确候选依次运行 Codex `gpt-5.6-sol` max 与 Claude Opus 5 max 独立只读复审；任一可复现 P0/P1 均阻断。
4. 复审通过后，另行决定是否安装容量 skill 候选、是否执行 Prime Agent 的真实 `install`/`enable`。这些都是独立变更门禁，不能由测试或静态审查自动授权。
5. 恢复中心 startup freshness，并以新的启动 ACK 验证上下文真正加载；旧 ACK 不可替代。
