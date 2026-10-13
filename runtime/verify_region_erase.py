# -*- coding: utf-8 -*-
"""直接跑 scripts/region_erase/region_erase.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：带 Alpha → 抹成 (0,0,0,0) 且输出 PNG；不带 Alpha → 抹成 (0,0,0) 且跟随原格式；
区域为空 / 全部越界 → 原样复制；部分越界 → 只抹交集；未擦到的像素保持不变；
错误分支（没连输入图）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "region_erase", "region_erase.py")
PY = sys.executable


def run(inputs, params, out_dir):
    payload = {"params": params, "inputs": inputs, "output_dir": out_dir}
    p = subprocess.run(
        [PY, SCRIPT],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=out_dir,
    )
    return p.returncode, p.stdout, p.stderr


def near(v, target, tol=2):
    return all(abs(a - b) <= tol for a, b in zip(v, target))


def main():
    root = tempfile.mkdtemp(prefix="region_erase_test_")
    src, out = os.path.join(root, "src"), os.path.join(root, "out")
    os.makedirs(src), os.makedirs(out)
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # 素材
    a = os.path.join(src, "A.png")                       # 带 Alpha
    Image.new("RGBA", (200, 120), (255, 0, 0, 255)).save(a)
    b = os.path.join(src, "B.jpg")                       # 不带 Alpha
    Image.new("RGB", (200, 120), (0, 200, 0)).save(b, "JPEG", quality=95)
    c = os.path.join(src, "C.png")                       # RGB 模式的 PNG（格式支持透明、但实际没有）
    Image.new("RGB", (200, 120), (0, 0, 200)).save(c)

    # ---- 1) 带 Alpha：抹成透明，输出 PNG，未擦像素不变 ----
    print("[1] 带 Alpha 的 PNG，抹一个区域内 → 透明")
    o1 = os.path.join(root, "o1")
    os.makedirs(o1)
    rc, so, se = run({"image": a}, {"regions": [{"x": 10, "y": 20, "w": 60, "h": 40}]}, o1)
    print(so.rstrip())
    if se.strip():
        print("  stderr:", se.strip())
    check(rc == 0, "退出码 0")
    check(os.listdir(o1) == ["0.png"], f"产出 0.png（实际 {os.listdir(o1)}）")
    with Image.open(os.path.join(o1, "0.png")) as im:
        px = im.convert("RGBA")
        check(im.format == "PNG", f"格式 PNG（实际 {im.format}）")
        check(px.getpixel((40, 40)) == (0, 0, 0, 0), f"区域中心 → 全透明（实际 {px.getpixel((40, 40))}）")
        check(px.getpixel((100, 100)) == (255, 0, 0, 255), f"区域外像素不变（实际 {px.getpixel((100, 100))}）")
        check(px.getpixel((5, 5)) == (255, 0, 0, 255), f"左上角（区域外）不变（实际 {px.getpixel((5, 5))}）")

    # ---- 2) 不带 Alpha 的 JPEG：抹成黑色，跟随原格式 ----
    print("\n[2] JPEG（无 Alpha），抹一个区域内 → 黑色")
    o2 = os.path.join(root, "o2")
    os.makedirs(o2)
    rc, so, se = run({"image": b}, {"regions": [{"x": 10, "y": 20, "w": 60, "h": 40}]}, o2)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check(os.listdir(o2) == ["0.jpg"], f"产出 0.jpg（实际 {os.listdir(o2)}）")
    with Image.open(os.path.join(o2, "0.jpg")) as im:
        check(im.format == "JPEG", f"格式 JPEG（实际 {im.format}）")
        check(near(im.convert("RGB").getpixel((40, 40)), (0, 0, 0), 6),
              f"区域中心 → 黑（实际 {im.convert('RGB').getpixel((40, 40))}）")
        check(near(im.convert("RGB").getpixel((150, 100)), (0, 200, 0), 6),
              "区域外像素仍是原绿色")

    # ---- 3) RGB 模式的 PNG（格式支持透明、实际无 Alpha）→ 抹成黑色、仍是 PNG 且不带 Alpha ----
    print("\n[3] RGB 模式的 PNG → 抹成黑色（不凭空加透明通道）")
    o3 = os.path.join(root, "o3")
    os.makedirs(o3)
    rc, so, se = run({"image": c}, {"regions": [{"x": 0, "y": 0, "w": 50, "h": 50}]}, o3)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check(os.listdir(o3) == ["0.png"], f"产出 0.png（实际 {os.listdir(o3)}）")
    with Image.open(os.path.join(o3, "0.png")) as im:
        check(im.mode == "RGB", f"仍是 RGB、没被加上 Alpha（实际 {im.mode}）")
        check(im.getpixel((25, 25)) == (0, 0, 0), f"区域中心 → 黑（实际 {im.getpixel((25, 25))}）")

    # ---- 4) 区域为空 → 原样复制（字节一致）----
    print("\n[4] 区域为空 → 原样复制输入")
    o4 = os.path.join(root, "o4")
    os.makedirs(o4)
    rc, so, se = run({"image": b}, {"regions": []}, o4)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check("原样复制" in so, "日志说明了原样复制")
    p4 = os.path.join(o4, "0.jpg")
    check(os.path.isfile(p4) and open(p4, "rb").read() == open(b, "rb").read(),
          "产出与原文件字节完全一致")

    # ---- 5) 全部区域越界 → 跳过并原样复制 ----
    print("\n[5] 区域完全落在图外 → 跳过 + 原样复制")
    o5 = os.path.join(root, "o5")
    os.makedirs(o5)
    rc, so, se = run({"image": a}, {"regions": [{"x": 500, "y": 500, "w": 40, "h": 40}]}, o5)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check("跳过" in so and "原样复制" in so, "日志里点了「跳过」与「原样复制」")
    p5 = os.path.join(o5, "0.png")
    check(os.path.isfile(p5) and open(p5, "rb").read() == open(a, "rb").read(), "字节一致（未改动）")

    # ---- 6) 部分越界 → 只抹交集 ----
    print("\n[6] 区域部分越界 → 夹回后只抹交集")
    o6 = os.path.join(root, "o6")
    os.makedirs(o6)
    rc, so, se = run({"image": c}, {"regions": [{"x": -30, "y": -30, "w": 80, "h": 80}]}, o6)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    with Image.open(os.path.join(o6, "0.png")) as im:
        im = im.convert("RGB")
        check(im.getpixel((10, 10)) == (0, 0, 0), "交集中的 (10,10) → 黑")
        check(im.getpixel((100, 100)) != (0, 0, 0), "图外方向的扩展没有影响图内其他像素")

    # ---- 7) 多个 / 重叠区域 ----
    print("\n[7] 两个重叠区域")
    o7 = os.path.join(root, "o7")
    os.makedirs(o7)
    rc, so, se = run(
        {"image": a},
        {"regions": [{"x": 0, "y": 0, "w": 80, "h": 80}, {"x": 40, "y": 40, "w": 80, "h": 80}]},
        o7,
    )
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    with Image.open(os.path.join(o7, "0.png")) as im:
        px = im.convert("RGBA")
        check(px.getpixel((70, 70)) == (0, 0, 0, 0), "重叠区 (70,70) → 透明")
        check(px.getpixel((10, 10)) == (0, 0, 0, 0), "A 独有区 → 透明")
        check(px.getpixel((110, 110)) == (0, 0, 0, 0), "B 独有区 → 透明")
        check(px.getpixel((150, 110)) == (255, 0, 0, 255), "两框之外 → 原色")

    # ---- 8) 错误：没连输入图 ----
    print("\n[8] 没连输入图")
    o8 = os.path.join(root, "o8")
    os.makedirs(o8)
    rc, so, se = run({"image": None}, {"regions": [{"x": 0, "y": 0, "w": 10, "h": 10}]}, o8)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("没有连接输入图" in se, "错误提示说明了没连图")
    check(os.listdir(o8) == [], "没有产出半套结果")

    shutil.rmtree(root, ignore_errors=True)
    print("\n" + ("全部通过" if not fails else f"{len(fails)} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
