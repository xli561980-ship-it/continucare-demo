# HANDOFF

> 给完全没有上下文的新会话使用。先完整阅读根目录 `AGENTS.md`，再阅读本文件。**最新权威状态：2026-08-15，最终独立全仓审核、完整运行验证、Claude Opus 高风险复核及其修复切片均已收口；其后用户明确要求把患者端、护士端和医生端拆成独立入口，并指出旧版文字与展示仍不像合格比赛 Demo。当前工作区已完成 `ROLE-1` 独立入口和 `DEMO-UI-2` 比赛展示重构，但尚未 commit / push。当前比赛级合成原型结论为 PASS，无未解决代码级 blocker。** 最新已提交审核基线见下方 `AUD-R4`；最新未提交切片见 `DEMO-UI-2` 与 `ROLE-1`。此前 B-01/B-02、A++ UI-1 至 UI-6 和 Knowledge v2 alias readiness 的历史收口仍然有效；分别见 `AUD-R2`、`UI-6` 与 `K2`。后续不得按旧历史中“A++ 尚未实施”“Knowledge v2 尚未合入”“最终审核修复尚未完成”“三端仍无独立入口”或“首页仍应以工程验收台为主”的状态执行。

## DEMO-UI-2. 比赛展示型三端重构（2026-08-15，已实现，未提交）

- 用户明确指出旧页面虽然能证明工程边界，但具体文字和首屏内容不像合格比赛 Demo。本切片不改业务状态机、持久化事实、患者确认门或临床安全边界，只重做首页与三端的信息故事、动作命名和视觉层级。
- 首页主张改为“让院外一句话，变成复诊前可追溯的记录”，首屏先展示患者端 → 护士端 → 医生端价值接力；动态进度改为“一句原话 → 本人确认 → 人工核对 → 复诊速览 → 完整留痕”。技术配置、负向路径和数据管理继续保留在次级折叠区。
- 患者端改为移动优先的对话确认：问题“今天身体怎么样？”，患者原话“我今天拉肚子。”，待确认记录“今天有腹泻”，以及“对，就是这个意思 / 我还不确定 / 不是这个意思”三个大按钮；确认前明确不写入随访记录，确认后不再重复询问。
- 护士端改名“随访待办”，首屏固定并排展示“患者原话 / 已确认记录 / 来源”，来源只写已持久化的“患者本人确认”；任务状态使用“待人工核对”，不出现风险分级或 Alert；唯一主动作位于首屏，沟通文字明确标记“未发送”。
- 医生端改名“复诊准备”，首屏使用“30 秒速览”分开显示本轮患者确认、护理接力和复诊时需要补充的信息；右侧/移动下方显示患者原话 → 本人确认 → 护士核对证据链。它仍明确写“尚无临床评估，需进一步询问”，不生成诊断、治疗建议或风险等级。
- 视觉采用代码原生的深青、暖白、轻边框与大留白；生成的概念图只作为设计规范，没有作为图片资产嵌入页面，也没有引入新依赖、头像素材、虚假指标或外部能力声明。
- 主要修改：`app.py`、`continucare/ui.py`、三张角色页、对应 UI/比赛测试、`README.md` 与本文件。受保护的未跟踪 `docs/ui_product_design_brief_2026-08-13.md` 未修改。
- 验证结果：患者/护士/医生/比赛 Demo 定向 `118 passed`；固定哈希的官方 FHIR R4 schema 下全量 `933 passed, 0 skipped`；`compileall` 通过；离线语义评测 8/8 通过；连续离线彩排 3/3 通过。Browser 用隔离 `/tmp` 数据库真实走通“开始演示 → 患者确认 → 护士接手/核对/保存结果 → 医生生成速览”，桌面和 `390×844` 均无横向溢出，最终页面 console 为 `0 error / 0 warning`。
- 本节取代历史 `UI-0.3` 中“患者说的话，一路跟到复诊速览”的首页主文案以及旧的“护士工作台 / 复诊速览”可见标题；历史章节仍保留用于解释安全边界与状态机决策，不应恢复旧首屏。

## ROLE-1. 患者端 / 护士端 / 医生端独立入口（2026-08-15，已实现，未提交）

- 用户明确要求将医生端、患者端、护士端分开。本切片采用一个 Streamlit 服务、一个共享 SQLite 合成记录、三个固定角色 URL 和一个演示控制台；没有复制业务逻辑或创建三套数据。
- 新入口文件为 `streamlit_app.py`，使用隐藏的 `st.navigation` 注册全部页面；默认控制台为 `/`，患者端为 `/patient`，护士端为 `/nurse`，医生端为 `/doctor`，记录追溯和 Knowledge 分别为 `/records`、`/knowledge`。
- 演示控制台新增三张角色入口卡，可以把三端分别放在不同标签页或演示设备上。既有角色间按钮仅承担演示接力，页面仍明确声明角色拆分不等于登录、身份认证或权限控制。
- 新增 `continucare/navigation.py` 作为页面源文件、标题、图标和 URL 的单一合同；README 的所有启动命令已切换为 `.venv/bin/streamlit run streamlit_app.py`。
- 验证结果：导航定向 `45 passed`；官方 FHIR schema 下全量 `933 passed, 0 skipped`；`compileall` 通过；离线语义评测 8/8 通过；连续离线彩排 3/3 通过。
- Browser 实测 `/`、`/patient`、`/nurse`、`/doctor` 均加载正确页面标题和独立工作区，控制台三入口可点击；桌面及 `390×844` 无横向溢出，console 为 `0 error / 0 warning`。三端读取到同一条本地合成故事事实。
- 本切片没有真实患者、真实身份系统、外部 API、消息发送、飞书/Aily/Bitable 联调、EMR 写入或部署；没有修改受保护的未跟踪 `docs/ui_product_design_brief_2026-08-13.md`。

## AUD-R4. 最终独立全仓审核与修复收口（2026-08-15，已完成）

### AUD-R4.1 结论与提交

- 最终审核以 `0f2b9d7cf10fff8c7bf1686235228e91a889245b` 为只读基线，完成全仓代码检查、全量测试、真实 Browser 工作流验证和 Claude Opus 高风险只读复核；随后用户在同一任务中明确授权修复与推送。
- 事务、并发和安全重放修复提交：`f2dfe393fb2d2d60a79f2c22d9878098fa2522bf`，`fix(persistence): make care workflows atomic and replay-safe`。
- 安全信任边界、脚本入口和 FHIR 验收说明提交：`e228d4e460468e86c149661b6ce186fb73a69474`，`fix(security): harden trust boundaries and validation tools`。
- 本节所在提交只负责更新交接事实，不包含新的产品能力。当前无后续默认代码切片；用户计划于次日先与队友讨论正式参赛方案，再决定文档、截图、视频或飞书能力工作。

### AUD-R4.2 已修复问题

- `SQLiteStore` 将本轮识别的 AgentRun、患者决定、上下文材料与必需审计事实纳入原子命令；冲突或部分历史重放 fail closed，提交后响应丢失可安全返回既有结果，不重复写入。
- Layer 4 将本轮识别的 Memory revision、Task/Rule、State、Summary 与对应 Provenance/版本记录改为原子 bundle，补齐事务内 CAS、故障注入回滚和 post-commit replay。
- MiMo 凭据请求禁止 HTTP redirect，避免 `Authorization` 被转发到重定向目标；3xx 以稳定 `ModelRequestError` 拒绝。
- Knowledge review 对 `evidence_candidate_v2` 的 digest 信任必须从 append-only ledger 精确回放 SourceCandidate 与 SourceSnapshot lineage，并重新执行 synthetic/sensitive-data 一致性检查。
- `evaluate_semantic_layer.py`、`rehearse_demo.py` 和 `validate_fhir_r4.py` 可在无 editable import path 的隔离 Python 入口中运行；已增加入口回归测试。
- README 现明确区分普通离线测试的 3 个条件 skip 与最终验收，固定官方 FHIR R4 schema SHA-256，并要求先验哈希、再执行零 skip 全量测试和独立校验器。

### AUD-R4.3 最终验证证据

```text
官方 FHIR R4 schema SHA-256:
75e5560da3cf503895a44c8ca7af17a83b4cca6c2cb5ba1883d2aec0d1cb5ac6

全量 pytest（配置官方 schema）: 932 passed, 0 skipped
compileall（continucare/app.py/pages/scripts）: 通过
离线 Layer 3 semantic evaluation: 全部用例 passed
连续离线 demo rehearsal: 3/3 passed
官方 FHIR 独立校验器: 7 类示例资源全部 valid
git diff --check / staged diff check: 通过
```

- 修复后的 Browser 核心故事、异常/重放路径和桌面/移动端检查均通过；没有真实患者、真实消息发送、真实飞书/Aily/Bitable、EMR 写入或部署。
- Claude Opus 最终高风险复核没有提出剩余代码级 BLOCKER。Codex 最终裁决：只有会造成错误验收、错误能力声明、安全/隐私/数据完整性失真的关键文档—代码矛盾才是 blocker；单纯缺少医院生产化能力属于已披露限制，不阻止比赛级合成原型交付。
- 当前结论仅是 **competition-grade synthetic engineering prototype PASS**，不是临床验证、医院试点、生产可用、医疗器械认证或已量化业务收益。

### AUD-R4.4 数据、工作区与下一步边界

- `data/continucare.db` 保持 SHA-256 `0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585`、size `311296`、mtime epoch `1786644509`；无 journal/WAL/SHM。
- `docs/ui_product_design_brief_2026-08-13.md` 继续保持受保护、未跟踪、未修改、未暂存，SHA-256 `e9e03bde1051f43ec8dbca2695716a70588b448e47b37e434015934121be03a6`。
- 官方 FHIR schema 只存在本机临时路径 `/tmp/fhir-r4-schema.zip`，不进入 Git；临时文件消失后必须按 README 重新下载并核对固定哈希。
- 官方 40 强模板要求的企业名称/命题原名、队名、成员分工和真实飞书能力仍需用户与队友确认。当前仓库只有飞书/Aily/Bitable 协议与 FakeTransport 证据，不得写成真实租户已联调。
- 在用户和队友完成讨论前，不默认实施真实外部集成、公开部署、医院/患者数据、临床规则、Knowledge alias UI consumer 或其他生产化工作。

## AUD-R2. 独立全项目审核 blocker 收口（2026-08-15，已完成）

- 独立全项目审核最初结论为 **BLOCKER**，包含 B-01 审计 Provenance 归属错误和
  B-02 Knowledge 历史文档与当前能力状态矛盾两项 blocker。
- B-01 已由 commit `ccbaac4d722039d1fcd95ad5341acdf3d7fc3819` 修复。验证结果为：
  Audit targeted `40 passed`；业务回归 `115 passed, 1 skipped`；UI targeted
  `243 passed`；全量 `906 passed, 3 skipped`；in-app Browser 真实完成
  `0/9 → story_complete`；12 条事件的 Provenance 均精确归属。
- B-02 已通过在
  [`docs/28_knowledge_ops_p0_p1_p2_readiness.md`](docs/28_knowledge_ops_p0_p1_p2_readiness.md)
  顶部明确标识历史快照，以及将当前能力权威路由到 docs/30、docs/31、docs/32
  和主线集成报告完成修复。两个 blocker 均已处理。
- README 页面名称陈旧这一 non-blocking 已在 `AUD-R3` 文档真实性切片处理。
- 剩余两项 non-blocking 已由 UI-H1 代码 commit
  `569c40851f4598182129dd4840d3ff7aec093b0f` 收口：显式重新开始会精确清理
  patient / nurse / doctor 当前浏览器状态并保留 Knowledge 与无关状态；Disclosure
  折叠态均有唯一 hidden inert target，展开态均绑定唯一真实可见内容 target，护士来源
  与记录使用独立稳定 ID，未知 query fail closed。
- UI-H1 最终验证为 Knowledge targeted `12 passed`、UI targeted `248 passed`、
  全量 `911 passed, 3 skipped`，三个 skip 仍仅因未配置 `FHIR_R4_SCHEMA_ZIP`；
  `compileall` 与 `git diff --check` 通过。此前 Browser 已验证 session reset 与
  Nurse / Doctor / Audit；本次单独复验 Knowledge 折叠、展开、再次折叠和 unknown query，
  `1280×720` / `390×844` 均无 console error/warn、overlay 或横向溢出，展开 target
  locator 为 `visible=true`。工作区数据库与受保护 brief 均未变化。
- v2 alias UI consumer integration 仍未实施。后续队友应先独立观察和提出意见，
  不直接修改代码。

## K2. Knowledge v2 主线集成最新权威交接（2026-08-14，已完成）

### K2.1 合入与精确提交

- 目标分支：`codex/docs-collaboration-init`；
- 合入前主线：`f315a6b6bf69d6c01927c53115a266d144ed09ab`；
- Knowledge 源分支 tip：`0110c319a58e2de7baa92e83788a56113bc62a0c`；
- merge commit：`88c85b3fb103e13f0770f385f01a0f1916a135ff`；
- merge parents 依次为上述主线基线与 Knowledge tip；
- 使用普通 `git merge --no-ff --no-commit`，保留 Knowledge 的 38 个历史提交；没有 squash、rebase、cherry-pick、force push 或历史改写；
- 合入精确为 66 个新增 Knowledge/terminology/manifest/文档/fixture/测试文件，`22,859 insertions`，零删除、零现有 UI 文件覆盖。

### K2.2 完整验证

```text
Knowledge targeted: 448 passed
UI targeted: 228 passed
关键业务回归: 35 passed
全量: 891 passed, 3 skipped
compileall: 通过
cached/worktree diff check: 通过
```

三个 skip 仍且仅因未配置官方 `FHIR_R4_SCHEMA_ZIP`，没有新增 skip。in-app Browser 使用隔离数据库完成六页 `1280×720` / `390×844` 冷加载和真实 `0/9 → story_complete`；console error/warn=0，无水平溢出或错误 overlay，页面资源仅来自 localhost。标准终态保持 QR 1、Observation 1、Task 历史 5、Communication 历史 2、Summary 历史 2、AuditEvent 12、Alert 0、approved ClinicalRule 0、sent/received Communication 0，`SEND_ENABLED=False`。

### K2.3 数据与工作区保护

- 工作区 `data/continucare.db` 前后保持 SHA-256 `0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585`、size `311296`、mtime epoch `1786644509`，17 张表行数逐项不变，且无 journal/WAL/SHM；
- 受保护 brief `docs/ui_product_design_brief_2026-08-13.md` 保持未跟踪、未修改、未暂存，SHA-256 仍为 `e9e03bde1051f43ec8dbca2695716a70588b448e47b37e434015934121be03a6`；
- Knowledge 终态浏览前后隔离 Browser 数据库 SHA-256/size/mtime 完全不变，证明现有 Knowledge 页仍是离线只读展示。

### K2.4 当前能力边界与后续

- `knowledge_effect=informational_only`，`runtime_authority=none`；
- 不诊断、不治疗、不分诊、不做自动临床决策；
- P1b 仍为 `not_attempted`，rights/live/socket/alias Gap 仍 fail closed；
- 没有真实患者数据、真实来源采集、真实网络请求、正式 reviewer、正式 rights decision 或正式 KnowledgeRelease；
- `approved_match_aliases` 仍为空，`consumer_integration_ready=false`；
- 当前 A++ Knowledge UI 没有导入 v2 alias API，仍展示既有四主题离线 bundle；
- 本次没有实施 UI v2 consumer 适配。任何 successor manifest/DTO、正式 alias 解除或 UI 适配都必须单独授权并独立审核；
- 后续还会由队友完成一次独立验证；本次没有调用 Claude、Sonnet、Opus、ImageGen 或子 Agent。

## UI-6. A++ UI Full Validation 最新权威交接（2026-08-14，已完成）

### UI-6.1 结论与精确基线

- UI-1 至 UI-6 已完成；Full Validation 结论为 **PASS，无未解决 UI blocker**；
- 最终测试代码基线：`cad99d98f5cc13947d6075d62a628e6fc410d873`，`fix(ui): resolve full-validation regressions`；
- 该代码 commit 已普通 push，父基线是 UI-5 `1ef0cefb9d8d387a2744c7f164cd79a04cdb59e1`；
- 完整报告：[`docs/ui_a_plus_plus_full_validation_report_2026-08-14.md`](docs/ui_a_plus_plus_full_validation_report_2026-08-14.md)；
- A++ 是当前已验收 UI 基线，但仍可在未来真实用户测试后继续优化；任何后续修改需要用户单独授权；
- 当前仍是 Streamlit 合成产品原型，不是已发布原生 App、临床试点或生产系统。

### UI-6.2 六页与验收范围

以下六页均已完成：

1. 合成演示导览 `/`；
2. 我的随访 `/patient_followup`；
3. 护士工作台 `/nurse_risk_center`；
4. 复诊速览 `/doctor_summary`；
5. 记录追溯 `/audit_log`；
6. Knowledge 资料库 `/knowledge_evidence`。

验收已覆盖：

- 真实 UI 正向路径从 0/9 到 `story_complete`；
- candidate ready/unsure/rejected、Task 四种异常终态、stale/integrity/generation conflict、DoctorReview 三种决定和 Knowledge 边界；
- 全部合法状态自动投影，以及 15 个可自然持久化/缺失状态的 Browser 首页、当前角色页和审计页矩阵；
- 六页 `1280×720` 桌面与 `390×844` 移动端；
- page identity、DOM、overlay、console、水平溢出、触控目标、ARIA、焦点样式、对比度和 reduced-motion CSS；
- 两张 accepted A++ concept 与最终截图的 fidelity 对照；
- 中文、临床边界、Mock/未发送说明和数据库只读不变性。

### UI-6.3 本轮最小修复

验收发现并修复两个明确 UI 问题：

1. 护士、医生、审计和 Knowledge 的自定义披露入口缺少稳定 `aria-expanded` / `aria-controls`；现使用同源原生状态链接、明确 ARIA、44px 高度、焦点样式和可恢复 query state；
2. 医生桌面窄来源栏三项横排导致中文逐字换行；现仅该来源栏改为纵向排列。

没有修改 service、FHIR 合同、数据库 schema、Knowledge manifest、terminology、配置或依赖。

### UI-6.4 最终验证

```text
UI targeted: 228 passed
关键业务回归: 35 passed
全量: 534 passed, 3 skipped
compileall: 通过
git diff --check: 通过
```

三个 skip 仍仅因没有配置官方 `FHIR_R4_SCHEMA_ZIP`，没有新增 skip。

标准 happy path 最终保持：QuestionnaireResponse 1、Observation 1、Task 历史版本 5、Communication 历史版本 2、Summary 历史版本 2、AuditEvent 12、Alert 0、approved ClinicalRule 0、sent/received Communication 0、`SEND_ENABLED=False`、`story_complete` / 9/9。

### UI-6.5 安全、数据与工具边界

- 无真实患者数据、无模型调用、无真实发送、无真实外部系统写入、无部署；
- 工作区 `data/continucare.db` 在验收前后保持 SHA-256 `0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585`、size `311296`、mtime `1786644509`，且无 journal/WAL/SHM；
- `docs/ui_product_design_brief_2026-08-13.md` 仍是受保护未跟踪输入，SHA-256 `e9e03bde1051f43ec8dbca2695716a70588b448e47b37e434015934121be03a6`，不得自动暂存或修改；
- UI-6 验收当时主线仍只有既有离线 Knowledge bundle；后续 K2 已将 `codex/knowledge-v2-alias-readiness` tip `0110c319a58e2de7baa92e83788a56113bc62a0c` 合入。当前 UI 仍未导入 v2 alias API，以上历史状态不得用来否定 K2 合入事实；
- 本轮使用 frontend testing/debugging skill 与 in-app Browser；没有使用 ImageGen，没有调用 Claude、Sonnet、Opus 或任何子 Agent；
- IAB 合成键盘事件不能作为真实键盘/屏幕阅读器结果；已验证原生语义、可聚焦性、ARIA、焦点 CSS 和真实点击状态。未运行 VoiceOver/NVDA，也未做 OS 级 reduced-motion 模拟；这些限制已在报告明确记录。

### UI-6.6 后续动作

A++ UI 当前没有待执行的默认代码切片。Knowledge v2/ops 合入已由 K2 完成；任何进一步 UI 优化、v2 alias consumer 适配、真实集成、部署、生产化或临床能力工作，都必须由用户另行明确授权。下面 `UI-0A`、`UI-0` 及其他旧“下一步”只保留历史，不得覆盖本节或 K2。

## UI-0A. A++ 暂定执行规格当前交接（2026-08-14，文档收口；UI-1 已获后续授权）

### UI-0A.1 当前状态与唯一权威方案

- 完整可实施规格已写入 [`docs/ui_a_plus_plus_provisional_execution_spec_2026-08-14.md`](docs/ui_a_plus_plus_provisional_execution_spec_2026-08-14.md)；
- 文档状态是 **Provisional Freeze / 暂定执行稿**：允许未来先做出真实可操作版本再根据效果调整，不是永久视觉定稿；
- 历史事实：规格写成时，用户明确要求先暂停实施、完成 Markdown；截至本次 UI-0A 文档收口，仍未修改任何 UI 代码、业务逻辑、测试或配置；
- 后续授权：用户现已明确授权，在本次 UI-0A 文档 commit 并普通 push 后，按 A++ 规格以独立切片开始 UI 实施；
- 下一个代码切片是 **UI-1**，不属于本次文档切片；UI-1 必须以本次文档提交 push 后的精确 commit SHA 为新基线，并先确认 HEAD 与 upstream 一致、behind/ahead=`0/0`；
- 每个 UI 切片只有在该切片自检和审查均无未解决 blocker 后，才允许精确暂存和 commit 该切片并普通 push；
- 授权不包括部署、真实患者数据、真实外部系统、临床能力或其他超出 A++ 规格的工作。

### UI-0A.2 相对旧 A+ 的关键修订

1. 核心规则收敛为：**默认只显示做出正确动作所需的最小事实；来源一跳可达；边界与后果永不折叠**；
2. 六个代码页面仍保留，但不再设计为每个角色都能看到的“六页菜单”；患者、护士、医生和审核者各自只看到本角色入口；
3. 演示者使用独立五步导览：患者表达 → 患者确认 → 护士核对 → 医生速览 → 记录追溯；第一、二步共用患者页，Knowledge 不参与故事；
4. Knowledge 从旧方案的医护核心导航和患者上下文入口中移出，保留为独立只读资料库，不携带患者上下文、不自动预选、不参与状态；
5. 患者全部拒绝会结束本轮，这一后果在决定按钮前不可折叠；`candidate_unsure` 仍可由患者接受或拒绝；
6. 护士队列使用“例行记录核对”，声明按提交时间排序，不显示风险优先级、SLA 紧急色或 Alert 指标墙；
7. 医生第一屏固定分开患者事实、护理动作和“尚未提供临床评估”；DoctorReview 只以措辞决定呈现，不包装成临床签署；
8. 终态分为正常完成、业务停止和记录错误三种表达；原因缺失时显示“原因：未记录”，不得补造原因；
9. Mock/未发送/未写入必须就地标明；重新开始仅在演示者的数据管理区二次确认，并明确会替换整份本地合成演示数据；
10. 投资者叙事保持诚实：当前是寻找设计合作方的合成产品原型，不宣称临床试点、ROI、数据护城河或真实集成。

### UI-0A.3 规格覆盖范围

新规格已包含：

- 产品定位、核心亮点、事实边界和角色优先级；
- 六个页面与五步演示故事的关系；
- 四层渐进披露和每类角色的默认/展开/隐藏内容；
- happy path、全部拒绝和 unsure 路径；
- 全局状态词表、三类终态、empty/loading/error；
- 六页逐屏结构、关键中文文案、动作与后果；
- 视觉 token、排版、组件、响应式和无障碍；
- Claude Opus 建议与 Codex 独立取舍；
- 市场模式、投资者/战略校正、未来价值指标；
- UI-1 至 UI-6 实施切片和完整验收标准。

### UI-0A.4 当前工作区保护

- 本次 UI-0A 文档收口只允许提交 `HANDOFF.md` 与 `docs/ui_a_plus_plus_provisional_execution_spec_2026-08-14.md`；
- `docs/ui_product_design_brief_2026-08-13.md` 仍是受保护的未跟踪规划输入，保持未修改、未暂存；
- 本次切片不包含 UI-1，也不修改代码、测试、配置、数据库或生成图；
- 旧的 `UI-0` A+ 内容保留为研究历史，但已由本节和 A++ 规格取代，不得据其冲突项开始实施。

### UI-0A.5 A++ 唯一概念板实施参考

A++ 的视觉实施与 fidelity 对照只参考 A++ 规格第 18 节的两张概念板：

1. 角色化渐进披露：
   `/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-5772d17f-07c0-4b3e-a794-92d053037c0d.png`
2. 演示者 60 秒正向 + 20 秒负向路径：
   `/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-146b2274-0937-4b74-b5dc-1e7002af6be9.png`

两张图是暂定概念参考，不是静态 UI 资产；若图中示例与 A++ 规格或真实仓库事实冲突，以规格和仓库事实为准。

## UI-0. UI 产品策略与 A+ 概念方向历史交接（2026-08-13，已由 UI-0A 取代）

> **历史状态说明：** 本节只保留 A+ 的研究过程。A+ 已由 A++ 暂定执行规格取代，不构成当前实施授权、产品决策或视觉 fidelity 基准。

### UI-0.1 当前阶段与授权边界

- 本轮已完成当前六页 UI 的桌面与 `390×844` 移动端只读检查、当前可核验产品案例研究、Codex 产品初稿、Claude Opus 产品策略审查、双模型拟人任务测试和概念方向生成；没有重新做全仓审核；
- 用户已选择继续优化方案 A，并明确表示此前“仍需用户决定”的项目全部采用 Codex 推荐项；当前候选已收敛为 **A+「原话接力」**；
- A+ 仍是**待冻结候选**，不是已批准方案。用户回来看完概念板后，需要明确说“冻结 A+”或继续提出调整；
- 在用户明确冻结并另行授权实施前，不修改 UI 代码、不接真实飞书/Aily/Bitable、不使用真实患者数据、不开始 UI 实施；
- 本轮按用户后续明确要求只更新了本 `HANDOFF.md`。没有修改代码、UI brief 或其他项目文档，没有 add、commit 或 push；
- 本轮没有代码 diff，因此没有调用 Sonnet。

### UI-0.2 已采用的产品决策

1. 继续方案 A，并吸收方案 B 的记录连续性与来源表达优点；
2. 患者入口使用“我的随访”；
3. 首页并列两个产品空间入口：“我的随访”与“医护工作台”；
4. Knowledge 属于医护工作台一级导航，并允许从相关事实进入；不作为患者端入口；
5. `story_complete` 与 rejected/cancelled/failed/entered-in-error 等异常终态统一展示角色化只读结果、终止原因、“已经产生 / 没有产生”和“查看完整记录”，不得继续提供业务动作；
6. 保留真实的医生 Summary 版本决定能力，但用户可见语言改为“这版速览的措辞”，动作使用“保留这版速览 / 调整速览措辞 / 不采用这版速览”；不得包装成临床审阅、批准、签署或定稿；
7. 患者的接受/不确定/拒绝以及医生的三种措辞决定均为等权选择，不预设系统推荐答案；只有已经选择的“不太确定”使用轻量琥珀色表示当前状态。

### UI-0.3 A+ 产品主张与亮点层级

建议的一句话产品定位：

```text
ContinuCare 是一个让患者表述在患者、护士、医生和审核者之间保持可理解、可追溯且不越过临床边界的合成诊后随访原型。
```

首页主文案：

```text
患者说的话，一路跟到复诊速览。
```

A+ 不再同时宣传多个平级亮点，而是收敛为一个核心、两个支撑和一条固定规则：

- **核心亮点——原话锚点**：每一条被交接的内容，都能在当前位置回到患者说过的原话；
- **支撑一——断点解释**：流程停在哪里，先用人能读懂的中文说明原因，再展示记录依据；
- **支撑二——措辞可调、来源不动**：医生可以改变速览的文字表达，但仍指向同一批患者确认和护理动作来源；
- **固定规则——边界始终可见**：`not_assessed`、没有真实发送、没有诊断/风险分级/治疗建议不是营销亮点，而是全局不变的可信边界。

“原话锚点”是内部设计机制；患者界面禁止使用“证据、取证、核验、存证”等法律化或技术化词语。按角色使用“您说的原话”“我们记成了”“来自本轮患者确认”“护士已核对”“当前速览措辞”等事实性标签。只有记录追溯页在真实 Provenance 对应位置可以使用“来源/追溯”概念。

### UI-0.4 方案 A 与 B 的合并边界

保留 A：

- “我的随访 / 医护工作台”两个清晰产品空间；
- 患者移动优先、医护桌面优先；
- 每个角色只先看到当前任务、下一步和能力边界；
- 低认知负担、克制的状态反馈和中文原生表达。

吸收 B：

- 患者原话使用仅限原话位置的宋体式字形；
- 横向记录行、细分隔线、窄来源栏与当前位置展开；
- 患者事实、护理动作和当前措辞之间保持连续来源关系；
- 当前措辞与上一版措辞的轻量关系；
- 审计页将“事实—动作—停止点”连接起来。

明确不吸收 B：

- 编号章节、拟物纸张和高密度文书布局；
- 7:3 常驻宽侧栏、大型时间线和大量卡片；
- 并排 diff、箭头版本对比、红绿修订色；
- 批注、签署、批准、定稿、归档等文档工具隐喻；
- 角色页面中的英文事件名、JSON、技术 ID、排序和日志控制台。

### UI-0.5 六页目标结构

- **首页**：等权显示“我的随访 / 医护工作台”；进行中显示“现在轮到谁：下一动作”，终态显示“本轮已结束：结论”；“这一轮留下什么”只用三条开放记录行，不显示进度百分比、风险指标或 9/9 庆祝；
- **患者页**：`390×844` 移动优先；先显示患者原话，再显示“我们记成了”；接受/不确定/拒绝三个动作等权；`candidate_unsure` 后仍可接受或拒绝；全部拒绝后明确本轮结束，不能在同一轮立即重新表述；
- **护士工作台**：任务列表存在多条时显示队列，当前合成 Demo 只有一条时直接进入详情；一条记录最多展开一个来源；真实动作按 Task 状态映射，不能把人工核对 Task 表达成风险 Alert；
- **医生速览**：第一屏先分开“患者确认的表述 / 护理动作 / 当前未评估边界”；措辞决定只针对当前单个合格 Summary 版本，不设计成无限版本编辑器；上一版只在同页轻量展开，不并排比较；
- **记录追溯**：第一层用中文句子解释发生了什么和为什么停止；第二层展示操作者、时间和影响；事件标识、资源标识和原始数据只在第三层技术详情展开；不做默认日志表、筛选器或装饰性动画时间线；
- **Knowledge**：独立只读，不参与故事状态；只展示仓库现有的腹泻、恶心、呕吐、腹痛内置知识主题。上下文入口不写入患者记录、不产生已读状态，并固定显示“这里只说明采集依据，没有对这位患者做过评估。”；知识来源标签不得与患者来源标签混用。

桌面来源区域建议约 `180px`，每次最多展开一项，展开内容占整行且不引起主体列重排。移动端把来源放在内容下方，不横向滚动、不弹全屏来源 Modal；关键触控目标至少 `44px`。

### UI-0.6 终态与中文语言冻结候选

所有成功和异常终态共用同一五段结构，不用绿色庆祝或更重的成功视觉：

1. 人能读懂的结论；
2. 停止或完成原因；
3. “已经产生”；
4. “没有产生”；
5. “查看完整记录”以及“尚未提供临床评估 / 没有发送任何消息”的固定边界。

关键候选文案：

```text
story_complete：演示记录链已走完
原因：合成演示 9/9

全部候选拒绝：这一轮到这里结束，我们不会再改这段记录。

没有可靠具体原因时：记录在这里停止；系统没有记录更具体的停止原因。

明确重新开始：将替换整份本地合成演示数据，当前这轮记录会消失。仅影响本地演示数据。
```

医生动作下固定显示：

```text
以上只调整速览的文字表达，不等于临床评估。
```

“不采用这版速览”旁显示：

```text
不采用只影响这段速览文字，不改变患者确认的记录。
```

### UI-0.7 Claude Opus 实际审查

- 用户明确要求继续与 Claude 优化后，Codex 实际调用了 Claude Opus，使用最强可用 `max` 推理档；命令退出码为 `0`；
- Claude 只收到 Codex 整理的 Review Packet，关闭工具和会话持久化，从临时目录运行，没有从仓库根目录扫描；
- Claude 的参与是真实策略审查，不是虚构，也没有让 Claude 修改任何工作区文件；
- Claude 的四项 blocker 已吸收：外显“证据边注”会造成法律/技术误读；医生并排版本 diff 会造成临床签署误读；`story_complete` 缺少统一终态结构；首页没有定义终态时的当前接力；
- 其他已采纳意见包括：来源栏缩窄而非 7:3、角色页最多两层、Knowledge 来源样式与患者来源区分、审计第一层禁止英文日志、宋体只用于患者原话、内部“记录带/轨道/章节”名称不对用户展示；
- Codex 独立核验后确定：患者是整组确认，不能宣称每条逐条签署；DoctorReview 是对单个合格 Summary 版本的一次决定，不是反复文档编辑；当前 Demo 可能有一条护士 Task，但架构允许队列多条；审计页没有真实权限体系；Knowledge 上下文入口是目标概念而非当前已实现行为；终态缺少具体原因时必须使用上述回退句，不能虚构原因；
- Claude 推荐的最终亮点排序为：原话锚点（核心）→ 断点解释（机制）→ 措辞可调、来源不动（支撑）→ 固定能力边界（产品规则）。Codex 同意该收敛。

### UI-0.8 概念板（A+ 历史探索图；非实施基准）

以下三张图仅作为 **A+ 历史探索图** 保存在 Codex 生成图目录，**不在仓库内，不是项目静态资产，也不是已批准 UI**。它们已由 `UI-0A.5` 和 A++ 规格第 18 节列出的两张 A++ 概念板取代，**不能作为后续视觉 fidelity 的实施基准**。未来控件、文字、表格和导航必须 code-native 实现：

1. 首页、患者正常/不确定/全部拒绝：
   `/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-215380ae-6ca6-40f0-978b-cbda348d3c84.png`
2. 护士、医生、空状态与处理中：
   `/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-ec878e1e-0ad0-4783-8b57-b885b3a59b76.png`
3. 记录追溯、Knowledge 与统一终态：
   `/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-516be65c-966b-4f5e-a2be-7002c0ff1203.png`

这些 A+ 历史概念图当时做过两类事实修订：删除依靠人物/状态图标制造成熟感的装饰；删除 ImageGen 擅自生成的“临床护理指南”等仓库不支持的来源内容。第一、二张还将患者和医生的三项并列决定改为等权，避免诱导接受或默认保留。

### UI-0.9 当前 Git、数据与下一步

本次 `HANDOFF.md` 更新前的只读基线：

```text
branch: codex/docs-collaboration-init
HEAD: b025a724c0d4ef760b4e847521a19f6dcdf8eb8e
upstream: b025a724c0d4ef760b4e847521a19f6dcdf8eb8e
behind/ahead: 0/0
protected untracked input: docs/ui_product_design_brief_2026-08-13.md
data/continucare.db SHA-256: 0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585
```

当时应用未运行，也没有点击创建、处理、重置或其他会改变数据库的业务动作；当时完成该轮交接更新后，预期工作区只比上述基线多一个 `HANDOFF.md` 修改，受保护 UI brief 保持未跟踪、未修改、未暂存。此句只记录旧 A+ 切片的历史工作区状态。

上述“冻结 A+ 或继续调整”是当时的历史开放项，现已由 A++ 暂定执行规格和后续实施授权关闭。当前下一步以 `UI-0A` 为准：本次文档提交普通 push 后，从其精确 SHA 开始独立的 UI-1 代码切片，并只以 A++ 第 18 节两张概念板作为视觉 fidelity 参考。

## 0. M1–M5 Baseline Repair 当前交接（2026-08-13，已完成）

### 0.1 Git 与提交

- 分支：`codex/docs-collaboration-init`；
- 已验证实现基线：`28b66a99688c1e586c7add66e4882ded45ad3d90`；
- 该实现基线已经 push；完整验证时 HEAD 与 upstream 均为上述 SHA，ahead/behind=`0/0`；
- BR-1：`2e1754e084795a8de39cef210db26ca6d92ca32b`，`fix(layer4): enforce pathway-scoped evidence admission`；
- BR-2：`07e1c37b5ad99a95a4709ba4c97263a8a3ff2ae6`，`fix(persistence): make review and decision bundles atomic`；
- BR-3：`28b66a99688c1e586c7add66e4882ded45ad3d90`，`fix(demo): project legal terminal workflow states`；
- 本次文档收口提交只包含本文件与下列三份证据文档；其提交 SHA 以实际 Git 历史为准，不在本节预写，本轮不 push。

### 0.2 B1–B5 收口

- B1 Pathway 隔离与 Summary identity：由 BR-1 修复；
- B2 DoctorReview 原子事务：由 BR-2 修复；
- B3 clinical-rule Task 正向准入：由 BR-1 修复；
- B4 M2/M3 decision 与 completion 原子事务：由 BR-2 修复；
- B5 rejected/cancelled/failed 等合法终态投影：由 BR-3 修复；
- B1–B5 不再是 UI blocker。

### 0.3 Baseline Full Validation

完整离线验证结论为 **PASS，无 BLOCKER**：

- 全量测试：`404 passed, 3 skipped`；
- 3 个 skip 仍仅因未配置官方 `FHIR_R4_SCHEMA_ZIP`；
- `.venv/bin/python -m compileall -q continucare app.py pages` 通过；
- `git diff --check dd9906215779c0b42004e5ef272321e698d6ef5c..HEAD` 与 `git diff --check` 均通过；
- rejected、unsure、unsure→accept/reject、Task rejected/cancelled 与完整 happy path 9/9 均通过；
- `task_failed` 与 `task_entered_in_error` 由自动测试证明 fail-closed，未绕过服务或直接修改数据库进行浏览器演示；
- in-app Browser 的桌面 `1280×720` 与移动端 `390×844` 六页验收通过，console error/warn=0；
- 页面刷新、终态导航和 Knowledge 浏览前后，隔离临时数据库的 SHA-256、大小、mtime、全部表行计数及 `-journal/-wal/-shm` 存在性均不变；
- 工作区 `data/continucare.db` 验证前后 SHA-256、大小和 mtime 不变；
- 没有实际发送、外部调用、真实患者、临床规则或 Alert。

### 0.4 当前能力边界

- 仅使用合成患者与合成运行数据；
- clinical assessment 仍为 `not_assessed`；
- 默认 `clinical_rules=[]`、Alert=0、approved ClinicalRule=0；
- Communication 即使为 `ready-to-send`，仍保持 `status=preparation`，没有 `sent` 或 `received`；
- `SEND_ENABLED=False`；
- 飞书/Aily 为 Mock，Bitable disabled；
- 不接真实患者、真实飞书/Aily/Bitable、EMR 或生产系统；
- Knowledge 独立只读，不参与患者事实、Task、ClinicalRule 或故事完成判定。

### 0.5 UI 状态与下一阶段

- M1–M5 baseline 已满足 UI 开工前置条件；
- UI/UX 可以进入独立的新切片，但不会从本次文档提交自动开始，实施仍需单独明确授权；
- UI 不得重新引入 B1–B5，也不得宣称自动诊断、风险分级、Alert、治疗或实际发送已经实现；
- 下一阶段先冻结中文原生的信息架构、状态词表和逐页文案，再开始代码实施；
- 真实外部系统与生产能力仍需之后单独授权和验证。

### 0.6 文档证据

- 全局审核：[`docs/m1_m5_global_integration_audit_2026-08-13.md`](docs/m1_m5_global_integration_audit_2026-08-13.md)；
- 冻结修复方案：[`docs/m1_m5_baseline_repair_plan_2026-08-13.md`](docs/m1_m5_baseline_repair_plan_2026-08-13.md)；
- 完整验证报告：[`docs/m1_m5_baseline_full_validation_report_2026-08-13.md`](docs/m1_m5_baseline_full_validation_report_2026-08-13.md)。

## 0A. M5-E 历史交接（已完成并在对应切片提交中收口）

- 分支：`codex/docs-collaboration-init`；
- 本次 M5-E 提交前 HEAD / upstream 基线：`389f5361e62ab6ef3b0c4b92e1d06e204567ebb4`；ahead/behind=`0/0`；
- M5-D 已以 `389f536 feat: add guided competition demo flow` 提交并推送；
- M5-E 已完成；代码、测试、文档与本文件在本次切片提交中一并收口，不 push；
- 本轮没有使用真实 Token，没有真实外部 API、Aily 推理、飞书发送、Bitable 写入、真实患者、EMR 写回或生产权限操作。

M5-E 新增统一 `AdapterFactory`/status、安全配置状态机、内存 tenant token cache、固定官方 host 的标准库 HTTPS transport、脱敏 `FakeTransport`、飞书 Bot 通知合同、Aily Layer 3 候选合同和 write-only Bitable 合成投影合同。默认飞书/Aily 为 Mock、Bitable disabled；无 Token 时零网络。Aily 候选仍经过本地 Schema、Safety、术语重绑和患者确认；Bitable 不是第二真相源；Bot 没有接入 manual-review Communication。

精确能力状态：`mock_fallback_verified=true`、`adapter_implemented=true`、`contract_tested_with_fake_transport=true`、`live_tenant_verified=false`、`production_ready=false`。页面状态是纯配置投影，不认证、不探活、不联网。M5-D 九项进度和 `story_complete` 仍只依赖 SQLite/FHIR 事实。

下一阶段先进行单独的 UX/UI 优化；完成后才考虑 M6 真实测试租户接线。M6 Live 必须另行获得明确授权，不得从本次提交自动开始，也不得复用本轮“仅 FakeTransport”的验收结论冒充真实联调。

最终验证（M5-E，完成后以本段命令结果为准）：

```text
.venv/bin/python -m pytest -q tests/test_m5_e_external_adapters.py
22 passed

.venv/bin/python -m pytest -q
338 passed, 3 skipped

.venv/bin/python -m compileall -q continucare app.py pages
通过

git diff --check
通过
```

三个 skip 仍只因未配置官方 `FHIR_R4_SCHEMA_ZIP`。应用内 Browser 在明确离线环境完成桌面与 390×844 六页面验收，并从 0/9 走完 M5-D 9/9；六页均显示一致的飞书/Aily Mock fallback 与 Bitable disabled 状态，没有发送按钮，console error/warn 为 0。逐页冷加载及 Knowledge 浏览前后数据库 SHA-256、大小和 mtime 不变；最终 QR=1、Observation=1、Layer4 FHIR rows=14、Summary versions=2、AuditEvent=12、Alert=0、外部审计事件=0、sent/received Communication=0、approved ClinicalRule=0。详细 API 事实、边界和回退见 `docs/29_m5_e_optional_feishu_aily_adapters.md`；用户另行授权隔离测试租户前，不得进行真实 health check、发送、写入或真实租户验证。

实施前 Opus Level 4 审查的八项 blocker 已全部吸收：全链 secret 脱敏、transport egress permit 与固定 host、动态路径校验、HMAC UUIDv4 幂等键、`outcome_unknown` 禁止盲重试、Aily 全量白名单、本地 source provenance，以及不可变的未联调/非生产标识。最终 Sonnet diff review 为 `no BLOCKER / no NEED_CONTEXT`。其 defense-in-depth 建议“permit 传实际 capability flag”已采纳；剩余三项非阻断建议为多个 client 未来可共享 token provider、Bot 上线前增加跨实例服务端幂等、共享 timeout 环境变量可改为更通用命名，均不影响当前未接线、默认离线合同。

## 1. 本次 M5-E 提交前 Git 基线与已完成提交

工作目录：

```text
/Users/zhangziyue/Documents/Codex/continucare-demo
```

当前分支：

```text
codex/docs-collaboration-init
```

upstream 为：

```text
389f5361e62ab6ef3b0c4b92e1d06e204567ebb4
```

`43012df` 之后的七个 M5 基础提交都已位于 upstream：

1. `8151161d527f717ad47a78cf145a6722e4268ece`
   `docs: define collaboration workflow and handoff`
2. `cd9bf456b0793b66fe73cd72862c93316dcb6733`
   `feat: add pathway-agnostic knowledge evidence foundation`

3. `3fa2e2a812dbf29d228cef95badb64bc894c8b3e`
   `feat: add confirmed manual review task flow`
4. `20d2521bf7bacaebe7d980c4013819d37de7fffb`
   `feat: add controlled nurse review workflow`
5. `3e171262ac94f699c5bd28ad11781c403354d9e3`
   `feat: add deterministic manual review doctor briefs`
6. `35ba612c5d04eed08ddfd4a7cb0fbdca30be0484`
   `feat: add symptom-centered knowledge evidence index`
7. `389f5361e62ab6ef3b0c4b92e1d06e204567ebb4`
   `feat: add guided competition demo flow`

Knowledge Evidence Foundation 已经正式提交，不再是未跟踪成果。它仍保持 Pathway-agnostic；GLP1-14D 只是 fixture。没有 `target_number`、固定分母或人工目标序号；20/11/9/0/0 只是当前 GLP 数据快照。Knowledge 只解释采集/展示依据，不能授权 Task、ClinicalRule 或其他运行时行为。

M5-A 已以单独 commit：

```text
feat: add confirmed manual review task flow
```

收口。

M5-B 已以单独 commit 收口：

```text
20d2521bf7bacaebe7d980c4013819d37de7fffb
feat: add controlled nurse review workflow
```

本次 M5-E 提交前 HEAD 为：

```text
389f5361e62ab6ef3b0c4b92e1d06e204567ebb4
feat: add guided competition demo flow
```

该提交前 HEAD 与 `origin/codex/docs-collaboration-init` 相同，ahead/behind 为 `0/0`。M5-C、M5-K 与 M5-D 已提交并推送。M5-E 已完成并在本次切片提交中收口；本轮不得 push。

## 2. M5-A 目标与完成结论

M5-A 已完成以下最小闭环：

```text
一键合成随访表达
→ Layer 3 受控候选
→ 患者人工确认
→ 完成 QuestionnaireResponse / final Observation
→ 创建常规护士人工复核 Task
```

这不是诊断、风险等级、临床结论、自动分诊或治疗建议。

### 2.1 首页一键入口

- 首页增加“患者确认后创建护士人工复核任务”合成场景；
- 合成原话固定为 `我今天拉肚子。`，只用于 Demo；
- 一键入口显式注入 `UnconfiguredModelAdapter(SemanticModelConfig())`，不读取环境中可能配置的真实 provider，也不发网络请求；
- 一键阶段只创建 CareSession、AgentRun 和分析审计；
- 不创建 QuestionnaireResponse、Observation、Task、Provenance 或 Alert；
- Streamlit session state 会把该 AgentRun 接到患者页待确认卡片，不再绕过 Layer 3。

### 2.2 Layer 3 人工确认边界

- 候选仍使用现有严格 candidate/clarification/resolution 合同；
- Demo 候选是 patient-reported symptom 的受控术语匹配，不是诊断或临床判断；
- 专用 M5-A 路径必须一次处理该轮完整候选集；服务端和 UI 均有完整集检查；
- `rejected`、`unsure` 和 `cancelled` 只保留允许的决策/停止审计，不发布临床资源或 Task；
- `unsure` 之后只有新的明确接受动作才能进入发布事务；
- 会话完成后不能再取消或追加第二次发布。

### 2.3 纯准备与原子发布

新增纯准备边界：

- `CareAgentService.prepare_confirmed_candidates(...)`：验证并物化答案上下文/患者自述症状，不写库；
- `CareEngine.prepare_completion(...)`：构建并校验 completed QuestionnaireResponse 和 final Observations，不写库；
- `ConfirmedReviewService.accept_all(...)`：计算稳定 receipt、组装 Task/Provenance，并调用唯一原子落库方法。

`SQLiteStore.persist_confirmed_review_bundle(...)` 使用同一个 SQLite 连接和显式 `BEGIN IMMEDIATE`，在一个事务中写入：

- confirmed answer contexts / symptom reports；
- FollowUpMessage；
- completed QuestionnaireResponse；
- final Observations 与 observation evidence；
- CareSession `completed` 转换；
- conversation action resolutions；
- 患者确认、问卷完成、Task 创建审计；
- FHIR Task 与 Provenance。

任何校验失败或事务中故障都会整体回滚。故障注入测试覆盖了 Task/Provenance 已尝试插入后的回滚，证明不会留下部分副作用。

### 2.4 幂等、并发和证据链

- receipt 是患者、会话、精确 Pathway 版本、AgentRun 和完整候选内容的 canonical SHA-256；
- Task identifier 只保存 64 位 opaque digest，不暴露 raw run/candidate ID；
- QuestionnaireResponse、Task、Provenance 和患者自述 Observation ID 均由稳定 receipt/response/report 身份派生；
- 顺序重试返回同一资源；
- 两线程并发接受测试只产生一套 QR、Observation、Task、Provenance；
- Provenance target 包含 QuestionnaireResponse、Observation 和 Task；
- Provenance 明确区分 Patient author 与 deterministic assembler；
- Task 只引用 completed QR 和 final Observation，不读取 AgentRun/candidate 作为 Layer 4 临床输入。

### 2.5 护士队列与 M6 隔离

手工复核 Task 使用独立 identifier system：

```text
urn:continucare:patient-confirmed-review
```

任务固定：

- `priority=routine`；
- 通用描述“人工复核患者已确认报告”；
- 不含 severity、risk、threshold、diagnosis、ClinicalRule 或治疗建议；
- 原话保留在 FollowUpMessage / QuestionnaireResponse，护士页从最终证据读取，不把模型标签当临床原因。

M5-A 当时为护士页新增独立只读队列；M5-B 已在相同正向分类边界上加入受控处理：

- 只正向选择 manual-review identifier；
- 不导入或暴露 `TaskWorkflowService`；
- 与旧 ClinicalRule/Alert 队列明确分开；
- 展示患者原话、最终 Observation、`routine` 和 `not_assessed`。

Doctor Workbench 的 Task 读取改为只正向选择：

```text
urn:continucare:clinical-rule
```

因此相同 Pathway 下的 M5-A 手工复核 Task 不会进入现有 M6 医生任务视图；已有 M6 接口未修改。

### 2.6 审计与可点击 Demo

审计页增加 M5-A 四步链：

```text
受控候选 → 患者确认 → 最终证据 → 护士任务
```

可点击验收路径已通过：

1. 首页点击“一键生成待患者确认的合成候选”；
2. 确认尚无 QR、Observation 或护士 Task；
3. 进入患者页，看到 SNOMED CT `62315008` 的未确认候选；
4. 点击“确认全部并创建护士人工复核任务”；
5. 患者页显示 completed QR/final Observation，临床评估仍为 `not_assessed`；
6. M5-A 当时的护士页显示独立只读 `requested/routine` Task、患者原话和最终证据；M5-B 已在此队列上加入受控领取、处理与完成；
7. Alert 队列仍为 0、获批临床规则仍为 0；
8. 审计页四步链全部完成。

桌面和 390×844 移动端均已验收；干净浏览器会话无相关 console error/warn。

## 3. 冻结安全边界

M5-A 保持以下边界，后续不得悄悄放宽：

- 只使用合成患者；
- 候选不是诊断、风险等级或临床结论；
- 必须患者明确确认后才能创建护士人工复核 Task；
- rejected、cancelled、unsure 或校验失败不得创建 Task 或留下部分临床副作用；
- 不新增或启用临床阈值、Alert、L0–L4 或 ClinicalRule；
- GLP1-14D 继续保持 `clinical_rules=[]`；
- 运行时临床评估继续为 `not_assessed`；
- 不输出治疗、改药或个体化患者建议；
- 不接真实飞书/Aily、外部 API、真实患者或生产权限；
- 不做数据库迁移；
- 不修改 M6 对外接口；
- Knowledge Evidence 只能解释采集依据，不能授权任务或规则执行。

## 4. 验证结果

最终验证：

```text
.venv/bin/python -m pytest -q
283 passed, 3 skipped

.venv/bin/python -m compileall -q continucare app.py pages
通过

git diff --check
通过
```

三个 skip 都是既有条件测试缺少官方 `FHIR_R4_SCHEMA_ZIP`：

- `tests/test_fhir_conformance.py:114`
- `tests/test_layer4_rules_tasks.py:575`
- `tests/test_layer4_summaries.py:428`

不是 M5-A 回归失败。

新增测试覆盖：

- analyze-only 零临床发布；
- happy path 完整证据链；
- 顺序幂等；
- raw run/candidate ID 不进入 Task/Provenance；
- rejected/unsure/cancelled 零发布；
- unsure 后重新明确接受；
- Task 插入后故障的全事务回滚；
- 两线程并发仅一套资源；
- Layer 4 completed/final/patient/derivedFrom admission；
- manual Task 严格 FHIR、无 rule/severity；
- 环境中配置 provider 时仍不联网；
- Doctor Workbench 排除同 Pathway manual-review Task。

## 5. Claude 审查记录

按 `AGENTS.md` 将本切片判断为 Level 4，因为它涉及医疗工作流边界和跨表原子性。

### 5.1 Opus 策略审查

实施前完成一次 Claude Opus 策略/高风险审查。其 blocker 已在冻结方案中处理，包括：

- M6 Doctor Workbench 必须正向筛选 clinical-rule Task；
- 幂等 receipt 不得直接暴露 raw run/candidate ID；
- 原子写入必须使用同连接 `BEGIN IMMEDIATE`；
- Layer 4 使用共享 completed/final admission predicate；
- Provenance 必须包含 Task，并区分 Patient confirmer 与 software assembler；
- 必须一次处理完整候选集；
- Task 不把模型标签当临床原因；
- Demo 必须显式注入本地 unconfigured adapter；
- 完成后取消不得改变 Task。

没有遗留 Opus blocker。

### 5.2 Sonnet 最终审查

实现、测试和浏览器验收后完成一次聚焦 Sonnet final review。初次只因普通 `git diff` 不包含 3 个 untracked 新文件而返回 `NEED_CONTEXT`，不是代码 blocker。补充这 3 个完整文件后，同一审核阶段最终结论：

```text
CLEAN PASS
```

Sonnet 确认：

- 服务端双重强制完整候选集；
- manual/clinical-rule identifier 为精确正向分类；
- M5-A 审查时的 ManualReviewQueue 只读边界正确；M5-B 已通过独立受控服务扩展处理能力；
- receipt/Task ID 稳定且不泄露 raw ID；
- reject/unsure/cancel、故障回滚和并发测试覆盖冻结边界；
- 无剩余 blocker 或 NEED_CONTEXT。

采纳的非阻断小建议：护士页复用 `DEMO_PATIENT_ID` 常量；UI 对 `LookupError` 也做友好提示。没有因建议扩大功能范围。

## 6. 当前剩余限制

M5-B 已解除 M5-A “护士队列只读、Task 固定 requested”的历史限制。当前明确限制是：

- 3 项官方 FHIR R4 schema 条件测试仍因缺少 `FHIR_R4_SCHEMA_ZIP` 而 skip；
- 合成护士身份没有真实鉴权或职责分离；
- Communication 批准暂不可撤销；
- 当前完全没有发送能力；
- 仍无真实患者、真实外部系统、临床规则、风险等级、Alert、诊断或治疗建议。

## 7. M5-B（已提交并推送）

M5-B 已完成以下受控闭环：

```text
requested Task
→ 护士确认收到（received）
→ 护士一次明确“接受并开始”（accepted → in-progress，同一原子动作）
→ 记录受控处理结果（completed）
→ 原子生成 Communication preparation / pending-approval
→ 护士另一次明确批准
→ Communication preparation / ready-to-send
```

`ready-to-send` 只由 ContinuCare 自定义 readiness extension 表达；Communication 所有版本均没有 `sent` 或 `received`，模块级 `SEND_ENABLED=False`，本切片不存在发送适配器、发送方法或发送按钮。

### 7.1 冻结安全与证据边界

- 专用服务只正向接收 `urn:continucare:patient-confirmed-review` Task；
- 每个动作重新校验 patient、completed QuestionnaireResponse、final Observation 与 `derivedFrom`；
- 规范化患者原话、QR 与 Observation 集合计算 SHA-256 evidence digest，写入 Task output、Provenance extension 与 audit；
- 证据完整性失败时处理动作 fail-closed、零写入，只允许用受控 `evidence-integrity-failure` 原因取消 Task；
- 两种受控处理结果不会改变患者可见模板，护士自由 note 不进入 Communication；
- 患者可见草稿是固定中性模板，不包含诊断、风险、阈值、治疗或用药建议；
- 拒绝/取消精确只写 Task 新版本、一个 Provenance 和一个 audit event，不创建 Communication/readiness；
- completed、rejected、cancelled 为本切片终态，不提供反向处理；批准当前不可撤销，但本切片没有发送能力。

### 7.2 原子性、幂等与审计

`Layer4SQLiteStore.persist_manual_review_action(...)` 使用 `BEGIN IMMEDIATE` 和 current-resource compare-and-swap，在一个事务中写入本次动作的完整 Task/Communication/Provenance 版本集合与单条 audit event。稳定 action digest 包含 Task、动作、from-version、合成 actor 与 canonical payload hash，不包含墙钟时间；顺序重试和并发重试返回原资源，不同 payload 或陈旧版本拒绝且不留下部分写入。

护士页可查看：

- 患者原话；
- completed QuestionnaireResponse；
- final Observation 与 `derivedFrom`；
- Task 全版本历史；
- 受控处理结果和护士 note；
- Communication 草稿与 pending/ready readiness。

审计页新增并验证：确认收到、接受并开始、结果与草稿、人工批准。M6 Doctor Workbench 仍只正向选择 `urn:continucare:clinical-rule` Task；回归测试覆盖 completed manual Task 也不会进入 M6。

### 7.3 M5-B 验证

```text
.venv/bin/python -m pytest -q
290 passed, 3 skipped

.venv/bin/python -m compileall -q continucare app.py pages
通过

git diff --check
通过
```

3 个 skip 仍只因缺少官方 `FHIR_R4_SCHEMA_ZIP`，与 M5-B 无关。

浏览器完整点击路径已通过，桌面与 390×844 移动视口均无相关 console error/warn；待批草稿明确显示“尚不可发送”，批准后显示“可发送（本切片未实际发送）”，Alert 与获批 ClinicalRule 均为 0。

### 7.4 M5-B Claude 审查

按 `AGENTS.md` 将 M5-B 判断为 Level 4。实施前完成一次 Opus 策略/高风险审查，冻结方案已吸收其 blocker：原子写路径、患者可见模板对称、单一发送资格合取谓词、批准并发终态守卫、证据失败 fail-closed 与 evidence digest。

实现、测试和浏览器验收后完成一次 Sonnet final review，结论：

```text
CLEAN PASS
BLOCKER: 无
NEED_CONTEXT: 无
```

采纳其一项非阻断纵深防御：原子 bundle 在存储边界显式要求恰好一个 Provenance。其余两项为已冻结的已知设计边界：QR/Observation 依赖现有不可变写入合同；批准 note 只进入内部 `Communication.note`，绝不进入患者 payload。

## 8. 接管与回退

新会话接管时先运行：

```bash
git branch --show-current
git rev-parse HEAD
git rev-parse @{upstream}
git status --short --untracked-files=all
```

当前 M5-D 交接状态：

- 工作区只包含 M5-D 未提交改动；
- 暂存区为空；
- 分支仍为 `codex/docs-collaboration-init`；
- HEAD 与 upstream 相同；
- M5-K 已 push；M5-D 未 commit、未 push。

M5-D 没有数据库迁移、真实数据或外部系统操作。若用户决定放弃本切片，只能在用户明确授权后回退未提交代码；未经授权不得 reset、clean、checkout、revert、commit 或 push。

## 9. 一句话接管结论

M5-K 已提交并位于 upstream。M5-D 已把 M5-A/B/C/K 串成稳定、可明确重置、可从持久化事实恢复、桌面/手机均验收通过的一键比赛 Demo；仍保持 synthetic-only、clinical_rules=[]、not_assessed、无外发，且当前 M5-D 改动未暂存、未提交。

## 10. 后续顺序

1. M5-C：已提交并推送；
2. M5-K：已提交并推送；
3. M5-D：已提交并推送；
4. M5-E：已完成并在本次切片提交中收口；保留无 Token Mock fallback，真实租户验证与生产可用均为 false；
5. 下一阶段：先进行 UX/UI 优化；
6. 后续 M6 Live：仅在 UX/UI 阶段之后、获得单独明确授权时，才进行真实测试租户接线。

## 11. M5-C（已提交并推送）

M5-C 新增 `ManualReviewBriefService`，只从 completed manual-review Task、completed QuestionnaireResponse、final Observation、Communication preparation、精确 Provenance 与必要 AuditEvent 形成固定模板 Summary。患者原话逐字显示；护士自由 note 不进入正文；pending 与 ready 两态分别明确为“尚不可发送；未发送”和“已人工批准；尚未发送”。

核心边界：

- `summary_kind=manual_review_brief` 隔离旧 Timeline Summary；
- 正文只由本地固定模板与受控来源事实生成；护士自由 note、AuditEvent 和模型候选不作为临床事实正文，Controlled Summary / controlled LLM 未调用；
- 同来源重复生成返回当前版本；来源变化生成新的不可变版本，旧医生审阅决定不迁移；
- 原子写入在 `BEGIN IMMEDIATE` 内重查精确来源并一次提交 Summary、Provenance 与 audit；
- 页面普通查询和陈旧性判断只读，只有明确生成/刷新按钮写入；
- Workbench as-of、精确/资源级 Provenance 关系和应用审计记录均可追踪；
- Timeline 明确标注可能为空或过时，不作为本简报事实来源；
- Alert、获批 ClinicalRule、M6 clinical-rule Task 都保持 0，发送能力关闭，临床评估保持 `not_assessed`。
- 不生成诊断、风险分级、阈值、治疗或改药建议，不发送消息，也不写回 EMR。

浏览器已完成从首页候选、患者确认、护士确认收到/接受/记录结果、医生查看 pending、护士批准、医生查看陈旧提示并明确刷新到 ready、审计全链的点击验收。桌面和 390×844 移动端通过，console 无 error/warn。

实现细节见 `docs/26_m5_c_deterministic_doctor_brief.md`。本轮还修复了批准动作的一个既有并发重试竞态：第二个同 payload 请求在看到 ready 版本后会再次执行精确幂等查找，而不是误报状态错误。

最终本地验证：

```text
.venv/bin/python -m pytest -q
299 passed, 3 skipped

.venv/bin/python -m compileall -q continucare app.py pages
通过

git diff --check
通过
```

3 个 skip 仍只因没有设置官方 `FHIR_R4_SCHEMA_ZIP`。实施前已完成一次 Opus Level 4 策略审查，并吸收其关于事务内精确来源重查、不可变版本/审阅绑定、Summary 生产者隔离、as-of 总排序、Timeline 陈旧标记以及精确/资源级 Provenance 区分的 blocker；没有遗留 Opus blocker。

实现冻结后完成一次 Sonnet final diff review。初次结论无 blocker，但要求补充 3 个最小上下文：`_replay` 是否精确匹配 actor/note、Workbench artifact version 是否来自真实 FHIR `meta.versionId`、共享准入是否拒绝非 completed/final。补充对应源码并加强 Communication v2 精确 Provenance / QR 资源级 Provenance 测试后，Sonnet 逐项关闭 NEED_CONTEXT，最终结论：

```text
CLEAN PASS
BLOCKER: 无
NON-BLOCKING: 无
NEED_CONTEXT: 无
```

M5-C 已以 `3e17126 feat: add deterministic manual review doctor briefs` 提交并推送；本轮没有修改 M5-C 运行时闭环。

## 12. M5-K（已提交并推送）

M5-K 新增严格版本化、reference-only 的 `SymptomIndexRecord` 与 `SymptomIndexFile`，只用精确 ref 把四个比赛 fixture（diarrhea、nausea、vomiting、abdominal-pain）连接到既有 catalog term、Claim、Binding 与 CoverageGap。索引自身不复制名称、coding、alias、患者表达、风险或运行时逻辑，不替代 Pathway，也不拥有临床真相。

核心行为：

- CURRENT 加载会对 catalog term、Claim、Binding、CoverageGap 及 current selection 全部 fail-closed；HISTORICAL 允许显示 unresolved catalog resolution，但绝不伪装为已解析；
- 腹泻新增一个严格 scope 的 draft collection-rationale Claim；四个症状都新增 patient-expression-evidence gap，明确没有复制 runtime aliases、独立表达来源、临床审核或真实患者验证；
- HPO v2026-06-23 与 NCI PRO-CTCAE 只登记为官方 `link_only`、`not_content_fixed`、未绑定候选来源；没有下载或打包 ontology/instrument/PDF/题目/选项/翻译/衍生内容，也没有声称 license clearance；
- PRO-CTCAE 未绑定 GLP1；CTCAE Grade 转换、分诊协议、红旗升级、FAERS、VigiAccess/VigiBase、MedDRA、UMLS 映射与新 SNOMED 内容全部延期；
- 新页面 `pages/5_knowledge_evidence.py` 只读取离线 Knowledge bundle，不导入数据库、患者、Service、模型或网络客户端；四个症状可切换，Claim scope、supports/does_not_support、source locator、review、Binding Pathway 和 gap 都可见；
- review aggregate 保持 `not_assessed`，`clinical_rules=[]` 不变；Knowledge 不授权 Observation、Task、Summary 或 ClinicalRule；
- CLI 支持精确 `--symptom-index-id` / `--record-version` 与 `--historical` 查询；wheel 包含六个 Knowledge JSON manifest；
- 最初只在首页增加独立只读入口；后续 M5-D 已将它作为不参与完成判定的独立证据出口，M5-E 完成后仍保持相同隔离边界。

最终验证：

```text
.venv/bin/python -m pytest -q tests/test_knowledge_registry.py
79 passed

.venv/bin/python -m pytest -q
307 passed, 3 skipped

.venv/bin/python -m compileall -q continucare app.py pages
通过

git diff --check
通过
```

3 个 skip 仍只因没有设置官方 `FHIR_R4_SCHEMA_ZIP`。wheel 已在临时目录构建并隔离安装，四个症状可从安装包解析，且包内没有 PDF/docx/PRO-CTCAE body/MedDRA/Vigi/NHS 内容。应用内浏览器桌面与 390×844 验收通过；正确路由冷加载 console 无 error/warn。浏览前后 `data/continucare.db` 的 SHA-256、大小、mtime 和资源计数完全一致，Alert 与 approved ClinicalRule 都为 0。

冻结方案后的 Sonnet final review 结论为 `CLEAN PASS`，没有 blocker 或 NEED_CONTEXT；唯一未使用 import 的非阻断提示已机械移除。详细设计见 `docs/27_m5_k_symptom_knowledge_expansion.md`。M5-K 已以 `35ba612 feat: add symptom-centered knowledge evidence index` 提交并推送。
