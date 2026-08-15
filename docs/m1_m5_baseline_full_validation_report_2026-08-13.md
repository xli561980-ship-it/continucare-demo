# ContinuCare M1–M5 Baseline Full Validation 报告

- 验证日期：2026-08-13
- 工作目录：`/Users/zhangziyue/Documents/Codex/continucare-demo`
- 分支：`codex/docs-collaboration-init`
- 验证 HEAD：`28b66a99688c1e586c7add66e4882ded45ad3d90`
- 验证方式：显式离线配置、全量自动测试、隔离合成数据库、in-app Browser 桌面与移动端验收
- 总结论：**PASS，无 BLOCKER**

本轮未修改代码，未 add、commit、push，未更新 `HANDOFF.md`，未开始 UI/UX，未调用 Claude，未连接真实飞书、Aily、Bitable、LLM 或其他外部 API，也未使用真实患者数据。

## 1. 完整读取范围

验证开始前已完整读取：

1. `AGENTS.md`
2. `HANDOFF.md`
3. `docs/m1_m5_global_integration_audit_2026-08-13.md`
4. `docs/m1_m5_baseline_repair_plan_2026-08-13.md`

本轮没有重新进行全仓审核，仅按冻结方案验证 BR-1、BR-2、BR-3 合并后的基线。

## 2. Git preflight 与最终状态

Preflight 与验证结束时的最终核验均为：

- 分支：`codex/docs-collaboration-init`
- HEAD：`28b66a99688c1e586c7add66e4882ded45ad3d90`
- upstream：`28b66a99688c1e586c7add66e4882ded45ad3d90`
- ahead/behind：`0 / 0`
- `git diff --name-only`：空
- `git diff --cached --name-only`：空
- `git status --short --untracked-files=all` 仅有：

```text
?? docs/m1_m5_baseline_repair_plan_2026-08-13.md
?? docs/m1_m5_global_integration_audit_2026-08-13.md
```

Preflight 最近 8 个提交：

```text
28b66a9 fix(demo): project legal terminal workflow states
07e1c37 fix(persistence): make review and decision bundles atomic
2e1754e fix(layer4): enforce pathway-scoped evidence admission
dd99062 feat: add optional feishu and aily adapters
389f536 feat: add guided competition demo flow
35ba612 feat: add symptom-centered knowledge evidence index
3e17126 feat: add deterministic manual review doctor briefs
20d2521 feat: add controlled nurse review workflow
```

## 3. 离线安全配置

所有自动测试和运行态验收均显式使用：

```text
CONTINUCARE_LLM_PROVIDER=unconfigured
CONTINUCARE_USE_SAFETY_LLM=false
CONTINUCARE_USE_LANGUAGE_LLM=false
CONTINUCARE_USE_SUMMARY_LLM=false
CONTINUCARE_FEISHU_MODE=mock
CONTINUCARE_AILY_MODE=mock
CONTINUCARE_BITABLE_MODE=disabled
CONTINUCARE_EXTERNAL_EGRESS_ENABLED=false
CONTINUCARE_FEISHU_TEST_TENANT_ENABLED=false
CONTINUCARE_AILY_TEST_TENANT_ENABLED=false
CONTINUCARE_BITABLE_TEST_TENANT_ENABLED=false
```

`PYTHONPYCACHEPREFIX` 均指向由 `mktemp` 创建的系统临时目录。

## 4. 验证命令与结果

| 命令 | Exit code | 决定性输出 |
|---|---:|---|
| `.venv/bin/python -m pytest -q` | 0 | `404 passed, 3 skipped in 14.48s` |
| `.venv/bin/python -m compileall -q continucare app.py pages` | 0 | 无错误输出 |
| `git diff --check dd9906215779c0b42004e5ef272321e698d6ef5c..HEAD` | 0 | 空 |
| `git diff --check` | 0 | 空 |
| 本地 Streamlit 启动 | 0 | `127.0.0.1:8507` 正常启动；最终 Ctrl-C 正常停止 |
| 各场景 `read_competition_demo` 投影 | 0 | rejected、unsure、Task terminal、story_complete 均得到预期事实 |
| 临时库只读快照脚本 | 0 | hash、stat、全部表计数、sidecar 均成功读取 |
| happy-path 资源检查脚本 | 0 | QR、Observation、Task、Communication、Summary 与安全边界均符合 |
| 服务停止后 `lsof -nP -iTCP:8507 -sTCP:LISTEN` | 1（预期） | 无监听进程 |
| 最终 Git 核验 | 0 | HEAD、upstream、ahead/behind、status 全部符合 |

执行过程说明：首次 pytest 包装命令因包含递归清理 trap，在进程启动前被平台安全检查拒绝；测试没有运行。随后保留系统临时目录并成功执行。另有一次只读 schema 探查使用了错误列名，修正后 exit 0；未发生数据库写入。

## 5. Pytest passed、skipped 与 skip 原因

实际结果：

```text
404 passed, 3 skipped in 14.48s
```

三个 skip 均仍只因为没有配置官方 `FHIR_R4_SCHEMA_ZIP`：

```text
tests/test_fhir_conformance.py:114
tests/test_layer4_rules_tasks.py:620
tests/test_layer4_summaries.py:727
```

共同原因：

```text
set FHIR_R4_SCHEMA_ZIP to the official HL7 R4 schema archive
```

`task_failed` 与 `task_entered_in_error` 由参数化测试 `test_manual_task_error_statuses_fail_closed_without_success_actions` 覆盖。该测试断言：

- 进入对应 fail-closed 终态；
- 不提供成功或继续业务导航；
- Communication=0；
- Summary=0；
- 投影读取不写数据库。

## 6. Compileall 与 diff check

- compileall 使用独立 `mktemp` `PYTHONPYCACHEPREFIX`，exit 0。
- BR-1/2/3 相对 `dd9906215779c0b42004e5ef272321e698d6ef5c` 的 diff check：exit 0。
- 当前工作树 diff check：exit 0。
- 未在仓库内产生 `.pyc` 或缓存改动。

## 7. 运行态场景

| 场景 | 实际状态 | 资源与行为 |
|---|---|---|
| 全部候选 rejected | `candidate_rejected`；`is_terminal=true` | QR=0、Observation=0、Task=0、Communication=0、Summary=0、Alert=0；仅审计或返回首页明确 restart |
| unsure | `candidate_unsure`；`is_terminal=false` | 所有临床资源为 0；导航明确要求继续接受或拒绝 |
| unsure→accept | 成功进入患者确认及 Task requested | QR=1、Observation=1、manual Task=1 |
| unsure→reject | `candidate_rejected`；`is_terminal=true` | 最终仍为零临床资源 |
| Task rejected | `task_rejected`；`is_terminal=true`；Task status=`rejected` | QR=1、Observation=1、Task=1、Communication=0、Summary=0、Alert=0；无后续业务动作 |
| Task cancelled | `task_cancelled`；`is_terminal=true`；Task status=`cancelled` | QR=1、Observation=1、Task=1、Communication=0、Summary=0、Alert=0；无后续业务动作 |
| 完整 happy path | `story_complete`；9/9；`is_terminal=true` | 见下方 |

### 7.1 全部候选 rejected

- stage：`candidate_rejected`
- is_terminal：`true`
- terminal reason：`所有候选均已由患者明确拒绝；未创建临床资源或护士任务。`
- QR、Observation、Task、Communication、Summary、Alert：全部为 0
- 页面内容区只提供“查看终态审计”和“返回首页（不会自动重新开始）”
- 角色页不再显示患者确认或其他业务动作

### 7.2 unsure

- 中间 stage：`candidate_unsure`
- is_terminal：`false`
- terminal reason：`null`
- QR、Observation、Task、Communication、Summary、Alert：全部为 0
- 共享导航明确说明“不确定不是终态”，仍可明确接受或拒绝
- `unsure→accept` 成功创建 completed QR、final Observation 和 manual-review Task
- `unsure→reject` 成功进入 `candidate_rejected`

### 7.3 Task 合法终态

Task rejected：

- stage：`task_rejected`
- is_terminal：`true`
- Task status：`rejected`
- terminal reason：`护士已明确拒绝人工复核任务；流程已终止，未创建 Communication 或医生简报。`
- Communication=0、Summary=0、Alert=0、approved ClinicalRule=0

Task cancelled：

- stage：`task_cancelled`
- is_terminal：`true`
- Task status：`cancelled`
- terminal reason：`护士已明确取消人工复核任务；流程已终止，未创建 Communication 或医生简报。`
- Communication=0、Summary=0、Alert=0、approved ClinicalRule=0

两种终态均没有倒退到患者确认，也没有后续护士或医生业务动作。

### 7.4 完整 happy path

- stage：`story_complete`
- 进度：9/9
- is_terminal：`true`
- terminal reason：`合成 happy path 的 9 项持久化事实已完成；这不代表临床结论，Communication 仍未发送。`
- QuestionnaireResponse：1，`completed`
- Observation：1，`final`，精确 `derivedFrom` 上述 QR
- manual-review Task：1 个逻辑资源、5 个版本，最终 `completed`
- Task identifier：`urn:continucare:patient-confirmed-review`
- M6 clinical-rule Task：0
- Communication：1 个逻辑资源、2 个版本：
  - v1：`preparation / pending-approval`
  - v2：`preparation / ready-to-send`
  - 两版均无 `sent`、无 `received`
- Summary：1 个逻辑资源、2 个不可变版本，v2 current
- Provenance：7
- AuditEvent：12
- Alert：0
- ClinicalRule resource/contract：0
- clinical assessment：`not_assessed`
- `SEND_ENABLED=False`
- 外部或发送相关审计事件：0

## 8. 桌面、390×844 与 console

使用 in-app Browser 完成验收，没有切换到外部 Playwright 或 Chrome。

- 桌面实际视口：`1280×720`
- 移动视口：精确 `390×844`
- 页面：首页、患者、护士、医生、审计、Knowledge

六页均满足：

- 页面身份和标题正确；
- 页面非空；
- 无框架异常或错误覆盖层；
- 无 dialog；
- 页面级横向溢出为 0；
- console error/warn 为 0。

终态患者、护士、医生页仅显示终态事实和审计/首页入口，没有确认、接收、处理、批准、生成或医生审阅等非法后续按钮。

移动审计页的长技术 JSON 位于 `overflow-x:auto` 代码容器内；页面本身宽度仍为 390，无覆盖或页面级横向滚动。

Knowledge 页面验证结果：

- 独立离线页面，不显示或参与比赛状态机；
- 明确“不读取患者数据”“不授权任何临床运行时行为”；
- `review=not_assessed`；
- 当前腹泻条目没有 exact Binding，页面明确说明不会据此创建 runtime artifact；
- 不参与 Observation、Task、Summary、ClinicalRule 或完成判定；
- 未点击任何外部来源链接。

## 9. 临时数据库只读证据

### 9.1 rejected 终态刷新与导航

刷新 rejected 终态、进入审计、返回首页前后完全一致：

```text
SHA-256: 6c2e5eba7f446bebcc96c349c790393beca643a845d9aee6a9d8718cdb8bc9ec
size: 253952
mtime_ns: 1786646172634577837
-journal/-wal/-shm: 均不存在
```

全部表计数：

```text
agent_runs=1
alert_actions=0
alerts=0
audit_events=4
care_sessions=1
confirmed_answer_contexts=0
confirmed_symptom_reports=0
conversation_action_resolutions=1
demo_metadata=1
fhir_observations=0
fhir_questionnaire_responses=0
followup_messages=0
layer4_contract_records=0
layer4_fhir_resources=0
observation_evidence=0
patients=1
summaries=0
```

### 9.2 story_complete 六页浏览、刷新与终态导航

happy-path 六页桌面/移动浏览、刷新、终态导航前后及服务停止后均完全一致：

```text
SHA-256: 198ea24842df12ebffe2a2596b1ab48bf8633fbb1c66dff964e11bf164d6f339
size: 311296
mtime_ns: 1786646502632390704
-journal/-wal/-shm: 均不存在
```

全部表计数：

```text
agent_runs=1
alert_actions=0
alerts=0
audit_events=12
care_sessions=1
confirmed_answer_contexts=0
confirmed_symptom_reports=1
conversation_action_resolutions=1
demo_metadata=1
fhir_observations=1
fhir_questionnaire_responses=1
followup_messages=1
layer4_contract_records=2
layer4_fhir_resources=14
observation_evidence=1
patients=1
summaries=0
```

两个医生简报版本位于 `layer4_contract_records`；legacy `summaries` 表保持 0。

## 10. 工作区数据库未变化

验证前后完全一致：

```text
data/continucare.db
SHA-256: 0d0b35a97d96faee19015d8917b6b5e42a65ff40a2dd99dca967d5b02e6ef585
size: 311296
mtime_ns: 1786644509261958461
```

## 11. BLOCKER

**无 BLOCKER。**

没有测试失败、Git 漂移、工作区数据库变化、非法终态动作、实际发送、临床规则、Alert 或外部调用证据。

## 12. 未执行项与剩余风险

- 官方 FHIR R4 schema archive 未配置，因此保留 3 个条件 skip。
- `task_failed`、`task_entered_in_error` 仅按要求由自动测试验证，没有绕过 UI 进行浏览器演示。
- 未进行真实飞书、Aily、Bitable、LLM、测试租户或其他外部 API 验证。
- 未测试 in-app Browser 以外的浏览器或其他响应式尺寸。
- 冻结方案中列出的可延期 HIGH PRIORITY 项未重新审核，本轮没有扩大范围。
- `mktemp` 目录仅含缓存和合成数据库，位于系统临时目录；未执行删除命令。
- 未更新 HANDOFF、其他文档或代码，未开始 UI/UX，未调用 Claude。

## 13. 报告文件生成说明

本文件是在 Baseline Full Validation 全部完成、服务停止且最终 Git 核验完成后，按用户新的明确指令生成。因此，第 2 节记录的是**验证结束当时**的 Git 状态；创建本报告后，当前工作区会额外出现本报告这一份未跟踪文件。本文件未被暂存、提交或推送。
