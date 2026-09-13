# -*- coding: utf-8 -*-
"""从盘档加载修复代理模块（路径可用 SUPERAPI_PROXY 覆盖）。
默认顺序: 仓库 tools/ → ~/.dsh/"""
import importlib.util, os, sys

def proxy_path():
    p = os.environ.get("SUPERAPI_PROXY")
    if p and os.path.isfile(p):
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, "..", "tools", "superapi-fix-proxy.py"),
              os.path.join(os.path.expanduser("~"), ".dsh", "superapi-fix-proxy.py")):
        if os.path.isfile(c):
            return os.path.abspath(c)
    raise SystemExit("找不到 superapi-fix-proxy.py（可用 SUPERAPI_PROXY 指定）")

def load():
    path = proxy_path()
    saved = sys.argv[:]
    sys.argv = ["px"]                      # 别让 --debug/--port 解析干扰
    try:
        spec = importlib.util.spec_from_file_location("px_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    return mod

def handler(mod):
    """_fix_nonstream / _render_stream 是 Proxy 方法：绕开 __init__（它要 socket）"""
    return object.__new__(mod.Proxy)
