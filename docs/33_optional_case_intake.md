# Optional Case Intake（可选病例接入）

## 1. 目标与启用方式

Case Intake 是一个独立、默认关闭的合成病例接入能力。它不属于随访
Pathway，不参与患者确认、护士任务或比赛里程碑。

模块读取自己独立的环境开关：

- `disabled`（默认）：不建表、不读取病例、不写入任何内容；
- `manual`：允许医生新建合成病例，并允许医生追加修订版本；
- `hospital_mock`：允许合成医院适配器导入，并允许医生追加修订版本。

`CONTINUCARE_CASE_INTAKE_MODE` 未设置时严格等于 `disabled`。无效值直接报错，
不会退化为隐式启用。当前主应用没有实例化该服务，因此行为保持不变。

显式启用示例：

```python
from continucare.case_intake import CaseIntakeService

case_intake = CaseIntakeService.from_db(
    "data/continucare.db",
    mode="manual",
)
```

只有 `manual` 或 `hospital_mock` 实例化时，模块才创建自己的加法式表。

## 2. 架构边界

数据流如下：

```text
医生手工输入 ───────┐
                    ├─> CaseDraft（统一标准模型）
合成医院适配器 ─────┘          │
                               v
                 不可变 CaseRecord 版本 + 独立审计
```

病例记录的 `pathway_code` / `pathway_version` 只是可选、不透明的引用。Case
Intake 不导入 Pathway registry，不读取或修改 `PathwayDefinition`，也不据此生成
问卷、规则、诊断或计划。

Case Intake 不调用以下既有数据流：

- FHIR `QuestionnaireResponse` / `Observation` 构造与映射；
- `Layer4InputReader` 的患者确认合同；
- Care Agent、care engine、风险规则和护士任务；
- `CompetitionDemoStage` 及现有比赛状态投影；
- 患者端、护士端、医生端现有页面和 navigation。

因此病例不会被表示成“患者本人确认”的事实，也不会推进现有五步演示。

## 3. 标准病例与临床归属

所有来源最终都产生同一个 `CaseRecord`，包含：

- 病例、患者、就诊标识；
- 来源类型、来源系统、外部记录 ID 和稳定幂等键；
- 作者（医生）、来源记录时间、就诊日期；
- 主诉、现病史、既往史、用药史、过敏史、检查摘要；
- 可选 Pathway 代码与版本引用；
- 状态、版本、被替代版本、合成标志和内容哈希；
- 可查询的独立追加式审计事件。

`clinician_assessment` 和 `followup_plan` 不是自由字符串，而是
`ClinicianAuthoredText`：必须带医生标识、带时区时间和固定的
`clinician_manual` 录入方式。模型没有自动诊断、风险等级、处方或治疗建议
字段；严格模型会拒绝未声明字段。

医院 mock 记录中的评估只有在来源记录明确保存了医生手工归属时才能导入。
“由适配器导入”和“由医生写下”分别保存在来源/审计与作者字段中，不混为一谈。

## 4. SQLite、幂等与版本

模块自有两张表：

- `case_intake_case_versions`：每个 `(case_id, version)` 一行，不更新旧版本；
- `case_intake_audit_events`：创建、导入、幂等重放和医生修订的追加式审计。

核心函数 `canonical_hospital_idempotency_key` 以
`source_system + external_record_id` 计算唯一允许的稳定幂等键。`CaseDraft` 和
服务入口都会验证适配器产物，拒绝适配器自定义或伪造键。repository 也只按
`(source_system, external_record_id)` 查找既有医院病例，不信任调用方键。

数据库同时使用两个 hospital_mock 部分唯一索引兜底：

- `(source_system, external_record_id)` 保证同一医院外部记录最多一个原始版本；
- `(source_system, idempotency_key)` 保证规范幂等键最多对应一个原始版本。

导入行为为：

- 同一键、同一规范化内容：返回原病例，不新增病例版本，并记审计重放；
- 同一键、不同内容：抛出 `IdempotencyConflict`，禁止静默覆盖。

医生修订必须提交 `expected_version`。repository 在 `BEGIN IMMEDIATE` 事务中
核对最新版本，追加 `version + 1`，并保存 `supersedes_case_version`。过期版本会
抛出 `ConcurrentCaseVersionError`。医院来源的 v1 保持不变；医生修订的 v2
标记为 manual 来源，通过版本链仍可完整追溯原医院记录。

写入前还会读取主库 `patients.synthetic`。患者不存在或未标记为合成时拒绝写入；
模型本身也只接受 `synthetic=True`。

## 5. 合成 hospital_mock 与未来适配器

`SyntheticHospitalMockAdapter` 只处理进程内合成对象，无网络、凭据、HTTP、
FHIR 或 HL7 连接。未来适配器只需实现 `CaseSource`：

```python
class CaseSource(Protocol):
    source_type: CaseSourceType
    source_system: str
    def iter_cases(self) -> Iterable[CaseDraft]: ...
```

真实医院连接、hybrid 模式、认证、授权、患者匹配、同意管理和真实数据治理都
不在本轮范围内。增加这些能力前必须重新做安全、隐私、医疗数据和运维审查。

## 6. Streamlit 组件与未来薄接线

`continucare.case_intake.ui.render_manual_case_intake` 是可复用的纯文本表单。
它不会自行创建服务、修改导航或读取比赛状态。本轮没有把它接到
`pages/3_doctor_summary.py` 或 `continucare/navigation.py`。

未来可选接线应保持很薄：

1. 医生页从已验证的医生会话构造 `CaseAuthor`；
2. 只在模式为 `manual` 时实例化已启用服务；
3. 将已有患者 ID 和可选 Pathway 引用传给表单组件；
4. 病例列表只调用 Case Intake 自己的读取 API；
5. 不把返回记录交给 Layer 3/4、风险规则或比赛状态机。

若要给 `hospital_mock` 增加导入按钮，应由独立的受控操作调用适配器；不要把
外部载荷直接传给 UI 或主随访存储。

## 7. Demo 重置顺序

“开始一轮合成演示”会原子替换整个 SQLite 文件，因此也会移除已显式创建的
Case Intake 表。本模块不修改 `reset_demo` 或比赛启动逻辑。

正确顺序是：

1. 先开始/重建一轮合成演示；
2. 再显式实例化已启用的 Case Intake；
3. 然后录入或导入病例。

如果已有服务对象跨越了数据库重建，调用 `service.reinitialize()` 重新创建加法式
表后再写入。初始化是幂等的，但不会恢复重置前病例。

## 8. 明确非目标

- 不接入真实医院系统或真实患者数据；
- 不实现 HTTP、FHIR、HL7 网络连接；
- 不上传图片，不做病例拍照识别或 OCR；
- 不生成诊断、风险等级、处方或治疗建议；
- 不修改 Pathway、患者确认资源、护士任务或比赛状态；
- 不修改现有医生页面和导航。
