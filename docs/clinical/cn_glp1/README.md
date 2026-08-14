# 中国 GLP-1 L1 知识版本 v1.0.3

本目录说明 ContinuCare 中国大陆 GLP-1 相关药品的 L1 知识与 FHIR 契约层。下载资料已经被登记为可追溯 Source，并加工成产品、Evidence Claim、Metric 和 FHIR 产物；运行时不直接读取 PDF、DOCX、网页或季度 ZIP。

## 当前状态

```text
jurisdiction = CN
status = engineering_validated
synthetic_only = true
clinical_approval = null
clinical_rules = []
```

**非临床用途：**本版本仅用于比赛原型、合成数据和工程验证，未完成临床审核，不提供诊断、风险分级或治疗建议，也不得用于真实患者。

## 产品覆盖快照

当前有 15 条产品记录；注册表采用“每个批准文号一条记录”的粒度：

- 诺和盈®五个批准文号：单一 GLP-1 受体激动剂；现行完整中国说明书仍缺失，5 条记录均为 `incomplete`；
- 穆峰达®八个批准文号：GIP/GLP-1 双靶点药物；按两种装置各 4 条登记，两份中国说明书已分别核验；一次性预填充笔 4 条的文号—规格逐项证据仍待补齐；
- 度易达®两个批准文号：单一 GLP-1 受体激动剂，仅登记成人 2 型糖尿病范围；中国说明书已核验。

其中 6 条产品记录为 `verified`，9 条为 `incomplete`（诺和盈 5 条、穆峰达一次性预填充笔 4 条）。这里的“登记”不等于“已进入运行路径”：当前 `GLP1-14D` 仍只限定诺和盈五个批准文号，并保持 `product_specific_label_incomplete`；穆峰达和度易达尚未启用。诺和泰®两个已核对的候选批准文号因未取得现行完整官方中文说明书，尚未进入注册表，也不计入上述 15 条。

详细范围见 [product_coverage.md](product_coverage.md)。动态来源、Claim、Metric 和哈希统计以 [coverage_report.json](../../../continucare/knowledge/data/cn_glp1/v1/coverage_report.json) 与 [release_manifest.json](../../../continucare/knowledge/data/cn_glp1/v1/release_manifest.json) 为准。

## 已建立的知识能力

- LOINC 2.82：校验当前三个 Observation 代码和版本；
- HL7 FHIR R4 4.0.1 JSON Schema：验证 FHIR 基础结构，同时保留项目跨文件约束；
- NCI PRO-CTCAE 简体中文定制子集：只登记来源和 11 个非运行指标元数据；许可范围确认前，公开版本不包含原文或衍生 Questionnaire；
- 国家卫生健康委《肥胖症诊疗指南（2024年版）》：仅支持中国场景与候选内容，不直接形成规则；
- 诺和盈中国上市许可持有人公告：支持产品身份、公告适应证和胃肠道采集主题，但不替代完整说明书；
- 穆峰达两种装置和度易达两个规格：中国说明书支持产品登记和产品特异数据采集 Claim；
- CTCAE v6、FDA WEGOVY 和 FAERS/AEMS：仅用于离线候选审阅、境外背景或信号研究，不进入中国运行时临床判断；
- ICD-11：只登记许可文件，未导入分类本体；SNOMED CT 中国许可和发行版仍待核验。

## 文档导航

- [来源方法学](source_methodology.md)：来源优先级、核验流程、许可和再分发要求；
- [产品覆盖范围](product_coverage.md)：15 条产品记录、剂型、适应证和路径状态；
- [证据覆盖范围](evidence_coverage.md)：Evidence Claim、Metric 和 FHIR 追溯边界；
- [未解决来源](unresolved_sources.md)：诺和盈说明书、产品目录、术语许可和机构审批缺口；
- [运行时边界](runtime_boundaries.md)：L1—L5 允许与禁止行为。

机器可读的事实来源：

- [source_registry.json](../../../continucare/knowledge/data/cn_glp1/v1/source_registry.json)
- [product_registry.json](../../../continucare/knowledge/data/cn_glp1/v1/product_registry.json)
- [evidence_claims.json](../../../continucare/knowledge/data/cn_glp1/v1/evidence_claims.json)
- [metric_definitions.json](../../../continucare/knowledge/data/cn_glp1/v1/metric_definitions.json)
- [terminology_manifest.json](../../../continucare/knowledge/data/cn_glp1/v1/terminology_manifest.json)
- [clinical_rules.json](../../../continucare/knowledge/data/cn_glp1/v1/clinical_rules.json)

## 安全与许可

`clinical_rules.json` 必须为空；L4 风险结果固定为 `not_assessed`，且 `create_alert=false`。数据质量规则只能请求澄清或拒绝无效资源，不能包装成临床风险规则。

PRO-CTCAE 定制表、穆峰达两份中国说明书和度易达中国说明书标记为 `restricted`。本地 `local_path` 是核验副本位置，不是再分发许可；这些 PDF/DOCX 不应进入公开仓库、比赛提交 ZIP、演示下载、网页静态资源或 API。公开 v1.0.3 已排除 PRO-CTCAE 原文及完整衍生 Questionnaire；系统运行时只使用版本锁定且经过范围约束的结构化契约。

## 校验与构建

```bash
# 公开 checkout：不依赖未分发的 source pack
.venv/bin/python scripts/validate_cn_glp1_knowledge.py --skip-source-files
.venv/bin/python scripts/build_cn_glp1_knowledge.py
.venv/bin/python scripts/build_cn_glp1_knowledge.py --check
.venv/bin/python -m pytest -q

# 受控本地核验环境：需要完整 source pack
.venv/bin/python scripts/validate_cn_glp1_knowledge.py
.venv/bin/python scripts/check_cn_glp1_sources.py
.venv/bin/python scripts/validate_fhir_r4.py --schema output/clinical-source-pack-2026-08-13/schemas/fhir_r4_4.0.1_json_schema.zip
FHIR_R4_SCHEMA_ZIP=output/clinical-source-pack-2026-08-13/schemas/fhir_r4_4.0.1_json_schema.zip .venv/bin/python -m pytest -q
```

受限原件和本地 source pack 不随仓库分发；公开校验验证结构化注册表、Manifest、构建可复现性和常规测试，受控本地核验额外验证原始文件 SHA-256、LOINC 内容和官方 FHIR Schema。

构建命令将确定性产物写入 `output/cn-glp1-l1-v1.0.3/`。Source、Product、Evidence Claim、Metric 或 Questionnaire 变化后必须创建新 Release 并重建 Coverage Report、Release Manifest 和全部编译产物，不能原地改写已发布目录。
