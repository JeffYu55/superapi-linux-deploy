import os
#!/usr/bin/env python3
"""d3_dist_check.py — D3 判据（行为型）：分发归档

依赖链：packaging/build_server_pack.sh（先隔离重构 → 从重构产物打包，脚本被变异必红）
行为验证：
 1. 调 build_server_pack.sh 隔离重构（对脚本变异敏感）
 2. 从重构产物打两份 tar.gz（amd64/arm64）
 3. **解压验证**：解压到临时目录 → 结构/件数/ELF 架构/配置解析 全查
 4. 归档清单核对（每份 7 件）
绿=exit 0；红=exit 1
"""
import json, os, shutil, struct, subprocess, sys, tarfile, tempfile

BUILD = os.environ.get("SUPERAPI_BUILD", os.path.join(os.path.expanduser("~"), "Downloads/v0.2", "packaging/build_server_pack.sh"))
OUTDIR = os.environ.get("SUPERAPI_ROOT", os.path.join(os.path.expanduser("~"), "Downloads/v0.2", "dist-server"))
ARCHS = [("amd64", 0x3E), ("arm64", 0xB7)]
REQUIRED = ["superapi", "config.json", "accounts.json", "run.sh",
            "superapi.service", "superapi.env", "README.md"]

def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)

def verify_tree(root):
    """校验解压后的包树；返回 (ok, msg, files)。忽略 macOS AppleDouble 残留（._*）。"""
    total = 0
    for arch, want in ARCHS:
        d = os.path.join(root, f"superapi-linux-{arch}")
        if not os.path.isdir(d):
            return False, f"缺 {arch} 目录", total
        files = sorted(f for f in os.listdir(d) if not f.startswith("._"))
        if len(files) != 7:
            return False, f"{arch} 件数 {len(files)}", total
        for r in REQUIRED:
            if r not in files:
                return False, f"{arch} 缺 {r}", total
        total += len(files)
        with open(os.path.join(d, "superapi"), "rb") as f:
            head = f.read(64)
        if head[:4] != b"\x7fELF" or struct.unpack_from("<H", head, 18)[0] != want:
            return False, f"{arch} ELF 架构不符", total
        with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
            if json.load(f).get("activated") is not True:
                return False, f"{arch} activated 非真", total
    return True, "结构/架构/配置 全过", total

def main():
    tmp = tempfile.mkdtemp(prefix="d3chk_")
    try:
        # 1) 隔离重构（脚本变异 → 失败）
        iso = os.path.join(tmp, "iso")
        os.makedirs(iso, exist_ok=True)
        b = sh(["bash", BUILD, iso], timeout=300)
        if b.returncode != 0 or not os.path.isdir(os.path.join(iso, "superapi-linux-amd64")):
            print("FAIL: 隔离重构失败\n" + (b.stdout + b.stderr)[-400:]); return 1

        os.makedirs(OUTDIR, exist_ok=True)
        tarballs = []
        archive_env = dict(os.environ, COPYFILE_DISABLE="1")  # 禁 macOS AppleDouble(._*) 进档
        for arch, _ in ARCHS:
            name = f"superapi-linux-{arch}.tar.gz"
            dest = os.path.join(OUTDIR, name)
            if os.path.isfile(dest):
                os.remove(dest)
            # 目录内打包（解压即得 superapi-linux-<arch>/）；--no-mac-metadata 双保险
            r = sh(["tar", "--no-mac-metadata", "-czf", dest, "-C", iso, f"superapi-linux-{arch}"],
                   timeout=300, env=archive_env)
            if r.returncode != 0:
                print(f"FAIL: {arch} 打包失败\n" + (r.stderr or r.stdout)[-300:]); return 1
            if not os.path.isfile(dest) or os.path.getsize(dest) < 1_000_000:
                print(f"FAIL: {arch} 归档异常 size={os.path.getsize(dest) if os.path.isfile(dest) else 0}"); return 1
            tarballs.append(dest)

        # 3) 解压验证（真解压，非清单臆测）
        xdir = os.path.join(tmp, "x")
        os.makedirs(xdir, exist_ok=True)
        for tb in tarballs:
            with tarfile.open(tb, "r:gz") as tf:
                tf.extractall(xdir)
        ok, msg, files = verify_tree(xdir)
        if not ok:
            print(f"FAIL: 解压校验 {msg}"); return 1

        # 4) 归档清单核对（只算普通文件；忽略目录条目与 AppleDouble）
        for tb in tarballs:
            with tarfile.open(tb, "r:gz") as tf:
                names = [m.name for m in tf.getmembers() if m.isfile()]
            real = [n for n in names if not os.path.basename(n).startswith("._")]
            if len(real) != 7:
                print(f"FAIL: {os.path.basename(tb)} 归档件数 {len(real)}（应 7）: {real}"); return 1
            if len(real) != len(names):
                print(f"FAIL: {os.path.basename(tb)} 含 AppleDouble 残留 {len(names) - len(real)} 项"); return 1

        sizes = ", ".join(f"{os.path.basename(t)}={os.path.getsize(t)//1_048_576}MB" for t in tarballs)
        print(f"D3_OK 分发归档绿：{len(tarballs)} 份 tar.gz（{sizes}），解压校验 {files} 件 / 件数与架构全对")
        return 0
    except Exception as e:
        print("FAIL: " + repr(e)); return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
