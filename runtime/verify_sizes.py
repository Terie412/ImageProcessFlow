# -*- coding: utf-8 -*-
"""验证「节点尺寸只有一个事实来源」。

背景：尺寸原先在前端 `App.jsx`（`RESOURCE_SIZE` / `PROCESS_SIZE`）和服务端
`engine.py`（`PROCESS_DEFAULT_W` / `RESOURCE_DEFAULT_H`）各算一遍，必然漂移 ——
数据节点没被拖过尺寸时，前端按 220 渲染、服务端按 280 排输出槽位，
于是每两个输出节点之间空出约 70px 的假缝。现在唯一的口径在 `server/nodespec.py`，
前端只查 `/api/state` 下发的 `sizeSpec`。

A 部分：纯函数（不用起服务）—— 解析链、兜底、夹取、以及「服务端排槽位的口径」
        和「前端渲染的口径」是同一个数
B 部分：对着跑着的实例（默认 127.0.0.1:8770）—— `sizeSpec` 下发了、每个节点都带
        算好的 w/h、改设置立刻反映到尺寸上；跑完把设置改回去

    IPF_BASE=http://127.0.0.1:8791 python runtime/verify_sizes.py   :: 打别的实例
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import engine as engine_mod  # noqa: E402
import nodespec as ns  # noqa: E402
import registry as registry_mod  # noqa: E402

BASE = os.environ.get("IPF_BASE", "http://127.0.0.1:8770")

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def req(method: str, path: str, body=None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------- A. 纯函数
def part_a() -> None:
    print("=" * 62)
    print("[A] nodespec：解析链 / 兜底 / 夹取 / 与 engine 同口径")
    print("=" * 62)
    reg = registry_mod.load_manifests(ROOT / "scripts")
    clean = {}   # 没有任何用户设置的环境
    t = ns.resolve_types(reg, clean)

    for m in reg.values():
        check(f"每个已注册类型都有尺寸规格：{m['id']}", f"process:{m['id']}" in t)
    check("基准项 resource / process 都在", "resource" in t and "process" in t)

    r0 = t["resource"]
    print(f"   数据节点：{r0}")
    check("数据节点初始尺寸 = 200×200（**和屏幕上的渲染口径一致**）",
          (r0["w"], r0["h"]) == (200, 200), str((r0["w"], r0["h"])))
    check("数据节点最小尺寸 = 200×200", (r0["minW"], r0["minH"]) == (200, 200), str(r0))

    # 兜底常量与 DEFAULT_SETTINGS 必须同值 —— 不同值就是「服务端按一个高排版、
    # 前端按另一个高渲染」那类假缝的温床（§3.5.1 踩过）
    import main as main_mod  # noqa: PLC0415
    ds = main_mod.DEFAULT_SETTINGS
    for kind in ("resource", "process"):
        want = (ds["nodeSize"][kind]["w"], ds["nodeSize"][kind]["h"])
        check(f"SIZE_FALLBACK[{kind}] == DEFAULT_SETTINGS.nodeSize[{kind}]",
              (ns.SIZE_FALLBACK[kind]["w"], ns.SIZE_FALLBACK[kind]["h"]) == want,
              f"{ns.SIZE_FALLBACK[kind]} vs {want}")
    # 最小尺寸：所有类型统一 200×200，写死在 nodespec.MIN_SIZE —— 不再是设置项
    check("设置里没有 nodeMin（最小尺寸不可配）", "nodeMin" not in ds, str(sorted(ds)))
    check("MIN_SIZE = 200×200，且正好是 SIZE_RANGE 的下界（初始尺寸压不到下限以下）",
          (ns.MIN_SIZE["w"], ns.MIN_SIZE["h"]) == (200, 200) and ns.SIZE_RANGE[0] == 200,
          f"{ns.MIN_SIZE} / {ns.SIZE_RANGE}")

    # 默认值解析出来的那张表 —— 就是界面 ⚙ 里「节点尺寸」一栏会显示的东西。
    # 这些数是 2026-10-14 在界面上一行一行定下来、再固化进代码的，改它们要连着改这里。
    print("\n[默认值] DEFAULT_SETTINGS 解析出的尺寸表")
    t_def = ns.resolve_types(reg, json.loads(json.dumps(ds)))
    WANT_DEFAULT = {
        "resource":              (200, 200),
        "process:alpha":         (220, 200),
        "process:alpha_erase":   (200, 200),
        "process:color_balance": (200, 200),
        "process:crop":          (250, 200),
        "process:ai_image":      (560, 300),
        "process:recolor":       (200, 200),
        "process:region_erase":  (200, 200),
        "process:resize":        (200, 200),
        "process:square_pad":    (200, 200),
        # 兜底那一行：只给"没声明 size 又没有自己那一行"的类型用
        "process":               (600, 340),
    }
    for key, init in WANT_DEFAULT.items():
        got = t_def.get(key) or {}
        g = (int(got.get("w", 0)), int(got.get("h", 0)),
             int(got.get("minW", 0)), int(got.get("minH", 0)))
        check(f"{key}：初始 {init[0]}×{init[1]} / 最小恒 200×200",
              g[:2] == init and g[2:] == (200, 200), str(got))

    q = t["process:ai_image"]
    print(f"   AI 生图：{q}")
    check("处理节点初始尺寸取 manifest 的 size（560×300）",
          (q["w"], q["h"]) == (560, 300), str((q["w"], q["h"])))

    # 最终尺寸 = max(最小尺寸, 存的 w/h ?? 初始尺寸)
    print("\n[最终尺寸] max(最小尺寸, 存的 ?? 初始)")
    check("没存过 → 初始尺寸", ns.size_of({}, kind="resource", settings=clean) == (200, 200))
    check("存了 h=260（高于下限）→ 用 260",
          ns.size_of({"h": 260}, kind="resource", settings=clean) == (200, 260))
    check("存了 h=150 但下限 200 → 兜到 200",
          ns.size_of({"h": 150}, kind="resource", settings=clean) == (200, 200))
    check("存了 h=444 → 用 444", ns.size_of({"h": 444}, kind="resource", settings=clean) == (200, 444))

    # 设置里只剩 nodeSize 那一张表：最小尺寸已写死在 nodespec.MIN_SIZE，
    # 老 settings.json 里残留的 nodeMin 不再被读（下面故意塞一份进去试）
    print("\n[设置] 只有 nodeSize（初始尺寸）可配")
    st = {
        "nodeSize": {"resource": {"w": 333, "h": 444}, "process:crop": {"w": 777, "h": 888}},
        "nodeMin": {"resource": {"w": 900, "h": 900}, "process:crop": {"w": 900, "h": 900}},
    }
    check("nodeSize 覆盖数据节点初始尺寸",
          ns.size_of({}, kind="resource", settings=st) == (333, 444),
          str(ns.size_of({}, kind="resource", settings=st)))
    check("nodeSize 按具体类型覆盖处理节点",
          ns.size_of({"defId": "crop"}, kind="process", manifest=reg["crop"], settings=st)
          == (777, 888))
    check("残留的 nodeMin 被完全忽略（下限仍是 200，不是 900）",
          ns.spec_of("process:crop", "process", reg["crop"], st)["minW"] == 200,
          str(ns.spec_of("process:crop", "process", reg["crop"], st)))
    check("存的尺寸低于下限时被顶到 200（下限绕不过去）",
          ns.size_of({"defId": "crop", "w": 120, "h": 100}, kind="process",
                     manifest=reg["crop"], settings=st) == (200, 200),
          str(ns.size_of({"defId": "crop", "w": 120, "h": 100}, kind="process",
                         manifest=reg["crop"], settings=st)))
    # 「通用兜底」不能盖掉脚本自己声明的尺寸（实测踩过：Qwen 被盖成 600×340）
    print("\n[优先级] nodeSize[类型] > manifest.size > nodeSize.process > 常量")
    defaults_like = {
        "nodeSize": {"resource": {"w": 200, "h": 200}, "process": {"w": 600, "h": 340}},
    }
    check("默认设置里的 nodeSize.process 只是兜底，不盖 manifest",
          ns.size_of({"defId": "ai_image"}, kind="process", manifest=reg["ai_image"],
                     settings=defaults_like) == (560, 300),
          str(ns.size_of({"defId": "ai_image"}, kind="process",
                         manifest=reg["ai_image"], settings=defaults_like)))
    check("没声明 size 的脚本才轮到 nodeSize.process",
          ns.size_of({"defId": "third_party"}, kind="process", manifest={"id": "third_party"},
                     settings=defaults_like) == (600, 340))
    check("针对该类型的 nodeSize 设置优先级最高",
          ns.size_of({"defId": "ai_image"}, kind="process", manifest=reg["ai_image"],
                     settings={"nodeSize": {"process:ai_image": {"w": 800, "h": 500}}})
          == (800, 500), str(ns.size_of({"defId": "ai_image"}, kind="process",
                                        manifest=reg["ai_image"],
                                        settings={"nodeSize": {"process:ai_image":
                                                               {"w": 800, "h": 500}}})))
    check("未注册 / 没声明 size 的 defId → nodeSize.process → 常量",
          ns.size_of({"defId": "nope"}, kind="process", manifest=None, settings=st) == (600, 340),
          str(ns.size_of({"defId": "nope"}, kind="process", manifest=None, settings=st)))

    # 夹取：区间下界就是最小尺寸，所以"填多小"都压不到下限以下
    crazy = {"nodeSize": {"resource": {"w": 1, "h": 99999}}}
    sp = ns.spec_of("resource", "resource", None, crazy)
    print(f"\n[夹取] 初始尺寸 {sp['w']}×{sp['h']}（区间 {ns.SIZE_RANGE}）")
    check("初始尺寸被夹进区间（下界 200 = 最小尺寸）", (sp["w"], sp["h"]) == (200, 4000),
          str((sp["w"], sp["h"])))
    check("最终尺寸同样不低于下限", ns.size_of({}, kind="resource", settings=crazy) == (200, 4000),
          str(ns.size_of({}, kind="resource", settings=crazy)))

    # 最关键的一条：服务端排输出槽位的口径，必须就是前端渲染的口径
    print("\n[同口径] engine 排槽位用的高度 == 前端渲染用的高度")
    s = {}
    for r in ({}, {"h": 200}, {"h": 150}, {"h": 364}):
        got = engine_mod.resource_height(r, s)
        want = ns.size_of(r, kind="resource", settings=s)[1]
        check(f"resource_height({r}) == size_of().h", got == want, f"{got} vs {want}")
    check("没存过尺寸 → 200（不再是服务端那个 280）",
          engine_mod.resource_height({}, s) == 200, str(engine_mod.resource_height({}, s)))

    # 横向落点用的宽度也得同口径
    manifest = {"id": "ai_image", "size": {"defaultWidth": 600, "defaultHeight": 340}}
    proc = {"id": "p1", "defId": "ai_image", "x": 1000, "y": 200}
    bx, _ = engine_mod.slot_origin(proc, manifest, s)
    pw = ns.size_of(proc, kind="process", manifest=manifest, settings=s)[0]
    check("slot_origin 横向 = x + 宽度 + 64（宽度同口径）",
          bx == 1000 + pw + engine_mod.SLOT_GAP_X, f"{bx} / w={pw}")


# ------------------------------------------------------------------ B. 接口
# 自带一张合成画布：故意混进「存过尺寸」和「从没存过尺寸」的节点，
# 这样才测得出「改初始尺寸会不会波及拖过尺寸的节点」。
TMPWS = ROOT / "runtime" / "size_ws"
TMP_CANVAS = {
    "id": "c_size_test",
    "name": "尺寸测试",
    "viewport": None,
    "resources": [
        {"id": "r1", "name": "A", "kind": "input", "x": 0, "y": 0},                    # 没存过
        {"id": "r2", "name": "B", "kind": "input", "x": 0, "y": 300, "w": 200, "h": 260},
        {"id": "r3", "name": "C", "kind": "input", "x": 0, "y": 600, "w": 200, "h": 150},  # 低于下限
        {"id": "r4", "name": "D", "kind": "output", "producer": "p1", "index": 0,
         "x": 900, "y": 0},                                                            # 没存过
    ],
    "processes": [
        {"id": "p1", "defId": "ai_image", "x": 400, "y": 0, "ports": {"image": 1}, "params": {}},
        {"id": "p2", "defId": "crop", "x": 400, "y": 300, "params": {}, "w": 350, "h": 250},
        {"id": "p3", "defId": "resize", "x": 400, "y": 600, "ports": {"image": 1}, "params": {}},
    ],
    "edges": [
        {"id": "e1", "from": "r1", "to": "p1", "port": "image1"},
        {"id": "e2", "from": "r2", "to": "p2", "port": "image"},
        {"id": "e3", "from": "r3", "to": "p3", "port": "image1"},
    ],
}


def part_b() -> None:
    print("\n" + "=" * 62)
    print("[B] /api/state：sizeSpec 下发 + 每个节点都带算好的 w/h")
    print("=" * 62)
    try:
        st = req("GET", "/api/state")
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] 打不开 {BASE}（{e}）—— 跳过 B 部分")
        return

    spec = st.get("sizeSpec")
    check("下发了 sizeSpec", isinstance(spec, dict) and "resource" in spec and "process" in spec)
    check("sizeRange = [200, 4000]", st.get("sizeRange") == [200, 4000], str(st.get("sizeRange")))
    for k, m in (st.get("manifests") or {}).items():
        check(f"sizeSpec 覆盖 manifest：{k}", f"process:{k}" in (spec or {}))

    # 线上 settings 里的 nodeSize.process 是 600×340；声明了 size 的 manifest 必须仍以自己为准
    ns_set = (st.get("settings") or {}).get("nodeSize") or {}
    if ns_set.get("process"):
        print(f"   settings.nodeSize.process = {ns_set['process']}（通用兜底）")
        for k, m in (st.get("manifests") or {}).items():
            if (m.get("size") or {}).get("defaultWidth"):
                got = spec[f"process:{k}"]["w"]
                check(f"manifest 声明的宽度没被通用兜底盖掉：{k}",
                      got == m["size"]["defaultWidth"], f"{got} vs {m['size']['defaultWidth']}")

    # 切换到一次性工作目录跑，跑完恢复（和 verify_slots 一个套路）
    orig_ws = st.get("workspace")
    # 下面的期望值按**代码里的默认值**算。但初始尺寸是用户级设置
    # （settings.json 的 nodeSize），机器上那份可能被改过，
    # 于是这个测试会跟着别人机器上的设置变红。所以把用到的这几项都钉住，
    # 跑完再**原样**还回去（原本没有的那几项要删掉，见下面的 finally）。
    #
    # 最小尺寸不用钉 —— 它写死在代码里（`nodespec.MIN_SIZE`），谁都改不动。
    orig_size = dict((st.get("settings") or {}).get("nodeSize") or {})
    TMPWS.mkdir(parents=True, exist_ok=True)
    (TMPWS / "graph.json").write_text(
        json.dumps({"version": 2, "current": TMP_CANVAS["id"], "canvases": [TMP_CANVAS]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        st = req("POST", "/api/workspace", {"path": str(TMPWS)})
        check("切到一次性工作目录", st.get("workspace") == str(TMPWS), str(st.get("workspace")))

        spec = st["sizeSpec"]
        got = {r["id"]: (r["w"], r["h"]) for r in st["graph"]["resources"]}
        got.update({p["id"]: (p["w"], p["h"]) for p in st["graph"]["processes"]})
        print(f"   服务端算出的尺寸：{json.dumps(got, ensure_ascii=False)}")
        want = {
            "r1": (200, 200),   # 没存过 → 初始尺寸（**就是屏幕上的高度**）
            "r2": (200, 260),   # 存过，且高于下限 → 用存的
            "r3": (200, 200),   # 存了 150，被下限 200 顶上去
            "r4": (200, 200),
            "p1": (560, 300),   # manifest.size
            "p2": (350, 250),   # 存的 250 高于 crop 的下限 200 → 用存的
            "p3": (200, 200),   # manifest.size
        }
        for nid, w in want.items():
            check(f"{nid} = {w[0]}×{w[1]}", got.get(nid) == w, str(got.get(nid)))

        # 只算不改：payload 里补的 w/h 不能写回 graph.json
        raw = json.loads((TMPWS / "graph.json").read_text(encoding="utf-8"))
        cam = raw["canvases"][0]
        dirty = [x["id"] for x in cam["resources"] + cam["processes"] if "w" in x or "h" in x]
        check("算出来的尺寸不落盘（graph.json 里仍只有用户存过的）",
              sorted(dirty) == ["p2", "r2", "r3"], str(sorted(dirty)))

        # 改「初始尺寸」：没存过的跟着变，存过的不动
        size_pin = dict(orig_size.get("resource") or {"w": 200, "h": 200})
        try:
            after = req("PATCH", "/api/settings",
                        {"nodeSize": {"resource": {"w": size_pin["w"], "h": 300}}})
            g2 = {r["id"]: (r["w"], r["h"]) for r in after["graph"]["resources"]}
            print(f"   初始高度 {size_pin['h']} → 300 之后：{json.dumps(g2, ensure_ascii=False)}")
            check("改 nodeSize 后 sizeSpec 跟着变", after["sizeSpec"]["resource"]["h"] == 300)
            check("没存过尺寸的节点跟着变（r1 → 300）", g2["r1"][1] == 300, str(g2["r1"]))
            check("存过尺寸的节点纹丝不动（r2 仍是 260）", g2["r2"][1] == 260, str(g2["r2"]))
        finally:
            req("PATCH", "/api/settings", {"nodeSize": {"resource": size_pin}})
            check("初始尺寸已改回", True)

        # 下限不给配了：PATCH nodeMin 应当被整项忽略（它是代码里的常量，不是用户设置）
        after = req("PATCH", "/api/settings", {"nodeMin": {"resource": {"w": 900, "h": 900}}})
        check("PATCH nodeMin 被忽略（settings 里不会多出这一项）",
              "nodeMin" not in (after.get("settings") or {}),
              str(sorted((after.get("settings") or {}).keys())))
        check("下限仍然是 200，没被 900 顶掉", after["sizeSpec"]["resource"]["minW"] == 200,
              str(after["sizeSpec"]["resource"]))
    finally:
        try:
            req("POST", "/api/workspace", {"path": orig_ws})
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(TMPWS, ignore_errors=True)
        print(f"   已恢复工作目录 {orig_ws}")

    # 前端构建产物里不该再有第二份尺寸常量。这几个探针是尺寸表里最有辨识度的组合：
    # 一旦有人在 App.jsx 里重新写死一份 `RESOURCE_SIZE` / `PROCESS_SIZE`，这里就会红
    js = sorted((ROOT / "server" / "static" / "assets").glob("index-*.js"))
    if js:
        text = js[-1].read_text(encoding="utf-8", errors="replace")
        probes = ["w:200,h:200", "w:600,h:340", "minW:200,minH:200"]
        hit = [p for p in probes if p in text]
        check("前端构建产物里没有硬编码的节点尺寸常量", not hit, f"{js[-1].name} 命中 {hit}")
    else:
        print("  [SKIP] server/static 还没有构建产物")


if __name__ == "__main__":
    part_a()
    part_b()
    bad = [n for ok, n, _ in results if not ok]
    print("\n" + "=" * 62)
    print(f"共 {len(results)} 项，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 62)
    sys.exit(1 if bad else 0)
