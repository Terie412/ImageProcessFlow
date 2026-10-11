# -*- coding: utf-8 -*-
"""直接跑 scripts/resize/resize.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：格式跟随（PNG/JPEG/WEBP）、alpha 强制 PNG、未连线的口跳过、尺寸未变走复制、
错误分支（向量没填 / 一个口都没连）。
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
SCRIPT = os.path.join(ROOT, "scripts", "resize", "resize.py")
PY = sys.executable


def make(dirname):
    a = os.path.join(dirname, "A.png")
    Image.new("RGB", (300, 200), (200, 30, 30)).save(a)

    b = os.path.join(dirname, "B.png")
    Image.new("RGBA", (200, 150), (0, 128, 255, 0)).save(b)  # 全透明

    c = os.path.join(dirname, "C.jpg")
    Image.new("RGB", (400, 300), (30, 200, 90)).save(c, "JPEG", quality=90)

    d = os.path.join(dirname, "D.webp")
    Image.new("RGB", (100, 100), (255, 200, 0)).save(d, "WEBP")

    e = os.path.join(dirname, "E.jpg")
    Image.new("RGB", (1024, 1024), (10, 10, 10)).save(e, "JPEG", quality=90)
    return a, b, c, d, e


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
    root = tempfile.mkdtemp(prefix="resize_test_")
    src, out = os.path.join(root, "src"), os.path.join(root, "out")
    os.makedirs(src), os.makedirs(out)
    a, b, c, d, e = make(src)
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ---- 1) 多张、格式各异，只给一个向量；image2 未连线 ----
    print("[1] 5 张输入（其中 1 个口未连线），向量 1024×768")
    rc, so, se = run(
        {"image1": a, "image2": None, "image3": b, "image4": c, "image5": d},
        {"width": 1024, "height": 768},
        out,
    )
    print(so.rstrip())
    if se.strip():
        print("  stderr:", se.strip())
    check(rc == 0, "退出码 0")
    files = sorted(os.listdir(out))
    check(files == ["0.png", "1.png", "2.jpg", "3.webp"], f"产出 4 张且扩展名跟随原图 → {files}")
    for name, expect_fmt in [("0.png", "PNG"), ("1.png", "PNG"), ("2.jpg", "JPEG"), ("3.webp", "WEBP")]:
        p = os.path.join(out, name)
        if not os.path.isfile(p):
            check(False, f"{name} 存在")
            continue
        with Image.open(p) as im:
            check(im.size == (1024, 768), f"{name} 尺寸 = 1024×768（实际 {im.size}）")
            check(im.format == expect_fmt, f"{name} 格式 = {expect_fmt}（实际 {im.format}）")
    with Image.open(os.path.join(out, "1.png")) as im:
        alphas = im.convert("RGBA").getchannel("A").getextrema()
        check(alphas == (0, 0), f"透明源的 alpha 被保住（alpha 极值 {alphas}）")

    # ---- 2) 尺寸未变 + 格式一致 → 走复制（字节完全一致）----
    print("\n[2] 1024×1024 的 JPEG 求 1024×1024，扩展名一致 → 直接复制")
    out2 = os.path.join(root, "out2")
    os.makedirs(out2)
    rc, so, se = run({"image1": e}, {"width": 1024, "height": 1024}, out2)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check("直接复制" in so, "日志里出现了「直接复制」")
    produced = os.path.join(out2, "0.jpg")
    check(os.path.isfile(produced), "产出 0.jpg")
    if os.path.isfile(produced):
        check(open(produced, "rb").read() == open(e, "rb").read(), "字节与原文件完全一致")

    # ---- 3) 放大 / 缩小 ----
    print("\n[3] 缩小到 64×64")
    out3 = os.path.join(root, "out3")
    os.makedirs(out3)
    rc, so, se = run({"image1": c}, {"width": 64, "height": 64}, out3)
    check(rc == 0, "退出码 0")
    with Image.open(os.path.join(out3, "0.jpg")) as im:
        check(im.size == (64, 64), f"尺寸 64×64（实际 {im.size}）")

    # ---- 4) 错误：向量没填 ----
    print("\n[4] width 留空")
    out4 = os.path.join(root, "out4")
    os.makedirs(out4)
    rc, so, se = run({"image1": a}, {"width": "", "height": 500}, out4)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("宽没填" in se, "错误提示点名了「宽」")
    check(os.listdir(out4) == [], "没有产出半套结果")

    # ---- 5) 错误：一个口都没连 ----
    print("\n[5] 所有输入口都没连图")
    rc, so, se = run({"image1": None}, {"width": 512, "height": 512}, out4)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("都没连图" in se, "错误提示说明了没连图")

    # ---- 6) 错误：像素面积过大 ----
    print("\n[6] 16384×16384 总面积超限")
    rc, so, se = run({"image1": a}, {"width": 16384, "height": 16384}, out4)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("太大" in se, "错误提示说明了太大")

    shutil.rmtree(root, ignore_errors=True)
    print("\n" + ("全部通过" if not fails else f"{len(fails)} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
