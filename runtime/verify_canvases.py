# -*- coding: utf-8 -*-
"""多画布验证。

A 部分：纯函数 / 文件系统（不起服务）—— v1 迁移、每画布图片池、名字只在画布内唯一
B 部分：打真实实例（默认 8770，可用 IPF_BASE 覆盖）——
  两个画布各有一个 A 互不干扰、执行产出落在本画布、跨画布复制粘贴会复制图片文件、
  删画布只删自己那份、最后一个画布不给删
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import engine as engine_mod  # noqa: E402
from workspace import Workspace  # noqa: E402

BASE = os.environ.get("IPF_BASE", "http://127.0.0.1:8770")
TMPWS = ROOT / "runtime" / "canvas_ws"
TMPSCRIPT = ROOT / "scripts" / "_canvas_test"

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def req(method: str, path: str, body=None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def upload(path: Path, x=60.0, y=60.0, resource_id=""):
    boundary = "----t" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = []

    def field(n, v):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())

    field("resource_id", resource_id)
    field("x", str(x))
    field("y", str(y))
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    r = urllib.request.Request(
        BASE + "/api/resources/import",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode())


def img_bytes(path: Path) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.read()


def color_of(raw: bytes):
    with Image.open(io.BytesIO(raw)) as im:
        return im.convert("RGB").getpixel((2, 2))


# ---------------------------------------------------------------- A. 纯函数/文件系统
def part_a() -> None:
    print("=" * 62)
    print("[A] v1 迁移 / 每画布图片池 / 名字作用域")
    print("=" * 62)
    root = ROOT / "runtime" / "_canvas_v1"
    shutil.rmtree(root, ignore_errors=True)
    (root / "images").mkdir(parents=True)
    Image.new("RGB", (8, 8), (200, 10, 10)).save(root / "images" / "A.png")
    (root / "graph.json").write_text(
        json.dumps({"version": 1, "resources": [{"id": "r1", "name": "A", "kind": "input",
                                                 "x": 10.0, "y": 20.0}],
                    "processes": [], "edges": []}),
        encoding="utf-8",
    )
    ws = Workspace(root)
    d = ws.load_doc()
    c1 = d["canvases"][0]
    check("v1 迁移成 v2、得到 1 个画布", d["version"] == 2 and len(d["canvases"]) == 1, str(d["version"]))
    check("老画布叫「画布 1」", c1["name"] == "画布 1", c1["name"])
    check("顶层 images/ 已挪进画布池、老目录消失",
          not (root / "images").exists() and (ws.image_dir(c1["id"]) / "A.png").is_file(), "")
    check("迁移后节点还在", len(c1["resources"]) == 1, str(len(c1["resources"])))
    check("再读一次不重复迁移", len(ws.load_doc()["canvases"]) == 1, "")

    # 新建画布 + 名字作用域
    c2 = ws.create_canvas(d)
    check("新画布自动叫「画布 2」", c2["name"] == "画布 2", c2["name"])
    check("两个画布各有独立的图片目录",
          ws.image_dir(c1["id"]) != ws.image_dir(c2["id"]), "")
    # c2 里没有 A → 可以分到 A；c1 里 A 已占用 → 分到 B
    c2["resources"].append({"id": "r2", "name": ws.alloc_name(c2), "kind": "input"})
    check("别的画布不受 c1 影响，也能拿到 A", c2["resources"][-1]["name"] == "A",
          c2["resources"][-1]["name"])
    check("同一个画布内 A 已被占用 → 下一个拿 B",
          ws.alloc_name({"resources": [{"name": "A"}]}) == "B", "")
    check("v1 迁移出来的位置保留", c1["resources"][0]["x"] == 10.0, str(c1["resources"][0]["x"]))
    # 删画布：把图片池一起删掉，但删不掉最后一个
    check("删除画布成功", ws.delete_canvas(d, c2["id"]) is True, "")
    check("被删画布的目录已消失", not ws.canvas_dir(c2["id"]).exists(), "")
    check("c1 的图片还在", (ws.image_dir(c1["id"]) / "A.png").is_file(), "")
    check("最后一个画布不给删", ws.delete_canvas(d, c1["id"]) is False, "")
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- B. 真实实例
GEN = '''\
# -*- coding: utf-8 -*-
"""测试用：把输入图画成纯色输出，不调用任何模型。"""
import json, os, sys
from PIL import Image

payload = json.loads(sys.stdin.readline())
src = next((p for p in (payload.get("inputs") or {}).values() if p), None)
out = payload["output_dir"]
im = Image.open(src).convert("RGB") if src else Image.new("RGB", (16, 16))
im.resize((24, 24)).save(os.path.join(out, "0.png"))
print("产出 1 张")
'''
MANIFEST = {
    "id": "_canvas_test",
    "name": "画布测试",
    "entry": "gen.py",
    "inputs": [{"key": "image", "type": "image", "label": "", "required": False}],
    "params": [],
    "outputs": [{"key": "image", "type": "image", "multi": True}],
    "size": {"defaultWidth": 400, "defaultHeight": 240},
}


def part_b() -> None:
    print("\n" + "=" * 62)
    print("[B] 真实实例：两个画布各有一个 A，互不干扰")
    print("=" * 62)
    st0 = req("GET", "/api/state")
    orig_ws = st0["workspace"]

    TMPSCRIPT.mkdir(parents=True, exist_ok=True)
    (TMPSCRIPT / "manifest.json").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    (TMPSCRIPT / "gen.py").write_text(GEN, encoding="utf-8")

    seed = ROOT / "runtime" / "_canvas_seed"
    seed.mkdir(parents=True, exist_ok=True)
    red, blue = seed / "red.png", seed / "blue.png"
    Image.new("RGB", (32, 32), (220, 20, 20)).save(red)
    Image.new("RGB", (32, 32), (20, 20, 220)).save(blue)

    try:
        req("POST", "/api/registry/reload")
        shutil.rmtree(TMPWS, ignore_errors=True)
        st = req("POST", "/api/workspace", {"path": str(TMPWS)})
        check("新工作目录自带 1 个画布", len(st["canvases"]) == 1, str(len(st["canvases"])))
        c1 = st["canvasId"]
        check("默认画布叫「画布 1」", st["canvases"][0]["name"] == "画布 1", st["canvases"][0]["name"])

        # ---- 画布 1 导入红图 ----
        st = upload(red)
        r1 = st["graph"]["resources"][-1]
        check("画布 1 的图片名是 A", r1["name"] == "A", r1["name"])
        p1 = Path(TMPWS) / "canvases" / c1 / "images" / "A.png"
        check("文件落在 canvases/<画布1>/images/A.png", p1.is_file(), str(p1))
        check("顶层不再有 images/", not (Path(TMPWS) / "images").exists(), "")

        # ---- 新建画布 2，导入蓝图 ----
        st = req("POST", "/api/canvases", {})
        c2 = st["canvasId"]
        check("切入新画布、名字是「画布 2」", c2 != c1 and len(st["canvases"]) == 2,
              str([c["name"] for c in st["canvases"]]))
        check("新画布是空的", not st["graph"]["resources"] and not st["graph"]["processes"], "")
        st = upload(blue)
        r2 = st["graph"]["resources"][-1]
        check("画布 2 的图片也分到了 A（短名只在本画布内唯一）", r2["name"] == "A", r2["name"])
        p2 = Path(TMPWS) / "canvases" / c2 / "images" / "A.png"
        check("两个画布各有一份 A.png", p1.is_file() and p2.is_file(), "")

        # ---- 取图必须带画布 id ----
        c_red = color_of(img_bytes(f"/api/images/{c1}/A"))
        c_blue = color_of(img_bytes(f"/api/images/{c2}/A"))
        check("带画布 id 取图：画布 1 的 A 是红的", c_red == (220, 20, 20), str(c_red))
        check("带画布 id 取图：画布 2 的 A 是蓝的", c_blue == (20, 20, 220), str(c_blue))
        check("两者是不同的文件（没互相覆盖）", p1.read_bytes() != p2.read_bytes(), "")

        # ---- 执行：产出必须落在本画布的池子里 ----
        st = req("POST", "/api/processes", {"defId": "_canvas_test", "x": 300, "y": 120})
        pid = st["graph"]["processes"][-1]["id"]
        req("POST", "/api/edges", {"from": r2["id"], "to": pid, "port": "image"})
        run = req("POST", f"/api/processes/{pid}/execute", {})
        with urllib.request.urlopen(f"{BASE}/api/runs/{run['runId']}/stream", timeout=120) as rs:
            for raw in rs:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: ") and json.loads(line[6:]).get("type") == "end":
                    print("   >>> 执行", json.loads(line[6:])["status"])
                    break
        outs = [r for r in req("GET", "/api/state")["graph"]["resources"] if r.get("producer") == pid]
        check("产出 1 个输出节点", len(outs) == 1, str(len(outs)))
        out_name = outs[0]["name"]
        check("输出图落在画布 2 的池子里",
              (Path(TMPWS) / "canvases" / c2 / "images" / f"{out_name}.png").is_file(), out_name)
        check("没有跑到画布 1 去",
              not (Path(TMPWS) / "canvases" / c1 / "images" / f"{out_name}.png").exists(), "")

        # ---- 切回画布 1，应该还是它自己的样子 ----
        st = req("POST", f"/api/canvases/{c1}/open")
        check("切回画布 1", st["canvasId"] == c1, st["canvasId"])
        check("画布 1 只有自己那个节点", len(st["graph"]["resources"]) == 1
              and len(st["graph"]["processes"]) == 0, str(len(st["graph"]["resources"])))

        # ---- 跨画布复制/粘贴：把画布 2 的输入图粘到画布 1 ----
        req("POST", f"/api/canvases/{c2}/open")
        st = req("POST", "/api/clipboard/copy", {"ids": [r2["id"]]})
        check("复制了 1 个节点", st["nodes"] == 1, str(st))
        st = req("POST", f"/api/canvases/{c1}/open")
        st = req("POST", "/api/clipboard/paste", {"x": 800.0, "y": 600.0})
        pasted = [r for r in st["graph"]["resources"] if r["id"] not in ("A",)]
        names = [r["name"] for r in st["graph"]["resources"]]
        check("粘贴后画布 1 有 2 个节点", len(st["graph"]["resources"]) == 2, str(names))
        check("新节点拿到了新名字 B", "B" in names, str(names))
        check("图片文件被复制进画布 1 的池子",
              (Path(TMPWS) / "canvases" / c1 / "images" / "B.png").is_file(), "")
        check("位置按粘贴点平移", any(abs(r["x"] - 800.0) < 40 for r in st["graph"]["resources"]
                                     if r["name"] == "B"), "")
        c_b = color_of(img_bytes(f"/api/images/{c1}/B"))
        check("粘过来的是蓝图（内容跟着原节点）", c_b == (20, 20, 220), str(c_b))
        c_a1 = color_of(img_bytes(f"/api/images/{c1}/A"))
        check("画布 1 原来的 A 没被覆盖（还是红的）", c_a1 == (220, 20, 20), str(c_a1))
        check("粘贴后 hasImage 为真",
              all(r.get("hasImage") for r in st["graph"]["resources"]), "")

        # ---- 改名 / 删除 ----
        st = req("PATCH", f"/api/canvases/{c2}", {"name": "抠图专用"})
        check("改名生效", next(c["name"] for c in st["canvases"] if c["id"] == c2) == "抠图专用", "")
        st = req("DELETE", f"/api/canvases/{c2}")
        check("删画布后只剩 1 个", len(st["canvases"]) == 1, str(len(st["canvases"])))
        check("被删画布的图片池目录已消失", not (Path(TMPWS) / "canvases" / c2).exists(), "")
        check("当前画布自动落到剩下的那个", st["canvasId"] == c1, st["canvasId"])
        check("画布 1 的图没受影响",
              (Path(TMPWS) / "canvases" / c1 / "images" / "A.png").is_file(), "")
        try:
            req("DELETE", f"/api/canvases/{c1}")
            check("最后一个画布不给删", False, "居然删成功了")
        except urllib.error.HTTPError as e:
            check("最后一个画布不给删", e.code == 400, f"HTTP {e.code}")

        # ---- 重启后能恢复（同一个文件里存了画布列表 + current）----
        raw = json.loads((Path(TMPWS) / "graph.json").read_text(encoding="utf-8"))
        check("graph.json 是 v2 容器", raw.get("version") == 2 and len(raw["canvases"]) == 1, "")
        check("current 指向存在的画布",
              raw["current"] in [c["id"] for c in raw["canvases"]], raw["current"])
    finally:
        shutil.rmtree(TMPSCRIPT, ignore_errors=True)
        try:
            req("POST", "/api/registry/reload")
        except Exception:  # noqa: BLE001
            pass
        if orig_ws:
            req("POST", "/api/workspace", {"path": orig_ws})
        time.sleep(1.0)
        shutil.rmtree(TMPWS, ignore_errors=True)
        shutil.rmtree(seed, ignore_errors=True)
        print(f"\n已清理临时脚本与工作目录，恢复到 {orig_ws}")


if __name__ == "__main__":
    part_a()
    try:
        part_b()
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] B 部分异常: {type(e).__name__}: {e}")
        results.append((False, "B 部分执行", str(e)))
    bad = [r for r in results if not r[0]]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    sys.exit(1 if bad else 0)
