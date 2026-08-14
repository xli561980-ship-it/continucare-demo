# 未解决来源与外部依赖

本文件记录当前不能通过工程推断补齐的缺口。机器可读状态以 [coverage_report.json](../../../continucare/knowledge/data/cn_glp1/v1/coverage_report.json) 为准；本文用于解释影响和关闭条件。

## 阻断性缺口

| 优先级 | 缺口 | 当前状态 | 影响 | 关闭条件 |
|---|---|---|---|---|
| P0 | 诺和盈现行完整中国法定说明书 | `blocked_external` | 五条诺和盈产品记录保持 `incomplete`；现有路径不能启用剂量、禁忌、频率、严重度、红旗或处置 | 从 NMPA 或上市许可持有人取得覆盖 2025-12-22 适应证更新的完整说明书；核对五个批准文号、剂型、规格、适应证和批准人群；记录修订日期、官方 URL 与 SHA-256 |
| P0 | 五个运行指标的逐项中国证据 | `blocked_external` | 当前仅有产品范围/胃肠道领域证据与工程数据契约；不能声称当前程度、24 小时次数、液体摄入等指标已获中国说明书逐项验证 | 取得现行完整说明书或其他产品特异中国一手资料，为每个指标增加可核验 locator、allowed/prohibited use，并经临床/药学审核 |
| P0 | 临床、药学和术语审批 | `blocked_external` | 整个发布只能用于合成数据与工程验证，不能用于真实患者 | 目标机构指定临床、药学和术语负责人，对版本化规则、问题、映射和患者文案完成书面审批；同时完成法务、隐私和信息安全流程 |
| P0 | 运行时临床规则 | 有意保持为空 | L4 必须返回 `not_assessed`，不能创建临床 Alert | 只有在产品特异证据与机构审批完成后，另建版本化规则发布；不得在当前版本直接填充 |
| P0 | PRO-CTCAE 项目使用与再分发许可 | `blocked_external` | 本地可核验来源原件；公开 v1.0.3 已排除原文、选项和衍生 Questionnaire | 保存许可/条款接受记录并确认项目使用、衍生 Questionnaire 与提交包的允许范围；获准后只能通过新的受控 Release 接入 |
| P1 | 诺和泰现行完整官方中文说明书 | `blocked_external` | `SJ20210014`、`SJ20210015` 仅为待接入候选，未进入产品注册表，也不计入当前 15 条 | 从 NMPA 或上市许可持有人取得并核验 2025-12-11 修订版或后续最新版完整说明书；逐项核对批准文号、规格、适应证、人群、官方 URL 和 SHA-256 |
| P1 | 中国大陆完整产品目录 | `open` | 当前目录不代表全部 GLP-1、双靶点和多靶点产品 | 按每个品牌、剂型、规格、批准文号、适应证和人群逐项核验 NMPA 记录与中国现行说明书 |
| P1 | 穆峰达一次性预填充笔文号—规格逐项证据 | `blocked_external` | 说明书分别列出 4 个规格与 4 个批准文号，不足以证明一一对应；4 条记录保持 `incomplete` | 从 NMPA 或 NHSA 取得每个文号的独立查询证据，复核候选映射 |
| P1 | L3 中国动态术语集 | `blocked_external` | 固定 5 个 `linkId` 的 CN 白名单和写入门禁已完成；动态 concepts 为空，旧 DailyMed 目录不进入中国 Observation | 术语许可、Edition、中国适用性和人工审核完成后，在新 Release 增加动态 concepts |
| P1 | SNOMED CT 中国许可与发行版 | `blocked_external` | 不能安全添加或分发 SNOMED CT 概念集 | 术语负责人核对 Edition、版本、发布日期、中国适用性及许可范围后再登记 |
| P1 | PRO-CTCAE 独立 Pathway 与 Mapping | `blocked_external` | 当前只登记来源和 11 个非运行指标元数据，没有公开 Questionnaire 或 L2 绑定 | 项目使用与再分发许可确认、临床审阅完成后，在新 Release 中建立独立 draft/synthetic Questionnaire、Pathway 与确定性 Mapping |
| P1 | HL7 FHIR Validator CLI / 机构 IG | `planned` | 已通过 FHIR R4 模型与官方 JSON Schema，不等于完整 Validator/IG 合规 | 机构实施阶段运行官方 Validator CLI 并指定目标 Implementation Guide |

## 产品接入缺口

### 诺和盈

现有上市许可持有人公告可以支持产品身份、公告适应证和胃肠道采集主题，但不能替代完整说明书。FDA WEGOVY 标签不得用于静默补齐中国剂量、禁忌、警示、频率或特殊人群信息。

说明书取得前，现有诺和盈路径只能保持：

```text
draft
synthetic_only
product_specific_label_incomplete
data_collection_only
```

### 穆峰达

两种装置的现行中国说明书已经核验，但一次性预填充笔还缺批准文号—规格的逐项原子证据，对应 4 条产品保持 `incomplete`。穆峰达是 `dual_gip_glp1_agonist`，还没有进入现有 Pathway。后续接入必须分别处理一次性预填充笔和多剂量预装笔，绑定各自说明书版本和批准文号，不能把它们并入单一 GLP-1 产品规则。

需要完成：

1. 产品特异 Pathway 范围；
2. 对应适应证和人群选择；
3. Metric 与中国说明书 Claim 的显式绑定；
4. Questionnaire、PlanDefinition 和 Observation Mapping 重编译；
5. 临床、药学和术语审核。

### 度易达

两个规格的中国说明书已经核验，但当前只登记 `type_2_diabetes`，且尚未进入现有 Pathway。未来路径必须保持糖尿病适应证，不能因为可能出现体重变化或同属 GLP-1 类就外推到长期体重管理。

### 诺和泰候选

诺和泰®／司美格鲁肽的 `SJ20210014`、`SJ20210015` 两个批准文号已完成候选核对，但没有取得可核验的现行完整官方中文说明书。它们尚未写入 `product_registry.json`，不计入当前 15 条产品记录，也不能复用诺和盈、美国 OZEMPIC 或第三方说明书的剂量、禁忌、频率和适应证信息。

取得 2025-12-11 修订版或后续最新版完整说明书后，仍需按“每个批准文号一条记录”分别登记规格和批准范围，再建立产品特异 Claim、Metric、Questionnaire 与 Pathway；仅有产品身份或批准文号不足以启用运行路径。

## 数据和术语缺口

- ICD-11 当前只有许可记录，没有分类本体或可运行 ValueSet；不得宣称 ICD-11 已接入。
- SNOMED CT 尚未锁定可用 Edition 和中国许可，不得批量加入或再分发术语内容。
- MedDRA 未作为当前运行时术语引入；FAERS 中的反应术语只用于离线研究，不能变成个体风险规则。
- FHIR R4 JSON Schema 已用于结构验证，但不能替代 FHIR Validator、术语绑定和项目跨文件约束。
- LOINC 只对当前登记代码和版本提供数据契约支持，不提供临床解释。

## 许可与文件保管缺口

以下材料标记为 `restricted`：PRO-CTCAE 定制表、穆峰达两份中国说明书、度易达中国说明书。当前本地 `local_path` 是工程核验副本位置，不是再分发许可。公开 v1.0.3 不包含 PRO-CTCAE 原文、选项或衍生 Questionnaire；受限原件所在 source pack 整体由 Git 忽略。

对外发布前必须确认：

1. 受限 PDF/DOCX 未进入公开 Git 历史；
2. 受限文件未进入比赛提交 ZIP、演示网页、静态资源和 API；
3. 文档只包含官方 URL、版本、哈希、短定位和规范化声明；
4. PRO-CTCAE 原文使用、衍生 Questionnaire 和项目提交方式均符合接受的条款与再分发边界，且未被改写后继续冒用名称；
5. 所有可分发文件均有明确许可依据。

## 发布一致性要求

Source、Product 或 Evidence Claim 更新后，Coverage Report、Release Manifest 和编译输出必须重新生成。若自动报告中的计数或哈希与注册表不一致，应视为发布尚未闭合，不能手工改文档数字掩盖差异。

关闭任一缺口都必须提交可审计证据；搜索摘要、第三方说明书、电商页面和模型推断不能作为关闭依据。
