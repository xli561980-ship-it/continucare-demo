# M5-C 确定性医生复诊前简报

## 1. 目标

M5-C 把 M5-A/B 已经人工确认并完成处理的合成证据组织成医生复诊前简报：

```text
患者逐字原话
→ completed QuestionnaireResponse
→ final Observation / derivedFrom
→ completed manual-review Task 的受控结果
→ Communication preparation 的人工批准准备度
→ 不可变、可版本化、可审阅的 Summary
```

正文由本地固定模板生成，不调用 LLM，不产生诊断、风险等级、阈值、治疗或用药建议。运行时临床评估保持 `not_assessed`。

## 2. 来源与准入

`ManualReviewBriefService` 只正向接收 `urn:continucare:patient-confirmed-review` Task，并且要求：

- Task 为 `completed`，且包含受控 outcome 和 evidence digest；
- 唯一 completed QuestionnaireResponse 含一条逐字自由文本答案；
- Observation 均为 `final`，并通过 `derivedFrom` 指向该 QuestionnaireResponse；
- Communication 为 `preparation`，准备度只能是 `pending-approval` 或 `ready-to-send`，没有 `sent` / `received`；
- M5-A/B 所需的精确 Provenance 与 AuditEvent 均存在。

任何缺失、歧义、患者/Pathway 不一致或 digest 不一致都会 fail closed，不生成部分简报。

## 3. 版本与原子性

- Summary 使用稳定 ID，并以来源 canonical SHA-256 digest 判断是否需要新版本；
- 同一来源重复生成返回当前版本，包括已经有医生审阅决定的当前版本；
- 来源版本变化时创建新的 `safety_reviewed` 版本，旧版本和旧医生决定保持不可变；
- `period.end` 包含 Task、Communication 和工作流 Provenance 时间，支持严格 as-of 回放；
- 存储使用 `BEGIN IMMEDIATE`、current Summary compare-and-swap，并在同一事务内重新核对精确来源版本；
- Summary、Summary Provenance 和生成审计要么全部成功，要么全部回滚。

## 4. Workbench 与证据图

医生页读取通过 `DoctorWorkbenchService(summary_kind="manual_review_brief")` 隔离 Summary 生产者命名空间。证据图区分：

- FHIR 资源与精确版本；
- `provenance_exact_version` 与 `provenance_resource_level`；
- FollowUpMessage、Pathway 和 AuditEvent 等应用记录。

AuditEvent 只证明流程动作发生，不作为 Summary 条目的临床事实证据。Timeline 不在页面加载或生成时重建，页面明确标注其可能为空或过时，不能作为本简报事实来源。

## 5. 写入边界

页面加载、刷新、历史回放和陈旧性检查均只读。只有用户点击“明确生成 / 刷新为当前来源版本”才写入 Summary、Provenance 和审计。医生接受、修改、拒绝继续复用现有不可变版本审阅合同。

本切片没有发送能力、EMR 写回、Alert、ClinicalRule 或 M6 clinical-rule Task；manual-review Task 继续被 M6 视图正向排除。
