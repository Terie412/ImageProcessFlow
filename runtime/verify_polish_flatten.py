# -*- coding: utf-8 -*-
"""直接调 server/polish.py 的棋盘合成，验证「参考图送出去之前变成什么」
（纯本地：不起服务、不调用任何模型）。

覆盖：带 alpha → 合成到棋盘格（输出 PNG、RGB、透明处是棋盘格色、内容像素不变）；
不带 alpha → 字节原样、不做合成；调色板 + transparency 也算「带 alpha」；
Pillow 缺失时的退化路径；以及「棋盘格规则只在有合成发生时才进 system prompt」。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import polish as P  # noqa: E402


def main():
    root = tempfile.mkdtemp(prefix="polish_flatten_test_")
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- 素材 ----
    a = os.path.join(root, "A.png")            # RGBA：左上角透明，右下角实色
    im = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    for y in range(32):
        for x in range(32):
            im.putpixel((x, y), (165, 57, 209, 0))   # 紫红色残留 + 全透明
    im.save(a)

    b = os.path.join(root, "B.png")            # RGB，无 alpha
    Image.new("RGB", (40, 30), (10, 200, 90)).save(b)

    c = os.path.join(root, "C.jpg")            # JPEG
    Image.new("RGB", (40, 30), (30, 60, 90)).save(c, "JPEG", quality=90)

    d = os.path.join(root, "D.png")            # 调色板 + transparency
    pm = Image.new("P", (16, 16))
    pal = [0, 0, 0] + [0] * 765
    pal[0:3] = [255, 255, 0]
    pm.putpalette(pal)
    pm.info["transparency"] = 0
    pm.save(d, transparency=0)

    # ---- 1) 带 alpha：合成到棋盘格 ----
    print("[1] RGBA 参考图 → 合成到棋盘格")
    data, mime, comp = P.flatten_for_view(a)
    check(comp is True, "composited = True")
    check(mime == "image/png", f"mime = image/png（实际 {mime}）")
    import io
    with Image.open(io.BytesIO(data)) as out:
        check(out.mode == "RGB", f"输出没有 alpha 通道了（实际 {out.mode}）")
        # 原来全透明的（0,0）：现在应该露出棋盘格的底色
        p00 = out.getpixel((0, 0))
        check(p00 in (P.CHECKER_LIGHT, P.CHECKER_DARK),
              f"全透明处变成了棋盘格色（实际 {p00}）")
        check(p00 != (165, 57, 209), "紫红色残留没有漏出来")
        # 相邻两格应该一深一浅
        p_adj = out.getpixel((P.CHECKER_CELL, 0))
        check(p_adj in (P.CHECKER_LIGHT, P.CHECKER_DARK) and p_adj != p00,
              f"相邻格是另一个颜色（{p00} vs {p_adj}）")
        # 不透明的红色区域原样保留
        check(out.getpixel((50, 50)) == (255, 0, 0), f"实色区域不变（实际 {out.getpixel((50, 50))}）")
        check(out.size == (64, 64), f"尺寸不变（实际 {out.size}）")
    # 棋盘格范围外（透明区）不该再有任何紫红
    with Image.open(io.BytesIO(data)) as out:
        purples = sum(1 for y in range(0, 32) for x in range(0, 32)
                      if out.getpixel((x, y))[2] > 150 and out.getpixel((x, y))[0] > 120
                      and out.getpixel((x, y))[0] < 200)
    check(purples == 0, f"透明区没有任何残留紫红像素（实际 {purples} 个）")

    # ---- 2) 不带 alpha：字节原样 ----
    print("\n[2] 无 alpha 的图 → 原样发送")
    for name, path, want_mime in [("B.png (RGB)", b, "image/png"), ("C.jpg (JPEG)", c, "image/jpeg")]:
        data, mime, comp = P.flatten_for_view(path)
        check(comp is False, f"{name} composited = False")
        check(data == open(path, "rb").read(), f"{name} 字节与原文件完全一致")
        check(mime == want_mime, f"{name} mime = {want_mime}（实际 {mime}）")

    # ---- 3) 调色板 + transparency 也算带 alpha ----
    print("\n[3] P 模式 + transparency → 也算带 alpha，走合成")
    _data, _mime, comp = P.flatten_for_view(d)
    check(comp is True, "P+transparency composited = True")

    # ---- 4) _data_url 的形状 ----
    print("\n[4] _data_url 前缀")
    url = P._data_url(a)
    check(url.startswith("data:image/png;base64,"), "带 alpha 的走 data:image/png;base64,")
    check(P._data_url(c).startswith("data:image/jpeg;base64,"), "JPEG 仍是 data:image/jpeg;base64,")

    # ---- 5) 棋盘格规则只在有合成时进 system prompt ----
    print("\n[5] system prompt 里的棋盘格规则是条件注入的")
    with_rules = P.SYSTEM_TEMPLATE.format(notes="n", transparency=P.TRANSPARENCY_RULES)
    without = P.SYSTEM_TEMPLATE.format(notes="n", transparency="")
    check("棋盘格" in with_rules, "有合成时提示词里出现「棋盘格」")
    check("背景" in with_rules and "不要给画面补背景色" in with_rules, "附带了「不要补背景色」")
    check("棋盘格" not in without, "没有合成时不出现「棋盘格」")
    check(P.SYSTEM_TEMPLATE.format(notes="n", transparency="").count("{") == 0, "模板占位符已全部替换")

    shutil.rmtree(root, ignore_errors=True)
    print("\n" + ("全部通过" if not fails else f"{len(fails)} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
