# 浏览器健康检查与内存保护

核对日期：2026-10-21。对应 `app/health.py`、Compose 与宿主 guard。部署见 [DEPLOYMENT.md](DEPLOYMENT.md)，默认值见 [CONFIGURATION.md](../docs/CONFIGURATION.md)。生产配置数据是已验证配置基线，不是实时用量。

| 检查 | 能确认什么 | 不能确认什么 |
| --- | --- | --- |
| `/healthz`、`/api/health` | 浏览器连接、页面执行能力与采样新鲜度 | 学校凭证有效、实际预约成功 |
| `/api/overview` | 本次图书馆只读验证成功 | 某个座位必定可约 |
| `/api/monitor/status` | 本人真实有效预约；失败时为 unknown | 不能把未知当作未预约 |

## Browser probes

The health monitor runs every 60 seconds. It checks the browser connection and
evaluates a constant JavaScript expression in each live login page. It does not
navigate, read credentials, submit bookings, or close login contexts. When no
login exists, it creates and closes a disposable blank context to test Chromium.
If cleanup fails, it retries cleanup rather than accumulating more contexts.

Authentication operations hold a page lock. A busy page is reported as degraded
without interrupting it. If it stays busy without a successful probe for over
180 seconds it is unhealthy. A dead monitor becomes unhealthy when its cached
sample is stale. A detected page crash immediately overrides the cached result.

## API

- `GET /healthz`: minimal service health; HTTP 200 when healthy or temporarily
  busy, HTTP 503 when disconnected, crashed, failed, stale, or not initialized.
  No credentials, session IDs, or account information are returned.
- `GET /api/health`: accepts application HTTP Basic username/password or an
  existing application login cookie. Always
  returns HTTP 200 for an authenticated status query; inspect `healthy`,
  `status`, and `sessions[].reason`. A normal account can see only its sessions;
  administrators can see all sessions. Through the existing HTTPS gateway use
  `/seat/api/health`, with both gateway and application authentication.

Example response (abbreviated):

```json
{"status":"unhealthy","healthy":false,"browser_connected":true,
 "sessions":[{"session_id":"own-session","status":"unhealthy","reason":"page_crashed"}],
 "automatic_restart":false}
```

Browser health is not proof of valid CAS credentials or a successful booking.
No automatic container restart or session recovery is performed. Docker marks
the container unhealthy; its ordinary restart policy does not restart an
unhealthy but running process. This avoids silently discarding valid logins.

`sessions[].library_authenticated` 是缓存认证标记，不是远端实时验证。Basic 不创建 Cookie，与 POST 监控共用每来源 IP 每分钟 30 次认证限流。普通账号仅能看自己的会话行，但顶层仍含全服务最小摘要；外层 SSO 网关认证另算。

```bash
curl --max-time 8 'http://SERVER:18001/healthz'
curl --max-time 10 -u 'APP_USERNAME:APP_PASSWORD' \
  'http://SERVER:18001/api/health'
```

默认采样过期阈值为 `60×2 + 3×4 = 132` 秒。Compose 每 30 秒读取健康接口，请求超时 5 秒，Docker 超时 10 秒、启动宽限 30 秒、失败阈值 2 次。外部查询不会每次启动完整认证，新容器可先显示 `starting`。

## Production host setup

The host uses a 1 GiB `/swapfile-ustb` with mode 0600 and an `/etc/fstab` entry.
Container RAM stays capped at 1536 MiB; its combined RAM+swap cap is 2560 MiB.
Swap is shared host capacity, not a dedicated reservation for this container.

The Compose `oom_score_adj` is -500. Chromium can override child scores, so the
host's `ustb-oom-guard.timer` reapplies -500 every 30 seconds to processes whose
kernel cgroup identifies the exact live booking container. No extra capability
is granted to the application container. The guard does not change other
containers. Priority reduces OOM victim selection risk; it is not immunity.

Guard 源文件为本目录 `ustb-oom-guard.py`、`.service`、`.timer`，宿主部署脚本为 `/usr/local/sbin/ustb-oom-guard.py`，unit 在 `/etc/systemd/system/`。复制 Compose 不会创建 swap 或安装 guard；移机要单独配置。无需给应用容器增加特权，新 Chromium 子进程在下一轮检查前可能保留其自设分数。

## 日志与资源诊断

资源日志默认每 60 秒采样进程、cgroup 内存、swap 和 I/O；缺失指标为 null，不是 0。RSS 可能重复计算共享页，不应与容器内存直接相加；`read_chars` 不等于物理读盘。持续换页也可能加重磁盘压力。

- 应用日志 `logs/ustb-seat-book.log`：单份 5 MiB、5 份备份，约 30 MiB。
- Docker `json-file`：单份 10m、最多 3 份，约 30 MiB。
- 不包含 SQLite 历史、备份、镜像层和其他容器，不能据此推断磁盘总占用上限。
- 健康日志主要记录状态变化，避免每次成功探测刷屏。
- 事件时间字段为 UTC，当前 Compose 日志前缀为北京时间，排查先统一时区。

```sh
cd /opt/ustb-seat-book
docker compose ps
curl http://127.0.0.1:18001/healthz
systemctl status ustb-oom-guard.timer
systemctl show ustb-oom-guard.service -p Result -p ExecMainStatus
docker stats --no-stream ustb-seat-book
swapon --show
```
