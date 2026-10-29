# 给 IrisIME v2.0.0-alpha.3 的 IrisIME.exe 打补丁
# 根因: ime/client.py 第 92 行使用 EngineService，但模块级只
#       `from ime.tsf_server import start_background`，漏导入 EngineService
#       （EngineService 定义在 ime/tsf_server.py:31）→ NameError
# 修法: 替换 PYZ 中的 ime/__init__ 字节码，把 EngineService 注入 builtins，
#       使 ime.client 的运行时全局查找能命中。无需厂商源码。
import struct, marshal, zlib, os, sys, importlib.util

SRC = r"D:\桌面\IrisIME-Setup-v2.0.0-alpha.3 (1).exe"     # 安装包
APP_EXE = r"C:\Users\nd851\AppData\Local\Temp\{app}\IrisIME.exe"  # 从安装包解出的主程序
OUT = os.path.join(os.path.dirname(SRC), "IrisIME-patched.exe")
TMP = r"C:\Users\nd851\AppData\Local\Temp\pyi-patch"

os.makedirs(TMP, exist_ok=True)
data = open(APP_EXE, "rb").read()
pos = data.rfind(b"MEI\x0c\x0b\n\x0b\x0e")
assert pos != -1, "不是 PyInstaller 单文件 exe"
assert pos + 88 == len(data), "cookie 后还有数据，需要另行处理"
pkgLen, tocOff, tocLen, pyver = struct.unpack_from(">IIII", data, pos + 8)
pylib = data[pos + 24:pos + 88].rstrip(b"\x00")
pkgStart = pos + 88 - pkgLen

# ---------- 1. 解析 CArchive TOC ----------
toc_raw = data[pkgStart + tocOff: pkgStart + tocOff + tocLen]
entries = []
p = 0
while p < len(toc_raw):
    (elen,) = struct.unpack_from(">I", toc_raw, p)
    epos, clen, ulen, flag, typ = struct.unpack_from(">IIIBc", toc_raw, p + 4)
    name = toc_raw[p + 20:p + elen].rstrip(b"\x00").decode()
    entries.append(dict(name=name, epos=epos, clen=clen, ulen=ulen, flag=flag, typ=typ))
    p += elen
assert [e["epos"] for e in entries] == sorted(e["epos"] for e in entries), "TOC 乱序"

# 相邻条目之间的对齐间隙，原样保留
gaps = {}
for i, e in enumerate(entries[:-1]):
    nxt = entries[i + 1]
    gaps[e["name"]] = nxt["epos"] - (e["epos"] + e["clen"])

# ---------- 2. 解析 PYZ 并重建 ----------
pyz_entry = next(e for e in entries if e["name"].endswith(".pyz"))
pyz = data[pkgStart + pyz_entry["epos"]: pkgStart + pyz_entry["epos"] + pyz_entry["clen"]]
assert pyz[:4] == b"PYZ\0"
magic = pyz[4:8]
assert magic == importlib.util.MAGIC_NUMBER, f"字节码魔数不匹配 {magic.hex()} vs 本机 {importlib.util.MAGIC_NUMBER.hex()}"
tocpos = struct.unpack_from(">I", pyz, 8)[0]
pyz_toc = marshal.loads(pyz[tocpos:])          # [(name, (typ, pos, len)), ...]

def make_new_ime():
    src = '''
__doc__ = "鸢尾输入法 (Iris IME): 解码引擎 + 神经重排 + 桌面客户端。"
__version__ = "1.1.2"
__all__ = ["Engine", "Candidate", "DecodeResult", "__version__"]

# === 补丁: ime/client.py 2.0.0-alpha.3 漏导入 EngineService（tsf_server.py:31）===
# 在包初始化时注入 builtins，兜住 client.py 第 92 行的运行时全局查找。
import builtins as _builtins
from ime.tsf_server import EngineService as _EngineService
_builtins.EngineService = _EngineService
'''
    return compile(src, "ime/__init__.py", "exec")

new_code = make_new_ime()
new_blob = zlib.compress(marshal.dumps(new_code), 9)

out = bytearray(pyz[:12])
new_toc = []
for name, (typ, bpos, blen) in pyz_toc:
    blob = new_blob if name == "ime" else pyz[bpos:bpos + blen]
    new_toc.append((name, (typ, len(out), len(blob))))
    out += blob
toc_bytes = marshal.dumps(new_toc)
struct.pack_into(">I", out, 8, len(out))       # tocpos
out += toc_bytes
new_pyz = bytes(out)

# ---------- 3. 重组 CArchive ----------
payload = bytearray()
for e in entries:
    # 保持原顺序；前补原间隙对齐
    payload += b"\x00" * gaps.get(e["name"], 0)
    e["new_epos"] = len(payload)
    if e is pyz_entry:
        payload += new_pyz
        e["new_clen"] = len(new_pyz)
        e["new_ulen"] = len(new_pyz)
    else:
        payload += data[pkgStart + e["epos"]: pkgStart + e["epos"] + e["clen"]]
        e["new_clen"] = e["clen"]
        e["new_ulen"] = e["ulen"]

toc_buf = bytearray()
for e in entries:
    name_b = e["name"].encode() + b"\x00"
    while (20 + len(name_b)) % 4:
        name_b += b"\x00"
    body = struct.pack(">IIIBc", e["new_epos"], e["new_clen"], e["new_ulen"], e["flag"], e["typ"] if isinstance(e["typ"], bytes) else e["typ"].encode()) + name_b
    toc_buf += struct.pack(">I", len(body) + 4) + body

new_pkg = bytes(payload) + bytes(toc_buf)
cookie = struct.pack(">8sIIII64s", b"MEI\x0c\x0b\n\x0b\x0e", len(new_pkg) + 88, len(payload), len(toc_buf), pyver, pylib + b"\x00" * (64 - len(pylib)))
new_pkg += cookie

new_exe = data[:pkgStart] + new_pkg
open(OUT, "wb").write(new_exe)
print(f"补丁完成: {OUT}")
print(f"原 exe {len(data):,} B -> 新 exe {len(new_exe):,} B (PYZ {pyz_entry['clen']:,} -> {len(new_pyz):,})")
