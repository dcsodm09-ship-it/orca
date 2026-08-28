# W0/W10 源码边界清单（2026-08-14）

## 用途与边界

本清单将 `优化本机code` 中当前脏工作区的 W10「按 worker 绑定 Codex 托管账户」候选，与其他未追踪产物分开。它是后续隔离、审阅和显式暂存的依据，不是提交、安装或启用指令。

- 源码工作区：`/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code`
- 分支／HEAD：`p0-ssd-r2-integration` / `6db25c5e9a9af14d1f32bf68eb625aab9943b71e`
- 本轮行为：只读分类；未对源码工作区执行暂存、提交、重置、清理或测试。
- 静态格式检查：W10 已跟踪 diff 的 `git diff --check` 无输出。

## 可归属 W10 候选（17 个文件）

以下路径共同实现或验证同一条 `--managed-account-ref` 链路，应作为一个审阅单元，而不是与无关产物混合提交：

```text
src/cli/handlers/orchestration-worker-cli.test.ts
src/cli/handlers/orchestration.ts
src/cli/specs/orchestration-worker-specs.ts
src/main/ipc/pty.ts
src/main/runtime/orca-runtime.test.ts
src/main/runtime/orca-runtime.ts
src/main/runtime/orchestration/worker-account-binding.test.ts
src/main/runtime/orchestration/worker-account-binding.ts
src/main/runtime/rpc/methods/orchestration-federated-worker-start.ts
src/main/runtime/rpc/methods/orchestration-migration-behavior.test.ts
src/main/runtime/rpc/methods/orchestration-worker-start-receipt.ts
src/main/runtime/rpc/methods/orchestration-worker-start-schema.ts
src/main/runtime/rpc/methods/orchestration-worker-topology.ts
src/main/runtime/rpc/methods/orchestration-workers-new-worktree.test.ts
src/main/runtime/rpc/methods/orchestration-workers.ts
src/main/runtime/rpc/methods/orchestration.test.ts
src/shared/protocol-version.ts
```

其中 `worker-account-binding.ts` 与其测试当前为未追踪文件；其余为已跟踪修改。提交前必须再次确认这 17 个路径仍是完整且唯一的候选集合。

## 明确排除（不得随 W10 暂存）

```text
graphify-out/
tools/
```

两者都是未追踪目录，且目录清单显示它们不属于 W10 worker-account-binding 链路。当前不判断其质量、来源或是否应保留；仅要求它们不进入本候选的 index、提交或发布包。

## 仍需完成的门禁

1. 先补齐 W10 new-worktree dispatch `start_options` 不含托管 home 的测试断言；详见同日静态验收记录。
2. 容量门转绿后，运行相关 Vitest、类型检查和 lint，并保留真实退出码。
3. 从本脏工作区隔离出仅含上述 17 文件的审阅单元；不使用 `git add -A`、不清理排除目录。
4. 由独立验收者审核路径脱敏、全局账户选择不变与凭据竞态；通过后才能讨论提交或安装。

## 当前容量阻断

本清单生成前的实时容量检查为红色：8 个逻辑 CPU、1 分钟负载 24.875、14 个已报告代理，建议 `new_workers_max = 0` 且仅协调者工作。因此没有启动新代理或重型测试。
