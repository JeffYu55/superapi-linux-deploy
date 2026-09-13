# 转换层修复代理（superapi-fix-proxy）

SuperAPI 把 DeepSeek 网页版的 LLM 接口转成 OpenAI 兼容接口。**模型能力没问题，问题出在转换层的四个缝**——
本代理坐在 DSH 与 SuperAPI 之间，专门把这些缝补上。

```
DSH/客户端 → 127.0.0.1:18091（本代理） → 127.0.0.1:18090（SuperAPI） → chat.deepseek.com
```

启动：

```sh
python3 tools/superapi-fix-proxy.py --port 18091 --upstream http://127.0.0.1:18090 --debug
# 可选：--heal-script /path/to/relay_heal.sh   （或环境变量 SUPERAPI_HEAL_SCRIPT）
```

---

## 一、四个缝，各自怎么补

### 1. 嵌套参数被压平成字符串（工具调用直接失败）

DeepSeek 网页版没有原生 function calling。SuperAPI 把工具参数序列化成文本塞进字段：

```
{"assertions": "<item><text><![CDATA[必须实际执行命令]]></text>…</item>"}
{"assertions": "[{\"text\":\"…\",\"severity\":\"major\"}]"}      ← 有时是 JSON 文本
{"assertions": "{\"text\":\"A\"},{\"text\":\"B\"}"}              ← 有时缺外层方括号
{"assertions": "<|EPSE|parameter name=\"text\">…"}               ← 有时是原生标记
```

DSH 期望 `array`，拿到 `string` → 闭环类工具全报错。

**补法**：按请求里的 `tools` schema 反向还原——JSON 数组 / 对象流 / `<item>` XML / EPSE 标记四条路都认。

### 2. 上游输出打满长度上限 → 嵌套参数尾部被砍半

实测样本：`assertions` 是**缺尾部 `]` 的截断 JSON 数组**，长度 309~455 字符不等；严格 `json.loads` 全败：

```
samples=10 list=6 str=4 strict_ok=0 strict_fail=4 salvage_ok=4 salvage_items=[5, 5, 4, 4]
```

**补法**：平衡括号扫描（感知字符串与转义）+ 尾部残片补引号/花括号/方括号，**丢掉半条、保住前面 N 条**；
外层 `arguments` 本身被截断时按 schema 逐字段抽取。上表 4 个失败样本全部救回。

请求侧同时做「降维」：把 `array/object` 参数改成 `string` 并附 JSON 格式示例与紧凑约束
（整体 ≤400 字符、条目 ≤4 条、字段值 ≤40 字），从源头降低被截断的概率。

### 3. EPSE 标记漏进上下文（越滚越脏）

DeepSeek 原生协议标记 `<|EPSE|…>` 会残留在 **`reasoning_content`**（模型在思考里"念协议"）：

```
6 次采样: hits = {content: 0, reasoning_content: 2, arguments: 0}
片段: 「输出必须是工具调用块，第一个非空白字符是 <|EPSE|tool_calls>」
```

DSH 把它存进会话历史、下一轮又回传给上游 → 回声循环。

**补法**：双向清污。响应侧洗 `reasoning_content` / `content` / `tool_calls.arguments`（只摘标记 token，保留正文）；
请求侧洗历史 `messages`（含多模态 content 数组）。流式路径先 join 再洗，顺带解决标记跨 SSE 分片被截断的漏网。

### 4. 上游不可达 → 502 → 客户端连退 5 次

反代/虚拟机一死，18090 直接 `Connection refused`。代理原样吐 502，DSH 的 llm-retry 把它当 SERVER 类错误连退 5 次：

```
DSH llm-retry 默认: initialDelayMs=500 maxDelayMs=10000 jitterRatio=0.1 maxRetries=5
第 5 次退避 = min(500×2^4, 10000) × jitter(0.9~1.1) = 8000 × 1.0998 = 8798ms
```

**补法**（三层）：

| 层 | 行为 |
|---|---|
| 短重试 | 识别拒连/重置 → 3 次短蹭（0.3/0.6s，总 ≤0.9s）→ 仍不行才回错；**绝不**混进空补全那套 0.8/1.6/2.4s 退避 |
| 报错面 | 换成可操作人话（谁不可达 / 探活命令 / 修复命令）+ `Retry-After: 10`（把 8s 倍数退避压成 10s 节奏） |
| 自愈 | 调 `tools/relay_heal.sh`：VM 停则 `limactl start`、进程死则起 `run.sh`；就绪判据是 **`/status` 回 200**（不是 TCP 通）；自愈后整轮重试 |

### 附：空补全与重试

上游偶发返回空补全（`completion_tokens: 0`、`content: " "`、`finish: stop`），且**成簇出现**。
代理对「出了响应但不可用」（无 tool_call / 参数救不回 / 空补全）退避重试 3 次（0.8/1.6/2.4s）+ 重试时追加一句推力提示。

---

## 二、效果（同一固定请求，10 次一批）

| 配置 | 非流式 | 流式 |
|---|---|---|
| 只用 SuperAPI（无代理） | 6/10 | — |
| 本代理（截断抢救 + 降维 + 重试） | **10/10** | **9/10** |

自愈实测：杀反代进程 → **同一请求内 4.6~6.1s 恢复 200、零 502**；整机停（模拟 Mac 睡眠）→ **35s 内第 3 发自动恢复**。

---

## 三、排障速查

| 症状 | 根因 | 处置 |
|---|---|---|
| `/status` 显示 `is_circuit_broken`、`401 (封号/失效)` | userToken 失效（网页版重登会顶掉旧 token） | `sh tools/ds_auth.sh check` → `set <新token>` |
| `/status` 显示 `is_cooling_down`、`available=0`，POST 挂起 | 反代自身冷静期（`config.json`: `cooldown_minutes:15`、`queue_enabled:true`），连续 3 次 error 触发 | 降 `cooldown_minutes` 到 1~2、关 `queue_enabled` 让它快速失败；有第二个账号才是真容灾 |
| 18090 直接 `Connection refused` | VM 停了（Mac 睡眠/合盖最常见） | `sh tools/relay_heal.sh`；装看门狗 plist 后 30s 内自动恢复 |
| 换 token 后反代起不来/没响应 | **单实例锁**：旧进程没死透，新实例 12s 倒计时自杀 | 等 `ps -ef \| grep -c '[.]/superapi'` 归零再起（`ds_auth.sh set` 已内置等待） |
| launchd 起的自愈失效、日志 `Operation not permitted` | `~/Downloads`、`~/Documents`、`~/Desktop` 受 TCC 保护 | 脚本放 `~/.dsh/`，别放受保护目录 |
| launchd 起的自愈找不到 `limactl` | launchd 的 PATH 没有 `/opt/homebrew/bin` | 脚本内部走绝对路径探测（已内置） |
| 自愈"成功"了但重打仍失败 | 端口先通、Go 还没开始服务 → `RemoteDisconnected` | 就绪判据用 `/status` 200（已内置） |

## 四、能力边界（实测，别指望）

- **长会话**：DSH 侧 750k token 输入预算会压爆，触发上下文压缩死锁 —— 与本转换层无关，属于上游账号额度限制
- **agent 多轮**：headless 跑多轮工具循环在本链路上不现实（单轮 30KB/8 工具请求约 7s，但轮数一多就撞额度）
- 免费账号**没有配额保证**：突发请求会踩反代冷静期，批测请串行、留冷却
