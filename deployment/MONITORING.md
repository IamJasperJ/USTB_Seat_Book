# 提醒程序接入：账号密码查询预约结果

核对日期：2026-10-21。完整接口见 [API.md](../docs/API.md)，部署地址与升级见 [DEPLOYMENT.md](DEPLOYMENT.md)。本页只说明查询和外部提醒，不安装定时任务，也不内置通知发送器。

`POST /api/monitor/status` accepts the existing **application account** username
and password directly. No login cookie, extra monitoring token, or prior login
request is required. This is not the school's CAS username/password. The query
does not issue cookies, create app login tokens, or start a school login flow.

```sh
curl --fail-with-body --max-time 50 \
  -H 'Content-Type: application/json' \
  --data '{"username":"YOUR_APP_USERNAME","password":"YOUR_APP_PASSWORD","day":"tomorrow"}' \
  'http://SERVER:18001/api/monitor/status'
```

Use the private VPN address from a trusted client. Do not expose the plain HTTP
port to the Internet or put credentials in URLs. The existing `/seat/` HTTPS
gateway has its own SSO authentication: clients using that gateway must still
supply its existing credentials. No gateway bypass is added. Send the password
only in the JSON body over trusted VPN or HTTPS, never in URL query parameters.
Prefer a protected secret/config file in reminder programs instead of putting
real passwords in shell history. The GET endpoint accepts HTTP Basic account
credentials or an existing application cookie. POST accepts credentials in JSON
and does not require either Cookie or an Authorization header.

`day` accepts `today`, `tomorrow`, or `both` (default). Dates use Asia/Shanghai.
Only the verified account's reservation status is returned, even if a different
account's cookie accompanies the request. Admin credentials also query only the
admin's own bookings. This endpoint is read-only; however the app password has
its normal account permissions, so protect it like any other account password.
POST polling and all HTTP Basic account authentication share a limit of 30
requests per minute per source IP (a reverse proxy can make callers share it).
PBKDF2 runs in a worker thread instead of blocking the event loop. Cache hits
still require authentication and count toward this limit.

The response contains `account`, `days`, a safe `automation` summary, and the
minimal browser `health` summary. Each selected day includes `target_date`,
`checked_at`, `cached`, `booking_status`, `confirmed`, `has_reservation`, and
`records`. A record includes a stable `event_id`, seat/room names, start/end
times with UTC+08:00 offsets, and its official status bits.

## Notification decisions

- `has_reservation: true`, `confirmed: true`, `booking_status: "booked"`:
  the library's actual reservation list contains a valid upcoming/active
  booking. Send a success notification; persist the booked record's `event_id`
  to avoid duplicate notifications on subsequent polls.
- `has_reservation: false`, `confirmed: true`, `booking_status: "not_booked"`:
  the query completed, with no currently valid booking. Historical finished,
  rejected, or violation records are not counted as a current successful seat.
- `booking_status: "pending"`: a reservation is awaiting approval/agreement or
  payment; it is not counted as confirmed booking success.
- `has_reservation: null`, `confirmed: false`, `booking_status: "unknown"`:
  no conclusion is possible. Inspect `reason`, e.g. `library_login_required`,
  `library_login_expired`, `library_unavailable`, or `query_timeout`. Do not
  treat this as proof that the account has no reservation.

For `both`, the top-level `has_reservation` is true if either day has a booking,
null if neither is true and at least one is unknown, and false otherwise.
Use the individual day when sending date-specific reminders.

Poll every 60 seconds. Snapshots are cached for 30 seconds per account/date;
concurrent callers share queries. Each query is time-bounded, pagination is
bounded to five pages, and incomplete/unknown responses are not reported as
confirmed absence. Polling uses read-only library HTTP requests and existing
credentials; it never navigates the login page or submits a booking. It does
not depend solely on local automation success history.

HTTP 401 means incorrect application username/password (or missing/expired
authentication on GET, which supports both Basic and Cookie). HTTP 429 means the authentication rate limit
was exceeded; wait at least `Retry-After` seconds. HTTP 422 means invalid request
fields or an invalid `day` value. Valid app credentials with expired library login
still returns HTTP 200 with an `unknown` reservation result. Browser health is
independent of credential validity and booking success. No notification sender
is built in; this endpoint is intended for integration with external tools.

## 提醒程序实现要点

1. 从受保护配置读取应用账号密码，通过 POST JSON 请求查询，不把密码放入 URL。
2. 建议每 60 秒查询一次，客户端超时 50 秒；`both` 会串行查询两天，不是整个请求只需 20 秒。
3. 对应日期为 true 时，只对 `booking_state="booked"` 的记录发送成功通知，持久化 `event_id` 去重。
4. `false` 与 `pending` 区分；`null` 为未知，不应发送“没有预约”的确定性通知。
5. HTTP 401 表示应用密码不正确，HTTP 200 里的 `library_login_expired` 表示学校凭证失效，两者不是同一层认证。
6. 429 按 `Retry-After` 等待；网络错误保留上次结果的时间戳，标记过期，不沿用旧结果假装本次已确认。
7. 重启会丢失浏览器学校会话但保留账号数据。重新扫码前返回未知是预期行为，Basic 密码不能代替学校扫码。

监控查询不会自行导航扫码页，也不直接刷新学校认证；独立启用的后台保活/每日任务仍可依其配置运行。响应中 `checked_at` / `served_at` 为 UTC，预约时段带北京时间偏移；按 `target_date` 匹配提醒日期。
