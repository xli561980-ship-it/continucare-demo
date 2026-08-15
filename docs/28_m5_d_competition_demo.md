# M5-D 稳定的一键比赛 Demo

## 1. 结论与边界

M5-D 把已冻结的 M5-A、M5-B、M5-C、M5-K 串成一条可重复的合成比赛故事，但不复制或改写它们的业务状态机。

“一键”的精确定义是：

> 用户明确同意重置本地合成运行数据后，一键原子替换为一条固定患者、固定原话、只有 Layer 3 未确认候选的故事起点。

这一键不会：

- 替患者确认候选；
- 创建 QuestionnaireResponse、Observation、Task、Communication、Summary 或 Alert；
- 替护士接收、处理或批准；
- 替医生生成、接受、修改或拒绝简报；
- 调用真实模型、飞书、Aily、外部 API 或发送适配器；
- 读取或修改 Knowledge manifests；
- 修改 M6 clinical-rule Task 接口。

所有身份、原话和资源都是合成数据。候选不是诊断、风险等级或临床结论。

## 2. 实施前行为与证据

- 首页已有 `load_manual_review_scenario()`，能生成固定原话“我今天拉肚子。”的本地候选，但先删除目标数据库；分析失败时可能只留下半起点。
- 患者页普通加载调用 `start_or_resume()`，可能隐式创建 CareSession；候选的可操作 run 主要依赖 Streamlit session state，新浏览器不能恢复。
- M5-A 的患者确认发布、M5-B 的护士动作、M5-C 的简报生成已经分别具备 SQLite 原子事务、CAS、幂等和并发测试。
- M5-K 页面已是离线只读，并有反向依赖测试保证 runtime 不导入 Knowledge。

因此 M5-D 只补“原子起点 + 纯读取进度 + 页面导览”，不重做已有服务。

## 3. 完整点击流

```text
首页明确开始
→ candidate_ready
→ 患者明确确认
→ completed QuestionnaireResponse + final Observation + derivedFrom
→ routine manual-review Task requested
→ 护士确认收到 received
→ 护士接受并开始 in-progress
→ 护士记录受控结果
→ Communication preparation / pending-approval / 未发送
→ 医生明确生成 pending 简报
→ 护士明确批准 ready-to-send / 未发送
→ 医生看到陈旧提示并明确刷新 ready 简报
→ 审计页查看完整链路
→ Knowledge 页面独立查看 diarrhea 采集依据
→ 返回首页查看 story_complete
```

护士先批准、医生后首次生成也是合法顺序。进度投影不会虚构不存在的 pending 简报。

## 4. 原子重置与失败恢复

`start_competition_demo()` 不直接清空目标数据库：

1. 获取目标数据库专用的跨进程文件锁；
2. 在同目录创建每次调用唯一的 staging SQLite 文件；
3. 在 staging 中调用既有 `load_manual_review_scenario()`；
4. 用 SQLite `mode=ro` 校验固定患者、唯一 session/run、未确认候选和零临床发布；
5. 确认没有 journal/WAL/SHM sidecar，并 fsync staging 主文件；
6. `os.replace()` 原子替换目标文件并 fsync 目录；
7. 重新从目标读取当前 generation。

失败发生在替换前时，目标数据库字节和 generation 保持不变；错误文案不包含路径、堆栈或底层异常。下一次明确点击可重新尝试。

旧技术 fixture 也通过相同 staging、文件锁和显式重置同意，不能旁路主入口的破坏性操作门。

## 5. 并发与 generation 护栏

文件锁覆盖：

- 完整比赛 Demo start/restart；
- 旧技术 fixture 的原子替换；
- 患者页面所有写动作；
- 护士 Task、Communication 与 Alert 写动作；
- 医生简报生成/刷新和医生审阅动作。

页面动作进入锁后会重新比较 `(session_id, run_id)` 派生的 generation。若另一标签页已经重新开始，旧页面动作会被拒绝并要求刷新，避免把人工决定写入错误故事。

业务动作本身仍由 M5-A/B/C 的 CAS 和事务负责；文件锁不替代这些合同。

## 6. 只读进度模型

`read_competition_demo()` 使用 SQLite URI `mode=ro` 和 `PRAGMA query_only=ON`。数据库不存在时返回 `not_started`，不会创建文件、表、患者或审计。

它只读取：

- 固定合成患者对应的 CareSession 与 AgentRun；
- candidate 与持久化患者决定；
- completed QuestionnaireResponse；
- final Observation 与 `derivedFrom`；
- manual-review Task 当前版本；
- Communication 全版本与 readiness；
- manual-review Summary 全版本与精确来源引用；
- Provenance、AuditEvent、Alert 和已批准/active ClinicalRule 计数。

进度是一组独立派生谓词，不是第二套线性状态机：

| 谓词 | 持久化事实 |
| --- | --- |
| `candidate_ready` | 固定 AgentRun 含候选 |
| `patient_confirmed` | session completed、QR completed、Observation final 且 derivedFrom 正确 |
| `task_requested` | 存在同 patient/pathway 的 manual-review Task |
| `nurse_received` | Task 当前或后续状态为 received/accepted/in-progress/completed |
| `nurse_in_progress` | Task 当前或后续状态为 accepted/in-progress/completed |
| `communication_pending` | 存在 pending-approval Communication 版本 |
| `doctor_brief_pending` | 存在精确引用 pending Communication 版本的 manual-review brief |
| `communication_ready` | 当前 Communication readiness 为 ready-to-send |
| `doctor_brief_ready` | 当前 brief 精确引用当前 ready Communication 版本 |
| `story_complete` | completed Task 且当前 ready brief 与当前来源一致 |

Knowledge availability 和“是否浏览过页面”都不参与 `story_complete`。Knowledge 页面本身加载 CURRENT/HISTORICAL registry 并显示状态，继续保持独立只读。

## 7. 页面加载与 session state

- 首页普通加载只读取；没有数据库时显示 `not_started`。
- 患者页不再调用 `start_or_resume()`；没有持久化比赛 session 时只引导返回首页。
- 新浏览器缺少 run 提示时，从 read model 恢复准确 AgentRun；session state 只保存 widget/navigation 提示。
- 主比赛 session 只允许通过候选卡片作出患者决定，不显示可绕过 manual-review Task 的完整问卷提交入口。
- 护士、医生、审计页普通加载只查询；写入只发生在明确按钮。
- Knowledge 页面不导入数据库、service、Layer 3 或 Layer 4。

## 8. 人工门禁与安全冻结

- `rejected`、`unsure`、`cancelled` 不创建 Task；
- Task 固定 `priority=routine`，临床评估 `not_assessed`；
- 护士完成后才有 pending 草稿；
- 人工批准后才是 ready-to-send；
- 所有 Communication 仍为 `status=preparation`，没有 `sent` / `received`；
- `SEND_ENABLED=False`，无发送按钮或适配器；
- `clinical_rules=[]`，Alert=0，approved ClinicalRule=0；
- manual-review Task 不进入 M6 clinical-rule Task 列表；
- controlled LLM 与真实模型均不实例化、不调用；
- 不输出诊断、治疗、改药、阈值、分级或个体化建议。

护士动作和批准 note 预填的是固定合成比赛文案，用于压缩 60–90 秒点击时长；
操作者仍须逐步明确点击。该文案不是独立人工审阅的证明，不能复用于真实审计、
真实患者或任何需要证明实际人员自由输入/核对的流程。

## 9. Knowledge 与患者事实隔离

Knowledge 页面展示：

- diarrhea exact catalog term；
- Claim statement 与 visible scope；
- `supports` / `does_not_support`；
- source locator 与 `link_only / not_content_fixed`；
- `review=not_assessed`；
- Binding（如存在）与 CoverageGap；
- HPO、PRO-CTCAE 的 unbound 边界。

它不读取患者数据库，不根据故事进度写入，不授权运行时动作，也不声称 PRO-CTCAE 已绑定 GLP1。

## 10. 比赛要求—Demo 证据映射

比赛文件的企业命题是“诊后主动随访、监测异常、起草沟通、院外数据沉淀、复诊前简报”，并建议借助飞书 AI 工具形成连续闭环。当前证据必须分层表述：

| 比赛要求 | 当前 Demo 已真实实现 | Mock / 合成 | 尚未实现或 M5-E |
| --- | --- | --- | --- |
| 诊后随访数据沉淀 | QR、Observation、derivedFrom、本地 SQLite 与审计 | 固定合成患者和原话 | 真实患者、医院接口 |
| Agentic 信息整理 | Layer 3 受控候选、硬规则、人工确认门 | 本地语义 Mock | 真实模型能力与真实语料验证 |
| 异常监测/分级 | fail-closed、明确显示 not_assessed | 无 | 医院批准规则、L0–L4、Alert |
| 护士闭环 | manual Task 状态、受控结果、Provenance | 合成护士身份 | 真实 IAM、SLA、组织职责 |
| 沟通起草 | 中性 preparation Communication 与人工 readiness | 固定模板 | 实际发送、真实飞书 |
| 复诊前简报 | 确定性、版本化、逐项证据简报 | 合成医生身份 | EMR 写回、真实临床使用 |
| 知识依据 | 离线 Claim/Binding/Gap/source registry | fixture snapshot | 临床/术语/许可审批 |
| 飞书/Aily | 无 | 首页诚实标注关闭 | M5-E 计划 |

不得据此宣称已完成医院集成、真实模型、真实患者使用、临床审批、临床效果、自动风险分级、实际发送或医院级 FHIR 合规。

## 11. 测试计划与验收命令

专项测试覆盖：

- 缺失数据库的只读 `not_started`；
- 一键只生成未确认 candidate；
- 失败 start 保留旧数据库字节和 generation；
- 并发 start 最终只有一条完整起点；
- 推荐完整顺序的每个派生里程碑；
- 先批准后首次生成简报的合法乱序；
- Knowledge 不参与 clinical completion；
- 显式重启清除旧链，旧 generation 写入拒绝；
- 患者页加载不再隐式创建 session；
- M5-K runtime 反向依赖保持关闭。

最终命令：

```bash
.venv/bin/python -m pytest -q tests/test_competition_demo.py
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q continucare app.py pages
git diff --check
```

最终结果：专项 `9 passed`；全量 `316 passed, 3 skipped`；compileall 与
`git diff --check` 均通过。三个 skip 只因未提供官方 `FHIR_R4_SCHEMA_ZIP`，
分别来自 FHIR conformance、Layer 4 rules/tasks 和 Layer 4 summaries 的条件测试。

## 12. Browser 验收

在应用内 Browser 完成桌面与 390×844 移动视口：

1. 完整推荐点击流；
2. pending/ready 两版简报与陈旧提示；
3. 审计与 diarrhea Knowledge；
4. 返回首页 story_complete；
5. 硬刷新不新增资源；
6. 明确重启第二轮，旧链清除；
7. 无横向溢出、关键按钮可操作；
8. console error/warn 为 0；
9. 无外部请求、模型调用或发送；
10. Knowledge 浏览前后患者数据库字节和资源计数不变。

实际验收结果：桌面与手机分别完成一轮 9/9 推荐故事；第二轮通过首页明确
restart 重新生成唯一链路。首页、患者、护士、医生、审计、Knowledge 六页 URL、
title 与 DOM 均正常，所有页面 console error/warn 为 0。逐页冷加载前后数据库
SHA-256 相同，且 QR=1、Observation=1、Layer 4 FHIR rows=14、Summary versions=2、
AuditEvent=12、Alert=0；Knowledge 单独浏览前后数据库 SHA-256、大小和 mtime 也
完全不变。没有点击任何外部来源链接；主线只调用 `UnconfiguredModelAdapter` 和
本地确定性语义 Mock，没有发送能力。

## 13. 回退

本切片没有数据库迁移。代码回退只需在用户明确授权后删除/恢复本轮文件；本地运行数据库只含可丢弃合成数据，可通过原子 start 重建。代码回退不会恢复已被明确替换的旧合成数据库，这一点不适用于真实数据系统。
