# Orca 启动编排与方法学习四维链设计

状态：P0 候选设计，未接入生产 SessionStart/UserPromptSubmit，未安装 Skill。

## 安全边界

- SessionStart 只能建立启动上下文、ACK、五项门禁、容量分类和计划壳；它不知道新的用户意图。
- 首个 UserPromptSubmit 只在瞬时内存中分类，输出枚举、风险标志和摘要哈希后立即丢弃正文。
- 不持久化 prompt、凭据、会话正文、工具正文、隐藏推理、capability secret 或 L0 原文。
- 自动规划不得自行扩大授权。持久 Run/Task/worker、外部写入、生产、删除、Skill 安装和记忆晋升继续经过既有权限或人工门禁。
- L0 候选与现有 AgentMemoryStore 隔离，默认关闭且不可召回；审批后只向现有受审 L1-L3 API 写 opaque reference。
- Graphify 是可重建投影，不是审批、撤销或真相源。
- 项目、候选 Skill、Graphify 投影和签名导出写 SSD；Orca runtime DB、socket、Keychain、provider session 和 capability verifier 保留在既有受保护运行态位置。
- SSD 迁移验收与 R2 客户端加密备份/恢复验收分别记账，任一 marker 不能替代另一条门禁。

## 启动与编排状态机

```text
BOOT
  -> CONTEXT_DELIVERED
  -> ACK_VERIFIED
  -> FIVE_GATES_EVALUATED
  -> CAPACITY_CLASSIFIED
  -> WAIT_FIRST_USER_TURN
  -> INTENT_CLASSIFIED_IN_MEMORY
  -> PLAN_PROPOSED
  -> AUTH_GATE
  -> RUN_BOUND
  -> DAG_CREATED
  -> WAVE_READY
  -> WAVE_DISPATCHED
  -> WAITING_RECEIPTS
  -> ACCEPTANCE_EVALUATED
  -> LEARNING_L0_CANDIDATE
  -> DONE
```

异常分支：

- storage、ACK 或 privacy 失败：`NACK_STOP`
- capability、账户、远端或生产目标不明确：`DECISION_GATE`
- capacity red：`COORDINATOR_ONLY`
- dependency cycle、跨 Run dependency、重叠写集合：`PLAN_REJECTED`
- 同一 Dispatch 连续三次失败：`CIRCUIT_BROKEN`
- 用户拒绝或撤回授权：`CANCELLED`

五项门禁的强制顺序：

1. 启动 bundle/challenge/provider/launch 绑定 ACK receipt。
2. Claude 与 Codex 使用同一 reviewed startup bundle/plan/memory pack，challenge 与 capability 必须各自独立；单 provider 时标记 `not_tested`，不能冒充同包 E2E。
3. privacy schema 拒绝 prompt/body/credential/session/tool output/hidden reasoning。
4. authority 与 freshness 分级：live、Git、human-approved、generated、historical；受影响能力出现 red/unknown 即阻断对应步骤。
5. cwd、Git common-dir、SSD UUID、symlink/realpath/storage boundary。

通过五门禁后才检查容量和单写者集合。每波只选择 ready、同 Run、无依赖环、写集合互斥的任务；默认并发 2，容量允许时最多 3，每波结算后重新检测。

## 四维链

### D1 任务/意图与步骤链

`Task -INTENT_HAS_STEP-> Step -NEXT_STEP-> Step`

只保存 `intent_class` 枚举和 `task_spec_sha256`，不保存原始任务正文。

### D2 因果/依赖链

`Step -DEPENDS_ON|BLOCKED_BY|CAUSED_OUTCOME|REPAIRED_BY-> Step|Outcome`

因果边必须有 evidence hash；普通时间先后不得自动升级为因果。

### D3 证据/结果链

`Step -PRODUCED-> Evidence -SUPPORTS|CONTRADICTS-> Outcome -ACCEPTED_BY-> Acceptance`

证据必须区分 focused、isolated、installed 和 real acceptance，不能把单测冒充真实验收。

### D4 时间/适用范围链

`Observation -OBSERVED_IN-> TimeWindow`

`Memory|SkillVersion -APPLIES_TO-> Scope`

演进边为 `VALID_FROM`、`VALID_UNTIL`、`SUPERSEDES`、`REVOKES`。Scope 与时间共同决定可用性，因此初期属于 D4 的强制节点，不拆成第五维。Actor 仅是 provenance facet，不能从 actor ID 推导权限；未来跨 OS user/tenant 的审计需要成熟后，才考虑第五审计维度。

## workflow-learning-event.v1

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "orca://schemas/workflow-learning-event.v1.json",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "record_id", "record_kind", "task", "step",
    "result", "time", "scope", "actor", "provenance", "learning", "privacy"
  ],
  "properties": {
    "schema_version": {"const": 1},
    "record_id": {"type": "string", "pattern": "^wle_[A-Za-z0-9_-]{16,80}$"},
    "record_kind": {
      "enum": ["observation", "acceptance", "review", "supersession", "revocation", "skill_candidate"]
    },
    "task": {
      "type": "object",
      "additionalProperties": false,
      "required": ["run_id", "task_id", "intent_class", "task_spec_sha256"],
      "properties": {
        "run_id": {"type": "string", "pattern": "^run_[A-Za-z0-9_-]+$"},
        "task_id": {"type": "string", "pattern": "^task_[A-Za-z0-9_-]+$"},
        "intent_class": {
          "enum": ["answer", "audit", "diagnose", "change", "build", "test", "browser", "desktop", "remote", "release", "monitor", "handoff", "unknown"]
        },
        "task_spec_sha256": {"$ref": "#/$defs/hash"}
      }
    },
    "step": {
      "type": "object",
      "additionalProperties": false,
      "required": ["step_id", "sequence", "action_type"],
      "properties": {
        "step_id": {"type": "string", "pattern": "^step_[A-Za-z0-9_-]+$"},
        "sequence": {"type": "integer", "minimum": 0},
        "parent_step_id": {"type": ["string", "null"]},
        "action_type": {
          "enum": ["inspect", "classify", "plan", "read", "search", "edit", "execute_test", "validate", "dispatch", "wait", "request_decision", "rollback", "deploy", "observe_runtime", "accept"]
        }
      }
    },
    "result": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "outcome_code", "evidence"],
      "properties": {
        "status": {"enum": ["succeeded", "failed", "blocked", "cancelled", "unknown"]},
        "outcome_code": {"type": "string", "pattern": "^[A-Z0-9_]{1,96}$"},
        "error_code": {"type": ["string", "null"], "pattern": "^[A-Z0-9_]{1,96}$"},
        "evidence": {
          "type": "array",
          "maxItems": 16,
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["kind", "sha256", "acceptance_class"],
            "properties": {
              "kind": {"enum": ["source", "unit_test", "integration_test", "installed_runtime", "real_traffic", "human_acceptance", "rollback", "receipt"]},
              "sha256": {"$ref": "#/$defs/hash"},
              "acceptance_class": {"enum": ["none", "focused", "isolated", "installed", "real"]}
            }
          }
        }
      }
    },
    "time": {
      "type": "object",
      "additionalProperties": false,
      "required": ["observed_at"],
      "properties": {
        "observed_at": {"type": "string", "format": "date-time"},
        "valid_from": {"type": ["string", "null"], "format": "date-time"},
        "valid_until": {"type": ["string", "null"], "format": "date-time"}
      }
    },
    "scope": {
      "type": "object",
      "additionalProperties": false,
      "required": ["scope_id", "kind", "selector_sha256"],
      "properties": {
        "scope_id": {"type": "string", "pattern": "^scope_[A-Za-z0-9_-]+$"},
        "kind": {"enum": ["host", "project", "repo", "worktree", "module", "environment", "service", "user_profile"]},
        "selector_sha256": {"$ref": "#/$defs/hash"}
      }
    },
    "actor": {
      "type": "object",
      "additionalProperties": false,
      "required": ["role", "actor_ref_sha256", "authority_claim"],
      "properties": {
        "role": {"enum": ["coordinator", "worker", "rule_reviewer", "human_reviewer", "runtime"]},
        "actor_ref_sha256": {"$ref": "#/$defs/hash"},
        "authority_claim": {"const": "provenance_only"}
      }
    },
    "provenance": {
      "type": "object",
      "additionalProperties": false,
      "required": ["startup_bundle_id", "dispatch_id", "source_kind", "event_sha256"],
      "properties": {
        "startup_bundle_id": {"$ref": "#/$defs/hash"},
        "dispatch_id": {"type": ["string", "null"]},
        "source_kind": {"enum": ["runtime_receipt", "worker_report", "acceptance_receipt", "review_receipt", "graph_projection"]},
        "event_sha256": {"$ref": "#/$defs/hash"},
        "memory_ids": {"type": "array", "maxItems": 8, "uniqueItems": true, "items": {"type": "string"}},
        "skill_version_id": {"type": ["string", "null"]}
      }
    },
    "learning": {
      "type": "object",
      "additionalProperties": false,
      "required": ["polarity", "state", "distinct_run_count", "success_count", "failure_count", "real_acceptance_count"],
      "properties": {
        "polarity": {"enum": ["correct_method", "failure_pattern", "neutral"]},
        "state": {"enum": ["l0_candidate", "corroborating", "review_ready", "l1_active", "l2_active", "l3_active", "rejected", "superseded", "revoked", "expired"]},
        "distinct_run_count": {"type": "integer", "minimum": 1},
        "success_count": {"type": "integer", "minimum": 0},
        "failure_count": {"type": "integer", "minimum": 0},
        "real_acceptance_count": {"type": "integer", "minimum": 0},
        "failed_condition_code": {"type": ["string", "null"]},
        "repair_step_id": {"type": ["string", "null"]},
        "unavailable_boundary_code": {"type": ["string", "null"]}
      }
    },
    "privacy": {
      "type": "object",
      "additionalProperties": false,
      "required": ["prompt_body", "credential", "session_body", "tool_output", "hidden_reasoning"],
      "properties": {
        "prompt_body": {"const": false},
        "credential": {"const": false},
        "session_body": {"const": false},
        "tool_output": {"const": false},
        "hidden_reasoning": {"const": false}
      }
    },
    "revokes_record_id": {"type": ["string", "null"]}
  },
  "$defs": {"hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}}
}
```

## 学习晋升规则

```text
L0_CANDIDATE
  -> REDACTION_VALIDATED
  -> CORROBORATING
  -> REVIEW_READY
  -> L1_PROCEDURE_OR_CONSTRAINT_ACTIVE
  -> L2_SCENARIO_PROPOSED
  -> L2_ACTIVE
  -> L3_PROFILE_PROPOSED
  -> L3_ACTIVE
```

旁路状态：`REJECTED`、`EXPIRED`、`SUPERSEDED`、`REVOKED`、`CONFLICT_REVIEW`。

正确方法进入 `REVIEW_READY` 的最低条件：

- 至少 2 个 distinct run；
- 至少 2 次成功；
- 至少 1 次 `acceptance_class=real`，或 installed-runtime 且含真实业务断言；
- scope selector 匹配；
- 没有 active contradiction。

错误方法第一次失败只写 provisional L0；至少 2 次独立复现才可进入 `REVIEW_READY`。必须保留 failed condition、unavailable boundary、repair step 和证据哈希。后续成功写 `CONTRADICTS/CONFLICT_REVIEW`，不改写历史；修复成功写 `REPAIRED_BY` 和新的适用范围。置信度只用于排队，不能自动审批、撤销或覆盖。

安全、生产、认证、凭据、删除或外部写入 constraint 必须人工审查。L2 至少依赖 2 个 active L1；L3 至少依赖 2 个已审批 L2，并需跨时间窗口稳定及人工确认。

## Skill 候选状态机

```text
MEMORY_ACTIVE
  -> SKILL_CANDIDATE
  -> DEDUPE_CHECKED
  -> CONFLICT_CHECKED
  -> VERSION_STAGED
  -> QUICK_VALIDATED
  -> ISOLATED_FORWARD_TESTING
  -> REVIEW_REQUIRED
  -> APPROVED
  -> MANUALLY_INSTALLED
  -> MONITORED
```

失败流：`REJECTED` 或 `ROLLBACK_READY -> ROLLED_BACK`。

- 相同 package digest：标记 duplicate 并链接既有版本。
- 同名不同 digest：标记 conflict，只生成 patch proposal 或新名称。
- 版本绑定 semver、package digest、source memory IDs、preimage digest、tree hash、目标拓扑和回滚包。
- 候选目录绝不直接覆盖 live Skill；安装前后都重新观察 digest。

推荐 Skill 名称：`review-orca-workflow-learning`。

SSD 权威源码目录：

`/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/skills/review-orca-workflow-learning/`

版本化隔离候选目录：

`/Volumes/Extreme SSD/Orca/review/skill-candidates/review-orca-workflow-learning/<semver>-<packageDigest>/`

建议结构：

```text
SKILL.md
agents/openai.yaml
scripts/validate_event.py
scripts/project_chain.py
scripts/build_skill_candidate.py
references/workflow-learning-event.v1.json
references/state-machines.md
references/acceptance-policy.md
references/rollback.md
```

## P0/P1/P2

P0 只做纯候选与 dry-run：

- 纯函数 classifier、privacy validator、five-gate evaluator、DAG planner、wave selector。
- `startup-plan --dry-run` 不安装 hook、不创建 Run/Task。
- append-only candidate journal 与现有 AgentMemoryStore 隔离，feature disabled，L0 不召回。
- settlement 后的 receipt 只消费 ID、outcome、typed error code、evidence hashes、time/scope hash。
- 离线四维投影和 trace/revocation 验证器。
- Skill staged candidate、manifest、quick_validate 和隔离 forward-test，不安装。

P1 隔离 Canary：

- disposable Orca profile 与临时 worktree；用户一次性开启 preview。
- decision gate 后才创建 Run/Task/worker，并按 live capacity 开 wave。
- 受审 promotion CLI 和独立 workflow-graph importer。
- crash/retry/idempotency、scope、provenance、revoke/supersede 级联测试。

P2 installed/runtime：

- 人工批准共享 loader/provider 配置和 exact build SHA。
- fresh Claude+Codex 同 bundle/plan/pack，不同 challenge/capability，ACK 完整。
- 至少两个真实任务重复成功，一个真实业务验收；另做失败、修复、边界更新。
- restart/crash/stale heartbeat/circuit breaker/gate/rollback 与 macOS/Windows/Linux 证据。
- SSD detach/locked/UUID/symlink fail-closed，以及 R2 最终增量与 reader 隔离恢复。

## P0 测试矩阵

1. Privacy：输入含 prompt、credential label、session path、tool output、hidden reasoning 和 absolute path；必须拒绝原值，只保留枚举和 hash。
2. Gates：ACK 缺失/错 bundle/错 challenge、同 challenge、SSD 越界/UUID mismatch/symlink；必须 NACK 且无 side effect。
3. Classifier：同输入得到稳定 enum/risk/plan digest；continuation 不重分类；unknown 进入 decision gate。
4. DAG/waves：cycle、跨 Run dep、重叠 write set、0/1/2/3 capacity。
5. Learning：单成功、双 focused、双成功加 real、单失败、双复现、失败后成功、scope/time 变化。
6. Graph：dangling edge、cross-scope、duplicate event、revoked ancestor、projection rebuild。
7. Skill：same digest、same name/different digest、existing tree drift、changed-during-read、oversize/path escape。
8. Incident failure fixture：`LIVE_SOURCE_HARDLINK_FIXTURE_FORBIDDEN`；单次事故只保留 provisional L0，边界为“provenance/xattr 测试仅允许独立临时文件或合成 manifest，绝不对 live source 建 hardlink，也不写受保护 xattr”。不得记录真实 App 路径或 xattr 原值。

## 源码集成点

- `orca-context-bridge/scripts/startup_context.py`：稳定 bundle、per-launch challenge、ACK receipt 和 fail-closed storage NACK。
- `orca-context-bridge/scripts/build_startup_bundle.py`：cwd/common-dir/UUID、Graphify hash/state、bundle identity 与脱敏渲染。
- 启动 bundle 的共享 identity 只接受 SSD `knowledge_root/.orca/context/reviewed-startup-pack-manifest.json`：该 manifest 钉住同目录 pack 的字节数/SHA，以及 Git、Orca capability、Wiki、Graphify catalog；Codex account/Claude 私有 registry 仅作为无路径隔离元数据附加在 identity 外。30 秒复用必须重新验证 Git、manifest/pack、Wiki/capability/catalog 和 Graphify 实体状态。
- 自动发现的 Codex account hook 不固化 account path，而以 `--require-codex-home-memory` 从当前进程自己的 Orca account `CODEX_HOME` 推导；缺失、显式混入或串号必须 NACK。仅当 UUID 账户名、常规 owner marker、同卷目标和无嵌套链接的目录链精确落到 `<expected-root>/local-homes/codex-accounts/<same-account>/home` 时，才允许本机内部账户 home 通过受控 symlink 强制落盘到 SSD；其他 symlink 一律 NACK。此处只是本地候选契约，未授权安装或迁移生产 hook。
- `orca-context-bridge/scripts/auto_index.py`：限时元数据索引；不得扩展为 session 正文学习器。
- `优化本机code/src/shared/agent-hook-listener.ts`：SessionStart 与首个 UserPromptSubmit 的真实边界。
- `优化本机code/src/main/runtime/orchestration/coordinator.ts`：ready wave、decision gate、maxConcurrent、convergence；当前自动 decomposition 尚不存在。
- `优化本机code/src/main/runtime/orchestration/db.ts`：same-run dependency、atomic dependent promotion、active dispatch lock、heartbeat 与 circuit breaker。
- `优化本机code/src/main/runtime/orchestration/lifecycle-reconciliation.ts`：task+dispatch-bound worker_done；学习投影必须丢弃 subject/body/reportPath/spec。
- `优化本机code/tools/orca-agent-memory`：immutable L0、reviewed L1、approved L2/L3 与 supports/contradicts/supersedes；L0 不得强塞进集成 store。
- `优化本机code/src/main/skills/skill-package-identity.ts` 与 freshness/update registration：bounded read、package/tree digest、path escape、unknown bytes 和 rollback identity。

## 人工授权点

- 安装、修改或删除 SessionStart/UserPromptSubmit hook；开启自动学习 feature flag。
- 持久 Run/Task/worker、跨模型调用、带成本 forward-test、生产/远端/账号/桌面动作。
- reviewer policy、阈值变更、安全/生产/凭据/删除 constraint、全部 L2/L3 approve/reject/revoke/supersede。
- cloud extraction/rerank、capability 签发/轮换/撤销。
- Skill 目标目录、install/update/rename/conflict/rollback；不提供自动覆盖选项。
- R2 backup/restore/delete、SSD cleanup、生产切换与真实验收结论。

## 当前不能声称的事项

- 自动 DAG decomposition 尚未存在。
- 当前集成 Agent Memory 不存 L0/procedure/constraint。
- 当前 Graphify bridge 是 code graph 契约，不是 workflow graph。
- fresh Claude/Codex 同窗口同包尚未重新验收。
- 尚无隔离 Canary、安装态、真实任务重复验收、Skill rollback 或最终 R2 reader 恢复证据。
