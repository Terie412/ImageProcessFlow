# -*- coding: utf-8 -*-
"""验证「尺寸 = 自动」的就近解析。

背景：Qwen 节点的「尺寸」下拉默认是「自动」（空串）。原先空串 = **不把 size 发给
DashScope**，于是输出分辨率完全由模型自己定 —— 输入 720×1280 实测出来是 1536×2720
（比例对得上，但面积被放大到 4.5 倍），而且用户要等生成完才知道结果。

现在空串的语义改成「**按最后一张参考图，在下拉选项里就近挑一个**」，
并且把这个落点在**生成之前**显示在节点上（`/api/state` 的 `autoSize`）。

A 部分：autosize 的纯函数 —— 解析 / 归一化 / 就近匹配 / 平手用面积兜底
B 部分：服务端接线（不起服务，直接调函数）——
        · `/api/state` 的 `autoSize` 就绪（有参考图 / 显式选了 / 没参考图 三种）
        · 执行时真的把命中值发给脚本，且**不写回画布**（params 仍是空串）
        · 界面显示的值 == 执行时发出去的值（两处口径必须一致）

    runtime/venv/Scripts/python.exe runtime/verify_autosize.py
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import autosize as A  # noqa: E402
import engine as engine_mod  # noqa: E402
import main as main_mod  # noqa: E402
import registry as registry_mod  # noqa: E402
import workspace as workspace_mod  # noqa: E402

TMP = ROOT / "runtime" / "_autosize_tmp"
# stub 脚本放 TMP **外面** —— TMP 每次建工作目录都会被整棵删掉重建
STUB_DIR = ROOT / "runtime" / "_autosize_stub"

OPTS = ["720x1280", "1024x1024", "1280x720", "1024x1536", "1536x1024", "1536x1536"]

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# ---------------------------------------------------------------- A. 纯函数
def part_a() -> None:
    print("=" * 62)
    print("[A] autosize：解析 / 归一化 / 就近匹配")
    print("=" * 62)

    # ---- 解析 ----
    check("720x1280 → (720,1280)", A.parse_size("720x1280") == (720, 1280))
    check("720*1280 / 720×1280 都吃",
          A.parse_size("720*1280") == (720, 1280) and A.parse_size("720×1280") == (720, 1280))
    check("带空格也吃", A.parse_size(" 720 x 1280 ") == (720, 1280))
    check("空串 / 「自动」→ None",
          A.parse_size("") is None and A.parse_size("自动") is None)
    check("残缺 / 非数字 / 非正数 → None",
          all(A.parse_size(x) is None for x in ("720", "abc", "0x100", "720x-1", None, 720)))

    # ---- 就近匹配：期望值逐个写死，改算法就得改这张表 ----
    cases = [
        ((720, 1280), "720x1280", "输入本身就在选项里 → 原样命中"),
        ((1024, 1024), "1024x1024", "正方形打平 → 用面积兜底，选面积最近的"),
        ((1536, 1536), "1536x1536", "同上，反过来"),
        ((941, 1672), "720x1280", "9:16 竖图 → 命中 720x1280（比例差 0.0003）"),
        ((1920, 1080), "1280x720", "16:9 → 命中 1280x720"),
        ((512, 1024), "720x1280", "1:2 → 比 1024x1536(0.667) 更接近 0.5625"),
        ((2000, 1000), "1280x720", "2:1 → 形状优先，选 16:9 而不是更大的 1536x1024"),
        ((800, 1200), "1024x1536", "2:3 → 命中 1024x1536"),
    ]
    for dims, want, why in cases:
        got = A.nearest_size(dims[0], dims[1], OPTS)
        check(f"{dims[0]}×{dims[1]} → {want}", got == want, f"{why}；实际 {got}")

    # ---- 为什么必须归一化：拿「不归一化」的实现在同一个输入上对照 ----
    def raw_nearest(w, h, options):
        best, bd = None, None
        for v in options:
            vw, vh = A.parse_size(v)
            d = math.hypot(w - vw, h - vh)
            if bd is None or d < bd:
                best, bd = v, d
        return best

    raw = raw_nearest(941, 1672, OPTS)
    check("不归一化会选错（把这条当反例钉住）",
          raw == "1024x1536" and A.nearest_size(941, 1672, OPTS) == "720x1280",
          f"裸距离选 {raw}，归一化后选 720x1280")

    # ---- 选项列表 = manifest 里能解析成宽高的那些 ----
    manifest = json.loads((ROOT / "scripts" / "qwen_image" / "manifest.json").read_text("utf-8"))
    check("manifest 里 size 参数声明了 autoFrom=input",
          A.auto_key(manifest) == "size", f"auto_key={A.auto_key(manifest)!r}")
    check("选项里的空值「自动」被排除",
          A.options_of(manifest, "size") == OPTS, str(A.options_of(manifest, "size")))
    check("没声明 autoFrom 的 manifest 不参与（裁剪节点）",
          A.auto_key(registry_mod.load_manifests(ROOT / "scripts").get("crop") or {}) is None)

    # ---- resolve 的四条分支 ----
    p_noauto = {"key": "size", "type": "enum", "options": [{"value": "1x1"}]}
    v, i = A.resolve({"params": [p_noauto]}, {"size": ""}, (720, 1280))
    check("没声明 autoFrom → 不介入", v is None and i is None)

    v, i = A.resolve(manifest, {"size": "1024x1024"}, (720, 1280))
    check("用户显式选了 → 原值返回且不提示", v == "1024x1024" and i is None)

    v, i = A.resolve(manifest, {"size": ""}, None)
    check("自动 + 无参考图 → 不发 size，标 model",
          v is None and i and i["source"] == "model", str(i))

    v, i = A.resolve(manifest, {"size": ""}, (720, 1280))
    check("自动 + 有参考图 → 命中值 + 带参考图尺寸",
          v == "720x1280" and i and i["source"] == "input"
          and (i["refWidth"], i["refHeight"]) == (720, 1280), str(i))

    # ---- pick()：界面用，**与用户当前选了什么无关** ----
    v1, i1 = A.pick(manifest, (720, 1280))
    v2, i2 = A.pick(manifest, (720, 1280))
    check("pick() 不看 params（显式选了也照样算）",
          v1 == "720x1280" and i1 and i1["source"] == "input",
          f"pick={v1}")
    check("pick() 是纯函数（同一输入同一结果）", (v1, i1) == (v2, i2))
    check("pick()：没 autoFrom → None",
          A.pick({"params": [p_noauto]}, (720, 1280)) == (None, None))


# ---------------------------------------------------------------- B. 接线
STUB_PY = '''
import json, os, shutil, sys
p = json.loads(sys.stdin.read())
d = p["output_dir"]
os.makedirs(d, exist_ok=True)
# 把收到的 params 落在脚本目录（output_dir 会被引擎回收）
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "_last_params.json"), "w", encoding="utf-8") as f:
    json.dump(p["params"], f, ensure_ascii=False)
src = p["inputs"].get("image1")
if src:
    shutil.copyfile(src, os.path.join(d, "0.png"))
print("stub ok, size =", repr(p["params"].get("size")))
'''


def _stub_manifest() -> dict:
    d = STUB_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "stub.py").write_text(STUB_PY, encoding="utf-8")
    return {
        "id": "stub_size",
        "name": "stub",
        "entry": "stub.py",
        "inputs": [{"key": "image", "type": "image", "dynamic": True, "label": "参考图",
                    "count": 1, "min": 0, "max": 10}],
        "params": [
            {"key": "prompt", "type": "text", "default": "", "primary": True},
            {"key": "size", "type": "enum", "default": "", "label": "尺寸", "autoFrom": "input",
             "options": [{"value": "", "label": "自动"}]
                        + [{"value": v, "label": v} for v in OPTS]},
        ],
        "outputs": [{"key": "image", "type": "image", "multi": True}],
        "_dir": str(d),
    }


def _last_params() -> dict:
    return json.loads((STUB_DIR / "_last_params.json").read_text("utf-8"))


def _make_ws(px: tuple[int, int] | None):
    shutil.rmtree(TMP, ignore_errors=True)
    ws = workspace_mod.Workspace(TMP)
    ws.ensure()
    doc = ws.load_doc()
    cid = doc["current"]
    view = ws.view(cid)
    view.ensure()
    if px:
        from PIL import Image
        Image.new("RGB", px, (12, 12, 12)).save(view.image_dir(create=True) / "A.png")
    return ws, cid, view


def _graph(size_value: str, px: tuple[int, int] | None) -> dict:
    graph = {
        "resources": (
            [{"id": "r1", "name": "A", "kind": "input", "x": 0, "y": 0}] if px else []
        ),
        "processes": [{
            "id": "p1", "defId": "stub_size", "x": 600, "y": 0,
            "params": {"prompt": "x", "size": size_value},
        }],
        "edges": (
            [{"id": "e1", "from": "r1", "to": "p1", "port": "image1"}] if px else []
        ),
    }
    return graph


def part_b() -> None:
    print()
    print("=" * 62)
    print("[B] 服务端接线：/api/state 提示 + 执行时下发")
    print("=" * 62)

    real_reg = registry_mod.load_manifests(ROOT / "scripts")
    stub = _stub_manifest()
    reg = dict(real_reg)
    reg[stub["id"]] = stub
    main_mod.STATE["registry"] = reg
    main_mod.STATE["settings"] = {}

    # ---- B1：有参考图 720×1280，「自动」 → 提示 720×1280 ----
    ws, cid, view = _make_ws((720, 1280))
    main_mod.STATE["workspace"] = ws
    out = main_mod._decorate(_graph("", (720, 1280)), cid)
    p = out["processes"][0]
    info = p.get("autoSize") or {}
    check("state：自动 + 720×1280 参考图 → autoSize=720x1280",
          info.get("value") == "720x1280" and info.get("source") == "input", str(info))
    check("state：提示里带上是哪张参考图",
          info.get("from") == "参考图1" and info.get("image") == "A", str(info))
    check("state：提示里的参考图尺寸正确",
          (info.get("refWidth"), info.get("refHeight")) == (720, 1280))
    check("state：数据节点的像素分辨率仍然下发了",
          out["resources"][0]["width"] == 720 and out["resources"][0]["height"] == 1280)

    # ---- B2：执行时真的把 720x1280 发出去，且不写回画布 ----
    graph = _graph("", (720, 1280))
    st = engine_mod.RunState("test", "p1")
    ok, err = engine_mod.execute(view, graph, reg, "p1", st, settings={})
    check("执行成功（stub 脚本）", ok, str(err))
    check("执行：脚本收到的 size == 界面显示的 720x1280",
          _last_params().get("size") == info.get("value"),
          f"收到 {_last_params().get('size')!r}")
    check("执行：画布里的 params.size 仍是空串（只算不写）",
          graph["processes"][0]["params"]["size"] == "")
    check("执行日志里有尺寸落点",
          any("自动 → 720×1280" in ln for ln in st.lines),
          next((ln for ln in st.lines if "自动" in ln), "（没有）"))
    out_png = sorted(view.image_dir().glob("*.png"))
    check("产出 720×1280 的图（等于输入分辨率）", bool(out_png))

    # ---- B3：显式选了 → 执行按用户的值发；但提示照样下发（显不显示归前端） ----
    ws, cid, view = _make_ws((720, 1280))
    main_mod.STATE["workspace"] = ws
    out = main_mod._decorate(_graph("1024x1024", (720, 1280)), cid)
    check("state：显式选了 → autoSize 仍然下发（前端据此保留/隐藏提示）",
          (out["processes"][0].get("autoSize") or {}).get("value") == "720x1280",
          str(out["processes"][0].get("autoSize")))
    graph = _graph("1024x1024", (720, 1280))
    st2 = engine_mod.RunState("t2", "p1")
    ok, err = engine_mod.execute(view, graph, reg, "p1", st2, settings={})
    check("执行：显式值原样下发", ok and _last_params().get("size") == "1024x1024",
          f"{err or ''} {_last_params().get('size')!r}")
    check("执行：显式选了就不打「自动 →」的日志",
          not any("自动 →" in ln for ln in st2.lines),
          next((ln for ln in st2.lines if "尺寸" in ln), "（没有尺寸日志，正确）"))

    # ---- B4：没有参考图 → 不发 size，交回模型 ----
    ws, cid, view = _make_ws(None)
    main_mod.STATE["workspace"] = ws
    out = main_mod._decorate(_graph("", None), cid)
    info = out["processes"][0].get("autoSize") or {}
    check("state：无参考图 → 标 model、不给 value",
          info.get("source") == "model" and info.get("value") is None, str(info))

    # ---- B5：改了参考图，提示跟着变（提示是现算的，不是缓存） ----
    ws, cid, view = _make_ws((1920, 1080))
    main_mod.STATE["workspace"] = ws
    out = main_mod._decorate(_graph("", (1920, 1080)), cid)
    check("state：换成 1920×1080 → 提示跟着变成 1280x720",
          (out["processes"][0].get("autoSize") or {}).get("value") == "1280x720")

    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(STUB_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        part_a()
        part_b()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        shutil.rmtree(STUB_DIR, ignore_errors=True)
    bad = [n for ok, n, _ in results if not ok]
    print("\n" + "=" * 62)
    print(f"共 {len(results)} 项，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 62)
    sys.exit(1 if bad else 0)
