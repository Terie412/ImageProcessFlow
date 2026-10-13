# -*- coding: utf-8 -*-
"""直接跑 scripts/alpha_erase/alpha_erase.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：低于阈值的像素 RGB 置 0 且 alpha 不变；等于/高于阈值的像素原样保留；
无 alpha 通道 → 原样复制（字节一致）；阈值 0 / 无像素达标 → 原样复制；
多输入 + 未连线的口跳过 + 序号紧凑；错误分支（没连输入图）。
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
SCRIPT = os.path.join(ROOT, "scripts", "alpha_erase", "alpha_erase.py")
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


def main():
    root = tempfile.mkdtemp(prefix="alpha_erase_test_")
    src, out = os.path.join(root, "src"), os.path.join(root, "out")
    os.makedirs(src), os.makedirs(out)
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- 素材 A：一行 5 个像素，alpha 覆盖 0 / 1 / 7 / 8 / 255 ----
    a = os.path.join(src, "A.png")
    im = Image.new("RGBA", (5, 1), (255, 255, 255, 255))
    px = im.load()
    px[0, 0] = (165, 57, 209, 0)
    px[1, 0] = (165, 57, 209, 1)
    px[2, 0] = (255, 0, 255, 7)
    px[3, 0] = (10, 20, 30, 8)     # == 阈值，不该被动
    px[4, 0] = (200, 100, 50, 255)
    im.save(a)

    # ---- 素材 B：不带 alpha 的 JPEG ----
    b = os.path.join(src, "B.jpg")
    Image.new("RGB", (20, 10), (30, 200, 90)).save(b, "JPEG", quality=95)

    # ---- 素材 C：RGBA 但 alpha 全是不透明 ----
    c = os.path.join(src, "C.png")
    Image.new("RGBA", (20, 10), (12, 34, 56, 255)).save(c)

    # ---- 1) 阈值 8：低于的置 0，等于/高于的不动 ----
    print("[1] 阈值 8：alpha<8 的像素 RGB 置 0")
    o1 = os.path.join(root, "o1")
    os.makedirs(o1)
    rc, so, se = run({"image1": a}, {"threshold": 8}, o1)
    print(so.rstrip())
    if se.strip():
        print("  stderr:", se.strip())
    check(rc == 0, "退出码 0")
    check(os.listdir(o1) == ["0.png"], f"产出 0.png（实际 {os.listdir(o1)}）")
    with Image.open(os.path.join(o1, "0.png")) as o:
        got = o.convert("RGBA")
        check(got.getpixel((0, 0)) == (0, 0, 0, 0), f"alpha=0 → RGB 置 0（实际 {got.getpixel((0, 0))}）")
        check(got.getpixel((1, 0)) == (0, 0, 0, 1), f"alpha=1 → RGB 置 0、alpha 保持 1（实际 {got.getpixel((1, 0))}）")
        check(got.getpixel((2, 0)) == (0, 0, 0, 7), f"alpha=7 → RGB 置 0（实际 {got.getpixel((2, 0))}）")
        check(got.getpixel((3, 0)) == (10, 20, 30, 8), f"alpha=8（等于阈值）→ 原样不动（实际 {got.getpixel((3, 0))}）")
        check(got.getpixel((4, 0)) == (200, 100, 50, 255), f"alpha=255 → 原样不动（实际 {got.getpixel((4, 0))}）")
        check(got.getchannel("A").tobytes() == im.getchannel("A").tobytes(), "alpha 通道一个字节都没改")

    # ---- 2) 阈值 1：只抹 alpha=0 的 ----
    print("\n[2] 阈值 1：只抹 alpha=0")
    o2 = os.path.join(root, "o2")
    os.makedirs(o2)
    rc, so, se = run({"image1": a}, {"threshold": 1}, o2)
    check(rc == 0, "退出码 0")
    with Image.open(os.path.join(o2, "0.png")) as o:
        got = o.convert("RGBA")
        check(got.getpixel((0, 0)) == (0, 0, 0, 0), f"alpha=0 → 置 0（实际 {got.getpixel((0, 0))}）")
        check(got.getpixel((1, 0)) == (165, 57, 209, 1), f"alpha=1（等于阈值）→ 不动（实际 {got.getpixel((1, 0))}）")

    # ---- 3) 无 alpha 的 JPEG → 原样复制（字节一致）----
    print("\n[3] 无 alpha 通道 → 原样复制")
    o3 = os.path.join(root, "o3")
    os.makedirs(o3)
    rc, so, se = run({"image1": b}, {"threshold": 200}, o3)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    p3 = os.path.join(o3, "0.jpg")
    check(os.path.isfile(p3) and open(p3, "rb").read() == open(b, "rb").read(), "产出 0.jpg 且字节与原文件一致")

    # ---- 4) 阈值 0 → 原样复制 ----
    print("\n[4] 阈值 0 → 原样复制")
    o4 = os.path.join(root, "o4")
    os.makedirs(o4)
    rc, so, se = run({"image1": a}, {"threshold": 0}, o4)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    p4 = os.path.join(o4, "0.png")
    check(os.path.isfile(p4) and open(p4, "rb").read() == open(a, "rb").read(), "字节一致")

    # ---- 5) 带 alpha 但没有像素低于阈值 → 原样复制 ----
    print("\n[5] 带 alpha、无像素低于阈值 → 原样复制")
    o5 = os.path.join(root, "o5")
    os.makedirs(o5)
    rc, so, se = run({"image1": c}, {"threshold": 8}, o5)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    p5 = os.path.join(o5, "0.png")
    check(os.path.isfile(p5) and open(p5, "rb").read() == open(c, "rb").read(), "字节一致")

    # ---- 6) 多输入 + 中间口未连线 → 序号紧凑 ----
    print("\n[6] 多输入（image2 未连线）→ 产出紧凑")
    o6 = os.path.join(root, "o6")
    os.makedirs(o6)
    rc, so, se = run({"image1": a, "image2": None, "image3": b}, {"threshold": 8}, o6)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check(sorted(os.listdir(o6)) == ["0.png", "1.jpg"],
          f"产出 0.png + 1.jpg，中间不留空槽（实际 {sorted(os.listdir(o6))}）")

    # ---- 7) 错误：没连输入图 ----
    print("\n[7] 没连输入图")
    o7 = os.path.join(root, "o7")
    os.makedirs(o7)
    rc, so, se = run({"image1": None}, {"threshold": 8}, o7)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("都没连图" in se, "错误提示说明了没连图")
    check(os.listdir(o7) == [], "没有产出半套结果")

    # ---- 8) 错误：阈值越界 ----
    print("\n[8] 阈值 300（越界）")
    rc, so, se = run({"image1": a}, {"threshold": 300}, o7)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("0~255" in se, "错误提示说明了范围")

    shutil.rmtree(root, ignore_errors=True)
    print("\n" + ("全部通过" if not fails else f"{len(fails)} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
