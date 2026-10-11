# -*- coding: utf-8 -*-
"""验证输出节点的落点：按节点实际尺寸推算，不再互相重叠、也不压住上游。

A 部分：直接调 engine 的纯函数（不用起浏览器）
B 部分：真跑一次执行（用临时脚本产出 3 张图，不调用任何模型、不花钱）

B 部分会**切换目标实例的工作目录**（跑完恢复），所以默认只打本机 8770；
想验证另一个实例（例如自己起的临时实例）用环境变量覆盖：
    IPF_BASE=http://127.0.0.1:8791 python runtime/verify_slots.py
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
import registry as registry_mod  # noqa: E402
from workspace import Workspace  # noqa: E402

BASE = os.environ.get("IPF_BASE", "http://127.0.0.1:8770")
TMPWS = ROOT / "runtime" / "slot_ws"
TMPSCRIPT = ROOT / "scripts" / "_slot_test"

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
    print("[A] slot_origin / resource_height：落点按节点实际尺寸算")
    print("=" * 62)
    manifest = {"id": "qwen_image", "size": {"defaultWidth": 600, "defaultHeight": 340}}
    settings = {"nodeMin": {"resource": {"w": 200, "h": 180}, "process": {"w": 560, "h": 280}}}

    proc = {"id": "p1", "defId": "qwen_image", "x": 1000, "y": 200}
    bx, by = engine_mod.slot_origin(proc, manifest, settings)
    print(f"   默认（无 w）：x={bx} y={by}")
    check("横向落点 = 节点 x + 宽度(600) + 留白", bx == 1000 + 600 + 64, str(bx))
    check("纵向起点 = 节点 y", by == 200, str(by))

    # 纵向间距的依据：数据节点的**实际渲染高**，必须和前端算法一致
    rh = lambda r: engine_mod.resource_height(r, settings)  # noqa: E731
    print(f"   实际渲染高：无尺寸={rh({})}  h=180 → {rh({'h': 180})}  "
          f"h=150 → {rh({'h': 150})}  h=364 → {rh({'h': 364})}")
    check("没存过尺寸 → 默认高 280", rh({}) == 280, str(rh({})))
    check("存了 h=180 → 实际高 180（用户报的那个场景）", rh({"h": 180}) == 180, str(rh({"h": 180})))
    check("存了 h=150 但下限是 180 → 被下限兜到 180", rh({"h": 150}) == 180, str(rh({"h": 150})))
    check("存了 h=364 → 用 364", rh({"h": 364}) == 364, str(rh({"h": 364})))
    check("间距不再写死：调矮后间距跟着变",
          rh({}) + 10 == 290 and rh({"h": 180}) + 10 == 190,
          f"280→{rh({}) + 10}, 180→{rh({'h': 180}) + 10}")

    # 用户把节点拖大了 → 落点跟着让开
    proc2 = dict(proc, w=880)
    bx2, _ = engine_mod.slot_origin(proc2, manifest, settings)
    print(f"   节点被拖到 880 宽：x={bx2}")
    check("节点变宽后输出跟着右移", bx2 == 1000 + 880 + 64, str(bx2))

    # 设置里把该类型的下限调大 → 也要让开
    s2 = json.loads(json.dumps(settings))
    s2["nodeMin"]["process:qwen_image"] = {"w": 900, "h": 400}
    bx3, _ = engine_mod.slot_origin(proc, manifest, s2)
    print(f"   设置里该类型下限改成 900：x={bx3}")
    check("设置里的专属下限生效", bx3 == 1000 + 900 + 64, str(bx3))

    # 数据节点最小高度被调大 → 实际高跟着变
    s3 = json.loads(json.dumps(settings))
    s3["nodeMin"]["resource"] = {"w": 200, "h": 500}
    print(f"   数据节点下限高度改成 500：实际高={engine_mod.resource_height({}, s3)}")
    check("数据节点下限变大 → 实际高跟着变", engine_mod.resource_height({}, s3) == 500,
          str(engine_mod.resource_height({}, s3)))

    # tidy_outputs：把摆歪的重新排好
    print("\n[tidy_outputs] 收拾摆歪的输出节点（数据节点 280 高）")
    g = {
        "processes": [{"id": "p1", "defId": "qwen_image", "x": 0, "y": 0, "w": 600}],
        "resources": [
            {"id": "r1", "producer": "p1", "index": 0, "x": 380, "y": 0},      # 压在节点上
            {"id": "r2", "producer": "p1", "index": 1, "x": 380, "y": 210},    # 又和 r1 重叠
            {"id": "r3", "producer": "p1", "index": 2, "x": 380, "y": 420},
            {"id": "r4", "name": "A", "kind": "input", "x": 5, "y": 5},        # 输入节点不该被碰
        ],
    }
    n = engine_mod.tidy_outputs(g, {"qwen_image": manifest}, settings)
    pos = {r["id"]: (r["x"], r["y"]) for r in g["resources"]}
    print(f"   移动了 {n} 个；结果 {pos}")
    check("三个输出都被移动", n == 3, str(n))
    check("都摆到节点右侧（x = 600+64）", all(pos[k][0] == 664 for k in ("r1", "r2", "r3")), str(pos))
    check("纵向按序号排开（间距 290 = 280+10）",
          pos["r1"][1] == 0 and pos["r2"][1] == 290 and pos["r3"][1] == 580, str(pos))
    check("输入节点没被碰", pos["r4"] == (5, 5), str(pos["r4"]))
    n2 = engine_mod.tidy_outputs(g, {"qwen_image": manifest}, settings)
    check("再跑一次是幂等的（不再移动）", n2 == 0, str(n2))

    # 回归：数据节点被调矮到 180 时，必须排得紧凑（用户报的问题）
    print("\n[tidy_outputs] 数据节点高 180 → 应该只留 10 的缝")
    g2 = {
        "processes": [{"id": "p1", "defId": "qwen_image", "x": 0, "y": 0, "w": 600}],
        "resources": [
            {"id": f"q{i}", "producer": "p1", "index": i, "x": 380, "y": 0, "h": 180, "w": 200}
            for i in range(4)
        ],
    }
    engine_mod.tidy_outputs(g2, {"qwen_image": manifest}, settings)
    ys = [r["y"] for r in sorted(g2["resources"], key=lambda r: r["index"])]
    print(f"   四个节点的 y：{ys}")
    check("相邻间距 = 180 + 10 = 190（不再是写死的 290）", ys == [0, 190, 380, 570], str(ys))
    check("缝隙正好是 10", all(ys[i + 1] - ys[i] - 180 == 10 for i in range(3)), str(ys))

    # 高低不一的一组：按每个节点自己的高度逐个累加
    print("\n[tidy_outputs] 高低不一的一组也要紧密贴合")
    g3 = {
        "processes": [{"id": "p1", "defId": "qwen_image", "x": 0, "y": 0, "w": 600}],
        "resources": [
            {"id": "a", "producer": "p1", "index": 0, "x": 0, "y": 0, "h": 180},
            {"id": "b", "producer": "p1", "index": 1, "x": 0, "y": 0, "h": 400},
            {"id": "c", "producer": "p1", "index": 2, "x": 0, "y": 0, "h": 180},
        ],
    }
    engine_mod.tidy_outputs(g3, {"qwen_image": manifest}, settings)
    ys3 = [r["y"] for r in sorted(g3["resources"], key=lambda r: r["index"])]
    print(f"   三个节点的 y：{ys3}（高 180 / 400 / 180）")
    check("按各自高度累加：0 → 190 → 600", ys3 == [0, 190, 600], str(ys3))


# ------------------------------------------------------ B. 真实执行一遍
SLOT_SCRIPT = '''\
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

SLOT_MANIFEST = {
    "id": "_slot_test",
    "name": "槽位测试",
    "entry": "gen.py",
    "inputs": [],
    "params": [{"key": "n", "type": "number", "default": 3, "label": "张数", "width": "narrow"}],
    "outputs": [{"key": "image", "type": "image", "multi": True}],
    "size": {"defaultWidth": 600, "defaultHeight": 340},
}


def part_b() -> None:
    print("\n" + "=" * 62)
    print("[B] 真实执行：产出 3 张图，检查生成的节点位置")
    print("=" * 62)
    orig_ws = req("GET", "/api/state")["workspace"]

    TMPSCRIPT.mkdir(parents=True, exist_ok=True)
    (TMPSCRIPT / "manifest.json").write_text(
        json.dumps(SLOT_MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    (TMPSCRIPT / "gen.py").write_text(SLOT_SCRIPT, encoding="utf-8")

    try:
        req("POST", "/api/registry/reload")
        shutil.rmtree(TMPWS, ignore_errors=True)
        req("POST", "/api/workspace", {"path": str(TMPWS)})

        st = req("POST", "/api/processes", {"defId": "_slot_test", "x": 500, "y": 300})
        pid = st["graph"]["processes"][-1]["id"]

        run = req("POST", f"/api/processes/{pid}/execute", {})
        with urllib.request.urlopen(f"{BASE}/api/runs/{run['runId']}/stream", timeout=180) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                msg = json.loads(line[6:])
                if msg["type"] == "log" and msg["line"].strip():
                    print("   |", msg["line"])
                elif msg["type"] == "end":
                    print("   >>>", msg["status"], msg.get("error") or "")
                    break

        g = req("GET", "/api/state")["graph"]
        proc = next(p for p in g["processes"] if p["id"] == pid)
        outs = sorted([r for r in g["resources"] if r.get("producer") == pid], key=lambda r: r["index"])
        print(f"   处理节点 x={proc['x']} y={proc['y']}  （渲染宽 600、高 340）")
        for o in outs:
            print(f"   输出 #{o['index']} {o['name']}  x={o['x']} y={o['y']}")

        check("产出了 3 个输出节点", len(outs) == 3, str(len(outs)))

        pw, ph = 600.0, 340.0          # 节点渲染尺寸（manifest defaultWidth/Height，未拖动）
        rw, rh = 250.0, 280.0          # 数据节点渲染尺寸（RESOURCE_SIZE 默认）
        ok_right = all(o["x"] >= proc["x"] + pw for o in outs)
        check("都在处理节点右侧（不压住上游）", ok_right,
              f"最小 x 差 {min(o['x'] for o in outs) - proc['x']:.0f} >= {pw:.0f}")

        gaps = [outs[i + 1]["y"] - outs[i]["y"] for i in range(len(outs) - 1)]
        check("相邻输出不重叠（间距 ≥ 数据节点高）", all(gp >= rh for gp in gaps),
              f"间距 {[round(x) for x in gaps]} >= {rh:.0f}")
        check("纵向留白正好是 SLOT_GAP_Y(10)，不多不少", all(abs(gp - (rh + 10)) < 1e-6 for gp in gaps),
              f"间距 {[round(x) for x in gaps]} == {rh:.0f}+10")

        xs = {round(o["x"]) for o in outs}
        check("同一列对齐", len(xs) == 1, str(xs))

        # 两两不相交（矩形判交）
        rects = [(o["x"], o["y"], o["x"] + rw, o["y"] + rh) for o in outs]
        rects.append((proc["x"], proc["y"], proc["x"] + pw, proc["y"] + ph))
        bad = []
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                if not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3]):
                    bad.append((i, j))
        check("任意两个矩形都不相交", not bad, f"相交对 {bad}")

        # 再执行一次：槽位是原地刷新，不该改变位置
        before = [(o["x"], o["y"]) for o in outs]
        run2 = req("POST", f"/api/processes/{pid}/execute", {})
        with urllib.request.urlopen(f"{BASE}/api/runs/{run2['runId']}/stream", timeout=180) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: ") and json.loads(line[6:]).get("type") == "end":
                    break
        g2 = req("GET", "/api/state")["graph"]
        outs2 = sorted([r for r in g2["resources"] if r.get("producer") == pid], key=lambda r: r["index"])
        after = [(o["x"], o["y"]) for o in outs2]
        check("重执行时槽位原地刷新、位置不变", before == after, f"{before} vs {after}")
    finally:
        shutil.rmtree(TMPSCRIPT, ignore_errors=True)
        try:
            req("POST", "/api/registry/reload")
        except Exception:  # noqa: BLE001
            pass
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
