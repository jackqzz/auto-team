# 账号注册追溯接口

本文档说明外部程序如何查询每个账号的注册追溯记录（出口 IP、出口地区、注册方式、注册时间、累计失败次数及最近失败详情）。

数据来源：`webui/webui.db` 的 `register_trace` 表，独立于凭证表 `registered` —— 账号被删除后追溯记录仍保留；「注册失败、从未成功」的账号也有行。

- 服务端点：`webui/app.py` → `GET /api/register-trace`、`POST /api/register-trace/query`、`GET /api/register-trace/{email}`
- 写入端：`webui/db.py` → `record_register_success` / `record_register_failure`，由 `webui/registrar.py`、`webui/auto_loop.py` 在每个注册 run 结束时调用
- 出口探测：`auth_flow.py` → `check_proxy()`（协议流）/ camoufox geoip 捕获（浏览器流，同代理会话探测）

## 1. 鉴权

追溯接口走管理员 token（`/api/` 统一鉴权中间件）。

```bash
# 配置了管理员密码时：先换 token（会话型，有过期时间，401 后重新登录即可）
TOKEN=$(curl -sX POST http://127.0.0.1:8767/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<管理员密码>"}' | jq -r .token)

# 之后每个请求带其中一种头
-H "Authorization: Bearer $TOKEN"
# 或
-H "x-admin-token: $TOKEN"
```

未设置管理员密码时所有接口直接开放，跳过鉴权。

注意：token 是内存态会话（重启即失效、有 TTL），外部程序应在收到 401 时自动重新登录，不要把 token 写死。

## 2. 接口

### 2.1 分页列表 `GET /api/register-trace`

| 参数 | 默认 | 说明 |
|---|---:|---|
| `limit` | 200 | 每页条数，最大 2000 |
| `offset` | 0 | 分页偏移 |
| `email` | `""` | 邮箱子串过滤（如 `gmail.com`） |
| `only_failed` | `false` | `true` 时只返回 `fail_count > 0` 的账号 |

返回 `{ok, items: [...], total}`，按 `COALESCE(registered_at, created_at)` 倒序。

```bash
curl -s "http://127.0.0.1:8767/api/register-trace?limit=500&only_failed=true" \
  -H "Authorization: Bearer $TOKEN"
```

### 2.2 批量查询 `POST /api/register-trace/query`

按邮箱精确批量查，适合外部程序核对自己手里的一批账号。

```bash
curl -sX POST http://127.0.0.1:8767/api/register-trace/query \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"emails":["a@x.com","b@x.com","c@x.com"]}'
```

请求体：`{"emails": ["..."], ...}`，1~10000 个邮箱，自动小写/去空格。

返回：

```json
{
  "ok": true,
  "items": { "a@x.com": { "...": "追溯行" }, "b@x.com": { "...": "..." } },
  "missing": ["c@x.com"]
}
```

- `items`：`email -> 追溯行` 的 map，只包含查到的。
- `missing`：系统里完全没见过的邮箱 —— 既没注册成功也没失败记录。

### 2.3 单账号详情 `GET /api/register-trace/{email}`

```bash
curl -s http://127.0.0.1:8767/api/register-trace/someone@gmail.com \
  -H "Authorization: Bearer $TOKEN"
```

返回 `{ok, item}`；账号和追溯记录都不存在时 `404 {"detail":"not found"}`。

## 3. 字段说明

每条追溯行的字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `email` | str | 账号邮箱（小写） |
| `register_ip` | str | 注册时的出口 IP（代理/直连出口）。探测失败为空串 |
| `register_region` | str | 出口国家码（cloudflare cdn-cgi/trace 的 `loc=`），如 `US`、`JP` |
| `register_mode` | str | `camoufox` / `protocol` / `import` / `""`（未知） |
| `register_timezone` | str | Camoufox geoip 生效时区（IANA 名）；协议注册为空 |
| `register_language` | str | Camoufox geoip 生效语言；协议注册为空 |
| `registered_at` | float\|null | 首次成功注册时间（Unix 秒）。只记最早一次，重跑不覆盖 |
| `last_success_at` | float\|null | 最近一次注册语义 run 成功时间 |
| `fail_count` | int | 累计注册失败次数（见 §5 注意事项） |
| `last_fail_at` | float\|null | 最近一次失败时间 |
| `last_fail_error` | str | 最近一次失败错误（截断 500 字符） |
| `last_fail_category` | str | 失败分类：`network` / `account` / `unknown` 等 |
| `last_run_id` | str | 最近一次 run 的 ID，可对照 `GET /api/runs` 或 `webui/logs/<run_id>.log` |
| `created_at` / `updated_at` | float | 追溯行本身的创建/更新时间 |
| `group_name` | str | 账号当前分组（LEFT JOIN registered，无凭证行时为 `""`） |
| `mail_kind` | str | 邮箱类型（outlook / icloud_relay / ...） |
| `account_status` | str | 账号状态（`active` / `permanently_invalid`；无凭证行时 `""`） |
| `has_credentials` | int | 1 = registered 表里有凭证行；0 = 只有追溯记录（从没成功过/已删除） |

## 4. 使用示例

Python（requests）：

```python
import requests

BASE = "http://127.0.0.1:8767"

s = requests.Session()
tok = s.post(f"{BASE}/api/auth/login", json={"password": "<管理员密码>"}).json()["token"]
s.headers["Authorization"] = f"Bearer {tok}"

# 批量追溯手里的账号
resp = s.post(f"{BASE}/api/register-trace/query",
              json={"emails": ["a@x.com", "b@x.com"]}).json()
for email, t in resp["items"].items():
    print(email, t["register_ip"], t["register_region"],
          t["register_mode"], "失败", t["fail_count"], "次")
for email in resp["missing"]:
    print(email, "系统无记录")

# 拉全量（分页翻完）
offset = 0
while True:
    page = s.get(f"{BASE}/api/register-trace",
                 params={"limit": 2000, "offset": offset}).json()
    for item in page["items"]:
        ...
    offset += len(page["items"])
    if offset >= page["total"]:
        break
```

## 5. 注意事项（语义边界）

- **`register_mode` 是注册事实，不是最近一次 run 的方式。** 仅登录（`login_only`）、两段式 RT 第二段、以及服务端识别为已有账号后转登录链的 run 都不会改写它 —— camoufox 注册的号再跑协议登录不会被标成 protocol。
- **`fail_count` 只累计注册语义的失败**，登录/凭证刷新失败不计。但**历史回填**（`init_db` 从 `runs` 表按 email 汇总）分不出注册/登录 run，老账号的回填值可能含登录失败，偏上限；如需精确可从零重计：
  ```sql
  UPDATE register_trace SET fail_count=0, last_fail_at=NULL,
         last_fail_error='', last_fail_category='';
  ```
- **`registered_at` ≠ `registered.created_at`。** 后者是凭证行最后一次落库时间（重跑会刷新）；追溯表里的 `registered_at` 是首次注册时间，只设一次。导入的账号（`register_mode=import`）没有真实注册时间，以导入时刻近似。
- **`register_ip`/`register_region` 只在真注册 run 落值**；探测失败或已有账号登录性质的 run 会留空/保留旧值。camoufox 注册用与浏览器同代理的协议会话探测，代表浏览器出口。
- **重试语义**：自动任务里同一账号的每次重试失败各计一次 `fail_count`，最终成功后计数保留（不清零）—— `fail_count` 表达的是「注册过程中失败过几次」，可用于质量筛选。
- **非池化邮箱源**（如临时域名生成的邮箱）在失败时若真实邮箱还没产生，记录会落在占位邮箱名下，与 `runs.email` 行为一致。
- `last_run_id` 对应的 run 日志在 `webui/logs/<run_id>.log`，`GET /api/runs` 可查 run 的 status/error 摘要。
