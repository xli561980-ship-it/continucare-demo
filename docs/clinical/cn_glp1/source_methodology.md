# 来源方法学

## 目的与适用边界

本方法学用于 ContinuCare 中国大陆 GLP-1 L1 知识与 FHIR 契约层的离线来源治理。它说明如何发现、核验、登记和引用资料，不赋予任何资料临床审批效力。

当前发布状态为 `engineering_validated`，并且 `synthetic_only=true`。这是明确的非临床用途发布：所有来源、结构化声明和编译产物仅用于比赛原型、合成数据和工程验证，不得用于真实患者诊疗、自动风险判断或治疗决策。

## 来源选择顺序

来源按下列顺序评估，较低层级不得静默替代较高层级缺口：

1. 中国监管或卫生行政机关发布的注册信息、正式指南、安全公告和说明书修订公告；
2. 中国现行药品说明书，以及上市许可持有人提供且能核对版本、剂型和批准文号的中国说明书；
3. 中国专业组织的正式指南、共识和 CDE 技术文件，仅用于场景背景或候选内容；
4. HL7 FHIR、LOINC、UCUM 等互操作和术语标准；
5. PRO-CTCAE、CTCAE、FDA 标签、FAERS/AEMS 等境外或研究资料，仅按登记用途使用。

搜索结果摘要、商业药店、电商页面、自媒体、无法确认修订日期的网络说明书和模型记忆不得作为权威来源。找不到中国现行说明书时，必须保留 `label_unverified` 或等价缺口，不得用美国或欧洲标签补齐中国事实。

## 纳入与核验流程

每个候选来源依次完成以下检查：

1. 核对发布机构、域名和中国大陆适用范围；
2. 核对标题、文件编号、发布日期、说明书修订日期、剂型和产品身份；
3. 优先保存官方原始文件；动态网页则保存抓取日期、官方 URL 和受控快照；
4. 对实际取得的字节计算 SHA-256，并记录本地保管路径；
5. 登记许可状态、核验状态、允许用途、是否可进入结构化运行时契约；
6. 从原始材料提取短小、可定位的 Evidence Claim，不在运行时解析整份 PDF；
7. 运行跨文件校验，确保 Claim、产品、指标和 FHIR 映射的引用可以闭合；
8. 来源或版本变化时创建新记录并重建 Coverage Report、Release Manifest 和编译产物，不原地伪装旧版本为新版本。

产品注册表采用“每个批准文号一条记录”的粒度。一个说明书来源可以支持同一装置下的多个批准文号，但每条产品记录必须各自保存批准文号、规格、适应证、人群和核验状态；不得把多个规格合并后丢失一一对应关系。

权威事实以 [source_registry.json](../../../continucare/knowledge/data/cn_glp1/v1/source_registry.json) 为准；覆盖统计和文件哈希分别以自动生成的 [coverage_report.json](../../../continucare/knowledge/data/cn_glp1/v1/coverage_report.json) 与 [release_manifest.json](../../../continucare/knowledge/data/cn_glp1/v1/release_manifest.json) 为准。

## `runtime_eligible` 的含义

Source 层的 `runtime_eligible=true` 只表示**中国来源**可以在已声明范围内支持当前合成运行路径，例如产品身份、问题措辞或原始患者报告采集。国际互操作/术语标准（HL7 FHIR、LOINC、UCUM）在 Source 层保持 `false`，具体固定代码、版本与许可状态由 `terminology_manifest.json` 逐项控制；登记标准不等于把境外临床证据放入中国运行时。该字段也不表示：

- 原始 PDF 会被运行时加载；
- 来源已经通过临床、药学或术语审批；
- 可以从说明书频率推算个体风险；
- 可以生成 CTCAE 分级、红旗、临床 Alert、停药或剂量建议；
- 可以将一个品牌、剂型或适应证的事实外推到另一个产品。

运行时只读取发布后的结构化 JSON/FHIR 产物。PDF、DOCX、网页快照和季度数据包只属于离线证据加工层。

## 中国资料与境外资料的隔离

- 国家卫生健康委指南用于中国诊疗场景、候选产品和候选采集主题，不能替代具体产品现行说明书，也不能直接形成处置规则。
- FDA WEGOVY 标签仅为 `background_comparison_only`，不能作为诺和盈中国说明书。
- CTCAE 仅为候选分级参考，当前不得进入运行时分级。
- FAERS/AEMS 仅用于离线信号研究和数据解析验证，不能计算发生率、证明因果或判断个体风险。
- ICD-11 当前只登记许可文件，不能宣称已导入 ICD-11 分类数据。
- LOINC 用于离线核验观察代码和版本；其 Source 不具备中国临床运行资格，固定代码是否可用于合成数据契约由 Terminology Manifest 单独门禁，代码本身不构成临床解释。

## 许可、保管与再分发

`verification_status=verified` 与“允许公开再分发全文”是两个不同判断。`local_path` 只表示受控工作副本的保管位置，不代表该文件可以进入公开仓库、提交包或产品界面。

当前明确标记为 `license_status=restricted` 的材料包括：

- NCI 生成的 PRO-CTCAE 简体中文定制表；
- 穆峰达一次性预填充注射笔中国说明书；
- 穆峰达多剂量预装笔式注射器中国说明书；
- 度易达中国说明书。

这些受限文件不得再次公开分发，不得放入公开 Git 历史、比赛提交 ZIP、网页静态资源、API 响应或演示下载入口。PRO-CTCAE 完整原文的衍生 Questionnaire/Release JSON 同样按受限资产处理，不能因为从 DOCX 转成 JSON 就视为取得再分发权。允许公开保留的范围应限于官方 URL、版本元数据、SHA-256、必要的短定位锚点和不复现完整受限文本的范围说明。若现有本地资料包或本地编译包包含受限内容，打包对外发布前必须通过排除清单检查。

PRO-CTCAE 条目必须保持批准版本原文，不得改写后继续使用该名称；任何使用都应遵守接受的条款和适用许可。

## 动态网页与可重复性

动态网页可能在相同 URL 下变化，因此必须同时记录 `retrieved_at`、本地快照哈希和页面标题。动态网页可支持当时可见的产品公告事实，但若新版现行说明书已经发布，旧公告不得被解释为完整说明书或最新标签。

每次重建前应执行：

```bash
# 先在所有受控文件中 bump release_id，并把新 Manifest 标为
# draft_candidate；命令中的 ID 必须与该候选一致：
.venv/bin/python scripts/build_cn_glp1_knowledge.py \
  --prepare-release <ISO-8601时间> \
  --candidate-release-id <新的release_id>

# 已发布版本只能核验；生成目录不在 Git 中也不代表可以原地重发：
.venv/bin/python scripts/validate_cn_glp1_knowledge.py
.venv/bin/python scripts/build_cn_glp1_knowledge.py --check
```

完整来源包不在场时，可以使用 `--skip-source-files` 只验证发布结构；这不能替代原始文件哈希核验。
