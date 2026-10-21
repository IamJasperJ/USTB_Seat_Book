# Redmi ARM64 迁移历史归档

归档说明更新：2026-10-21。下文记录 **2026-10-11** 的历史迁移，不是当前部署指引。应用后来已迁至 txyun，Redmi 上应用容器与数据曾按用户要求清理；不能依据下文断言历史文件、镜像、目录今天仍存在。

当前生产基线：txyun `/opt/ustb-seat-book`，18001 端口，amd64。现行步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)，接口见 [API.md](../docs/API.md)。Redmi 或 WRT 上的反向代理与应用容器不是同一回事。

历史的 `start.sh`、`redmi-smoke.py`、`validation-config.json` 等并非当前仓库提供的可执行部署套件，下述路径、审批阶段与保留状态只描述当时。不要照旧命令启动第二套生产调度器。未来重做 ARM64 迁移时仍应在独立构建机编译、验证，目标 Redmi 仅加载运行，不在 Redmi 上构建。

## 原始迁移记录（2026-10-11）

Target: SSH `redmi`, postmarketOS/Alpine host, ARM64 Docker. The application
container remains Ubuntu-based; do not install its Python dependencies into the
host's Alpine environment.

Staging directory: `/home/server/ustb-seat-book-migration-20261011`.
Image: `ustb-seat-book:migration-arm64-20261011`.

The application, production config, logs, and SQLite task history were copied
from the stopped Aliyun deployment. Config and history SHA-256 checks matched;
SQLite quick_check passed. The original production config has daily automation
enabled. `validation-config.json` is the disabled example config and is only for
isolated tests. No old browser profiles are imported: a fresh QR login is needed.

## Build outside Redmi

Do not build on Redmi. Use a separate Docker-capable build machine to build the
ARM64 image from the staged application sources (excluding config.json and logs),
test it there, export it with docker save, and transfer the resulting archive to
Redmi for docker load. Redmi is only a runtime target. The first two Redmi build
attempts were interrupted by Docker termination; their seven identified cache
records and temporary smoke/config files have been removed. No image was built.

Selected build host: SSH `vm130_complie` (via its configured `win11` jump host).
Build directory: `/home/jasper/ustb-seat-book-build-20261011`.
Docker/Buildx and QEMU ARM64 emulation were installed there. The build uses
`Dockerfile.arm64` and `requirements.arm64.txt`, pinning the six primary package
versions to those inspected in the old Aliyun image. No production credentials,
config.json, or logs are included in this build context.

Deployment completed on 2026-10-11. ARM64 image ID:
`sha256:fdbe74ee97b88425dbaac9574740a8097db4f7f03ad7bc5cb2e2af0c97c013fb`.
Native Redmi Chromium navigation verified the library HTTP 200 and JWGL -> SSO
redirect. Hermes ran 40 isolated regression tests: all passed, no skips.
Production binds `0.0.0.0:18001`; fresh QR login is still required. No real
reservation was submitted during validation. Compiler download intermediates
(about 6.1 GiB) and temporary Redmi smoke files were removed; final image/archive,
production settings/history/logs and old Aliyun data remain available.

```sh
sudo docker buildx build --platform linux/arm64 --load --progress plain \
  -f /home/jasper/ustb-seat-book-build-20261011/Dockerfile.arm64 \
  -t ustb-seat-book:migration-arm64-20261011 \
  /home/jasper/ustb-seat-book-build-20261011
```

The following commands are for a separate build/validation machine only, after
copying the sources and test helpers there; they must not be run on Redmi.

## Validation on the separate build machine

Build an ARM64 image, not by copying the existing amd64 image:

```sh
cd /path/to/migration-sources
docker build --platform linux/arm64 -t ustb-seat-book:migration-arm64-20261011 .
docker image inspect ustb-seat-book:migration-arm64-20261011 --format '{{.Architecture}}'
docker run --rm --memory 1g --memory-swap 1536m --shm-size 256m \
  --mount "type=bind,src=$PWD/redmi-smoke.py,dst=/tmp/redmi-smoke.py,readonly" \
  --entrypoint python ustb-seat-book:migration-arm64-20261011 /tmp/redmi-smoke.py
docker run --rm --memory 1g --memory-swap 1536m --shm-size 256m \
  -e RUN_BROWSER_TESTS=1 \
  --mount "type=bind,src=$PWD/tests,dst=/app/tests,readonly" \
  --mount "type=bind,src=$PWD/validation-config.json,dst=/app/config.json,readonly" \
  --entrypoint python ustb-seat-book:migration-arm64-20261011 \
  -m unittest discover -s tests -v
```

## Activate only after approval and successful validation

```sh
sh /home/server/ustb-seat-book-migration-20261011/start.sh
```

The script refuses to overwrite an existing container. It binds port 18001 on all
interfaces, mounts the migrated config/history, limits memory to 1 GiB with up
to 512 MiB additional swap, and limits Docker logs to three 10 MiB files.
Access: `http://10.11.12.10:18001/`. Log in again and verify the target date, daily
mode, seat, time range, and next trigger before relying on scheduled reservations.
Do not run two production schedulers simultaneously. Old Aliyun data is retained
for rollback; do not restart Aliyun automation while Redmi automation is active.
