import os
#!/usr/bin/env python3
"""d1_pack_check.py — D1 判据（全链路行为型，对写入面每条依赖链敏感）

设计依据（闸的证伪门机制）：产物集 = 判据可达文件集（源码里带 "/" 与扩展名的路径字面量）∪ 写入面；
闸会把每个产物替换成 掏空/字面壳/空转 三态假壳再跑本判据，任何一条仍绿即判空转。
故本判据不比对字符串，只做行为验证，且对每条依赖链敏感：

 1. dist-server 双架构包：件数与必需件（结构）
 2. superapi：ELF 架构（amd64=0x3E / arm64=0xB7）+ 体积下限
 3. config.json / accounts.json：JSON 解析（被变异 → 解析失败 → 红）
 4. run.sh：无 token 必拒且提示凭据（被变异 → 守卫失效 → 红）
 5. **隔离重构**：调 build_server_pack.sh 到临时目录重建两份包（脚本被变异 → 重建失败/产物缺 → 红）
绿=exit 0；红=exit 1
"""
import json, os, shutil, struct, subprocess, sys, tempfile

BUILD = os.environ.get("SUPERAPI_BUILD", os.path.join(os.path.expanduser("~"), "Downloads/v0.2", "packaging/build_server_pack.sh"))
ROOT = os.environ.get("SUPERAPI_ROOT", os.path.join(os.path.expanduser("~"), "Downloads/v0.2", "dist-server"))
CFG_A = os.environ.get("SUPERAPI_ROOT", os.path.join(os.path.expanduser("~"), "Downloads/v0.2", "dist-server/superapi-linux-amd64/config.json"))
RUN_A = os.environ.get("SUPERAPI_ROOT", os.path.join(os.path.expanduser("~"), "Downloads/v0.2", "dist-server/superapi-linux-amd64/run.sh"))
ARCHS = [("amd64", 0x3E), ("arm64", 0xB7)]
REQUIRED = ["superapi", "config.json", "accounts.json", "run.sh",
            "superapi.service", "superapi.env", "README.md"]

def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)

def check_pack(root, expect_arch=ARCHS):
    """校验一份包根目录；返回 (ok, msg, files_total)"""
    total = 0
    for arch, want_machine in expect_arch:
        d = os.path.join(root, f"superapi-linux-{arch}")
        if not os.path.isdir(d):
            return False, f"缺 {arch} 包目录", total
        files = sorted(os.listdir(d))
        if len(files) != 7:
            return False, f"{arch} 件数 {len(files)}（应 7）", total
        for r in REQUIRED:
            if r not in files:
                return False, f"{arch} 缺 {r}", total
        total += len(files)
        binp = os.path.join(d, "superapi")
        if not os.access(binp, os.X_OK):
            return False, f"{arch} superapi 不可执行", total
        with open(binp, "rb") as f:
            head = f.read(64)
        if head[:4] != b"\x7fELF":
            return False, f"{arch} 非 ELF", total
        if struct.unpack_from("<H", head, 18)[0] != want_machine:
            return False, f"{arch} 架构不符", total
        if os.path.getsize(binp) < 5_000_000:
            return False, f"{arch} 体积异常", total
        with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
            if json.load(f).get("activated") is not True:
                return False, f"{arch} config.activated 非真", total
        with open(os.path.join(d, "accounts.json"), encoding="utf-8") as f:
            json.load(f)
        # run.sh 守卫（无 token 必拒）
        runp = os.path.join(d, "run.sh")
        if not os.access(runp, os.X_OK):
            return False, f"{arch} run.sh 不可执行", total
        env = dict(os.environ, SUPERAPI_TOKEN="")
        r = sh(["sh", runp], timeout=30, env=env, cwd=d)
        if r.returncode == 0:
            return False, f"{arch} run.sh 无 token 仍成功", total
        if "SUPERAPI_TOKEN" not in (r.stdout + r.stderr):
            return False, f"{arch} run.sh 未提示缺凭据", total
    return True, "结构/架构/配置/守卫 全过", total

def main():
    tmp = tempfile.mkdtemp(prefix="d1chk_")
    try:
        # 1-4) 原始包校验（对 config.json / run.sh / 二进制头部变异敏感）
        ok, msg, total = check_pack(ROOT)
        if not ok:
            print(f"FAIL: 原始包 {msg}"); return 1

        # 5) 隔离重构（对 build_server_pack.sh 变异敏感：脚本被换壳 → 重建不出产物）
        if not os.path.isfile(BUILD):
            print(f"FAIL: 缺打包脚本 {BUILD}"); return 1
        iso = os.path.join(tmp, "iso")
        os.makedirs(iso, exist_ok=True)
        b = sh(["bash", BUILD, iso], timeout=300)
        if b.returncode != 0:
            print("FAIL: 打包脚本隔离重构失败\n" + (b.stdout + b.stderr)[-400:]); return 1
        ok2, msg2, total2 = check_pack(iso)
        if not ok2:
            print(f"FAIL: 隔离重构产物 {msg2}"); return 1

        print(f"D1_OK 全链路绿：原始包 {total} 件 + 隔离重构 {total2} 件（结构/ELF架构/配置解析/run.sh 守卫/脚本可重建）")
        return 0
    except Exception as e:
        print("FAIL: " + repr(e)); return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
