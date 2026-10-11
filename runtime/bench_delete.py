# -*- coding: utf-8 -*-
"""定位删除数据节点的延迟来源：分别量 GET /api/state 与 DELETE /api/nodes 的耗时。"""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
TMPWS = str(ROOT / "runtime" / "benchws")
BIG = str(ROOT / "workspace" / "images" / "E.png")   # 1536x2720, ~3MB


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return time.perf_counter() - t0, len(raw), json.loads(raw.decode())


def upload(path: Path, x=0, y=0):
    boundary = "----b" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="resource_id"\r\n\r\n\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="x"\r\n\r\n{x}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="y"\r\n\r\n{y}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + "/api/resources/import", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return time.perf_counter() - t0, json.loads(raw.decode())


def main() -> None:
    _, _, st = call("GET", "/api/state")
    original = st["workspace"]
    print(f"原工作目录: {original}")

    import shutil
    shutil.rmtree(TMPWS, ignore_errors=True)
    call("POST", "/api/workspace", {"path": TMPWS})

    try:
        print("\n===== 准备：导入 4 张 3MB 大图 =====")
        ids = []
        for i in range(4):
            dt, st = upload(Path(BIG), x=80 + i * 20, y=80 + i * 20)
            ids.append(st["graph"]["resources"][-1]["id"])
            print(f"  第 {i+1} 张  上传+state 用时 {dt*1000:7.1f} ms")

        print("\n===== 基线：GET /api/state（4 张图，含 Pillow 读元信息）=====")
        for i in range(5):
            dt, n, _ = call("GET", "/api/state")
            print(f"  #{i+1}  {dt*1000:7.1f} ms   body={n} bytes")
        base = dt

        print("\n===== 拆解：单独量 Pillow 读元信息 =====")
        from PIL import Image
        t0 = time.perf_counter()
        for p in sorted(Path(TMPWS, "images").glob("*.png")):
            with Image.open(p) as im:
                _ = (im.size, im.format)
        print(f"  4 张图读文件头: {(time.perf_counter()-t0)*1000:.1f} ms")

        print("\n===== 关键：DELETE /api/nodes/{id} =====")
        for i, rid in enumerate(ids[:3]):
            dt, n, st = call("DELETE", f"/api/nodes/{rid}")
            print(f"  删第 {i+1} 张  {dt*1000:7.1f} ms   body={n} bytes  剩 {len(st['graph']['resources'])} 个节点")

        print("\n===== 对照：只剩 1 张图时的 GET /api/state =====")
        dt, n, _ = call("GET", "/api/state")
        print(f"  {dt*1000:7.1f} ms   body={n} bytes")
    finally:
        call("POST", "/api/workspace", {"path": original})
        shutil.rmtree(TMPWS, ignore_errors=True)
        print(f"\n已恢复工作目录: {original}")


if __name__ == "__main__":
    main()
