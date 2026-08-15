# AI Native Doctor Copilot 文档包

本文档包是项目的v0.1设计基线，用于比赛提交和后续开发。它同时包含当前原型事实、目标系统设计和入围后计划，不应将规划能力表述为已实现或已获临床验证。

项目定位：

> ContinuCare Copilot提出一套面向医院的连续照护系统，协助医护在诊后收集、理解和整理院外变化，并在复诊前提供可审阅、可解释、可追溯的连续健康记忆；系统不承担诊断、治疗或用药决策。

## 文档结构

| 文件 | 用途 |
|---|---|
| [00_product_overview.md](00_product_overview.md) | 产品定位、使命愿景、核心边界、竞品差异 |
| [01_prd.md](01_prd.md) | 完整产品需求文档、用户、场景、MVP范围、验收标准 |
| [02_system_architecture.md](02_system_architecture.md) | 系统架构、模块职责、数据流、部署和集成方式 |
| [14_layered_solution_blueprint.md](14_layered_solution_blueprint.md) | 今天确定的六层方案、前后端/Agent边界和端到端工作流 |
| [15_layer_1_acceptance.md](15_layer_1_acceptance.md) | 第一层交付物、验收结果、剩余工程项和医院上线阻断项 |
| [16_layer_2_acceptance.md](16_layer_2_acceptance.md) | 第二层 Care Engine、动态患者端、确定性映射、验收结果和生产缺口 |
| [17_layer_3_acceptance.md](17_layer_3_acceptance.md) | 第三层 Agent 契约、语义确认、Safety Agent、模型接口与验收结果 |
| [releases/layer3_v1.0.0.md](releases/layer3_v1.0.0.md) | 第三层冻结版本、官方 FHIR/MiMo 评测、第四层输入边界与回滚点 |
| [18_layer_4_contract_and_storage.md](18_layer_4_contract_and_storage.md) | 第四层第 1 步：Memory/规则/Summary 合同、FHIR 工作流资源和版本化存储 |
| [19_layer_4_clinical_memory.md](19_layer_4_clinical_memory.md) | 第四层第 2 步：最终资源摄取、证据化 Clinical Memory、Timeline、冲突/缺失和修订链 |
| [20_layer_4_approved_rules_and_tasks.md](20_layer_4_approved_rules_and_tasks.md) | 第四层第 3 步：双审批规则门、逐条件解释、FHIR Task 去重和责任状态机 |
| [21_layer_4_evidence_summary_and_doctor_review.md](21_layer_4_evidence_summary_and_doctor_review.md) | 第四层第 4 步：当前 Timeline 的证据化 Summary、版本链和医生审阅 |
| [22_layer_4_state_snapshot_and_numeric_trends.md](22_layer_4_state_snapshot_and_numeric_trends.md) | 第四层第 5 步：current/stale/unknown/conflict 状态快照与单位一致的原始数值趋势 |
| [23_layer_4_doctor_workbench_read_model.md](23_layer_4_doctor_workbench_read_model.md) | 第四层第 6 步：Doctor Workbench 只读组合查询、历史回放、权限隔离、证据图和故障降级 |
| [24_layer_4_controlled_llm_summary.md](24_layer_4_controlled_llm_summary.md) | 第四层增强项：动态 Fact Ledger、受控 LLM 编排、事实/证据锁与确定性回退 |
| [26_m5_c_deterministic_doctor_brief.md](26_m5_c_deterministic_doctor_brief.md) | M5-C：从人工确认与受控护士处理证据生成确定性、版本化、可追溯的医生复诊前简报 |
| [03_data_model_fhir.md](03_data_model_fhir.md) | FHIR风格数据模型和核心实体关系 |
| [13_fhir_conformance_policy.md](13_fhir_conformance_policy.md) | FHIR R4 合规策略、验证层级和上线门槛 |
| [clinical/glp1_14d_observation_evidence.md](clinical/glp1_14d_observation_evidence.md) | GLP-1 指标、术语映射与权威临床信源 |
| [04_pathway_engine.md](04_pathway_engine.md) | Clinical Pathway Engine、规则、模板、审批和版本管理 |
| [05_ai_agents.md](05_ai_agents.md) | Guideline/Care/Memory/Risk/Summary/Safety Agent设计 |
| [06_safety_and_governance.md](06_safety_and_governance.md) | 医疗安全、证据链、急症流程、人工审批、审计 |
| [07_workbench_and_patient_app.md](07_workbench_and_patient_app.md) | 医生工作台、护士风险中心、患者端体验设计 |
| [08_demo_script.md](08_demo_script.md) | 比赛Demo故事线、页面、演示数据、讲解词 |
| [09_pitch_and_defense.md](09_pitch_and_defense.md) | 开题、创新点、商业价值、答辩PPT和评委问答 |
| [10_roadmap.md](10_roadmap.md) | MVP、比赛版、医院部署版、商业版路线图 |
| [11_wireframes_and_visuals.md](11_wireframes_and_visuals.md) | 系统图表清单、医生端/护士端/患者端低保真线框图 |

## 关键原则

1. AI不诊断、不改药、不决定治疗方案。
2. 医生负责照护路径确认、最终审批和临床决策。
3. 关键风险分级由经临床审批的确定性规则执行，LLM只辅助理解和整理。
4. 所有临床相关输出必须有证据链、置信度和审计记录。
5. 系统围绕 Care Pathway 设计，而不是围绕单个疾病设计。

## 比赛命题对齐

比赛命题强调“贯穿诊前诊后的医生AI Copilot”，具体包括：

- 诊后协助医护开展主动随访。
- 监测异常并进行分级。
- 起草人性化医患沟通。
- 院外数据沉淀后，在复诊前生成医生简报。
- 形成院外连续照护与院内精准决策互相喂养的闭环。

本项目以此为第一版落地方向，并将产品边界收敛为“连续照护记忆与工作流系统”。当前原型使用合成数据和Mock适配器；Aily、飞书和医院系统真实联调属于入围后计划。

## 当前已有图表

文档包已包含 Figma/Figwright 风格的系统架构图、数据模型图、Pathway引擎图、Agent关系图、安全工作流图、时序图、页面信息架构图和中保真线框图。图表资产集中在 [assets](assets)，图表索引和页面规格集中放在 [11_wireframes_and_visuals.md](11_wireframes_and_visuals.md)。
