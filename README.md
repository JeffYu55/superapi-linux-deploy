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
| `docs/DEPLOY.md` | 服务器部署手册 |
| `docs/REVERSE_NOTES.md` | 逆向要点：激活链、参数表、实例锁、PoW |

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

## 注意

- **单实例锁**：程序默认只允许一个实例（防账号并发超限）。换 token 或换端口后必须**先停旧实例**再启动，否则新实例会在 15 秒后自动退出。要多开加 `-multi-instance`。
- **凭据安全**：`SUPERAPI_TOKEN`（DeepSeek 账号 userToken）与 `SUPERAPI_APIKEY`（客户端钥匙）都放 `superapi.env`（`chmod 600`）。本仓库的 `.gitignore` 已排除所有凭据文件与二进制。
- **token 有效期**：过期后 `/status` 会显示账号不可用，重取 userToken 更新 `superapi.env` 再 `systemctl restart superapi`。
- **对外暴露**：务必修改默认 `SUPERAPI_APIKEY`，并在安全组限制来源 IP。
