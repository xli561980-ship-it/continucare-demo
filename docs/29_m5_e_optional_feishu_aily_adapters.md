# M5-E 可选飞书 / Aily 适配器与无 Token Mock fallback

## 1. 冻结结论

M5-E 只实现可注入的外部协议边界、FakeTransport 合同验证和默认离线状态展示。
本轮不使用凭据，不访问真实租户，不发送消息，不写 Bitable，也不把 Aily 输出直接发布为临床事实。

固定状态：

- `mock_fallback_verified=true`；
- `adapter_implemented=true`；
- `contract_tested_with_fake_transport=true`；
- `live_tenant_verified=false`；
- `production_ready=false`。

## 2. 官方事实与未决项

核验日期：2026-08-13。仅使用飞书开放平台与官方 SDK。

| 能力 | 官方事实 | 权限 / 限制 | 本轮结论 |
| --- | --- | --- | --- |
| Tenant token | `POST /open-apis/auth/v3/tenant_access_token/internal/`，返回 token 与 `expire` | `app_id` / `app_secret`；内存缓存并提前刷新 | 实现并仅用 FakeTransport 测试 |
| Bot | `POST /open-apis/im/v1/messages`；`receive_id_type` 与 `receive_id` 必须匹配；卡片 `content` 为 JSON 字符串 | 机器人能力、发布、可用范围、`im:message:send_as_bot`；总 50 QPS / 1000 次每分钟，同一用户或群 5 QPS | 远端接受不等于送达或已读；超时为 `outcome_unknown`，不可盲重试 |
| Bitable | `POST /bitable/v1/apps/:app_token/tables/:table_id/records`；`client_token` 是 UUIDv4 形状的幂等键 | 编辑文档权限；50 QPS；一致性检查默认开启 | 只做默认关闭的合成投影写合同，不实现读取，不成为真相源 |
| Aily | 正式 API 包含 Session、Message、Run；Run 完成后可按 `run_id` 读取消息 | `aily:session:write`、`aily:message:write/read`、`aily:run:write/read`；仅自建应用 | 官方合同不保证 ContinuCare 所需结构化 JSON；协议已实现，但结构化输出真实能力为 `not_verified` |
| 回调 | Webhook 可校验 timestamp + nonce + encrypt key + raw body 的 SHA-256；challenge 需及时返回，事件以 `event_id` 去重 | 还需重放窗口、去重存储与权限设计 | 本轮不实现回调，因此没有 delivery confirmation |

官方链接：

- https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
- https://open.feishu.cn/document/server-docs/im-v1/message/create
- https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create
- https://open.feishu.cn/document/aily-v1/aily_session/create
- https://open.feishu.cn/document/aily-v1/aily_session-aily_message/create
- https://open.feishu.cn/document/aily-v1/aily_session-run/create
- https://open.feishu.cn/document/aily-v1/aily_session-run/get
- https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case

## 3. 配置状态机

每个能力只允许：

- `mock`：默认；不读取 secret、不创建真实 transport、不访问网络；
- `test_tenant`：必须同时满足精确 mode、能力显式 enable、全局 egress enable、完整配置和固定官方 host；
- `disabled`：关闭能力。

没有 `production` 模式。环境中偶然存在凭据不会自动启用。显式选择 `test_tenant`
但配置缺失或非法时 fail closed，只返回缺失 key 名和稳定错误，不回显值。

## 4. Transport、凭据与审计

- 业务服务只依赖 provider-neutral 合同；不引入飞书 SDK。
- `StdlibHttpTransport` 必须持有 factory 生成的显式 egress permit；固定 HTTPS `open.feishu.cn:443`，禁用代理与重定向，TLS 最低 1.2，限制响应大小并设置 timeout。
- 所有动态路径段经过严格字符集验证和百分号编码。
- FakeTransport 保存脱敏请求副本；Authorization、app secret、token 和配置 secret 不进入 repr、异常、状态、日志、审计或 UI。
- token 只存在内存，按服务返回的有效期记录并提前 180 秒刷新。
- 外部尝试若未来接入业务动作，必须写 append-only AuditEvent；`outcome_unknown` 是终态且没有重试入口。M5-E 没有把发送器接入 manual-review Communication，因此验收数据库不会出现 external attempt。

## 5. Aily 安全边界

Aily 只可提出不含 code 的结构化候选。解析采用全量 `extra=forbid` 合同；未知、诊断、风险、治疗、审批或优先级字段使整个响应失败，不接受部分结果。只有 completed Run 的唯一、completed assistant 消息可被解析；流式、失败、取消、未知状态、多义消息或非 JSON 全部回退本地 deterministic semantic Mock。

候选的 coding 只从当前锁定 Questionnaire / 本地 terminology catalog 重新绑定，随后仍经过现有 Safety Agent、患者确认和第二层 FHIR 构造。Aily 不能创建 Observation、Task、诊断、风险等级、建议或患者确认。

## 6. Bot、Communication 与 Bitable

- 卡片渲染与发送分离，模板只含合成、非诊断性、最小信息；假 receive_id 只用于 FakeTransport 测试。
- 状态区分 `rendered / external_request_prepared / external_attempted / accepted_by_remote / delivery_confirmed / failed / outcome_unknown`。本轮 `delivery_confirmed` 恒为 false。
- `SEND_ENABLED=False` 不变；manual-review Communication 始终 `preparation`，没有 `sent` / `received`，UI 没有发送按钮。
- Bitable 只有写入合成投影的合同，不实现读取；SQLite/FHIR 继续是唯一权威源。幂等键为 HMAC 派生后设置 UUIDv4 version/variant 位的稳定值，输入不直接暴露患者 ID。

## 7. UI 与 M5-D 回归

所有页面从同一只读状态 factory 展示：

```text
飞书：Mock fallback / 未进行真实租户联调
Aily：Mock fallback / 未进行真实 API 调用
Bitable：disabled / 未写入外部数据
```

状态读取不创建 client、不取 token、不 health check、不联网。M5-D 九项进度和 `story_complete`
只依赖现有 SQLite 事实；`clinical_rules=[]`、`not_assessed`、Alert=0、approved ClinicalRule=0、
M6 只选 clinical-rule Task、Knowledge 隔离和 manual-review 无发送保持不变。

## 8. 验收与回退

专项测试覆盖配置、零网络、secret、token cache、transport、Bot、Aily、Bitable 和 M5-D 不变量；
随后运行全量 pytest、compileall、diff check、secret 扫描和 Browser 桌面/移动验收。

最终结果：M5-E 专项 `22 passed`；全量 `338 passed, 3 skipped`（三个 skip 仅因未提供官方 FHIR R4 schema archive）；M5-D 专项 `9 passed`。Browser 在桌面和 390×844 上完成六页面及 9/9 故事，console error/warn 为 0；逐页冷加载与 Knowledge 浏览不改变完成后的数据库字节或计数。没有外部审计事件、sent/received Communication、Alert 或 approved ClinicalRule。

本切片没有数据库迁移和外部副作用。回退仅涉及本轮代码、测试与文档；未经用户明确授权不执行回退、暂存、提交或推送。
