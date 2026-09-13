#!/bin/sh
# DeepSeek userToken 校验 / 换取（反代凭据运维）
#   sh ds_auth.sh check [token]     只验活（默认读 TOKEN_FILE）
#   sh ds_auth.sh set <token>       验活 → 写本机 → 写 VM superapi.env → 重启反代 → 等就绪 → 回读 /status
# 环境变量: SUPERAPI_TOKEN_FILE(默认 ~/.dsh/.ds_user_token) LIMA_VM RELAY_DIR RELAY_PORT SUPERAPI_APIKEY
DS_API="https://chat.deepseek.com/api/v0/chat_session/create"
TOKEN_FILE="${SUPERAPI_TOKEN_FILE:-$HOME/.dsh/.ds_user_token}"
VM="${LIMA_VM:-linuxverify}"
RDIR="${RELAY_DIR:-/tmp/opt_git}"
PORT="${RELAY_PORT:-18090}"
KEY="${SUPERAPI_APIKEY:-gittest}"
LIMACTL=$(command -v limactl 2>/dev/null || true)
[ -x "$LIMACTL" ] || LIMACTL=/opt/homebrew/bin/limactl
[ -x "$LIMACTL" ] || LIMACTL=/usr/local/bin/limactl

check() {
  T="$1"
  [ -n "$T" ] || { echo "INVALID: 空 token"; return 1; }
  R=$(curl -s -m 15 -X POST "$DS_API" -H "Authorization: Bearer $T" \
        -H "Content-Type: application/json" -H "User-Agent: Mozilla/5.0" -d '{}')
  case "$R" in
    *'"code":0'*)  echo "VALID   $(echo "$R" | head -c 160)"; return 0 ;;
    *40003*)       echo "INVALID 40003 Authorization Failed (invalid token)"; return 1 ;;
    *40002*)       echo "INVALID 40002 Missing Token（没带上 token）"; return 1 ;;
    *)             echo "UNKNOWN  $(echo "$R" | head -c 160)"; return 1 ;;
  esac
}

status() {
  curl -s -m 8 -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/status" | /usr/bin/env python3 -c "
import sys,json
try:
    d=json.load(sys.stdin); a=d['accounts']; ad=d['accounts_detail'][0]
    print('    available=%s total=%s circuit_broken=%s cooling=%s err=%s ok=%s last_err=%s' % (
     a['available'],a['total'],ad['is_circuit_broken'],ad['is_cooling_down'],ad['error_count'],ad['success_count'],ad['last_error_code']))
except Exception as e:
    print('    /status 解析失败: %r' % (e,))
"
}

case "$1" in
  check)
    if [ -n "$2" ]; then T="$2"; else T=$(cat "$TOKEN_FILE" 2>/dev/null); fi
    check "$T"
    ;;
  set)
    NEW="$2"
    [ -n "$NEW" ] || { echo "用法: sh ds_auth.sh set <NEW_TOKEN>"; exit 1; }
    printf '验活: '; check "$NEW" || { echo "不换——先拿个有效的再来"; exit 1; }
    mkdir -p "$(dirname "$TOKEN_FILE")"; printf '%s' "$NEW" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
    echo "[1/4] 本机 token 已写入 $TOKEN_FILE"
    "$LIMACTL" shell "$VM" sh -c "printf 'SUPERAPI_TOKEN=$NEW\nSUPERAPI_PORT=$PORT\nSUPERAPI_APIKEY=$KEY\n' > $RDIR/superapi.env; chmod 600 $RDIR/superapi.env"
    echo "[2/4] VM superapi.env 已更新"
    # 单实例锁：必须等旧实例真的死透，否则新实例 12s 倒计时自杀
    "$LIMACTL" shell "$VM" pkill -f ./superapi 2>/dev/null
    i=0
    while [ $i -lt 25 ]; do
      N=$("$LIMACTL" shell "$VM" sh -c "ps -ef | grep -v grep | grep -c '[.]/superapi'" 2>/dev/null | tr -d ' \r')
      [ "$N" = "0" ] && break
      sleep 1; i=$((i+1))
    done
    echo "    旧实例已退（等 ${i}s）"
    "$LIMACTL" shell "$VM" sh -c "cd $RDIR && nohup ./run.sh >> srv.log 2>&1 &" >/dev/null 2>&1
    i=0
    while [ $i -lt 20 ]; do
      C=$(curl -s -m 4 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/status")
      [ "$C" = "200" ] && break
      sleep 2; i=$((i+1))
    done
    echo "[3/4] 反代已就绪（等 $((i*2))s），状态:"; status
    echo "[4/4] 完事——available=1 就能用了"
    ;;
  *) sed -n '2,5p' "$0" ;;
esac
