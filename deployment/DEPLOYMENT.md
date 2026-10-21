# 部署、升级与回滚

核对日期：2026-10-21。此页描述仓库配置与最近一次已验证部署，不是持续监控结果；命令是供部署者手动执行的示例。

## 当前部署基线

| 项目 | 值 |
| --- | --- |
| 生产主机 / 架构 | SSH `txyun` / Linux amd64 |
| 项目目录 | `/opt/ustb-seat-book` |
| 容器 / Compose 服务名 | `ustb-seat-book` |
| Compose 文件 | 服务器 `compose.yaml`；仓库模板 `deployment/compose.txyun.yaml` |
| 最近验证镜像 | `ustb-seat-book:txyun-amd64-20261021a`（git `f935db1`，基于 `txyun-control-fixed-20261020` 的代码层更新） |
| 端口 / VPN 地址 | `0.0.0.0:18001` → 容器 8000；`http://10.11.12.9:18001` |
| 时区 | `Asia/Shanghai` |
| RAM / RAM+swap 上限 | 1536 MiB / 2560 MiB |
| 共享内存 / PID 上限 | 256 MiB / 512 |

Redmi 和 Aliyun 的旧镜像、地址和目录是历史记录。反向代理可以位于其他设备，不代表应用也在那里运行。Compose 的 `pull_policy: never` 要求镜像已在本机，复制 YAML 不会自动下载或构建镜像。

## 1. Python / uv 直接运行

需要 Python 3.11+，示例用 3.12；项目没有 `pyproject.toml`、`uv.lock`，使用 `uv pip` 安装 `requirements.txt`，不能直接假设支持 `uv sync`。不要替换系统 Python。Python 3.6 无法运行当前代码，升级 pip 或换源不能解决语言版本问题。

Linux/macOS 首次安装：

```bash
cd /path/to/ustb-seat-book
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.json config.json
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Linux 缺少 Chromium 系统库时，可在相应权限下执行 `.venv/bin/python -m playwright install --with-deps chromium`。无 uv 时用 `python3.12 -m venv .venv`，再通过虚拟环境中的 `python -m pip install -r requirements.txt` 安装。

Windows PowerShell 首次安装：

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
Copy-Item config.example.json config.json
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

复制配置仅用于文件尚不存在的新安装，升级不能覆盖旧 `config.json` 或 `logs`。直接运行不会自动获得 Compose 的资源限制和日志轮转；长期运行需用服务管理器管理进程。旧系统还需核对浏览器系统依赖，不能只更换 Python。

## 2. 新建 Docker 部署

在目标架构的构建环境中从仓库根目录执行。下面标签与当前模板一致，仅用于新安装，不应随意覆盖生产正在使用的镜像标签：

```bash
docker build -t ustb-seat-book:txyun-amd64-20261021a .
mkdir -p logs
cp config.example.json config.json
cp deployment/compose.txyun.yaml compose.yaml
docker compose config -q
docker compose up -d
docker compose ps
curl --max-time 8 http://127.0.0.1:18001/healthz
```

确保 Docker 运行用户可读写日志挂载目录；`config.json` 必须先存在且是文件，Compose 禁止自动创建该绑定路径。`0.0.0.0` 不提供访问保护，先配置 VPN、防火墙或外层 SSO，再注册首个管理员。依赖版本范围不是锁文件，不保证不同时间全量构建得到完全相同的依赖。

## 3. 升级流程

先核对本地、服务器源码与镜像的差异，保留其他人的修改。使用独立暂存目录和唯一新标签，构建、测试完成后再停生产。

依赖未变且架构一致时，可复用已验证镜像：

```bash
docker build --network none --build-arg BASE_IMAGE=EXISTING_VALIDATED_IMAGE \
  -f Dockerfile.incremental -t ustb-seat-book:NEW_RELEASE_TAG .
docker run --rm --network none --memory 768m --memory-swap 1g --shm-size 256m \
  -e RUN_BROWSER_TESTS=1 -e PYTHONDONTWRITEBYTECODE=1 \
  --mount "type=bind,src=$PWD/tests,dst=/app/tests,readonly" \
  --entrypoint python ustb-seat-book:NEW_RELEASE_TAG \
  -m unittest discover -s tests -q
```

替换 `EXISTING_VALIDATED_IMAGE` 和 `NEW_RELEASE_TAG`。增量 Dockerfile 的默认基础镜像是历史值，当前升级必须显式传入；依赖改变或跨架构应重新构建适配运行时。测试命令从暂存目录执行，只挂载测试文件，不挂生产配置、日志、账号或凭证。2026-10-20 修复版曾在 txyun 隔离环境通过 141 项测试，这是版本记录，不代表之后的修改自动通过。

切换前停服务并备份；把 `RELEASE_ID` 换成唯一目录名，不覆盖已有备份：

```bash
cd /opt/ustb-seat-book
mkdir -p backups/RELEASE_ID
chmod 700 backups/RELEASE_ID
docker compose stop -t 30 ustb-seat-book
tar -czf backups/RELEASE_ID/before-update.tar.gz \
  app tests compose.yaml config.json logs
chmod 600 backups/RELEASE_ID/before-update.tar.gz
```

停服后备份 SQLite，避免不一致快照。备份含账号哈希、个人配置和记录，应限制权限；覆盖已有独立文档前也应备份文档。复制已验证源码、测试与文档，将 Compose 镜像改成唯一新标签，保留生产挂载、端口和资源限制，再执行：

```bash
docker compose config -q
docker compose up -d
systemctl start ustb-oom-guard.service
docker compose ps
curl --max-time 8 http://127.0.0.1:18001/healthz
```

systemd 命令仅用于已配置 guard 的主机。Docker 状态可能先为 `starting`，待首轮探测后才转为 `healthy`。核对 `/docs`、`/openapi.json`、未认证查询的 401、数据库完整性与账号数量。不要为烟雾测试创建生产账号、提交或取消真实预约。

## 4. 重启与回滚

- 账号参数和任务历史持久化，应用 Cookie 令牌、浏览器上下文不持久化；重启后重新扫码并核对任务。
- 单次任务不恢复提交；每日开关保留，重新扫码后依调度规则安排。已尝试且因重启中断的当天任务不会盲目重放。
- 不同时运行两个共享账号数据库且开启自动预约的正式实例。
- 回滚先选择上一版本镜像，保留当前数据并核对数据库兼容性。恢复旧数据库前先停服务并备份现状，不能覆盖新产生的预约历史后才考虑补救。
- 不用 `docker compose down -v`、删除整个目录等方式回滚；认证上下文无法通过解包备份恢复。
- 只清理本次明确创建的临时目录和测试容器。旧镜像、回滚备份不是构建垃圾，应按保留策略处理。

## 5. 资源与排障

```bash
cd /opt/ustb-seat-book
docker compose ps
docker stats --no-stream ustb-seat-book
docker logs --tail 100 ustb-seat-book
swapon --show
systemctl status ustb-oom-guard.timer
```

最近验证基线有 1 GiB `/swapfile-ustb` 并已加入 `/etc/fstab`；这是主机共享 swap，不是容器专属额度。Compose 不自动创建 swap 或安装 guard，移机需单独配置。不要在未确认已有文件用途时重新创建或格式化 swap 文件。OOM 优先级只降低风险，不保证不会被杀，持续换页也可能加重 I/O，详见[健康与内存保护](HEALTH.md)。

排障区分 VPN/网关、应用健康、学校凭证和预约结果；`/healthz` 健康不代表登录有效。事件时间字段为 UTC，当前 Compose 日志前缀为北京时间，比较前统一时区。

## 6. ARM64

amd64 镜像不能当原生 ARM64 镜像使用。未来迁移 ARM64 时，在独立构建机构建和测试，目标设备仅加载运行。`deployment/Dockerfile.arm64` 要求暂存上下文根目录有 `requirements.arm64.txt`、`app/` 和示例配置；不能直接在原仓库根目录套用历史命令。

`Dockerfile.arm64-update` 依赖历史 ARM64 基础镜像，只有确认镜像存在、架构正确、依赖未变时才适用。历史记录见 [Redmi 迁移归档](REDMI-MIGRATION.md)，其中路径和镜像不保证今天仍存在。
