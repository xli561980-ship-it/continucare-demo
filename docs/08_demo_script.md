# 08. 比赛 Demo 方案（FHIR 基线版）

> 当前 M5-D 主比赛故事与最新点击脚本见 [28_m5_d_competition_demo.md](28_m5_d_competition_demo.md) 和 [demo_scripts.md](demo_scripts.md)。本章保留早期 FHIR 基线场景，现已归入首页“其他技术演示”。

本章全部身份、事件和数值均为合成演示内容。当前版本先证明“患者原始回答如何可靠进入标准数据层”，不演示未经审批的自动分诊、诊断、治疗或用药建议。

## 1. Demo 目标

展示一条可验证的数据链：

```text
患者原话
  → FHIR QuestionnaireResponse
  → 受控抽取
  → FHIR Observation（LOINC / SNOMED CT / UCUM）
  → derivedFrom 回到原回答
  → 证据化复诊简报与审计
```

## 2. 主线场景

选择 GLP-1 治疗后 14 天患者报告采集。当前临床资料主要来自 Wegovy 的 FDA/EMA 监管材料，不能直接外推为所有 GLP-1 或 GLP-1/GIP 产品的统一路径。

患者提交合成原文：

```text
今天吐了一次，估计过去24小时喝水800毫升。
```

系统保存：

- 一条完整的 FHIR R4 `QuestionnaireResponse`；
- LOINC `94070-0` Emesis count 24 hour，`valueQuantity=1`，UCUM `/d`；
- LOINC `75301-2` Fluid intake 24 hour Estimated，`valueQuantity=800`，UCUM `mL/(24.h)`；
- 两条 Observation 的 `derivedFrom` 均指向本次 QuestionnaireResponse；
- 原文字符位置与抽取置信层级保存在应用证据表，不添加到 FHIR 资源。

系统不做：

- 不把“一次呕吐”自动判定为 L2；
- 不把“800 mL”自动解释为脱水或报警阈值；
- 不给出诊断、药物调整或处置建议；
- 不声称已通过具体医院 Profile 或术语服务器认证。

## 3. 三个可点击场景

| 场景 | 预期结果 |
|---|---|
| 恶心记录 | 明确恶心形成 SNOMED CT `422587007`；否定呕吐不形成阳性 Observation |
| 呕吐与摄入记录 | 形成两个量化的 LOINC/UCUM Observation |
| 仅保留患者原文 | 原文保存在 QuestionnaireResponse；当前范围外表达不被瞎编为结构化事实 |

三个场景均返回 `not_assessed`，因为当前 Pathway 的 `clinical_rules=[]`。

## 4. 页面展示重点

| 页面 | 演示重点 |
|---|---|
| 首页 | 当前 FHIR 基线、合成数据和未启用临床规则 |
| 患者随访 | 原话、标准化事实、编码系统、来源关系 |
| 护士任务中心 | 当前没有获批规则，队列为空；未来只接收已审批规则产生的任务 |
| 医生复诊简报 | 患者报告事实、缺失日期、原文证据和审阅留痕 |
| 审计日志 | 提交、抽取、规则检查、摘要和审阅全过程 |

## 5. 临床与术语依据

演示时以 [GLP-1 Observation 与临床信源包](clinical/glp1_14d_observation_evidence.md) 为唯一指标说明文档；以 [FHIR R4 合规策略](13_fhir_conformance_policy.md) 解释“基础结构合规”和“医院实施合规”的区别。

## 6. 后续临床闭环

护士任务、SLA 和升级流程属于下一层。在启用前必须为每条规则补齐：适用药物与人群、证据章节、操作阈值、临床负责人、术语负责人、批准日期、回顾性测试和回退方案。未满足时保持 fail-closed。
