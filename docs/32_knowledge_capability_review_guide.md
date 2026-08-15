# Knowledge Capability Review Guide

## 1. Executive summary

ContinuCare Knowledge 当前解决的是：用版本、精确引用、来源定位、hash、lineage、
Gap 和审核事件解释“这项知识从哪里来、当前能支持什么、仍缺什么治理证据”。它
为内部知识运营、采集依据解释和 informational display 提供可重复的离线基础。

当前实现包括 v1 Knowledge Evidence、Core Symptom Catalog v2 readiness、
SourcePolicy、离线 acquisition、append-only ledger、EvidenceCandidate、审核机制、
readiness Gap registry 和冻结只读 DTO。当前没有真实 reviewer、正式许可决定、
正式 KnowledgeRelease 或 v2 alias UI consumer integration。

全局边界始终是：

- `knowledge_effect=informational_only`；
- `runtime_authority=none`；
- 不诊断、不治疗、不分诊、不作自动临床决策；
- 不把 Knowledge 描述成临床决策系统。

## 2. Capability matrix

| 能力 | 状态 | 可验证的当前范围 | 门禁或下一步 |
|---|---|---|---|
| v1 Knowledge Evidence | 已实现并验证 | versioned Source/Claim/Binding/Gap、精确 refs、CURRENT/HISTORICAL 只读加载 | 不授权 runtime；既有 API/manifest 保持兼容 |
| Core Symptom Catalog v2 | 已实现但仍被治理门禁 | 12 个 benchmark；9 reused、2 alias candidates、1 internal candidate | inherited mapping/alias 均未正式审核 |
| Core Symptom alias audit | 已实现但仍被治理门禁 | v2.4 hash-pinned technical audit；35 个 v1 aliases 完整覆盖 | 26 个 inherited aliases withheld；Gap open |
| SourcePolicy | 已实现并验证 | 精确 origin/path/query/MIME/size、operation default deny、rights posture | 五个来源 rights 仍 unresolved；live disabled |
| Offline acquisition | 已实现并验证 | synthetic fixtures、quarantine、Candidate/Snapshot/ChangeSet/Gap | 不处理真实医疗内容或患者数据 |
| SourceSnapshot / ChangeSet | 已实现并验证 | exact-byte digest、metadata digest、append-only history、change classification | 生产 promotion 仍需 formal decisions 和 closed Gaps |
| EvidenceCandidate | 已实现并验证 | body-free locator、whole-record digest、typed provenance、machine draft path | synthetic/non-release-ready；不能直达 v1 registry/runtime |
| Append-only ledger | 已实现并验证 | exact versions、predecessor digest、exclusive lock、no-overwrite、chain replay | 不替代正式 review/rights evidence |
| ReviewPacket / ReviewEvent / ReviewerVerifier | 已实现但仍被治理门禁 | packet pins、role/scope、identity/principal separation、attestation 验证机制 | 无真实 identity provider 或正式 reviewer |
| Readiness Gap registry | 已实现并验证 | 12 个 open Gaps；rights/live/socket/alias 问题显式化 | registry v1 只能 open；resolution 需 successor manifest |
| P1 source connectors | 已实现但仍被治理门禁 | DailyMed、EMA、MedlinePlus、PubMed、PMC metadata contracts；fake transport tests | operational live acquisition disabled；P1b 未执行 |
| v2 governance read model | 已实现并验证 | Source readiness、persistent Gap、release/consumer readiness 的只读投影 | 所有生产与 release readiness 仍 false |
| Core Symptom v2 DTO/API | 已实现但仍被治理门禁 | 12 条 frozen DTO；caller catalog 必须与 hash-pinned canonical catalog 完整相等 | 当前仅为 open-Gap readiness-only contract；`approved_match_aliases=()`；consumer integration false |
| Knowledge Ops 治理 UI | 已实现只读展示 | 仅消费 `ops.read_model`，展示来源策略、审核门、Gap 和发布状态 | 不读取患者数据、不导入 alias consumer API、不授权 runtime |
| Core Symptom alias UI consumer | 尚未实施 | 当前 UI 不导入 `catalog_read_model` | 需正式 alias review、successor Gap manifest、版本化 successor DTO/builder 和独立 UI 审核 |
| P1b live validation | 尚未实施 | default-off report 为 `not_attempted` | 需要单独授权、冷导入 socket 证明和隔离执行 |
| Formal KnowledgeRelease | 尚未实施 | release readiness/finalize fail-closed 机制存在 | 无正式 reviewer、rights decisions、selected artifacts 或 release approval |
| 患者匹配、临床规则、诊断、分诊、治疗 | 明确非目标 | Knowledge 不参与这些状态或结论 | 不能由本仓库当前 Knowledge 能力推断或宣传 |

## 3. 有代码与测试证据支持的优势

- Source、manifest、catalog、ledger entry 和 review event 都有明确 version/hash/
  lineage，可回放并定位来源。
- Ledger 和治理 manifests 使用 append-only 方式；旧 bundle index 继续原样加载，
  新能力通过 successor index 增量加入。
- Author identity/principal 与 reviewer identity/principal 分离，多角色门禁要求
  principal 分离。
- Synthetic approval 只能测试机制，不能冒充 formal approval 或生产 eligibility。
- Rights、live validation、cold-import proof 和 terminology alias review 都形成
  显式 open Gap，缺证据时 fail closed。
- 默认环境零网络、零真实患者数据；read API 不读取或写入 SQLite。
- Knowledge 不修改患者状态、Task、Observation、Summary、Alert、ClinicalRule
  或临床结论。
- v1 read API 与旧 manifests 保持兼容；v2 通过独立 frozen DTO 增量提供。
- 未知、未审核或语义有歧义的内容形成 Gap/withheld 状态，而不是自动补全。
- dizziness、decreased appetite、abdominal pain、dyspnea、bloating 和 rash 的
  不安全等价关系由结构化 boundary codes 和 withheld audit 隔离。
- 所有相关测试可以使用 synthetic fixtures 在离线临时目录确定性复现。

以上不能被扩写为临床成熟度、正式临床验证、完整知识覆盖、生产可用、正式许可
或正式审核声明。本项目当前不能自动诊断、分诊、治疗或提供患者个体化临床建议。

## 4. Architecture and data flow

```text
official/repository source
  -> SourcePolicy
  -> connector / quarantine
  -> SourceSnapshot / ChangeSet
  -> EvidenceCandidate / machine draft
  -> formal ReviewPacket / ReviewEvent / ReviewerVerifier gates
  -> versioned release / read API
  -> informational-only Knowledge UI
```

当前实际能力有两条停点：

1. 离线 synthetic acquisition 可以到 EvidenceCandidate 和 synthetic machine
   draft，但不能取得 formal reviewer/rights decision，也不能形成 production
   KnowledgeRelease；
2. Core Symptom v2 已形成 technical alias audit 和冻结 DTO，但
   `terminology_alias_review_pending` 仍 open，所有 inherited aliases 均不可匹配，
   UI integration 尚未开始。当前 DTO/builder 只能表达 open-Gap readiness；即使
   未来完成正式审核，仍需新的 hash-pinned readiness manifest、版本化 successor
   contract 及 resolved/approved 实现与测试，不能放宽当前 `Literal[False]` 模型。

五个真实来源仍受 rights/live-validation Gaps 阻断；P1b 为 `not_attempted`。
release intent 明确为 `readiness_only_blocked`。

## 5. UI contract

当前 Knowledge Ops 治理 UI 只读取 `continucare.knowledge.ops.read_model`，展示
来源、审核、Gap 和发布准备状态；它不枚举或消费未审核 alias。

未来 Core Symptom alias consumer UI 必须：

- 只读取 `continucare.knowledge.ops.catalog_read_model` 的 frozen DTO/API；
- 不直接读取或修改 raw manifests；
- 不使用 withheld aliases、preferred display labels 或 English benchmark labels
  进行患者文本匹配；
- 在 `consumer_integration_ready=false` 时明确表述 v2 aliases 尚未启用；
- 保留 `informational_only` 和 `runtime_authority=none` 边界；
- 保持 Knowledge 页面与患者事实、Task、Observation、Summary、Alert、
  ClinicalRule、状态机及故事完成判定隔离。

当前代码中 `app.py`、`pages/**`、`continucare/knowledge/render.py`、pathway、
Layer 4 和 runtime 均未导入 `catalog_read_model` 或使用新 v2 alias API；Knowledge
页面对 `ops.read_model` 的只读展示不改变该门禁。

## 6. 当前限制与剩余风险

- 没有正式 terminologist、rights officer 或 knowledge curator。
- `gap-core-symptom-catalog-terminology-alias-review-pending` 仍为 open。
- P1b live validation 未执行，`request_count=0`。
- DailyMed、EMA、MedlinePlus、PubMed、PMC 的真实 rights/live 状态均未解除。
- 没有正式 KnowledgeRelease、selected production artifact 或 release approval。
- 没有真实患者数据、患者表达研究或临床验证。
- 进程内 NCBI 限速不能替代多进程/多实例部署的共享限速设施。
- 官方 endpoint、schema 和 terms 可能变化，需要持续 ChangeSet/Gap 治理。
- 严格模型变化时必须同步维护 exact digest trust profiles；未知 path 继续
  default deny。
- Alias audit 是当前 v1/v2 catalog bytes 的 technical snapshot；catalog 变化必须
  创建新 artifact/index，不能原地重写。
- 当前 Core Symptom DTO/builder 是 open-Gap readiness-only contract；正式审核后
  仍需独立的版本化 successor DTO/builder、resolved/approved 实现和测试，再进入
  Knowledge UI 独立审核。

## 7. 审阅导航

### 设计与治理文档

- [`docs/25_knowledge_evidence_foundation.md`](25_knowledge_evidence_foundation.md)：
  v1 Source/Claim/Binding/Gap 基础、加载模式和运行时隔离。
- [`docs/27_m5_k_symptom_knowledge_expansion.md`](27_m5_k_symptom_knowledge_expansion.md)：
  symptom-centered reference-only index 和外部来源边界。
- [`docs/28_knowledge_ops_p0_p1_p2_readiness.md`](28_knowledge_ops_p0_p1_p2_readiness.md)：
  historical P0–P2 architecture/readiness baseline，用于理解初始治理、离线
  acquisition、review/release 机制；其中的能力状态已由本指南、
  [`docs/30_knowledge_ops_p1_source_connectors.md`](30_knowledge_ops_p1_source_connectors.md)、
  [`docs/31_knowledge_v2_alias_consumer_readiness.md`](31_knowledge_v2_alias_consumer_readiness.md)
  和
  [`docs/knowledge_v2_mainline_integration_validation_2026-08-14.md`](knowledge_v2_mainline_integration_validation_2026-08-14.md)
  取代。
- [`docs/30_knowledge_ops_p1_source_connectors.md`](30_knowledge_ops_p1_source_connectors.md)：
  P1 connector、SSRF/privacy、digest trust、persistent Gaps 和最终修复证据。
- [`docs/31_knowledge_v2_alias_consumer_readiness.md`](31_knowledge_v2_alias_consumer_readiness.md)：
  9 个 reused concept 的完整 alias audit、frozen DTO 和正式解除步骤。

### 关键生产入口

- `continucare/knowledge/registry.py`：v1 Knowledge read registry；
- `continucare/terminology/catalog.py`：v1 terminology catalog；
- `continucare/terminology/core_catalog.py`：Core Symptom Catalog v2；
- `continucare/knowledge/ops/manifests.py`：hash-pinned v2 bundle loader；
- `continucare/knowledge/ops/acquisition.py`、`connectors.py`、`evidence.py`：
  staged acquisition 与 EvidenceCandidate；
- `continucare/knowledge/ops/store.py`：append-only ledger；
- `continucare/knowledge/ops/review.py`：ReviewPacket/Event/Verifier gates；
- `continucare/knowledge/ops/read_model.py`：governance readiness read model；
- `continucare/knowledge/ops/catalog_read_model.py`：frozen Core Symptom v2 DTO/API。

### 关键测试

- `tests/test_knowledge_registry.py`；
- `tests/knowledge_ops/test_core_symptom_catalog_v2.py`；
- `tests/knowledge_ops/test_core_symptom_alias_audit.py`；
- `tests/knowledge_ops/test_core_symptom_catalog_read_api.py`；
- `tests/knowledge_ops/test_manifest_history_v2.py`；
- `tests/knowledge_ops/test_readiness_gap_registry.py`；
- `tests/knowledge_ops/test_offline_import_guards.py`；
- `tests/knowledge_ops/test_evidence_candidate_promotion.py`；
- `tests/test_knowledge_ops_acquisition.py`；
- `tests/test_knowledge_ops_review_release.py`；
- `tests/knowledge_ops/test_privacy_guard_technical_ids.py`；
- `tests/knowledge_ops/test_promotion_import_isolation.py`。

## 8. Verification snapshot

本次 BLOCKER 修复的验证基线 HEAD 为
`db485c45caf59c8a715361924bbcfbe405097d38`；测试工作树同时包含 canonical
catalog binding、负向测试和两份文档修正。包含本文件的 Git commit 无法在自身
内容中自引用其 SHA，因此最终 branch HEAD 在同一执行报告中精确记录，不以占位
值冒充已知 commit。

| 验证 | 结果 |
|---|---|
| Alias audit/read API 定向测试 | exit 0；`27 passed in 0.85s` |
| `tests/knowledge_ops` | exit 0；`193 passed in 5.15s` |
| 全量 pytest | exit 0；`802 passed, 3 skipped in 21.19s` |
| Skip 原因 | 仅未设置官方 `FHIR_R4_SCHEMA_ZIP`，与基线一致 |
| Compileall | exit 0；无输出 |
| 基线至 implementation HEAD diff check | exit 0 |
| 增量网络/数据探针 | socket/DNS/HTTP/API/SQLite 均为 0 |
| P1b | `not_attempted`；`request_count=0` |
| 主数据库 | SHA-256 `0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585`；size `311296`；mtime epoch `1786644509`，前后不变 |

三个 skip 的精确位置为：

- `tests/test_fhir_conformance.py:114`；
- `tests/test_layer4_rules_tasks.py:620`；
- `tests/test_layer4_summaries.py:727`。
