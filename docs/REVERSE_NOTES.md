# 逆向与行为要点

对 SuperAPI（Dszsu & llucccian）Linux 版可执行文件的静态分析与实测行为记录，**用于正确部署与排障**。
不含程序本体，也不含任何绕过收费授权的产物——激活状态通过配置文件声明（见下），二进制零修改。

## 一、程序形态

- 静态链接 ELF（Go 1.27 构建，`-s -w` stripped），无运行时依赖
- 支持 `linux/amd64`、`linux/aarch64`；另有 Windows 与 Android（JNI 库）版本
- 原始发行版常以 UPX 加壳；解壳：`upx -d ./superapi-linux-amd64`（若头部被改，需先修正 UPX 魔数）

## 二、完整参数表（`--help` 实测）

| 参数 | 说明 |
|---|---|
| `-activate <key>` | 首次启动的授权激活密码 |
| `-token <userToken>` | **DeepSeek Bearer token（覆盖账号文件）** ← 单账号最简用法 |
| `-account <path>` | 账号池配置（默认 `accounts.json`） |
| `-account-proxy` | 按账号走独立出站代理 |
| `-apikey <key>` | 客户端访问本网关所需的 API Key（守门） |
| `-config <path>` | 持久化配置（默认 `config.json`） |
| `-host` / `-port` | 监听地址与端口（默认 `0.0.0.0:8080`） |
| `-model <name>` | 默认模型（默认 `deepseek-v4.1-flash`） |
| `-proxy <url>` | 出站代理（HTTP/SOCKS5） |
| `-server-only` | 纯 HTTP 后台模式（无交互 CLI）——**部署必用** |
| `-no-test` / `-startup-test` | 跳过 / 启用启动时账号探活 |
| `-thinking` / `-dual-write-think` | 思考模式 / 双写 thinking |
| `-queue` | 并发削峰队列 |
| `-risk-detect` | 移动端风控拦截检测 |
| `-ollama` | Ollama 协议兼容（`/api/*`） |
| `-multi` / `-multi-instance` | 允许多实例并行 |
| `-system <text>` | 自定义系统提示 |
| `-log` | 启动期详细日志 |

## 三、激活与持久化

- 授权状态在**持久化配置**中声明（字段 `activated`），程序启动时读取；首次通过 `-activate` 写入
- 部署时在 `config.json` 中直接声明已激活状态即可跳过交互提示，**无需修改二进制**
- 附带的自检逻辑（含完整性/环境探测相关符号）用于识别调试与注入环境；保持二进制原样即可避免误触

## 四、单实例锁

- 默认只允许一个实例运行（防止账号并发超限与会话冲突）
- 检测到已有实例时打印 `[INSTANCE] 检测到已有 SuperAPI 实例正在运行`，并在 15 秒倒计时后退出
- **部署启示**：换 token/端口后必须**先停旧实例**再启动；systemd 的 `restart` 能正确替换进程
- 需要并行时加 `-multi-instance`

## 五、上游协议（DeepSeek Web/App 侧）

程序的账号池面向 DeepSeek 的 App/网页接口，具备：

- **PoW 挑战应答**：请求携带 `x-ds-pow-response`（含 challenge/salt/answer/signature），由内置求解器产出；会话初始化时会先取挑战
- **会话管理**：`chat_session/create` 建会话，`chat_session_id` 贯穿续聊
- **流式解析**：上游返回 SSE 帧（`data: {...}`），程序转译为各方言格式下发
- **风控信号**：错误语义包含 `POW_FAIL`、`challenge rejected`、`session HTTP <code>`、`completion HTTP <code>` 等

## 六、账户池语义（对 `/status` 读数的解释）

| 字段 | 含义 |
|---|---|
| `accounts.available` / `total` | 可用 / 总账号数——为 0 说明全部熔断或不可用 |
| `is_available` | 该账号当前是否可调度 |
| `is_circuit_broken` | 连续失败触发熔断 |
| `is_cooling_down` / `cooldown_until` | 冷却中（失败后退避） |
| `is_muted` / `mute_until` | 静默（鉴权类错误触发） |
| `pool_type` | 账号所属池（默认池 / 弹性池） |
| `error_count` / `success_count` / `total_calls` | 调用计数与错误计数 |
| `token` | 账号凭据（仅显示首尾） |

错误码语义：`BALANCE_EMPTY`（额度耗尽）、`NETWORK_ERROR`、`pool is empty`（池全不可用）等。

## 七、账号池配置（多账号）

`accounts.json` 支持多账号轮询与熔断，字段形如：

```json
{
  "accounts": [
    { "credential": { "token": "<userToken-1>", "email": "a@example.com" } },
    { "credential": { "token": "<userToken-2>", "proxy": "http://127.0.0.1:7890" } }
  ]
}
```

多账号时可省略 `-token`（`-token` 会覆盖账号文件，视作单账号）。

## 八、实测环境

- Alpine Linux 3.24（内核 6.18）aarch64 / x86_64 双架构
- 静态二进制直接执行，无缺失依赖；端口就绪后 `/healthz` 返回 `healthy`
- 真实对话返回正常并计入 `total_calls`（流式逐帧下发、`[DONE]` 收尾）
