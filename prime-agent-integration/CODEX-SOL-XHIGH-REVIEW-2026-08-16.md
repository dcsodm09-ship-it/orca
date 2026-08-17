# Prime Agent 四文件独立只读复核 · Codex gpt-5.6-sol/xhigh（2026-08-16）

来源：Orca orchestration 正式派发（`run_3cdb6411dd63` / `task_8c03a3e15ba9` / dispatch `ctx_4c00981a9385`），
容量黄灯窗口内派出，只读复核，未修改/安装/激活/删除任何文件，未运行真实 install 或 sandbox E2E。

## 结论：NO-GO（P0=0，P1=4）

冻结候选有效：四个现场 SHA-256 与 `reports/orca-prime-agent-gap-audit-20260815.md` 第 87-92 行逐项一致——
README `37bb9d…cd19`、installer `bdad7e…f9de`、unit tests `35d5fa…b1db`、sandbox E2E `2fe1c1…76fa`。

启动处于降级态：中央 reviewed context 因 wiki freshness mismatch 未加载，本结论只使用现场四文件与报告哈希段。

## P1-1：符号链接任意文件覆盖

`install_prime_agent.py:1115-1139` 的 `make_patched_asset` 对已知输出名直接执行 `patched.open('wb')` 与
`chmod`，没有 `O_NOFOLLOW`、create-only 或目标身份校验。

**复现**：在临时 SSD 根创建最小合法 package tgz 和 release/assets；把预期 patched 输出名做成指向 SSD 外
victim 的符号链接；patch `SSD_ROOT` 与 `RELEASE_DIR` 后调用 `make_patched_asset`；函数会截断并改写 victim 为
gzip 内容，随后才可能在更晚的 `tree_digest` 阶段发现逃逸，外部损害不回滚。真实安装存在从 assets 目录创建到
四个 patched 输出生成之间的窗口；同 UID 进程可放入该链接。现有 56 个单测没有 `make_patched_asset` 输出占位者
或符号链接用例。

## P1-2：公共命令父目录 TOCTOU 可被静默验收

`install_prime_agent.py:1635-1665` 只按路径验证 `USER_HOME`、`.local`、`bin`，`atomic_symlink` 在 1676 后再按
路径 `symlink_to`；检查与使用之间没有固定父目录描述符。

**复现**：包装 `ensure_local_link_parent`，使其完成验证后把 `user/.local/bin` 改名并以指向 user 外 escape
目录的链接替换，再调用 `atomic_symlink`；`escape/prime-agent` 被创建且函数成功。`verify_link`（2283-2311）只
核对最终链接 inode、原始 target 与 target 的 SSD 归属，不核对 link 父链仍为已验证的非链接路径；
`prime_agent_command_candidates` 也用词法路径，因此 enable 的后验检查仍可通过。测试（842-991）只覆盖最终
路径占位者和链接 inode 换位，没有覆盖祖先换位。

## P1-3：pending 清除存在成功误报

`remove_private_file_durable`（378-408）将原文件 rename 到 quarantine 后只复核和删除 quarantine，从不确认
原路径仍为空；`finalize_pending_install`（2050-2051）随即返回成功。

**复现**：沿 tests:2096-2215 的有效 pending fixture，在 `rename_noreplace` 处理 `PENDING_PATH` 时先执行真实
rename，再于原 `PENDING_PATH` 创建新的 0600 文件；`finalize_pending_install` 会返回 receipt，state link 与
receipt 已提交，但 pending 仍存在，随后 `load_receipt`（2273-2275）立即拒绝。当前测试只在 publication 前插入
late occupant，未覆盖成功 rename 后的原路径再占用。

## P1-4：sandbox E2E 核心验收可被优化模式全部移除

`tests/sandbox_e2e.py:132-190` 与 223 使用裸 `assert`，191-205 在断言之后硬编码 `passed`、`True` 和
`real_user_state_changed=False`；README:191-193 的执行命令没有拒绝或清除 `PYTHONOPTIMIZE`。

**复现**：以 `PYTHONOPTIMIZE=1` 或 `python -O` 加载该脚本，并用 mock 让 install 返回含
`bin_target=/usr/bin/true` 及四个报告字段的假 receipt、verify/enable/uninstall/recover 返回空或错误结果；
所有生命周期断言被编译掉，`finally` 的真实路径污染门禁仍通过，脚本仍输出 `ok=true` 与
`enable_verify_disable_recover=passed`。不存在蓄意测试造假证据，但这会让验收在常见环境变量下产生可复现假
阳性；应改用显式检查并主动拒绝 `__debug__=False`。

## 交叉核对的次要问题（非 P1，一并记录）

- 单测有 56 个 `def test_`，README:195 仍声称 55。
- README:70-73 声称任何既有 `~/.prime` 或 receipt 都在 pending commit 前拒绝，但
  `finalize_pending_install:2035-2049` 会接受精确 state link 与完全相同 receipt 以支持幂等恢复。
- wrapper 的 runtime guard 单测使用 fake node，只验证参数排列；E2E 对真实 wrapper 只跑 `--version` 与
  `update`，未在真实 Prime parser 上验证 `agents`、`attach`、`model` 与资源禁用参数的语义效果。

## 整改门禁

1. patched 产物必须用同目录 O_NOFOLLOW 加 create-only 发布并绑定父目录身份。
2. 公共链接创建与验证必须绑定已验证父目录而非重新按路径解析。
3. pending 移除后必须复核词法路径缺席并将晚到占位者作为失败证据。
4. E2E 全部裸 assert 改为不可优化掉的显式失败。

修复后冻结新哈希，并对同一候选重新做 max-effort 双复核（Codex sol/max **与** Claude opus/max）；当前不得
安装、启用或称为验收通过。

## 执行边界

未修改、安装、激活或删除任何文件，未运行真实 install 或 sandbox E2E；末次四文件哈希、大小、mtime 与 inode
均与开工时一致。
