# 运行时边界

## 强制状态

中国 GLP-1 L1 当前发布只能保持：

```text
status = engineering_validated
synthetic_only = true
clinical_approval = null
clinical_rules = []
```

这表示系统完成了来源、数据模型、跨文件引用和 FHIR 契约层面的工程校验，但没有完成临床验证、医疗机构审批或生产合规评估。任何界面和答辩材料都必须同时展示“仅用于合成数据和工程验证、未完成临床审核、不提供诊断和治疗建议”。

## 运行时允许做什么

运行时可以：

- 离线加载版本化 JSON Release 和编译后的 FHIR R4 产物；
- 按已声明的 Pathway 产品、适应证和人群范围展示问题；
- 原样展示已获准使用的患者报告问题和选项；
- 保存原始回答、否定、未知、未回答、冲突和需澄清状态；
- 按确定性映射生成符合契约的 Observation；
- 校验 LOINC 版本、UCUM 单位、时间窗和 FHIR 结构；
- 计算 `current`、`stale`、`unknown`、`conflict` 和原始数值方向；
- 展示产品、知识 Release、来源和审核状态；
- 对缺少单位、时间窗不清或 FHIR 无效执行数据质量处理。

数据质量澄清不是临床风险处置。`request_clarification` 只要求补全或确认数据，不能被呈现为红旗、急症分流或医护响应时限。

明确否定保留在 `QuestionnaireResponse`，并由固定布尔映射创建 `valueBoolean=false` 的 Observation；未回答则不创建 Observation。因此 L4 可以区分“明确否认”与“未回答”。

## 运行时禁止做什么

当前版本禁止：

- 诊断疾病或判断“病情恶化”；
- 根据 CTCAE、说明书或模型生成自动严重度等级；
- 识别或发布未经审批的临床红旗；
- 创建临床 Alert 或通知医护进行处置；
- 建议停药、换药、加减剂量或改变给药时间；
- 给出个体化就医、饮食或治疗建议；
- 用说明书总体频率、FAERS 报告数或试验数据推算个人风险；
- 将 FDA、EMA 或其他境外标签作为中国产品事实；
- 将一个品牌、剂型、规格或适应证的证据应用到另一个产品；
- 在运行时联网搜索医学资料、调用 RAG 自由生成临床内容，或直接解析散落 PDF；
- 使用真实患者数据或连接真实医院生产系统。

[clinical_rules.json](../../../continucare/knowledge/data/cn_glp1/v1/clinical_rules.json) 必须保持空数组。[risk_rules.py](../../../continucare/services/risk_rules.py) 当前固定返回：

```text
severity = not_assessed
create_alert = false
```

这两个安全条件不得为了演示效果、测试通过或“先跑起来”而降低。

## 产品门禁

产品进入 [product_registry.json](../../../continucare/knowledge/data/cn_glp1/v1/product_registry.json) 不代表自动进入 Pathway。

### 现有 GLP1-14D 路径

当前路径只包含诺和盈 `SJ20240020`—`SJ20240024` 五条产品记录，并标记 `product_specific_label_incomplete`。由于缺少 2025-12-22 更新后的完整中国说明书，路径只允许采集经过范围约束的胃肠道事实，不能启用剂量、禁忌、频率、分级、报警或处置。

### 穆峰达

穆峰达按每个批准文号一条记录，共登记 8 条产品记录：一次性预填充笔和多剂量预装笔各 4 条。两种装置的中国说明书已经核验，但一次性预填充笔 4 条缺文号—规格逐项证据，保持 `incomplete`。穆峰达是 GIP/GLP-1 双靶点药物。当前登记仅供产品目录和未来 Pathway 设计使用；它没有被现有诺和盈路径覆盖，也不得继承单一 GLP-1 产品规则。

### 度易达

度易达按两个批准文号登记 2 条产品记录并绑定中国说明书，但范围仅为成人 2 型糖尿病。当前没有进入现有路径，不能把其说明书或患者报告主题外推到长期体重管理。

启用新产品至少需要：产品特异 Pathway、适应证和人群范围，显式 Claim／Metric／Questionnaire 绑定，重编译的 FHIR 产物，完整自动校验，以及临床、药学和术语审批。

## 分层消费边界

### L1：知识与契约

负责版本化 Source、Product、Evidence Claim、Metric、术语和患者文案，并编译 FHIR 契约。L1 不发布临床结论。

### L2：Care Engine

只能读取 L1 发布的 Questionnaire 和 Pathway 范围，不得硬编码第二套医学问题、单位或风险逻辑。

### L3：语义抽取

只能使用发布的 `linkId`、Metric、Observation Mapping、术语白名单和中文同义词。输出限于候选概念、值、原文证据、时间范围、否定状态、主体和澄清需求；不能通过指南检索生成诊断、风险或治疗建议。

仓库中现有 DailyMed 多产品症状目录仍是历史合成演示兼容路径，不属于本中国 L1 产品事实或正式白名单。它产生的动态概念只能服务旧合成测试，不能据此宣称中国产品证据、不能进入生产临床路径；正式 CN 白名单发布前，任何未在 L1 Metric 与 Mapping 中绑定的动态 Observation 均视为待迁移兼容行为。

### L4：Clinical Memory

只能保存事实状态和趋势方向。允许表达“过去两次记录的数值增加”，不允许表达“风险升高”“病情恶化”或“治疗效果不佳”。风险结果必须为 `not_assessed`。

### L5：应用界面

必须展示原始回答、标准化 Observation、Pathway 和知识版本、产品范围、来源与审核状态。不得把数据质量提示包装成临床建议。

## 原始文件与许可边界

运行时不读取 PDF、DOCX 或 FAERS ZIP。标记为 `restricted` 的 PRO-CTCAE 定制表及三份中国说明书只能作为受控离线核验副本，不应通过仓库、提交包、页面或 API 再分发。完整复现 PRO-CTCAE 原文的编译 Questionnaire/Release JSON 同样不得进入公开分发包，直到项目许可明确。运行时只能消费由其支持、且已限制产品和用途的结构化 Claim。

## 构建与验证

```bash
# 公开 checkout
.venv/bin/python scripts/validate_cn_glp1_knowledge.py --skip-source-files
.venv/bin/python scripts/build_cn_glp1_knowledge.py
.venv/bin/python scripts/build_cn_glp1_knowledge.py --check
pytest -q

# 持有完整 source pack 的受控本地核验
.venv/bin/python scripts/validate_cn_glp1_knowledge.py
.venv/bin/python scripts/check_cn_glp1_sources.py
.venv/bin/python scripts/validate_fhir_r4.py --schema output/clinical-source-pack-2026-08-13/schemas/fhir_r4_4.0.1_json_schema.zip
FHIR_R4_SCHEMA_ZIP=output/clinical-source-pack-2026-08-13/schemas/fhir_r4_4.0.1_json_schema.zip pytest -q
```

受限原件和本地 source pack 不随公开仓库分发。常规运行和测试不应依赖互联网；持有受控核验包时才执行原始文件、LOINC 内容和官方 FHIR Schema 的附加检查。FHIR JSON Schema 不能覆盖所有术语、Questionnaire 和业务约束，因此必须同时保留项目跨文件校验；任何注册表变化后都应重建 Manifest 和编译产物。
