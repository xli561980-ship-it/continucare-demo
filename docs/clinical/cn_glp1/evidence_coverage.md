# 证据覆盖范围

## 如何理解“覆盖”

本项目中的 Evidence Claim 是带来源、产品、适应证、人群、定位、允许用途和禁止推导的结构化声明。它不是临床建议，也不是对整份来源的自由检索授权。

当前证据覆盖数量以自动生成的 [coverage_report.json](../../../continucare/knowledge/data/cn_glp1/v1/coverage_report.json) 为准；具体声明内容以 [evidence_claims.json](../../../continucare/knowledge/data/cn_glp1/v1/evidence_claims.json) 为准。`runtime_eligible=true` 只允许在 Claim 的 `allowed_use` 范围内消费，不能越过 `prohibited_inference`。

## 当前证据族

| 证据族 | 当前支持内容 | 允许用途 | 不能支持的内容 |
|---|---|---|---|
| 诺和盈中国上市许可持有人公告 | 诺和盈中国产品身份、已公告适应证范围、胃肠道患者报告采集主题 | 将现有路径限制到诺和盈；展示来源；采集原始胃肠道事实 | 不能替代完整说明书，不能支持剂量、禁忌、频率、分级或处置 |
| 穆峰达一次性预填充笔中国说明书 | 对应装置的 4 个规格、4 个批准文号、三类适应证及胃肠道采集主题 | 支持装置级说明书事实；文号—规格逐项对应尚缺独立官方证据，4 条产品保持 `incomplete` | 不能套用于多剂量装置、其他品牌或单一 GLP-1；不能推算个体风险 |
| 穆峰达多剂量预装笔中国说明书 | 对应装置 4 个批准文号各自的规格、三类适应证及胃肠道采集主题 | 支持 4 条独立产品记录；为未来产品特异问题提供结构化证据 | 不能套用于一次性装置或其他产品；不能生成剂量、停药或就医建议 |
| 度易达中国说明书 | 两个批准文号各自规格的成人 2 型糖尿病范围及恶心、呕吐、腹泻采集主题 | 支持 2 条独立糖尿病产品记录；为未来糖尿病路径提供数据采集证据 | 不能外推到体重管理，也不能套用于其他度拉糖肽产品 |
| PRO-CTCAE 简体中文定制子集 | 来源版本和食欲下降、恶心、呕吐、便秘、腹泻、腹痛的 11 个计划指标元数据 | 记录待授权范围与未来映射位置 | 许可范围确认前，公开版本不能包含原文、选项或衍生问卷；不能自动诊断或分级 |
| LOINC 2.82 | 恶心、24 小时呕吐次数和 24 小时估计液体摄入的观察代码与版本 | 校验 FHIR Observation 代码；约束确定性映射 | 代码不构成临床含义、风险或治疗判断 |
| 国家卫生健康委《肥胖症诊疗指南（2024年版）》 | 中国肥胖症诊疗场景、候选产品类别和候选胃肠道采集主题 | 离线发现和背景说明 | 不能替代产品说明书，不能直接进入运行时产品事实或规则 |
| CTCAE v6 | 胃肠道严重度概念的候选人工参考 | 离线设计待审映射 | 当前不得自动分级或报警 |
| FDA WEGOVY 2026 标签 | 美国 WEGOVY 安全背景 | 离线背景比较和答辩边界说明 | 不能作为中国说明书或支持中国运行时判断 |
| FAERS/AEMS 2026 Q2 | 自发报告数据结构和离线信号探索 | 报告计数、术语探索和解析流程验证 | 不能计算发生率、证明因果或判断个人风险 |

## 指标与问卷覆盖

[metric_definitions.json](../../../continucare/knowledge/data/cn_glp1/v1/metric_definitions.json) 定义当前工程指标。所有指标均设置 `clinical_interpretation_allowed=false`，并显式规定缺失值不创建 Observation、冲突时请求澄清。

当前指标分为两类：

1. `GLP1-14D` 的当前或过去 24 小时事实，包括恶心、呕吐次数、估计液体摄入和腹痛；
2. PRO-CTCAE 的 11 个计划指标元数据，包括频率、最严重程度和活动影响；当前不含问题原文或选项。

必须区分两套内容：`GLP1-14D` 的当前/24 小时问题是工程团队自拟的数据采集文案，引用 `continucare-glp1-14d-wording-2026-001`，不得称为 PRO-CTCAE；11 个 PRO-CTCAE 指标仅保留来源、范围和待授权元数据。许可确认前，公开 Release 不生成独立七日 Questionnaire，也不绑定 L2 Pathway 或 Observation Mapping。

还必须区分“领域证据”和“逐指标证据”：诺和盈现有中国公告只支持产品范围以及胃肠道患者报告采集主题，没有逐项验证当前恶心程度、24 小时呕吐次数、24 小时液体摄入或当前腹痛这些工程契约。五个指标的 `synthetic_runtime_eligible=true` 仅表示确定性合成数据链可运行；它们全部保持 `clinical_runtime_eligible=false`，不得答辩成“已经由中国说明书逐项验证的临床指标”。

三个 Observation 代码已经用 LOINC 2.82 离线发行包核验。Quantity 指标使用 UCUM 单位；缺少单位、时间窗不清或 FHIR 结构无效只触发数据质量处理，不产生临床风险等级。

需要特别区分：穆峰达和度易达的中国说明书 Claim 已进入证据库，但当前 `GLP1-14D` Pathway 仍只限定诺和盈，现有 Metric 的 Evidence 绑定也没有因此自动扩展到穆峰达或度易达。启用这些产品前，必须建立产品特异的 Metric／Questionnaire 绑定并重新验证范围，不能复用诺和盈绑定来“默认支持”。

## FHIR 追溯链

当前预期追溯关系为：

```text
Questionnaire.item
→ metric_id
→ evidence_claim_ids
→ source_id
→ 官方 URL、版本与 SHA-256
```

Observation Mapping 还应能回到 Questionnaire `linkId`、Metric 和 Evidence Claim。FHIR R4 JSON Schema 只验证结构的一部分；产品范围、术语版本、时间窗、单位和跨文件引用仍由项目自身校验器检查。

每次 Product、Source、Claim、Metric 或 Questionnaire 变化后，必须重新生成 Release Manifest 和以下编译产物，不能手工修改输出文件：

- `release.json`；
- `glp1_14d_questionnaire.json`；
- `glp1_14d_plan_definition.json`；
- `glp1_14d_observation_mapping.json`。

## 未覆盖的临床证据

当前没有经过审批、可用于运行时的下列证据：

- CTCAE 或其他临床严重度到系统等级的映射；
- 个体红旗、临床报警阈值或响应时限；
- 停药、换药或剂量调整逻辑；
- 个体化就医、饮食或治疗建议；
- 从 FAERS、说明书频率或临床试验总体频率到个人风险的转换；
- 覆盖全部中国 GLP-1 和多靶点产品的目录与产品特异问卷；
- 诺和盈 2025-12-22 更新后的完整中国法定说明书证据。
- 诺和泰两个候选批准文号的现行完整官方中文说明书证据；候选尚未进入产品注册表。

因此 `clinical_rules.json` 必须保持为空，L4 只能存储事实状态和原始数值方向，不能表达“病情恶化”“风险升高”或“治疗无效”。

## 受限材料边界

Evidence Claim 可以保存必要的短定位锚点和规范化表述，但不得长篇复制来源。PRO-CTCAE 定制表和三份上市许可持有人中国说明书标记为 `restricted`，其原始 PDF/DOCX 不应随仓库、提交包、演示资产或 API 再分发。公开 v1.0.3 已排除 PRO-CTCAE 原文与衍生 Questionnaire；只有取得并留存适用于本项目交付方式的许可后，才能在新的受控 Release 中加入。运行时只使用经范围约束的结构化声明。
