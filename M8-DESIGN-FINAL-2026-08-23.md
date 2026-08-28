# M8 跨项目共享开发系统设计文档（最终定稿）

本文档是 M8（M8-1 文本模糊证据、M8-2 自动扫描发现、M8-3 能力自动发布、M8-4 跨项目自动兼容性测试）四个子项合并设计草案，在吸收三份独立审查（安全/隔离不变量、范围蔓延、可实现性）意见之后的最终版本。M8 此前按用户既有决定处于 deferred 状态；本文档是"设计定稿"，不是"开工许可"——第 4 节会明确说明哪些部分具备立即请示实现的条件、哪些部分设计已完成但仍需独立的后续授权。

---

## 1. 概述

M8 要解决的核心问题是：跨项目共享的知识/能力目前完全依赖人工发现、人工搬运；M4-M7 建立的 `catalog.json` 只读聚合体系解决了"已经存在的东西怎么被找到"，但没有解决"新东西怎么被发现、怎么被审核、怎么被正式收录，以及收录之后怎么知道它会不会影响别的项目"。这正是用户此前明确要打破的两条边界：**不自动发布**、**不自动做跨项目兼容性测试**。

四个子项的分工：

- **M8-3（能力自动发布）**是打破"不自动发布"边界的唯一入口，是本次修订投入篇幅最大的部分，因为它是整个 M0-M8 体系里第一个允许自动化写入某个项目自己 tracked 文件的机制。
- **M8-4（跨项目自动兼容性测试）**打破"不自动做兼容性测试"边界，分 Tier-1（只读、结构级）和 Tier-2（真实执行、默认关闭）两档。
- **M8-1（text_mention 模糊证据）**和 **M8-2（自动扫描发现候选）**是两个独立的效率/发现层能力，设计已经完成，但本次审查后判定它们不是打破上述两条边界所必需的部分，因此被移出本轮的核心授权范围（详见第 2 节、第 4 节）。

四者的关系仍然是"只读派生层"（M8-1、M8-4）与"发现-发布层"（M8-2、M8-3）两层，中间只有一条单向门：任何新内容必须先经 `promote_capability.py approve` 写进某个项目自己的 `wiki/*.json` 并被人提交，才能被 M4 重新聚合进 `catalog.json`，才能被 M8-1/M8-4 看见。这条不变量在本轮修订中没有变化，只是围绕 `approve` 这一个动作本身的安全细节被大幅加固（第 3.4 节）。

```
                     只读              只读
  M4 catalog.json ───────► M8-1 mention-evidence.json  （独立授权，建议/仅供人查询）
        │                  M8-4 Tier-1 capability-content-hashes.json /
        │                        capability-changes.json（建议/受影响面）
        │
        │  （新能力/新知识只能从这一条路径进入）
        ▼
  某项目 wiki/*.json（人工提交，由 approve 原子完成写入+提交）
        ▲
        │ approve（唯一写入口，本轮修订重点）
  M8-3 promote_capability.py
        ▲
        │ draft --from-discovery-hit（独立授权后才设计/实现，v1 不做）
  M8-2 discover_capability_candidates.py（独立授权）→ capability-discovery/discovery-hits.json
```

---

## 2. 审查意见的采纳与跳过

本节逐条列出三份审查各自的意见，以及本文档的处理方式和理由。第 3-7 节的具体设计内容是本节裁决的落地展开，不重复论证。

### 2.1 安全/隔离不变量审查

| # | 意见 | 处理 | 理由 / 落地位置 |
|---|---|---|---|
| 1 | ①跨项目写入例外应从表格脚注提升为独立声明的架构决策 | **采纳** | 新增 3.4.1"跨项目目录写入的唯一例外"独立小节 |
| 2 | ②SessionStart 不得调用发现型索引器 | 无需修改（确认通过） | 四个子项在这条上已经一致自律，原样保留 |
| 3 | ③`approve` 写完 `wiki/` 不自动提交会触发 `AUTHORITY_TRACKED_PATHS` NACK，判定为具体违反 | **采纳** | `approve` 改为默认原子完成"写入+git commit"，新增 `--no-commit` 逃生阀，见 3.4.4 |
| 4 | ④不做黑箱 AI 自动判断 | 无需修改（确认通过） | 原样保留 |
| 5 | ⑤跨项目代码执行需隔离/授权机制 | 无需修改（确认通过） | 原样保留；其"应作为独立授权节点对待"的隐含建议并入第 4 节 Gate D |
| 6 | `compat-check.json` 的 `reviewed_by`/`reviewed_at` 是自证字段，无第三方校验（非阻塞建议） | **跳过实际设计变更，仅记入风险清单** | 该声明必须提交进项目自己的 git 仓库才生效，已处于该项目正常 PR/评审可见性之下，不构成可绕过的黑箱环节；记入第 6 节风险 6.9 存档，不在 v1 阻塞 |

### 2.2 范围蔓延审查

| # | 意见 | 处理 | 理由 / 落地位置 |
|---|---|---|---|
| 1 | 核查"是否悄悄改变 M0-M7 行为" | 无需修改（确认通过） | 原样保留，第 5 节继续逐条对照确认 |
| 2 | 建议整体移出 M8-1（不属于打破两条边界的必需范围） | **部分采纳** | 不删除设计内容（第 3 节结构要求四个子项各自的设计），但采纳其实质——移出本轮核心授权范围，列为需独立前置验证（数值验收线）后才能单独请示的 Gate C，见第 4 节。跳过"直接删除章节"的字面做法，理由：完整设计record 对将来独立授权仍有价值，删除会造成信息丢失而非风险降低 |
| 3 | 建议整体移出 M8-2（同上） | **部分采纳** | 处理方式同上，见 3.3、第 4 节 Gate C |
| 4 | 建议删除"空的外部适配器注册表"占位（YAGNI） | **采纳，且扩大适用范围** | 不仅删除该占位，`promote_capability.py draft --from-discovery-hit` 桥接子命令本身也一并推迟到 M8-2 真正获得独立授权时才设计——现在设计这个接口形状同样缺乏信息基础，是同一类 YAGNI 问题 |
| 5 | 建议 `capability-discovery/` 目录名跟着收缩 | **采纳，但落地方式不同于字面建议** | 不是把目录整体改名为 `capability-promotion/`（因为 M8-2 设计仍然保留），而是拆成两个平级的独立顶层目录：`capability-promotion/`（M8-3 专属）与 `capability-discovery/`（M8-2 专属，待独立授权），消除"promotion 嵌套在 discovery 之下"暗示的层级关系，见 3.0.1 |
| 6 | 建议 `amend` 与 M8-1 解耦 | **采纳** | `--source` 字段泛化为自由文本，`mention-evidence:<edge-id>` 只是其中一种可能取值，见 3.4.5 |
| 7 | 发现内部不一致：Tier-2 `compat/results/` 混进"已认证"目录树 | **采纳** | 新增独立顶层目录 `compat-runs/`，Tier-2 原始执行结果与 Tier-1 确定性 hash/diff 产物分开存放，见 3.0.1 |

### 2.3 可实现性审查

| # | 意见 | 处理 | 理由 / 落地位置 |
|---|---|---|---|
| 1 | M8-2 四条文件名启发式规则从未列出 | **采纳，定为独立授权前置条件** | 不在本文档展开具体规则——M8-2 已移出本轮范围，展开规则的正确时机是那次独立授权申请本身，届时须先用真实文件列表试跑并公布命中率，见 3.3.3 |
| 2 | M8-3 跨候选去重算法未定义 | **采纳** | 明确定为"精确匹配（归一化元组 + 内容哈希），不做模糊/语义匹配"，并显式与 M8-1 方法论解耦，见 3.4.3 |
| 3 | Tier-1 内容哈希范围未定义 | **采纳** | 补充显式字段 allowlist/denylist 及配套测试要求，见 3.5.2 |
| 4 | CJK 缓解手段停留在口号层面，无数值验收线 | **采纳，定为独立授权前置条件** | 理由同第 1 条，M8-1 已移出本轮范围，数值验收线在其独立授权申请时提出，见 3.2.4 |
| 5 | `approve` 写 wiki 会使 `reviewed-startup-pack-manifest.json` 的 pin 失效，导致 SessionStart fail-closed（新发现，比"未提交"问题更深一层） | **采纳** | `approve` 新增显式检测 + 不可抑制提醒 + `requires_manifest_resign` 字段，见 3.4.4 |
| 6 | `wiki_edit_guard.py`（658 行、曾出过安全漏洞）不适合被"复制小段" | **采纳** | 改为子进程调用，作为"复制不导入"约定下的具名例外，并对其内容做 SHA-256 pin 防漂移，见 3.0.2 |
| 7 | "复制不导入"已有真实分叉案例（`agent_capacity.py`），`KIND_VALUES` 等重复常量需要漂移检测 | **采纳** | 为 v1 范围内已确认存在的重复常量补充跨文件一致性测试的强制要求，见 3.0.2 |
| 8 | 7 个新脚本各自重实现写安全原语，成本被低估 | **部分采纳** | 不改变原语本身设计（这套模式本身经受过验证，值得继续用），但采纳"先缩小 v1 脚本数量"这一间接缓解——移出 M8-1/M8-2 后，v1 实际新脚本数从 7 个降到 3 个，作为范围收缩的附带收益记录在第 6 节，不再额外发明新机制 |
| 9 | 建议拆成 4 次独立授权而非一次性开工 | **采纳** | 第 4 节改写为 Gate A/B/C/D 四个独立授权关口，而非一次性批准的内部阶段 |

对review3提出的两个开放式问题（approve 应该"拒绝写入已启用 SessionStart 门禁的项目"还是"写入+强提示重签"；`wiki_edit_guard.py` 应该"子进程调用"还是"完整复刻"）本文档在 3.4.4 与 3.0.2 中给出明确选择并说明理由，不作为待定项遗留。

---

## 3. 四个子项的设计

### 3.0.1 统一目录布局

```
manifests/
  cross-project-catalog/                 # M4 专属（既有），只放"已聚合/已认证派生"数据
    catalog.json                         # M4（既有）
    .catalog.lock                        # M4（既有）
    mention-evidence.json                # M8-1 新增（只读派生自 catalog.json）——Gate C 通过后才实际产生
    compat/                              # M8-4 Tier-1 新增
      capability-content-hashes.json
      capability-changes.json
      .compat.lock

  capability-promotion/                  # M8-3 专属顶层目录（新增，与 discovery 彻底分开，不互相嵌套）
    <project_id>/<candidate_id>.json
    <project_id>/<candidate_id>/content.md
    promotion-ledger.jsonl               # 只追加的审批台账
    .promotion.lock

  capability-discovery/                  # M8-2 专属顶层目录（设计保留，Gate C 通过后才实现）
    discovery-hits.json
    .discovery.lock
    runs/                                # NDJSON 扫描日志

  compat-runs/                           # M8-4 Tier-2 专属顶层目录（新增，未认证/可变数据类）
    <run_id>/<project_id>.json           # 受影响项目自己声明命令的真实执行输出
    .compat-runs.lock
```

四个目录各自独立 pin 自己的根，遵循 M4 `write_only_within()` 已验证过的纪律（单一外部根、无覆盖开关、`O_NOFOLLOW|O_EXCL` 临时文件 + rename、陈旧锁清理），但没有一个真的 `import` 或修改 `build_cross_project_catalog.py`——全部是"抄写模式、独立实现"。

`capability-promotion/` 与 `capability-discovery/` 分成两个平级目录（而不是像最初草稿那样让 promotion 嵌套在 discovery 之下）是本轮修订的一处结构调整：M8-3 是本轮唯一要落地的写入口，不应该让它的目录名暗示自己依赖一个尚未授权、尚未存在的发现层。`compat-runs/` 独立于 `cross-project-catalog/compat/` 是另一处结构调整：后者只放确定性的哈希 diff（Tier-1，性质上"已认证"），前者放的是受影响项目自己声明命令的真实执行原始输出（Tier-2，性质上和候选暂存区一样"可变、未经认证、可能包含不可预知内容"），两者信任级别不同，不该共享目录树。

### 3.0.2 跨子项统一约定

**"只复制、不 import"及其唯一例外**：任何新脚本需要用到 M3-M7 已上线代码的小段逻辑（`_normalize()`、路径 containment 校验、`write_only_within()` 风格守卫、`fstat` 身份校验等），一律在自己文件里复制一份最小实现，不 import 已交付模块，保持每个新工具的信任面独立。

本轮修订为这条约定新增一处**具名例外**：`wiki_edit_guard.py` 的语义判定逻辑（区分"仅时间戳/校验字段变了"与"语义变了"的 deny-list 投影，历史上出过一次可被利用的"洗版本号"漏洞并已修复）不适合被复制——复制一份 658 行、曾经出过安全漏洞的判定逻辑，等于重新创造一次同等量级的审查负担，与"只复制不 import"原本想省下来的成本背道而驰。`promote_capability.py approve` 写 `orca-context-wiki.json` 路径时，改为**子进程方式**调用 `wiki_edit_guard.py` 暴露的校验入口（把拟写入的"改动前/改动后"两个版本交给它判定，只有它返回允许后才真正原子落盘）。这不是 `import`（两者仍是独立进程/信任边界，`promote_capability.py` 不需要理解 deny-list 内部细节），但确实构成一个运行时依赖，必须在文档里承认，不能用"复制小段"一句话带过。作为配套义务：`approve` 启动时钉住一份 `wiki_edit_guard.py` 当前内容的 SHA-256；运行时若发现该文件哈希与钉住值不一致，视为环境不一致，拒绝执行并以致命错误退出，而不是静默用一个自己不认识的新版本做安全判定（该 pin 的维护方式类比 `reviewed-startup-pack-manifest.json`，需要人工在 `wiki_edit_guard.py` 合法变更时同步更新，见第 6 节风险 6.3）。

`validate_reusable_capabilities.py` 的校验逻辑相对简单（单一枚举成员校验），不构成同等风险，继续沿用"复制小段"的原约定。但既然本仓库已经证实存在真实的复制拷贝分叉案例（`agent_capacity.py` 的两份独立维护副本已经真的漂移），本轮为 `KIND_VALUES` 这类必须逐字节一致的拷贝新增一条强制配套义务：任何"复制不导入"产生的常量/规则拷贝，都必须配一条跨文件相等性测试（例如断言 `validate_reusable_capabilities.py` 与 `build_cross_project_catalog.py` 两处 `KIND_VALUES` 定义逐元素相等），而不是仅在裁决叙述里强调"目前一致"。这条义务对本轮 v1 范围内已确认的重复常量强制适用，对将来新脚本新增的重复常量同样适用，不是一次性任务。

**生成器 vs 查询器：两套 exit code 语义**：

- **生成器类**（`detect`/`scan`/`build`）：0 = 跑完（即使结果为空、即使有部分条目被跳过/降级——必须体现在 JSON 正文里的具名字段，例如 `degraded_projects`/`skipped_kind`，不能默默丢弃）；2 = 用法错误；4 = 整个跑不动（致命错误，产物字节不变）。
- **查询/裁决类**（`search`/`show`/`list`/`affected`/`run`）：0 = 找到/给出确定答案；1 = 确认没有（完整检索后的真实"无"）；2 = 用法错误；3 = 部分可信（检索不完整，不能当成"确认无"）；4 = 完全查不了（源文件缺失/损坏/过期）。
- **决策/动作类**（`draft`/`approve`/`reject`/`withdraw`/`amend`）：0 = 完成了请求的状态迁移；1 = 校验未通过、未发生写入；2 = 用法错误；4 = 环境损坏（例如 `wiki_edit_guard.py` 哈希不匹配）。

**置信度词表**：统一为 `low | medium | high` 三档共用词表，但不同证据类型可达到的上限不同——纯文本子串匹配天生只能到 `medium`（字符串重叠永远不构成确定性证据）；结构性探测（目录内确实存在某文件）可以到 `high`（确定性事实，不是猜测）。这不是矛盾，是同一词表下两种证据类型的合理不同上限。

**命名规范**：脚本名一律 `verb_noun.py`（`build_*`/`query_*`/`validate_*`/`discover_*`/`promote_*`/`detect_*`）。

**discovery hit ≠ promotion candidate**：拆成两段、两个术语、一条单向链路——

- **第一阶段"发现命中"（discovery hit）**：`discover_capability_candidates.py scan` 产出，存于 `capability-discovery/discovery-hits.json`。状态机 `pending → triaged_for_promotion | dismissed`。`review_capability_candidates.py mark` 的写入权限**仅限于这一个文件的这一条记录**，纯记账，从不触碰任何项目的 `wiki/*.json`。重跑扫描时，`dismissed`/`triaged_for_promotion` 状态按内容哈希保留，绝不复活成新的 `pending`。
- **第二阶段"提升候选"（promotion candidate）**：由人手填 JSON 运行 `promote_capability.py draft`（v1 阶段唯一支持的输入方式），存于 `capability-promotion/<project_id>/<candidate_id>.json`，状态机 `pending_approval → approved | rejected | withdrawn`。**`promote_capability.py approve` 是整条链路里唯一被允许写入任何项目 `wiki/*.json` 的动作**。`draft --from-discovery-hit` 桥接子命令留待 M8-2 获得独立授权时再设计（第 2.2 节第 4 条已说明）。

### 3.1 关键裁决：知识型内容（X 算法笔记）不新增 `kind:"knowledge"`

**已核实的事实**（非转述，直接读取源码确认）：

- `validate_reusable_capabilities.py` 明确写着："reusable-capabilities.json 收 CAPABILITY 半边（技能/脚本/工具），project-specific facts 留在 orca-context-wiki.json……deliberately no 'knowledge' value in this file's kind enum"。这是产品边界，不是遗漏。
- `KIND_VALUES = ("skill", "script", "config-pattern")` 在 `validate_reusable_capabilities.py` 和 `build_cross_project_catalog.py` 两个文件里各自独立维护、字面完全相同，两处代码注释都强调这是"故意保留的本地拷贝"——新增一个枚举值意味着这两个已加固文件必须同一个 commit、同一轮复核一起改。
- `capability_ref_index`/`capability_reverse_index` 的构建循环只遍历 `capabilities[]`，完全不涉及 `wiki_pages[]`。即便新增 `kind:"knowledge"` 放进 `capabilities[]`，`depends_on`/新鲜度提醒确实能立刻用上；但如果不新增，`wiki_pages` 想获得同等能力无论如何都需要改这同一段聚合器代码——这个代价在两个方案里都存在，只是落点不同。

**裁决：复用 `wiki_pages`，不新增 `kind` 值**。理由：①已被反复复核加固的产品边界文档明确写死"deliberately no knowledge value"，X 算法笔记属于"知识/经验"而非"技能/脚本/工具"，应按字面文档走 `wiki_pages`；②这条裁决让 M8 完全不需要触碰任何已加固的 M0-M7 文件——不改 `KIND_VALUES`（两处），不改 `validate_reusable_capabilities.py` 的校验逻辑，不改 `build_cross_project_catalog.py` 的分类逻辑；③M8-2 的候选 schema 本来就已经原生支持 `proposed_target: "orca-context-wiki.json"`，零改动；需要改的只有 M8-3——知识型候选的 `proposed_target` 一律为 `orca-context-wiki.json`，approve 时写入目标项目 `wiki/orca-context-wiki.json` 的 `pages[]`。

**遗留的真实缺口**（承认、但不在 M8 范围内解决）：`wiki_pages` 目前没有 `depends_on`/`last_verified_at`，M7 的新鲜度提醒和 M5 的依赖解析都覆盖不到知识型内容。正确的修复方式是将来单独给 `orca-context-wiki.json` 的 page schema 加两个可选字段，复用与 capabilities 相同的引用语法和 `capability_ref_index` 解析逻辑，而不是把知识内容错误地贴上"capability"标签换取这两个字段。这个扩展需要改动 `build_cross_project_catalog.py` 聚合循环，属于对已加固 M4 代码的改动，必须走独立授权和独立复核，列入第 6 节。

### 3.2 M8-1：text_mention 模糊证据

**授权状态**：设计完整，移出本轮核心授权范围（Gate C，见第 4 节），需先产出数值验收线才能独立请示实现。

- **产物**：`manifests/cross-project-catalog/mention-evidence.json`，只读派生自 `catalog.json`，不回填任何字段到 `catalog.json` 本体、不修改任何 M0-M7 文件。
- **扫描范围**：`capabilities[] ∪ wiki_pages[]` 全集——`wiki_pages` 从设计第一天起就是一等公民，裁决 3.1 生效后这一点完全不需要改。
- **匹配算法**：确定性字符串/正则匹配（NFC + casefold，照抄 `query_catalog.py::_normalize()`），四道假阳性闸门（长度下限、停用词表、ASCII 单词边界/CJK 子串退化、自匹配排除），同 `(from,to)` 对聚合成一条边。
- **置信度**：仅 `low`/`medium` 两档（3.0.2 节，文本子串证据不设 `high`）。
- **CLI**：`build_mention_evidence.py build`（生成器语义）、`query_mention_evidence.py search`（查询语义，含 exit 3 = 内部部分行结构异常但其余部分仍可回答）。
- **SessionStart**：明确不接入，`catalog_session_hint.py` 零改动。若未来要加第三行提示，须是独立哨兵 token（`ORCA_CATALOG_MENTION_V1`）、默认关闭、仅 `medium` 且条数收紧，本轮不做。
- **独立授权前置条件**（3.2.4）：CJK 词边界目前"只能靠长度下限部分缓解"，这个表述是诚实的但不足以作为验收依据。独立请示实现前，必须先用真实数据给出一条具体、可复算的噪声率上限（例如"随机抽样 N 条 `high`/`medium` 边，人工判定精确率低于 X% 则该批参数不达标，回炉重调"），把"部分缓解"换成一个可验收的数字，作为该次独立授权申请的一部分提交，而不是留到实现阶段才发现。

### 3.3 M8-2：自动扫描发现候选

**授权状态**：设计完整，移出本轮核心授权范围（Gate C，见第 4 节），需先产出真实命中率数据才能独立请示实现。

- **产物**：`manifests/capability-discovery/discovery-hits.json` + `runs/` 扫描日志（生成器语义）。
- **扫描内容**：能力型信号（脚本 shebang + argparse 特征、目录含 `SKILL.md`）与知识型信号（`reports/` 目录下或文件名匹配研究/笔记类模式的 `.md`）。**不再预留任何面向未来外部适配器的接口占位**——`write_candidate_capture.py` 的对接方式将在该工具输出格式确定、且 M8-2 本身获得独立授权之后单独设计；现在描述任何具体接口形状都缺乏信息基础（第 2.2 节第 4 条）。
- **基线去重**：读 `catalog.json` 已声明的能力/页面集合做差集，只有基线里没有的才算新发现；若 `catalog.json` 过期则显式标注 `baseline_stale:true`，不悄悄当真发现。
- **状态机**：`pending → triaged_for_promotion | dismissed`，按内容哈希在重跑之间保留终态，防止噪音复活。
- **CLI**：`discover_capability_candidates.py scan`（生成器语义）、`review_capability_candidates.py list/show/mark`（查询 + 记账，`mark` 只写这一个文件的一条记录，永不触碰任何项目文件）。
- **不做的事**：不直接写任何项目的 `wiki/*.json`；不注册进任何 hook，纯人工/事件驱动触发，比 M4 更强调"绝对不能挂 SessionStart"（因为要做全树遍历，比 M4 的三文件读取重得多）。
- **独立授权前置条件（3.3.3）**：报告类文件名启发式规则（草案里提到的"四道文件名规则"）此前从未被具体列出，本文档也不在此展开——正确时机是 M8-2 独立请示实现时，先用本仓库真实文件列表（含几十个 `CODEX-SOL-*-REVIEW-*.md`/`M4-CANDIDATE-*.md` 等）试跑一遍候选规则草案，把命中列表和误报率贴进那次独立授权申请里再定参数,以及评估 142+ worktree 全树遍历的 I/O 代价是否需要按仓库去重。这两项都不是本文档能自行拍板的,是那次独立授权本身的前置作业。

### 3.4 M8-3：能力自动发布流程（核心）

**授权状态**：本轮核心修订对象，是打破"不自动发布"边界的唯一入口（Gate B，见第 4 节）。v1 阶段 `draft` 只支持手填 JSON 输入，不依赖 M8-2。

#### 3.4.1 跨项目目录写入的唯一例外（独立声明）

M8 四个子项里，只有一处、且是唯一一处，允许一个进程写入"另一个"项目自己的目录：`promote_capability.py approve` 把候选内容写进它自己所属的目标项目的 `wiki/reusable-capabilities.json` 或 `wiki/orca-context-wiki.json`（及配套的 `wiki/knowledge/<id>.md`）。这不是从 M0-M7 只读聚合器传统里的意外突破，而是 M8 的产品定位（"跨项目共享"）决定了终归要有一个环节把内容真正落到目标项目里——问题不是"要不要有这个例外"，而是"这个例外有没有被足够多的门控包住"。本设计要求该例外必须同时满足：

- 目标路径由 catalog.json 自身的 `projects[]` 数组解析（`project_id` → `real_path`），不信任候选记录里的字符串——M8 四个子项（`promote_capability.py` 的 approve、`detect_capability_changes.py`、`check_cross_project_compatibility.py` 的 affected/run、`discover_capability_candidates.py` 的 `--all-projects`）在这一点上是同一套一以贯之的架构选择：只认 catalog.json 这一个项目枚举来源，不再额外发起一次 `orca repo list`/`worktree list` 调用，避免两个可能相互漂移的项目枚举机制并存；
- 写入前 `fstat` 身份校验防并发改动；
- 写入是原子的（临时文件 + 同文件系统 rename）；
- 写入与 `git commit` 在同一次调用内完成（默认，见 3.4.4），消灭"写完未提交"的窗口；
- 写入前必须有非空的 `--approved-by` + `--rationale`（人工具名签字）；
- **除这一处外，M8 全部四个子项的其余任何脚本、任何路径，都不允许出现第二处跨项目目录写入**——这是一条可被后续复核直接核查的红线，任何新增脚本若被发现写入了"自己所属项目"之外的路径，或写入了自己所属项目 `wiki/` 之外的、非本条描述的路径，均视为对本设计的偏离。

#### 3.4.2 产物与目标分类

产物为 `manifests/capability-promotion/` 下的候选记录 + `promotion-ledger.jsonl`（只追加审批台账）。`proposed_target` 只有两种：`"reusable-capabilities.json"`（script/skill/config-pattern 型候选）或 `"orca-context-wiki.json"`（知识型候选，裁决见 3.1）。

#### 3.4.3 draft：标准化与去重

`draft` 对手填输入做以下处理：

1. 跑 `validate_reusable_capabilities.py` 的既有校验函数（复制小段，不 import，见 3.0.2）；
2. 用 `catalog.json` 已有的 `capability_ref_index` 判定 `depends_on` 是否可解析；
3. **跨候选去重**（本轮修订明确定义算法，回应可实现性审查）：

   - `key = sha256(normalize(target_project) + "|" + kind + "|" + normalize(name_or_title))`；
   - `content_hash = sha256(normalize(summary) + normalize(body_or_content_md))`；
   - 若已存在（`pending_approval` 或 `approved` 状态）候选共享同一个 `key`：`content_hash` 也相同 → 拒绝为完全重复，指向既有 `candidate_id`；`content_hash` 不同 → 允许，但标注 `possible_revision_of: <candidate_id>` 交人工在 approve 时判断，不自动合并。
   - **明确不使用模糊/语义相似度**——如果未来想要模糊预筛，必须建成一个复用 M8-1 基础设施（待其独立授权后）的、纯建议性的 draft 前置提示，而不是在 M8-3 内部再造一套并行的模糊匹配引擎。这条设计显式地把 M8-3 的去重和 M8-1 的文本重叠证据机制解耦，避免两套相似度判断各说各话。

#### 3.4.4 approve：唯一写入口，原子写入+提交+重签提醒

`approve` 是整条 M8 链路里**唯一**被允许写入任何项目 `wiki/*.json` 的动作，按目标类型分两条路径：

**目标 `reusable-capabilities.json`**：重读现有文件 → `fstat` 身份校验防并发 → 原子写（临时文件 + rename）→ 写后用 `validate_reusable_capabilities.py` 自检，失败则回滚，不留已知非法文件。

**目标 `orca-context-wiki.json`**（本轮修订重点，修复两处审查发现的真实问题）：

1. 读现有 wiki 文件，计算按语义变更规则递增后的 `content_version`；
2. 序列化拟写入的"改动前/改动后"两个版本，**子进程调用** `wiki_edit_guard.py` 暴露的校验入口做合规判定（3.0.2 节的具名例外），只有其返回允许判定才继续；调用前校验 `wiki_edit_guard.py` 自身内容的 SHA-256 与启动时钉住的值一致，不一致则拒绝执行（致命错误退出）；
3. 原子写入（临时文件 + rename）；把暂存的 `content.md` 正文（如有）一并搬进目标项目的 `wiki/knowledge/<id>.md`；
4. **默认在同一次调用内执行 `git add` + `git commit`**（提交信息带审批理由摘要 + 台账条目引用），彻底消灭"写完未提交"的 NACK 窗口——这是本轮针对安全/隔离审查发现的具体违反项（候选落 `wiki/` 未提交会让目标项目现网 SessionStart 从 DELIVERY 变 NACK，M1、M7 已真实撞上过两次）所做的直接修复；
5. **检测 `reviewed-startup-pack-manifest.json` 的 `shared_source_sha256s` 是否钉住了刚写入的路径**（本轮修订新纳入，回应可实现性审查发现的更深一层问题）：即使 `content_version` 校验通过、即使写入与提交在同一操作内完成，任何改变该路径字节的写入仍然会让下一次 SessionStart 报"central reviewed source freshness mismatch"并 fail-closed，直到有人手动重签 manifest——这不是 bug，是钉子机制正常工作的结果。`approve` 的成功输出（人类可读文本 + JSON 结果里的 `requires_manifest_resign: true` 字段）必须把"目标项目 SessionStart 将在下次启动时 fail-closed，需要按既有重签流程手动重签"作为**不可抑制**的强提示打印出来，不提供任何"安静模式"选项能让这句话不出现。`approve` 本身不代为执行重签——重签是一个更高权限、需要独立人工授权的动作，`approve` 只负责"不让人在不知情的情况下踩坑"。

   关于是"拒绝写入已启用门禁的项目"还是"写入并强提示"，本设计选择后者：如果只要目标项目启用了 SessionStart 门禁就拒绝写入，那么 `approve` 在实践中永远用不上——门禁本来就应该对每个真实项目保持开启，这正是它该有的安全状态。把"SessionStart 会 fail-closed 直到重签"当成一个被清晰揭示、由人执行的预期后果（类似任何安全门禁写入后本就需要重新审核），比彻底拒绝这条唯一写入路径更符合 M8-3 的产品目的。

6. `--no-commit` 作为例外逃生阀，需同时传 `--i-understand-this-leaves-an-uncommitted-tracked-path`（刻意冗长，降低误用概率），帮助文本和输出都明确标注 NACK 风险。

无论哪条路径，`approve` 都不代为解决 `content_version` 校验或 manifest 重签之外的任何审批判断——终态判定始终来自人工具名的 `--approved-by`/`--rationale`。

#### 3.4.5 amend：受控依赖更新（与 M8-1 解耦）

对已发布条目追加/修改 `depends_on` 的受控更新路径。`--source <string>` 为自由文本（本轮修订，回应范围蔓延审查）：可以是 `human`，也可以是任何来源标识（例如 `mention-evidence:<edge-id>`，仅在 M8-1 获得独立授权后才有意义），`amend` 本身不预设任何特定来源格式，不硬编码依赖 M8-1 的存在。任何"建议存在依赖关系"的信号，无论来自哪里，都必须包装成一条 `--amend <kind>:<name> --add-depends-on <ref> --source <...>` 候选，走 `draft → approve` 全套审批，才能变成正式的 `declared_dependency`——不允许被任何自动化直接 patch 进已发布文件。

#### 3.4.6 非人工来源摩擦

`approve` 对 `source.mechanism != "human"` 的候选（不论来自未来的 M8-2 扫描还是未来的外部适配器）强制要求 `--confirm-non-human-source`，这条摩擦对所有非人工来源一视同仁，不区分具体机制。

### 3.5 M8-4：跨项目自动兼容性测试

**授权状态**：两档拆分授权——Tier-1（Gate A，只读结构级，可最先请示）、Tier-2（Gate D，真实执行第三方命令，需独立最高审慎度授权）。

#### 3.5.1 Tier-1：结构级、默认、零执行

`detect_capability_changes.py detect` 对 `capabilities[]` 中 `kind ∈ {"script","config-pattern"}`（**允许清单，不是排除清单**——任何未来新 kind 值自动落入"不可执行检查"分支，不需要回来改这个脚本；`wiki_pages` 从一开始就不在这份允许清单的讨论范围内，因为它压根不在 `capabilities[]` 里）的条目做内容哈希 diff，产出确定性的"变了/没变/新增/删除"信号——比 M5 的 `last_verified_at` 时间戳启发式更可靠（时间戳可能被手滑打新而没真的核实，内容哈希不会说谎）。`check_cross_project_compatibility.py affected` 消费 `catalog.json` 已经算好的 `capability_reverse_index`，回答"谁依赖了这个变化了的能力"。全程只读，可无人值守跑。

#### 3.5.2 哈希输入范围（本轮修订新增定义，回应可实现性审查）

**纳入哈希输入**：`name`、`kind`、`target`（`reusable-capabilities.json` 里的路径/命令定义字段本体，例如 script 的路径 + 参数说明、config-pattern 的模式定义本体）、`summary`、`depends_on`；若 `kind == "script"` 且候选引用了独立的脚本文件，还需纳入**该被引用脚本文件自身的字节内容**——否则脚本文件本身被改了，但 JSON 元数据没变，Tier-1 会漏检，这恰恰是"比时间戳更可靠"这条设计卖点能否成立的前提。

**排除哈希输入**：`last_verified_at`、`source_mtime`、`discovered_at`、`reviewed_at`，以及任何名字匹配 `*_at`/`*_timestamp` 的字段——这些字段会随时间自然变动但不代表语义变化。

**配套测试要求**（作为 Tier-1 设计被视为完成的必要条件，不是可选项）：仅改动被排除字段时哈希必须不变；仅改动被纳入字段时哈希必须改变；被引用脚本文件内容改变时哈希必须改变。

#### 3.5.3 Tier-2：可执行、默认关闭

`check_cross_project_compatibility.py run` 真正执行**受影响项目自己声明、自己提交**的检查命令（新文件 `wiki/compat-check.json`，argv 数组、`reviewed_by`/`reviewed_at` 必填）。要求逐项目显式 `--authorize-project`（必须是 `affected` 结果的子集，没有 `--authorize-all`），`shell=False`，执行前重新读一遍声明文件关闭 TOCTOU 窗口，`catalog.json` 新鲜度超过 6 小时阈值（与 `query_catalog.py`/`catalog_session_hint.py` 同一个数字）默认拒绝执行。执行结果写入独立的 `manifests/compat-runs/<run_id>/<project_id>.json`（3.0.1 节的目录调整），不与 Tier-1 的确定性产物共享目录。

`reviewed_by`/`reviewed_at` 是该项目自己填写的自证字段，没有独立第三方校验其真实性；但因为这段声明必须提交进项目自己的 git 仓库才能生效，已经天然置于该项目正常的 PR/评审可见性之下，不算可被绕过的黑箱环节，作为已知风险记录（第 6 节），不阻塞设计。

Tier-2 没有真正的 OS 级沙箱，只有"命令已经过该项目维护者评审并提交"这一层人工信任；后续加固方向是迁移到 Orca 现有的一次性沙箱环境，这需要独立授权，不在本文档范围。

#### 3.5.4 知识型内容的边界（永久规则）

1. 知识型内容（`wiki_pages` 中的条目）**永远不参与 Tier-2 可执行兼容性测试**——"运行一份笔记"本身没有意义，`compat-check.json` 的 `check_command` 语义假设的是"这是一段可执行代码变了，需要验证调用方还能不能跑"，这个假设对知识内容不成立。
2. 当前阶段，知识型内容对 `detect_capability_changes.py`/`check_cross_project_compatibility.py` **完全不可见**（已核实：`capability_reverse_index` 只由 `capabilities[].depends_on` 构建，`wiki_pages` 从不参与该循环），不需要额外写排除代码，是现有实现的自然结果。
3. **若未来** `wiki_pages` 的 schema 按 3.1 节遗留问题扩展出 `depends_on`/`last_verified_at`（需要独立授权和独立复核，不在本文档范围），届时唯一允许发生的联动是：知识条目所依赖的能力发生变化时，Tier-1 的 `affected` 结果里把它标成"not-opted-in"（因为它天然没有、也永远不会有 `check_command`），退化成一条"这份笔记的依据可能已经过时，建议人工重新核实"的纯提示信号——**绝不允许**因为这个信号存在就把知识条目升级为 Tier-2 执行候选。这是本文档明确写死的红线，不是留白等以后再定。

---

## 4. 子项间衔接与实施顺序建议

本节给出的是**技术依赖关系与独立授权颗粒度建议**，不是排期计划。四个"关口"对应四次相互独立的用户请示节点，而不是一份文档一次性拿到的"可以开始做"的许可——这是对可实现性审查"应拆成 4 次独立授权"意见的直接采纳。

```
Gate A（只读、零新写入路径）：       M8-4 Tier-1
Gate B（系统首个跨项目写入机制）：    M8-3 核心（draft/approve/reject/withdraw/amend，仅手填 JSON）
Gate C（发现与模糊证据层，需先补前置数据）：M8-1、M8-2（各自独立的数值/命中率前置验证）
Gate D（最高风险，执行第三方代码）：  M8-4 Tier-2
```

**依赖关系**（技术上的，而非顺序上强制的）：

- Gate D 在技术上依赖 Gate A 的产物——`affected` 需要 Gate A 算出的 `capability-changes.json`/`capability_reverse_index` 结果作为输入；因此 Gate D 不能早于 Gate A 完成。
- Gate D 在方法论上复用 Gate B 沉淀下来的安全模式（显式授权标志、`shell=False`、TOCTOU 收窄、新鲜度阈值 fail-closed）——不是技术上的强制依赖，但 Gate D 的安全设计假设了这些模式已经在 Gate B 里被验证过，不建议在 Gate B 尚未走完独立复核之前就开始设计或实现 Gate D。
- Gate C 中 `promote_capability.py draft --from-discovery-hit` 桥接子命令依赖 Gate B 的候选 schema 已经稳定，因此技术上晚于 Gate B。Gate C 中 M8-1 的部分本身不依赖 Gate B/D，只是因为"需要先产出独立的数值前置验证"这一共同特征而与 M8-2 归为同一关口，这是复核带宽/审批颗粒度的分组理由，不是技术依赖。
- Gate A 与 Gate B 之间没有技术依赖，可以并行推进；但因为 Gate A 是零边际风险、能立刻产生独立价值的只读能力，建议不与 Gate B 的复核占用同一批审查带宽——让 Gate B（系统首个跨项目写入机制）获得不被稀释的复核注意力。

**每个关口的复核强度**：Gate B（首个跨项目自动写入机制）与 Gate D（执行第三方项目代码）按用户既有规则属于"安全/发布"级别变更，各自独立走完整的 Claude opus/max 迭代复核，并在各自被判定为完成、即将进入合并/部署前，各自单独追加一轮 Codex sol+max 终审（`[强制双复核]` 标记）——两个关口的终审互不替代、不能共用同一次结果。Gate A、Gate C 走标准的 opus/max 迭代复核即可，只有在 M8 整体工作被判定为全部收尾时才需要补一轮覆盖全局的终审，不需要在每个只读关口各自终审一次。

---

## 5. 硬约束合规性说明（逐条对照）

| # | 约束 | 状态 | 说明 |
|---|---|---|---|
| 1 | 不能有任何一个项目的进程写入另一个项目的目录 | **有条件符合** | 唯一例外是 `promote_capability.py approve`，已在 3.4.1 独立声明并列出全部门控条件；除此之外零写入 |
| 2 | SessionStart 不能调用发现型索引器 | **符合** | 四个子项一致自律，`catalog_session_hint.py` 全程零改动 |
| 3 | 候选落地不能触发 `AUTHORITY_TRACKED_PATHS` NACK | **修复后符合** | `approve` 改为默认原子完成"写入+提交"，并新增 manifest 重签的不可抑制提醒（3.4.4）；此前的合并草案在此项上不符合，本轮已直接修复 |
| 4 | 不做静默降级、不做无法验证的 AI 自动判断黑箱步骤 | **符合** | 生成器类必须在 JSON 正文具名字段体现降级；M8-1 100% 确定性字符串匹配；`approve` 强制人工具名签字；M8-4 的裁决唯一来源是受影响项目自己提交命令的真实退出码 |
| 5 | exit code 一致性 | **符合** | 3.0.2 节统一为生成器 / 查询器 / 决策类三套语义，覆盖全部脚本 |
| 6 | 不做自动发布/自动装到别的项目代码 | **符合** | `approve` 只写"能力自己所属"的一个项目的元数据 + 知识正文，从不复制可执行代码；Tier-2 只执行已存在、已提交的命令,不生成不注入不修改任何项目代码 |

---

## 6. 已知风险与未决问题

1. **`wiki_pages` 缺少 `depends_on`/`last_verified_at`**（3.1 节裁决的直接遗留）：知识型内容目前无法获得依赖解析和新鲜度提醒。修复需要独立扩展 `orca-context-wiki.json` schema 并改动 `build_cross_project_catalog.py` 聚合循环，必须走独立授权 + 独立复核。
2. **M8-2 候选量与 M8-1 假阳性率的耦合放大效应**：M8-2 一旦开始把大量新脚本/新知识条目提升进 `catalog.json`，`capabilities[] ∪ wiki_pages[]` 的规模扩大会直接抬高 M8-1 文本匹配的偶然重叠概率——两者共享同一个"假阳性预算"，若两者都在将来分别获得独立授权，应该用同一批真实数据联合验证参数，而不是各自独立评估。
3. **`discover_capability_candidates.py` 的报告类文件名启发式假阳性率未经真实数据验证**（M8-2 独立授权的前置条件之一，3.3.3 节）。
4. **Tier-2 无真正 OS 级沙箱**，后续加固方向是迁移到 Orca 的一次性沙箱环境，需独立授权。
5. **`capability_ref_index` 在 approve 时的时效性**：approve 用来判定 `depends_on` 可解析性的索引来自上一次人工触发的 `catalog.json` 构建，可能滞后。建议采纳与 Tier-2 相同的"新鲜度阈值 + 显式覆盖标志"模式，让 `approve` 和 `run` 共用同一套"过期数据默认拒绝、需要显式 override"的纪律。
6. **`candidate_id` 稳定性/内容变更后的 supersede 语义**未决。
7. **审批疲劳/橡皮图章风险**：批量场景下 `--confirm-non-human-source` 可能被机械跳过，是组织流程问题，非纯技术可解。
8. **`depends_on_ref`（`compat-check.json`）与 `depends_on`（`reusable-capabilities.json`）的拼写漂移**，只能靠运行时报告暴露，无法根治。
9. **暂存区/台账/日志的无限增长**：`promotion-ledger.jsonl`、`capability-discovery/runs/`、`compat-runs/<run_id>/` 都是只追加或按 run 累积，长期运行会无限增长。不阻塞设计，但应作为共享的后续保留策略问题记录，不要在各子项里各自发明不同的清理规则。
10. **142+ worktree 场景下 M8-2 全树遍历的 I/O 代价**未经真实数据验证（M8-2 独立授权前置条件之一）。
11. **CJK 词边界在 M8-1 匹配中无干净解法**，只能靠长度下限部分缓解，需要数值验收线（M8-1 独立授权前置条件之一，3.2.4 节）。
12. **`wiki_edit_guard.py` 子进程调用的哈希校验窗口，比字面描述更窄**（本轮新增；此处描述的是 Gate B round 5 之后当前已落地的实现，机制可能在后续轮次中被进一步修订；本条经独立复核指出原措辞过度美化后已改写）：`approve` 每次被调用时，在该次调用开始处对 `wiki_edit_guard.py` 当前字节内容现场重新计算一次 SHA-256（`pin_wiki_edit_guard_sha256()`），紧接着（零间隔语句）用 `verify_wiki_edit_guard_sha256()` 再读一次并比对——这一读一比之间几乎不可能被打断，实际意义有限。这一对调用发生在知识正文写入（`atomic_write_in_dir`）**之前**，而真正的 subprocess 调用在其后约 130 行才发生，中间跨越了知识正文写入这一次真实文件系统 mutation；这段"verify→spawn"窗口**没有**对 `wiki_edit_guard.py` 本身重新做哈希校验（`round-5-fix` 那次补的再校验只针对 `wiki_path` 的 containment/identity，见 2379-2382 行注释，不覆盖 guard 二进制自身）。换句话说，这是"同次调用内不跨进程持久化的哈希校验"，但不是"紧邻 subprocess 调用前的校验"——`promote_capability.py` 里 `verify_wiki_edit_guard_sha256()` 的函数文档字符串同样需要按此更正（已同步修正，见其 docstring）。哈希值本身确实不跨 `approve` 调用持久化，不存在需要人工维护的同步 pin：`wiki_edit_guard.py` 的合法变更会在下一次 `approve` 调用时被自动、无感知地读到并使用，没有运维步骤需要人工同步执行,也就不存在"忘记同步导致 fail-closed 拒绝执行"这条风险——这部分结论仍然成立,本条修正的只是"校验时点"这一句的表述。若要收紧这段窗口（把 `verify_wiki_edit_guard_sha256()` 移到紧邻 2384 行 subprocess 调用之前,与 2379-2382 的再校验并列执行）,是对 `approve` 安全序列的重排,需要独立走一次专门的 final gate,不在本轮范围内。
13. **manifest 重签的运维纪律依赖人工及时执行**（本轮新增）：`approve` 成功后目标项目 SessionStart 会持续 fail-closed 直到重签，这是设计上"预期且已充分提示"的行为，但重签本身仍依赖人工不忘记执行，存在被搁置的运营风险。
14. **`compat-check.json` 的 `reviewed_by`/`reviewed_at` 为自证字段**，无独立第三方校验，因已置于目标项目正常 PR/评审可见性之下暂不阻塞，记录为后续加固点。
15. **`--compat-runs-root` 符号链接校验的一个已接受残留缺口**（本轮新增，补记入风险登记表；机制本身非本轮改动，是既有实现，此前遗漏在本节列出）：`_nearest_existing_ancestor_is_symlink()`（`check_cross_project_compatibility.py:717`）只向上走到"第一个在磁盘上确实存在的节点"为止并检查该节点是否是符号链接,这是为了不误伤 `/var`、`/tmp` 这类系统级、良性的符号链接前缀（`tempfile.mkdtemp()` 产生的目录天然如此）。该函数自己的 docstring 已经明确写出一个被接受的残留场景：如果攻击者能预先把最终请求目录本身（而不是它的某个祖先）伪造成一个看起来正常的真实目录/符号链接,让它先于校验存在于磁盘上,walk 会在这个已存在的叶子节点处停下,不再检查它上方那个真正的符号链接祖先,校验会放行。因为每次运行的实际写入目的地仍然是 `_allocate_run_id` 现场生成的不可预测 uuid4 子目录,攻击者预先布置的诱饵无法命中某一次具体运行的真实输出路径,所以该残留缺口只能让输出**落到攻击者已经完全控制的位置**,不能读取或篡改某次具体运行的真实产出。此前该残留缺口只写在代码 docstring 里,未同步登记进本节风险清单,现补记于此。

---

## 7. 与两个驱动场景的具体映射

### 7.1 场景 A：X 算法调研笔记

**v1 路径（仅 Gate A/B，不依赖 Gate C）**：

1. 某项目会话正常产出 `reports/X-ALGORITHM-RESEARCH-NOTES-....md`，不需要任何特殊动作。
2. 人工判断这份笔记值得跨项目共享，直接执行 `promote_capability.py draft`，手填 JSON（`proposed_target: "orca-context-wiki.json"`，含 summary、正文引用或暂存的 `content.md`）。工具跑校验（路径存在性/containment、摘要长度、`content_version` 预检查、3.4.3 节的去重 key/内容哈希碰撞检查）。
3. 校验通过后 `promote_capability.py approve --approved-by "..." --rationale "..."`——原子完成：子进程调用 `wiki_edit_guard.py` 校验并写入目标项目 `wiki/orca-context-wiki.json` 的 `pages[]`（`content_version` +1），把 `content.md` 正文（如有）搬进 `wiki/knowledge/<id>.md`，同一次调用内 `git add`+`commit`，追加台账；若该路径被 `reviewed-startup-pack-manifest.json` 钉住，强提示重签需求。
4. 人执行既有重签流程（不在本文档范围内定义，复用既有机制），恢复目标项目 SessionStart。
5. 下次 `build_cross_project_catalog.py build` 跑起来时，这个新页面自然进入 `catalog.json` 的 `wiki_pages[]`——这是它第一次、也是唯一一次"变得可见"的方式。
6. 从此以后：`query_catalog.py search` 能直接搜到它（既有能力，零改动）；它不出现在 `capability_reverse_index` 里，也不被 `detect_capability_changes.py` 的允许清单选中——对 M8-4 完全不可见，永不触发、永不参与 Tier-1/Tier-2（3.5.4 节永久规则）。

**未来增强路径（Gate C 中 M8-1、M8-2 分别获得独立授权后）**：第 2 步可以换成"`discover_capability_candidates.py scan` 自动命中这份笔记生成 discovery hit → 人工 `review_capability_candidates.py mark --triaged-for-promotion` → `promote_capability.py draft --from-discovery-hit` 桥接成候选"，只是把"人工发现+手填"换成"自动发现+人工审核+桥接"，第 3 步以后的行为完全不变。第 6 步之后，一旦 M8-1 上线，`build_mention_evidence.py` 下一次运行会自动、免费把这条新页面纳入 text_mention 扫描范围，不需要因为这条新页面而改代码。

### 7.2 场景 B：自动学习候选（`write_candidate_capture.py`）

**v1 路径（无 M8-2 适配器）**：与场景 A 唯一的区别在于第 2 步——人工从 `write_candidate_capture.py`（一个独立进行中的项目,输出格式尚未确定）的实际产出里转抄/整理内容,手填进候选 JSON 的 `source.mechanism` 字段（例如填 `"external-capture-manual-transcription"`,只要不是 `"human"` 即视为非人工来源）。第 5 步 approve 时因为 `source.mechanism != "human"`,强制要求额外的 `--confirm-non-human-source` 标志。其余步骤（校验、唯一写入口、聚合器重新收录、下游可见性、对 M8-4 不可见的边界）与场景 A 完全相同——两个场景共用同一条流水线,不是两套平行机制。

**未来路径（M8-2 获得独立授权、且 `write_candidate_capture.py` 输出格式确定之后）**：可以单独设计一个真正的外部适配器，把 `write_candidate_capture.py` 的输出自动转成 discovery hit，免去人工转抄这一步。这个适配器的具体接口形状现在设计缺乏信息基础，必须等两个前提都成立后再单独设计，本文档不为它预留任何接口占位（2.2 节第 4 条采纳的直接结果）。

### 7.3 与 M8-1 声明依赖的协同（能力型候选，两个场景通用）

若提升的是 script/skill/config-pattern 型候选且带 `depends_on`，发布后立刻在 `catalog.json` 里产生一条 `declared_dependency` 级证据，同时具备 M5/M7 既有的新鲜度提醒资格，也会被 `detect_capability_changes.py`/`check_cross_project_compatibility.py affected` 纳入兼容性受影响面。M8-1 获得独立授权上线后，若其模糊匹配发现"文本上很像但没声明依赖"，这条建议只能通过 `promote_capability.py amend --add-depends-on ... --source mention-evidence:<edge-id>` 走完整审批才能变成正式声明——文本证据本身永远不会被任何自动化直接 patch 进已发布文件（对应第 5 节约束 4）。

---

**核实方式说明**：本次定稿过程中，除通读合并草案与三份审查全文外，额外用 Bash/Read 直接核对了 `validate_reusable_capabilities.py`（docstring 与 `KIND_VALUES` 定义）、`build_cross_project_catalog.py`（`KIND_VALUES` 定义、`capability_ref_index`/`capability_reverse_index` 构建循环的实际遍历范围）、`wiki_edit_guard.py`（`content_version` 强制递增闸门的存在性）、`reviewed-startup-pack-manifest.json`（对 `wiki/orca-context-wiki.json` 的真实 SHA-256 pin）四处对裁决起决定性作用的事实，未创建或修改任何生产文件。
