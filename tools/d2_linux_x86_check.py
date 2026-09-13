#!/usr/bin/env python3
"""d2_linux_x86_check.py — D2 判据（真 x86_64 Linux 端到端）

依赖链（判据可达产物≈真依赖产物）：
 - packaging/build_server_pack.sh（隔离重构部署包 → 被变异必红）
行为验证（真机真链路，非字面比对）：
 1. 调 build_server_pack.sh 到临时目录（取 amd64 包）
 2. 拷入 linuxx86 VM → 写 env（含 token）→ run.sh 起服务（真进程）
 3. 宿主经 lima 转发读 /healthz（真 HTTP 读数）+ 账号可用数断言
 4. **真反代对话**：经反代打真 DeepSeek → 回复含目标串
 5. 清理（停进程 / 删测试目录）
绿=exit 0；红=exit 1
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time
import urllib.request, urllib.error

BUILD = os.environ.get("SUPERAPI_BUILD", "/Users/jeff/Downloads/v0.2/packaging/build_server_pack.sh")
TOKENFILE = os.environ.get("SUPERAPI_TOKEN_FILE", "/Users/jeff/Downloads/v0.2/.ds_user_token")
VM = "linuxx86"
PORT = 18101

def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)

def wait_port(port, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.3)
    return False

def req(path, payload=None, timeout=120):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json",
                                        "Authorization": "Bearer localtest"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "ignore")

def main():
    tmp = tempfile.mkdtemp(prefix="d2chk_")
    try:
        if not os.path.isfile(TOKENFILE):
            print(f"FAIL: 缺 token 文件 {TOKENFILE}"); return 1
        token = open(TOKENFILE, encoding="utf-8").read().strip()
        if len(token) < 20:
            print("FAIL: token 文件内容异常"); return 1

        # 1) 隔离重构部署包（对 build_server_pack.sh 变异敏感）
        iso = os.path.join(tmp, "iso")
        os.makedirs(iso, exist_ok=True)
        b = sh(["bash", BUILD, iso], timeout=300)
        pkg = os.path.join(iso, "superapi-linux-amd64")
        if b.returncode != 0 or not os.path.isdir(pkg):
            print("FAIL: 打包脚本隔离重构失败\n" + (b.stdout + b.stderr)[-400:]); return 1

        # 2) 拷入 VM（清场 → 拷贝；用脚本文件传命令——limactl shell 的引号处理会吃 sh -c）
        prep = os.path.join(tmp, "prep.sh")
        with open(prep, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\npkill -f 'deploy/run/superapi' 2>/dev/null\nrm -rf /tmp/deploy\nmkdir -p /tmp/deploy\necho PREP_OK\n")
        if sh(["limactl", "copy", prep, f"{VM}:/tmp/prep.sh"], timeout=180).returncode != 0:
            print("FAIL: 清场脚本拷贝失败"); return 1
        pr = sh(["limactl", "shell", VM, "sh", "/tmp/prep.sh"], timeout=180)
        if "PREP_OK" not in pr.stdout:
            print("FAIL: VM 清场失败\n" + (pr.stdout + pr.stderr)[-300:]); return 1
        cp = sh(["limactl", "copy", pkg, f"{VM}:/tmp/deploy/pkg"], timeout=300)
        if cp.returncode != 0 or "error" in (cp.stderr or "").lower():
            print("FAIL: 拷贝到 VM 失败\n" + (cp.stdout + cp.stderr)[-400:]); return 1

        envfile = os.path.join(tmp, "superapi.env")
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(f"SUPERAPI_TOKEN={token}\nSUPERAPI_PORT={PORT}\nSUPERAPI_APIKEY=localtest\nSUPERAPI_MODEL=deepseek-v4.1-flash\n")
        sh(["limactl", "copy", envfile, f"{VM}:/tmp/deploy/superapi.env"], timeout=120)

        starter = os.path.join(tmp, "start.sh")
        with open(starter, "w", encoding="utf-8") as f:
            f.write("""#!/bin/sh
ARCH_DIR=$(ls -d /tmp/deploy/pkg/superapi-linux-* 2>/dev/null | head -1)
[ -n "$ARCH_DIR" ] || { echo "[FATAL] 包目录缺失"; exit 1; }
rm -rf /tmp/deploy/run; mkdir -p /tmp/deploy/run
cp -r "$ARCH_DIR"/. /tmp/deploy/run/
cp /tmp/deploy/superapi.env /tmp/deploy/run/superapi.env
chmod +x /tmp/deploy/run/superapi /tmp/deploy/run/run.sh
cd /tmp/deploy/run
setsid ./run.sh > /tmp/deploy/srv.log 2>&1 < /dev/null &
sleep 6
echo "--- srv.log ---"; head -16 /tmp/deploy/srv.log
""")
        sh(["limactl", "copy", starter, f"{VM}:/tmp/deploy/start.sh"], timeout=120)
        r = sh(["limactl", "shell", VM, "sh", "/tmp/deploy/start.sh"], timeout=300)
        log = r.stdout + r.stderr

        # 3) 端口就绪 + healthz
        if not wait_port(PORT):
            print("FAIL: 端口未就绪（部署失败）\n" + log[-700:]); return 1
        st, body = req("/healthz", timeout=30)
        if st != 200 or "healthy" not in body:
            print(f"FAIL: /healthz 异常 st={st} {body[:200]}"); return 1
        avail = ((json.loads(body).get("accounts") or {}).get("available"))
        if avail != 1:
            print(f"FAIL: 账号可用数 {avail}（应 1）"); return 1

        # 4) 真反代对话
        st, body = req("/v1/chat/completions", {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "只回四个字：服务器通"}],
            "stream": False})
        if st != 200:
            print(f"FAIL: 对话 st={st} {body[:300]}"); return 1
        txt = (json.loads(body).get("choices") or [{}])[0].get("message", {}).get("content", "")
        if "服务器通" not in txt:
            print(f"FAIL: 回复异常 {txt[:80]!r}"); return 1

        print(f"D2_OK x86_64 真机全绿：隔离重构→VM 起服→/healthz(accounts={avail})→真对话[{txt.strip()[:12]}] 端口 {PORT}")
        return 0
    except urllib.error.HTTPError as e:
        print(f"FAIL: HTTP {e.code} {e.read()[:200]!r}"); return 1
    except Exception as e:
        print("FAIL: " + repr(e)); return 1
    finally:
        try:
            cleanup = os.path.join(tmp, "cleanup.sh")
            with open(cleanup, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\npkill -f 'deploy/run/superapi' 2>/dev/null\nrm -rf /tmp/deploy\necho CLEAN_OK\n")
            if sh(["limactl", "copy", cleanup, f"{VM}:/tmp/cleanup.sh"], timeout=120).returncode == 0:
                sh(["limactl", "shell", VM, "sh", "/tmp/cleanup.sh"], timeout=180)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
