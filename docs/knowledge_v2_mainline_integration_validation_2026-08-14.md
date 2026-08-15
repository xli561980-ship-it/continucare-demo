# ContinuCare Knowledge v2 主线集成验证

日期：2026-08-14
结论：**PASS — 合入、自动验证、真实 Browser smoke、数据不变性和范围核查均无 blocker**

## 1. 目标与非目标

本切片将已推送的 `codex/knowledge-v2-alias-readiness` 完整合入 `codex/docs-collaboration-init`，保留 Knowledge v2 的历史提交，并对 Knowledge、既有 A++ UI、关键业务路径、全仓测试、离线/数据边界和真实 Browser 交互进行集成验证。

本切片不实施 Knowledge v2 alias UI consumer integration，不修改现有 UI 页面、CompetitionDemo 状态机、业务 service/FHIR 合同、数据库/schema、配置、依赖或外部适配器；不执行真实来源采集、live validation、外部发送、部署或生产操作。

## 2. Git 安全闸门与 merge

### 2.1 合入前精确状态

```text
branch: codex/docs-collaboration-init
HEAD: f315a6b6bf69d6c01927c53115a266d144ed09ab
upstream: f315a6b6bf69d6c01927c53115a266d144ed09ab
behind/ahead: 0/0
Knowledge local: 0110c319a58e2de7baa92e83788a56113bc62a0c
Knowledge origin: 0110c319a58e2de7baa92e83788a56113bc62a0c
merge-base: 1139dea9c9c4189802c2879ffa12add5a78dadef
主线/Knowledge 独有提交: 5 / 38
worktree status: ?? docs/ui_product_design_brief_2026-08-13.md
```

既存三个其他 worktree 的路径与 SHA 均符合任务给定状态，本次未修改、删除或清理它们。未执行 pull、fetch、stash、reset、clean 或切换分支。

### 2.2 merge 方式与结果

```text
command: git merge --no-ff --no-commit codex/knowledge-v2-alias-readiness
result: automatic merge; no conflicts; stopped before commit
merge commit: 88c85b3fb103e13f0770f385f01a0f1916a135ff
first parent: f315a6b6bf69d6c01927c53115a266d144ed09ab
second parent: 0110c319a58e2de7baa92e83788a56113bc62a0c
```

该 merge 保留了 Knowledge 分支 38 个历史提交；没有 squash、rebase、cherry-pick、force push 或改写历史。

## 3. 精确合入范围

合入范围为 **66 个新增文件、22,859 行新增、0 删除**。没有覆盖现有 UI-3 至 UI-6 文件，没有修改现有 UI、数据库、schema、业务 service/FHIR 合同、配置、依赖或外部适配器。

```text
A continucare/knowledge/manifests_v2/__init__.py
A continucare/knowledge/manifests_v2/bundle_index_v2.json
A continucare/knowledge/manifests_v2/bundle_index_v2_2.json
A continucare/knowledge/manifests_v2/bundle_index_v2_3.json
A continucare/knowledge/manifests_v2/bundle_index_v2_4.json
A continucare/knowledge/manifests_v2/core_symptom_alias_audit_v1.json
A continucare/knowledge/manifests_v2/coverage_profiles_v2.json
A continucare/knowledge/manifests_v2/readiness_gaps_v1.json
A continucare/knowledge/manifests_v2/release_intent_v2.json
A continucare/knowledge/manifests_v2/review_policy_v2.json
A continucare/knowledge/manifests_v2/safety_boundary_v2.json
A continucare/knowledge/manifests_v2/source_policies_v2.json
A continucare/knowledge/manifests_v2/source_policies_v2_2.json
A continucare/knowledge/ops/__init__.py
A continucare/knowledge/ops/acquisition.py
A continucare/knowledge/ops/catalog_read_model.py
A continucare/knowledge/ops/connectors.py
A continucare/knowledge/ops/evidence.py
A continucare/knowledge/ops/manifests.py
A continucare/knowledge/ops/models.py
A continucare/knowledge/ops/promotion.py
A continucare/knowledge/ops/read_model.py
A continucare/knowledge/ops/release.py
A continucare/knowledge/ops/review.py
A continucare/knowledge/ops/security.py
A continucare/knowledge/ops/source_connectors/__init__.py
A continucare/knowledge/ops/source_connectors/common.py
A continucare/knowledge/ops/source_connectors/contracts.py
A continucare/knowledge/ops/source_connectors/dailymed.py
A continucare/knowledge/ops/source_connectors/ema.py
A continucare/knowledge/ops/source_connectors/errors.py
A continucare/knowledge/ops/source_connectors/flags.py
A continucare/knowledge/ops/source_connectors/live_validation.py
A continucare/knowledge/ops/source_connectors/medlineplus.py
A continucare/knowledge/ops/source_connectors/parsing.py
A continucare/knowledge/ops/source_connectors/pubmed.py
A continucare/knowledge/ops/source_connectors/transport.py
A continucare/knowledge/ops/store.py
A continucare/terminology/core_catalog.py
A continucare/terminology/data/core_symptom_catalog_v2.json
A docs/28_knowledge_ops_p0_p1_p2_readiness.md
A docs/30_knowledge_ops_p1_source_connectors.md
A docs/31_knowledge_v2_alias_consumer_readiness.md
A docs/32_knowledge_capability_review_guide.md
A tests/fixtures/knowledge_ops/acute_high_risk.txt
A tests/fixtures/knowledge_ops/catalog.json
A tests/fixtures/knowledge_ops/chronic_cardiopulmonary.txt
A tests/fixtures/knowledge_ops/medication_followup.txt
A tests/fixtures/knowledge_ops/oncology_pro.txt
A tests/fixtures/knowledge_ops/rare_terminology.txt
A tests/knowledge_ops/conftest.py
A tests/knowledge_ops/test_core_symptom_alias_audit.py
A tests/knowledge_ops/test_core_symptom_catalog_read_api.py
A tests/knowledge_ops/test_core_symptom_catalog_v2.py
A tests/knowledge_ops/test_evidence_candidate_promotion.py
A tests/knowledge_ops/test_manifest_history_v2.py
A tests/knowledge_ops/test_offline_import_guards.py
A tests/knowledge_ops/test_privacy_guard_technical_ids.py
A tests/knowledge_ops/test_promotion_import_isolation.py
A tests/knowledge_ops/test_readiness_gap_registry.py
A tests/knowledge_ops/test_source_connector_contracts.py
A tests/knowledge_ops/test_source_parser_security.py
A tests/knowledge_ops/test_source_transport_security.py
A tests/test_knowledge_ops_acquisition.py
A tests/test_knowledge_ops_governance.py
A tests/test_knowledge_ops_review_release.py
```

## 4. 验证环境与离线门禁

所有 Python 命令均使用项目 `.venv/bin/python`。验证根目录由 `mktemp` 在 `/tmp` 创建；每类验证使用仓库外的 `PYTHONPYCACHEPREFIX` 和 `CONTINUCARE_DB_PATH`。显式环境边界：

```text
CONTINUCARE_EXTERNAL_EGRESS_ENABLED=false
CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION=false
CONTINUCARE_FEISHU_MODE=mock
CONTINUCARE_AILY_MODE=mock
CONTINUCARE_BITABLE_MODE=disabled
all external test-tenant flags=false
FHIR_R4_SCHEMA_ZIP unset
```

没有下载新依赖，没有启用真实 connector、live validation、真实发送或非 localhost 网络。Knowledge targeted 覆盖 offline import/socket/transport/security、promotion isolation、privacy guard、Gap/readiness、review/release 和 v1/UI 回归。

## 5. 自动测试与静态检查

| 验证 | 结果 |
|---|---|
| Knowledge targeted | `448 passed in 10.07s`，exit 0 |
| UI targeted | `228 passed in 3.07s`，exit 0 |
| 关键业务回归 | `35 passed in 2.36s`，exit 0 |
| 全量 pytest | `891 passed, 3 skipped in 19.86s`，exit 0 |
| `compileall -q app.py continucare pages tests` | exit 0，无输出 |
| `git diff --cached --check` | exit 0，无输出 |
| `git diff --check` | exit 0，无输出 |

合入后实际 collection 为 894 项，高于任务中根据两分支历史估算的约 885 项；以实际 collection 为准。零 failed、零 errors、无新增 skip。三个 skip 仍且仅为：

- `tests/test_fhir_conformance.py:114`；
- `tests/test_layer4_rules_tasks.py:620`；
- `tests/test_layer4_summaries.py:727`。

原因均为未配置官方 `FHIR_R4_SCHEMA_ZIP`。

## 6. Core Symptom v2 readiness 探针

合入后通过冻结只读 API 读取的实际结果：

```text
record_count=12
audited_alias_count=35
preferred_display_label_count=9
withheld_alias_count=26
approved_match_alias_count=0
terminology_review_status=pending_formal_terminology_review
formal_terminologist_review_completed=false
clinical_patient_expression_validation_completed=false
consumer_integration_ready=false
contains_patient_data=false
knowledge_effect=informational_only
runtime_authority=none
open Gap: gap-core-symptom-catalog-terminology-alias-review-pending
```

全部 12 条 record 的 `approved_match_aliases` 总数为 0，`consumer_integration_ready` 均为 false。

## 7. in-app Browser 集成 smoke

### 7.1 环境

- Browser：Codex in-app Browser，可用且成功调用；未回退至 Playwright CLI；
- URL：`http://127.0.0.1:8510`，Streamlit 仅绑定 `127.0.0.1`；
- 视口：`1280×720` 和 `390×844`；
- Browser 数据库：仓库外全新隔离 SQLite；
- 服务结束后已停止，8510 端口不再监听；Browser tab 已关闭。

### 7.2 六页冷加载

| 路由 | title | 桌面 | 移动 |
|---|---|---|---|
| `/` | `ContinuCare｜合成演示导览` | PASS | PASS |
| `/patient_followup` | `我的随访 · ContinuCare` | PASS | PASS |
| `/nurse_risk_center` | `护士工作台 · ContinuCare` | PASS | PASS |
| `/doctor_summary` | `复诊速览 · ContinuCare` | PASS | PASS |
| `/audit_log` | `记录追溯 · ContinuCare` | PASS | PASS |
| `/knowledge_evidence` | `Knowledge 资料库 · ContinuCare` | PASS | PASS |

12 次冷加载均有正确 title/H1 和可读 DOM，无空页、无 Streamlit exception/error surface、无框架错误 overlay，`scrollWidth-clientWidth=0`。

### 7.3 真实 `0/9 → story_complete`

在全新隔离数据库中通过真实 UI 完成：

1. 首页点击“开始一轮合成演示”；
2. 患者页点击“对，就是这个意思”；
3. 护士接手并开始核对；
4. 保持受控结果“记录一致”，记录结果并生成未发送沟通文字；
5. 医生按当前记录明确生成 pending 速览；
6. 护士确认沟通文字已人工核对；
7. 医生按当前来源生成新版本；
8. 首页刷新后仍显示五步完成和“演示记录链已走完”；
9. 记录追溯页显示“原因：合成演示 9/9 完成”和 12 条参与者动作。

流程后精确资源为：

| 资源 | 数量 |
|---|---:|
| QuestionnaireResponse | 1 |
| Observation | 1 |
| Task 历史版本 | 5 |
| Communication 历史版本 | 2 |
| Summary 历史版本 | 2 |
| AuditEvent | 12 |
| Alert | 0 |
| approved ClinicalRule | 0 |
| sent/received Communication | 0 |

`SEND_ENABLED=False`；Communication 两个历史版本均为 `preparation`，没有真实发送。

### 7.4 Knowledge 独立只读与 Browser 健康

- 终态后独立打开 Knowledge，从“腹泻”真实切换到“恶心”；
- 页面仍是既有腹泻/恶心/呕吐/腹痛四主题离线展示；
- 页面明确声明不读取患者故事、不创建记录、不参与完成判定、不授权运行时动作；
- 没有将 v2 aliases 表述成已批准、已等价、已安全可匹配或已正式接入；
- Knowledge 浏览前后隔离 DB 均为 SHA-256 `78c5f9cdd62cae18b7b97a5490584da209e411893d507509969369d662efe975`、size `311296`、mtime epoch `1786744349`，且无 journal/WAL/SHM；
- 返回首页并刷新后仍是 `story_complete`；
- 全程 console error/warn=`0`，无页面 exception/overlay；
- 导航和 DOM asset URLs 仅为 `http://127.0.0.1:8510`，未观察到非 localhost 资源或请求。

## 8. 工作区数据不变性

合入、全部自动验证、Browser smoke 和 merge commit 前后，工作区数据库均为：

```text
path: data/continucare.db
SHA-256: 0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585
size: 311296
mtime epoch: 1786644509
journal/WAL/SHM: absent
```

17 张表行数前后逐项一致：

```text
agent_runs=1
alert_actions=0
alerts=0
audit_events=12
care_sessions=1
confirmed_answer_contexts=0
confirmed_symptom_reports=1
conversation_action_resolutions=1
demo_metadata=1
fhir_observations=1
fhir_questionnaire_responses=1
followup_messages=1
layer4_contract_records=2
layer4_fhir_resources=14
observation_evidence=1
patients=1
summaries=0
```

受保护未跟踪 brief 前后均为：

```text
path: docs/ui_product_design_brief_2026-08-13.md
SHA-256: e9e03bde1051f43ec8dbca2695716a70588b448e47b37e434015934121be03a6
status: untracked; not modified; not staged
```

## 9. 当前能力边界与未实现项

合入代码不等于临床批准、患者匹配就绪或生产可用。当前仍固定：

- `knowledge_effect=informational_only`；
- `runtime_authority=none`；
- 不诊断、不治疗、不分诊、不做自动临床决策；
- 不接真实患者数据；
- 不执行真实来源采集或真实网络请求；
- P1b 仍为 `not_attempted`，`request_count=0`；
- rights/live/socket/alias Gap 继续 fail closed；
- 没有正式 reviewer、正式 rights decision、正式 clinical approval 或正式 KnowledgeRelease；
- v2 alias 仍是 readiness/gated 能力；
- `approved_match_aliases=()`，`consumer_integration_ready=false`；
- A++ Knowledge UI 尚未导入 v2 alias API；
- 本次没有实施 UI v2 consumer 适配、真实网络、真实患者、真实外部发送、真实来源采集、live health check 或生产操作。

任何 v2 alias successor manifest/DTO、正式 Gap resolution、consumer integration 或 UI 适配均需单独授权和独立审核。按任务安排，后续还会由队友进行一次独立验证。

## 10. 工具、Agent 与外部操作

- 使用 `build-web-apps:frontend-testing-debugging` 和 in-app Browser；
- 没有回退到 Playwright CLI；
- 没有调用 Claude、Sonnet、Opus、ImageGen 或任何子 Agent；
- 没有真实患者数据、模型调用、真实网络、外部系统写入、部署或生产操作。

文档收口 commit 无法在自身内容中自引其 SHA；该 SHA 与普通 push 后的 HEAD/upstream/ahead-behind 由本次最终执行报告精确记录。

## 11. 结论

Knowledge v2 alias readiness 已以正确的非快进 merge 进入 A++ UI 主线。合入范围精确，自动测试、静态检查、真实 Browser 流程、离线/网络边界、工作区数据不变性和受保护 brief 均通过。未解除任何临床、rights、review、release、live 或 alias consumer 门禁。
