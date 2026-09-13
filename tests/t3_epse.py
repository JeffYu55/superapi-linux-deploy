#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EPSE 双向清污：响应侧三字段 + 请求侧历史 + 流式跨分片。"""
import json, sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _load_proxy import load, handler

def sse_args(text):
    """把 SSE 里的 tool_calls 参数拼起来（自带解析，不依赖外部测试工具）"""
    acc = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            j = json.loads(body)
        except Exception:
            continue
        for ch in (j.get("choices") or []):
            for tc in ((ch.get("delta") or {}).get("tool_calls") or []):
                fn = tc.get("function") or {}
                if fn.get("arguments"):
                    acc[tc.get("index", 0)] = acc.get(tc.get("index", 0), "") + fn["arguments"]
    return acc

px = load(); M = handler(px)
SCHEMA = {"type": "object", "properties": {"purpose": {"type": "string"},
          "assertions": {"type": "array", "items": {"type": "object",
          "properties": {"text": {"type": "string"}}}}}}
fails = 0
def chk(n, c, d=""):
    global fails
    print("%-30s %s %s" % (n, "OK" if c else "FAIL", d))
    if not c: fails += 1

chk("scrub_open", px.scrub_epse("<|EPSE|tool_calls>x") == "x")
chk("scrub_close", px.scrub_epse("</|EPSE|tool_calls>") == "")
chk("scrub_keeps_text", px.scrub_epse("先 <|EPSE|invoke> 后") == "先  后")
chk("scrub_noop", px.scrub_epse("干净正文") == "干净正文")

pay = {"messages": [{"role": "assistant", "content": "输出 <|EPSE|tool_calls> 块",
                     "reasoning_content": "用 <|EPSE|invoke name=\"x\"> 干",
                     "tool_calls": [{"function": {"name": "t", "arguments": '{"a":"<|EPSE|p>"}'}}]},
                    {"role": "user", "content": [{"type": "text", "text": "历史 <|EPSE|tool_calls>"}]}]}
n = px.scrub_messages(pay)
chk("req_scrubbed", "|EPSE|" not in json.dumps(pay, ensure_ascii=False), "cleaned=%d" % n)

resp = {"choices": [{"message": {"content": "调 <|EPSE|tool_calls>",
                                 "reasoning_content": "思考 <|EPSE|invoke name=\"x\">",
                                 "tool_calls": [{"function": {"name": "t", "arguments":
                                     json.dumps({"purpose": "P", "assertions": '[{"text":"A"}'}, ensure_ascii=False)}}]}}]}
out, ok = px.Proxy._fix_nonstream(M, json.dumps(resp, ensure_ascii=False).encode(), {"t": SCHEMA}, True)
d = json.loads(out); msg = d["choices"][0]["message"]
chk("resp_no_epse", "|EPSE|" not in json.dumps(d, ensure_ascii=False))
chk("resp_keeps_prose", "调" in (msg.get("content") or "") and "思考" in (msg.get("reasoning_content") or ""))
chk("resp_args_fixed", isinstance(json.loads(msg["tool_calls"][0]["function"]["arguments"]).get("assertions"), list))

def sse(delta, fin=None):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}, ensure_ascii=False) + "\n\n"
frames = (sse({"role": "assistant"}) + sse({"reasoning_content": "以 <|EPSE"}) + sse({"reasoning_content": "|tool_calls> 开头"})
          + sse({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "t",
                  "arguments": '{"purpose":"P","assertions":"[{\\"text\\":\\"A\\"}"}'}}]}) + sse({}, "tool_calls") + "data: [DONE]\n\n")
sout, sok = px.Proxy._render_stream(M, frames.encode(), {"t": SCHEMA}, True)
stxt = sout.decode()
chk("stream_no_epse", "|EPSE|" not in stxt)
chk("stream_keeps_reasoning", "开头" in stxt)
acc = sse_args(stxt)
chk("stream_args_fixed", bool(acc) and isinstance(json.loads(acc[0]).get("assertions"), list))
print("RESULT fails=%d" % fails)
sys.exit(0 if fails == 0 else 1)
