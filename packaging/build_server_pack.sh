#!/bin/bash
# build_server_pack.sh — 生成 SuperAPI Linux 服务器部署包（amd64 + arm64）
# 产物: dist-server/superapi-linux-{amd64,arm64}/
# 原则: 二进制零修改（直接拷解壳产物）；凭据不写死（走环境变量/EnvironmentFile）
set -euo pipefail

ROOT="/Users/jeff/Downloads/v0.2"
OUT="${1:-$ROOT/dist-server}"
UNPACKED="${SUPERAPI_BIN_DIR:-$ROOT/unpacked}"

build_arch() {
  local arch="$1"
  local src="$2"
  local dir="$OUT/superapi-linux-$arch"
  echo "==> $arch"
  rm -rf "$dir"; mkdir -p "$dir"

  # 1) 二进制（零修改拷贝）
  cp "$src" "$dir/superapi"
  chmod 755 "$dir/superapi"

  # 2) 激活直通配置（不改二进制即可过激活门）
  cat > "$dir/config.json" <<'JSON'
{
  "activated": true,
  "activationKey": "local"
}
JSON
  chmod 600 "$dir/config.json"

  # 3) 账号文件模板（用 -token 时可不填；多账号池则填入 credential 数组）
  cat > "$dir/accounts.json" <<'JSON'
{
  "_comment": "多账号池模板：用 -token 单账号时可留空数组；多账号时按 credential 结构填 token/email/proxy",
  "accounts": []
}
JSON
  chmod 600 "$dir/accounts.json"

  # 4) 启动脚本（凭据全部走环境变量）
  cat > "$dir/run.sh" <<'SH'
#!/bin/sh
# SuperAPI 反代启动脚本 —— 凭据走环境变量，不落脚本
set -e
DIR=$(cd "$(dirname "$0")" && pwd)

TOKEN="${SUPERAPI_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  if [ -f "$DIR/superapi.env" ]; then
    . "$DIR/superapi.env"
    TOKEN="${SUPERAPI_TOKEN:-}"
  fi
fi
[ -n "$TOKEN" ] || { echo "[FATAL] 未提供 SUPERAPI_TOKEN（DeepSeek 账号 userToken）"; exit 1; }

PORT="${SUPERAPI_PORT:-8080}"
APIKEY="${SUPERAPI_APIKEY:-changeme}"

# 激活直通配置（缺失则补写）
[ -f "$DIR/config.json" ] || printf '{\n  "activated": true,\n  "activationKey": "local"\n}\n' > "$DIR/config.json"

cd "$DIR"
exec ./superapi \
  -host "${SUPERAPI_HOST:-0.0.0.0}" \
  -port "$PORT" \
  -apikey "$APIKEY" \
  -token "$TOKEN" \
  -model "${SUPERAPI_MODEL:-deepseek-v4.1-flash}" \
  ${SUPERAPI_PROXY:+-proxy "$SUPERAPI_PROXY"} \
  -server-only -no-test -log
SH
  chmod 755 "$dir/run.sh"

  # 5) systemd 单元
  cat > "$dir/superapi.service" <<'UNIT'
[Unit]
Description=SuperAPI Gateway (DeepSeek relay)
Documentation=file:/opt/superapi/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/superapi
EnvironmentFile=-/opt/superapi/superapi.env
ExecStart=/opt/superapi/run.sh
Restart=always
RestartSec=3
LimitNOFILE=65535
# 加固（按需调整）
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

  # 6) 环境变量模板
  cat > "$dir/superapi.env" <<'ENV'
# SuperAPI 运行时环境（systemd EnvironmentFile）—— 填好后 chmod 600
# DeepSeek 账号 userToken（浏览器 chat.deepseek.com 登录后 Console 取）
SUPERAPI_TOKEN=
# 监听端口
SUPERAPI_PORT=8080
# 客户端访问本网关所需的 API Key（自己定，客户端填这个）
SUPERAPI_APIKEY=changeme
# 默认模型
SUPERAPI_MODEL=deepseek-v4.1-flash
# 出站代理（可选，如 http://127.0.0.1:7890）
SUPERAPI_PROXY=
ENV
  chmod 600 "$dir/superapi.env"

  # 7) 部署说明
  cat > "$dir/README.md" <<'MD'
# SuperAPI Linux 部署包

把 DeepSeek 账号反代成 OpenAI 兼容 API。**单文件服务，无依赖**（静态编译，无需额外运行库）。

## 一分钟部署

```bash
# 1) 拷贝到服务器
scp -r superapi-linux-amd64/ root@YOUR_SERVER:/opt/superapi

# 2) 填凭据（DeepSeek userToken）
ssh root@YOUR_SERVER
cd /opt/superapi
chmod 600 superapi.env
vi superapi.env          # 填 SUPERAPI_TOKEN=你的userToken，改 SUPERAPI_APIKEY=你的访问密钥

# 3) 前台试跑（验证能起来）
./run.sh
#   看到 "SuperAPI Gateway Server Started" 即成功

# 4) 装成 systemd 服务（开机自启 + 崩溃重拉）
cp superapi.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now superapi
systemctl status superapi --no-pager

# 5) 验证
curl http://127.0.0.1:8080/healthz
curl -H "Authorization: Bearer 你的APIKEY" http://127.0.0.1:8080/v1/models
```

## 客户端接入

```
Base URL : http://YOUR_SERVER:8080/v1
API Key  : superapi.env 里 SUPERAPI_APIKEY 的值
Model    : deepseek-v4.1-flash
```

## 端点

| 端点 | 说明 |
|---|---|
| `/v1/chat/completions` | OpenAI 兼容对话（支持 stream） |
| `/v1/models` | 模型列表 |
| `/v1/user`、`/v1/balance` | 账号/余额信息 |
| `/anthropic/v1/messages`、`/v1/messages` | Anthropic 方言 |
| `/api/chat`、`/api/generate`、`/api/tags` | Ollama 方言 |
| `/healthz`、`/status` | 健康与状态 |

## 运维

```bash
journalctl -u superapi -f          # 看日志
systemctl restart superapi         # 重启
vi superapi.env && systemctl restart superapi   # 换 token/端口
```

## 常见问题

- **token 过期**：跑到 `/status` 看账号是否 `is_available`；过期就回 chat.deepseek.com Console 重取 userToken，更新 `superapi.env` 后重启
- **换端口**：改 `SUPERAPI_PORT`，重启
- **多账号池**：`accounts.json` 填多个 credential（token/email/proxy），实现轮询与熔断——此时可不传 `-token`
- **防火墙**：对外暴露前请改掉默认 `SUPERAPI_APIKEY`，并在安全组/防火墙限制来源

## 组成

| 文件 | 作用 |
|---|---|
| `superapi` | 主程序（静态 ELF，零依赖） |
| `config.json` | 激活直通配置 |
| `accounts.json` | 账号池模板（可选） |
| `run.sh` | 启动脚本（读环境变量） |
| `superapi.service` | systemd 单元 |
| `superapi.env` | 环境变量模板 |
MD

  echo "    $(ls -1 "$dir" | wc -l | tr -d ' ') 个文件 → $dir"
}

mkdir -p "$OUT"
build_arch amd64 "$UNPACKED/superapi-linux-amd64"
build_arch arm64 "$UNPACKED/superapi-linux-arm64"

echo
echo "==> 完成。目录："
ls -la "$OUT"
