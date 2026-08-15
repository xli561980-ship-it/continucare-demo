# 本地 Demo 安全边界

- 数据：只 Seed `P-DEMO-001 / 陈女士（合成）`，运行数据库被 `.gitignore` 排除。
- FHIR：临床数据边界固定为 R4 4.0.1；完整资源持久化，搜索列只是投影。
- 追溯：患者原文进入 QuestionnaireResponse；Observation 通过 `derivedFrom` 指回原回答；字符位置和抽取置信度单独保存。
- 临床规则：当前 `clinical_rules=[]`。所有输入返回 `not_assessed`，不产生 L0–L4、报警、SLA 或处置建议。
- 摘要：每条内容必须有 `evidence_refs`；推断内容只能进入“医生待确认”。
- 外部系统：飞书/Aily 默认为 Mock、Bitable 默认为 disabled；可选适配器仅完成 FakeTransport 合同测试，真实租户验证与生产可用性均为否。
- 外部启用：偶然存在凭据不生效；只有 `test_tenant` + 对应 flag + 全局 egress flag + 完整配置才可创建 client。页面读取状态不会认证、探活或联网。
- 外部写入：Bot timeout 为 `outcome_unknown` 且不盲重试；Bitable 不参与本地事务或临床进度；manual-review `SEND_ENABLED=False` 不变。
- EMR：医生审阅不会默认写入 EMR。
- 上线门槛：具体药物、人群、医院 Profile、术语版本和临床规则须分别完成审批与验证。
