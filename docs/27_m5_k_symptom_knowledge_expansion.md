# M5-K 症状中心 Knowledge Expansion

## 1. 目标与非目标

M5-K 增加一个跨 Pathway 可复用的 reference-only symptom index。它只保存：

- `symptom_index_id` 与 record version；
- exact terminology catalog term ref；
- exact Claim / Binding / CoverageGap refs；
- lifecycle、supersedes 与 registered timestamp。

索引不保存名称、别名、编码、分级、红旗、review 状态或临床动作。显示名称与
SNOMED coding 每次都从精确 catalog/version 解析；Claim、Binding、Gap 与 review
aggregate 每次都从 Knowledge registry 派生。

本切片不替代 Pathway，不创建 Questionnaire、Observation、Task、Summary、
Communication 或 ClinicalRule，也不读取患者资源。

## 2. 首批比赛 fixture

首批四个 index key 是 `diarrhea`、`nausea`、`vomiting` 和
`abdominal-pain`。它们只是当前比赛 fixture snapshot，不是症状排名、固定分母、
`target_number`、覆盖率目标或完整症状库。

腹泻新增一条精确限定于 `GLP1-14D|1.0.0` 的 draft Claim，但没有 Binding；另外
三个症状复用已有 Claim/Binding。所有 Claim review 继续由空 ReviewEvent registry
派生为 `not_assessed`。同一症状未来可以引用多个 Pathway 的 exact bindings，
但每个 Claim 的 scope 和每个 Binding 的 Pathway/version 始终独立显示。

## 3. 官方外部来源边界

### HPO

- 官方 release：`v2026-06-23`；
- 官方 release 页面：<https://github.com/obophenotype/human-phenotype-ontology/releases/tag/v2026-06-23>；
- 官方文档：<https://obophenotype.github.io/human-phenotype-ontology/>；
- 官方 license 指引：<https://github.com/obophenotype/human-phenotype-ontology/blob/master/LICENSE.md>；
- access：`link_only / not_content_fixed`。

HPO 仅登记为表型/症状概念组织候选。本切片没有建立 HPO→SNOMED mapping，
没有复制 HPO ontology bytes，也不把疾病—表型关联当作患者诊断依据。许可链接已
登记，但没有真实 rights/compliance reviewer，因此没有 license approval event。

### NCI PRO-CTCAE

- 官方 overview：<https://healthcaredelivery.cancer.gov/pro-ctcae/overview.html>；
- 官方 Terms of Use：<https://healthcaredelivery.cancer.gov/pro-ctcae/terms_of_use.html>；
- 官方 validated translations/certificate landing page：<https://healthcaredelivery.cancer.gov/pro-ctcae/countries-pro.html>；
- 官方 instrument/Form Builder landing page：<https://healthcaredelivery.cancer.gov/pro-ctcae/instrument-pro.html>；
- access：`link_only / not_content_fixed`。

官方页面说明 PRO-CTCAE 面向肿瘤临床研究、与 clinician CTCAE 配合使用，并且
不是诊断、预后或治疗工具。Terms of Use 禁止未经书面许可修改、缩写、翻译或制作
衍生版本，并限制分发。本仓库不保存 PDF、题目、选项或翻译，不绑定 GLP1
Questionnaire/Observation，不做 CTCAE Grade 或 L0/L2/L4 转换。study registration
和 agreement 状态未核验，因此该 Source 保持 unbound，review 为 `not_assessed`。

## 4. 患者表达与 CoverageGap

现有 terminology catalog 已拥有 runtime aliases。M5-K 不再创建第二份患者口语
fixture，也不把这些 aliases 重新声明为已验证患者表达。四个 symptom index 各自
引用一个 `patient_expression_evidence` CoverageGap，明确缺少独立来源、临床审核和
真实患者验证。

## 5. 加载与失败语义

`bundle_index_v1.json` 是唯一入口，并固定 symptom index manifest 的 path、raw-byte
SHA-256 和 size。所有 index→catalog/Claim/Binding/Gap refs 在返回 registry 前一次性
验证：

- CURRENT：未知 exact catalog term 或非 current exact ref fail-closed；
- HISTORICAL：未知 catalog term 明确显示 unresolved；
- 任一 hash、size、schema 或交叉引用失败都不返回部分 registry。

CLI 示例：

```console
python -m continucare.knowledge --symptom-index-id nausea --record-version 1
python -m continucare.knowledge --symptom-index-id nausea --record-version 1 --historical
```

## 6. 单向依赖与只读页面

依赖方向保持：

```text
Knowledge symptom index
→ terminology catalog exact ref
→ Knowledge Claim / Binding / CoverageGap exact refs
```

`terminology`、`care_agent`、`layer4`、`services` 和 Pathway runtime 不导入
`continucare.knowledge`。独立 Streamlit 页面只导入 Knowledge registry 和共享视觉
样式；它不导入数据库、患者 store、runtime service、Layer 3 或 Layer 4。页面加载、
切换症状和 CURRENT/HISTORICAL 都是离线只读操作。
