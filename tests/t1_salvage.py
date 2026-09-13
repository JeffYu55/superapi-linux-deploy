#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嵌套参数还原 + 截断抢救 + 幂等：JSON/对象流/XML/EPSE/截断 五类形态 + 两次幂等。"""
import json, sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _load_proxy import load, handler

px = load()
SCHEMA = {"type": "object", "properties": {
    "purpose": {"type": "string"},
    "assertions": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "severity": {"type": "string"}, "source": {"type": "string"}}}},
    "opts": {"type": "object", "properties": {"n": {"type": "integer"}, "on": {"type": "boolean"}}}}}
A1 = '{"text":"A","severity":"major","source":"u"}'
A2 = '{"text":"B","severity":"minor","source":"u"}'
fails = 0
def chk(n, c, d=""):
    global fails
    print("%-30s %s %s" % (n, "OK" if c else "FAIL", d))
    if not c: fails += 1

cases = [("valid_array", "[" + A1 + "," + A2 + "]", 2),
         ("trunc_missing_bracket", "[" + A1 + "," + A2, 2),
         ("trunc_half_object", "[" + A1 + ',{"text":"B","sever', 1),
         ("object_stream", A1 + "," + A2, 2),
         ("xml_item", "<item><text>A</text><severity>major</severity><source>u</source></item>", 1),
         ("epse_param", '<|EPSE|parameter name="assertions">' + "[" + A1 + "]", 1)]
for name, raw, want in cases:
    got = px.restore(raw, SCHEMA["properties"]["assertions"])
    ok = isinstance(got, list) and len(got) >= want and all(isinstance(x, dict) and "text" in x for x in got)
    chk(name, ok, "items=%s" % (len(got) if isinstance(got, list) else "-"))

# 外层 arguments 被截断
outer = '{"purpose":"P","assertions":"' + A1.replace('"', '\\"') + '"'
new, fixed = px.fix_arguments("t", outer, {"t": SCHEMA})
a = json.loads(new).get("assertions")
chk("outer_truncated", bool(fixed) and isinstance(a, list) and a, "fixed=%s" % fixed)

# 幂等：合法输入零改动；修复后再修必须 no-op
good = json.dumps({"purpose": "P", "assertions": [{"text": "A", "severity": "major", "source": "u"}],
                   "opts": {"n": 3, "on": True}}, ensure_ascii=False)
g2, f2 = px.fix_arguments_by(SCHEMA, good)
chk("valid_untouched", g2 == good and f2 == [])
s1, _ = px.fix_arguments_by(SCHEMA, json.dumps({"purpose": "P", "assertions": "[" + A1 + "]"}, ensure_ascii=False))
s2, fs2 = px.fix_arguments_by(SCHEMA, s1)
chk("fixed_point", s2 == s1 and fs2 == [])
o1, _ = px.fix_arguments_by(SCHEMA, json.dumps({"opts": '{"n":7,"on":true}'}, ensure_ascii=False))
chk("object_repaired", isinstance(json.loads(o1).get("opts"), dict))

print("RESULT fails=%d" % fails)
sys.exit(0 if fails == 0 else 1)
