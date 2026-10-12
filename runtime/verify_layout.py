# -*- coding: utf-8 -*-
"""验证「格式化所有节点」（整图自动布局，DESIGN.md §3.11）。

A 部分：纯函数，直接喂合成图给 `engine.format_all`（不用起服务）
    - 任意两个节点不相交 / 每条边都向右 / 同列不重叠
    - 同一处理节点的多个输出：连排 + 序号递增，且**不会被上一个输出的下游压到**
    - 幂等（连跑两次结果一致，第二次移动 0 个）
    - 吸附开着时所有坐标都落格点，且间距只大不小
    - 平移不变性（整块画布挪个位置，排出来的形状完全一样，只是跟着挪）
    - 孤立的输入图、单节点、空图、成环（防御）都不炸
B 部分：对着跑着的实例（默认 127.0.0.1:8770）——/api/format 的真实行为 + 撤销

    IPF_BASE=http://127.0.0.1:8791 python runtime/verify_layout.py   :: 打别的实例

⚠️ B 部分会**切换目标实例的工作目录**（跑完恢复）：期间那个实例的界面上会看到
别的画布。所以**对着自己起的临时实例跑**，别打正在用的 8770。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import engine as engine_mod  # noqa: E402
import nodespec as ns  # noqa: E402

BASE = os.environ.get("IPF_BASE", "http://127.0.0.1:8770")
TMPWS = ROOT / "runtime" / "layout_ws"

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


# 宽松的下限：让合成图里写死的 w/h 原样生效（默认下限 560×280 / 200×180 会顶掉小尺寸）
S_MIN = {"nodeMin": {"resource": {"w": 80, "h": 80}, "process": {"w": 80, "h": 80}}}
S_SNAP = dict(S_MIN, snapToGrid=True, gridSize=5)
REG: dict = {}


def res(rid, x, y, producer=None, index=0, name=None, **kw):
    r = {"id": rid, "name": name or rid.upper(), "kind": "output" if producer else "input",
         "x": float(x), "y": float(y)}
    if producer is not None:
        r["producer"] = producer
        r["index"] = index
    r.update(kw)
    return r


def proc(pid, x, y, w=400, h=200, **kw):
    p = {"id": pid, "defId": "_t", "x": float(x), "y": float(y), "w": float(w), "h": float(h)}
    p.update(kw)
    return p


def edge(a, b, port="image1"):
    return {"id": f"e_{a}_{b}", "from": a, "to": b, "port": port}


def all_nodes(graph):
    return [*graph.get("resources", []), *graph.get("processes", [])]


def node_of(graph, nid):
    return next(n for n in all_nodes(graph) if n["id"] == nid)


def positions(graph):
    return {n["id"]: (n["x"], n["y"]) for n in all_nodes(graph)}


def boxes(graph, settings=S_MIN):
    """{id: (x, y, w, h)} —— 尺寸按**服务端口径**算，和前端渲染的是同一个数。"""
    out = {}
    for r in graph.get("resources", []):
        w, h = ns.size_of(r, kind="resource", settings=settings)
        out[r["id"]] = (r["x"], r["y"], w, h)
    for p in graph.get("processes", []):
        w, h = ns.size_of(p, kind="process", manifest=REG.get(p.get("defId")), settings=settings)
        out[p["id"]] = (p["x"], p["y"], w, h)
    return out


def intersected(box: dict) -> list:
    ids = list(box)
    bad = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ax, ay, aw, ah = box[a]
            bx, by, bw, bh = box[b]
            if not (ax + aw <= bx + 1e-9 or bx + bw <= ax + 1e-9
                    or ay + ah <= by + 1e-9 or by + bh <= ay + 1e-9):
                bad.append((a, b))
    return bad


def edge_pairs(graph):
    pairs = [(e["from"], e["to"]) for e in graph.get("edges", [])]
    pairs += [(r["producer"], r["id"]) for r in graph.get("resources", []) if r.get("producer")]
    return pairs


def wrong_direction(graph, settings=S_MIN) -> list:
    """返回所有「从右指向左」的边（每条边都必须源在左、目标在右）。"""
    box = boxes(graph, settings)
    return [
        (a, b)
        for a, b in edge_pairs(graph)
        if a in box and b in box and box[a][0] + box[a][2] > box[b][0] + 1e-9
    ]


def off_grid(graph, g=5.0) -> list:
    return [
        (n["id"], axis, n[axis])
        for n in all_nodes(graph)
        for axis in ("x", "y")
        if abs(float(n[axis]) / g - round(float(n[axis]) / g)) > 1e-9
    ]


def distinct_x(graph, ids) -> set:
    return {round(float(node_of(graph, i)["x"]), 3) for i in ids}


# ------------------------------------------------------------------ A. 纯函数
def case_basic() -> None:
    print("\n[1] 基本分层：输入图 → 处理 → 3 个输出（第 1 个还有下游）")
    graph = {
        "resources": [
            res("i1", 0, 0, w=200, h=100),
            res("o1", 900, 30, producer="k1", index=0, w=200, h=120),   # 故意摆得又歪又远
            res("o2", 60, 900, producer="k1", index=1, w=200, h=120),
            res("o3", -500, 400, producer="k1", index=2, w=200, h=120),
            res("o4", 700, 700, producer="k2", index=0, w=200, h=120),
        ],
        "processes": [
            proc("k1", 300, 0, w=400, h=200),
            proc("k2", 1500, 0, w=400, h=200),
        ],
        "edges": [edge("i1", "k1", "image1"), edge("o1", "k2", "image1")],
    }
    anchor_x = min(n["x"] for n in all_nodes(graph))
    anchor_y = min(n["y"] for n in all_nodes(graph))
    moved = engine_mod.format_all(graph, REG, S_MIN)
    print(f"   移动 {moved} 个；锚点 ({anchor_x}, {anchor_y})")
    print("   结果：" + json.dumps(positions(graph), ensure_ascii=False))
    check("所有节点都被重新摆过", moved == 7, str(moved))
    check("任意两个节点都不相交", not intersected(boxes(graph)), str(intersected(boxes(graph))))
    check("每条边都从左指向右", not wrong_direction(graph), str(wrong_direction(graph)))
    check("整体左上角对回原来的包围盒左上角（视野不跳）",
          min(n["x"] for n in all_nodes(graph)) == anchor_x
          and min(n["y"] for n in all_nodes(graph)) == anchor_y)

    x_i1 = node_of(graph, "i1")["x"]
    x_k1 = node_of(graph, "k1")["x"]
    check("输入图在最左列", x_i1 == anchor_x, f"{x_i1} vs {anchor_x}")
    check(f"处理节点紧跟输入图右侧（+ 宽 200 + {engine_mod.COL_GAP_X}）",
          x_k1 == x_i1 + 200 + engine_mod.COL_GAP_X, str(x_k1))
    check("三个输出同列、且在处理节点右侧一列",
          len(distinct_x(graph, ["o1", "o2", "o3"])) == 1
          and node_of(graph, "o1")["x"] == x_k1 + 400 + engine_mod.COL_GAP_X,
          str(distinct_x(graph, ["o1", "o2", "o3"])))

    ys = [node_of(graph, i)["y"] for i in ("o1", "o2", "o3")]
    print(f"   三个输出的 y（按序号）：{ys}")
    check("按序号从上到下", ys == sorted(ys), str(ys))
    check(f"相邻输出紧贴：间距 = 上一个的高 + {engine_mod.ROW_GAP_Y}",
          all(abs(ys[i + 1] - ys[i] - (120 + engine_mod.ROW_GAP_Y)) < 1e-9 for i in range(2)), str(ys))
    check("o3 没有为 o1 的下游（k2）让位 —— 下游在右边的列里，不需要让",
          abs(ys[1] - ys[0] - (120 + engine_mod.ROW_GAP_Y)) < 1e-9
          and abs(ys[2] - ys[1] - (120 + engine_mod.ROW_GAP_Y)) < 1e-9, str(ys))

    check("单前驱单后继：处理节点与上游**对齐顶边**（链条拉成一条水平线）",
          node_of(graph, "k2")["y"] == node_of(graph, "o1")["y"],
          f"{node_of(graph, 'k2')['y']} vs {node_of(graph, 'o1')['y']}")

    before = positions(graph)
    again = engine_mod.format_all(graph, REG, S_MIN)
    check("幂等：再格式化一次不再移动", again == 0 and positions(graph) == before, str(again))


def case_branch() -> None:
    print("\n[2] 一个处理节点下挂多棵子树（真正要分摊的情形）")
    graph = {
        "resources": [
            res("i1", 0, 0, w=200, h=100),
            res("a", 0, 0, producer="k1", index=0, w=200, h=100),
            res("b", 0, 0, producer="k1", index=1, w=200, h=100),
        ],
        "processes": [
            proc("k1", 0, 0, w=400, h=200),
            proc("k2", 0, 0, w=400, h=300),
            proc("k3", 0, 0, w=400, h=300),
            proc("k4", 0, 0, w=400, h=300),
        ],
        "edges": [edge("i1", "k1"), edge("a", "k2"), edge("a", "k3"), edge("b", "k4")],
    }
    engine_mod.format_all(graph, REG, S_MIN)
    print("   结果：" + json.dumps(positions(graph), ensure_ascii=False))
    box = boxes(graph)
    check("任意两个节点都不相交", not intersected(box), str(intersected(box)))
    check("每条边都从左指向右", not wrong_direction(graph), str(wrong_direction(graph)))
    check("同一处理节点的两个输出仍连排（a 在上、b 在下）",
          box["a"][1] < box["b"][1], f"a.y={box['a'][1]} b.y={box['b'][1]}")
    check("a 的两个下游在同一列（各占各的高度）", len(distinct_x(graph, ["k2", "k3"])) == 1,
          str(distinct_x(graph, ["k2", "k3"])))
    check("b 的下游不会被 a 的下游压住（k4 在 k3 下方）",
          box["k4"][1] >= box["k3"][1] + 300 - 1e-9, f"k4.y={box['k4'][1]} k3.y={box['k3'][1]}")


def case_isolated() -> None:
    print("\n[3] 没有连线的散图（先导入一堆图、还没接线的常见状态）")
    graph = {
        "resources": [
            res("i1", 0, 0, w=200, h=100),
            res("i2", 300, 0, w=200, h=100),
            res("i3", 700, 0, w=200, h=100),
        ],
        "processes": [proc("k1", 1200, 0, w=400, h=200)],
        "edges": [],
    }
    engine_mod.format_all(graph, REG, S_MIN)
    print("   结果：" + json.dumps(positions(graph), ensure_ascii=False))
    xs = {n["x"] for n in all_nodes(graph)}
    check("任意两个节点都不相交", not intersected(boxes(graph)), str(intersected(boxes(graph))))
    check("都排在最左边一列（左对齐）", len(xs) == 1, str(xs))
    ys = [node_of(graph, i)["y"] for i in ("i1", "i2", "i3", "k1")]
    check("按原来的上下顺序堆叠", ys == sorted(ys), str(ys))
    check("彼此间距 = 上一个的高 + 组件间距(80)", ys[1] - ys[0] == 100 + 80, str(ys))


def case_snap() -> None:
    print("\n[4] 吸附开着：全部落格点，间距只大不小（格 5）")
    graph = {
        "resources": [
            res("i1", 3, 7, w=200, h=187),
            res("a", 0, 0, producer="k1", index=0, w=200, h=187),
            res("b", 0, 0, producer="k1", index=1, w=200, h=163),
        ],
        "processes": [proc("k1", 11, 13, w=400, h=193)],
        "edges": [edge("i1", "k1")],
    }
    engine_mod.format_all(graph, REG, S_SNAP)
    print("   结果：" + json.dumps(positions(graph), ensure_ascii=False))
    check("所有坐标都是 5 的倍数", not off_grid(graph), str(off_grid(graph)))
    check("任意两个节点都不相交", not intersected(boxes(graph, S_SNAP)),
          str(intersected(boxes(graph, S_SNAP))))
    gap = node_of(graph, "b")["y"] - node_of(graph, "a")["y"] - 187
    check(f"间距不小于 ROW_GAP_Y({engine_mod.ROW_GAP_Y})", gap >= engine_mod.ROW_GAP_Y - 1e-9,
          f"实际 {gap}")
    check("每条边都从左指向右", not wrong_direction(graph, S_SNAP), "")


def case_translate() -> None:
    print("\n[5] 平移不变性：整块画布挪个位置，形状一模一样")

    def build(dx, dy):
        return {
            "resources": [
                res("i1", dx, dy, w=200, h=100),
                res("a", dx + 40, dy + 30, producer="k1", index=0, w=200, h=120),
                res("b", dx + 40, dy + 90, producer="k1", index=1, w=200, h=120),
            ],
            "processes": [proc("k1", dx + 300, dy + 10, w=400, h=200)],
            "edges": [edge("i1", "k1")],
        }

    g1, g2 = build(0, 0), build(1000, 500)
    engine_mod.format_all(g1, REG, S_MIN)
    engine_mod.format_all(g2, REG, S_MIN)
    print("   " + json.dumps(positions(g1), ensure_ascii=False))
    print("   " + json.dumps(positions(g2), ensure_ascii=False))
    same = all(
        abs(n2["x"] - n1["x"] - 1000) < 1e-9 and abs(n2["y"] - n1["y"] - 500) < 1e-9
        for n1, n2 in zip(all_nodes(g1), all_nodes(g2))
    )
    check("结果 = 原结果整体平移 (1000, 500)", same)


def case_size_source() -> None:
    print("\n[6] 尺寸走统一口径：数据节点没存过高 = 220（不是旧的服务端常量 280）")
    graph = {
        "resources": [
            res("i1", 0, 0, w=200, h=100),
            res("a", 0, 0, producer="k1", index=0),   # 没存过 h
            res("b", 0, 0, producer="k1", index=1),
        ],
        "processes": [proc("k1", 0, 0)],   # w=400 会被默认下限 560 顶上去
        "edges": [edge("i1", "k1")],
    }
    engine_mod.format_all(graph, REG, {})   # 默认设置（无吸附）
    dy = node_of(graph, "b")["y"] - node_of(graph, "a")["y"]
    print(f"   两个输出的 y 差：{dy}")
    check("间距 = 220 + 20（220 就是屏幕上的实际高度）",
          abs(dy - (220 + engine_mod.ROW_GAP_Y)) < 1e-9, str(dy))
    check("处理节点按默认下限 560 宽算",
          ns.size_of(node_of(graph, "k1"), kind="process", settings={})[0] == 560
          and node_of(graph, "k1")["x"] == node_of(graph, "i1")["x"] + 200 + engine_mod.COL_GAP_X,
          str(node_of(graph, "k1")["x"]))


def case_degenerate() -> None:
    print("\n[7] 退化情形：空图 / 单节点 / 成环")
    check("空图返回 0 且不炸",
          engine_mod.format_all({"resources": [], "processes": [], "edges": []}, REG, S_MIN) == 0)
    one = {"resources": [res("i1", 10, 20)], "processes": [], "edges": []}
    check("单节点不移动", engine_mod.format_all(one, REG, S_MIN) == 0)

    # 人为造环（设计上禁止，但代码不能死循环）：k1 → a → k2 → b → k1
    cyc = {
        "resources": [res("a", 0, 0, producer="k1", index=0, w=200, h=100),
                      res("b", 0, 0, producer="k2", index=0, w=200, h=100)],
        "processes": [proc("k1", 0, 0, w=200, h=100), proc("k2", 0, 0, w=200, h=100)],
        "edges": [edge("a", "k2"), edge("b", "k1")],
    }
    done = engine_mod.format_all(cyc, REG, S_MIN)
    print(f"   成环时返回 {done}（没死循环）")
    check("成环不死循环、能返回", isinstance(done, int))


def case_chain_no_drift() -> None:
    print("\n[9] 扇入扇出的长链：整条链不许一级级往下漂")
    graph = {
        "resources": [
            res("i0", 0, 0, w=200, h=100),
            res("o1", 0, 0, producer="k0", index=0, w=200, h=120),
            res("o2", 0, 0, producer="k0", index=1, w=200, h=120),
            res("o3", 0, 0, producer="k0", index=2, w=200, h=120),
            res("p1", 0, 0, producer="k1", index=0, w=200, h=120),
            res("p2", 0, 0, producer="k1", index=1, w=200, h=120),
            res("p3", 0, 0, producer="k1", index=2, w=200, h=120),
            res("q1", 0, 0, producer="k2", index=0, w=200, h=120),
        ],
        "processes": [proc("k0", 0, 0, w=400, h=200), proc("k1", 0, 0, w=400, h=200),
                      proc("k2", 0, 0, w=400, h=200)],
        "edges": [edge("i0", "k0"), edge("o1", "k1"), edge("o2", "k1"), edge("o3", "k1"),
                  edge("p1", "k2"), edge("p2", "k2"), edge("p3", "k2")],
    }
    engine_mod.format_all(graph, REG, S_MIN)
    y = {i: node_of(graph, i)["y"] for i in ("i0", "k0", "o1", "k1", "p1", "k2", "q1")}
    print("   " + json.dumps({k: round(v) for k, v in y.items()}, ensure_ascii=False))
    check("三个阶段处理节点在同一条水平线上（跟最上面那个输入走，不取均值）",
          y["k0"] == y["k1"] == y["k2"], f"k0={y['k0']} k1={y['k1']} k2={y['k2']}")
    check("输出块从处理节点的顶边开始往下挂",
          y["o1"] == y["k0"] and y["p1"] == y["k1"], f"o1={y['o1']} p1={y['p1']}")
    check("任意两个节点都不相交", not intersected(boxes(graph)), str(intersected(boxes(graph))))
    check("每条边都从左指向右", not wrong_direction(graph), "")


def case_undo_pure() -> None:
    print("\n[8] 快照 / 还原（撤销格式化用的两个纯函数）")
    graph = {
        "resources": [res("i1", 33, 44, w=200, h=100)],
        "processes": [proc("k1", 500, 600, w=400, h=200)],
        "edges": [],
    }
    snap = engine_mod.snapshot_positions(graph)
    engine_mod.format_all(graph, REG, S_MIN)
    check("格式化后坐标确实变了",
          positions(graph) != {"i1": (33.0, 44.0), "k1": (500.0, 600.0)})
    n = engine_mod.restore_positions(graph, snap)
    print(f"   还原了 {n} 个")
    check("还原后与快照完全一致", positions(graph) == {"i1": (33.0, 44.0), "k1": (500.0, 600.0)})
    check("快照里有已被删掉的节点也不炸",
          engine_mod.restore_positions(graph, snap + [{"id": "gone", "x": 1, "y": 2}]) == 0)


# ------------------------------------------------------------------ B. 接口
def make_canvas(cid: str) -> dict:
    return {
        "id": cid, "name": "排版测试", "viewport": None,
        "resources": [
            {"id": "i1", "name": "A", "kind": "input", "x": 700, "y": 40, "w": 200, "h": 220},
            {"id": "i2", "name": "B", "kind": "input", "x": 700, "y": 900, "w": 200, "h": 220},
            {"id": "o1", "name": "C", "kind": "output", "producer": "k1", "index": 0,
             "x": 100, "y": 100, "w": 200, "h": 220},
            {"id": "o2", "name": "D", "kind": "output", "producer": "k1", "index": 1,
             "x": 120, "y": 130, "w": 200, "h": 220},
        ],
        "processes": [
            {"id": "k1", "defId": "crop", "x": 2000, "y": 300, "w": 400, "h": 300, "params": {}},
            {"id": "k2", "defId": "resize", "x": 100, "y": 800, "w": 400, "h": 300,
             "ports": {"image": 1}, "params": {}},
        ],
        "edges": [
            {"id": "e1", "from": "i1", "to": "k1", "port": "image"},
            {"id": "e2", "from": "o1", "to": "k2", "port": "image1"},
        ],
    }


def part_b() -> None:
    print("\n" + "=" * 62)
    print("[B] 接口：/api/format 与 /api/format/undo")
    print("=" * 62)
    try:
        st = req("GET", "/api/state")
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] 打不开 {BASE}（{e}）—— 跳过 B 部分")
        return

    orig_ws = st.get("workspace")
    cid = f"c_layout_{uuid.uuid4().hex[:6]}"      # 每次跑用新画布 id，免得受上次残留影响
    canvas = make_canvas(cid)
    TMPWS.mkdir(parents=True, exist_ok=True)
    (TMPWS / "graph.json").write_text(
        json.dumps({"version": 2, "current": cid, "canvases": [canvas]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    def pos_of(payload):
        return {n["id"]: (n["x"], n["y"])
                for n in (*payload["graph"]["resources"], *payload["graph"]["processes"])}

    try:
        st = req("POST", "/api/workspace", {"path": str(TMPWS)})
        check("切到一次性工作目录", st.get("workspace") == str(TMPWS))
        check("格式化前 canUndoFormat = false", st.get("canUndoFormat") is False,
              str(st.get("canUndoFormat")))
        before = pos_of(st)

        st2 = req("POST", "/api/format", {})
        after = pos_of(st2)
        print(f"   移动了 {st2.get('moved')} 个：{json.dumps(after, ensure_ascii=False)}")
        check("返回了移动数量", isinstance(st2.get("moved"), int) and st2["moved"] > 0,
              str(st2.get("moved")))
        check("所有节点都挪了位置", before != after)
        check("格式化后 canUndoFormat = true", st2.get("canUndoFormat") is True)

        # 用返回的 payload 自己验一遍几何不变量（服务端落盘后的真实结果）
        box = {n["id"]: (n["x"], n["y"], n["w"], n["h"])
               for n in (*st2["graph"]["resources"], *st2["graph"]["processes"])}
        check("落盘后的结果也不相交", not intersected(box), str(intersected(box)))
        check("两条边都从左指向右",
              box["i1"][0] + box["i1"][2] <= box["k1"][0] + 1e-9
              and box["o1"][0] + box["o1"][2] <= box["k2"][0] + 1e-9,
              f"k1.x={box['k1'][0]} k2.x={box['k2'][0]}")
        check("两个输出仍按序号上下排（C 在 D 上方）", box["o1"][1] < box["o2"][1],
              f"{box['o1'][1]} / {box['o2'][1]}")

        st3 = req("POST", "/api/format", {})
        check("再格式化一次是幂等的（移动 0 个）", st3.get("moved") == 0, str(st3.get("moved")))

        st4 = req("POST", "/api/format/undo", {})
        back = pos_of(st4)
        print(f"   撤销后：{json.dumps(back, ensure_ascii=False)}")
        check("撤销把坐标精确还原", back == before, f"{before} vs {back}")
        check("撤销后 canUndoFormat = false", st4.get("canUndoFormat") is False)
        try:
            req("POST", "/api/format/undo", {})
            check("再撤一次应当 400", False, "居然成功了")
        except urllib.error.HTTPError as e:
            check("再撤一次返回 400（只能用一次）", e.code == 400, str(e.code))
    finally:
        try:
            req("POST", "/api/workspace", {"path": orig_ws})
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(TMPWS, ignore_errors=True)
        print(f"   已恢复工作目录 {orig_ws}")


if __name__ == "__main__":
    for fn in (case_basic, case_branch, case_isolated, case_snap, case_translate,
               case_size_source, case_degenerate, case_chain_no_drift, case_undo_pure):
        fn()
    part_b()
    bad = [n for ok, n, _ in results if not ok]
    print("\n" + "=" * 62)
    print(f"共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 62)
    sys.exit(1 if bad else 0)
