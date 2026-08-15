# Knowledge v2 Alias Consumer Readiness

## 1. 结论与边界

本切片完成的是 Core Symptom Catalog v2 的 **technical boundary audit** 和
只读 consumer contract，不是 terminologist approval、translation approval、
clinical validation 或患者表达验证。

当前固定事实：

- `knowledge_effect=informational_only`；
- `runtime_authority=none`；
- `release_ready=false`；
- `consumer_integration_ready=false`；
- `contains_patient_data=false`；
- P1b 为 `not_attempted`，`request_count=0`；
- 没有真实网络、患者数据、临床批准或正式 reviewer identity。

本切片没有新增或宣称任何 SNOMED、ICD、LOINC、MedDRA 或其他外部编码
映射。`bloating` 仍只是 `abdominal-distension` 的 alias candidate，`rash`
仍只是 `skin-eruption` 的 alias candidate，`chest-pain` 仍是没有外部 code
的 internal candidate。

## 2. Append-only artifact

新增文件：

- `continucare/knowledge/manifests_v2/core_symptom_alias_audit_v1.json`；
- `continucare/knowledge/manifests_v2/bundle_index_v2_4.json`。

alias audit 的 raw-byte SHA-256 为
`b1c78809fabcf4ba2974a0190d1a9dfc57db8ec7dc935f6f93644b38ef2a637d`，
size 为 `12,839` bytes。它同时固定：

- Core Symptom Catalog v2 identity/version 和 raw-byte digest；
- GLP1 terminology catalog v1 identity/version 和 raw-byte digest；
- 恰好 9 个 reused concept；
- 每个 concept 的完整、有序 `aliases_zh` 集合；
- v1 preferred label、其他 inherited alias 和 v2 English benchmark label
  之间的不同用途。

loader 在返回 bundle 前重算两个 catalog digest，并逐项比较 concept、preferred
label、English benchmark label、alias 顺序和完整集合。删除、增加、改写 alias，
改变 catalog identity/version，或只修改 artifact 后重新 pin index，都会 fail
closed。

`bundle_index_v2.json`、`bundle_index_v2_2.json`、
`bundle_index_v2_3.json`、`readiness_gaps_v1.json` 和两个 catalog 均未原地
修改；旧 index 继续独立加载，且不会回填 alias audit。

## 3. 9 个 concept 的完整 alias 审计

v1 的 35 个 alias 均且仅均出现一次：9 个 preferred label 只允许显示，另外
26 个 inherited alias 全部为
`withheld_pending_formal_terminology_review`。所有条目的 `matchable=false`、
`semantic_equivalence_status=not_established`、
`formal_terminology_review_completed=false`。

| Concept | 仅显示的 v1 preferred label | Withheld inherited aliases | Withheld 理由 |
|---|---|---|---|
| `nausea` | 恶心 | 反胃；想吐 | 未正式审核的口语表达 |
|  |  | 胃里难受 | 宽泛或解剖/症状范围有歧义 |
| `vomiting` | 呕吐 | 吐了；吐过；一直吐 | 未正式审核的口语表达 |
| `diarrhea` | 腹泻 | 拉肚子 | 未正式审核的口语表达 |
|  |  | 大便稀；稀便 | 大便性状不能自动视为临床等价 |
| `abdominal-pain` | 腹痛 | 肚子痛；肚子疼；胃痛 | 部位和症状范围可能不同，不能自动等价 |
| `constipation` | 便秘 | 排便困难；解不出来 | 功能描述不能自动视为临床等价 |
|  |  | 大便干 | 大便性状不能自动视为临床等价 |
| `decreased-appetite` | 食欲下降 | 没胃口；食欲差 | 未正式审核的口语表达 |
|  |  | 不想吃东西 | 行为/功能描述不能自动等同食欲下降或 reduced intake |
| `fatigue` | 疲劳 | 疲倦 | 未正式审核的口语表达 |
|  |  | 很累；没精神 | 宽泛、非特异表达需要语义范围审核 |
| `dizziness` | 头晕 | 晕乎乎；头昏 | 必须与 vertigo、presyncope、syncope 等亚型区分 |
| `dyspnea` | 呼吸困难 | 喘不上气；气短；不好呼吸 | 不能由表达自动推断急诊、红旗或严重程度 |

其中“胃里难受、胃痛、大便稀、稀便、大便干、解不出来、没精神、晕乎乎、
头昏、不想吃东西、气短”均有显式 withheld 记录，但审计并不限于这些例子。

v1 preferred label 也没有患者表达审核或 clinical equivalence；它只作为
display label。9 个 v2 English benchmark label 只作为 benchmark/display label，
统一为 `benchmark_display_only_pending_formal_translation_review`，不能冒充
正式翻译审核。

## 4. 冻结只读 DTO/API

唯一新增入口是：

`continucare.knowledge.ops.catalog_read_model`

稳定函数：

- `load_builtin_core_symptom_catalog_read_model()`；
- `list_core_symptom_records()`；
- `get_core_symptom_record(benchmark_key)`；
- `get_core_symptom_alias_readiness()`；
- `get_core_symptom_gap_resolution_readiness()`；
- `build_core_symptom_catalog_read_model(bundle, catalog)`，只提供当前 open-Gap
  readiness-only contract，没有 readiness/override 参数。builder 先重算 repository
  canonical catalog raw bytes 的 SHA-256 并与 alias audit pin 比较，再将 caller
  catalog 的完整 12-record 结构与 canonical catalog 比较；任一字段不同都会在
  DTO 生成前 fail closed，后续投影只使用 canonical catalog。

`CoreSymptomRecordReadDTO` 固定暴露：

- `catalog_id`、`catalog_version`；
- `benchmark_id`、`benchmark_key`；
- `preferred_zh`、`preferred_en`、`display_labels`；
- `concept_status`、`existing_concept_ref`、`candidate_target_ref`；
- `mapping_status`、`semantic_boundary_codes`；
- `withheld_alias_count`、`withheld_aliases`；
- `approved_match_aliases`；
- `terminology_review_status`、`open_gap_ids`；
- `consumer_integration_ready`；
- `knowledge_effect`、`runtime_authority`。

所有 DTO 均是 Pydantic `frozen=True`、`extra=forbid`，集合使用 tuple，12 条
record 的顺序固定为 catalog benchmark 顺序。未知 benchmark 抛出
`LookupError`。API 不返回可修改的 raw manifest，不导入 SQLite、网络、患者
store、pathway、Layer 4 或 runtime。

当前每一条 record 的 `approved_match_aliases=()`；调用者不能通过参数、本地
布尔值或修改返回对象把 alias 变成可匹配状态。

## 5. Gap resolution readiness

当前 DTO 从 hash-pinned `readiness_gaps_v1.json` 和
`review_policy_v2.json` 实际派生：

| 字段 | 当前值 |
|---|---|
| `gap_id` | `gap-core-symptom-catalog-terminology-alias-review-pending` |
| `lifecycle` | `open` |
| `required_gate` | `terminology_mapping_promotion` |
| `required_roles` | `terminologist`, `rights_officer`, `knowledge_curator` |
| `formal_decision_present` | `false` |
| `valid_attestations_present` | `false` |
| `successor_manifest_present` | `false` |
| `resolution_permitted` | `false` |
| `consumer_integration_ready` | `false` |

缺失的正式证据包括：精确 alias audit 的正式 Review Packet；三种角色各自的
非 synthetic、formally verified、attested ReviewEvent；不同 reviewer identity
和 principal；每个决定的 ReviewerVerifier attestation；hash-pinned successor
readiness manifest；解除后的独立 consumer review。

现有机制保证：

- synthetic ReviewEvent 不计入 production/release readiness；
- 同一 identity 或同一 principal 不能满足多角色门禁；
- Codex、Claude 或其他模型输出不是 reviewer identity、ReviewEvent 或
  verifier attestation；
- 本地布尔值不能解除门禁；
- 删除当前 Gap 会使 manifest 或 consumer read model 加载失败；
- registry v1 只能表达 `lifecycle=open`，不存在可伪造的 resolved 字段；
- 没有 successor manifest 和有效 attestation 时保持 fail closed。

本轮没有创建 resolved successor manifest，也没有伪造 terminologist、rights
officer、knowledge curator 或任何正式决定。

## 6. UI contract

当前 UI 集成尚未实施。未来 Knowledge UI 只能：

- 读取本文件定义的冻结 DTO/API；
- 显示 preferred/benchmark labels、withheld 状态、Gap 和治理边界；
- 在 `consumer_integration_ready=false` 时明确显示 v2 alias 未启用。

UI 禁止：

- 直接读取 raw manifests；
- 使用 withheld alias 或 preferred display label 进行患者文本匹配；
- 把 English benchmark label 表述成正式翻译；
- 宣称 alias 已 approved、verified、equivalent 或 safe to match；
- 让 Knowledge 页面影响患者事实、Task、Observation、Summary、Alert、
  ClinicalRule、状态机或故事完成判定。

当前 DTO 使用 `Literal[False]`、`approved_match_aliases` 的 `max_length=0`，并要求
exact open Gap。它刻意不能表示 resolved/approved 状态；未来不得通过放宽当前
模型来复用这一版本。

## 7. 真实 reviewer 到位后的治理与 successor 实现

1. 为精确 alias audit 创建正式 Review Packet；
2. terminologist、rights officer、knowledge curator 三个合格、非 synthetic、
   principal 分离的角色完成现有 attested ReviewEvent；
3. ReviewerVerifier 验证当前身份、授权、scope、principal separation 和
   attestation；
4. 创建 hash-pinned successor readiness manifest，旧 manifest 保持可加载；
5. 设计新的版本化 successor DTO/builder contract，不修改当前 open-Gap contract；
6. 独立实现 resolved/approved 状态及其 fail-closed 测试；
7. 重新加载 successor bundle，通过新 contract 重新计算 consumer readiness；
8. 对 successor API、DTO 和匹配边界进行独立审核；
9. Knowledge UI 完成独立审核并另行授权后，才可使用正式获批的 matchable aliases。

因此，真实 reviewer 到位不是本能力的最后一个动作；之后仍有明确、独立的代码、
测试和 UI 审核切片。

## 8. Verification snapshot

本次 BLOCKER 修复的验证基线 HEAD 为
`db485c45caf59c8a715361924bbcfbe405097d38`；最终包含本次修复的 branch HEAD
在执行报告中记录，避免在 commit 内容中伪造自引用 SHA。全部 Python 命令使用
独立 `mktemp` 目录、临时 `PYTHONPYCACHEPREFIX`、临时
`CONTINUCARE_DB_PATH` 和冻结离线环境。

- alias audit/read API 定向测试：`27 passed in 0.85s`，exit 0；
- `tests/knowledge_ops`：`193 passed in 5.15s`，exit 0；
- 全量 pytest：`802 passed, 3 skipped in 21.19s`，exit 0；
- 三个 skip 仍只因未设置官方 `FHIR_R4_SCHEMA_ZIP`；
- `python -m compileall -q continucare app.py pages`：exit 0；
- `git diff --check 5779238040212bbe7edfee968081c48527fbcfd7..HEAD`：
  exit 0；
- 增量探针计数：`socket=0`、`dns=0`、`http=0`、`api=0`、`sqlite=0`；
- P1b：五条记录均为 `not_attempted`，`request_count=0`；
- 主数据库前后均为 SHA-256
  `0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585`、
  size `311296`、mtime epoch `1786644509`。
