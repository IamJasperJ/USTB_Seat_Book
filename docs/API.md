# 图书馆预约服务 API

文档核对日期：2026-10-21，对应当前仓库的控制接口修复版。所有示例账号、密码、会话、日期和座位均为占位或演示值，不是实际预约记录。相关说明：[配置](CONFIGURATION.md) · [部署](../deployment/DEPLOYMENT.md) · [提醒接入](../deployment/MONITORING.md) · [健康检查](../deployment/HEALTH.md)。

## 1. 地址与认证

- txyun VPN 地址：`http://10.11.12.9:18001`，调用设备必须能访问该 VPN。
- 在线交互文档：`/docs`；OpenAPI 定义：`/openapi.json`。
- 上述地址和 txyun 镜像基线最近于 2026-10-20 验证；文档更新不是实时连通性检查。反向代理子路径下的交互文档可能还需要代理正确处理 OpenAPI 路径，直接 VPN 地址不需要该子路径改写。
- 原有 HTTPS 反向代理以 `/seat/` 为前缀，例如 `/seat/api/overview`；代理自己的 SSO 验证保持不变，应用账号密码不能绕过外层 SSO。
- 明文 HTTP 仅用于受信任的 VPN/本机连接。公网调用应使用 HTTPS；不要把密码放入 URL、日志、代码仓库或公开的命令历史。

### HTTP Basic（推荐脚本使用）

绝大部分账号接口均支持 HTTP Basic：

```bash
curl --max-time 50 -u 'APP_USERNAME:APP_PASSWORD' \
  'http://10.11.12.9:18001/api/overview'
```

这是**本项目应用账号密码**，不是学校统一认证账号密码。每次校验，不创建应用登录会话、不下发 Cookie、不需要新增令牌。学校扫码登录仍用于取得访问图书馆的凭证，不能由应用密码代替。

已有应用登录 Cookie 仍兼容；不要在同一个请求中混用不同账号的 Cookie 和 Basic，当前实现优先使用有效 Cookie。普通账号只能访问自己拥有的图书馆会话；管理员专用接口另有权限检查。

Basic 和下述 POST 预约状态接口共用认证限流：同一来源 IP 每 60 秒最多 30 次，超过返回 HTTP 429 和 `Retry-After: 60`。经过代理时，多人可能共享来源 IP。建议提醒程序每 60 秒查询一次。

Basic 本质是每次发送账号密码的编码形式，不是加密，也不创建额外只读令牌。密码校验在线程中运行；账号密码仍有该账号正常权限，不因请求某个只读接口就变成低权限密钥。交互文档把 Basic 作为 `Authorization` 请求头展示，填写 `Basic <用户名:密码的Base64>`；直接使用 `curl -u` 不需要手工编码。

## 2. 预约结果监控：不需要 Cookie

### `POST /api/monitor/status`

无需 Basic 头或 Cookie，在 JSON 中提交账号密码：

```bash
curl --max-time 50 -X POST 'http://10.11.12.9:18001/api/monitor/status' \
  -H 'Content-Type: application/json' \
  --data '{"username":"APP_USERNAME","password":"APP_PASSWORD","day":"tomorrow"}'
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `username` | string，必填 | 应用账号 |
| `password` | string，必填 | 应用密码 |
| `day` | string | `today`、`tomorrow`、`both`；默认 `both` |

也支持 `GET /api/monitor/status?day=tomorrow`，使用 Basic 或已有应用 Cookie 认证。

简化响应示例（示例日期、座位，不是实时查询结果）：

```json
{
  "account": "APP_USERNAME",
  "timezone": "Asia/Shanghai",
  "day": "tomorrow",
  "has_reservation": true,
  "days": {
    "tomorrow": {
      "target_date": "2026-10-22",
      "booking_status": "booked",
      "has_reservation": true,
      "confirmed": true,
      "reason": null,
      "cached": false,
      "records": [{
        "event_id": "STABLE_EVENT_HASH",
        "begin_time": "2026-10-22T08:00:00+08:00",
        "end_time": "2026-10-22T12:00:00+08:00",
        "status": 2,
        "booking_state": "booked",
        "devices": [{"name":"F4B114","room":"图西楼四楼北区"}]
      }]
    }
  },
  "cache_seconds": 30
}
```

实际响应另有 `checked_at`、`served_at`、`automation`、`health` 等字段。

**提醒判断必须区分三态：**

- `has_reservation=true`：已从图书馆实际记录确认有效预约。发送成功提醒时，保存 `booking_state="booked"` 记录的 `event_id` 去重。
- `has_reservation=false` 且 `confirmed=true`：已完成查询，没有有效预约。`booking_status="pending"` 表示等待审核、同意或付款等，不能作为成功。
- `has_reservation=null` 且 `confirmed=false`：无法确认，不等于没有预约。查看 `reason`，例如 `library_login_required`、`library_login_expired`、`library_unavailable`、`query_timeout`。

`both` 的顶层结果：任一天为 true 则 true；否则任一天未知则 null；两天都确认无预约才是 false。日期明确的提醒应使用 `days.today` 或 `days.tomorrow`，不要只看顶层。

查询使用已有图书馆凭证，不触发扫码、浏览器跳转或预约提交。缓存 30 秒，并发查询会合并；翻页、等待和网络请求均有上限。不依赖本地自动任务成功日志，也不会因为查询而创建新的登录令牌。

每账号/日期最多读取 5 页，每页 20 条；确认存在有效记录时可提前返回，`complete=false` 表示记录明细未穷尽，不影响已找到预约的存在性证明。未找到有效预约且结果不完整时返回 unknown，不能断言未预约。`both` 串行查两天，单日远端查询限时 20 秒，另有锁等待与客户端清理上限，调用方建议设置 50 秒超时。`checked_at` 是数据查询时间，缓存命中不应视为本次重新查询；`served_at` 是本次响应时间。

常见 `reason` 包括 `library_login_required`、`library_login_expired`、`library_unavailable`、`library_response_error`、`query_timeout`。记录归属/日期不匹配、分页不一致或未知状态也会返回 unknown。POST 始终验证 JSON 中的账号，不会因携带其他账号 Cookie 而切换查询对象；GET/Basic 的 Cookie 优先规则不同。

## 3. 状态总览

### `GET /api/overview`

参数：`auth_session_id`（可选）、`include_reservations`（布尔，默认 true）。不指定会话时优先选择本账号自动任务绑定的已认证会话，其次选择最新的已认证会话。

返回：`account`、`now`、`config`、`sessions`、`automation`、`daily_automation`、`session`、`ready`、`library`；开启明细时另有 `reservations.today/tomorrow`。

- `ready=true`、`library.confirmed=true`：本次实际访问图书馆并验证成功，不保证某个位置一定可约，也不代表提交一定成功。
- 无会话时：`ready=false`，`hint` 提示先扫码。
- 登录过期、服务不可用或超时时：仍可返回 HTTP 200，但 `ready=false`，具体查看 `library.reason`。
- `include_reservations=false` 不返回明细，但仍会做一次只读验证，不能只相信缓存的认证状态。
- 远端验证整体限时 20 秒，随后另有最多 2 秒客户端清理；查询失败的记录为 `null`，不是空列表。成功获取的部分结果会保留。

总览的预约明细只读取每一天的第一页。需要翻页使用 `/api/reservations/mine`；只判断是否预约成功，优先使用监控接口。

`library.reason` 可为 `library_login_required`、`library_login_expired`、`library_unavailable`、`library_response_error`、`query_timeout`。局部失败时保留成功获取的日期记录，失败项为 `records=null`。`session.status` 等字段可能是缓存值，应优先用 `ready` 与本次 `library` 判断是否已完成验证；它们不是预约成功标志。

## 4. 学校扫码登录与会话

以下均使用 Basic 或已有应用 Cookie；`session_id` 与 `auth_session_id` 指同一个图书馆浏览器会话标识，不是密码或应用登录 Cookie。

| 方法 | 路径 | 请求及作用 |
| --- | --- | --- |
| POST | `/api/auth/sessions` | 无请求体；按账号配置的 `login_source` 创建扫码会话 |
| GET | `/api/auth/sessions` | 列出自己的会话及容量限制 |
| GET | `/api/auth/sessions/{session_id}/qr` | 返回 `image/png` 二维码 |
| GET | `/api/auth/sessions/{session_id}` | 查询扫码、SSO、图书馆认证状态 |
| DELETE | `/api/auth/sessions/{session_id}` | 关闭自己的会话；若绑定自动任务，会停止相应任务 |

创建响应有 `session_id`、`status`、`qr_url`、`status_url`。扫码状态中 `authenticated` 表示已建立图书馆会话，`sso_authenticated` 仅代表学校 SSO 已登录，还不等于图书馆凭证已可用。

```bash
curl -u 'APP_USERNAME:APP_PASSWORD' -X POST 'http://10.11.12.9:18001/api/auth/sessions'
curl -u 'APP_USERNAME:APP_PASSWORD' \
  'http://10.11.12.9:18001/api/auth/sessions/SESSION_ID/qr' -o qr.png
```

二维码有有效期，请及时扫码。服务重启后浏览器会话不保留，需要重新扫码；账号和配置保存在数据库中。

## 5. 楼层、区域、座位与时间

先通过区域接口获取真实 `room_id`，不要自行猜测 ID。

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| GET | `/api/areas` | 必填 `auth_session_id`；返回楼层/区域树 |
| GET | `/api/rooms/{room_id}/availability` | 必填 `auth_session_id`；可选 `date`、`only_free=false` |
| GET | `/api/rooms/{room_id}/seats/{seat}/slots` | 必填 `auth_session_id`；可选 `date`、`begin=08:00` |

`date` 默认 `tomorrow`，接受 `today`、`tomorrow`、`dayafter` 或 `YYYY-MM-DD`，查询限北京时间今天到后天。`seat` 可以是座位名或设备 ID。

区域响应有 `target_date`、`total`、`listed`、`seats`。座位响应有 `bookable`、`time_options`、`free_time_options`、`reservations` 等；单座位接口带 `begin` 时另有图书馆的 `tips`。

- `bookable` 只表示座位开放且设备状态正常，不是预约成功保证。
- `time_options` 来自开放时间和预约规则；`free_time_options` 进一步扣除已有预约冲突。
- `free_time_options=null` 表示已有预约无法可靠解析，不应当视为全天空闲。
- 数据可能在查询后发生变化，提交时仍需让图书馆做最终校验。

## 6. 指定座位预约

### `POST /api/reservations`

Basic 或已有应用 Cookie 认证，JSON 请求体：

```json
{
  "auth_session_id": "SESSION_ID",
  "room_id": 102057443,
  "seat": "F4B114",
  "date": "tomorrow",
  "start": "08:00",
  "end": "12:00",
  "dry_run": true,
  "confirm": false
}
```

`auth_session_id`、`seat`、`start`、`end` 必填；`room_id` 省略时使用账号配置；`date` 默认明天；`dry_run`、`confirm` 默认 false。

- 日期只能是今天或明天；时间格式 `HH:MM`，必须在座位实际可选范围内。
- `dry_run=true` 只查询和校验，不提交，不要求开启提交开关或确认。
- 真正提交必须同时满足账号配置 `submit_enabled=true`、请求 `dry_run=false` 和 `confirm=true`。
- 会重新查询座位并检查开放状态、时间范围、已有预约冲突；只调用一次提交。
- HTTP 200 不等于预约成功，必须检查 `success=true`、`code=0`。遇到请求超时，不要立即反复提交；先查询自己的预约确认是否已成功。

## 7. 本人预约明细与取消

### `GET /api/reservations/mine`

必填 `auth_session_id`；可选 `day=today|tomorrow`（默认 tomorrow）、`page`（默认 1）。返回 `records`、`count`、`page`、`page_size=20`、`target_date`。记录包含取消所需的 `uuid`、时间、座位和 `cancellable`。

### `POST /api/reservations/cancel`

```json
{
  "auth_session_id": "SESSION_ID",
  "uuid": "UUID_FROM_MY_RESERVATIONS",
  "day": "tomorrow",
  "page": 1,
  "confirm": true
}
```

`day` 必填，`page` 需与取得该记录时的页码一致。服务会先重新查询确认记录属于该账号并可取消，再发送取消请求。成功返回 `success=true`；不能取消时通常返回 409。

## 8. 配置和自动预约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/config` | 当前账号配置 |
| PUT | `/api/config` | JSON 局部修改，只提交要改的字段 |
| GET | `/api/automation` | 当前单次任务和每日模式状态 |
| GET | `/api/automation/history` | 历史任务；`page=1`、`page_size=10`，最大每页 50 |
| POST | `/api/automation/start` | JSON `{"auth_session_id":"SESSION_ID"}`；按已保存配置启动单次任务 |
| POST | `/api/automation/stop` | 停止当前任务；每日模式开启时一并关闭 |
| POST | `/api/automation/daily/start` | JSON `{"auth_session_id":"SESSION_ID"}`；开启每日模式 |
| POST | `/api/automation/daily/stop` | 关闭每日模式，并停止正在运行的每日任务 |

常用配置示例（区域和座位仅为示例，请先查询）：

```json
{
  "room_id": 102057443,
  "room_name": "图西楼四楼北区",
  "seat_names": ["F4B114"],
  "target_date": "2026-10-23",
  "start_time": "08:00",
  "end_time": "12:00",
  "auto_trigger_time": "06:00:00",
  "auto_retry_interval_seconds": 30,
  "keepalive_enabled": true,
  "keepalive_interval_seconds": 300,
  "submit_enabled": true
}
```

触发时间按北京时间，`06:00:00` 只是配置示例，不保证图书馆此时开机。单次任务使用已保存的目标日期；每日模式按触发日期的次日计算目标日期。不要把配置示例中的固定日期直接用于长期任务。开启每日模式还需要允许提交和保活；已有单次任务时需先停止。

`PUT /api/config` 的可写字段与默认值见[配置参考](CONFIGURATION.md)。`daily_auto_enabled` 不是普通配置接口的可写字段，应通过每日 start/stop 接口切换；修改参数不改写已启动任务快照。普通单次任务启动需要提交开关、有效学校会话；夜间仅有学校 SSO 时还需保活。服务重启不会自动恢复单次提交，每日开关保留但需要重新扫码并遵守历史任务去重规则。

## 9. 健康、运行状态与日志

| 方法 | 路径 | 认证与含义 |
| --- | --- | --- |
| GET | `/healthz` | 无应用认证；200 健康/短暂忙，503 异常 |
| GET | `/api/health` | Basic/Cookie；浏览器详细健康状态；看响应里的 `healthy` |
| GET | `/api/status` | Basic/Cookie；运行和任务状态；管理员有额外资源信息 |
| GET | `/api/logs?limit=200` | 仅管理员，最多 500 条内存中的运行日志 |

浏览器健康不等于学校凭证有效，也不等于预约成功。健康检查不自动重启容器或关闭登录会话。

## 10. 常见错误与调用约定

| HTTP 状态 | 含义 |
| --- | --- |
| 200 | 请求已处理，仍需检查业务字段；可能是预约失败或查询 unknown |
| 400 | 缺少明确提交/取消确认等业务参数错误 |
| 401 | 应用认证失败，或某些图书馆接口凭证无效；结合响应 `detail` 判断 |
| 403 | 没有管理员权限，或提交开关未开启 |
| 404 | 座位/会话/记录不存在，或会话不属于当前账号 |
| 409 | 会话容量、任务状态、座位冲突等，不应盲目重试 |
| 422 | 日期、字段或时间参数不合法 |
| 429 | 认证限流，按 `Retry-After` 等待 |
| 502/503 | 上游或浏览器服务异常；监控/总览也可能通过 HTTP 200 的结构化状态表达异常 |

不要高频并发提交。提醒程序应持久化成功记录的 `event_id` 去重；失效登录应单独通知，而不是发送“没抢到座位”。接口返回的已登录用户信息不包含学校认证 token，但应用密码本身有账号权限，需作为敏感信息保护。

## 11. 应用账号与兼容接口

| 方法 | 路径 | 请求与说明 |
| --- | --- | --- |
| POST | `/api/account/register` | JSON `username`、`password`；无需已有应用认证，首个用户为管理员；默认每来源 IP 每小时最多 5 次注册尝试 |
| POST | `/api/account/login` | JSON `username`、`password`；校验成功下发 `session_token` HttpOnly Cookie |
| POST | `/api/account/logout` | 撤销随请求携带的 Cookie 令牌，不禁用 Basic 账号密码，也不关闭学校浏览器会话 |
| GET | `/api/account/me` | Basic/Cookie，返回应用账号与管理员标记 |
| GET | `/api/seats` | 必填 `auth_session_id`；按已保存日期和候选座位查询 |
| GET | `/api/rooms/{room_id}/seats` | 必填 `auth_session_id`；按账号已保存日期返回区域全部座位 |
| POST | `/api/reservations/submit` | JSON `auth_session_id`、`confirm=true`；按保存的候选座位和时段提交，需 `submit_enabled=true` |

注册用户名为 3–32 位字母、数字或下划线，密码至少 8 位。已有账号不要再次注册。登录/注册创建 Cookie 的行为与无 Cookie 的 Basic、POST 监控不同；学校扫码与应用账号登录又是两回事。

## 12. 管理员接口

以下均要求应用管理员 Basic 或 Cookie，普通用户返回 403。`user_id` 是应用账号 ID，不是学校学号；账号列表返回该 ID。修改个人设置只作用于后续任务，已运行任务保留快照；关闭每日开关等操作会按实现停止相应每日任务。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/admin/users` | 用户列表、会话数、任务与每日模式概况 |
| GET | `/api/admin/users/{user_id}` | 用户参数、任务与会话详情 |
| GET | `/api/admin/users/{user_id}/history` | 分页历史，`page` 默认 1，`page_size` 默认 10、最大 50 |
| PUT | `/api/admin/users/{user_id}/settings` | JSON 局部参数；另支持 `daily_auto_enabled`，不能为空更新 |
| POST | `/api/admin/users/{user_id}/automation/stop` | 停任务并关闭每日模式，避免立即被调度重建 |
| DELETE | `/api/admin/users/{user_id}/sessions/{session_id}` | 关闭该用户会话，必要时停止绑定任务 |
| POST | `/api/admin/users/{user_id}/password` | JSON `{"password":"NEW_PASSWORD"}`；撤销该账号全部应用 Cookie，Basic 后续必须使用新密码 |
| PUT | `/api/admin/users/{user_id}/role` | JSON `{"is_admin":true}` 或 false；不能修改自身角色，至少保留一名管理员 |
| DELETE | `/api/admin/users/{user_id}` | 不能删除自己；停止任务、关闭会话、删除账号，任务历史移至 `logs/automation-history/deleted/` |

管理操作写入审计日志，管理员不能通过上述接口借用另一用户的学校凭证查询或提交图书馆预约。删除用户和重置密码具有实际影响，不应作为接口连通性测试。删除历史是归档而不是擦除，备份和日志另按部署保留策略管理。
