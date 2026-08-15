# M1–M5 Baseline Repair 冻结实施方案

> 状态声明（2026-08-13）
>
> - 本文是冻结实施方案，不是已完成实现。
> - B1–B5 尚未修复。
> - BR-1、BR-2、BR-3 均未开始。
> - UI 仍未解锁，不应开始 UX-0/UI 优化。
> - 方案 Git 基线：`dd9906215779c0b42004e5ef272321e698d6ef5c`。
> - 本方案以 [M1–M5 全局整合审核](m1_m5_global_integration_audit_2026-08-13.md) 为只读依据。

结论先行：B1–B5 全部继续作为 UI 开工前 blocker。冻结顺序为 **BR-1 → BR-2 → BR-3**；三片全部通过后才解锁 UX-0/UI 优化。方案制定阶段未开始实现，未修改或暂存任何源代码。

## 1. Claude Opus 实际调用与结论

已真实调用：

- Claude Code CLI `2.1.228`；
- 模型：`opus`；
- effort：`max`；
- 工作目录：`/tmp`；
- 参数：`--safe-mode --tools "" --no-session-persistence`；
- 退出码：`0`；
- 未赋予仓库读取、文件写入或测试工具。

调用使用的 Review Packet 仅包含审核报告确认的 B1–B5、最小代码证据、冻结安全边界、拟议拆分和待审问题。Opus 被明确限制为只审架构、事务、数据完整性与医疗任务准入，不扫描仓库、不运行测试、不修改文件。

Opus 返回了完整策略内容，但形式是一个被禁用工具捕获的 `Write` 提案，而非普通文本回答。提案目标文件 `/Users/zhangziyue/.claude/plans/task-ui-shiny-pie.md` 经检查不存在，因此没有产生任何实际写入。

Opus 对 B1–B5 的判断：

| 项目 | Opus | Sol 最终判定 |
|---|---|---|
| B1 Pathway 隔离 | ADJUST | CONFIRM，采纳部分调整 |
| B2 DoctorReview 原子性 | ADJUST | CONFIRM，调整幂等和审计方案 |
| B3 clinical-rule Task 准入 | ADJUST | CONFIRM，并强化版本链验证 |
| B4 M2/M3 原子性 | CONFIRM | CONFIRM |
| B5 合法终态投影 | ADJUST | CONFIRM，`unsure` 明确为非终态 |

Opus 没有提出否决三片方案的新 blocker，但指出了 Summary 新旧链、混合候选状态、SQLite 事务共库和旧合成数据处理等风险。其输出没有严格使用要求的 `BLOCKER / NON-BLOCKING` 一级标题，而是将内容嵌入“结论摘要、判定、风险、NEED_CONTEXT”中；为遵守“一次 Opus 审查”，未再次调用。

## 2. Sol 对 Opus 意见的独立裁决

| Opus 意见 | 处理 | 理由 |
|---|---|---|
| B1–B5 均阻断 UI | 采纳 | 与审核报告和代码复现一致 |
| Reader 应以单一 CareSession 为边界 | 调整 | Memory、State、Summary 是 Pathway 内跨多次随访的纵向视图；冻结为 `patient + pathway code/version`，由 SQL session join 证明归属 |
| orphan Observation 整体 fail-closed | 采纳 | 不允许静默缩减证据；任一 Observation 无法唯一归属 admitted QR，则整个 snapshot 失败 |
| legacy Summary 只读、禁止重写 | 采纳 | 新 ID 链与旧链隔离；旧行保持逐字节不变 |
| Summary period 不应由过滤后的证据跨度决定 | 采纳 | 当前两个通用 Summary API 已显式接收 period，继续把它作为稳定身份输入 |
| clinical-rule identifier 与 rule URN 必须交叉一致 | 采纳 | 当前仅检查 identifier system，确有伪造准入风险 |
| 所有 current Task 都直接要求 RuleEngine Provenance target | 调整 | 状态转换后的 Task v2+ 由 Task transition Provenance 证明；应验证 v1 创建链和随后每一版连续转换链 |
| Task evidence 必须等于任意规则执行 Provenance evidence | 调整 | RuleEngine 的去重评估可能再次引用既有 Task；冻结为“创建 Provenance 的 evidence 与 Task v1 精确一致”，后续评估不改写创建证据 |
| DoctorReview 使用显式 UI action ID | 调整 | 当前合同保证一个 source Summary exact version 只能成功审阅一次；采用稳定内容键可跨刷新重试且无需页面状态合同 |
| DoctorReview 不新增 AuditEvent | 拒绝 | 原审核明确要求该事件；现有审计页也已有 `doctor_reviewed_summary` 词表，应与 Provenance 一起原子写入 |
| 自动删除并重建受污染合成数据 | 调整 | 禁止隐式删除或重标；实施验收时由用户明确同意后执行已有合成 Demo restart |
| BR-2 再细分 | 采纳为内部顺序 | 保持三片结构，BR-2 内按 DoctorReview、decision、completion 三个 command 独立实现和验证 |
| 混合候选必须先定义聚合规则 | 采纳 | 防止部分 rejected/unsure 被误判终态 |
| SQLite BUSY 不做内部重试 | 采纳 | 映射为稳定并发冲突，由调用方以同一请求重试 |

Opus 的主要 NEED_CONTEXT 已由 Sol 使用仓库事实独立解决：

- Provenance、Summary、DoctorReview、AuditEvent 均在同一 SQLite 数据库，可被一个连接覆盖；
- 当前无通用 idempotency ledger；本方案使用 DoctorReview、resolution、completed session 和确定性 AuditEvent ID 作为自然幂等记录，不新增表；
- 通用 Summary period 是调用方显式输入；
- 比赛投影锚定固定 AgentRun/CareSession；BR-3 继续通过该 run 的 Pathway 过滤 Task/Communication；
- `summary_kind` 与 `record_type` 不是一一对应，因此新 Summary ID 必须显式包含 kind。

## 3. BR-1、BR-2、BR-3 的目标和非目标

### 3.1 BR-1：Pathway 隔离、Summary identity、临床任务正向准入

目标：

- Layer 4 输入统一以 `patient_id + pathway_code + pathway_version` 为边界；
- QR、Observation、Pathway Audit、Memory、Timeline、State、Task、Communication、Workbench trace 和 Layer 3 长期历史完全隔离；
- Observation 必须唯一 `derivedFrom` 当前 Pathway admitted QR；orphan 或歧义整体失败；
- 通用 Summary 使用 v2 identity：`patient + pathway code/version + summary_kind + period_start/end`；
- legacy timeline Summary 只读；M5-C manual-review brief ID 保持不变；
- Workbench 只准入有完整 ClinicalRule、审批、Pathway、创建 Provenance 和连续版本链的 Task；
- ApprovedRuleEngine 只能消费 Pathway-scoped admitted Observation。

非目标：

- 不修复所有通用 M4 多资源写入原子性；
- 不选择 EvidenceSummary 与 ControlledSummary 的未来唯一 UI writer；
- 不新增 ClinicalRule、阈值、风险等级或临床结论；
- 不修改 schema，不迁移、重标或删除旧行；
- 不进行页面或视觉修改。

### 3.2 BR-2：DoctorReview、M2/M3 decision 和 completion 原子事务

目标：

- 增加三个独立的原子 command：
  - `persist_doctor_review_bundle`；
  - `persist_conversation_decision_bundle`；
  - 原子化后的 `complete_care_session_submission`；
- 每个 command 使用单连接、`BEGIN IMMEDIATE`、事务内重读和 CAS；
- DoctorReview 原子写入 Provenance、新 Summary、DoctorReview、`doctor_reviewed_summary` AuditEvent；
- confirm/reject/unsure/verbatim-only/clarification 原子写入适用的 draft、context、report、resolution 和审计；
- completion 将现有 `questionnaire_response_completed` AuditEvent 纳入 QR/Observation/session 同一事务；
- `reject_candidates()` 要求非空、唯一且所有传入 ID 均属于精确 AgentRun；
- exact replay 零新增写入；冲突请求全回滚。

非目标：

- 不原子化普通手工 `save_draft()`；
- 不修复通用 Summary generation、TaskWorkflow、RuleEngine Task/Provenance 等其他 M4 writer；
- 不新增幂等台账表或数据库迁移；
- 不改变 M5-A 已正确的 `persist_confirmed_review_bundle()`；
- 不修改页面视觉或按钮名称。

### 3.3 BR-3：终态与共享比赛进度投影

目标：

- 新增明确状态：
  - `candidate_unsure`：非终态，可继续；
  - `candidate_rejected`；
  - `task_rejected`；
  - `task_cancelled`；
  - 同步 fail-closed 处理 `task_failed`、`task_entered_in_error`；
- 增加 `is_terminal` 和 `terminal_reason`；
- 全部候选 rejected 才进入 candidate terminal；
- 任一未决或 unsure 存在时保持可继续；
- 终态不再显示业务“推荐下一步”；只允许查看审计或从首页明确 restart；
- 首页与角色页继续消费同一只读事实投影。

非目标：

- 不建立第二套 UI 状态机；
- 不自动 restart、reset 或创建资源；
- 不改配色、布局或视觉组件；
- 不改变 Knowledge 的独立边界。

## 4. 每片精确预计修改文件

### 4.1 BR-1

生产代码：

- [`continucare/layer4/inputs.py`](../continucare/layer4/inputs.py)
- [`continucare/adapters/sqlite_store.py`](../continucare/adapters/sqlite_store.py)
- [`continucare/layer4/memory.py`](../continucare/layer4/memory.py)
- [`continucare/layer4/states.py`](../continucare/layer4/states.py)
- [`continucare/layer4/summaries.py`](../continucare/layer4/summaries.py)
- [`continucare/layer4/summary_agent.py`](../continucare/layer4/summary_agent.py)
- [`continucare/layer4/rules.py`](../continucare/layer4/rules.py)
- [`continucare/layer4/workbench.py`](../continucare/layer4/workbench.py)
- [`continucare/care_agent/service.py`](../continucare/care_agent/service.py)

测试：

- [`tests/test_layer3_release_boundary.py`](../tests/test_layer3_release_boundary.py)
- [`tests/test_layer3_agents.py`](../tests/test_layer3_agents.py)
- [`tests/test_layer4_memory.py`](../tests/test_layer4_memory.py)
- [`tests/test_layer4_states.py`](../tests/test_layer4_states.py)
- [`tests/test_layer4_summaries.py`](../tests/test_layer4_summaries.py)
- [`tests/test_layer4_summary_agent.py`](../tests/test_layer4_summary_agent.py)
- [`tests/test_layer4_rules_tasks.py`](../tests/test_layer4_rules_tasks.py)
- [`tests/test_layer4_workbench.py`](../tests/test_layer4_workbench.py)
- [`tests/test_manual_review_briefs.py`](../tests/test_manual_review_briefs.py)
- 新增 `tests/test_m1_m5_pathway_isolation.py`。

### 4.2 BR-2

生产代码：

- 新增 `continucare/errors.py`；
- [`continucare/services/audit.py`](../continucare/services/audit.py)
- [`continucare/layer4/contracts.py`](../continucare/layer4/contracts.py)
- [`continucare/layer4/repository.py`](../continucare/layer4/repository.py)
- [`continucare/layer4/storage.py`](../continucare/layer4/storage.py)
- [`continucare/layer4/summaries.py`](../continucare/layer4/summaries.py)
- [`continucare/adapters/sqlite_store.py`](../continucare/adapters/sqlite_store.py)
- [`continucare/care_agent/service.py`](../continucare/care_agent/service.py)
- [`continucare/care_engine/service.py`](../continucare/care_engine/service.py)

测试：

- `tests/test_layer4_summaries.py`；
- [`tests/test_layer4_fhir_storage.py`](../tests/test_layer4_fhir_storage.py)
- `tests/test_layer3_agents.py`；
- [`tests/test_conversation_temporal_context.py`](../tests/test_conversation_temporal_context.py)
- [`tests/test_terminology_conversation_flow.py`](../tests/test_terminology_conversation_flow.py)
- [`tests/test_care_engine.py`](../tests/test_care_engine.py)
- [`tests/test_end_to_end.py`](../tests/test_end_to_end.py)

### 4.3 BR-3

- [`continucare/services/competition_demo.py`](../continucare/services/competition_demo.py)
- [`continucare/ui.py`](../continucare/ui.py)
- [`app.py`](../app.py)
- [`tests/test_competition_demo.py`](../tests/test_competition_demo.py)

## 5. 接口、状态和数据流变化

冻结的 Layer 4 输入接口：

```python
Layer4InputReader.read(
    patient_id,
    *,
    pathway_code,
    pathway_version,
    assembled_at=None,
) -> Layer4InputSnapshot
```

`Layer4InputSnapshot` 增加必填 `pathway_code/pathway_version`。SQLite 的 completed QR、final Observation、Pathway Audit 查询同步要求精确 Pathway。

冻结的 clinical-rule Task 准入接口：

```python
admit_clinical_rule_task(
    task,
    *,
    patient_id,
    pathway_code,
    pathway_version,
    cutoff,
    repository,
    admitted_observation_refs,
) -> dict
```

正向准入必须验证：

- 唯一、可解析的 clinical-rule identifier；
- identifier 与唯一 rule `basedOn` URN 完全一致；
- 唯一精确 Pathway code/version；
- ClinicalRule exact version 存在且为 current、active、双审批；
- 审批时间不晚于 Task v1 创建；
- RuleEngine `EXECUTE` Provenance target 精确 Task v1 history；
- 创建 Provenance 的 rule/evidence entity 与 Task v1 精确一致；
- Task v2+ 每一版均有连续 transition Provenance；
- Task evidence 属于当前 Pathway admitted final Observation；
- `entered-in-error` 不准入；其他合法终态可追溯但不可操作；
- Knowledge reference 永远不能替代规则或 Observation evidence。

冻结的原子接口：

```python
Layer4Repository.persist_doctor_review_bundle(...) -> bool
SQLiteStore.persist_conversation_decision_bundle(...) -> bool
SQLiteStore.complete_care_session_submission(..., audit_event=...) -> bool
```

对外 service API 尽量保持不变。`DoctorReviewOutcome` 只增加向后兼容的 `idempotent_replay=False`。UI 禁止绕过这些 command 直接调用通用 save 方法。

数据流冻结为：

```text
CareSession(pathway code/version)
  → completed QuestionnaireResponse
  → final Observation derivedFrom exact QR
  → Pathway-scoped Layer4InputSnapshot
  → Memory / State / timeline_evidence Summary / Workbench

ClinicalRule exact version + dual approval
  + Pathway-scoped admitted Observation
  + RuleEngine EXECUTE Provenance
  → admitted clinical-rule Task

Service pure bundle
  → single repository/store transaction
  → business facts + exact version Provenance + AuditEvent
```

## 6. 事务、CAS、幂等、并发和零副作用边界

统一事务合同：

1. 所有 bundle 在同一 SQLite connection 中执行；
2. 入口显式 `BEGIN IMMEDIATE`；
3. 事务外验证只用于早失败，所有可变前置条件必须在事务内重读；
4. CAS 至少绑定：
   - Summary exact ID/version/current JSON；
   - CareSession status、updated_at、answers；
   - AgentRun 与 session/patient 归属；
   - action 当前 resolution；
   - completion 当前状态；
5. CAS 失败抛 `ConcurrentWriteConflict(ValueError)`，禁止 last-write-wins；
6. `SQLITE_BUSY` 不做应用层隐式重试，映射为同一冲突类型；
7. 所有审计均在业务写入提交前插入，禁止提交后补写；
8. 只读页面继续使用 `mode=ro + query_only`，不获取写锁。

幂等合同：

- DoctorReview key：`source summary exact version + reviewer + decision + canonical note + modified-items digest`；
- `reviewed_at` 是第一次成功提交的结果时间，不参与 key；exact replay 返回首次保存的 review/summary/time，零新增行；
- 同源版本不同内容或不同决策：首个完整事务胜出，其他请求冲突；
- M2/M3 key：`source run + sorted action IDs + decision + option`；resolution 和确定性 AuditEvent ID 充当自然幂等记录；
- completion：completed CareSession、QR 和同事务 completion AuditEvent 共同证明成功；相同 answers 重试返回既存结果，不同 answers 冲突；
- 不新增 idempotency 数据表。

状态转换合同：

```text
none   → accepted | rejected | unsure
unsure → accepted | rejected
accepted/rejected → 无后续转换
```

exact replay 不产生第二条 `care_session_draft_saved`、`semantic_candidate_patient_decision`、`questionnaire_response_completed` 或 `doctor_reviewed_summary`。

并发结果只允许：

- `OK`：完整事务成功；
- `IDEMPOTENT_REPLAY`：精确相同请求返回既存结果，零新增写；
- `CONFLICT`：前置状态、版本、payload 或锁竞争不一致，全回滚；
- `PRECONDITION/INTEGRITY_FAILURE`：非法证据或状态，全回滚。

不存在部分成功、静默 no-op 成功或 last-write-wins。

## 7. 旧数据与 Summary v2 ID 兼容策略

新通用 ID：

```text
summary-v2-<hash(
  patient_id,
  pathway_code,
  pathway_version,
  summary_kind,
  period_start,
  period_end
)>
```

兼容规则：

- 旧 `summary-...` timeline chain 保持逐字节不变；
- 新路径首次生成从 v1 开始，不复制、不提升旧链；
- Workbench 的 operational current 只选择：
  - `summary-v2-*` 的 `timeline_evidence`；
  - 原有 `summary-manual-review-*` 的 `manual_review_brief`；
- legacy timeline Summary 仍可在 trace/audit/history 中读取，但不能成为新 DoctorReview source；
- M5-C manual-review brief ID 不改；
- EvidenceSummary 与 ControlledSummary 暂时继续共享同一 `timeline_evidence` 链；未来 UI 唯一 writer 选择列为 HIGH PRIORITY；
- 不新增 schema migration，不重写 `is_current`，不删除旧链。

旧的 Pathway 误标 Memory：

- 不自动 relabel，因为无法证明原始归属；
- 不在代码启动时删除；
- BR-1 验收要求用户明确使用现有 synthetic Demo restart 重建运行数据；
- 若检测到任何非 synthetic 数据，立即停止，不允许 reset。

回退安全依赖于以下不变量：

- BR-1 对旧 Summary 行零写；
- 新旧 identity 永不共享 current chain；
- period 是调用方明确的临床窗口，不从过滤后的实际证据跨度重新推导；
- Pathway 参数无 Optional、无默认全量读取回退。

## 8. 精确测试计划

审核报告基线为 `338 passed, 3 skipped`；本规划阶段没有重复运行测试。实施后要求至少保留全部 338 项并通过新增测试。3 个 skip 只允许继续由缺少官方 `FHIR_R4_SCHEMA_ZIP` 导致。

### 8.1 BR-1 必测

- 同患者双 Pathway、同时间窗、同 Observation code，QR→Memory→State→两种 Summary→Workbench→Layer 3 long-term memory 零交叉；
- 同 Pathway code、不同 version 隔离；
- 任一 orphan、跨 Pathway或多 QR `derivedFrom` Observation：整个 snapshot 失败、零写；
- Task 精确 Pathway；Communication 通过 exact Task history 传递 Pathway；
- Pathway Audit 不能被另一条路径 Memory 接纳；
- legacy Summary 表全行摘要前后相同；
- 新 v2 ID 稳定、双 Pathway current chain 独立；
- M5-C brief ID 回归不变；
- 默认 `clinical_rules=[]`：Workbench clinical-rule Task 数量为 0；
- 隔离测试库中的合法规则链正向准入；
- 负向矩阵：伪 identifier、无 rule、非 active、单审批、版本/path 错、缺 Provenance、错误 agent/activity/target/entity/evidence、断裂 Task 版本链、Knowledge 冒充证据；
- ApprovedRuleEngine 拒绝调用方任意 final Observation；
- 所有读操作 DB hash/mtime/row count 不变。

### 8.2 BR-2 必测

- DoctorReview、五类 decision、completion 每个写点 fault injection；
- 首写前、写中、commit 前异常：所有表全无变化；
- 模拟 commit 成功但响应丢失：同 key 重试返回 replay；
- 同 key 同 payload：一写一 replay；
- 同 key 不同 payload：冲突且零写；
- 异 key 并发同一 source：一完整胜者，一冲突；
- `SQLITE_BUSY`：稳定冲突，无应用层重试；
- 多候选中途失败：零 resolution、零 draft/context/report、零 audit；
- reject 空、重复、未知、跨 run、混合 ID：严格拒绝；
- unsure exact replay、unsure→accepted/rejected、终态重试；
- 已完成 session 相同 answers replay；不同 answers 冲突；
- completion 成功必须同时存在 QR、Observation、completed session 和 `questionnaire_response_completed`；
- DoctorReview 成功必须同时存在 Provenance、result Summary、Review 和 `doctor_reviewed_summary`。

### 8.3 BR-3 必测

- 全部 rejected → `candidate_rejected`，无业务下一步；
- 部分 rejected + 未决 → 继续 patient decision；
- rejected + unsure 且无未决 → `candidate_unsure`，仍可继续；
- unsure→accepted/rejected；
- Task rejected/cancelled/failed/entered-in-error 不回退 candidate ready；
- 终态仅链接审计/明确 restart；
- 五个共享进度入口表现一致；Knowledge 继续独立；
- 投影反复读取不改变 DB，也不产生 sidecar。

### 8.4 每片及最终验证命令

```text
.venv/bin/python -m pytest -q <该片目标测试>
.venv/bin/python -m pytest -q
PYTHONPYCACHEPREFIX=<系统临时目录> .venv/bin/python -m compileall -q continucare app.py pages
git diff --check
git status --short --untracked-files=all
```

不得运行 MiMo live、真实飞书/Aily/Bitable 或任何外部 API。

## 9. Demo 验收步骤

1. 确认 Git 和受保护报告状态；
2. 用户明确同意后，使用现有比赛 Demo restart 重建纯合成运行数据；
3. 起点验证：candidate=1，QR/Observation/Task/Communication/Summary/Alert/approved rule 全为 0；
4. 执行“拒绝全部”：
   - stage 为 `candidate_rejected`；
   - QR/Observation/Task/Alert 仍为 0；
   - 仅存在 decision audit；
   - 导航只指向审计或明确 restart；
5. restart 后选择 unsure：
   - stage 为 `candidate_unsure`；
   - 无临床资源；
   - 后续可一次 accept 或 reject；
6. 完整 happy path：
   - patient confirmation；
   - completed QR、final Observation；
   - manual-review Task，非 clinical-rule Task；
   - `not_assessed`、Alert=0、approved rule=0；
   - Communication `pending-approval → ready-to-send`；
   - 始终无发送；
7. 分别复现 Task rejected 和 cancelled，确认终态不倒退；
8. 医生 review 并发/故障演示只用合成测试库，确认全有或全无；
9. 刷新首页及角色页，确认零写、零网络；
10. 确认 `SEND_ENABLED=False`，Knowledge 不参与任务或完成判定。

## 10. 回退方式

- 每片独立提交，只使用 `git revert <slice-commit>` 回退；不使用 reset、clean、checkout 或删除；
- 推荐回退顺序：BR-3 → BR-2 → BR-1；
- 无 schema 变化，因此不需要逆向 migration；
- BR-1 生成的 v2 Summary 行可保留；旧链未变。代码回退后不得继续 UI 开工，因为会重新暴露 B1；
- BR-2 原子生成的记录与旧 reader 兼容，不需要拆除；
- BR-3 只读投影不写数据库；
- 对任何发现的旧部分写入或误标数据，不做自动补写、删除或重标；纯合成 Demo 仅在用户明确授权后 restart。

## 11. 可延期 HIGH PRIORITY

三片完成后仍建议在大规模 UI 修改前排期，但不阻断 UX-0：

- Memory、State、通用 Summary、Task transition、RuleEngine Task/Provenance 的通用事务/CAS；
- EvidenceSummary 与 ControlledSummary 的唯一 canonical UI writer；
- Memory 重叠窗口冲突与 State 完全相同窗口冲突语义统一；
- QR/Observation 的真实 `meta.versionId`，替代 `_history/1` 回退；
- manual-review replay 对精确 Communication 历史版本的锁定；
- 护士页旧 Alert 队列与 M6 正向准入 Task 的彻底分离；
- 六页 adapter/ClinicalRule/Alert 状态共源；
- 页面加载构造初始化型 store 的只读证明；
- 非空但非法外部适配器配置的状态文案；
- HANDOFF、README、Feishu README 漂移及内部术语收敛；
- Controlled Summary fallback 的模型执行元数据审计粒度。

## 12. BLOCKER 和必须由用户决定的问题

当前冻结方案没有尚未解决的架构选择 blocker。

B1–B5 本身仍是未修复的实施 blocker：

- B1：Pathway 未进入 Layer 4 admission、历史过滤和 Summary identity；
- B2：DoctorReview 的 Provenance、Summary、Review 和审计非原子；
- B3：clinical-rule Task 只凭 identifier/basedOn 准入；
- B4：M2/M3 decision 与 completion audit 非原子；
- B5：rejected/cancelled 合法终态未进入比赛进度投影。

仍需用户决定：

1. 是否授权按精确 allowlist 开始 BR-1；
2. BR-1 完成后，是否明确同意 restart 当前纯合成 Demo 数据，以清除无法安全重标的旧 Pathway Memory；
3. 若届时检测到非 synthetic 数据或 allowlist 外工作区变化，必须停止并重新授权。

在 BR-1、BR-2、BR-3 全部验收前，仍不建议开始 UX-0/UI。

## 13. 建议 commit message

- BR-1：`fix(layer4): enforce pathway-scoped evidence admission`
- BR-2：`fix(persistence): make review and decision bundles atomic`
- BR-3：`fix(demo): project legal terminal workflow states`

每次只能显式 stage 该片 allowlist；禁止 `git add .`。受保护审核报告和本冻结方案文档不得混入后续修复暂存区，除非用户另行明确授权文档提交。

## 14. BR-1 实施精确 allowlist

BR-1 唯一允许修改或新增的文件：

```text
continucare/layer4/inputs.py
continucare/adapters/sqlite_store.py
continucare/layer4/memory.py
continucare/layer4/states.py
continucare/layer4/summaries.py
continucare/layer4/summary_agent.py
continucare/layer4/rules.py
continucare/layer4/workbench.py
continucare/care_agent/service.py

tests/test_layer3_release_boundary.py
tests/test_layer3_agents.py
tests/test_layer4_memory.py
tests/test_layer4_states.py
tests/test_layer4_summaries.py
tests/test_layer4_summary_agent.py
tests/test_layer4_rules_tasks.py
tests/test_layer4_workbench.py
tests/test_manual_review_briefs.py
tests/test_m1_m5_pathway_isolation.py
```

明确禁止 BR-1 修改：

```text
docs/m1_m5_global_integration_audit_2026-08-13.md
docs/m1_m5_baseline_repair_plan_2026-08-13.md
continucare/db.py
continucare/layer4/contracts.py
continucare/layer4/repository.py
continucare/layer4/storage.py
app.py
pages/*
任何外部适配器、配置、Pathway/Questionnaire/ClinicalRule 数据
```

若 BR-1 实施中证明必须修改 allowlist 之外的文件，应停止，不得自行扩大范围，并向用户报告新证据和最小增补请求。

## 15. 冻结方案的 Git 与执行边界

- 方案基线分支：`codex/docs-collaboration-init`；
- 方案基线 HEAD/upstream：`dd9906215779c0b42004e5ef272321e698d6ef5c`；
- 冻结方案制定时 ahead/behind：`0 / 0`；
- 冻结方案制定和保存均不代表 BR-1 已获实施授权；
- 不 add、commit、push，不创建分支或 PR；
- 不运行真实患者数据、外部 API、MiMo live、飞书、Aily 或 Bitable；
- 不启动 UI 重构；
- 下一步必须等待用户对 BR-1 的明确授权。
