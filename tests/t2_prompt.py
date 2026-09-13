#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求侧降维：array/object → string + 紧凑约束 + 深拷贝 originals（响应侧仍拿得到原 schema）。"""
import json, sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _load_proxy import load

px = load()
MARK = ["\u2264400 \u5b57\u7b26", "\u22644 \u6761", "\u226440 \u5b57"]
fails = 0
def chk(n, c, d=""):
    global fails
    print("%-30s %s %s" % (n, "OK" if c else "FAIL", d))
    if not c: fails += 1

payload = {"tools": [{"type": "function", "function": {
    "name": "t", "description": "d",
    "parameters": {"type": "object", "properties": {
        "purpose": {"type": "string"},
        "assertions": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}}},
        "meta": {"type": "object", "properties": {"n": {"type": "integer"}}}}}}}]}
orig, changed = px.flatten_tools(payload)
props = payload["tools"][0]["function"]["parameters"]["properties"]
desc = props["assertions"].get("description") or ""
chk("changed", changed is True)
chk("array_to_string", props["assertions"]["type"] == "string")
chk("object_to_string", props["meta"]["type"] == "string")
chk("plain_untouched", props["purpose"]["type"] == "string")
chk("constraints", all(m in desc for m in MARK), "found=%d" % sum(m in desc for m in MARK))
chk("example_json", "[" in desc and "{" in desc)
chk("originals_deepcopy", orig["t"]["properties"]["assertions"]["type"] == "array"
    and orig["t"]["properties"] is not props)
print("RESULT fails=%d" % fails)
sys.exit(0 if fails == 0 else 1)
