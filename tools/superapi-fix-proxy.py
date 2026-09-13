#!/usr/bin/env python3
"""
superapi-fix-proxy — SuperAPI 工具调用修复代理
------------------------------------------------
病：SuperAPI 把 DeepSeek 网页版的 XML 工具参数**压平成字符串**塞进 JSON
    （{"assertions": "<item><text><![CDATA[..]]></text></item>"}），
    DSH 期望 array/object → 解析失败 → 闭环工具全报错。

药：本代理位于 DSH 与 SuperAPI 之间，按请求里的 tools schema
    **把被压平的 XML/CDATA 字符串还原成结构化 JSON**，再交给 DSH。

链路：DSH → :18091(本代理) → :18090(SuperAPI) → DeepSeek 免费账号
"""
import copy, json, os, re, socket, subprocess, sys, time, http.server, urllib.request, urllib.error

def _arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

UPSTREAM = _arg("--upstream", "http://127.0.0.1:18090")
PORT = int(_arg("--port", "18091"))
DEBUG = "--debug" in sys.argv
SALVAGE_HITS = [0]        # 截断抢救命中计数（每响应重置；批测用来看上游截断率）
RETRY_LIMIT = 3           # 上游响应不可用（空补全/无 tool_call/救不回）时的整请求重试次数
CONNECT_RETRIES = 3       # 上游「连接被拒」时的短重试次数（首次 + 2 次蹭）
CONNECT_DELAYS = (0.3, 0.6)   # 短重试退避（总等待 ≤0.9s）
RELAY_PORT = 18090        # 我们部署的反代端口（自愈只对它生效）
HEAL_SCRIPT = _arg("--heal-script",
                     os.environ.get("SUPERAPI_HEAL_SCRIPT",
                                    os.path.join(os.path.expanduser("~"), ".dsh", "relay_heal.sh")))
# 放 ~/.dsh/ 而不是 ~/Downloads：launchd 起的进程读不了 TCC 保护目录
HEAL_WAIT = 10.0          # 就地等待自愈的上限（VM 冷启更久，脚本会继续在后台跑）
HEAL_COOLDOWN = [0.0]     # 防风暴：两次「拉起脚本」的最小间隔（秒）——脚本幂等，健康时 0.1s 退

def _port_open(port, timeout=0.3):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout)
        s.close()
        return True
    except Exception:
        return False

def _api_ready(timeout=2.0):
    """真就绪：/status 能回 200。端口先通但服务没起时，裸连接会 RemoteDisconnected"""
    if not _port_open(_upstream_port()):
        return False
    try:
        req = urllib.request.Request(UPSTREAM + "/status",
                                     headers={"Authorization": "Bearer %s" % _arg("--apikey", "gittest")})
        return urllib.request.urlopen(req, timeout=timeout).status == 200
    except Exception:
        return False

def _upstream_port():
    m = re.search(r":(\d+)", UPSTREAM)
    return int(m.group(1)) if m else RELAY_PORT

# -------- 连接层：上游（反代/VM）不在时别把 urllib 原文丢给上层 --------
def _is_conn_error(e):
    """连接被拒/主机不可达 —— 这不是「响应不可用」，不该走 4.8s 那套退避"""
    reason = getattr(e, "reason", e)
    txt = "%s %s" % (e, reason)
    return ("Connection refused" in txt or "Errno 61" in txt
            or "Connection reset" in txt or "No route to host" in txt
            or isinstance(reason, (ConnectionRefusedError, ConnectionResetError)))

def _upstream_error_body(e):
    """可操作的人话错误体：谁不可达、怎么探活、怎么修"""
    msg = ("上游反代 %s 连不上（%s）。这通常不是模型问题，是反代进程或虚拟机没起。"
           "探活：curl %s/status ｜ 修复：sh <repo>/tools/relay_heal.sh（VM 停则 start、进程死则起）；"
           "若是 token 失效，用 tools/ds_auth.sh set <新userToken>（带验活+重启反代）"
           % (UPSTREAM, str(getattr(e, "reason", e))[:80], UPSTREAM))
    return json.dumps({"error": {"message": msg, "type": "upstream_unreachable"}},
                      ensure_ascii=False).encode()

# -------- EPSE 清污：DeepSeek 原生工具协议标记 <|EPSE|…> 残留在正文/思考里 --------
EPSE_MARK = re.compile(r"</?\|EPSE\|[^>]*>?")
EPSE_HITS = [0]           # 清洗命中计数（每请求重置）

def scrub_epse(text):
    """只摘标记 token、保留正文；无标记时零开销（返回原对象）"""
    if not isinstance(text, str) or "|EPSE|" not in text:
        return text
    return EPSE_MARK.sub("", text)

def scrub_messages(payload):
    """请求侧清污：历史里残留的 EPSE 标记先洗掉，断掉回上游的回声循环"""
    n = 0
    for m in (payload.get("messages") or []):
        if not isinstance(m, dict):
            continue
        for fld in ("content", "reasoning_content"):
            v = m.get(fld)
            if isinstance(v, str) and "|EPSE|" in v:
                m[fld] = scrub_epse(v); n += 1
            elif isinstance(v, list):
                for part in v:
                    if isinstance(part, dict) and isinstance(part.get("text"), str) \
                            and "|EPSE|" in part["text"]:
                        part["text"] = scrub_epse(part["text"]); n += 1
        for tc in (m.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            if isinstance(fn.get("arguments"), str) and "|EPSE|" in fn["arguments"]:
                fn["arguments"] = scrub_epse(fn["arguments"]); n += 1
    if n:
        EPSE_HITS[0] += n
        if DEBUG:
            sys.stderr.write("[proxy] 请求侧清污 %d 处 EPSE 标记\n" % n)
    return n

# -------- 截断抢救：上游输出打满长度上限 → 嵌套参数尾部被砍半 --------
def _scan_objects(s):
    """平衡扫描提取所有完整 {...}（感知字符串与转义）。
    返回 (完整对象列表, 残余深度, 残余起点, 是否停在字符串内)"""
    out, depth, start, instr, esc = [], 0, -1, False, False
    for i, ch in enumerate(s):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    v = json.loads(s[start:i + 1])
                    if isinstance(v, dict):
                        out.append(v)
                except Exception:
                    pass
                start = -1
    return out, depth, start, instr

def _salvage_array(s):
    """上游输出被截断时抢救出所有完整的数组成员：丢掉尾部半条，保住前面 N 条。"""
    s = (s or "").strip()
    if not s:
        return []
    objs, depth, start, instr = _scan_objects(s)
    if start >= 0:                      # 尾部残片：补引号/花括号/方括号再试
        frag = s[start:]
        for tail in (('"' if instr else "") + "}" * max(depth, 0) + "]",
                     ('"' if instr else "") + "}" * max(depth, 0),
                     "}" * max(depth, 0) + "]", "}" * max(depth, 0)):
            try:
                v = json.loads(frag + tail)
                if isinstance(v, dict):
                    objs.append(v)
                    break
            except Exception:
                continue
    if objs:
        SALVAGE_HITS[0] += 1
        if DEBUG:
            sys.stderr.write("[proxy] 截断抢救命中 %d 条\n" % len(objs))
    return objs

def _salvage_outer(arguments, schema):
    """外层 arguments JSON 本身被截断时，按 schema 逐字段抽取（字符串值允许未闭合）"""
    out = {}
    for key, sub in (schema.get("properties") or {}).items():
        m = re.search(r'"%s"\s*:\s*"' % re.escape(key), arguments)
        if not m:
            m2 = re.search(r'"%s"\s*:\s*(-?[0-9.]+|true|false|null)' % re.escape(key), arguments)
            if m2:
                try:
                    out[key] = json.loads(m2.group(1))
                except Exception:
                    pass
            continue
        i, buf, esc = m.end(), [], False
        while i < len(arguments):
            c = arguments[i]
            if esc:
                buf.append(c); esc = False
            elif c == "\\":
                buf.append(c); esc = True
            elif c == '"':
                break
            else:
                buf.append(c)
            i += 1
        body = "".join(buf)
        try:
            raw = json.loads('"' + body.rstrip("\\") + '"')
        except Exception:
            raw = body
        out[key] = restore(raw, sub)
    return out

# ---------------- 还原器（schema 驱动） ----------------
def norm_epse(raw):
    """把 DeepSeek 原生标记 <|EPSE|parameter name="x"> 规范化为 <x>…</x>"""
    if "<|EPSE|" not in raw:
        return raw
    # 去掉 <|EPSE|invoke ...> 等包裹，仅保留 parameter 对
    parts = re.split(r'<\|EPSE\|parameter name="([^"]+)">', raw)
    out = []
    for i in range(1, len(parts), 2):
        name = parts[i]
        val = parts[i + 1] if i + 1 < len(parts) else ""
        val = re.sub(r'<\|EPSE\|[^>]*>', '', val)   # 清尾随标记
        val = re.sub(r'</item>.*$', '', val, flags=re.S)
        out.append(f"<{name}>{val.strip()}</{name}>")
    if out:
        return "<item>" + "".join(out) + "</item>" if "<item>" not in raw else "".join(out)
    return raw

def restore(raw, schema):
    t = (schema or {}).get("type")
    if not isinstance(raw, str):
        return raw                       # 已是原生类型（bool/int/list/dict）直接透传
    raw = raw.strip()
    raw = norm_epse(raw)   # 兼容 DeepSeek 原生 EPSE 标记
    if t == "array":
        s = raw.strip()
        # ① 已经是 JSON 数组
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    return v
            except Exception:
                v = _salvage_array(s)     # 被截断 → 抢救完整成员
                if v:
                    return v
        # ② JSON 对象流：{"..."},{...}（缺外层方括号）——模型常见漂移
        if s.startswith("{"):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    return [v]
            except Exception:
                try:
                    v = json.loads("[" + s + "]")
                    if isinstance(v, list):
                        return v
                except Exception:
                    # 逐对象扫描兜底
                    objs = re.findall(r"\{.*?\}", s, re.S)
                    acc = []
                    for o in objs:
                        try:
                            acc.append(json.loads(o))
                        except Exception:
                            pass
                    if acc:
                        return acc
        # ③ XML <item> 形态
        items = re.findall(r"<item>(.*?)</item>", raw, re.S)
        if items:
            return [restore(it, schema.get("items", {})) for it in items]
        return []
    if t == "object":
        props = list((schema.get("properties") or {}).items())
        out = {}
        for k, sub in props:
            m = re.search(rf"<{re.escape(k)}>(.*?)</{re.escape(k)}>", raw, re.S)
            if m:
                out[k] = restore(m.group(1), sub)
        if not out:
            # 标签名漂移回退：取 item 内所有子标签，按 schema 顺序映射
            pairs = re.findall(r"<([a-zA-Z_][\w-]*)>(.*?)</\1>", raw, re.S)
            for i, (_, val) in enumerate(pairs):
                if i < len(props):
                    out[props[i][0]] = restore(val, props[i][1])
        if not out:
            # 截断的 JSON 对象：抢救第一个完整成员
            objs, _d, _s, _i = _scan_objects(raw)
            if objs:
                for k, sub in props:
                    if k in objs[0]:
                        out[k] = restore(objs[0][k], sub)
        return out
    m = re.search(r"<!\[CDATA\[(.*?)\]\]>", raw, re.S)
    val = (m.group(1) if m else raw).strip()
    if t == "integer":
        try: return int(re.sub(r"[^\d-]", "", val) or 0)
        except Exception: return 0
    if t == "number":
        try: return float(re.sub(r"[^0-9.\-]", "", val) or 0)
        except Exception: return 0.0
    if t == "boolean":
        return val.lower() in ("true", "1", "yes")
    return val

def schemas_from_request(payload):
    """工具名 → parameters schema"""
    out = {}
    for t in (payload.get("tools") or []):
        fn = t.get("function") or {}
        if fn.get("name") and fn.get("parameters"):
            out[fn["name"]] = fn["parameters"]
    return out

def fix_arguments(name, arguments, schemas):
    """按 schema 还原被压平的字段；返回 (新 arguments, 修复字段名列表)
    流式上游常不带 function.name → 回退：唯一工具直用 / 多工具逐个试取最佳"""
    schema = schemas.get(name)
    if not schema:
        if len(schemas) == 1:
            schema = next(iter(schemas.values()))
        elif schemas:
            best_fixed = []
            best_args = arguments
            for _n, _s in schemas.items():
                _a, _f = fix_arguments_by(_s, arguments)
                if len(_f) > len(best_fixed):
                    best_fixed, best_args = _f, _a
            return best_args, best_fixed
        if not schema:
            return arguments, []
    return fix_arguments_by(schema, arguments)

def fix_arguments_by(schema, arguments):
    """按给定 schema 还原；返回 (新 arguments, 修复字段名列表)"""
    if not schema:
        return arguments, []
    salvaged = False
    try:
        args = json.loads(arguments)
    except Exception:
        args = _salvage_outer(arguments or "", schema)   # 外层被截断 → 逐字段抽
        salvaged = True
    if not isinstance(args, dict) or not args:
        return arguments, []
    fixed = []
    for key, sub in (schema.get("properties") or {}).items():
        want = sub.get("type")
        cur = args.get(key)
        if want in ("array", "object") and isinstance(cur, str):
            try:
                converted = restore(cur, sub)
                if (want == "array" and isinstance(converted, list) and converted) or \
                   (want == "object" and isinstance(converted, dict) and converted):
                    args[key] = converted
                    fixed.append(key)
            except Exception:
                pass
        elif salvaged and key in args and args.get(key) not in (None, "", [], {}):
            fixed.append(key)
    return json.dumps(args, ensure_ascii=False), fixed


def _calls_ok(calls, schemas, want_tools):
    """响应可用性判定：要求出过工具调用，且 schema 期望 array/object 的字段不再残留字符串"""
    if want_tools and not calls:
        return False
    for fn in calls:
        schema = schemas.get(fn.get("name"))
        if not schema and len(schemas) == 1:
            schema = next(iter(schemas.values()))
        if not schema:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            return False
        if not isinstance(args, dict):
            return False
        for k, sub in (schema.get("properties") or {}).items():
            if isinstance(sub, dict) and sub.get("type") in ("array", "object") \
                    and isinstance(args.get(k), str):
                return False
    return True

def _json_complete(s):
    """判断缓冲的 arguments 是否已是完整 JSON"""
    if not s:
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def _example_of(sub, depth=0):
    """从 schema 生成一段示例 JSON 文本（给模型看格式）"""
    t = (sub or {}).get("type")
    if t == "array":
        it = _example_of(sub.get("items", {}), depth + 1)
        return f"[{it}]"
    if t == "object":
        parts = []
        for k, v in (sub.get("properties") or {}).items():
            parts.append(f'"{k}":{_example_of(v, depth + 1)}')
        return "{" + ",".join(parts) + "}"
    if t == "integer":
        return "0"
    if t == "number":
        return "0.0"
    if t == "boolean":
        return "true"
    return '"..."'

def flatten_tools(payload):
    """把 tools 里的 array/object 参数改成 string + JSON 格式说明（模型更能写对），
    返回值: (原 schema 映射, 是否改动过)"""
    originals = {}
    changed = False
    for t in (payload.get("tools") or []):
        fn = t.get("function") or {}
        name = fn.get("name")
        params = fn.get("parameters")
        if not name or not isinstance(params, dict):
            continue
        originals[name] = copy.deepcopy(params)   # 深拷贝：flatten 会原地改
        for k, sub in (params.get("properties") or {}).items():
            if not isinstance(sub, dict):
                continue
            if sub.get("type") in ("array", "object"):
                ex = _example_of(sub)
                old_desc = (sub.get("description") or "").strip()
                sub["type"] = "string"
                sub["description"] = (old_desc + " " if old_desc else "") + \
                    f"必须是一个 JSON 文本（不要用 XML/标记语言），格式示例：{ex}" \
                    "。内容务必紧凑：整体 ≤400 字符、数组条目 ≤4 条、每个字段值 ≤40 字" \
                    "（超出上游输出上限会被截断，导致整条调用作废）。"
                changed = True
        # required 不变（字段名保持）
    return originals, changed

# ---------------- 代理 ----------------
class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if DEBUG:
            sys.stderr.write("[proxy] " + (fmt % args) + "\n")

    def _fwd_headers(self):
        h = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "accept-encoding"):
                continue
            h[k] = v
        return h

    def do_GET(self):
        """转发 GET（DSH 会探测 /v1/models 等）"""
        try:
            req = urllib.request.Request(UPSTREAM + self.path,
                                         headers=self._fwd_headers(), method="GET")
            resp = urllib.request.urlopen(req, timeout=120)
            raw = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except urllib.error.HTTPError as e:
            err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
        except Exception as e:
            msg = json.dumps({"error": {"message": f"fix-proxy GET: {e}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                payload = {}
            schemas = schemas_from_request(payload)
            streaming = bool(payload.get("stream"))
            want_tools = bool(payload.get("tools"))
            scr_req = scrub_messages(payload)

            # 请求侧优化：嵌套 schema 降维成「字符串 + JSON 格式说明」
            fwd_body = body
            _changed = False
            if want_tools:
                _orig, _changed = flatten_tools(payload)
                if _changed and _orig:
                    schemas = _orig              # 响应侧按**原始** schema 还原
                if _changed and DEBUG:
                    sys.stderr.write("[proxy] 请求侧已降维 %d 个工具\n" % len(_orig))
            if _changed or scr_req:
                fwd_body = json.dumps(payload, ensure_ascii=False).encode()
                if scr_req and DEBUG:
                    sys.stderr.write("[proxy] 请求侧清污后转发体残留=%d\n"
                                     % fwd_body.decode("utf-8", "ignore").count("|EPSE|"))

            headers = self._fwd_headers()
            retry_body = fwd_body
            out, ok = b"", False
            for attempt in range(RETRY_LIMIT + 1):
                raw = self._upstream_post(retry_body, headers)
                if streaming:
                    out, ok = self._render_stream(raw, schemas, want_tools)
                else:
                    out, ok = self._fix_nonstream(raw, schemas, want_tools)
                if ok or attempt >= RETRY_LIMIT:
                    break
                time.sleep(0.8 * (attempt + 1))          # 退避：空补全成簇出现，立刻重打没用
                if want_tools:
                    retry_body = self._nudge(fwd_body)
                if DEBUG:
                    sys.stderr.write("[proxy] 上游响应不可用（第 %d 次）→ 退避重试\n" % (attempt + 1))

            self.send_response(200)
            if streaming:
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
            else:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except urllib.error.HTTPError as e:
            err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
        except urllib.error.URLError as e:
            msg = _upstream_error_body(e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "10")    # VM 冷启要 ~20s：让上层按 10s 节奏重试，别按 8s 倍数瞎退
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        except Exception as e:
            msg = json.dumps({"error": {"message": f"fix-proxy: {e}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _heal(self):
        """反代不在 → 拉起（VM 停则 start，进程死则起 run.sh）。带冷却，防并发风暴。
        只对自家反代端口生效；就地等到上限，脚本仍在后台继续把服务拉回来。"""
        if _upstream_port() != RELAY_PORT:
            return False
        now = time.time()
        if now - HEAL_COOLDOWN[0] < 3:
            # 3s 内刚拉过：脚本还在后台跑，就地等结果，不重复拉起（防风暴）
            if DEBUG:
                sys.stderr.write("[proxy] 自愈刚拉过（<3s），就地等就绪\n")
        else:
            HEAL_COOLDOWN[0] = now
            try:
                subprocess.Popen(["/bin/sh", HEAL_SCRIPT, str(int(HEAL_WAIT) + 60)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                if DEBUG:
                    sys.stderr.write("[proxy] 自愈脚本起不来: %r\n" % (e,))
                return False
        t0 = time.time()
        while time.time() - t0 < HEAL_WAIT:
            if _api_ready():
                if DEBUG:
                    sys.stderr.write("[proxy] 自愈成功（%.1fs，/status 已 200），重打上游\n" % (time.time() - t0))
                return True
            time.sleep(0.4)
        if DEBUG:
            sys.stderr.write("[proxy] 自愈未在上限内就绪（后台继续），先回 502+Retry-After\n")
        return False

    def _upstream_post(self, body, headers):
        """上游调用：连接被拒=反代/VM 没了 → 短蹭（≤0.9s）→ 自愈 → 整轮重来。
        绝不混进「响应不可用」那套 0.8/1.6/2.4s 退避（那是给空补全用的）"""
        last = None
        for rnd in range(2):
            last = None
            for i in range(CONNECT_RETRIES):
                try:
                    req = urllib.request.Request(UPSTREAM + self.path, data=body,
                                                 headers=headers, method="POST")
                    return urllib.request.urlopen(req, timeout=600).read()
                except Exception as e:
                    if not _is_conn_error(e):
                        raise
                    last = e
                    if i < CONNECT_RETRIES - 1:
                        time.sleep(CONNECT_DELAYS[min(i, len(CONNECT_DELAYS) - 1)])
                        if DEBUG:
                            sys.stderr.write("[proxy] 上游连接被拒 → 短重试 %d/%d\n"
                                             % (i + 1, CONNECT_RETRIES - 1))
            if rnd == 0 and self._heal():
                continue
            break
        raise last

    def _nudge(self, fwd_body):
        """重试时给上游加一句推力提示（空补全/漏 tool_call 的对症解）"""
        try:
            p = json.loads(fwd_body.decode("utf-8"))
        except Exception:
            return fwd_body
        msgs = list(p.get("messages") or [])
        msgs.append({"role": "user",
                     "content": "上一轮返回为空。请立刻以 function call 形式调用工具并给出参数，不要用纯文字回复。"})
        p["messages"] = msgs
        return json.dumps(p, ensure_ascii=False).encode()

    def _fix_nonstream(self, raw, schemas, want_tools=True):
        try:
            d = json.loads(raw)
        except Exception:
            return raw, False
        fixed_any = False
        SALVAGE_HITS[0] = 0
        calls = []
        if DEBUG:
            for _k, _s in schemas.items():
                _pr = (_s or {}).get("properties") or {}
                sys.stderr.write("[dbg] schema=%r fields=%r\n" % (
                    _k, {kk: vv.get("type") for kk, vv in _pr.items()}))
        for ch in (d.get("choices") or []):
            msg = ch.get("message") or {}
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                if DEBUG and isinstance((json.loads(fn.get("arguments") or "{}") or {}), dict):
                    try:
                        _a = json.loads(fn.get("arguments") or "{}")
                        with open("/tmp/fix-proxy-raw.log", "a", encoding="utf-8") as _f:
                            _f.write(json.dumps({"tool": fn.get("name"),
                                                 "flat_fields": {k: v for k, v in _a.items() if isinstance(v, str) and "<" in v}},
                                                ensure_ascii=False)[:4000] + "\n")
                    except Exception:
                        pass
                if DEBUG:
                    _a0 = json.loads(fn.get("arguments") or "{}")
                    sys.stderr.write("[dbg] name=%r args_keys=%r types=%r\n" % (
                        fn.get("name"), list(_a0.keys()) if isinstance(_a0, dict) else type(_a0).__name__,
                        {k: type(v).__name__ for k, v in (_a0.items() if isinstance(_a0, dict) else [])}))
                new_args, fixed = fix_arguments(fn.get("name"), fn.get("arguments") or "", schemas)
                if fixed:
                    fn["arguments"] = new_args
                    fixed_any = True
                    if DEBUG:
                        sys.stderr.write(f"[proxy] 非流式修复 {fn.get('name')}: {fixed}\n")
                calls.append(fn)
        # 响应侧清污：思考/正文/参数里的 EPSE 标记一律摘掉（不回灌上下文）
        n_epse = 0
        for ch in (d.get("choices") or []):
            msg = ch.get("message") or {}
            for fld in ("content", "reasoning_content"):
                v = msg.get(fld)
                if isinstance(v, str) and "|EPSE|" in v:
                    msg[fld] = scrub_epse(v)
                    fixed_any = True
                    n_epse += 1
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                if isinstance(fn.get("arguments"), str) and "|EPSE|" in fn["arguments"]:
                    fn["arguments"] = scrub_epse(fn["arguments"])
                    fixed_any = True
                    n_epse += 1
        ok = _calls_ok(calls, schemas, want_tools)
        if DEBUG:
            sys.stderr.write("[proxy] 统计 修复=%s 截断抢救=%d 清污=%d 可用=%s\n"
                             % (fixed_any, SALVAGE_HITS[0], n_epse, ok))
        return (json.dumps(d, ensure_ascii=False).encode() if fixed_any else raw), ok

    def _render_stream(self, raw, schemas, want_tools=True):
        """缓冲整段 SSE 再下发：还原参数 + 不可用时允许整请求重试（DSH 走流式）"""
        text = raw.decode("utf-8", "ignore")
        SALVAGE_HITS[0] = 0
        pending, contents, reasons, first_role, fin = {}, [], [], None, None
        for line in text.split("\n"):
            s = line.strip()
            if not s.startswith("data:"):
                continue
            data = s[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                j = json.loads(data)
            except Exception:
                continue
            for ch in (j.get("choices") or []):
                dl = ch.get("delta") or {}
                if dl.get("role") and first_role is None:
                    first_role = dl["role"]
                if dl.get("content"):
                    contents.append(dl["content"])
                if dl.get("reasoning_content"):
                    reasons.append(dl["reasoning_content"])
                if ch.get("finish_reason"):
                    fin = ch["finish_reason"]
                for tc in (dl.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = pending.setdefault(idx, {"name": "", "arguments": "", "template": tc})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        def frame(delta, finish=None):
            return ("data: " + json.dumps(
                {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                ensure_ascii=False) + "\n\n").encode()

        out, calls = [], []
        raw_text = "".join(contents)
        raw_think = "".join(reasons)
        text = scrub_epse(raw_text)
        think = scrub_epse(raw_think)
        n_epse = (1 if text != raw_text and raw_text else 0) + (1 if think != raw_think and raw_think else 0)
        if first_role or text or think:
            out.append(frame({"role": first_role or "assistant"}))
            if think:
                out.append(frame({"reasoning_content": think}))
            if text:
                out.append(frame({"content": text}))
        for idx in sorted(pending):
            slot = pending[idx]
            args, fixed = fix_arguments(slot["name"], slot["arguments"], schemas)
            if DEBUG and fixed:
                sys.stderr.write(f"[proxy] 流式修复 {slot['name']}: {fixed}\n")
            fn = {"name": slot["name"], "arguments": args}
            calls.append(fn)
            out.append(frame({"tool_calls": [{
                "index": idx,
                "id": slot["template"].get("id") or f"call_{idx}",
                "type": "function",
                "function": fn}]}))
        ok = _calls_ok(calls, schemas, want_tools)
        out.append(frame({}, fin or ("tool_calls" if calls else "stop")))
        out.append(b"data: [DONE]\n\n")
        if DEBUG:
            sys.stderr.write("[proxy] 流式统计 调用=%d 截断抢救=%d 清污=%d 可用=%s\n"
                             % (len(calls), SALVAGE_HITS[0], n_epse, ok))
        return b"".join(out), ok

class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    srv = ThreadingServer(("127.0.0.1", PORT), Proxy)
    print(f"[fix-proxy] 监听 http://127.0.0.1:{PORT} → 上游 {UPSTREAM}", flush=True)
    srv.serve_forever()
