#!/bin/sh
# Run on redmi only after the migration is approved. Existing config is preserved.
set -eu
cd /home/server/ustb-seat-book-migration-20261011
if docker container inspect ustb-seat-book >/dev/null 2>&1; then
  echo 'Container ustb-seat-book already exists; inspect it before replacing it.' >&2
  exit 1
fi
mkdir -p logs
docker run -d --name ustb-seat-book \
  --platform linux/arm64 \
  --restart unless-stopped \
  --memory 1g --memory-swap 1536m --pids-limit 512 \
  --shm-size 256m \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  -e TZ=Asia/Shanghai \
  -p 0.0.0.0:18001:8000 \
  --mount "type=bind,src=$PWD/config.json,dst=/app/config.json" \
  --mount "type=bind,src=$PWD/logs,dst=/app/logs" \
  "${USTB_SEAT_IMAGE:-ustb-seat-book:accounts-arm64-20261011}"
