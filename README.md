# SuperAPI Linux 部署工具链

把 **SuperAPI**（作者：Dszsu & llucccian）打包成可直接部署到 Linux 服务器的发行包，
把 DeepSeek 账号（App/网页版会话）反代成 **OpenAI / Anthropic / Ollama 兼容 API**。

> **归属声明**：本仓库**不包含** SuperAPI 程序本体（版权归原作者 Dszsu & llucccian，交流 QQ 群 1106465300）。
> 这里只有打包脚本、校验判据与部署文档，用于**自用部署**。二进制请自备（见「准备」一节），
> 或从本仓库的 [Releases](../../releases) 取已打包的发行包。
> 请遵守原作者许可与所在地法律，勿用于商业转售。

---

## 这是什么

SuperAPI 本身是一个多方言 LLM 网关：账户池、熔断、PoW 反爬、会话管理都内置。
本工具链解决的是「**让它跑在 Linux 服务器上、用 systemd 托管、一条命令部署**」这件事：

| 组件 | 作用 |
|---|---|
| `packaging/build_server_pack.sh` | 生成 amd64/arm64 双架构部署包（7 件套） |
| `tools/d1_pack_check.py` | 部署包结构与可执行性校验（含隔离重构） |
| `tools/d2_linux_x86_check.py` | 真 Linux 端到端：起服 → 探活 → 真对话 |
| `tools/d3_dist_check.py` | 发行归档打包与解压校验（防 AppleDouble 污染） |
| `tools/superapi-fix-proxy.py` | **转换层修复代理**：补嵌套参数/截断抢救/EPSE 清污/连接容错与自愈 |
| `tools/relay_heal.sh` | 反代自愈：VM 停则启动、进程死则拉起，直到 `/status` 就绪 |
| `tools/ds_auth.sh` | DeepSeek userToken 验活 / 换取 / 重启反代（带单实例锁等待） |
| `deploy/launchd/*.plist` | macOS 常驻托管模板（代理 + 反代看门狗） |
| `docs/DEPLOY.md` | 服务器部署手册 |
| `docs/PROXY.md` | 转换层修复原理、效果数据与排障速查 |
| `docs/REVERSE_NOTES.md` | 逆向要点：激活链、参数表、实例锁、PoW |

> **客户端接哪条链路**：直连性能最好，但 SuperAPI 的转换层有四个已知缝（嵌套参数被压成字符串、
> 输出截断丢半条、`<|EPSE|…>` 标记漏进上下文、上游挂了直接 502）。用 agent/工具调用类客户端
> （如 DSH）时，建议把 Base URL 指向本代理 `18091`，细节与实测数据见 [`docs/PROXY.md`](docs/PROXY.md)。

---

## 快速部署

```bash
# 方式 A：一键安装（推荐）——clone 后跑一条命令，自动拉 Release 发行包
git clone https://github.com/JeffYu55/superapi-linux-deploy.git
cd superapi-linux-deploy && bash install.sh
vi /opt/superapi/superapi.env    # 填 SUPERAPI_TOKEN 与 SUPERAPI_APIKEY（install.sh 已建好空模板）
cd /opt/superapi && ./run.sh     # 前台试跑，出现 "SuperAPI Gateway Server Started" 即成功
cp /opt/superapi/superapi.service /etc/systemd/system/ \
  && systemctl daemon-reload && systemctl enable --now superapi

# 方式 B：直接下发行包（不经 git）
#   curl -LO https://github.com/JeffYu55/superapi-linux-deploy/releases/download/v1.0.0/superapi-linux-amd64.tar.gz
#   tar -xzf superapi-linux-amd64.tar.gz -C /opt && mv /opt/superapi-linux-amd64 /opt/superapi
#   cd /opt/superapi && vi superapi.env && cp superapi.service /etc/systemd/system/ \
#     && systemctl daemon-reload && systemctl enable --now superapi

curl http://127.0.0.1:8080/healthz
```

> `install.sh` 只做搬运与写模板：**不启动服务、不改 systemd、不覆盖已存在的 `superapi.env`**；
> 可用 `SUPERAPI_DEST=~/superapi` 换安装目录、`SUPERAPI_VERSION=v1.0.0` 指定版本。
>
> **本仓库不含程序本体**——直接 `git clone` 得到的是脚本与文档，发行二进制通过
> [Releases](../../releases) 分发（`install.sh` 会自动拉取并校验 SHA256）。

客户端接入：

```
Base URL : http://YOUR_SERVER:8080/v1
API Key  : superapi.env 里的 SUPERAPI_APIKEY
Model    : deepseek-v4.1-flash
```

详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## 准备：自备程序本体

本仓库不含二进制。自备方式：

1. 从原作者处获取 SuperAPI 发行版（Windows/Android 等）
2. 取 Linux 版本二进制（若为 UPX 加壳，先解壳：`upx -d superapi-linux-amd64`）
3. 放到 `unpacked/` 目录（或设 `SUPERAPI_BIN_DIR` 指向别处），然后：

```bash
bash packaging/build_server_pack.sh          # 产出 dist-server/superapi-linux-{amd64,arm64}/
python3 tools/d1_pack_check.py               # 结构校验
```

---

## 端点速查

| 端点 | 说明 |
|---|---|
| `/v1/chat/completions` | OpenAI 兼容对话（支持 `stream`） |
| `/v1/models` | 模型列表 |
| `/v1/user`、`/v1/balance` | 账号 / 余额信息 |
| `/anthropic/v1/messages`、`/v1/messages` | Anthropic 方言 |
| `/api/chat`、`/api/generate`、`/api/tags` | Ollama 方言 |
| `/healthz`、`/status` | 健康 / 状态（含账号可用数、调用计数） |

---

## agent 客户端接入（含 macOS 常驻托管）

直接连 18090 能用，但工具调用会踩转换层的缝。给 agent 用的时候走代理：

```bash
# 1) 起代理（或交给 launchd 托管，见下）
python3 tools/superapi-fix-proxy.py --port 18091 --upstream http://127.0.0.1:18090 --debug

# 2) 客户端 Base URL 指向它
#    Base URL : http://127.0.0.1:18091/v1
#    API Key  : 与 SuperAPI 的 SUPERAPI_APIKEY 相同
```

macOS 想常驻（登录自启 + 崩了自拉 + 反代被睡眠打死自动救活）：

```bash
mkdir -p ~/.dsh ~/Library/LaunchAgents
cp tools/relay_heal.sh ~/.dsh/            # 必须放 TCC 保护目录之外（别放 ~/Downloads）
cp tools/superapi-fix-proxy.py ~/.dsh/

for f in fix-proxy relay-watchdog; do
  sed "s#__HOME__#$HOME#g" deploy/launchd/com.jeff.superapi-$f.plist \
    > ~/Library/LaunchAgents/com.jeff.superapi-$f.plist
  launchctl load -w ~/Library/LaunchAgents/com.jeff.superapi-$f.plist
done

launchctl list | grep superapi      # 两个作业都在即可
sh tools/relay_heal.sh              # 手动救一次（VM 停/进程死都管）
sh tools/ds_auth.sh check           # token 还活着吗（40003 = 失效，重取）
```

> 为什么脚本要放 `~/.dsh/`：`~/Downloads`、`~/Documents`、`~/Desktop` 受 macOS TCC 保护，
> **launchd 起的进程读不了**，会以 `Operation not permitted` 静默失败。
> 同理，launchd 的 PATH 里没有 `/opt/homebrew/bin`，所以自愈脚本内部走绝对路径找 `limactl`。

---

## 注意

- **单实例锁**：程序默认只允许一个实例（防账号并发超限）。换 token 或换端口后必须**先停旧实例**再启动，否则新实例会在 15 秒后自动退出。要多开加 `-multi-instance`。
- **凭据安全**：`SUPERAPI_TOKEN`（DeepSeek 账号 userToken）与 `SUPERAPI_APIKEY`（客户端钥匙）都放 `superapi.env`（`chmod 600`）。本仓库的 `.gitignore` 已排除所有凭据文件与二进制。
- **token 有效期**：过期后 `/status` 会显示账号不可用，重取 userToken 更新 `superapi.env` 再 `systemctl restart superapi`。
- **对外暴露**：务必修改默认 `SUPERAPI_APIKEY`，并在安全组限制来源 IP。
