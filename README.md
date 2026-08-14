# ContinuCare Demo

用版本化 FHIR R4 `Questionnaire` 动态驱动合成患者随访，并通过受控 Care Agent 把患者自由表达整理成待确认候选；患者确认后，第二层将答案保存为完整 `QuestionnaireResponse`，把明确事实确定性沉淀为可追溯的 `Observation`。

> **安全边界：仅使用合成数据。系统不是医疗急救通道，不生成诊断、治疗或用药建议。**

当前版本已接入小米 MiMo OpenAI-compatible 适配器；配置本地密钥时使用 `mimo-v2.5` JSON mode，分别承担受控抽取、Safety Critic 和患者语言改写。主抽取不可用时回退本地语义 Mock，辅助模型不可用时回退确定性硬规则或固定语言模板。无论哪种模式，Safety Agent 和患者确认门都不能绕过。飞书通知仍使用 Mock 适配器。

## 本地运行

要求 Python 3.11+。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/streamlit run app.py
```

打开 Streamlit 输出的本地地址即可进入首页。运行数据默认保存在 `data/continucare.db`，该目录已被 Git 忽略。

## 小米 MiMo 配置

复制 `.env.example` 为被 Git 忽略的 `.env`，只在本机填入轮换后的密钥：

```dotenv
CONTINUCARE_LLM_PROVIDER=xiaomi_mimo
CONTINUCARE_LLM_MODEL=mimo-v2.5
CONTINUCARE_LLM_BASE_URL=https://api.xiaomimimo.com/v1
CONTINUCARE_LLM_API_KEY_ENV=MIMO_API_KEY
MIMO_API_KEY=your-rotated-key
CONTINUCARE_LLM_PROMPT_VERSION=mimo-semantic-extraction-v4
CONTINUCARE_USE_SAFETY_LLM=true
CONTINUCARE_SAFETY_PROMPT_VERSION=mimo-safety-critic-v2
CONTINUCARE_USE_LANGUAGE_LLM=true
CONTINUCARE_LANGUAGE_PROMPT_VERSION=mimo-language-rewrite-v1
CONTINUCARE_USE_SUMMARY_LLM=false
CONTINUCARE_SUMMARY_PROMPT_VERSION=mimo-summary-outline-v1
```

最小联通测试：

```bash
.venv/bin/python scripts/mimo_smoke_test.py
```

10例合成文本的真实模型回归：

```bash
.venv/bin/python scripts/evaluate_mimo_live.py --fail-on-mismatch
```

评测报告默认写入 `/tmp/continucare-mimo-live-evaluation.json`。该结果属于工程验证，不能替代临床验证。

同一 MiMo 配置用于 Care 抽取、Safety Critic 与患者语言改写，但三者使用独立 Prompt 和严格 JSON 合同。Safety LLM 只能降级候选，不能推翻确定性硬规则；它发现有逐字证据的遗漏时，只能触发已发布 linkId 的定向补抽取。第三层以每日随访 occurrence 保存本次全部对话，并把已完成 Observation 作为只读跨日上下文；历史记录不能成为今天候选的证据。“今天/昨天/过去24小时”按患者时区解析并贯通到 Observation `effective[x]`。语言改写会本地锁定数字、单位、症状、程度、肯否和时间，不满足事实完整性时自动回退固定模板。

第四层另提供默认关闭的受控 Summary LLM：它面对任意数量的动态事实清单，只能返回已有 `fact_id` 的分组和顺序，不能生成正文、风险或建议。事实遗漏、伪造 ID、跨栏目移动、模型故障或超过容量门时，系统使用完整事实清单确定性回退。

受控 Summary 已使用官方 MiMo API 完成固定合成数据验收：5/5 用例、64/64 条事实恰好覆盖一次，包括未知医生自定义指标、事实文本提示注入、40 指标容量场景和完整服务/存储/Provenance 链路。该结果是工程验收，不是临床验证；提交配置仍默认关闭真实 Summary 调用。

MiMo 不生成医学代码。已知 Questionnaire 字段和患者自述新症状都必须经过 `continucare/terminology/data/glp1_symptom_catalog_v1.json` 检索与版本校验：唯一命中后显示确认卡，多候选（例如“头晕”）显示语义区分按钮，未命中则只保留原话并等待术语/医生复核。当前目录是基于官方 GLP-1 药品标签建立的原型覆盖集，不声称穷尽所有可能症状；医院部署时通过同一后端协议接入其 FHIR 术语服务器。

2026-08-02 的冻结配置 10 例单轮合成数据评测达到业务结果 10/10、完整三 Prompt 链路 10/10、原始模型输出 10/10。Layer 3 工程发布清单、原始报告和 Git 回滚标签固定为 `continucare-layer3-v1.0.0` / `layer3-v1.0.0`。该结果是工程回归，不是临床验证。

密钥不会进入 AgentRun、审计日志或 Git。当前只允许官方 `*.xiaomimimo.com` HTTPS 地址，并且只发送合成演示文本。[MiMo 官方快速接入](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call) · [JSON mode](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/text-generation/structured-output)

## 三个核心价值

- 原始回答与 FHIR Observation 通过 `derivedFrom` 可追溯；
- 患者端由 Questionnaire 动态渲染，结构化答案不依赖 LLM；
- 对话式自由表达只生成候选，患者确认前不写入第二层；
- Agent 输出通过 linkId/code、enableWhen、证据跨度、候选值、主语、否定、时间和单位安全检查；
- 随访会话锁定 Pathway/Questionnaire 版本，支持草稿恢复和幂等提交；
- LOINC、SNOMED CT 与 UCUM 映射有独立权威信源包；
- GLP-1 症状目录对已知与新增症状统一检索，保留目录版本、命中别名、code 与确认来源；
- 没有获批临床规则时采取 fail-closed，不输出风险等级或 Alert；
- Summary 审阅和 Mock 通知进入本地审计链。

## 页面

- 首页：先展示最终交付物，以及患者 → 护士 → 医生的闭环；
- 患者随访：先展示本次结果、患者原话、记录事实和明确下一步；
- 护士任务中心：先展示今天要处理什么、为什么进入队列以及任务最终结果；
- 医生复诊简报：首屏用 30 秒呈现“患者报告、团队处理、复诊待确认”；
- 工作流证据链：用人类可读的六阶段时间线还原结果形成过程，技术记录按需展开。

## 临床与标准依据

- [整体六层方案与端到端工作流](docs/14_layered_solution_blueprint.md)
- [第一层验收报告](docs/15_layer_1_acceptance.md)
- [第二层验收报告](docs/16_layer_2_acceptance.md)
- [第三层验收报告](docs/17_layer_3_acceptance.md)
- [第三层 v1.0.0 发布基线与回滚说明](docs/releases/layer3_v1.0.0.md)
- [第四层第 1 步：合同与存储](docs/18_layer_4_contract_and_storage.md)
- [第四层第 2 步：Clinical Memory 与 Timeline](docs/19_layer_4_clinical_memory.md)
- [第四层第 3 步：双审批规则与 Task 责任闭环](docs/20_layer_4_approved_rules_and_tasks.md)
- [第四层第 4 步：证据化 Summary 与医生审阅](docs/21_layer_4_evidence_summary_and_doctor_review.md)
- [第四层第 5 步：状态快照与原始数值趋势](docs/22_layer_4_state_snapshot_and_numeric_trends.md)
- [第四层第 6 步：Doctor Workbench 只读组合查询与整体回放](docs/23_layer_4_doctor_workbench_read_model.md)
- [第四层增强项：指标数量无关的受控 LLM Summary](docs/24_layer_4_controlled_llm_summary.md)
- [FHIR R4 合规策略与上线门槛](docs/13_fhir_conformance_policy.md)
- [GLP-1 指标、术语映射与权威临床信源](docs/clinical/glp1_14d_observation_evidence.md)
- [GLP-1 患者可报告症状术语目录与检索流程](docs/clinical/glp1_symptom_terminology_catalog.md)
- [当前 FHIR 数据模型](docs/03_data_model_fhir.md)

`assets/screenshots` 中旧截图属于早期工作流原型，包含已停用的合成 L2/L4 规则，不作为当前实现或临床依据。

## 当前实施范围

- 第一层工程基线：FHIR R4 契约、术语、证据、追溯和 fail-closed 治理
- 第二层比赛工程基线：Care Session、动态 Questionnaire renderer、通用回答 Builder 和确定性 Observation 映射
- 第三层比赛工程基线：Agent Runtime、MiMo/Care Agent 语义候选、Safety Agent v4 混合复核、受控连续对话、患者本地时间、事实锁定语言改写、患者确认与本地回退
- 第四层第 1 步工程基线：最终 Observation 状态门禁、Memory/Timeline/Revision/规则/Summary 合同、FHIR Task/Communication/Provenance 和版本化完整 JSON 存储；规则执行仍保持关闭
- 第四层第 2 步工程基线：最终资源确定性摄取、证据化 Clinical Memory、按临床有效时间组织的 Timeline、迟到数据重排、冲突/缺失表达和可追溯修订；规则执行仍保持关闭
- 第四层第 3 步工程基线：双审批规则执行门、逐条件证据解释、Task 去重、版本化责任状态机及 Clinical Memory 历史；仓库无 active 临床规则，产品路径仍为 not_assessed
- 第四层第 4 步工程基线：当前 Timeline 的确定性证据简报、生成时点门、Summary 版本链及医生接受/修改/拒绝；LLM 摘要仍未启用
- 第四层第 5 步工程基线：版本化指标定义、current/stale/unknown/conflict 状态、单位一致的端点数值方向、快照版本链及 Provenance；不输出好转/恶化或风险解释
- 第四层第 6 步工程基线：Timeline/State/Summary/Task 只读组合查询、patient/pathway 权限隔离、历史 as-of 回放、版本化证据图及组件级故障降级；尚未替换旧医生页面或接入真实 IAM/EMR
- 旧 M0–M5 自由文本链继续作为兼容测试夹具，不是患者端主流程
- M6：真实飞书/Aily 接入，明确不在第一版范围内

## 演示

- [60–90 秒与 2–3 分钟脚本](docs/demo_scripts.md)
- [安全边界](docs/implementation_safety.md)
- [实施架构](docs/implementation_architecture.md)
- [飞书 / Aily 集成状态](docs/feishu_integration.md)
- 连续三次无外部服务彩排：`.venv/bin/python scripts/rehearse_demo.py`

## 验收命令

```bash
curl -L https://hl7.org/fhir/R4/fhir.schema.json.zip -o /tmp/fhir-r4-schema.zip
FHIR_R4_SCHEMA_ZIP=/tmp/fhir-r4-schema.zip .venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_fhir_r4.py --schema /tmp/fhir-r4-schema.zip
.venv/bin/python scripts/evaluate_semantic_layer.py
.venv/bin/streamlit run app.py
```

所有演示身份、消息和结果均为合成数据。禁止把运行数据库、密钥或真实患者信息提交到仓库。
