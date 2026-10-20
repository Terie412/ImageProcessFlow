# -*- coding: utf-8 -*-
"""直接跑 scripts/mask/mask.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：默认参数 / 矩形与椭圆的**几何位置**（半轴、居中、边界落在哪一像素）/
两组半径互不串（切形状各用各的）/ 椭圆两条半轴各自生效（扁椭圆）/
半径超出画布被裁 / 半径为 0 全透明 / 空串兜底 / 形状写错回退 /
缺 output_dir 非 0 退出 / 半径夹到上限 /
**全图 RGB 一律是白**（透明区也是白的，下游采样不会压出黑边）/
**预览纯函数与执行产出逐像素一致** / manifest 的形状对得上。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_mask.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "scripts", "mask")
SCRIPT = os.path.join(DIR, "mask.py")
MANIFEST = os.path.join(DIR, "manifest.json")
PY = sys.executable

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run(params, out_dir):
    payload = {"params": params, "inputs": {}, "output_dir": out_dir}
    p = subprocess.run(
        [PY, SCRIPT],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=out_dir,
    )
    return p.returncode, p.stdout, p.stderr


def load_mod(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    root = tempfile.mkdtemp(prefix="mask_test_")
    mod = load_mod("mask_under_test")

    def out_dir(n):
        d = os.path.join(root, n)
        os.makedirs(d)
        return d

    def shot(d, idx=0):
        return Image.open(os.path.join(d, f"{idx}.png")).convert("RGBA")

    # ---- 1) 默认参数：512×512、白、居中 384×384 的方块 ----
    print("[1] 默认参数 → 512×512 RGBA，形状内白、外面透明")
    d = out_dir("default")
    rc, so, se = run({}, d)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    im = shot(d)
    check(im.size == (512, 512) and im.mode == "RGBA", f"尺寸 {im.size} / {im.mode}")
    check(os.path.getsize(os.path.join(d, "0.png")) > 0, "0.png 有内容")
    check(im.getpixel((256, 256)) == (255, 255, 255, 255), "画布中心是纯白不透明")
    check(im.getpixel((2, 2))[3] == 0, "左上角透明")
    check(im.getpixel((2, 2))[:3] == (255, 255, 255), "透明区的 RGB 也是白（不是黑）")

    # ---- 2) 矩形边界落在哪一像素：半径 192 → 64..447 ----
    print("[2] 矩形：半径 192 → 左右边界正好在 x=64 / x=447")
    check(im.getpixel((64, 256))[3] == 255, "x=64 不透明（形状里）")
    check(im.getpixel((63, 256))[3] == 0, "x=63 透明（形状外）")
    check(im.getpixel((447, 256))[3] == 255, "x=447 不透明")
    check(im.getpixel((448, 256))[3] == 0, "x=448 透明")
    check(im.getpixel((256, 64))[3] == 255 and im.getpixel((256, 63))[3] == 0, "上下边界同样准")
    # 数一下不透明的列数：应该是 384（2×192），不多不少
    alpha = im.getchannel("A")
    row = [alpha.getpixel((x, 256)) for x in range(512)]
    opaque_w = sum(1 for v in row if v == 255)
    check(opaque_w == 384, f"这一行不透明宽度 = 384（实测 {opaque_w}）")
    col = [alpha.getpixel((256, y)) for y in range(512)]
    opaque_h = sum(1 for v in col if v == 255)
    check(opaque_h == 384, f"这一列不透明高度 = 384（实测 {opaque_h}）")

    # ---- 3) 椭圆：同样半径，四角必须是空的 ----
    print("[3] 椭圆形：同样的 192×192，四角是空的（这是和矩形的唯一区别）")
    d = out_dir("ellipse")
    rc, so, se = run({"shape": "ellipse", "ellipseRX": 192, "ellipseRY": 192}, d)
    print(so.rstrip())
    e = shot(d)
    check(rc == 0, "退出码 0")
    check(e.getpixel((256, 256))[3] == 255, "中心不透明")
    check(e.getpixel((64, 64))[3] == 0, "左上角透明（矩形在这里是不透明的）")
    check(e.getpixel((447, 447))[3] == 0, "右下角透明")
    check(e.getpixel((447, 256))[3] == 255, "最右边（y 居中）不透明 —— 半轴确实到 192")
    check(e.getpixel((256, 447))[3] == 255, "最下边（x 居中）不透明")
    check(e.getpixel((448, 256))[3] == 0, "再往右一像素就没了")

    # ---- 4) 椭圆两条半轴各自生效（扁椭圆）----
    print("[4] 扁椭圆：横 192 / 竖 32 → 只在中线附近有面积")
    d = out_dir("flat")
    rc, so, se = run({"shape": "ellipse", "ellipseRX": 192, "ellipseRY": 32}, d)
    print(so.rstrip())
    f = shot(d)
    check(f.getpixel((256, 256 + 31))[3] == 255, "中线下方 31px 还实心")
    check(f.getpixel((256, 256 + 33))[3] == 0, "向下 33px 已经空了")
    check(f.getpixel((256 + 191, 256))[3] == 255, "横轴方向仍到 191")
    check(f.getpixel((256, 256 - 60))[3] == 0, "纵向被半轴夹住（不是矩形）")

    # ---- 5) 两组半径互不串 ----
    print("[5] 两组半径各存各的：切形状只用当前那一组")
    g = mod.resolve({"shape": "rect", "rectRX": 100, "rectRY": 40, "ellipseRX": 200, "ellipseRY": 20})
    check((g["rx"], g["ry"]) == (100, 40), f"矩形用 rectRX/rectRY（{g['rx']},{g['ry']}）")
    g = mod.resolve({"shape": "ellipse", "rectRX": 100, "rectRY": 40, "ellipseRX": 200, "ellipseRY": 20})
    check((g["rx"], g["ry"]) == (200, 20), f"椭圆用 ellipseRX/ellipseRY（{g['rx']},{g['ry']}）")

    # ---- 6) 半径超出画布：被裁，不报错 ----
    print("[6] 半径 1000 > 画布一半 → 铺满整张")
    d = out_dir("oversize")
    rc, so, se = run({"rectRX": 1000, "rectRY": 1000}, d)
    print(so.rstrip())
    o = shot(d)
    check(rc == 0, "退出码 0（不报错）")
    a = o.getchannel("A")
    check(min(a.getdata()) == 255, "整张全不透明")
    check("超出部分被裁" in so, "日志里点明了被裁")

    # ---- 7) 半径为 0 → 全透明 ----
    print("[7] 半径 0 → 整张全透明（不是崩溃）")
    d = out_dir("zero")
    rc, so, se = run({"rectRX": 0, "rectRY": 192}, d)
    print(so.rstrip())
    z = shot(d)
    check(rc == 0, "退出码 0")
    check(max(z.getchannel("A").getdata()) == 0, "全透明")
    check("半径是 0" in so, "日志里给了提示")

    # ---- 8) 空串 / 乱填 / 形状写错 ----
    print("[8] 空串兜底、形状写错回退矩形")
    g = mod.resolve({"width": "", "height": "", "rectRX": "", "rectRY": ""})
    check((g["w"], g["h"], g["rx"], g["ry"]) == (512, 512, 192, 192), f"空串全走默认 {g}")
    g = mod.resolve({"shape": "triangle"})
    check(g["shape"] == "rect", "未知形状回退成矩形")
    g = mod.resolve({"rectRX": 99999})
    check(g["rx"] == 4096, f"半径被夹到上限 4096（实测 {g['rx']}）")

    # ---- 9) 协议分支 ----
    print("[9] 缺 output_dir / stdin 空 → 非 0 退出")
    p = subprocess.run([PY, SCRIPT], input='{"params": {}}', capture_output=True, text=True, encoding="utf-8")
    check(p.returncode != 0 and "output_dir" in p.stderr, "缺 output_dir 报错退出")
    p = subprocess.run([PY, SCRIPT], input="  ", capture_output=True, text=True, encoding="utf-8")
    check(p.returncode != 0, "空 stdin 非 0 退出")

    # ---- 10) 预览纯函数 == 执行产出（逐像素）----
    print("[10] render() 与执行产出的 0.png 逐像素一致")
    params = {"width": 300, "height": 200, "shape": "ellipse", "ellipseRX": 120, "ellipseRY": 60}
    d = out_dir("preview")
    rc, so, se = run(params, d)
    built = shot(d)
    pure = mod.render(params)
    check(pure.size == built.size and pure.mode == built.mode, f"尺寸/模式一致 {pure.size} {pure.mode}")
    diff = ImageChops.difference(pure, built)
    check(diff.getbbox() is None, "逐像素一致")

    # ---- 11) manifest ----
    print("[11] manifest 与脚本对得上")
    mf = json.load(open(MANIFEST, encoding="utf-8"))
    check(mf["id"] == "mask" and mf["name"] == "蒙版制作", f"id/名字 {mf['id']} / {mf['name']}")
    check(mf.get("inputs") == [], "没有输入口")
    keys = [p["key"] for p in mf["params"]]
    check(keys == ["width", "height", "shape", "rectRX", "rectRY", "ellipseRX", "ellipseRY"], f"参数 {keys}")
    enum = next(p for p in mf["params"] if p["key"] == "shape")
    check([o["value"] for o in enum["options"]] == ["rect", "ellipse"], "形状两个选项")
    check(hasattr(mod, mf["preview"]["func"]), "preview.func 在脚本里真的存在")
    check(mf["outputs"][0]["multi"] is False, "单输出")
    for p in mf["params"]:
        if p["type"] == "number":
            check(p["min"] <= p["default"] <= p["max"], f"{p['key']} 默认值在区间内")

    print("=" * 66)
    ok = 0 if fails else 1
    print("全部通过" if ok else f"{len(fails)} 项失败")
    for m in fails:
        print("  FAIL " + m)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
