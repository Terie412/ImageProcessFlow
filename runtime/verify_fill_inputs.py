# -*- coding: utf-8 -*-
"""端到端验证 /api/processes/{pid}/fill-inputs（「填充输入」）。

自包含：自己建临时工作区、起服务端、跑断言、收尾杀掉。用项目 venv 跑。
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "runtime" / "tmp" / "ws_fill_test"
PORT = 8773
BASE = f"http://127.0.0.1:{PORT}"
PY = ROOT / "runtime" / "venv" / "Scripts" / "python.exe"

FAILED = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILED.append(label)
    print(f"  {'OK ' if ok else 'XX '} {label}: {got!r}" + ("" if ok else f"   期望 {want!r}"))


def build_ws():
    cid = "c_fill"
    imgdir = WS / "canvases" / cid / "images"
    imgdir.mkdir(parents=True, exist_ok=True)
    for name in ("A", "B", "C", "D", "E"):
        Image.new("RGB", (120, 90), (180, 120, 90)).save(imgdir / f"{name}.png")
    doc = {
        "version": 2,
        "current": cid,
        "resourceDir": "",
        "canvases": [{
            "id": cid,
            "name": "填充测试",
            "viewport": None,
            "resources": [
                # 到 p1(600,100) 的距离平方：C=6.4k < B=100k < A=650k < E=730k
                # rD 是 p1 自己产出的，必须被排除
                {"id": "rA", "name": "A", "kind": "input", "x": 1000, "y": 800},
                {"id": "rB", "name": "B", "kind": "input", "x": 700, "y": 400},
                {"id": "rC", "name": "C", "kind": "input", "x": 520, "y": 100},
                {"id": "rD", "name": "D", "kind": "output", "producer": "p1", "index": 0,
                 "x": 900, "y": 100},
                {"id": "rE", "name": "E", "kind": "input", "x": 300, "y": 900},
            ],
            "processes": [
                {"id": "p1", "defId": "ai_image", "x": 600, "y": 100, "ports": {"image": 2},
                 "params": {"model": "qwen-image-2.1-turbo"}},
                {"id": "p2", "defId": "crop", "x": 600, "y": 900, "params": {}},
            ],
            "edges": [
                {"id": "e_old1", "from": "rA", "to": "p1", "port": "image1"},
                {"id": "e_old2", "from": "rE", "to": "p1", "port": "image2"},
            ],
        }],
    }
    WS.mkdir(parents=True, exist_ok=True)
    (WS / "graph.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace"))


def edges_to(st, pid):
    return sorted(((e["from"], e["port"]) for e in st["graph"]["edges"] if e["to"] == pid),
                  key=lambda x: x[1])


def ports_of(st, pid):
    for p in st["graph"]["processes"]:
        if p["id"] == pid:
            return p.get("ports") or {}
    return {}


def main():
    build_ws()
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONSTARTUP"] = ""
    env["TEMP"] = str(ROOT / "runtime" / "tmp")
    env["TMP"] = str(ROOT / "runtime" / "tmp")
    proc = subprocess.Popen(
        [str(PY), str(ROOT / "server" / "main.py"), "-w", str(WS), "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                code, _ = call("GET", "/api/state")
                if code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ready:
            print("FAIL: 服务端起不来")
            return 1

        print("=" * 70)
        print("1. 填充：距离排序 + 多删少补（补）+ 覆盖 + 排除自身产出")
        print("=" * 70)
        code, st = call("POST", "/api/processes/p1/fill-inputs",
                        {"resources": ["rA", "rB", "rC", "rD", "rE"]})
        check("HTTP", code, 200)
        check("端口数 2 → 4（选中 5 个，rD 被排除）",
              ports_of(st, "p1").get("image"), 4)
        got = edges_to(st, "p1")
        print("     实际连线:", got)
        check("按距离自上而下接入", got,
              [("rC", "image1"), ("rB", "image2"), ("rA", "image3"), ("rE", "image4")])
        check("rD（本节点产出）没有接入",
              any(f == "rD" for f, _ in got), False)
        check("旧连线已被覆盖（旧的是 rA→image1 / rE→image2）",
              [f for f, p in got if p in ("image1", "image2")], ["rC", "rB"])

        print()
        print("=" * 70)
        print("2. 多删少补（删）：先把口扩到 8，再填充 2 个")
        print("=" * 70)
        code, st = call("PATCH", "/api/processes/p1", {"ports": {"image": 8}})
        check("端口先扩到 8", ports_of(st, "p1").get("image"), 8)
        code, st = call("POST", "/api/processes/p1/fill-inputs", {"resources": ["rB", "rA"]})
        check("HTTP", code, 200)
        check("端口 8 → 2", ports_of(st, "p1").get("image"), 2)
        check("只剩两条边（rB 更近 → image1）", edges_to(st, "p1"),
              [("rB", "image1"), ("rA", "image2")])

        print()
        print("=" * 70)
        print("3. 固定单口节点（裁剪）：只填最近的一个")
        print("=" * 70)
        code, st = call("POST", "/api/processes/p2/fill-inputs",
                        {"resources": ["rB", "rA", "rC"]})
        check("HTTP", code, 200)
        # 到 p2(600,900)：A=170k < B=260k < C=646k → 最近的是 A
        check("只连一条，且是最近的 rA", edges_to(st, "p2"), [("rA", "image")])
        check("固定口不产生 ports 字段", "image" in ports_of(st, "p2"), False)

        print()
        print("=" * 70)
        print("3b. 只动本节点：填充 p1 不该碰 p2 的连线")
        print("=" * 70)
        code, st = call("POST", "/api/processes/p1/fill-inputs", {"resources": ["rC", "rE"]})
        check("HTTP", code, 200)
        check("p1 已重连", edges_to(st, "p1"), [("rC", "image1"), ("rE", "image2")])
        check("p2 的边不受影响", edges_to(st, "p2"), [("rA", "image")])

        print()
        print("=" * 70)
        print("4. 边界：没选中 / 只选中自身产出")
        print("=" * 70)
        code, body = call("POST", "/api/processes/p1/fill-inputs", {"resources": []})
        check("空选中 → 400", code, 400)
        print("     提示:", body.get("detail"))
        code, body = call("POST", "/api/processes/p1/fill-inputs", {"resources": ["rD"]})
        check("只有自身产出 → 400", code, 400)
        print("     提示:", body.get("detail"))
        code, body = call("POST", "/api/processes/p1/fill-inputs", {"resources": ["nope"]})
        check("不存在的 id → 400", code, 400)
        code, body = call("POST", "/api/processes/nope/fill-inputs", {"resources": ["rA"]})
        check("不存在的节点 → 404", code, 404)

        print()
        print("=" * 70)
        if FAILED:
            print(f"失败 {len(FAILED)} 项：")
            for f in FAILED:
                print("  -", f)
            return 1
        print("全部通过")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
