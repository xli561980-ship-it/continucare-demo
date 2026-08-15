# 飞书 / Aily / Bitable 集成状态

## 当前真实状态（M5-E）

M5-E 已实现可选协议适配器、安全配置工厂、内存 Token cache、标准库 HTTPS transport、FakeTransport 和零 Token fallback。本轮没有任何真实 Token、租户认证、health check、Aily 推理、Bot 发送或 Bitable 写入。

| 能力 | 已实现代码 | FakeTransport 测试 | 无 Token fallback | 真实租户验证 | 生产可用 |
|---|---:|---:|---:|---:|---:|
| 飞书 Bot | 是 | 是 | 是（Mock） | 否 | 否 |
| Aily 候选抽取 | 是 | 是 | 是（deterministic semantic Mock） | 否 | 否 |
| Bitable 合成投影 | 是 | 是 | 是（默认 disabled） | 否 | 否 |

必须分别解读这些状态：

- `adapter_implemented=true`：代码合同存在；
- `contract_tested_with_fake_transport=true`：请求/响应只在内存 fake 中验证；
- `mock_fallback_verified=true`：缺 Token 的默认路径保持离线可运行；
- `live_tenant_verified=false`：未验证真实权限、字段、租户数据或投递；
- `production_ready=false`：没有生产模式，也没有生产验收。

## 配置状态机

每个能力只支持 `mock`、`test_tenant`、`disabled`。安全默认：

```dotenv
CONTINUCARE_FEISHU_MODE=mock
CONTINUCARE_AILY_MODE=mock
CONTINUCARE_BITABLE_MODE=disabled
CONTINUCARE_EXTERNAL_EGRESS_ENABLED=false
```

只有 mode 精确为 `test_tenant`，对应 `*_TEST_TENANT_ENABLED=true`、全局 egress flag 为 true 且所有配置非空时，factory 才允许创建外部 client。页面只读取纯配置状态，不会认证、探活或访问网络。环境中偶然存在 Token 不会改变 mode。

`.env.example` 只保留空凭据占位符。Token 和 App Secret 仅由 environment secret provider 在请求边界解析；Token cache 只在内存，按过期时间提前刷新。状态、异常、repr、FakeTransport capture 和审计均不展示 secret 值。

## 协议与边界

### 飞书 Bot

- 卡片先本地渲染，再由独立 notifier 发送；固定模板仅含合成标识和本地不透明引用；
- `rendered`、`external_request_prepared`、`external_attempted`、`accepted_by_remote`、`delivery_confirmed`、`failed`、`outcome_unknown` 分开表达；
- HTTP/API 接收不等于送达或已读，`delivery_confirmed` 在当前合同中始终为 false；
- 远端可能接收后的 timeout/network failure 进入 `outcome_unknown`，不会盲重试或切 Mock 冒充成功；
- notifier 没有接入 M5-B/C Communication。`SEND_ENABLED=False`，患者 Communication 仍为 `preparation`，没有发送按钮。

### Aily

- 接入点是当前 `SemanticModelAdapter.extract(SemanticTask) -> SemanticResult`，不是旧 `AIExtractor`；
- 只允许候选与患者原话证据，不允许 diagnosis/risk/treatment/code 等未知字段；整个响应严格校验，不能部分接受；
- 只读取唯一 completed assistant message；失败、歧义或未知状态回退本地 deterministic semantic Mock，并记录 fallback reason；
- 远端不能提供可信 coding。questionnaire code、choice value 和 quantity UCUM 由本地发布合同重新绑定；
- 结果仍经过本地 Safety Agent 与患者确认门，不能直接形成 Observation、Task、风险或建议；
- 官方 API 存在 session/message/run 合同，但其结构化输出对 ContinuCare Schema 的真实符合性尚未验证，因此状态固定为 `not_verified`。

### Bitable

- 只实现默认关闭、write-only 的合成投影，不读取 Bitable；
- SQLite/FHIR 是唯一权威数据源；外部结果不参与本地事务、CAS、进度或 `story_complete`；
- 最小字段包含 `Synthetic`、投影类型、不透明引用和受限工作流状态，不含患者标识或临床正文；
- `client_token` 是由私有 salt 与本地不透明引用 HMAC 派生、再设置 RFC 4122 UUIDv4 version/variant 位的稳定键；
- timeout/network failure 为 `outcome_unknown`，不会自动重试。真实幂等和一致性仍需测试租户验证。

## Transport

业务服务只依赖 `HttpTransport`。真实 transport 固定 `https://open.feishu.cn/open-apis/`、禁用代理继承与重定向、TLS 最低 1.2、限制 method/timeout/响应大小，并把错误转为不带 URL、正文或凭据的稳定异常。所有自动化协议测试使用 `FakeTransport`，不会创建网络连接。

## M5-D 不变量

M5-D 主线继续显式使用 `UnconfiguredModelAdapter + deterministic semantic Mock`。九项进度和 `story_complete` 只读取 SQLite/FHIR 事实，不依赖 Token、Aily、飞书、Bitable或网络。`clinical_rules=[]`、`not_assessed`、Alert=0、approved ClinicalRule=0、Knowledge 独立只读等边界不变。

## 真实联调前置条件

需要用户另行授权并提供隔离测试租户后，才可进行：

1. 最小权限应用与测试身份/群组；
2. 已审批的数据处理、保留、审计与 incident 流程；
3. 测试租户字段、错误码、rate limit、Aily 输出及 Bitable 幂等验证；
4. Bot 投递/已读语义验证以及安全的人为重试流程；
5. 回调签名、challenge、时间窗口与 event-id replay 防护；
6. 真实 tenant 验收报告、回滚方案与独立安全复核。

详细 API 事实和冻结方案见 [M5-E 设计验收](29_m5_e_optional_feishu_aily_adapters.md)。
