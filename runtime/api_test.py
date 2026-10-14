# -*- coding: utf-8 -*-
"""端到端接口测试：导入图片 → 建节点 → 连线 → 执行 → 校验产出。

用法：
  python runtime/api_test.py [--prompt "..."] [--no-transparent]
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
DEMO = ROOT / "demo"


def call(method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def upload(path: Path, resource_id: str = "", x: float = 100, y: float = 100):
    boundary = "----test" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []

    def field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    field("resource_id", resource_id)
    field("x", str(x))
    field("y", str(y))
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + "/api/resources/import",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def step(msg):
    print(f"\n{'=' * 62}\n>> {msg}\n{'=' * 62}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="抠图编辑：只保留橙色的圆形，其余全部删除")
    ap.add_argument("--transparent", action="store_true", default=True)
    ap.add_argument("--no-transparent", dest="transparent", action="store_false")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--seed", default=str(DEMO / "_seed.png"))
    ap.add_argument("--reuse-resource", default="", help="复用已有数据节点 id（跳过上传）")
    args = ap.parse_args()

    step("1. 导入图片")
    if args.reuse_resource:
        st = call("GET", "/api/state")
        rid = args.reuse_resource
        print(f"   复用数据节点 {rid}")
    else:
        st = upload(Path(args.seed), x=80, y=160)
        rid = st["graph"]["resources"][-1]["id"]
    names = [(r["id"], r["name"], r.get("hasImage")) for r in st["graph"]["resources"]]
    print(f"   数据节点: {names}")

    step("2. 创建处理节点")
    st = call("POST", "/api/processes", {"defId": "ai_image", "x": 420, "y": 150})
    pid = st["graph"]["processes"][-1]["id"]
    print(f"   处理节点: {pid}")

    step("3. 连线 image1")
    st = call("POST", "/api/edges", {"from": rid, "to": pid, "port": "image1"})
    print(f"   连线: {[(e['from'], e['to'], e['port']) for e in st['graph']['edges']]}")

    step("4. 填参数")
    st = call(
        "PATCH",
        f"/api/processes/{pid}",
        {"params": {"prompt": args.prompt, "transparent": args.transparent, "n": args.n}},
    )
    print(f"   参数: {json.dumps(st['graph']['processes'][-1]['params'], ensure_ascii=False)}")

    step("5. 执行")
    t0 = time.time()
    run = call("POST", f"/api/processes/{pid}/execute")
    rid_run = run["runId"]
    print(f"   runId = {rid_run}")

    # 消费 SSE
    final_state = None
    with urllib.request.urlopen(f"{BASE}/api/runs/{rid_run}/stream", timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[6:])
            if msg["type"] == "log":
                print("   |", msg["line"])
            elif msg["type"] == "end":
                final_state = msg.get("state")
                print(f"\n   >>> 结果: {msg['status']}")
                if msg.get("error"):
                    print(f"   >>> 错误: {msg['error']}")
                break
    print(f"   耗时 {time.time() - t0:.1f}s")

    step("6. 最终画布状态")
    if final_state:
        g = final_state["graph"]
        print("   数据节点:")
        for r in g["resources"]:
            extra = f" 来自 {r.get('producer')} #{r.get('index')}" if r.get("producer") else ""
            print(f"     {r['name']:>3}  kind={r['kind']:<6} hasImage={r.get('hasImage')} "
                  f"mtime={r.get('mtime')}{extra}")
        print("   处理节点:")
        for p in g["processes"]:
            print(f"     {p['id']}  status={p['status']} dirty={p.get('dirty')} lastRun={p.get('lastRun')}")
        print("   连线:")
        for e in g["edges"]:
            print(f"     {e['from']} -> {e['to']}:{e['port']}")

    print(f"\n   工作目录 {DEMO}:")
    for p in sorted((DEMO / "images").glob("*")):
        print(f"     images/{p.name}  ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
