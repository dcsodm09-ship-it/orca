# W10 按 Worker Codex 账户绑定：静态验收记录（2026-08-14）

## 定位与边界

本记录仅审阅候选源码；没有修改 Orca 源码、应用包、运行数据库、会话、凭据或 `.orca` 内容，也没有启动应用、远程连接或执行 OAuth。

- 证据工作区：`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca`
- 证据工作区基线：`b88eae79570dce5af6ff978411f9eeca2a50899e`
- 候选源码工作区：`/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code`
- 候选分支／基线：`p0-ssd-r2-integration` / `6db25c5e9a9af14d1f32bf68eb625aab9943b71e`
- 候选状态：工作区仍有未提交变更；因此本记录不是发布、安装或启用许可。

## 审阅范围

候选新增 `--managed-account-ref`，意图是在本机新建 Codex worker 时，使用一个已验证的托管账户 home，而不切换用户的全局 Codex 账户选择。审阅覆盖 CLI、RPC 参数、联邦拒绝、账户目录归属、PTY 环境传递、分派回执和覆盖测试。

对候选改动运行了 `git diff --check`，无空白错误。

## 已验证的静态控制点

1. 本机限定：带 `--on` 的联邦 worker-start 会在创建任何远端效果前拒绝 `managedAccountRef`；不会把本机账户引用传给远端。
2. 新鲜 Codex worker 限定：复用既有 terminal、非 Codex agent、未知账户、WSL home 都会失败关闭。
3. 托管 home 归属：账户 home 必须位于当前 Orca 用户数据根下的 `codex-accounts/<account-id>/home`，解析后的规范路径不得进入系统 `CODEX_HOME`，并且必须有匹配账户 ID 的常规 `.orca-managed-home` 标记文件。
4. 凭据就绪：只检查 `auth.json` 的状态，不将凭据内容写入 worker receipt 或 dispatch `start_options`。
5. 进程传递：经验证 home 作为运行时内部 `codexHomePathOverride` 传递给 PTY；renderer 可调用的 `pty:spawn` API 不接收这个字段。
6. 回执最小化：运行时结果及 current-worktree 覆盖测试只记录 `{ requested, effective }` 的账户 ID；当前 worktree 测试明确断言 dispatch `start_options` 不含托管 home 路径。

这些证据支持“单次本机启动可定向到已验证托管 home，且正常回执不披露路径”的设计判断；它们不构成真实账户隔离、轮换完成或已安装运行时支持的证明。

## 尚未闭合的验收缺口

1. **新工作树无持久化测试不足。** `orchestration-workers-new-worktree.test.ts` 的用例标题声明“不持久化”，但只断言 `createManagedWorktree` 收到 home 和 RPC 回执含账户 ID；没有读取该 dispatch 的 `start_options` 并断言 home 路径不存在。应补上与 current-worktree 用例相同的数据库断言。
2. **全局选择不变只具静态证明。** resolver 是无副作用函数，PTY 也优先使用 override；但没有运行时级测试在已有不同的全局选择时验证该选择值前后不变、且子进程实际只得到 requested home。
3. **凭据竞态没有端到端覆盖。** 建议在账户 home 通过初始校验、随后 `auth.json` 变为不可用的场景下，断言启动失败且没有 worker dispatch／terminal 残留。
4. **尚未执行本轮测试、类型检查或打包。** 当前容量门为红色（负载超过逻辑 CPU），本轮没有启动重型验证；不能把旧测试结果当作这份候选改动的当前通过证据。

## 建议的最小收口顺序（仅供后续获准后在候选源码工作区执行）

1. 补齐上述三个针对性测试，尤其是 new-worktree `start_options` 路径不泄露断言。
2. 在容量门转绿后，只运行相关 Vitest 文件；记录实际命令、退出码和时间。
3. 执行目标 TypeScript／lint 检查，再进行打包前的静态审阅。
4. 将候选变更与本报告从现有脏工作区隔离、显式提交，并由独立验收者审阅。
5. 在获得单独授权前，禁止安装、启用 runtime capability、登录账户或修改 Orca 运行态。

## 当前结论

静态设计方向可继续，但 **不满足发布／启用门槛**：候选未提交，new-worktree 的路径不持久化断言缺失，且本轮未进行当前测试。下一步应先补候选测试，再由独立环境验证；这不需要也不应触碰已安装 Orca 底层。
