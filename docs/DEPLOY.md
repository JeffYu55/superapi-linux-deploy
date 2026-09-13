# 部署手册

## 一、准备

| 项 | 要求 |
|---|---|
| 服务器 | Linux x86_64 或 aarch64，1 核 256MB 起（静态二进制，无运行时依赖） |
| 凭据 | DeepSeek 账号 userToken（App/网页版登录后获取） |
| 二进制 | SuperAPI Linux 版（见仓库 README「准备」一节），或直接取本仓库 Release 里的发行包 |

**取 DeepSeek userToken**：浏览器登录 `chat.deepseek.com` → F12 → Console：

```js
JSON.parse(localStorage.getItem('userToken')).value
```

复制打印出的那串（`eyJ` 或随机串），有效期见 JWT `exp`，过期重取。

## 二、部署（四步）

```bash
# 1) 上传并解压
scp superapi-linux-amd64.tar.gz root@SERVER:/tmp/
ssh root@SERVER
mkdir -p /opt/superapi && tar -xzf /tmp/superapi-linux-amd64.tar.gz -C /opt
mv /opt/superapi-linux-amd64/* /opt/superapi/ && rmdir /opt/superapi-linux-amd64

# 2) 填凭据
cd /opt/superapi
chmod 600 superapi.env
vi superapi.env
#   SUPERAPI_TOKEN=<你的 userToken>
#   SUPERAPI_APIKEY=<自定义的客户端访问密钥>
#   SUPERAPI_PORT=8080

# 3) 前台试跑（确认能起来，Ctrl+C 退出）
./run.sh

# 4) 装 systemd 服务
cp superapi.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now superapi
systemctl status superapi --no-pager
```

## 三、验证

```bash
curl http://127.0.0.1:8080/healthz
# {"accounts":{"available":1,"total":1},"status":"healthy",...}

curl -H "Authorization: Bearer $SUPERAPI_APIKEY" http://127.0.0.1:8080/v1/models

curl -H "Authorization: Bearer $SUPERAPI_APIKEY" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8080/v1/chat/completions \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

`/status` 可看账号可用数、调用计数、token 装载情况（`accounts_detail[].token` 只显示首尾）。

## 四、客户端接入

任何 OpenAI 兼容客户端：

```
Base URL : http://SERVER:8080/v1
API Key  : SUPERAPI_APIKEY 的值
Model    : deepseek-v4.1-flash
```

其他方言：

| 方言 | 路径 |
|---|---|
| Anthropic | `http://SERVER:8080/anthropic/v1/messages` 或 `/v1/messages` |
| Ollama | `http://SERVER:8080/api/chat`、`/api/generate`、`/api/tags` |
| Gemini | `/v1beta/models/...` |

## 五、日常运维

```bash
journalctl -u superapi -f                       # 实时日志
systemctl restart superapi                      # 重启
systemctl stop superapi                         # 停止

# 换 token / 换端口
vi /opt/superapi/superapi.env
systemctl restart superapi

# 换模型默认值
#   superapi.env 里改 SUPERAPI_MODEL，重启
```

## 六、排障

| 现象 | 原因与处理 |
|---|---|
| `[INSTANCE] 检测到已有实例正在运行` | 单实例锁：先停旧进程再启；确实要多开则加 `-multi-instance` |
| `[FATAL] account file not found: accounts.json` | 传了 `-account` 但文件不存在；单账号用 `-token` 即可，不必用账号文件 |
| `[FATAL] 未提供 SUPERAPI_TOKEN` | `superapi.env` 未填 token，或 run.sh 未读到该文件 |
| `/status` 显示账号 `is_available: false` | token 过期或账号被限；重取 userToken |
| 端口占用 | 改 `SUPERAPI_PORT`，或先 `ss -lntp` 查出占用进程 |
| 502 / `POW_FAIL` | 上游风控（PoW 校验）或网络问题；稍后重试，必要时设 `SUPERAPI_PROXY` |

## 七、安全建议

1. **必须**改掉默认 `SUPERAPI_APIKEY`（默认值是 `changeme`）
2. 安全组/防火墙只放行必要来源 IP；或放在 Nginx/Caddy 后加 HTTPS 与限速
3. `superapi.env` 保持 `chmod 600`，不要提交到任何代码仓库
4. 定期轮换 userToken 与 API Key
