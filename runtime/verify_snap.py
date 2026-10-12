# -*- coding: utf-8 -*-
"""网格吸附验证。

A 部分：纯函数（不起服务）
  - grid_size / snap_value / snap_up / snap_graph 的行为
  - **优先级**：吸附开着时，整图排版既要落在格点上，间距又不能小于 ROW_GAP_Y
B 部分：打真实实例（默认 8770，可用 IPF_BASE 覆盖），验证
  - PATCH 设置开启吸附 → 整个画布的坐标立刻被收齐
  - 新增输出槽位也落在格点上
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import engine as engine_mod  # noqa: E402

BASE = os.environ.get("IPF_BASE", "http://127.0.0.1:8770")
TMPWS = ROOT / "runtime" / "snap_ws"
TMPSCRIPT = ROOT / "scripts" / "_snap_test"

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


# ---------------------------------------------------------------- A. 纯函数
def part_a() -> None:
    print("=" * 62)
    print("[A] 纯函数：吸附格 / 就近吸附 / 向上吸附 / 全局收口")
    print("=" * 62)

    off = {"nodeMin": {"resource": {"w": 200, "h": 180}, "process": {"w": 560, "h": 280}}}
    on = {**off, "snapToGrid": True, "gridSize": 5}

    check("没开吸附 → 格大小 0", engine_mod.grid_size(off) == 0, str(engine_mod.grid_size(off)))
    check("开了吸附 → 格大小 5", engine_mod.grid_size(on) == 5, str(engine_mod.grid_size(on)))
    check("开了但格大小非法 → 0",
          engine_mod.grid_size({"snapToGrid": True, "gridSize": 0}) == 0
          and engine_mod.grid_size({"snapToGrid": True, "gridSize": "abc"}) == 0, "")

    # 就近吸附：122.4 → 120，122.6 → 125（半个格 2.5 为界）
    check("就近吸附 122.4 → 120", engine_mod.snap_value(122.4, 5) == 120, str(engine_mod.snap_value(122.4, 5)))
    check("就近吸附 122.6 → 125", engine_mod.snap_value(122.6, 5) == 125, str(engine_mod.snap_value(122.6, 5)))
    check("就近吸附已经是格点 → 不动", engine_mod.snap_value(120.0, 5) == 120, "")
    # 向上吸附：122.4 → 125（绝不下探）
    check("向上吸附 122.4 → 125", engine_mod.snap_up(122.4, 5) == 125, str(engine_mod.snap_up(122.4, 5)))
    check("向上吸附 120.0 → 120（已对齐就不推上去）", engine_mod.snap_up(120.0, 5) == 120,
          str(engine_mod.snap_up(120.0, 5)))
    check("向上吸附 120.000000001 → 120（浮点容差）",
          engine_mod.snap_up(120.000000001, 5) == 120, str(engine_mod.snap_up(120.000000001, 5)))

    # 全局收口：只动不对齐的
    g = {"resources": [{"id": "r1", "x": 120.0, "y": 161.32}, {"id": "r2", "x": 0.0, "y": 100.0}],
         "processes": [{"id": "p1", "x": 100.0, "y": 83.02}]}
    n = engine_mod.snap_graph(g, on)
    pos = {**{r["id"]: (r["x"], r["y"]) for r in g["resources"]},
           **{p["id"]: (p["x"], p["y"]) for p in g["processes"]}}
    print(f"   移动 {n} 个 → {pos}")
    check("只移动不对齐的两个（r1 与 p1）", n == 2, str(n))
    check("r1 → (120, 160)", pos["r1"] == (120.0, 160.0), str(pos["r1"]))
    check("p1 → (100, 85)", pos["p1"] == (100.0, 85.0), str(pos["p1"]))
    check("本来就对齐的没被碰", pos["r2"] == (0.0, 100.0), str(pos["r2"]))
    check("再跑一次是幂等的", engine_mod.snap_graph(g, on) == 0, "")
    g2 = {"resources": [{"id": "r1", "x": 123.7, "y": 161.3}], "processes": []}
    check("吸附关着时是空操作", engine_mod.snap_graph(g2, off) == 0 and g2["resources"][0]["x"] == 123.7, "")

    # ---- 优先级：先满足吸附，再尽量满足间距 ----
    print("\n[优先级] 吸附开着时整图排版：落在格点上 且 间距 ≥ ROW_GAP_Y")
    manifest = {"id": "crop", "size": {"defaultWidth": 560, "defaultHeight": 480}}
    for gs in (5, 8, 13):
        s = {**off, "snapToGrid": True, "gridSize": gs}
        graph = {
            "processes": [{"id": "p1", "defId": "crop", "x": 2577.04, "y": 83.02, "w": 560.0}],
            # 高度刻意取非格点倍数（445 是 5 的倍数，但 8/13 不是）
            "resources": [
                {"id": f"r{i}", "name": f"R{i}", "kind": "output", "producer": "p1", "index": i,
                 "x": 0.0, "y": 0.0, "h": 445.0}
                for i in range(4)
            ],
            "edges": [],
        }
        engine_mod.format_all(graph, {"crop": manifest}, s)
        items = sorted(graph["resources"], key=lambda r: r["index"])
        procs = graph["processes"]
        on_grid = all(r["x"] % gs == 0 and r["y"] % gs == 0 for r in items + procs)
        gaps = [items[i + 1]["y"] - (items[i]["y"] + 445.0) for i in range(len(items) - 1)]
        print(f"   格={gs:>2}  y={[r['y'] for r in items]}  间距={gaps}")
        check(f"格={gs}: 所有节点都落在格点上", on_grid,
              f"资源 x={[r['x'] for r in items]} 处理 {[(p['x'], p['y']) for p in procs]}")
        check(f"格={gs}: 间距都不小于 ROW_GAP_Y({engine_mod.ROW_GAP_Y})",
              all(gp >= engine_mod.ROW_GAP_Y for gp in gaps), str(gaps))
        check(f"格={gs}: 相邻不重叠", all(gp >= 0 for gp in gaps), str(gaps))


# ------------------------------------------------------ B. 真实实例
SNAP_SCRIPT = '''\
# -*- coding: utf-8 -*-
"""测试用：产出 N 张小图，不调用任何模型。"""
import json, os, sys
from PIL import Image

payload = json.loads(sys.stdin.readline())
out = payload["output_dir"]
n = int(payload["params"].get("n") or 1)
for i in range(n):
    Image.new("RGB", (64, 64), (40 + i * 60, 90, 200)).save(os.path.join(out, f"{i}.png"))
print(f"产出 {n} 张")
'''

SNAP_MANIFEST = {
    "id": "_snap_test",
    "name": "吸附测试",
    "entry": "gen.py",
    "inputs": [],
    "params": [{"key": "n", "type": "number", "default": 3, "label": "张数", "width": "narrow"}],
    "outputs": [{"key": "image", "type": "image", "multi": True}],
    "size": {"defaultWidth": 600, "defaultHeight": 340},
}


def part_b() -> None:
    print("\n" + "=" * 62)
    print("[B] 真实实例：开启吸附 → 整个画布立刻收齐；新槽位也落在格点上")
    print("=" * 62)
    st0 = req("GET", "/api/state")
    orig_ws = st0["workspace"]
    orig_snap = st0["settings"].get("snapToGrid")
    orig_grid = st0["settings"].get("gridSize")

    TMPSCRIPT.mkdir(parents=True, exist_ok=True)
    (TMPSCRIPT / "manifest.json").write_text(
        json.dumps(SNAP_MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    (TMPSCRIPT / "gen.py").write_text(SNAP_SCRIPT, encoding="utf-8")
    try:
        req("POST", "/api/registry/reload")
        # 设置是工程级共用的（settings.json 不在工作目录里），别的测试或界面可能
        # 把它开着。先明确关掉，否则下面"造几个不对齐的节点"会被就地吸走。
        req("PATCH", "/api/settings", {"snapToGrid": False})
        shutil.rmtree(TMPWS, ignore_errors=True)
        req("POST", "/api/workspace", {"path": str(TMPWS)})

        # 先造几个刻意不对齐的节点
        st = req("POST", "/api/processes", {"defId": "_snap_test", "x": 503.3, "y": 297.7})
        pid = st["graph"]["processes"][-1]["id"]
        req("PATCH", "/api/positions", [{"id": pid, "x": 502.6, "y": 297.7}])
        st = req("POST", "/api/processes", {"defId": "crop", "x": 111.4, "y": 88.9})
        pid2 = st["graph"]["processes"][-1]["id"]
        req("PATCH", "/api/positions", [{"id": pid2, "x": 111.4, "y": 88.9}])

        before = [(p["id"], p["x"], p["y"]) for p in req("GET", "/api/state")["graph"]["processes"]]
        print(f"   开启前：{[(round(x, 2), round(y, 2)) for _, x, y in before]}")

        st = req("PATCH", "/api/settings", {"snapToGrid": True, "gridSize": 5})
        after = [(p["id"], p["x"], p["y"]) for p in st["graph"]["processes"]]
        check("贴的是 5 的格（不是别的格大小）",
              st["settings"]["gridSize"] == 5, str(st["settings"]["gridSize"]))
        print(f"   开启后：{[(round(x, 2), round(y, 2)) for _, x, y in after]}")
        check("开启吸附会返回新状态（前端据此重绘）", "graph" in st, "")
        check("所有节点坐标都被收齐（5 的倍数）",
              all(x % 5 == 0 and y % 5 == 0 for _, x, y in after),
              str([(x, y) for _, x, y in after]))
        check("就近吸附：502.6 → 505（过了半格就进位）",
              dict((i, (x, y)) for i, x, y in after)[pid][0] == 505.0,
              str(dict((i, (x, y)) for i, x, y in after)[pid]))
        check("就近吸附：88.9 → 90", dict((i, (x, y)) for i, x, y in after)[pid2][1] == 90.0,
              str(dict((i, (x, y)) for i, x, y in after)[pid2]))

        # 落盘也应该是收齐的（重开实例才会一样）
        doc = json.loads((TMPWS / "graph.json").read_text(encoding="utf-8"))
        cur = next(c for c in doc["canvases"] if c["id"] == doc["current"])
        check("graph.json 里存的坐标也是收齐的",
              all(p["x"] % 5 == 0 and p["y"] % 5 == 0 for p in cur["processes"]), "")

        # 执行一个节点 → 新增的输出槽位也该落在格点上
        n_slots = 4
        req("PATCH", f"/api/processes/{pid}", {"params": {"n": n_slots}})
        run = req("POST", f"/api/processes/{pid}/execute", {})
        with urllib.request.urlopen(f"{BASE}/api/runs/{run['runId']}/stream", timeout=180) as r:
            for raw_line in r:
                line = raw_line.decode("utf-8", "replace").strip()
                if line.startswith("data: ") and json.loads(line[6:]).get("type") == "end":
                    print("   >>>", json.loads(line[6:])["status"])
                    break
        g = req("GET", "/api/state")["graph"]
        outs = sorted([r for r in g["resources"] if r.get("producer") == pid], key=lambda r: r["index"])
        proc = next(p for p in g["processes"] if p["id"] == pid)
        print(f"   产出 {len(outs)} 个：{[ (round(o['x']), round(o['y'])) for o in outs ]}")
        check(f"产出了 {n_slots} 个输出", len(outs) == n_slots, str(len(outs)))
        check("新槽位坐标也在格点上",
              all(o["x"] % 5 == 0 and o["y"] % 5 == 0 for o in outs),
              str([(o["x"], o["y"]) for o in outs]))
        check("都排在同一列", len({o["x"] for o in outs}) == 1, str({o["x"] for o in outs}))
        h = float(outs[0].get("h") or 280.0)
        gaps = [outs[i + 1]["y"] - (outs[i]["y"] + h) for i in range(len(outs) - 1)]
        print(f"   间距 {gaps}（节点高 {h}）")
        check("间距不小于 SLOT_GAP_Y", all(gp >= engine_mod.SLOT_GAP_Y for gp in gaps), str(gaps))
        # 处理节点自身也回到格点上了吗
        check("处理节点也在格点上", proc["x"] % 5 == 0 and proc["y"] % 5 == 0,
              f"({proc['x']}, {proc['y']})")
    finally:
        # 关掉吸附并还原现场
        try:
            req("PATCH", "/api/settings", {"snapToGrid": bool(orig_snap), "gridSize": orig_grid or 5})
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(TMPSCRIPT, ignore_errors=True)
        try:
            req("POST", "/api/registry/reload")
        except Exception:  # noqa: BLE001
            pass
        if orig_ws:
            req("POST", "/api/workspace", {"path": orig_ws})
        time.sleep(1.0)
        shutil.rmtree(TMPWS, ignore_errors=True)
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
