# USTB Seat Book

文档核对日期：2026-10-21；以当前仓库代码为准。最近一次已验证的 txyun 部署是 2026-10-20 的 `txyun-control-fixed-20261020`，这不是实时在线状态声明。

一个面向服务器部署的北京科技大学图书馆座位预约辅助服务，支持多个账号各自独立登录、配置和运行自动预约任务。它使用 FastAPI 提供接口，使用 Playwright 在一个共享的无 UI Chromium 进程中为每次登录创建一个轻量浏览器上下文。二维码优先获取站点原始 PNG，无法获取时才截图；用户用微信扫码后，程序复用同一个上下文获取登录态，再查询座位或提交预约。

## 文档导航

- [接口文档](docs/API.md)：账号密码认证、查询、扫码、选座、预约、取消、自动任务和管理员接口。
- [部署与升级](deployment/DEPLOYMENT.md)：Python/uv、Docker、当前 txyun 配置、备份、回滚和排障。
- [配置参考](docs/CONFIGURATION.md)：部署级与账号级参数、默认值、日期和持久化规则。
- [提醒程序接入](deployment/MONITORING.md)：免 Cookie 账号密码查询、三态判断和通知去重。
- [健康检查与内存保护](deployment/HEALTH.md)：浏览器探测、API、OOM 优先级和日志限制。
- [Redmi ARM64 迁移历史](deployment/REDMI-MIGRATION.md)：历史归档，不是当前生产部署指南。

默认从 `jwgl.ustb.edu.cn` 进入学校统一登录。扫码完成后，程序会在同一个浏览器上下文中请求图书馆的登录地址，等待图书馆签发自己的 token。学校登录成功不等于图书馆会话已经可用；只有状态变为 `authenticated` 后才能查询或预约。图书馆暂时返回 503 时，学校会话保持在 `sso_authenticated`，程序每 60 秒尝试建立图书馆会话。若图书馆另行要求扫码，页面会显示新二维码。也可以在网页“扫码入口”选择原来的图书馆登录方式。

默认只查询，不提交预约。手动提交需要在**账号配置**中开启 `submit_enabled`，并在提交接口明确传入 `confirm: true`；自动任务需要显式启动。个人配置保存在账号数据库中，不是当前部署的 `config.json`。

## 运行

需要 Python 3.11 或更高版本（代码使用 `asyncio.timeout`），部署示例使用 Python 3.12。Python 3.6 无法运行当前代码，单纯升级 pip 不能解决。项目尚无 `pyproject.toml`、`uv.lock`，使用 uv 时应通过 `uv pip` 安装 `requirements.txt`，不能直接假定支持 `uv sync`。

### 使用 uv 创建环境

以下命令适用于 Linux/macOS；Windows 使用 `.venv\Scripts\python.exe`，详见[部署说明](deployment/DEPLOYMENT.md)。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.json config.json  # 仅首次安装，不覆盖已有配置
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 使用已有 Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp config.example.json config.json
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

如果 Linux 上 Chromium 系统依赖不完整，应安装所需依赖（例如 `python -m playwright install --with-deps chromium`，需要相应系统权限），或使用项目中的 Dockerfile 构建镜像。
内存较小的服务器建议为容器设置至少 256 MB 共享内存，并为主机准备足够的 RAM 或 swap；Chromium 打开登录页需要额外内存。

txyun 部署目录为 `/opt/ustb-seat-book`，宿主端口为 **18001**（容器内 8000），VPN 地址为 `http://10.11.12.9:18001`。本地默认 8000 与生产 18001 不要混用。现有 Compose 与镜像构建、持久化和升级步骤见[部署说明](deployment/DEPLOYMENT.md)。

## 账号与多用户

打开服务根地址会先看到登录/注册界面：

```text
http://服务器地址:8000/
```

支持任何访问者自助注册（用户名 3-32 位字母/数字/下划线，密码至少 8 位）；**第一个注册的账号自动成为管理员**。每个账号拥有自己的一份预约参数（区域、座位、时段、触发时间、保活和提交开关等）、自己的自动预约任务与每日模式、自己的 SSO/图书馆登录会话和历史记录，互不可见、互不影响——多个人可以同时登录同一个部署，各自抢各自的座位。登录状态保存在服务端内存中的 HttpOnly Cookie 对应的会话令牌里，服务重启后需要重新登录（与此前扫码登录重启后需要重新扫码一致）。

**用户管理页**（仅管理员，顶栏“用户管理”按钮）：左侧列出所有用户及其登录/会话/任务概况，点开一个用户可以：查看并修改他的预约设置（区域、座位、时段、触发时间、保活/提交/每日模式开关，保存前会校验格式）；查看他当前的自动预约任务和分页历史，停止任务（同时关闭每日模式，否则调度器会立刻重新安排）；查看并关闭他的登录会话；重置密码（预填随机密码，重置后他所有网页登录立即失效）；设为/取消管理员；删除用户。修改设置只影响之后新建的任务，已在运行的任务沿用启动时的参数。保护措施：管理员不能改自己的角色或删除自己，也不能删掉最后一个管理员；删除用户时他的预约历史只是移到 `logs/automation-history/deleted/`，不会被抹掉；所有管理操作都会以管理员用户名记入运行日志。这里的“预约”指本程序的自动预约任务——管理员**不会**借用用户的学校登录去替他查询或取消图书馆里的预约，该登录只属于用户本人。

管理员账号额外可见：`GET /api/status` 返回的进程内存/cgroup 用量（`memory` 字段）和全部账号的浏览器会话列表，以及 `GET /api/logs` 运行日志；普通账号只能看到自己的会话与任务状态，看不到其他账号的信息或运行日志。

`base_url`、`max_browser_sessions`、`request_timeout_seconds` 等基础设施参数属于部署级配置，仍保存在服务端的 `config.json`，由部署者编辑后重启生效，不通过网页或账号修改；账号能修改的是 `GET/PUT /api/config` 返回的个人预约参数。升级前已经在旧版 `config.json` 里配置过座位/时段等参数的部署，第一个注册的账号会自动继承这些参数，不需要重新填写。

页面提供：

- SSO 二维码生成、扫码状态和当前登录用户状态；
- 登录后从站点实时读取楼层 / 区域树并下拉选择，日期、座位、时间、保活和预约提交开关的修改；
- 座位查询及 `devName -> devId` 映射展示；
- 预约提交按钮和二次确认；
- 服务状态、认证会话数量和内存日志查询。
- 开始/结束时间来自座位实时规则；未查询时由 `/api/time-rule/default?day=today|tomorrow` 提供常见规则（07:00–22:15、每 5 分钟、最短 30 分钟），无需学校登录。今天会剔除已过的开始时间，页面每分钟刷新；保存值失效时保留并标注，修改或确认后才能保存。脚本的 availability、slots 和提交接口执行相同的服务端时间校验；
- 自动触发时间、失败重试间隔、启动和停止自动预约；
- 按“座位状态刷新间隔”自动刷新只读座位状态。

页面保存个人参数时会写入 `logs/accounts.sqlite3` 对应账号的记录；部署级 `config.json` 不会被个人设置覆盖。手动预约提交仍同时受 `submit_enabled` 和明确确认保护。

新标签页、其他设备或重新打开网页时，会从后台恢复已有有效登录，无需重新扫码。优先保留当前标签页选中的有效会话（包括正在扫码的二维码）；没有有效本地选择时，优先恢复运行中预约任务使用的登录，其次恢复唯一有效登录。若有多个有效登录且没有任务指向其中一个，登录区提供选择框，不擅自切换账号。恢复只读取现有状态，不创建浏览器、不提交预约。

自动预约的工作方式是：先完成学校 SSO 登录，再在页面中开启保活并保存 `submit_enabled: true`，然后点击“启动自动预约”。即使夜间图书馆停服、尚未取得图书馆会话，只要学校 SSO 仍有效，也可以先启动。选择“今天”“明天”或“后天”时，页面会显示并保存北京时间对应的**具体日期**；自动任务启动时再次固定该日期，跨过午夜也不会改成其他日期。例如今天选择“后天”，将触发时间设在明早，即可在明早预约当时的“明天”。旧版配置若仍是 `today` / `tomorrow`，须在页面重新选择一次以确认日期。程序按北京时间等待**下一次** `auto_trigger_time`（如果今天这个时间已过，就等明天），到点后按 `auto_retry_interval_seconds` 间隔尝试取得图书馆会话并预约；图书馆恢复后会进入座位查询和提交。目标日期必须处于触发日的今天或明天，否则拒绝启动。成功后停止；需要再次扫码或遇到终止性的业务规则拒绝时停止。点击“停止自动预约”或服务关闭也会取消任务。

图书馆旧凭证失效时，会同时清理该站点 `sessionStorage` 中的 `userInfo` 和 `isLogin`，保留学校 SSO Cookie，然后重新获取登录地址完成认证跳转。程序最多等待 20 秒取得新 Token；只有看见实际扫码二维码才判定需要人工登录，经过 SSO 域名本身不算失效。日志中的 `JWGL application keepalive succeeded` 只表示教务站点会话可访问，不证明统一认证中心仍能签发新凭证；自动换取结果以 `Library renewal completed` 为准。真正要求再次认证时，不能保证免扫码恢复。恢复过程的日志只保留阶段、域名和路径，不记录票据、Cookie 或 Token。

网页“自动预约任务”区域展示当前运行任务的参数快照、触发时间、状态、尝试次数和最近错误；下方提供已结束任务的分页历史。任务启动后修改配置只影响下一次任务，不会修改当前任务的日期、座位、时段和重试间隔。历史记录按账号分别写入 `logs/automation-history/<账号 ID>.sqlite3`，使用现有日志目录挂载，容器重建后仍保留；不存储 Cookie、Token 或登录会话 ID。功能启用前的任务仍查看原运行日志，不推测补填其参数。服务非正常退出时，下次启动会将未完成任务标记为“服务重启中断”，不会自动恢复提交。接口：`GET /api/automation` 查询当前/最近任务，`GET /api/automation/history?page=1&page_size=10` 查询历史（每页最多 50 条）。

每日模式：完成扫码登录，设置区域、候选座位、开始/结束时间、每天的北京时间触发点及重试间隔，开启提交和保活，然后点击“开启每日预约”。系统从下一次触发点开始，每次预约触发日的次日，不使用单次任务的固定目标日期；成功后自动安排下一天，临时失败按间隔重试至当日结束。历史记录按每次执行保存并标注“每日自动”。每次尝试按候选座位的顺序逐个检查：座位未开放、时段不在该座位可选范围内、或与已有预约**时段重叠**才跳过（不重叠的已有预约不影响），图书馆拒绝某个座位时继续下一个；“超出预约数量”“不能连续预约”“没有权限”这类账号级拒绝直接停止。提交请求超时或无响应时，下一次尝试会先查询“我的预约”，已约到就视为成功，不会再约第二个座位。需要再次扫码时，模式仍保留，单账号程序会使用最近一次成功登录的会话；同一天重新登录可继续当天未完成的认证任务。关闭网页不影响执行，点击“关闭每日模式”或“停止自动预约”可关闭模式并停止当前任务。修改参数只影响尚未创建的下次任务，若要更新已排定任务，请关闭模式后重新开启。

每日开关持久化为 `daily_auto_enabled`（默认 false），服务重启后会等待新扫码并安排下一次触发；尚未开始尝试的排定任务可以重建，已成功/终止的同一天任务不会重复自动提交。对已尝试但因重启中断的任务，本日不自动重试，避免提交结果不明时重复预约。保活不能保证统一认证永不过期；实际出现二维码时仍需要扫码。每日开关接口：`POST /api/automation/daily/start`（`auth_session_id`）和 `POST /api/automation/daily/stop`；`GET /api/automation` 的 `daily` 字段展示开关、下次触发、目标日期和等待登录状态。

## 浏览器资源管理

所有登录会话共享同一个 Playwright `Browser` 进程，每次登录只新建一个轻量的 `BrowserContext`（而不是像此前那样每次登录都用 `launch_persistent_context` 单独启动一整个 Chromium 进程），显著降低了多会话、多账号同时在线时的内存占用；也不再写入每会话独立的磁盘 Profile 目录（原 `auth-profiles/` 已废弃）。

每个账号最多保留 **2 个登录会话**（`max_sessions_per_account`，允许 1–4），且**同时只保留 1 个已登录的会话**：已登录时再生成一个新二维码，扫码完成后旧的登录会被自动关闭；若自动预约任务正使用旧会话，会自动切换到新会话继续运行，而不是停止。未扫码的二维码会话不受这条规则影响，只占用名额。网页“我的登录会话”列表可以查看、切换和删除自己的会话。

另有服务器级上限 `max_browser_sessions`（默认 5，允许 1–16），用于限制全部账号合计的浏览器上下文数量。此前环境单会话曾测得约 125–135 MiB，但该测量不是容量承诺，页面、浏览器版本和缓存都会改变占用；当前 txyun 容器 RAM 上限为 1536 MiB。达到任一上限时创建接口返回 HTTP 409，并说明是哪一种上限；不会挤掉已有登录，并发创建也受上限约束。降低上限不会强制关闭已有有效会话。

后台每 30 秒（`session_cleanup_interval_seconds`）检查一次，释放未登录且超过 5 分钟（`auth_session_ttl_seconds`，默认 300）仍未扫码成功的会话，以及明确失败或失效的浏览器。每个账号最多 2 个会话、至多 1 个已登录：多出来的那个只是待扫码的临时会话，5 分钟内没登录就被删除；两个都没登录，则两个都会被删除。有效学校/图书馆登录、图书馆停服期间的学校会话和正在执行自动预约的会话均保留。回收旧二维码前再次确认是否已完成扫码；最多保留 32 条终态信息供网页查询，不删除磁盘认证目录。保留会话不代表能绕过学校自身的登录过期策略。

每 60 秒（`memory_log_interval_seconds`）在普通日志中写入 `Memory usage`：浏览器数量和状态、进程树 RSS、容器当前/峰值内存和缓存、容器内存上限、主机可用内存及交换占用。Linux `/proc` 与 cgroup 提供完整指标；其他系统或缺失指标时值为 null。RSS 合计可能重复计算共享页，容器内存包含文件缓存，两者不能直接相加。日志不记录凭证或会话标识；最近一次采样通过 `GET /api/status` 的 `memory` 返回。

`max_browser_sessions`、`session_cleanup_interval_seconds`、`memory_log_interval_seconds` 等会话容量/回收参数属于部署级配置，编辑 `config.json` 后重启生效；账号可见的座位/时段/保活等个人参数通过 `PUT /api/config` 设置，保存在每个账号自己的记录里。运行日志按每份 5 MiB 轮转，保留 5 份备份。限制会话数量不等同于容器硬内存上限，也不能保证其他服务或单个浏览器永远不会引发内存压力。

学校保活和二维码原图的 Playwright API 响应会在读取后单独释放，不关闭 Cookie 上下文。图书馆换凭证先请求小型 `/ic-web/auth/address` 接口探测可用性，503 时不重置页面或跳转 SSO；预约重试可按原频率探测，未完成或失败的完整认证跳转至少间隔 `library_navigation_interval_seconds`（默认 60 秒，允许 10–600 秒）。明确停服后入口恢复可立即执行一次跳转，不等待旧冷却；成功取得凭证则清除失败冷却，避免妨碍之后真正失效时恢复。并发刷新复用会话页锁和已取得的新凭证。

`Memory usage` 同时包含 Linux 进程和 cgroup 的读写累计值与采样间隔增量、进程状态和主机缺页/回收增量。最多列出 12 个重点进程（内存前 6 和 I/O 前 6 的并集），不记录命令行或凭证。`read_bytes` 是存储读取计数，`read_chars` 是读取字符计数（可含缓存等），不能把两者混作物理读盘；进程树统计可能受共享读取归属、进程退出影响，cgroup 统计可作补充。第一次采样和缺失计数器的增量为 null；PID 起始时间用于避免复用产生负增量。容器内存峰值在重启后重置。

Docker 部署还应限制容器标准输出日志。当前 txyun Compose 使用 `json-file`，设置 `max-size=10m`、`max-file=3`。应用日志最多约 30 MiB，Docker 输出最多约 30 MiB（不包含其他容器、备份或任务历史数据库）；轮转时最老日志会被替换，如需长期历史请另行归档。

## 运行日志

程序会把日志持续追加到 `logs/ustb-seat-book.log`。扫码登录成功后，校园 SSO 和图书馆会话各自记录登录时间；每次保活确认有效时，分别记录 `last_valid_at` 和从登录起算的 `alive_seconds`。临时网络错误只记录为保活失败，不等同于会话过期；确认失效时会记录检测时间。正常关闭时会记录最终状态与最后有效时间，意外断电或强制重启则只能以最后一条保活成功记录为存活时间下界。事件时间字段使用 UTC ISO 格式；日志行前缀采用进程所在时区，当前 txyun Compose 配置为 `Asia/Shanghai`，两者显示可能相差 8 小时。真实失效时刻在最后有效和检测到失效之间，检测精度受保活间隔影响。日志不会写入 token 或 Cookie。容器部署时请继续将 `logs` 目录挂载到宿主机，以免重建容器时丢失记录。

## 登录 API

扫码接口支持 HTTP Basic 应用账号密码或已有应用 Cookie，不要求先创建 Cookie。下面保留 Cookie 方式示例；已有账号使用 `/api/account/login`，新安装才按需注册。第一个注册者会成为管理员，不要在未配置访问控制时向不可信网络开放注册：

```bash
curl -c cookie.jar -X POST http://127.0.0.1:8000/api/account/register \
  -H 'Content-Type: application/json' -d '{"username":"alice","password":"a-strong-password"}'
```

开始一次 SSO 登录：

```bash
curl -b cookie.jar -X POST http://127.0.0.1:8000/api/auth/sessions
```

响应会返回 `session_id` 和二维码接口地址：

```json
{
  "session_id": "...",
  "qr_url": "/api/auth/sessions/.../qr",
  "status_url": "/api/auth/sessions/..."
}
```

将 `qr_url` 返回的 PNG 展示给用户，用微信扫码。然后轮询：

```bash
curl -b cookie.jar http://127.0.0.1:8000/api/auth/sessions/{session_id}
```

`waiting_scan` 表示等待学校统一登录扫码；`sso_authenticated` 表示学校已登录、图书馆会话尚未建立；`library_scan_required` 表示图书馆要求再次扫码。只有 `status` 变为 `authenticated` 后，才可以使用该 `session_id` 查询座位。

## 座位查询

```bash
curl -b cookie.jar "http://127.0.0.1:8000/api/seats?auth_session_id={session_id}"
```

日期、候选座位和时间都可以在网页中修改；目标日期下拉框提供“今天”“明天”和“后天”，按北京时间计算并保存为具体的 `YYYY-MM-DD` 日期。区域建议在登录后通过页面的“刷新楼层 / 区域”下拉框选择，程序会同时保存实时的区域 ID 和名称。

时间选择由座位查询返回的 `openTimes` 和 `resvRule` 动态生成，开始时间和结束时间共用同一套实际可选项。`poll_interval_seconds` 只控制 Web 控制台的只读座位状态刷新，不是预约失败重试；自动预约失败重试使用独立的 `auto_retry_interval_seconds`。

查询接口会从 `/ic-web/reserve` 返回的座位数组中实时建立 `devName -> devId` 映射，不需要手工维护数字 ID。

## 预约提交

默认关闭：

```json
{"submit_enabled": false}
```

确认查询逻辑无误后，手动改为 `true`，再显式提交：

```bash
curl -b cookie.jar -X POST "http://127.0.0.1:8000/api/reservations/submit" \
  -H 'Content-Type: application/json' \
  -d '{"auth_session_id":"...", "confirm":true}'
```

程序会再次查询座位，然后只提交一次。HTTP 200 不代表预约成功，必须检查返回 JSON 中的 `code == 0`。

## 查看与取消自己的预约

扫码登录后，在网页“我的预约”区域选择“今天”或“明天”，点击“查询我的预约”。列表显示图书馆返回的本人预约、时间、座位和可取消状态，并支持翻页。可取消的记录会显示“取消预约”按钮，点击后需要再次确认；程序会重新核对当前登录账号的列表和状态，随后调用图书馆的取消接口。取消与“提前结束使用”不同，不会自动对已生效或已结束的记录提供取消按钮。

对应接口为 `GET /api/reservations/mine`（参数：`auth_session_id`、`day=today|tomorrow`、`page`）和 `POST /api/reservations/cancel`（参数：`auth_session_id`、`uuid`、`day=today|tomorrow`、`page`、`confirm: true`）。服务端只接受今天和明天，且查询状态与图书馆前端“我的预约”的默认状态筛选保持一致。

今天已签到且未结束、未在暂离中的本人预约会显示“暂离”按钮，确认后立即提交。暂离不会取消预约；预约须知要求 1 小时内返回，实际期限以图书馆返回为准，超时未返记违约。列表显示中文状态和“暂离中，请在 … 前返回”；未取得期限时请查看图书馆“我的预约”。请求超时后服务只重新查询确认，不自动重复提交。

脚本可直接使用同一接口（无需网页、无需开启 `submit_enabled`）：

```bash
curl -u 'APP_USERNAME:APP_PASSWORD' -H 'Content-Type: application/json' \
  -X POST 'http://127.0.0.1:8000/api/reservations/temp-leave' \
  -d '{"auth_session_id":"SESSION_ID","uuid":"UUID_FROM_TODAY_RESERVATIONS","page":1,"confirm":true}'
```

服务端重新核对今日指定页的本人预约和暂离权限。成功返回 `success=true`、`message`、`temp_leave_end_time`（实测为剩余返回分钟数，如 `59`；可为 null）；结果未确认返回 502，请刷新我的预约。一个账号同时只能执行一个预约操作，冲突返回 409。完整参数与状态码见[接口文档](docs/API.md)。


今天可取消或可提前结束的预约还会显示“取消并延后重约”或“结束并延后重约”。弹窗可选择延后分钟数（默认 120，可自定义 5..900），预览由服务端计算：从现在加上延后时间并按实时网格向上取整，保留原座位和原结束时间。例如 10:02 延后 120 分钟，立即提交 12:05–原结束时间的新预约。2026-10-21 已在真实系统验证：已签到（且暂离中）的 F4B114 10:00–22:15 在 12:10 提前结束，并立即约到 14:15–22:15；新预约需在开始后重新签到。

预览使用 `POST /api/reservations/rebook` 的 `dry_run: true`，无需开启提交。真实执行需 `submit_enabled: true` 和 `confirm: true`，释放后仅尝试预约一次。两步之间座位可能被占用，失败不自动重试；页面会持续显示“原预约已释放，重约失败：…”或结果未知提示，请查看我的预约。脚本支持 HTTP Basic，无需 Cookie：

```bash
curl -u 'APP_USERNAME:APP_PASSWORD' \
  -X POST 'http://127.0.0.1:8000/api/reservations/rebook' \
  -H 'Content-Type: application/json' \
  --data '{"auth_session_id":"SESSION_ID","uuid":"RESERVATION_UUID","delay_minutes":120,"dry_run":true}'
```

确认计划后改为 `"dry_run":false,"confirm":true` 执行；务必检查 `state` 和 `success`，HTTP 200 也可能是 `released_not_rebooked` 或 `unknown`。完整字段与状态码见[接口文档](docs/API.md)。

## 脚本化控制 API

完整接口参数、响应判断和调用示例见 [接口文档](docs/API.md)。运行中的交互文档位于 `/docs`，机器可读定义位于 `/openapi.json`。

以下接口方便用脚本完整驱动查询、选座、预约、取消、暂离和延后重约，全部调用图书馆官方前端使用的同一组接口（暂离为 `/ic-web/seatOperation/tempLeave`，提前结束为 `/ic-web/reserve/endAhaed`）。

### 提醒程序查询：无需 Cookie

```bash
curl --max-time 50 -X POST 'http://10.11.12.9:18001/api/monitor/status' \
  -H 'Content-Type: application/json' \
  --data '{"username":"APP_USERNAME","password":"APP_PASSWORD","day":"tomorrow"}'
```

使用应用账号密码，不是学校账号密码；不需要先登录应用或生成额外只读令牌。`day` 支持 `today`、`tomorrow`、`both`。结果必须区分已预约 `true`、已确认无有效预约 `false`、无法确认 `null`；登录失效、超时不是“没有预约”。成功通知使用预约记录的 `event_id` 去重，建议每 60 秒查询一次。完整示例和限制见[提醒接入文档](deployment/MONITORING.md)。

### 认证

脚本可以继续用 Cookie（`curl -c/-b cookie.jar`），也可以在每个请求里直接带本账号的用户名和密码（HTTP Basic）。服务端每次用与网页登录相同的密码校验，不创建登录会话、不下发 Cookie；用户名或密码错误返回 401。建议只在 HTTPS 或受信任网络中使用。

```bash
curl -u alice:a-strong-password http://127.0.0.1:8000/api/overview
```

HTTP Basic 与 POST 预约状态查询共用认证限流：同一来源 IP 每分钟最多 30 次，超过返回 429 和 `Retry-After: 60`。密码校验在线程中执行，不阻塞自动预约和保活的事件循环；代理后的多个用户可能共享来源 IP。

### 状态总览

`GET /api/overview`（可选 `auth_session_id`、`include_reservations=true|false`）一次返回：账号、当前预约参数和 `submit_enabled`、自己的全部登录会话、选中会话的 SSO/图书馆状态与最近错误、单次任务和每日模式状态，以及今天和明天的本人预约。未指定会话时优先选自动任务正在使用的已登录会话，其次选最新的已登录会话。只有实际访问图书馆并验证成功后才返回 `ready: true` 和 `library.confirmed: true`，这不代表某个座位一定可约或预约一定成功。`include_reservations=false` 不返回预约明细，但仍进行一次只读验证。凭证过期、网络异常、超时返回 `ready: false`、`library.reason` 和 `hint`，不因这些预期故障返回 500；尚未获取的记录为 `null`，不伪装为空列表。验证过程限时 20 秒，不自动提交预约。

### 座位与时段

- `GET /api/areas?auth_session_id=...`：楼层 / 区域树（已有）；
- `GET /api/rooms/{room_id}/availability?auth_session_id=...&date=tomorrow&only_free=true`：区域内全部座位及每个座位的 `bookable` 和 `free_time_options`（已扣除和已有预约重叠的时段；预约记录无法解析时为 `null`）；
- `GET /api/rooms/{room_id}/seats/{seat}/slots?auth_session_id=...&date=today&begin=08:00`：单个座位的规则、已有预约和可选开始 / 结束时间，`seat` 可以填座位名（如 `A101`）或 `devId`；带 `begin` 时附带图书馆 `/ic-web/device/tips` 的提示。

`date` 接受 `today`、`tomorrow`、`dayafter` 或 `YYYY-MM-DD`（北京时间），查询最多到后天。

### 指定座位预约

`POST /api/reservations` 直接预约指定的区域、座位和时段，不依赖网页保存的候选座位：

```bash
# 先只校验，不提交（不要求 submit_enabled 和 confirm）
curl -u alice:a-strong-password -X POST http://127.0.0.1:8000/api/reservations \
  -H 'Content-Type: application/json' \
  -d '{"auth_session_id":"...","room_id":123,"seat":"A101","date":"tomorrow","start":"08:00","end":"12:00","dry_run":true}'
```

真正提交时仍使用同一个 POST 接口和完整请求体，将 `dry_run` 改为 `false`、`confirm` 改为 `true`；账号还必须已开启 `submit_enabled`。上例保持只校验，不会提交。不要把省略号当作合法 JSON。

`room_id` 省略时使用账号配置的区域；日期只能是今天或明天。程序会重新查询座位，检查座位开放、时段在实际可选范围内且不与已有预约冲突，然后只提交一次。和原有提交接口一样，HTTP 200 不代表成功，要看返回的 `success` / `code == 0`。原有的 `POST /api/reservations/submit`（按配置的候选座位提交）保持不变。

### 取消

取消仍使用 `POST /api/reservations/cancel`，必须传 `confirm: true`，详见“查看与取消自己的预约”。暂离使用 `POST /api/reservations/temp-leave`，支持 HTTP Basic 或 Cookie，详见[接口文档](docs/API.md)。签到、续约目前尚未提供；提前结束可作为延后重约的释放步骤。

## 安全注意事项

- 不要把 `config.json`、`logs/accounts.sqlite3`、Cookie、token 或 HAR 文件提交到代码仓库。
- 账号相关接口通常需要 Cookie 或 HTTP Basic；`POST /api/monitor/status` 则直接校验 JSON 中的应用账号密码，无需 Cookie。`/healthz`、注册/登录、交互文档与 OpenAPI 定义不要求已有应用登录，外层网关认证另算。退出接口撤销 Cookie，不会禁用 Basic 密码认证。注册开放自助，密码使用 PBKDF2-HMAC-SHA256 加盐哈希存储，不保存明文。
- 登录态保存在内存中的 Playwright 浏览器上下文中；服务重启后通常需要重新扫码（图书馆/学校登录如此），应用账号的登录态同样保存在内存令牌中，服务重启后需要重新登录账号。
- 当前实现不尝试绕过验证码。若服务端要求验证码，提交接口会返回明确错误，后续可再增加人工接管流程。
- 不要高频并发提交。遇到“当前时段已有预约”或“不能连续预约”时，程序不会自动重试。
- 登录成功后默认每 5 分钟使用同一个浏览器上下文访问教务系统，确认教务应用页面仍可访问；这不能证明 CAS 仍可签发新票据。图书馆会话建立后还会调用只读 `/ic-web/auth/userInfo`，token 明确失效时尝试用保留的学校 SSO 重新换取。保活不保证服务端一定续期，图书馆服务不可用时无法签发新 token。
- 如不希望执行保活请求，将 `keepalive_enabled` 改为 `false`；间隔可通过 `keepalive_interval_seconds` 修改，程序会将过短值限制为至少 30 秒。
