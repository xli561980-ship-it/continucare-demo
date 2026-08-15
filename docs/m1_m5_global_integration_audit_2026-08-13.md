# ContinuCare M1–M5 全局整合审核

- 审核日期：2026-08-13
- 审核方式：只读、三路 Sol Ultra 并行检查后统一复核
- Git 分支：`codex/docs-collaboration-init`
- 审核基线：`dd9906215779c0b42004e5ef272321e698d6ef5c`
- 比赛题面：[2026 AI 先锋未来人才大赛.pdf](<../2026 AI 先锋未来人才大赛.pdf>)

## 1. 总体结论

M1–M5 的主比赛 happy path 已形成较强闭环，但尚不能作为可信的 UI 改造基线。

**结论：暂不建议开始大规模 UI/UX 实施；应先完成一个最小 UX-0R 合同修复切片。**

已确认成立的基线：

- M5-A/B/C 主链能够追溯患者原话、completed QuestionnaireResponse、final Observation、manual-review Task、Communication readiness、Summary、Provenance 和 AuditEvent。
- candidate 始终是待确认候选；未发现自动诊断、风险分级、治疗、改药、个体化建议或隐式临床阈值。
- `clinical_rules=[]`、`not_assessed`、Alert=0、approved ClinicalRule=0 在默认故事中成立。
- Knowledge 只解释采集依据，不授权运行时资源或动作。
- M5-E 默认 `mock / mock / disabled`，凭据偶然存在不足以开启飞书/Aily/Bitable；`SEND_ENABLED=False`，无发送按钮，`live_tenant_verified=false`、`production_ready=false` 表达真实。
- 比赛要求中的自动异常分级目前诚实地保持未实现，而不是伪造结果；UI 不得宣称该能力已完成。

但以下 5 项会造成数据完整性、错误状态或错误临床任务合同，因此阻断 UI 基线冻结。

## 2. BLOCKER

### B1. Pathway 未进入 Layer 4 输入和 Summary 身份边界

位置：

- [`continucare/adapters/sqlite_store.py:979`](../continucare/adapters/sqlite_store.py#L979)
- [`continucare/adapters/sqlite_store.py:1086`](../continucare/adapters/sqlite_store.py#L1086)
- [`continucare/layer4/inputs.py:50`](../continucare/layer4/inputs.py#L50)
- [`continucare/layer4/memory.py:219`](../continucare/layer4/memory.py#L219)
- [`continucare/layer4/summaries.py:119`](../continucare/layer4/summaries.py#L119)
- [`continucare/layer4/summary_agent.py:370`](../continucare/layer4/summary_agent.py#L370)
- [`continucare/care_agent/service.py:1865`](../continucare/care_agent/service.py#L1865)

证据：

- QR/Observation 查询只按 patient；`Layer4InputReader` 没有 Pathway 参数。
- `ClinicalMemoryService` 随后把全部患者资源标记为调用方指定 Pathway。
- 两套通用 Summary ID 都只使用 patient+时间窗。
- 隔离诊断复现了同一患者、同一时间窗、两个 Pathway 得到相同 Summary ID，第二条路径成为同一 version chain 的 v2/current。

影响：

- 多 Pathway 患者可能串入错误 Timeline、State、Summary 和 Layer 3 长期上下文。
- 一条路径的 Summary 会让另一条路径的审阅版本变 stale。

最小修复方向：

- 将 Pathway code/version 纳入 Layer 4 输入端口和 SQL join。
- Task/Communication 要求精确 `basedOn` Pathway。
- Layer 3 历史按 session Pathway 过滤。
- Summary ID 加入 Pathway code/version 和 summary kind。

建议验证：

同一患者建立两个 Pathway、相同时间窗和相同 Observation code，断言 QR→Memory→State→Summary→Agent memory 完全隔离，各自保持独立 current/reviewable 版本。

### B2. 医生审阅不是原子事务

位置：

- [`continucare/layer4/summaries.py:251`](../continucare/layer4/summaries.py#L251)
- [`continucare/layer4/summaries.py:341`](../continucare/layer4/summaries.py#L341)
- [`continucare/layer4/storage.py:159`](../continucare/layer4/storage.py#L159)
- [`continucare/layer4/storage.py:812`](../continucare/layer4/storage.py#L812)
- [`pages/3_doctor_summary.py:336`](../pages/3_doctor_summary.py#L336)

证据：

DoctorReview 依次以三个独立事务保存 Provenance、新 Summary 和 DoctorReview。故障注入复现：DoctorReview 保存失败后，Summary 已从 v1 变为 v2/`doctor_reviewed`，Provenance 增加，但 DoctorReview 行仍为 0。

影响：

- 医生页会显示“已审阅”，实际没有对应审阅记录。
- 并发 accept/reject 还可能留下孤立 Provenance 或冲突版本。

最小修复方向：

增加 `persist_doctor_review_bundle()`，在一个 `BEGIN IMMEDIATE` 中 CAS 校验精确当前 Summary，并原子写入 Provenance、新 Summary、DoctorReview 和医生审阅 AuditEvent。

建议验证：

- 每个插入点 fault injection。
- 并发 accept/modify/reject。
- 相同请求重试。
- 失败必须全无写入，竞争只能有一个完整胜者。

### B3. M6 clinical-rule Task 准入只相信 identifier

位置：

- [`continucare/layer4/manual_reviews.py:79`](../continucare/layer4/manual_reviews.py#L79)
- [`continucare/layer4/workbench.py:410`](../continucare/layer4/workbench.py#L410)
- [`continucare/layer4/fhir.py:295`](../continucare/layer4/fhir.py#L295)
- [`tests/test_layer4_workbench.py:177`](../tests/test_layer4_workbench.py#L177)

证据：

Workbench 只检查 `urn:continucare:clinical-rule` identifier 和 Pathway `basedOn`。现有测试直接构造没有对应 ClinicalRule 合同或规则执行 Provenance 的 Task，并期待其进入 Workbench。

影响：

伪造、遗留或直接写入的 Task 可被 UI 表述为获批规则任务，即使 approved ClinicalRule=0。

最小修复方向：

建立单一正向准入谓词：精确 rule ID/version、active、双审批、Pathway/version、规则执行 Provenance 和 Task target 全部一致才进入 M6。

建议验证：

无规则、draft、单审批、版本错误、Pathway 错误、缺 Provenance、仅伪 identifier 均须排除。

### B4. 普通 M2/M3 决策和完成审计不是原子命令

位置：

- [`continucare/care_agent/service.py:1303`](../continucare/care_agent/service.py#L1303)
- [`continucare/care_agent/service.py:1495`](../continucare/care_agent/service.py#L1495)
- [`continucare/care_agent/service.py:1701`](../continucare/care_agent/service.py#L1701)
- [`continucare/adapters/sqlite_store.py:203`](../continucare/adapters/sqlite_store.py#L203)
- [`continucare/care_engine/service.py:164`](../continucare/care_engine/service.py#L164)

证据：

- reject/unsure 先写 AuditEvent，再逐项关闭 action；confirm/clarification 也跨多个事务。
- 故障注入复现了 `semantic_candidate_patient_decision` 审计已存在、durable decision 仍为空。
- 普通 `CareEngine.complete()` 在 QR/Observation/session 事务提交后另写 completion AuditEvent。

影响：

可能出现“审计声称已决定但 action 未关闭”、部分候选关闭，或临床资源完成但缺少关键 completion audit。

最小修复方向：

仿照已经正确实现的 [`persist_confirmed_review_bundle()`](../continucare/adapters/sqlite_store.py#L573)，为普通 decision/completion 增加事务内 CAS command。

建议验证：

逐写点故障、不同决定并发、多候选中途失败和 audit 写失败；断言业务事实、resolution 和 AuditEvent 全有或全无。

### B5. 比赛进度遗漏合法终态

位置：

- [`continucare/services/competition_demo.py:38`](../continucare/services/competition_demo.py#L38)
- [`continucare/services/competition_demo.py:337`](../continucare/services/competition_demo.py#L337)
- [`continucare/services/competition_demo.py:363`](../continucare/services/competition_demo.py#L363)
- [`pages/1_patient_followup.py:494`](../pages/1_patient_followup.py#L494)

证据：

Stage 没有 candidate rejected、Task rejected/cancelled。实际拒绝全部候选后，诊断输出仍为：

```text
stage=candidate_ready
next_label=前往患者端明确确认
```

影响：

- 首页和六页共享导航会要求用户重复执行已经结束的操作。
- Task 拒绝/取消后还会倒退到患者确认阶段。

最小修复方向：

增加明确终态及 reason；终态只允许查看审计或明确重启。`unsure` 保留后续接受/拒绝路径。

建议验证：

覆盖全体 rejected、unsure→accept/reject、Task rejected、Task cancelled，并核对 stage、milestones、next page 和零额外资源。

## 3. HIGH PRIORITY

1. [`Layer4InputReader`](../continucare/layer4/inputs.py#L62) 没有拒绝不属于任何 admitted QR 的 orphan Observation；默认 SQLite join 暂时保护当前路径，但替换适配器会绕过。`ApprovedRuleEngine` 也仍接受调用方任意 final Observation。
2. Memory、State、Evidence Summary、Controlled Summary、Task transition 和规则 Task/Provenance 等通用 M4 多资源写入仍缺少统一事务/CAS；在修复前不要作为新 UI 写接口。
3. Memory 以重叠区间判冲突，而 State 仅以完全相同区间判冲突：[`memory.py:651`](../continucare/layer4/memory.py#L651)、[`states.py:451`](../continucare/layer4/states.py#L451)。同一事实可能在 Timeline 显示 conflict、State 却显示 current/trend。
4. QR/Observation 没有资源内 `meta.versionId`，证据展示以 `"1"` 回退拼出 `_history/1`；manual-review 幂等 replay 也按 ID 读取当前 Communication，而非动作发生时的精确版本：[`manual_review_workflow.py:710`](../continucare/services/manual_review_workflow.py#L710)、[`manual_review_workflow.py:811`](../continucare/services/manual_review_workflow.py#L811)。
5. 六页状态未完全共源：患者实际强制 Mock，但徽标按环境重建 adapter；护士/医生硬编码 ClinicalRule=0；护士 Alert 未按患者过滤；`story_complete` 不因异常 Alert/approved rule 降级。
6. 患者页加载会构造带默认初始化行为的 `Layer4SQLiteStore`。当前 schema 的字节/hash/mtime 诊断未发生变化，但这不是可证明的只读接口，且缺少真实页面执行测试。
7. 护士页仍保留可操作的旧 Alert 队列，并称其为“获批规则产生的任务”；正式 UI 应只消费正向准入的 FHIR Task。
8. 非空但格式非法的飞书/Aily/Bitable 配置会被状态层报为 `external_calls_allowed=true`，随后才在 client 构造时失败。仍会 fail-closed，但 UI 状态不诚实。
9. `reject_candidates()` 对空、未知或混合 ID 可静默过滤并产生空决定审计，应要求非空、唯一且全集有效。

## 4. NON-BLOCKING

- [`HANDOFF.md:8`](../HANDOFF.md#L8) 仍记录 M5-E 提交前的 `389f536` 和“不 push”，实际已在 `dd99062` upstream；实现边界描述基本正确，仅 Git 状态漂移。
- [`continucare/adapters/feishu/README.md:3`](../continucare/adapters/feishu/README.md#L3) 仍称适配器是未来占位，并使用旧配置名。
- 根 README 页面清单遗漏 Knowledge 页面，但正文已有入口说明。
- 六页暴露较多 M5-C/M6、canonical、digest、FHIR history 等内部术语；应移入技术展开区。
- 首页仍保留会替换主故事的旧技术 fixture 入口。可保留，但应与比赛/普通角色主入口进一步隔离。
- terminology catalog 没锁进 CareSession，不过 AgentRun、match 和 Observation evidence 已保存精确 catalog 版本，当前追溯仍成立。
- 医生修改 Summary 文字只锁定原 evidence refs，不做自动医学语义审查；这是已记录的人工责任边界，UI 不得表述为自动 safety-reviewed。

## 5. M1–M5 接口地图

| 层 | UI 可依赖接口/状态 | 禁止绕过或暂缓冻结 |
|---|---|---|
| M1 | `PathwayRegistry/load_builtin_pathways`；Pathway、Questionnaire canonical/version、mapping、`clinical_rules=[]` | 页面自行定义 code、规则、阈值或风险 |
| M2 | `questionnaire_for_session`、`save_draft`、`stop`；状态 `in_progress/completed/stopped/entered_in_error` | `complete` 在 B4 修复前不作为完整审计原子接口 |
| M3 | `CareAgentService.analyze`；candidate/clarification 只作展示和人工决定 | 页面直接构造 FHIR；reject/unsure 在 B4/B5 前暂缓冻结 |
| M5-A | `ConfirmedReviewService.accept_all`：完整候选集原子发布 | 绕过它分别保存 QR、Observation、Task、Provenance |
| M5-B | `ManualReviewQueue`、`ManualReviewWorkflowService`；`requested→received→accepted→in-progress→completed`，以及 rejected/cancelled 分支 | 把 manual Task 当 clinical-rule Task；任何发送调用 |
| Communication | `status=preparation`；`pending-approval→ready-to-send` | 把 ready 表述为 sent；直接调用 Bot/Bitable |
| M5-C | `ManualReviewBriefService.generate/is_stale`；`DoctorWorkbenchService.query/trace_evidence` 的只读证据部分 | `DoctorReviewService.review` 在 B2 前；原始 store join |
| M5-D | `read_competition_demo` 的事实计数；`demo_write_guard` generation CAS | B5 修复前不要冻结 `stage/next_page/next_label` |
| M5-K | `load_builtin_bundle()` 离线只读 | Knowledge 参与患者事实、完成判定或规则授权 |
| M5-E | `read_adapter_statuses()` 纯配置投影；默认 Mock/disabled | 页面构造外部 client；Bitable 作为读取或真相源 |

SQLite/FHIR 是唯一权威源；Bitable 只能是未来 write-only projection。Layer 4 不得读取 AgentRun、候选、聊天或模型原始输出。

## 6. UI 开工建议

先申请并完成 **UX-0R 合同修复**，不做视觉重构：

1. Pathway-scoped admission、Summary identity 和 M6 Task admission。
2. DoctorReview 及普通 M2/M3 command 的事务/CAS。
3. rejected/cancelled 终态及共享安全完整性投影。
4. 针对上述合同补 fault、并发、多 Pathway、终态和六页只读测试。

之后建议顺序：

| UI 切片 | 不可破坏的回归条件 |
|---|---|
| UX-0 共享 read model、状态词表、能力徽标 | 六页冷加载零 DB 变化；实际 adapter 与文案一致；无 client/token/network |
| 首页与导航 | 只消费共享进度；完整性异常不得显示成功；终态不推荐非法动作 |
| 患者页 | candidate 非临床；完整确认才发布；reject/unsure 零临床资源 |
| 护士页 | manual Task 与 clinical-rule Task 分离；CAS 状态机；Communication 永不发送 |
| 医生页 | 显式生成/刷新；陈旧版本 fail-closed；逐项证据；医生审阅原子 |
| 审计页 | 临床事实与流程 AuditEvent 分开；每个动作绑定精确版本 |
| Knowledge | 不读患者 DB、不参与完成判定、不授权 runtime |

## 7. 验证结果

- 全量测试：`338 passed, 3 skipped in 10.02s`。
- 3 个 skip 均因未提供官方 `FHIR_R4_SCHEMA_ZIP`：
  - [`tests/test_fhir_conformance.py:114`](../tests/test_fhir_conformance.py#L114)
  - [`tests/test_layer4_rules_tasks.py:575`](../tests/test_layer4_rules_tasks.py#L575)
  - [`tests/test_layer4_summaries.py:428`](../tests/test_layer4_summaries.py#L428)
- `compileall -q continucare app.py pages`：通过，缓存定向至系统临时目录。
- `git diff --check`：通过。
- 测试使用显式离线环境：LLM 未配置、飞书/Aily Mock、Bitable disabled、所有 egress/capability flag 为 false。
- 未发现本地 `.env`；未运行 live 脚本、真实模型、飞书、Aily、Bitable 或其他外部 API。
- 隔离诊断复现了：跨 Pathway Summary 碰撞、DoctorReview 部分写入、orphan Observation 准入、患者决定部分写入、错误终态导航。
- 未重复已有 Claude 审核；本次按要求使用三路 Sol Ultra 并行检查，由主审统一复核和定级。

## 8. Git 状态

审核结束时：

- 分支：`codex/docs-collaboration-init`
- HEAD：`dd9906215779c0b42004e5ef272321e698d6ef5c`
- upstream：`dd9906215779c0b42004e5ef272321e698d6ef5c`
- ahead/behind：`0 / 0`
- `git status --short --untracked-files=all`：空
- 审核未修改任何项目文件；未 add、commit、push，未创建分支或 PR。

## 9. 建议的最小后续授权

如继续，建议只授权 **UX-0R 合同修复切片**，不要同时启动视觉重构，也不要接入任何真实外部 API。
