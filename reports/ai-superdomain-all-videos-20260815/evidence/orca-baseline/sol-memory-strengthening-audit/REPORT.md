# Orca 全局自动增强学习记忆与关联强度：Sol 独立架构审计

审计日期：2026-08-11（Asia/Taipei）  
结论：`GO_OFFLINE_CANDIDATE_ONLY`  
运行态、安装、hook、settings、memory DB、Graphify 写入、Skill 安装与发布：`NO_GO`

## 结论摘要

Orca 已有一条安全性不错的离线学习骨架：严格脱敏事件、L0 不激活、两次独立 Run 阈值、真实/installed acceptance、四维链、真值祖先和撤销传播、content-addressed Skill staging。当前缺口不是“再加一个向量库”，而是把这些治理规则接入真实 recall/pack 决策，并闭合 source → wheel → installed runtime 的同一身份。

最高优先级有三项：

1. live、SSD authority source、旧 wheel 三者字节不一致，旧 wheel 还缺 `context_pack_hook.py` 和 source 已有的 safety lane；当前无可发布包。
2. live `search/context_pack` 没有执行 local-first → abstain → Sol fallback → 首次仅 L0 的状态门；Sol 也没有 provider/privacy/authority/cost admission receipt。
3. 四维链中的 `CONTRADICTS`、`REVOKES`、truth ancestry 尚未参与运行态检索硬过滤；运行态 graph boost 只是 lexical top-10 的近邻布尔加分，不能称为关联强度治理。

本目录提供的 evaluator/schema 只是确定性、无正文、无网络、无副作用的离线候选。它不修改任何现有 Skill 或 runtime。

## 0. Fail-closed 前置门禁与审计边界

### 五门禁复跑

| 门禁 | 只读实测 | 结果 |
|---|---|---|
| 外层工作树 | `pwd` = `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca` | PASS |
| Git common-dir | `/Volumes/Extreme SSD/Orca/projects/orca/.git` | PASS |
| SSD UUID | `6EF3720E-C57F-4352-BA47-CFE88BFEFDA7` | PASS |
| SSD 属性 | APFS；FileVault Yes；Locked No；Owners Enabled；External SSD | PASS |
| Orca | runtime `ready`；graph `ready`；app 1.4.179 | PASS |

这些门禁只授权本次离线审计，不授权安装或运行态变更。

### 数据与写入边界

- 已完整读取：`SKILL.md`、`state-machines.md`、`rollback.md`、`memory_eval.py`、`project_chain.py`、`validate_event.py`、`skilld.py`、`startup_plan.py`。
- 已只读审计：`scripts/complete_memory_injection.py`，以及 live/SSD authority source 两份 `memory_storage.py` 的 `search/context_pack` 和直接相关常量。
- 未读取：memory DB 内容、凭据、capability secret、session/prompt/tool 正文、隐藏推理。
- 未修改：原 Skill、live runtime、SSD authority source、wheel、hook、settings、manifest、memory DB、Graphify 输出。
- 本次新增文件只在 `sol-memory-strengthening-audit/`。

## 1. Exact 现状、优点与 P0–P2 差距

### 已有且应保留的能力

- live recall 只查询 `approved` 且未过期记录，并可精确 scope/layer 过滤；context pack 明确选 L3/L2/L1，没有 L0 lane（live `memory_storage.py:4572-4594, 5393-5425`）。
- context pack 绑定 capability、audit checkpoint、query HMAC、pack SHA 和 receipt HMAC，并设 `reference_only_never_execute`。
- `validate_event.py` 拒绝 prompt、credential、session body、tool output、hidden reasoning、absolute path；一次观察仍不激活（`validate_event.py:265-280, 343-368`）。
- 正确方法达到 review-ready 需要两个不同 Run 的成功和至少一次 installed/real business acceptance；失败模式需要两个不同 Run 的失败（`validate_event.py:329-368`）。
- `project_chain.py` 会对 truth ancestor、event dependency、expiry、revocation 做传递失活，历史节点仍保留（`project_chain.py:290-346`）。
- `skilld.py` 只写新的 immutable staging 目录，按 payload digest 去重，同 semver 不同字节冲突，且不安装 live Skill。

### P0：在任何 runtime/发布候选前必须关闭

#### P0-1 — source / wheel / installed 身份断裂

| 对象 | bytes / SHA-256 | 观察 |
|---|---|---|
| live installed `memory_storage.py` | 351373 B / `14e585161714c1a322e619ed821b78c3667602df857835ebc80808cdf5a3a3a0` | 普通 pack 8/6000、1/2/5；无 safety lane |
| SSD authority source `memory_storage.py` | 360534 B / `d2862861f9b481b75bf71ea8dbd0334f3b7fac83651daa478374f3784f5398a1` | 新增 safety lane 8/4000；整个 sidecar source 当前 untracked |
| source 目录旧 wheel | 127566 B / `f3a889d8d181eb1fb3de15283a96c2901435034ecad6fe50f36c7c4aac38bf88` | 内含旧 `memory_storage.py` SHA `c974140d…d07`，缺 `context_pack_hook.py` |
| SSD 0700 独立重建 wheel | 160428 B / `d470da5c4300d4c3b3b68c26549904eaeebc94390a421cc2c5199444cde024a3` | 与 source 的 storage/MCP/hook/safe_parent 四文件 exact；9/9 console entrypoints 在隔离 venv PASS；未安装 |

旧 wheel 的 `entry_points.txt` 声明了 context-pack hook，但 payload 中没有实现文件，因此入口声明本身不是可运行证明。独立重建表明 packaging 修复可行，但它仍是未安装的本地候选；wheel 0600、candidate/venv 0700 的权限归一化也必须成为可重复 build contract，不能靠事后 chmod。上表 9/9 结论来自并行的独立离线 packaging receipt；本审计只独立复核了 wheel SHA 与 storage/MCP/hook/safe_parent 四个 member 的 SHA，没有重跑该候选的 entrypoints。

发布前必须由一个 receipt 同时绑定：clean tracked source SHA、wheel SHA、wheel payload manifest、9 个 console entrypoints、安装后 exact bytes、policy、rollback preimage、platform/package smoke。任一不一致都 fail closed。

#### P0-2 — local-first / Sol fallback 不存在于可执行合同

`startup_plan.py` 负责 ACK、privacy、authority、storage、risk、capacity 和 worker wave，但没有 memory truth snapshot、local recall quality、abstention 或 Sol admission。`context_pack()` 总是尝试组包；低置信、前提不足和冲突没有结构化 route。

目标合同必须是：先用本机已审批未撤销 L1–L3 + Skill + active Graph projection；只有 `NO_ELIGIBLE_LOCAL_MEMORY`、`LOCAL_CONFIDENCE_BELOW_THRESHOLD`、`DIMENSION_COVERAGE_INSUFFICIENT` 或 `ACTIVE_CONFLICT` 才进入 Sol admission。Sol 首次输出只能是 `l0_candidate`、`activation_permitted=false`。

#### P0-3 — runtime recall 没有四维关系与撤销的硬门

当前 live 算法：FTS top-160 + 最近更新 top-160 中 cosine ≥ 0.45 的候选，之后 lexical/vector RRF；graph 只查看 lexical top-10 的一跳近邻并统一加 `1/70`（live `memory_storage.py:4595-4638`）。问题包括：

- graph neighbor 不会进入 candidate set，只能给已经入选的候选加分；
- 所有关系没有 type、dimension、confidence、direction、validity 或 provenance 区分；
- `CONTRADICTS`、`REVOKES`、`SUPERSEDES`、inactive ancestor 不是 hard exclusion；
- score ≥ 0.020 不是经校准的置信度，也没有 margin、coverage 或 reason-coded abstention（live `memory_storage.py:4635-4656`）。

四维链已在离线投影中存在，但尚未成为 recall authority。Graphify 只能是 projection，append-only journal/truth registry 才能决定 active/inactive。

#### P0-4 — safety lane 与注入验证器的版本合同未闭合

SSD source 定义 regular pack 为 8 items / 6000 B、L3/L2/L1 = 1/2/5，并新增 safety lane 8 items / 4000 B（source `memory_storage.py:151-158`）。live 缺该 lane。与此同时 `complete_memory_injection.py:603-648` 只读取 `pack.items` 并要求其中同时有 L1/L2/L3，完全忽略 source v2 的 `safety_items`：

- safety-only L1 不会被 verifier 计入层覆盖；
- Claude/Codex 的 `item_digest` 不绑定 `safety_items`；
- safety lane 被篡改或 provider 间不一致时，旧 verifier 可能仍报告 regular items 相同。

需要 pack contract version bump 或兼容 verifier；不能只部署 source 一侧。

### P1：离线质量与可重复性门

1. **候选截断偏差。** semantic fallback 只看最新 160 条；较旧但高相关记录在无 lexical 命中时不可见。应以可验证索引取 top-N，再做 active truth 与 scope filter，而不是把更新时间当语义候选预过滤器。
2. **时间快照不一致。** 排名 recency 使用 checkpoint time，但 eligibility/expiry 使用 `utc_now()`；同一 checkpoint 的 pack 可随墙钟变化。建议 receipt 同时绑定 `truth_as_of` 和 `ranking_as_of`，所有 eligibility、recency、pack digest 使用同一 trusted instant；实时 expiry 另产生新 snapshot。
3. **容量语义不清。** source safety 上限 4000 B 仍嵌在总 envelope 6000 B；安全内容可能挤掉几乎所有动态内容。`max_items=8` 又没有说明是否包含 `safety_items`。应分别实验 `reserved safety`、`shared total`、`dual envelope`，不能直接选默认值。
4. **读取时 secret exclusion 不够显式。** 审批链与输入 schema 是第一道门，但 pack 构造没有二次 privacy classification/scan receipt。建议在 truth snapshot 中存 privacy enum/hash，读取前硬排除，pack receipt 绑定 privacy scan hash；绝不在 evaluator 中重读正文。
5. **现有 evaluator 只做宏平均。** `memory_eval.py:161-218` 有 precision/recall/MRR/abstention/revoked/p95/tokens，但缺 per-category minimum、nDCG、dimension coverage、secret/L0 hits、calibration、pack overflow、Sol routing false-positive、graph ablation。少量易例可掩盖危险类别失败。
6. **失败链缺 repair outcome 与强绑定。** 事件已有 typed `error_code`、failed condition、repair step、unavailable boundary（`validate_event.py:235-258`），但 `repair_step_id` 只校验格式；如果它不在 dependency/parent 列表，`project_chain.py:475-488` 会静默不生成 `REPAIRED_BY`。应在验证阶段拒绝 dangling repair，并加入 `repair_outcome_code/evidence_sha256`。
7. **sandbox fixture contract 尚未闭合。** SSD source `safe_parent.py` 已有 approved storage root/UUID 和 diskutil APFS/FileVault/unlocked/Owners 证明；真实外部 `diskutil` 七字段 PASS，卷根 mode 775 不应被误判为算法缺陷。现有 focused deny-network：safety 6/7、semantic 1/6、feedback 5/5、retrieval-eval 2/2、context-hook 4/5；失败主要来自 sandbox 禁止 `diskutil`/Unix socket 和清空 HOME 后 directory-service `getpwuid`，属于 P1 harness/fixture policy，不是 safe-parent P0。

### P2：自动沉淀与长期优化

1. Graph projection 没有 query-time typed path strength、hub cap、hop decay 或 per-dimension diversity。
2. L1/L2/L3 → Graphify rebuild → Skill suggestion 仍是人工拼接流程；缺一个只读、content-addressed suggestion receipt。
3. feedback 只能作为证据；尚无经过校准的 usefulness/contradiction reconciliation 队列。它不应直接改 confidence 或 approval。
4. 多 scope 继承、跨时间窗口稳定性、Skill 与 memory 双向 revocation closure 需要更大的合成与 packaged-runtime E2E。

## 2. 推荐架构、状态机与 local-first/Sol 门

### 权威层次

```text
append-only redacted event journal + reviewed memory/Skill truth registry
                         |
                         v
          deterministic active/inactive closure
                         |
          +--------------+---------------+
          |                              |
          v                              v
  Graphify rebuildable projection   local retrieval indexes
          |                              |
          +----------> retrieval planner <---------- query hash/scope/intent
                                  |
                     +------------+------------+
                     |                         |
              LOCAL_PACK                REASON-CODED_ABSTAIN
                                                |
                                        Sol admission gates
                                                |
                                    first result = inactive L0
```

权威顺序固定为 journal/truth registry > active closure > projection/index > pack。Sol、Graphify、reranker 和模型 judge 都不能写 truth 或激活记忆。

### Retrieval 状态机

```text
QUERY_REDACTED
 -> SCOPE_BOUND
 -> PACKAGE_IDENTITY_VERIFIED
 -> TRUTH_SNAPSHOT_PINNED
 -> HARD_EXCLUSIONS_APPLIED
 -> LOCAL_MULTI_CHANNEL_RETRIEVAL
 -> RELATION_STRENGTH_COMPUTED
 -> CONFLICT_AND_PREMISE_CHECKED
 -> SCORE_AND_COVERAGE_GATED
 -> PACK_CAPACITY_GATED
 -> LOCAL_PACK_READY
```

任何阶段失败都进入 `ABSTAIN(reason_codes)`，而不是返回一个低分结果。`production/security/external_write` 的 local pack 只能提供参考，不能替代原任务的 authorization gate。

### Sol fallback admission

仅在以下本地结果之一成立时提议 Sol：

- `NO_ELIGIBLE_LOCAL_MEMORY`
- `LOCAL_CONFIDENCE_BELOW_THRESHOLD`
- `DIMENSION_COVERAGE_INSUFFICIENT`
- `ACTIVE_CONFLICT`
- `PACK_CAPACITY_EXHAUSTED`（先尝试合法压缩/更窄 scope，仍失败才提议）

然后按顺序检查：privacy/redaction → provider availability → network policy → cost → user/operation authority → exact scope。离线 evaluator 只能输出 `sol_fallback_required`，不得自行调用 provider。

Sol 返回后的学习状态机：

```text
SOL_RESULT
 -> L0_CANDIDATE (activation=false)
 -> REDACTION_VALIDATED
 -> REPRODUCTION_1
 -> REPRODUCTION_2_DISTINCT_RUN
 -> INDEPENDENT_ACCEPTANCE
 -> REVIEW_READY
 -> L1_ACTIVE (explicit human or allowed signed low-risk rule)
 -> L2_PROPOSED -> L2_ACTIVE (2 active L1 + human)
 -> L3_PROPOSED -> L3_ACTIVE (2 approved L2 + 2 stable windows + human)
```

一次 Sol 成功、一次失败、模型自评或 focused unit test 都不能跳过状态。

### 撤销状态机

```text
REVOCATION_EVENT_APPENDED
 -> TARGET_INACTIVE
 -> EVENT/TRUTH_DESCENDANTS_INACTIVE
 -> NEXT_PACK_HARD_EXCLUSION
 -> GRAPH_PROJECTION_REBUILT
 -> SKILL_SUGGESTION_INVALIDATED
 -> INSTALLED_SKILL_REVIEW_OR_ROLLBACK_GATE (if applicable)
```

撤销记录不删除旧证据。只有新的、显式审批事件可以处理后续替代；不能让一个过期的 projection 或 cache 使旧方法“复活”。

## 3. Retrieval 关系加权与 pack 容量实验

### 先 hard filter，再评分

以下永远不进入 soft score：L0、非 active、revoked/expired/superseded、inactive ancestor、scope mismatch、secret/credential/session/tool-output/hidden-reasoning/direct-PII、active contradiction。零次命中是发布硬门，不是平均指标。

`REVOKES`、`SUPERSEDES`、`CONTRADICTS` 只有在关系经 review 且 confidence = 1 时才是硬关系；其中撤销/替代的来源节点本身还必须先通过 active、scope、privacy、time 与 ancestor 门。L0 或 inactive 节点绝不能借一条边获得撤销权。

### 离线候选评分（实验权重，不是生产默认）

`retrieval_strength_evaluator.py` 实现下列可复现公式：

| 特征 | 权重 |
|---|---:|
| lexical RRF normalized | 0.10 |
| semantic similarity | 0.14 |
| task-intent-step | 0.14 |
| causal-dependency | 0.12 |
| evidence-outcome | 0.16 |
| time-scope | 0.14 |
| typed relation strength | 0.10 |
| reviewed confidence | 0.04 |
| evidence strength | 0.04 |
| installed/real acceptance | 0.02 |

关系 strength 在 active、同 scope 子图内计算；硬关系不参与正权重。

| soft relation | 单边基础权重 |
|---|---:|
| SUPPORTS | 0.30 |
| ACCEPTED_BY | 0.25 |
| DERIVED_FROM | 0.20 |
| REPAIRED_BY | 0.18 |
| DEPENDS_ON | 0.16 |
| CAUSED_OUTCOME | 0.14 |
| APPLIES_TO | 0.12 |
| INTENT_HAS_STEP / OBSERVED_IN | 0.10 |
| NEXT_STEP | 0.04 |
| RELATED_TO | 0.03 |

候选 prototype 只做一跳并将 strength cap 为 1.0。下一阶段才实验最多两跳、第二跳 ×0.5、每维 cap 与 hub-degree penalty；没有 ablation 证据前不扩大遍历。

### Pack 实验矩阵

用同一 frozen truth snapshot、同一 query set 和同一 trusted time 跑全因子矩阵：

| 维度 | 候选值 |
|---|---|
| regular item cap | 6 / 8 / 12 |
| final UTF-8 envelope | 4000 / 6000 / 8000 / 10000 B |
| layer policy | current 1/2/5；score-only；minimum-per-needed-dimension |
| safety policy | shared-total；reserved-then-regular；separate signed safety envelope |
| retrieval | current RRF；RRF + untyped neighbor；typed 1-hop；typed 2-hop decay |
| abstention threshold | 0.45 / 0.55 / 0.65，并做 calibration curve |

每格至少覆盖八类现有 suite，各类建议 ≥50 个哈希化 synthetic cases，并增加：safety-only、L0 adversarial high-score、secret-shaped、revoked ancestor、contradiction pair、old-but-relevant >160、oversized item、missing premise、scope collision、clock boundary。

### Proposed release gates

- `L0_hits = secret_hits = revoked_hits = inactive_ancestor_hits = 0`
- 每一类别单独达到门槛，不能只看 macro average
- precision/recall/MRR/nDCG@k、dimension coverage、abstention accuracy、false-Sol rate、missed-Sol rate
- Brier/ECE 或等价 calibration；score 不等同于 confidence，必须测
- p50/p95/p99 latency、pack bytes、tokens、truncation、capacity abstention
- current-RRF / no-graph / typed-graph ablation 的 bootstrap confidence interval
- deterministic repeated receipt equality
- 模型 judge 只能补充，不是唯一 gate

保留当前 live 8/6000 作为 baseline；任何新容量必须由质量与成本曲线选择，不能因“能放更多”直接上调。

## 4. 失败方法、反例与四维链 schema

现有 schema 已具备 `result.error_code`、`failed_condition_code`、`repair_step_id`、`unavailable_boundary_code`。建议补齐下列严格字段；本目录 JSON Schema 的 `$defs.failure_pattern_extension` 给出 content-free 版本：

| 字段 | 目的 |
|---|---|
| typed_error_code | 稳定枚举，不存 raw exception |
| failed_condition_code | 失败的可测条件 |
| repair_step_id | 指向同 scope、存在的 step；必须是 dependency/parent |
| repair_outcome_code | 修复是否成功/不适用/仍 blocked |
| unavailable_boundary_code | 何时不可使用该方法 |
| reproduction_run_hashes | 至少两个不同 Run 的 hash |
| counterexample_scope_sha256 | 防止跨 scope 泛化 |
| failure/repair evidence SHA、acceptance class | 分别绑定失败与修复证据及验收级别，不存正文 |

四维映射：

- D1 task-intent-step：`ATTEMPTED_STEP`、`NEXT_STEP`
- D2 causal-dependency：`DEPENDS_ON`、`FAILED_WITH`、`BLOCKED_BY`、`REPAIRED_BY`
- D3 evidence-outcome：`SUPPORTS`、`CONTRADICTS`、`ACCEPTED_BY`
- D4 time-scope：`OBSERVED_IN`、`APPLIES_TO`、`UNAVAILABLE_WHEN`

失败第一次只进入 L0。两次独立复现 + 完整 condition/repair/boundary 才可 review-ready。若后来成功，追加 `CONTRADICTS` 并进入 conflict review；若方法或边界被撤销，沿 `DERIVED_FROM/DEPENDS_ON` 传播失活到 Graph projection、pack 和 Skill suggestion。

## 5. Graphify 与 Skill candidate 沉淀

推荐的离线、单向管线：

1. 验证 append-only event journal 和 truth registry，固定 `evaluated_at`。
2. 计算 active/inactive closure；只有 active 的 L1–L3 可进入 active projection，L0 只留在不可召回审计面。
3. 生成四维 Graphify input manifest：每个 node/edge 只有 enum/hash/time/scope/strength/provenance；不含正文、路径、prompt、tool output。
4. 用现有 Graphify extract/postprocess/rebuild 路径生成 projection；生成文件不可手改。
5. 对重复稳定方法生成 `SKILL_SUGGESTION.json`，绑定 event-set SHA、truth-registry SHA、projection SHA、evaluation receipt、policy SHA、revocation-closure SHA。
6. 只有 active L1/L2 且满足重复/独立验收的建议才能进入 `skilld.py stage`；先 dedupe/conflict，再 versioned staging。
7. quick validation、deny-network forward test、独立 reviewer 后仍只到 review-required；安装必须另行人工授权。

建议 staged Skill 新增 `LEARNING-PROVENANCE.json`（需要未来 schema/skilld 版本明确允许），使候选能被反查和撤销。任何祖先 inactive 时，候选状态改为 `invalidated_by_truth_change` 并要求重新 stage；不能在原目录覆写，也不能自动删除 live Skill。

## 6. Phased implementation backlog 与本地项目优先级

### Phase P0-A — 身份与包闭合

- 在 `/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code/tools/orca-agent-memory` 建立 tracked ship-set；当前整个目录 untracked，不能作为 release authority。
- build manifest 枚举所有 wheel members、modes、entrypoints、bytes/SHA；拒绝声明入口但缺 payload。
- 把 wheel/venv 私密权限归一化写进 build/verify，不靠人工 chmod。
- 对 source wheel、安装 wheel、installed files 做三向 exact identity；隔离 9/9 entrypoints 只是必要条件。
- 修订 `complete_memory_injection.py` verifier，使 `safety_items`、regular `items`、policy 和完整 pack receipt 都进入跨 provider equality；不要编辑 live hook。

### Phase P0-B — 离线 retrieval contract

- 将本目录 schema/evaluator 的 hard exclusions、reason-coded abstention、Sol-L0 contract迁入 authority source 的独立 v2 模块；先不接 hook。
- truth snapshot 必须由 journal/truth registry 生成，不能让 Graphify 或搜索索引自报 active。
- 为 contradiction/revocation/ancestor propagation 加 deterministic cases。
- 扩展 `memory_eval.py` 为 per-category gate、forbidden-hit gate、pack/capacity/calibration/ablation receipt。

### Phase P1 — pack v2 与 sandbox 合同

- 用 feature flag/独立 CLI 输出 pack v2；同时保留 current live baseline，不改 installed runtime。
- 统一 `truth_as_of`/`ranking_as_of`；解决 latest-160 semantic blind spot。
- 实验 safety/shared/dual budgets；以 exact final UTF-8 envelope 计数。
- 将 safe_parent 外部 diskutil 与 sandbox fixture 分层：纯 parser/unit、approved synthetic plist、受控真实 diskutil、Unix socket integration 各自独立报告。sandbox 的网络 deny 不应误伤本地 IPC 测试而被当作算法失败；如果策略确实禁止 socket，则该测试明确 `not_run_by_policy`，不能算 PASS。

### Phase P1/P2 — failure chain、Graphify、Skill suggestion

- `validate_event.py` 拒绝 dangling `repair_step_id`，并验证 repair outcome/evidence。
- `project_chain.py` 产生 typed failure relations和稳定方向的 provenance edges。
- Graphify 只消费 content-free manifest；运行 query/explain 和 revocation rebuild tests。
- `skilld.py` 未来版本绑定 learning provenance 和 revocation closure；仍停在 staged/review-required。

### Phase P2 — Canary/packaged runtime（本审计未授权）

- clean exact SHA、完整 unit/integration、Linux/macOS/Windows package smoke、fault/stress、provider/network admission、Claude/Codex same-pack。
- Canary 只验证 isolated installed copy；真实 hook/所有账户事务发布、preimage/rollback、真实流量/E2E 仍需单独授权和证据。

### 最值得继续优化的本地项目

| 优先级 | 路径 | 理由 |
|---|---|---|
| 1 | `/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code/tools/orca-agent-memory` | authority source、packaging、runtime search/context pack/safety lane 的汇合点；先 tracked + identity |
| 2 | `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/skills/review-orca-workflow-learning` | 已有状态机、四维链、撤销和 Skill staging，适合承载离线 contract/eval |
| 3 | `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/scripts/complete_memory_injection.py` | 需要识别 pack v2 safety lane 与完整跨 provider digest；保持 interactive/human auth |
| 4 | `/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code/tools/orca-agent-memory/graphify-out` | 只作为生成 projection；优化应发生在 extractor/postprocess/manifest，绝不手改输出 |
| 5 | `/Volumes/Extreme SSD/Orca/workspaces/orca/优化本机code/src/shared/agent-memory-store.ts` | host memory draft 与 sidecar learning contract 最终需统一 scope/identity，但晚于 P0 sidecar 闭合 |

冻结不动：live runtime、memory DB、capabilities、hooks、settings、当前已安装 package。

## 7. 官方一手项目对照（仅借鉴概念，未复制代码）

版本为 2026-08-11 通过官方 GitHub Releases/API 观察到的对应 core release；文档/论文为直接一手来源。

| 项目 | 版本/日期 | 可借鉴 | 不直接照搬 |
|---|---|---|---|
| Mem0 | [Python SDK v2.0.17, 2026-08-05](https://github.com/mem0ai/mem0/releases/tag/v2.0.17)；[search docs](https://docs.mem0.ai/core-concepts/memory-operations/search)；[paper 2025-04-28](https://arxiv.org/abs/2504.19413) | extraction/consolidation、filter-first、threshold/rerank、关系 memory 与 latency/token evaluation | “latest truth wins”不能替代 append-only evidence、review 和 revocation；不自动存 raw transcript |
| Graphiti/Zep | [Graphiti v0.29.3, 2026-07-27](https://github.com/getzep/graphiti/releases/tag/v0.29.3)；[official repo](https://github.com/getzep/graphiti)；[Zep paper 2025-01-20](https://arxiv.org/abs/2501.13956) | temporal validity、episode provenance、hybrid semantic/keyword/graph、historical relationships | Orca learning journal 不接收 raw episode；Graphiti 不是 truth authority，telemetry/network 也不能默认启用 |
| Letta/MemGPT | [Letta 0.16.8, 2026-05-14](https://github.com/letta-ai/letta/releases/tag/0.16.8)；[Letta Code v0.30.17, 2026-08-11](https://github.com/letta-ai/letta-code/releases/tag/v0.30.17)；[memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks)；[MemGPT paper 2023-10-12](https://arxiv.org/abs/2310.08560) | 分层 memory/paging、always-visible 小型 safety/policy lane、read-only shared blocks | 不允许 agent 自行编辑/激活安全规则；always-visible lane 仍受 pack/secret/revocation 硬门 |
| LangGraph | [langgraph 1.2.10, 2026-07-28](https://github.com/langchain-ai/langgraph/releases/tag/1.2.10)；[memory/store docs](https://docs.langchain.com/oss/python/langgraph/add-memory)；[persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | thread checkpoint 与 cross-thread Store 分离、namespace scope、semantic search、hot-path/background 写入分离 | Store API 本身不提供 Orca 的 L0/review/revocation/secret authority，必须外加治理门 |
| Microsoft GraphRAG | [v3.1.1, 2026-07-18](https://github.com/microsoft/graphrag/releases/tag/v3.1.1)；[query modes](https://microsoft.github.io/graphrag/query/overview/)；[methods](https://microsoft.github.io/graphrag/index/methods/)；[paper 2024-04-24](https://arxiv.org/abs/2404.16130) | local/global/DRIFT routing、entity-neighbor + community context、明确 context/token budget、graph ablation | 适合 corpus/global sensemaking，不应把 LLM community summary 当 operational truth；Sol/global 路径必须在 local abstain 后才入场 |

许可证和实现边界：本候选只记录公开架构概念和直接 URL；没有复制上述项目代码、prompt、schema 或受许可约束的实现片段。

## 8. 本目录离线候选与验证范围

文件：

- `retrieval_strength_evaluator.py`：content-free deterministic local-first evaluator；不读 DB、不联网、不调用 Sol。
- `schemas/workflow-learning-strengthening.v1.schema.json`：query/memory/relation/signal、失败模式扩展和 Sol 首次 L0 合同。
- `fixtures/local_first_suite.v1.json`：纯 hash/enum/count synthetic fixture。
- `tests/run_deny_network.py`：要求 SSD candidate root 0700、TMPDIR 在 evidence 0700 下；先验证 macOS sandbox 对 loopback connect 返回 `EPERM`，再 monkeypatch DNS/socket network entrypoints 为 fail closed。
- `tests/test_retrieval_strength_evaluator.py`：L0、secret、revocation ancestry、L0 无撤销权、硬关系 review、conflict、low confidence、relation boost、capacity、missing ancestor、determinism、schema、network deny。

验证结果：在 `sandbox-exec '(deny network*)'`、清空环境和 fail-closed proxy 下，14/14 unittest PASS，`deny_network=true`、`os_sandbox_network_denied=true`，0 failure，0 error。该结果只证明本目录 prototype 的确定性与边界，不证明 live runtime、provider、hook、wheel、Graphify、Skill、跨平台或 production。

最终结论保持：`GO_OFFLINE_CANDIDATE_ONLY`。
