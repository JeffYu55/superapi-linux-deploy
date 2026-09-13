#!/bin/bash
# install.sh — 一键安装 SuperAPI 反代（从 GitHub Release 拉取发行包）
#
# 用法：
#   bash install.sh                     # 装到 /opt/superapi（需写权限）
#   SUPERAPI_DEST=~/superapi bash install.sh
#   SUPERAPI_VERSION=v1.0.0 bash install.sh
#
# 行为：检测架构 → 下载对应发行包 → 校验 SHA256 → 解压 → 生成 env 模板
#      **不启动服务、不改 systemd、不覆盖已存在的 superapi.env**
set -euo pipefail

REPO="${SUPERAPI_REPO:-JeffYu55/superapi-linux-deploy}"
VERSION="${SUPERAPI_VERSION:-v1.0.0}"
DEST="${SUPERAPI_DEST:-/opt/superapi}"
BASE="https://github.com/${REPO}/releases/download/${VERSION}"

# 0) 依赖检查 + 下载器选择（curl 优先，回退 wget——精简镜像常无 curl）
if command -v curl >/dev/null 2>&1; then
  DL_KIND=curl
elif command -v wget >/dev/null 2>&1; then
  DL_KIND=wget
else
  echo "[FATAL] 需要 curl 或 wget 之一" >&2; exit 1
fi
command -v tar >/dev/null 2>&1 || { echo "[FATAL] 缺少依赖: tar" >&2; exit 1; }

dl() {  # dl <url> <outfile>
  if [ "$DL_KIND" = curl ]; then curl -fsSL -o "$2" "$1"; else wget -q -O "$2" "$1"; fi
}

# 1) 架构检测
case "$(uname -s)" in
  Linux) ;;
  *) echo "[FATAL] 本脚本面向 Linux 服务器（当前: $(uname -s)）" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64)   ARCH=amd64 ;;
  aarch64|arm64)  ARCH=arm64 ;;
  *) echo "[FATAL] 不支持的架构: $(uname -m)（仅 amd64 / arm64）" >&2; exit 1 ;;
esac

TARBALL="superapi-linux-${ARCH}.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> 目标架构: $ARCH  版本: $VERSION  下载器: $DL_KIND"
echo "==> 下载 $TARBALL"
dl "$BASE/$TARBALL" "$TMP/$TARBALL"
dl "$BASE/SHA256SUMS.txt" "$TMP/SHA256SUMS.txt" || echo "    (校验和文件缺失，跳过校验)"

# 2) SHA256 校验（跨平台哈希命令）
if [ -s "$TMP/SHA256SUMS.txt" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then
    HASH_CMD="shasum -a 256"
  else
    HASH_CMD=""
  fi
  if [ -n "$HASH_CMD" ]; then
    WANT="$(grep -E "[[:space:]]${TARBALL}$" "$TMP/SHA256SUMS.txt" | awk '{print $1}')"
    GOT="$( cd "$TMP" && $HASH_CMD "$TARBALL" | awk '{print $1}' )"
    if [ -n "$WANT" ] && [ "$WANT" != "$GOT" ]; then
      echo "[FATAL] SHA256 校验失败" >&2
      echo "  期望: $WANT" >&2
      echo "  实得: $GOT" >&2
      exit 1
    fi
    echo "==> SHA256 校验通过"
  fi
fi

# 3) 解压并安装（保护已有 superapi.env：包内模板不得覆盖用户配置）
echo "==> 解压到 $DEST"
tar -xzf "$TMP/$TARBALL" -C "$TMP"
SRC="$TMP/superapi-linux-${ARCH}"
[ -d "$SRC" ] || { echo "[FATAL] 归档结构异常（缺 $SRC）" >&2; exit 1; }
mkdir -p "$DEST"
ENV_FILE="$DEST/superapi.env"
KEEP_ENV=""
if [ -f "$ENV_FILE" ]; then
  KEEP_ENV="$(cat "$ENV_FILE")"
  echo "==> 检测到已存在的 superapi.env，安装后将原样恢复"
fi
cp -r "$SRC/." "$DEST/"
chmod +x "$DEST/superapi" "$DEST/run.sh"
if [ -n "$KEEP_ENV" ]; then
  printf '%s\n' "$KEEP_ENV" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

# 4) env 模板（仅在无既有配置时生成）
if [ -f "$ENV_FILE" ]; then
  echo "==> 保留已存在的 $ENV_FILE（未覆盖）"
else
  cat > "$ENV_FILE" <<'ENVEOF'
# SuperAPI 运行时环境（run.sh / systemd EnvironmentFile 读取）
# DeepSeek 账号 userToken：浏览器登录 chat.deepseek.com → F12 Console:
#   JSON.parse(localStorage.getItem('userToken')).value
SUPERAPI_TOKEN=
# 监听端口
SUPERAPI_PORT=8080
# 客户端访问本网关所需的密钥（自定义，客户端填这个）
SUPERAPI_APIKEY=changeme
# 默认模型
SUPERAPI_MODEL=deepseek-v4.1-flash
# 出站代理（可选）
SUPERAPI_PROXY=
ENVEOF
  chmod 600 "$ENV_FILE"
fi

# 5) 完成提示（不启服务）
cat <<EOF

==> 安装完成: $DEST
    文件: $(ls -1 "$DEST" | tr '\n' ' ')

下一步：
  1) 填凭据:  vi $ENV_FILE      # 填 SUPERAPI_TOKEN 与 SUPERAPI_APIKEY
  2) 试跑:    cd $DEST && ./run.sh
  3) 装服务:  cp $DEST/superapi.service /etc/systemd/system/ \\
              && systemctl daemon-reload && systemctl enable --now superapi
  4) 验证:    curl http://127.0.0.1:8080/healthz

客户端接入: Base URL = http://<本机IP>:8080/v1, API Key = SUPERAPI_APIKEY 的值
EOF
