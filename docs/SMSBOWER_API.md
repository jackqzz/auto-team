# SmsBower 接码 API 与项目调用流程

本文档按当前项目实现整理，代码来源：

- `sms_provider.py`：SmsBower 租号、查码、状态变更、释放与复用
- `auth_flow.py`：OpenAI 手机发码、重发及验证码校验
- `webui/app.py`：WebUI 余额测试和国家库存查询

## 1. 基础协议

SmsBower 使用兼容 sms-activate 的 HTTP GET 接口：

```text
https://smsbower.page/stubs/handler_api.php
```

所有 SmsBower 请求均为 GET，请求参数放在 query string，并自动附带：

```text
api_key=<SMSBOWER_API_KEY>
```

通用请求示例：

```bash
curl -G 'https://smsbower.page/stubs/handler_api.php' \
  --data-urlencode 'api_key=YOUR_API_KEY' \
  --data-urlencode 'action=getBalance'
```

注意：API Key 会出现在 URL、代理日志和访问日志中，不要记录完整请求 URL。

项目默认参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `service` | `dr` | OpenAI 服务代码 |
| `country` | `52` | 泰国 |
| 请求超时 | 30 秒 | SmsBower 单次 HTTP 请求 |
| 手机号生命周期 | 20 分钟 | 本项目的本地复用窗口 |

## 2. SmsBower 接口清单

### 2.1 查询余额 `getBalance`

```http
GET /stubs/handler_api.php?action=getBalance&api_key=...
```

成功响应为纯文本：

```text
ACCESS_BALANCE:12.34
```

项目取冒号后的值并转换为浮点数。WebUI 的“测试接码配置”调用此接口。

### 2.2 查询价格和库存 `getPrices`

```http
GET /stubs/handler_api.php?action=getPrices&service=dr&country=52&api_key=...
```

参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `service` | 否 | 服务代码，OpenAI 为 `dr` |
| `country` | 否 | 国家数字 ID |

响应为 JSON。项目兼容以下库存字段：

- 价格：`cost` 或 `price`
- 库存：`count`、`qty` 或 `available`

该接口也是国家排名接口不可用时的最终回退。

### 2.3 查询国家排名

项目依次尝试两个 action：

```http
GET /stubs/handler_api.php?action=getTopCountriesByServiceRank&service=dr&api_key=...
GET /stubs/handler_api.php?action=getTopCountriesByService&service=dr&api_key=...
```

两个接口都失败或没有有效数据时，回退 `getPrices`。

项目兼容的数据容器：

```json
{"data": {"52": {"price": 1.2, "count": 30}}}
```

也兼容顶层为 `result`、`response`、国家 ID 对象或对象数组。解析后按以下规则排序：

1. 价格从低到高。
2. 同价格时库存从高到低。
3. 自动选择时先要求 `min_stock`，无匹配则降为库存至少 1。
4. 配置了最高价格时排除超价国家。

### 2.4 租号 `getNumberV2`

首选接口：

```http
GET /stubs/handler_api.php?action=getNumberV2&service=dr&country=52&api_key=...
```

可选价格参数：

```text
maxPrice=<最高价格>
```

固定价格模式下，SmsBower 要求：

```text
minPrice=<固定价格>&maxPrice=<固定价格>
```

成功响应示例：

```json
{
  "activationId": "123456789",
  "phoneNumber": "66812345678",
  "countryPhoneCode": "66"
}
```

项目保存 `activationId`，并把号码规范化为 E.164，例如 `+66812345678`。

### 2.5 租号回退 `getNumber`

同一国家的 V2 失败后调用 V1：

```http
GET /stubs/handler_api.php?action=getNumber&service=dr&country=52&api_key=...
```

成功响应是纯文本：

```text
ACCESS_NUMBER:123456789:66812345678
```

项目的租号顺序是：每个候选国家先 `getNumberV2`，失败后 `getNumber`，再尝试下一个国家。

### 2.6 标记已触发发码 `setStatus=1`

OpenAI 的 `add-phone/send` 成功后调用：

```http
GET /stubs/handler_api.php?action=setStatus&id=<activation_id>&status=1&api_key=...
```

该调用失败不会中断主流程。

### 2.7 查询验证码 `getStatusV2`

轮询时优先调用：

```http
GET /stubs/handler_api.php?action=getStatusV2&id=<activation_id>&api_key=...
```

项目支持：

- 响应本身为状态字符串。
- JSON 的 `status` 字段为状态字符串。
- JSON 的 `sms.code`。
- JSON 的 `call.code`。

收到 code 后项目生成 `SHA256(activation_id + ':' + code)` 作为本地去重键。

### 2.8 查询验证码回退 `getStatus`

每轮 V2 后还会调用 V1：

```http
GET /stubs/handler_api.php?action=getStatus&id=<activation_id>&api_key=...
```

常见纯文本响应：

| 响应 | 项目解释 |
|---|---|
| `STATUS_WAIT_CODE` | 等待验证码 |
| `STATUS_WAIT_RETRY...` | 等待重试 |
| `STATUS_WAIT_RESEND` | 等待重新发码 |
| `STATUS_OK:123456` | 已收到验证码 |
| `STATUS_CANCEL` | 激活已取消 |

未识别的响应会标记为 `unknown`，下一轮继续查询。

### 2.9 请求再次接收短信 `setStatus=3`

```http
GET /stubs/handler_api.php?action=setStatus&id=<activation_id>&status=3&api_key=...
```

本项目把它与 OpenAI 的 resend 同步调用。默认每 20 秒最多触发一次；即使 OpenAI resend 回调不可用，也会间歇调用 SmsBower 的 `status=3`。

### 2.10 完成激活 `finishActivation`

业务侧验证码验证成功并且不再复用号码时调用：

```http
GET /stubs/handler_api.php?action=finishActivation&id=<activation_id>&api_key=...
```

项目接受 HTTP 200、204 或响应包含 `ACCESS`。接口失败时回退：

```http
GET /stubs/handler_api.php?action=setStatus&id=<activation_id>&status=6&api_key=...
```

启用号码复用时，不一定立刻完成激活。达到复用次数上限或号码接近过期后才完成。

### 2.11 取消激活 `cancelActivation`

流程超时或异常清理时首选：

```http
GET /stubs/handler_api.php?action=cancelActivation&id=<activation_id>&api_key=...
```

项目接受 HTTP 204 或响应包含 `ACCESS_CANCEL`。失败时回退：

```http
GET /stubs/handler_api.php?action=setStatus&id=<activation_id>&status=8&api_key=...
```

OpenAI 在发码前拒绝号码时，项目直接使用 `setStatus=8` 尝试取消退款，并清除本地复用缓存。

## 3. 配套的 OpenAI 手机验证接口

这些不是 SmsBower 接口，但构成完整接码业务链。必须使用当前 OpenAI 授权流程的同一 HTTP Session、Cookie、device ID 和认证状态，不能单独无状态调用。

通用请求头包括：

```http
Accept: application/json
Content-Type: application/json
Origin: https://auth.openai.com
oai-device-id: <device_id>
Cookie: <当前授权会话 Cookie>
```

### 3.1 让 OpenAI 向租用号码发码

```http
POST https://auth.openai.com/api/accounts/add-phone/send
Referer: https://auth.openai.com/add-phone
Content-Type: application/json

{"phone_number":"+66812345678"}
```

HTTP 200 后，项目调用 SmsBower `setStatus=1`。常见拒绝原因包括号码已使用、号码不允许、号码可疑及手机验证频控；此时调用 SmsBower `setStatus=8` 取消。

### 3.2 请求 OpenAI 重发手机验证码

```http
POST https://auth.openai.com/api/accounts/phone-otp/resend
Referer: https://auth.openai.com/phone-verification
```

无 JSON body。项目等待期间默认每 20 秒调用一次，最多 3 次；每次同时调用 SmsBower `setStatus=3`。

### 3.3 向 OpenAI 提交收到的验证码

```http
POST https://auth.openai.com/api/accounts/phone-otp/validate
Referer: https://auth.openai.com/phone-verification
Content-Type: application/json

{"code":"123456"}
```

HTTP 200 表示验证码通过。之后调用 SmsBower 完成/复用逻辑。验证失败时：

1. 本地记录该验证码，防止重复提交。
2. 调用 OpenAI `phone-otp/resend`。
3. 调用 SmsBower `setStatus=3`。
4. 在同一号码的剩余时间内等待下一条验证码。

## 4. 项目实际调用时序

```text
getBalance                         仅配置测试时调用
getTopCountries... / getPrices    可选：自动选择国家
getNumberV2
  └─失败→ getNumber
OpenAI add-phone/send
  ├─失败→ setStatus=8（取消）→换号
  └─成功→ setStatus=1
循环等待：
  getStatusV2
  getStatus
  每 20 秒：
    OpenAI phone-otp/resend
    setStatus=3
收到 code：
  OpenAI phone-otp/validate
  ├─失败→记录旧 code→resend→继续等
  └─成功→finishActivation / 保留复用
超时或流程中断：
  cancelActivation
  └─失败→setStatus=8
```

## 5. 重试、超时和复用规则

当前默认业务策略：

| 项目 | 默认值 |
|---|---:|
| 单号等待窗口 | 80 秒 |
| 最多换号次数 | 3 |
| 单号验证码验证重试 | 2 次 |
| SmsBower 状态轮询间隔 | 3 秒 |
| OpenAI resend 间隔 | 20 秒 |
| OpenAI resend 上限 | 3 次 |
| 本地号码生命周期 | 20 分钟 |
| 默认号码成功复用上限 | 3 次（由配置决定） |

复用缓存保存在：

```text
data/.smsbower_phone_cache.json
```

缓存包含 API Key 的 SHA-256，而不是明文 Key，同时保存 service、country、activation ID、号码、租用时间、使用次数和已经提交过的验证码。以下情况停止复用：

- 用户关闭号码复用。
- 达到 `sms_phone_success_max`。
- 距 20 分钟生命周期结束不足 30 秒。
- OpenAI 拒绝该号码。
- 激活被取消或缓存身份不匹配。

项目用全局锁串行化同一复用号码的验证流程，避免多个注册线程同时消费同一个号码或验证码。

## 6. 最小 Python 调用示例

```python
import time
import requests

BASE_URL = "https://smsbower.page/stubs/handler_api.php"
API_KEY = "YOUR_API_KEY"


def call(action, **params):
    response = requests.get(
        BASE_URL,
        params={"api_key": API_KEY, "action": action, **params},
        timeout=30,
    )
    response.raise_for_status()
    return response


# 1. 租号
info = call("getNumberV2", service="dr", country="52").json()
activation_id = str(info["activationId"])
phone = str(info["phoneNumber"])

# 2. 业务侧向 phone 发出短信后通知平台
call("setStatus", id=activation_id, status=1)

# 3. 轮询验证码
deadline = time.time() + 80
code = ""
while time.time() < deadline:
    text = call("getStatus", id=activation_id).text.strip()
    if text.startswith("STATUS_OK:"):
        code = text.split(":", 1)[1]
        break
    time.sleep(3)

# 4. 业务侧验证 code 后，按结果完成或取消
if code:
    call("finishActivation", id=activation_id)
else:
    call("cancelActivation", id=activation_id)
```

这个最小示例没有实现 V2/V1 回退、OpenAI resend、验证码去重、号码复用和异常清理；生产移植时应按前述完整时序补齐。

## 7. 配置字段对应关系

| WebUI/配置字段 | Provider 行为 |
|---|---|
| `sms_api_key` | 所有 SmsBower 请求的 `api_key` |
| `sms_service` | `service`，默认 `dr` |
| `sms_country` | `country`，默认 `52` |
| `sms_max_price` | 租号的 `maxPrice` |
| `sms_fixed_price` | SmsBower 的 `minPrice=maxPrice` |
| `sms_reuse_phone` | 是否复用号码 |
| `sms_phone_success_max` | 单号成功复用上限 |
| `sms_auto_country` | 是否查询价格库存后自动选国 |
| `sms_allowed_countries` | 自动选择的候选国家列表 |
| `sms_auto_min_stock` | 自动选国最低库存 |
| `sms_auto_max_price` | 自动选国最高价格 |
| `sms_strict_whitelist` | 只使用项目确认适合 OpenAI SMS 的国家 |
| `sms_max_phone_attempts` | 最大换号次数 |
| `sms_per_phone_timeout` | 单号等待窗口 |

截至当前项目实测，OpenAI 纯 SMS 路径只将国家 ID `52`（泰国）列入安全白名单；其他国家可能进入 WhatsApp 验证而无法收到普通短信。
