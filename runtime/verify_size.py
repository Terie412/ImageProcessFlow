# -*- coding: utf-8 -*-
"""验证尺寸参数：确认换成官方预设后能真正跑通（会真实调用 1 次生图）。

全程在一次性临时工作目录里，不覆盖用户画布上的产出。
"""
import json
import mimetypes
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
TMPWS = ROOT / "runtime" / "size_ws"
SEED = ROOT / "runtime" / "seed.png"

PROMPT = "把这张图里的三个形状重新排布成一行，保持配色不变。"


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def upload(path, x=0, y=0):
    b = "----b" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"".join(
        [
            f'--{b}\r\nContent-Disposition: form-data; name="resource_id"\r\n\r\n\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="x"\r\n\r\n{x}\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="y"\r\n\r\n{y}\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{b}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(
        BASE + "/api/resources/import",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


orig = call("GET", "/api/state")["workspace"]

print("===== 0. 尺寸选项 =====")
st = call("GET", "/api/state")
size_spec = next(p for p in st["manifests"]["ai_image"]["params"] if p["key"] == "size")
vals = [(o if isinstance(o, str) else o["value"]) for o in size_spec["options"]]
expected = ["", "720x1280", "1024x1024", "1280x720", "1024x1536", "1536x1024", "1536x1536"]
print(f"  当前选项: {vals}")
check("选项是常用尺寸，且都落在 512x512~2048x2048 内", vals == expected, str(vals))
for v in vals[1:]:
    w, h = (int(x) for x in v.split("x"))
    ok = 512 * 512 <= w * h <= 2048 * 2048
    check(f"{v} 面积 {w * h} 合法", ok, "" if ok else "超出 262144~4194304")

print("\n===== 1. 旧画布上的失效参数值会被自动修正 =====")
# 直接写进 graph.json 会造成服务端状态不同步，所以用 API 设一个非法值再 reload
shutil.rmtree(TMPWS, ignore_errors=True)
call("POST", "/api/workspace", {"path": str(TMPWS)})
try:
    st = upload(SEED, 80, 120)
    rid = st["graph"]["resources"][-1]["id"]
    st = call("POST", "/api/processes", {"defId": "ai_image", "x": 520, "y": 120})
    pid = st["graph"]["processes"][-1]["id"]
    call("POST", "/api/edges", {"from": rid, "to": pid, "port": "image1"})

    call("PATCH", f"/api/processes/{pid}", {"params": {"prompt": PROMPT, "size": "640x480"}})
    before = next(p for p in call("GET", "/api/state")["graph"]["processes"] if p["id"] == pid)["params"]["size"]
    print(f"  人为设成不在选项里的值后: size = {before!r}")
    call("POST", "/api/registry/reload")
    after = next(p for p in call("GET", "/api/state")["graph"]["processes"] if p["id"] == pid)["params"]["size"]
    print(f"  reload 之后:              size = {after!r}")
    check("失效的尺寸值被回落到默认（自动）", after == "", repr(after))

    print("\n===== 2. 用 720x1280 真跑一次（消耗 1 次生图调用）=====")
    call("PATCH", f"/api/processes/{pid}", {"params": {"size": "720x1280"}})
    rid_run = call("POST", f"/api/processes/{pid}/execute")["runId"]
    status, err = None, None
    with urllib.request.urlopen(f"{BASE}/api/runs/{rid_run}/stream", timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[6:])
            if msg["type"] == "log" and msg["line"].strip():
                print("  |", msg["line"])
            elif msg["type"] == "end":
                status, err = msg["status"], msg.get("error")
                break
    check("执行成功", status == "done", f"{status} {err or ''}")

    imgs = sorted((TMPWS / "images").glob("*.png"))
    produced = [p for p in imgs if p.name not in ("A.png",)]
    print(f"  产出的图片: {[p.name for p in produced]}")
    if produced:
        with Image.open(produced[-1]) as im:
            print(f"  实际尺寸: {im.size[0]}×{im.size[1]}  模式 {im.mode}")
            check("输出尺寸等于请求的 720×1280", im.size == (720, 1280), str(im.size))
    else:
        check("有产出图片", False, "没有找到产出文件")
finally:
    call("POST", "/api/workspace", {"path": orig})
    shutil.rmtree(TMPWS, ignore_errors=True)
    print(f"\n已恢复工作目录: {orig}")

bad = [r for r in results if not r]
print(f"\n共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
sys.exit(1 if bad else 0)
