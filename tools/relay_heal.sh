#!/bin/sh
# 反代自愈（幂等、可并发）：VM 没起就 start，进程死了就起 run.sh；每轮都重试拉起，直到 /status 就绪
# 用法: sh relay_heal.sh [等待秒数，默认 60]
# 环境变量: LIMA_VM(默认 linuxverify) RELAY_DIR(默认 /tmp/opt_git) RELAY_PORT(默认 18090) SUPERAPI_APIKEY(默认 gittest)
# 为什么 limactl 要走绝对路径：launchd 起的进程 PATH 里没有 /opt/homebrew/bin
WAIT="${1:-60}"
VM="${LIMA_VM:-linuxverify}"
RDIR="${RELAY_DIR:-/tmp/opt_git}"
PORT="${RELAY_PORT:-18090}"
KEY="${SUPERAPI_APIKEY:-gittest}"
LIMACTL=$(command -v limactl 2>/dev/null || true)
[ -x "$LIMACTL" ] || LIMACTL=/opt/homebrew/bin/limactl
[ -x "$LIMACTL" ] || LIMACTL=/usr/local/bin/limactl
[ -x "$LIMACTL" ] || { echo "[heal] 找不到 limactl"; exit 2; }
export PATH="$(dirname "$LIMACTL"):/usr/bin:/bin:/usr/sbin:/sbin"

STATE=$("$LIMACTL" list 2>/dev/null | awk -v vm="$VM" '$1==vm{print $2}')
if [ "$STATE" != "Running" ]; then
  echo "[heal] VM $VM $STATE → limactl start"
  "$LIMACTL" start "$VM" >/dev/null 2>&1 &
fi
i=0
while [ $i -lt "$WAIT" ]; do
  C=$(curl -s -m 3 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/status")
  [ "$C" = "200" ] && { echo "[heal] ready after ${i}s"; exit 0; }
  "$LIMACTL" shell "$VM" sh -c "ps -ef | grep -v grep | grep -q '[.]/superapi' || (cd $RDIR && nohup ./run.sh >> srv.log 2>&1 &)" >/dev/null 2>&1
  sleep 1; i=$((i+1))
done
echo "[heal] 等待 ${WAIT}s 仍未就绪"; exit 1
