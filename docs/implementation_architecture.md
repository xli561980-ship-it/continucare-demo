# ContinuCare 本地 Demo 实施架构

## 当前主流程：受控语义第三层 + Questionnaire 第二层

```text
Patient.pathway_code
  └─ CareEngine.start_or_resume
       ├─ Pathway Registry / 版本锁定
       ├─ CareSession 草稿与生命周期
       └─ FHIR Questionnaire
            ├─ 动态患者端 Renderer（完整问卷兜底）
                 ├─ answerOption / enableWhen
                 ├─ boolean / choice / integer / quantity / text
                 └─ QuestionnaireResponse Builder + 语义校验
                      ├─ 完整 FHIR QuestionnaireResponse
                      ├─ 确定性 Observation Mapping Policy
                      │    └─ FHIR Observation + derivedFrom
            └─ Care Agent 对话辅助
                 ├─ SemanticTask（锁定问卷 + 短期上下文 + 时间锚点）
                 ├─ ConversationContext（本次每日随访全部轮次 / 待确认 action）
                 ├─ TemporalContext（患者时区 / 当地日期 / 每日 occurrence）
                 ├─ MiMoSemanticAdapter（抽取字段 + 不带 code 的症状检索词）
                 ├─ Repository Terminology Resolver
                 │    ├─ GLP-1 版本化症状目录 / Questionnaire bindings
                 │    ├─ 唯一匹配 / 多候选消歧 / 未匹配待复核
                 │    └─ FHIR Terminology Server / RAG backend 可替换接口
                 ├─ 本地语义 Mock 回退
                 ├─ Safety Agent v4
                 │    ├─ 确定性硬规则
                 │    ├─ MiMo Safety Critic（只能降级）
                 │    └─ 遗漏 linkId 定向补抽取 / 问卷澄清
                 ├─ Care Agent 内部 MiMo Language Rewriter
                 │    └─ 本地事实完整性检查 / 固定模板回退
                 └─ 候选卡片 / 动态澄清 / 分阶段审计
                      └─ 患者确认后 CareEngine.save_draft

SQLiteStore 原子提交
  ├─ CareSession
  ├─ AgentRun（版本、模式、输入哈希、结构化输出）
  ├─ Conversation action resolution（上轮候选/澄清的接受、拒绝、不确定）
  ├─ Confirmed answer context（原话、occurrence、患者时区、effective start/end）
  ├─ Confirmed symptom report（目录概念、code、原话、来源分栏、时间）
  ├─ 完整 FHIR JSON
  ├─ 检索投影和证据元数据
  ├─ Summary / Review
  └─ AuditEvent
```

手工结构化答案不经过 Mock 或 LLM。自由文本只由第三层提出候选；未经过 Safety Agent 与患者确认不能进入 CareSession。当前 `clinical_rules=[]`，完成采集后保持 `not_assessed / no Alert`。

第四层的唯一读取入口是 `continucare.layer4.Layer4InputReader`。它只返回 completed CareSession 的最终 QuestionnaireResponse、由这些响应派生的最终 Observation 和 AuditEvent，不提供 AgentRun、聊天轮次、SemanticCandidate、模型响应或 Mock 正则中间状态。第四层因此必须从标准化最终事实重建记忆和工作流，不能耦合第三层实现细节。

## 第三层强制边界

- `AgentRuntime`：只运行显式注册 Agent；当前独立注册 Care Agent 与 Safety Agent，二者工具白名单均为空，不能直接操作数据库或 FHIR。
- `SemanticModelAdapter`：Provider-neutral 适配协议；仓库不包含密钥。
- `MiMoSemanticAdapter`：仅允许小米官方 HTTPS 域名，严格解析 JSON/Pydantic 契约，不信任模型提供的 FHIR code 或患者措辞。
- `CareSemanticAgent`：MiMo 可用时调用适配器，失败或未配置时回退本地 Mock。
- `SafetyAgent`：硬规则拒绝未知 linkId/code、无效 answerOption/UCUM、未满足的 enableWhen、错误证据跨度、候选值与证据冲突、错误主语/时间和无需确认的候选；可选 MiMo Critic 只能进一步降级。
- `MiMoSafetyCritic`：独立 Prompt 复核所有硬规则幸存候选，并检查有逐字证据但遗漏的已发布字段；发现遗漏时只触发该 linkId 的定向补抽取，不能新增问题或恢复硬拒绝。
- `MiMoLanguageRewriter`：作为 Care Agent 内部能力优化患者措辞；本地校验数字、单位、症状、程度、肯否和时间，任何改变都回退 `PatientLanguageRenderer` 固定模板。
- `stage_traces`：在单个顶层 AgentRun 中记录每个阶段的 Agent/Prompt/模型、Token、延迟、请求 ID、重试与回退，不另造重复业务任务。
- `CareAgentService`：只有患者接受候选或澄清选项时才能调用 `CareEngine.save_draft`。
- `ConversationContext`：短期记忆覆盖本次每日 follow-up occurrence，而非固定 5 轮；短回答只能绑定唯一待处理动作。
- `long_term_memory`：只读取此前已完成的 Observation，不读取草稿或模型摘要；只能作上下文，不能成为今天候选的证据。
- `RepositoryTerminologyBackend`：已知与新增症状走同一检索合同；模型只给检索词，不给 code。部署时可以替换为医院 FHIR `$expand/$lookup/$validate-code` 服务或检索增强后端。
- `TemporalContext`：所有相对时间使用患者 IANA 时区和该轮 `received_at` 解析；`scheduled / submitted / effective` 不复用一个时间字段。
- Agent 不生成诊断、治疗、用药建议、风险等级或 Alert。

## 兼容测试流程

旧 `FollowUpService → MockExtractor` 自由文本链继续用于固定测试夹具和彩排回归，但不再是患者端主流程。它不会调用外部模型，也不能代表第三层生产语义理解能力。

## 强制边界

- `CareEngine`：锁定 Pathway/Questionnaire 版本，管理草稿、完成、停止和幂等提交。
- `build_questionnaire_response`：只能按照 Questionnaire item type 构造 FHIR `answer.value[x]`。
- `validate_questionnaire_response_against_questionnaire`：检查 canonical、版本、linkId、回答类型、answerOption、required、repeats 和 enableWhen。
- `ObservationMappingPolicy`：只有登记的 linkId 和转换方式可以形成 Observation。
- `validate_r4_resource`：写入前使用 R4 生成模型拒绝未知字段和无效基础结构。
- `validate_official_json_schema`：CI 使用 HL7 官方 `fhir.schema.json.zip` 做独立验证。
- `evaluate_risk`：当前没有获批规则，始终返回 `not_assessed`；未来规则必须经版本化临床审批后才可启用。
- `SQLiteStore`：保存完整资源；数据库索引列只是投影，不能替代 FHIR 资源。

业务服务不导入 Streamlit、飞书 SDK 或特定外部模型 SDK。医院对接时须增加目标 Profile、CapabilityStatement、术语服务和安全规范，而不是修改或伪造 FHIR 基础字段。

## M5-E 可选外部适配器边界

飞书 Bot、Aily 与 Bitable 通过统一 `AdapterFactory` 和 provider-neutral `HttpTransport` 隔离。默认 `mock/mock/disabled` 不创建真实 transport；`test_tenant` 必须同时满足精确 mode、能力 flag、全局 egress flag 与完整配置。真实 transport 只允许官方飞书 HTTPS host，禁用代理与 redirect，并限制 timeout/response size。

Aily 仅实现 Layer 3 `SemanticModelAdapter`，输出仍经过本地全量 Schema、安全门、terminology 重绑和患者确认。Bitable 是 write-only 合成投影，不是 store；Bot notifier 未接入 manual-review Communication。完整边界见 `docs/29_m5_e_optional_feishu_aily_adapters.md`。
