# ContinuCare UI-6：A++ UI Full Validation

日期：2026-08-14
结论：**PASS — 无未解决 UI blocker**

## 1. 目标与非目标

本轮接管 UI-6，以验收和文档收口为主：验证 UI-1 至 UI-5 已实现的 A++「原话接力」界面，覆盖六页、正向 9/9 路径、负向和边界路径、状态一致性、桌面/移动响应式、无障碍语义、中文、事实边界、数据不变性和视觉 fidelity。

本轮不是业务重构、FHIR/数据库/Knowledge 合同变更、真实外部系统接线、部署或临床能力扩展。只修复了验收中明确复现的 UI 范围问题。

## 2. 精确基线与提交链

验收输入基线：

```text
branch: codex/docs-collaboration-init
HEAD/upstream: 1ef0cefb9d8d387a2744c7f164cd79a04cdb59e1
behind/ahead: 0/0
```

| 切片 | SHA | 提交 |
|---|---|---|
| UI-0A | `8de1039a75329c3f4603d21dfc2a3b5a50e7d88a` | `docs: record provisional A++ UI execution plan` |
| UI-1 | `1a50ba8b98e666ec3fc92a882a78dbad6fd0d8cb` | `feat(ui): add A++ demo guide shell` |
| UI-2 | `1139dea9c9c4189802c2879ffa12add5a78dadef` | `feat(ui): add A++ patient follow-up` |
| UI-3 | `a14c31c7f468bdd36e778c64ac668739acf9afee` | `feat(ui): add A++ nurse workbench` |
| UI-4 | `529a0b6ba821f0cad5ba5d776bb328e926e21cad` | `feat(ui): add A++ doctor visit brief` |
| UI-5 | `1ef0cefb9d8d387a2744c7f164cd79a04cdb59e1` | `feat(ui): add A++ audit and knowledge views` |

UI-6 最小修复已经单独提交并推送：

```text
cad99d98f5cc13947d6075d62a628e6fc410d873
fix(ui): resolve full-validation regressions
```

这是本报告记录的精确测试代码基线；后续文档提交不改变已测试代码。

## 3. 六页清单

| 页面 | 路由 | 页面身份 |
|---|---|---|
| 合成演示导览 | `/` | `ContinuCare｜合成演示导览` |
| 我的随访 | `/patient_followup` | `我的随访 · ContinuCare` |
| 护士工作台 | `/nurse_risk_center` | `护士工作台 · ContinuCare` |
| 复诊速览 | `/doctor_summary` | `复诊速览 · ContinuCare` |
| 记录追溯 | `/audit_log` | `记录追溯 · ContinuCare` |
| Knowledge 资料库 | `/knowledge_evidence` | `Knowledge 资料库 · ContinuCare` |

六页均核验 title、唯一 H1、可读 DOM、角色身份、无 Streamlit exception/error surface 和无水平溢出。

## 4. 验收中修复的问题

### 4.1 渐进披露控件缺少可靠展开语义

复现位置：护士、医生、记录追溯与 Knowledge 页。修复前自定义展开入口没有稳定的 `aria-expanded`，状态只在 Streamlit session widget state 中表达。

新增共享 `render_disclosure_controls(...)`，使用同源、可聚焦的原生链接和 URL query state，提供：

- `aria-expanded="true|false"`；
- `aria-controls` 与唯一面板 ID；
- 44px 最小高度和 `:focus-visible` 轮廓；
- 当前项再次激活可收起；
- 刷新、直接链接和浏览器历史可恢复展开状态；
- 不新增第二套业务状态，不写数据库。

Browser 复验：原生 `A`、`tabIndex=0`、`target=_top`、展开前 `false`、点击后 `true`、受控面板存在、页面异常为 0。

### 4.2 医生页窄来源栏中文逐字换行

三个来源入口在约 180px 桌面窄栏中横向排列，中文逐字换行。修复后医生页单独使用纵向来源栏；桌面每项约 `172×44px`，移动端每项约 `358×44px`。其他页面布局不变。

业务服务、FHIR 合同、数据库 schema、Knowledge manifest、配置和依赖均未修改。

## 5. 自动测试与静态验证

UI targeted：

```bash
.venv/bin/python -m pytest -q \
  tests/test_competition_demo.py \
  tests/test_patient_followup_ui.py \
  tests/test_nurse_workbench_ui.py \
  tests/test_doctor_visit_brief_ui.py \
  tests/test_audit_trail_ui.py \
  tests/test_knowledge_library_ui.py \
  tests/test_knowledge_registry.py
```

结果：`228 passed in 2.85s`，exit `0`。

关键业务回归：

```bash
.venv/bin/python -m pytest -q \
  tests/test_manual_review_workflow.py \
  tests/test_manual_review_briefs.py \
  tests/test_confirmed_review.py \
  tests/test_m1_m5_pathway_isolation.py
```

结果：`35 passed in 2.38s`，exit `0`。

全量：

```bash
.venv/bin/python -m pytest -q
```

结果：`534 passed, 3 skipped in 12.66s`，exit `0`。三个 skip 与验收前一致，且仅因为没有配置官方 `FHIR_R4_SCHEMA_ZIP`：

- `tests/test_fhir_conformance.py:114`；
- `tests/test_layer4_rules_tasks.py:620`；
- `tests/test_layer4_summaries.py:727`。

没有新增 skip。

静态验证：

```bash
.venv/bin/python -m compileall -q app.py continucare pages tests
git diff --check
```

两项 exit 均为 `0`。

## 6. 完整正向 9/9 路径

使用 in-app Browser，在隔离数据库 `/tmp/continucare-ui6.GD5urs/continucare-ui6.db` 中通过真实 UI 完整执行两次；没有直接 SQL 伪造正常状态。标准路径：

1. 首页明确开始合成演示；
2. 患者核对原话“我今天拉肚子。”和系统记法“今天有腹泻”；
3. 患者明确接受；
4. 护士接手例行记录核对；
5. 护士开始核对；
6. 护士记录受控结果并形成未发送沟通文字；
7. 医生按当前记录生成 pending 速览；
8. 护士确认文字已人工核对；
9. 医生按当前来源刷新 ready 速览；
10. 进入 `story_complete` / 9/9；
11. 记录追溯解释完整链与事实边界；
12. Knowledge 独立浏览，故事状态与数据库不变。

每次关键动作后均从持久化投影重新显示；刷新保持状态；未用 Streamlit session state 保存业务阶段。

## 7. 最终资源计数与边界

| 资源 | 数量 |
|---|---:|
| QuestionnaireResponse | 1 |
| Observation | 1 |
| Task 历史版本 | 5 |
| Communication 历史版本 | 2 |
| Summary 历史版本 | 2 |
| AuditEvent | 12 |
| Alert | 0 |
| approved ClinicalRule | 0 |
| sent/received Communication | 0 |

同时确认：

```text
stage=story_complete
story_complete=True
患者可见进度=9/9
communication_readiness=ready-to-send
SEND_ENABLED=False
```

`ready-to-send` 不等于 sent；没有真实发送。

## 8. 负向和边界路径

负向夹具位于独立临时数据库，不互相污染。服务级写入使用现有受控服务；Browser 对可自然持久化状态进行了真实页面投影核查。

| 路径 | 证据 | 结论 |
|---|---|---|
| candidate_ready | Browser + automatic | 患者可接受/不确定/拒绝；此前无临床资源 |
| candidate_unsure | Browser + automatic | 仍可明确接受或拒绝 |
| unsure → accepted | automatic | 正常形成患者确认与 routine Task |
| unsure → rejected | automatic | 零 QR/Observation/Task，转终态 |
| 全部候选拒绝 | Browser + automatic | 同轮结束，不立即重说 |
| Task rejected | Browser + automatic | 无后续 Communication/Summary |
| Task cancelled | Browser + automatic | 无后续业务动作 |
| Task failed | Browser + automatic | fail-closed，保留历史 |
| Task entered-in-error | Browser + automatic | 标记无效，保留历史，无成功动作 |
| terminal reason 缺失 | automatic UI projection | 显示“原因：未记录” |
| generation 冲突 | automatic | 陈旧标签页写入被拒绝 |
| integrity_issue | automatic UI projection | 不泄露内部细节，无业务动作 |
| 未知/不一致状态 | automatic UI projection | fail-closed |
| stale Summary | automatic UI projection | 不能提交 DoctorReview |
| DoctorReview accept | automatic | 保留当前合格版本 |
| DoctorReview modify | automatic | 只改变一个既有条目，保留 section/evidence refs |
| DoctorReview reject | automatic | 不删除患者确认事实 |
| Knowledge 无 Claim | automatic | 解释缺口，不改变故事 |
| Knowledge unresolved catalog | automatic | HISTORICAL 可显示 unresolved，CURRENT fail-closed |
| Knowledge CURRENT/HISTORICAL | Browser + automatic | 来源/版本层可展开，独立只读 |

## 9. 合法状态一致性矩阵

自动测试 `test_home_guide_projects_every_supported_story_state` 覆盖全部 17 个 `CompetitionDemoStage`；角色页与审计页投影测试覆盖终态、integrity、stale 和 review guard。

Browser 使用 15 份独立/缺失数据库状态，完成 38 次首页/当前角色页/追溯页访问：

```text
not_started
candidate_ready
candidate_unsure
candidate_rejected
task_requested
nurse_received
nurse_in_progress
communication_pending
doctor_brief_pending
communication_ready
task_rejected
task_cancelled
task_failed
task_entered_in_error
story_complete
```

38 次访问均有正确 title/H1、可读内容、0 DOM error surface、0 水平溢出。

`patient_confirmed` 和 `doctor_brief_ready` 是受支持里程碑，但当前原子合同不会把它们暴露为可停留的独立持久化顶层阶段：患者确认与 Task 创建同事务完成；ready brief 直接形成 `story_complete`。二者以全部状态自动投影测试、milestone 断言和相邻真实 Browser 状态验证，没有伪造中间数据库。

## 10. Browser/IAB 环境与视口

- 工具：Codex in-app Browser；未回退 Playwright CLI；
- 服务：本地 Streamlit `127.0.0.1:8507`；状态矩阵临时使用 `8508`；
- 桌面：`1280×720`；移动：`390×844`；
- 六页桌面和六页移动最终截图均保存在仓库外；
- 两张负向代表截图为 `candidate_unsure` 和 `task_failed`；
- 主流程与六页巡检 console error/warn 为 0；状态矩阵另确认无 Streamlit exception/error surface；
- 未发现全屏 overlay、空页面或错误页面身份；
- 所有移动页 `scrollWidth-clientWidth=0`。

截图目录：

```text
/Users/zhangziyue/.codex/visualizations/2026/08/14/01a00208-76a0-7ea1-a5da-3d80f1812c02/continucare-ui6
```

## 11. 键盘、ARIA 与触控目标

- 原生 Streamlit button、radio、link 保持浏览器原生键盘语义；
- 新披露入口是原生链接，`tabIndex=0`，Enter 导航并恢复展开状态；
- 每个入口有 `aria-expanded`、`aria-controls` 和具名 `nav`；
- 焦点样式使用 3px accent 轮廓与 2px offset；
- Browser 实测展开前 `false`、展开后 `true`，面板 ID 存在；
- 移动端首页/护士/医生/审计/Knowledge 核心操作至少 44px；患者 active 决策按钮由页面样式保持至少 48px；
- radio 保持 native radio 角色和标准方向键/Space 行为。

工具限制：IAB 的合成 `.press()` 在本轮环境不能可靠激活包括标准 Streamlit 控件在内的页面控件，因此没有把该合成结果当作真实键盘失败或成功。未运行 VoiceOver/NVDA。结论基于原生元素语义、DOM 属性、可聚焦性、焦点 CSS、真实点击后的状态变化和自动测试；这是非阻断人工验收项。

## 12. 对比度与 reduced motion

| 组合 | 对比度 |
|---|---:|
| `#172126` / white | 16.38:1 |
| `#5E6B70` / white | 5.51:1 |
| `#006D70` / white | 6.14:1 |
| `#004F52` / white | 9.37:1 |
| `#A15C00` / `#FFF7ED` | 4.89:1 |
| `#B42318` / `#FFF5F4` | 6.14:1 |

均达到普通文字 AA 的 4.5:1。全局 `@media (prefers-reduced-motion: reduce)` 把滚动、动画和 transition 压到近零。IAB 不提供本轮可用的媒体偏好模拟接口；已验证 CSS 与默认 `matchMedia`，未声称完成 OS 级 reduced-motion 人工测试。

## 13. 中文逐句与事实边界

重点逐句核对：

- “患者说的话，一路跟到复诊速览。”；
- “角色切换仅用于演示，不代表已实现身份认证或权限控制。”；
- “不提供临床评估、诊断或风险分级；不会真实发送；外部系统为 Mock。”；
- 患者页明确区分“您刚才说 / 我们记成了”；
- 全部拒绝前明确“本轮会结束，当前不能立即重新表述”；
- 护士页使用“例行记录核对”，不称风险任务；
- 医生页固定显示“尚未提供临床评估”；
- 终态分开“已经产生 / 没有产生”；
- 缺失原因使用“原因：未记录”，不补造事实；
- Knowledge 声明“这里只说明采集依据，没有对这位患者做过评估。”。

未发现内部 stage、Layer、JSON、FHIR ID 或模型候选泄露到默认业务文案。事实边界保持：只有合成患者；无临床评估、诊断、风险等级、治疗/改药建议；无真实鉴权、发送或 EMR 写回；飞书/Aily 为 Mock，Bitable disabled；Knowledge 不创建患者事实、Task、Summary、AuditEvent 或已读状态；9/9 不代表临床成功。

## 14. 数据库与工作区不变性

受保护工作区数据库验收前后完全一致：

```text
path: data/continucare.db
SHA-256: 0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585
size: 311296
mtime: 1786644509
journal/WAL/SHM: 不存在
```

受保护未跟踪 brief 保持不变、未暂存：

```text
docs/ui_product_design_brief_2026-08-13.md
SHA-256: e9e03bde1051f43ec8dbca2695716a70588b448e47b37e434015934121be03a6
```

happy-path 数据库六页桌面/移动只读巡检前后：

```text
SHA-256: 797b4800359ef2272c5cdfcb2701a5f0a6097d4a507a8eda1c8516c8f9337138
size: 311296
mtime: 1786741360
```

三项均不变，证明页面刷新、截图、来源展开和 Knowledge 浏览没有业务写入。

## 15. Fidelity ledger

| A++ 概念要求 | 实际结果 | 结论 |
|---|---|---|
| 角色默认只见当前所需事实 | 六页按角色收敛，无全局六页菜单 | 符合 |
| 来源一跳可达 | 护士/医生/审计/Knowledge 就地展开 | 符合；补齐 ARIA |
| 边界和后果不折叠 | 首页、患者决定前、医生与终态直接可见 | 符合 |
| 五步演示故事 | 首页 1–5 显示当前/完成 | 符合 |
| 患者原话优先 | 原话与系统记法分层，移动优先 | 符合 |
| routine record check | 无风险/SLA/Alert 墙 | 符合 |
| 医生三项事实分开 | 患者事实、护理动作、未评估边界首屏分离 | 符合 |
| 医生窄来源栏 | 约 180px，纵向 44px 入口 | 符合；验收修复 |
| 审计先人话后技术 | 结论/原因/产物先显示，技术默认折叠 | 符合 |
| Knowledge 独立只读 | 无患者上下文、无故事参与、无 DB 写入 | 符合 |
| 390px 无横向滚动 | 六页 overflow 均为 0 | 符合 |
| 克制终态 | complete 与错误终态共用事实结构 | 符合 |

两张 accepted concept：

```text
/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-5772d17f-07c0-4b3e-a794-92d053037c0d.png
/Users/zhangziyue/.codex/generated_images/019ffc9e-ca93-7320-8c30-3014c82e4493/exec-146b2274-0937-4b74-b5dc-1e7002af6be9.png
```

两图和六页桌面/移动截图均用 `view_image` 核对。概念板是结构/视觉参考，不是静态资产；真实事实和 A++ 规格优先。

## 16. Knowledge v2/ops 边界

当前 UI 继续使用主分支既有离线 Knowledge bundle。独立分支 `codex/knowledge-v2-alias-readiness` 已 push，tip 为：

```text
0110c319a58e2de7baa92e83788a56113bc62a0c
fix(privacy): verify typed review evidence digest context
```

该提交不是当前 HEAD 的 ancestor，尚未合入 `codex/docs-collaboration-init`。UI-6 未 cherry-pick、merge 或修改该分支。后续合入仍需用户单独授权，并按独立 Knowledge/ops 切片验证。

## 17. 工具、Agent、外部操作与限制

- 使用 `build-web-apps:frontend-testing-debugging` 与 in-app Browser；
- 没有使用 ImageGen；
- 没有调用 Claude、Sonnet、Opus 或任何子 Agent；
- 没有真实患者数据、模型调用、真实网络发送、外部系统写入或部署；
- 只执行用户授权的 commit 与普通 push；没有 force push。

剩余非阻断限制：未运行真实屏幕阅读器；未做 OS 级 reduced-motion 模拟；三个官方 FHIR schema 条件测试仍需外部 `FHIR_R4_SCHEMA_ZIP`；当前是 synthetic-only 本地原型，不是临床试点或生产系统。

## 18. 最终结论

UI-1 至 UI-5 的 A++「原话接力」实现通过 UI-6 全量验收。两个可复现 UI 问题已以最小改动修复并完整回归；六页、正向路径、持久化状态矩阵、负向投影、响应式、ARIA、中文、事实边界、数据不变性和概念 fidelity 均无未解决 blocker。
