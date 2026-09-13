#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全离线端到端：起一个桩上游 + 代理实例，验证
① 嵌套参数经代理后还原成 list；② 桩上游挂掉 → 502 人话 + Retry-After；
③ 桩上游返空补全 → 代理按 RETRY_LIMIT 重试（桩端计数 1+RETRY_LIMIT）；④ 桩就绪即 200。
不依赖真实反代/网络。"""
import json, os, socket, subprocess, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _load_proxy import load, proxy_path

CALLS = []
MODE = {"v": "ok"}
SCHEMA = {"type": "object", "properties": {"purpose": {"type": "string"},
          "assertions": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}}}}}

class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        body = b'{"object":"list","data":[{"id":"stub"}]}'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        CALLS.append(time.time())
        if MODE["v"] == "empty":
            d = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": " "}}],
                 "usage": {"completion_tokens": 0}}
        else:
            d = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "",
                 "reasoning_content": "想 <|EPSE|invoke name=\"x\"> 一下",
                 "tool_calls": [{"type": "function", "function": {"name": "t", "arguments":
                     json.dumps({"purpose": "P", "assertions": '[{"text":"A","severity":"major","source":"u"}'})}}]}}]}
        body = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

fails = 0
def chk(n, c, d=""):
    global fails
    print("%-28s %s %s" % (n, "OK" if c else "FAIL", d))
    if not c: fails += 1

up_port, px_port = free_port(), free_port()
srv = ThreadingHTTPServer(("127.0.0.1", up_port), Stub); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

proc = subprocess.Popen(["/usr/bin/python3", proxy_path(), "--port", str(px_port),
                         "--upstream", "http://127.0.0.1:%d" % up_port, "--debug"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def post(port, payload=None, timeout=60):
    p = payload or {"model": "stub", "stream": False,
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function", "function": {"name": "t", "parameters": SCHEMA}}]}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port,
                                 data=json.dumps(p, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer k"}, method="POST")
    r = urllib.request.urlopen(req, timeout=timeout)
    return r.status, dict(r.headers), r.read().decode()

try:
    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", px_port), 0.2).close(); break
        except Exception: time.sleep(0.2)
    code, hdr, body = post(px_port)
    d = json.loads(body); msg = d["choices"][0]["message"]
    a = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    chk("proxy_200", code == 200, "http=%s" % code)
    chk("nested_restored", isinstance(a.get("assertions"), list) and a["assertions"])
    chk("resp_epse_clean", "|EPSE|" not in body)

    MODE["v"] = "empty"; CALLS.clear()
    code, hdr, body = post(px_port)
    chk("empty_retried", len(CALLS) == 1 + load().RETRY_LIMIT, "stub_calls=%d" % len(CALLS))
finally:
    proc.terminate(); srv.shutdown(); srv.server_close()   # shutdown 只停循环，socket 得单独关，否则连接被挂住

# 上游挂掉 → 502 人话 + Retry-After（端口已随 srv.shutdown 关闭）
p_fixed = free_port()
proc2 = subprocess.Popen(["/usr/bin/python3", proxy_path(), "--port", str(p_fixed),
                          "--upstream", "http://127.0.0.1:%d" % up_port, "--debug"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(30):
        try:
            socket.create_connection(("127.0.0.1", p_fixed), 0.2).close()
            break
        except Exception:
            time.sleep(0.2)
    try:
        post(p_fixed, timeout=30)
        chk("dead_upstream_502", False, "上游已关却成功了？")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        chk("dead_upstream_502", e.code == 502, "http=%d" % e.code)
        chk("actionable_msg", ("连不上" in body) and ("urllib" not in body), body[:60])
        chk("retry_after", bool(e.headers.get("Retry-After")), "Retry-After=%s" % e.headers.get("Retry-After"))
finally:
    proc2.terminate()

print("RESULT fails=%d" % fails)
sys.exit(0 if fails == 0 else 1)
