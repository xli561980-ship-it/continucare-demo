# ContinuCare M5-D 比赛演示脚本

> 全程使用“陈女士（合成）”与固定原话“我今天拉肚子。”。主线离线运行，不调用真实模型、飞书、Aily 或外部 API。

页面的 M5-E 状态用于诚实展示工程边界：飞书/Aily 是 Mock fallback，Bitable disabled；适配器代码和 FakeTransport 合同已完成，但真实租户验证与生产可用均为否。不要把该状态口述为“已联调”。

## 60–90 秒精简版

| 时间 | 点击位置 | 讲解重点 | 可展示证据 |
| --- | --- | --- | --- |
| 0–10s | 首页勾选重置确认，点击“开始完整比赛 Demo” | 一键只准备未确认候选，不替任何角色做决定 | 首页 QR/Observation/Task/Alert 全为 0 |
| 10–25s | “前往患者端明确确认”→“确认全部并创建护士人工复核任务” | 患者原话不丢失；患者确认是发布门 | completed QR、final Observation、SNOMED CT `62315008`、derivedFrom |
| 25–45s | 护士页依次“确认收到”→“接受并开始”→“记录结果并生成沟通草稿” | 任务是 routine manual review，不是自动分诊 | Task 版本链；`not_assessed`；pending-approval；尚不可发送 |
| 45–58s | 医生页“明确生成 M5-C 证据简报” | 固定模板只组织已确认来源，不调用 LLM | 患者逐字原话、QR、Observation、护士结果、Provenance |
| 58–70s | 护士页“明确批准进入可发送状态”→医生页“明确生成/刷新” | ready-to-send 仍未发送；来源变化使旧简报陈旧 | Communication `preparation`、无 sent/received、ready 简报新版本 |
| 70–90s | 审计页→Knowledge 页 | 临床事实与流程审计分开；Knowledge 只解释采集依据 | AuditEvent 非临床 evidence；Claim scope、supports、does_not_support、review=not_assessed、CoverageGap |

精简讲稿：

> 我们先一键准备一条完全合成的院外故事，此时只有未确认候选。患者明确确认后，原话进入 completed QuestionnaireResponse，并形成带 derivedFrom 的 final Observation；系统只创建 routine 人工复核任务，临床评估仍是 not_assessed。护士显式接收、开始和记录受控结果，得到 pending 草稿；医生明确生成确定性证据简报。护士人工批准后只是 ready-to-send，仍未发送；医生再明确刷新到当前来源版本。最后可把每条临床事实回放到原话、FHIR 资源和 Provenance，并独立查看腹泻的 Knowledge Claim 与 CoverageGap。

## 2–3 分钟完整版

1. **首页：明确开始**
   - 勾选“会替换本地合成 Demo 运行数据”。
   - 点击“开始完整比赛 Demo”。
   - 指出固定合成身份、固定原话、local semantic Mock、无外部 API。
   - 展示计数：QR=0、Observation=0、manual Task=0、Alert=0、获批 ClinicalRule=0。

2. **患者页：人工确认门**
   - 点击“前往患者端明确确认”。
   - 展开候选，读出患者逐字原话与 SNOMED CT `62315008`。
   - 点击“确认全部并创建护士人工复核任务”。
   - 展示 completed QuestionnaireResponse、final Observation 和 `derivedFrom QuestionnaireResponse/...`。
   - 强调 candidate 不是诊断；拒绝/不确定/取消不会创建 Task。

3. **护士页：受控 Task**
   - 点击“确认收到任务”。
   - 点击“接受并开始复核”。
   - 选择“已核对证据，记录一致”，点击“记录结果并生成沟通草稿”。
   - 展示 Task requested→received→accepted→in-progress→completed 版本链。
   - 展示中性固定模板、pending-approval、尚不可发送、未发送。

4. **医生页：显式生成 pending 简报**
   - 点击“先前往医生端生成 pending 简报”。
   - 点击“明确生成 M5-C 证据简报”。
   - 展示逐字原话、completed QR、final Observation/derivedFrom、护士受控结果、pending readiness。
   - 展开一条 evidence reference 和证据图，指出 AuditEvent 只证明流程发生。

5. **护士页：人工批准**
   - 返回护士页。
   - 点击“明确批准进入可发送状态”。
   - 展示 `ready-to-send`，同时读出“尚未发送”。
   - 明确页面没有发送按钮，`SEND_ENABLED=False`。

6. **医生页：显式刷新 ready 简报**
   - 返回医生页，展示旧 pending 简报的陈旧提示。
   - 点击“明确生成 / 刷新为当前来源版本”。
   - 展示 ready 简报新版本、Communication 仍为 preparation、无 sent/received。

7. **审计与 Knowledge**
   - 打开审计页，查看候选、患者确认、最终证据、Task、接收、开始、结果、批准、简报事件。
   - 指出 clinical fact evidence 与 workflow AuditEvent 的区别。
   - 打开 Knowledge 页，默认选择 diarrhea。
   - 展示 exact terminology、Claim scope、supports、does_not_support、source locator、`review=not_assessed` 和 CoverageGap。
   - 指出 PRO-CTCAE 未绑定 GLP1，Knowledge 不读取患者数据库、不授权运行时动作。

8. **收尾与重放**
   - 返回首页，展示 `story_complete`。
   - 普通刷新，说明进度从 SQLite 事实恢复，不来自浏览器 session state。
   - 如需第二轮，重新勾选并明确点击“重新开始”；再次确认只留下新一轮 candidate 起点。

## 失败恢复路径

- **开始失败**：页面显示稳定错误，旧故事不会被 staging 半成品替换；再次明确点击即可重试。
- **旧标签页提示 generation 已变化**：刷新页面，从当前数据库恢复正确故事后再操作。
- **按钮快速重复点击**：M5-A/B/C 的幂等与 CAS 返回既有资源或要求刷新，不创建重复资源。
- **简报显示陈旧**：不要口头忽略；明确点击“生成 / 刷新为当前来源版本”。
- **Knowledge CURRENT 不可用**：诚实说明独立 registry 暂不可用；不把临床故事事实改写为未完成或已完成。
- **演示需要完全重来**：只在首页勾选重置确认并明确重新开始；页面加载不会自动重置。

## 演示禁语

- 不说“已接入医院/EMR/真实飞书/Aily”；
- 不说“真实模型已识别患者风险”；
- 不说“系统诊断为腹泻”或“自动分诊/自动上报”；
- 不说“消息已发送给患者/医生”；
- 不说 ready-to-send 等于 sent；
- 不说 `not_assessed` 是低风险或安全；
- 不说已通过医院级 FHIR Profile/术语/临床审批；
- 不说 PRO-CTCAE 已绑定 GLP1；
- 不引用真实患者、真实临床效果或未经验证的模型准确率；
- 不把 Knowledge Claim、CoverageGap 或 AuditEvent 当成患者临床事实。

## 固定收尾句

> 这条 Demo 证明的是：合成患者原话可以在人工门禁下形成可追溯、可版本化、可审计、可回放的复诊上下文。它没有证明真实医院集成、临床效果、自动风险分级或实际消息发送；这些仍需要后续临床、术语、权限和集成验证。
