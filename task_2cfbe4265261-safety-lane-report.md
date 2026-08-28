# task_2cfbe4265261 Safety Lane 实施报告

## 结论

已在 SSD 源包 `/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code/tools/orca-agent-memory` 实现未部署的源码候选：经人工/管家显式复核的 L1 `constraint` 进入不受 query/top-k 过滤的 `safety_items` lane。本候选没有迁移或启动生产 memory store，也没有修改已安装 venv、manifest、hook 或 capability 实现。

## 五项门禁

1. 已安装 venv：未读写；`py_compile` 用临时 `PYTHONPYCACHEPREFIX`，随后删除临时 pycache。
2. 生产 memory store：未打开、未读取生产 L0 或会话正文；测试仅使用 `TemporaryDirectory` state fixture。
3. manifest：`graphify-out/manifest.json` SHA-256 前后一致 `2b7c166717f187b7a360969c0aa355d2cff2be5fb2a1c8564081b1efeda12512`。
4. hook：`context_pack_hook.py` 为 `797b3bfd0336a6e0188472121107f97114276e23ac11950431c14cad086ad562`，`hook_registration_artifact.py` 为 `ae17e42a6f3b068e78633ad1bc996f227e7afeececad23c0775c9f08ca4177ff`，前后一致。
5. capability：`agent_capability.py` SHA-256 前后一致 `2e34f7c47e8b26773e82478d6580f1eb331b74f4a9058f55d34f08056fd8a289`。

工作目录与源包均位于 `disk4s1` / Extreme SSD；父仓库当前将整个 `tools/orca-agent-memory/` 视为既有未跟踪目录，因此报告使用文件级字节数与 SHA，不伪造 Git diff 证据。

## 实现证据

- `memory_storage.py:101-105, 908-931, 2404-2447`：schema v11 和不可更改/删除的 `memory_safety_constraints` truth table；v10 迁移创建空 metadata，不自动分类旧 constraint。
- `memory_storage.py:3934-4035`：`approve(..., safety_constraint=False)` 默认保持旧行为；只有显式 boolean `True` + L1 `constraint` + 已有 confirm/review-secret/reviewer 门禁才能写入 metadata。
- `memory_storage.py:4686-4708, 5515-5706`：safety lane 不走 query/search，按 `reviewed_at ASC, memory.id ASC` 稳定排序；安全项从普通 L1 搜索中排除，避免双重注入。
- 独立硬上限：safety 最多 8 项、4,000 UTF-8 字节；任一上限或最终 6,000 字节 envelope 无法容纳全部 safety 时直接 `StorageError` fail closed，不静默丢项。
- 普通 `items` 仍保留 L3=1、L2=2、L1=5 的分层配额与 top-k 行为；空 query 时 safety 仍全量返回，普通 query 候选为空。
- `memory_mcp_stdio.py:41-55, 495-569`：当前 MCP 验证新 `safety_items`/policy/digest，并继续接受旧 V1 无 safety lane 封包；不需改 hook 文件即可渲染小于 7,000 字节的现有 hook context。
- `memory_daemon.py:370-380`, `orca_memory.py:210-220, 1450-1457`：daemon 只接受显式 boolean metadata，CLI 新增默认关闭的 `--safety-constraint`。

## 文件字节与 SHA-256

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `memory_storage.py` | 360534 | `d2862861f9b481b75bf71ea8dbd0334f3b7fac83651daa478374f3784f5398a1` |
| `memory_daemon.py` | 32840 | `f99e9c7f053d0a76d300f8ddf17d264ee5063d17712a52136a986fa25a3a83fd` |
| `memory_mcp_stdio.py` | 35112 | `37132854b3919cea7e54624c11a61621feb6b238832fe0b1ad3fee6ea81da0b9` |
| `orca_memory.py` | 91167 | `7546a8bd15ad8cc910ef23d22770493cbc0568537ae8c7a610397c1679e2a39a` |
| `tests/test_agent_capability.py` | 39187 | `9f05e468b97fddf18b4175c759bb16e789759735d42fb3ce0766ef11619515d9` |
| `tests/test_audit_checkpoints.py` | 12035 | `9cfeede6dd787c28b21137b4d052fdcf1ba678cfdf00d5e6a827bea93cd78b45` |
| `tests/test_memory_mcp_stdio.py` | 18155 | `402ea0aa352de8623c5e819759340640353015cb7b3263d5d435f519930d6ed9` |
| `tests/test_safety_context_pack.py` | 11135 | `d5a8ac21305ae8d1911124437aa272fd34d2287aa736cabb157ad63c2f5a9acb` |

`memory_storage.py` 预变更 SHA-256 为 `14e585161714c1a322e619ed821b78c3667602df857835ebc80808cdf5a3a3a0`。

## 测试与检查

- 最终全套：`PYTHONDONTWRITEBYTECODE=1 make test` -> `Ran 249 tests in 106.893s`, `OK`。
- 新 fixture：7/7，覆盖 8 条 safety + 48 条闲聊、无关 query、空 query、12 次/4 worker 并发读、重复/撤销/替代/过期过滤、普通 constraint 不自动安全化、fact/procedure 防伪晋升、9 条超限 fail closed、v10 空 metadata 迁移。
- `python3 -m py_compile` 已检查 8 个改动 Python 文件；临时 pycache 已删除。
- 源包未配置 Ruff/Black 且当前主机无该命令；已执行尾随空白与 Tab 缩进检查，均通过。

## 仍需人工的 metadata 阻断

这不是生产迁移完成证据。v10→v11 只创建空的受审 metadata 表，不会把任何旧 `constraint`、`fact` 或 `procedure` 自动标为 safety；已经 approved 的旧记录也不能通过只允许 proposed memory 的 `approve` 原路径补标。上线前必须由人工/管家给出确切清单，并在受审、幂等、有回执的专用迁移入口中写入 metadata（或从可追溯 L0 重新提议/批准）；本任务未实施该人工选择、未迁移生产 store、未部署。
