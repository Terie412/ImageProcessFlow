# -*- coding: utf-8 -*-
"""直接跑 scripts/rounded_box/rounded_box.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：默认与自定义参数、透明底、描边**画在框内**（逐像素量描边宽度）、圆角 0 / 拉满被夹住、
无描边、越界夹取、空值兜底、非法颜色与非 0 退出、缺 output_dir，以及
**预览纯函数与执行产出逐像素一致**（这是「节点上的缩略图 = 会产出的那张图」这句承诺的落点）。
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
SCRIPT = os.path.join(ROOT, "scripts", "rounded_box", "rounded_box.py")
PY = sys.executable


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


def shot(out_dir, idx=0):
    return Image.open(os.path.join(out_dir, f"{idx}.png")).convert("RGBA")


def px(im, x, y):
    return im.getpixel((x, y))


def load_render():
    """把脚本当模块导进来，拿它的纯函数 render —— 服务端实时预览就是这么调的。"""
    spec = importlib.util.spec_from_file_location("rounded_box_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.render


def main():
    root = tempfile.mkdtemp(prefix="rounded_box_test_")
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def out_dir(name):
        d = os.path.join(root, name)
        os.makedirs(d)
        return d

    # ---- 1) 默认参数 ----
    print("[1] 一个参数都不给 → 512×512 PNG，透明底，描边在框内")
    d = out_dir("default")
    rc, so, se = run({}, d)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check(sorted(os.listdir(d)) == ["0.png"], f"只产出 0.png → {sorted(os.listdir(d))}")
    im = shot(d)
    check(im.size == (512, 512), f"尺寸 512×512（实际 {im.size}）")
    check(im.mode == "RGBA", f"带 alpha（实际 {im.mode}）")
    check(px(im, 6, 6)[3] == 0, f"圆角外是透明的（{px(im, 6, 6)}）")
    check(px(im, 0, 256)[:3] == (17, 17, 17) and px(im, 0, 256)[3] == 255,
          f"左边中点是描边色 #111111（{px(im, 0, 256)}）")
    check(px(im, 256, 256)[:3] == (255, 255, 255), f"中心是主体色 #ffffff（{px(im, 256, 256)}）")

    # ---- 2) 自定义参数：尺寸 / 颜色 / 描边宽度（逐像素量）----
    print("\n[2] 320×200、圆角 40、描边 16、主体 #4c8dff、描边 #003366")
    d = out_dir("custom")
    rc, so, se = run(
        {"width": 320, "height": 200, "radius": 40, "strokeWidth": 16,
         "fillColor": "#4c8dff", "strokeColor": "#003366"},
        d,
    )
    check(rc == 0, "退出码 0")
    im = shot(d)
    check(im.size == (320, 200), f"尺寸 320×200（实际 {im.size}）")
    check(px(im, 0, 100)[:3] == (0, 51, 102), f"描边色 #003366（{px(im, 0, 100)}）")
    check(px(im, 15, 100)[:3] == (0, 51, 102), f"描边内沿仍在外侧（{px(im, 15, 100)}）")
    check(px(im, 16, 100)[:3] == (76, 141, 255), f"描边宽度正好 16px：第 17 列起是主体色（{px(im, 16, 100)}）")
    check(px(im, 160, 100)[:3] == (76, 141, 255), f"中心是主体色（{px(im, 160, 100)}）")
    check(px(im, 2, 2)[3] == 0, f"圆角外透明（{px(im, 2, 2)}）")
    check(px(im, 319, 100)[:3] == (0, 51, 102), f"右边贴着图片边缘、没被裁（{px(im, 319, 100)}）")

    # ---- 3) 圆角 0 = 直角；拉满被夹到短边的一半 ----
    print("\n[3] 圆角 0 → 直角；圆角 999 → 夹到短边的一半（100）")
    d = out_dir("r0")
    rc, so, _ = run({"width": 200, "height": 200, "radius": 0, "strokeWidth": 8}, d)
    im = shot(d)
    check(rc == 0 and px(im, 0, 0)[:3] == (17, 17, 17), f"直角：左上角就是不透明的描边色（{px(im, 0, 0)}）")
    d = out_dir("rmax")
    rc, so, se = run({"width": 200, "height": 300, "radius": 999, "strokeWidth": 8}, d)
    print("  " + so.strip().replace("\n", "\n  "))
    im = shot(d)
    check(rc == 0, "退出码 0")
    check("夹住" in so, "日志点名了参数被夹住")
    check(px(im, 0, 150) == (17, 17, 17, 255) and px(im, 200 // 2, 0)[3] == 255,
          "圆角按**短边**（宽 200）的一半算：上下边剩下一条直线")

    # ---- 4) 描边 0 = 无描边 ----
    print("\n[4] 描边 0 → 只有主体色，没有描边")
    d = out_dir("sw0")
    rc, so, _ = run({"width": 100, "height": 100, "radius": 0, "strokeWidth": 0,
                     "fillColor": "#ff0000"}, d)
    im = shot(d)
    check(rc == 0, "退出码 0")
    check("无描边" in so, "日志里标了「无描边」")
    check(px(im, 0, 50)[:3] == (255, 0, 0) and px(im, 50, 50)[:3] == (255, 0, 0),
          f"边上就是主体色（{px(im, 0, 50)}）")

    # ---- 5) 越界 / 空值 / 乱填 ----
    print("\n[5] 宽填 9999 → 夹到 2048；空串与乱填 → 用默认值，不报错")
    d = out_dir("clamp")
    rc, so, _ = run({"width": 9999, "height": 9999}, d)
    check(rc == 0, "退出码 0")
    check(shot(d).size == (2048, 2048), f"宽高被夹到 2048（实际 {shot(d).size}）")
    d = out_dir("blank")
    rc, so, _ = run({"width": "", "height": "abc", "radius": None}, d)
    print(so.rstrip())
    check(rc == 0, "退出码 0（清空输入框不该让节点报错）")
    check(shot(d).size == (512, 512), f"退回默认 512×512（实际 {shot(d).size}）")
    d = out_dir("tiny")
    rc, so, _ = run({"width": 1, "height": 1}, d)
    check(rc == 0 and shot(d).size == (16, 16), f"下界 16（实际 {shot(d).size}）")

    # ---- 6) 错误分支 ----
    print("\n[6] 非法颜色 / 缺 output_dir → 非 0 退出且不产半套")
    d = out_dir("badcolor")
    rc, so, se = run({"fillColor": "nope"}, d)
    print("  stderr:", se.strip())
    check(rc != 0, "非 0 退出")
    check("颜色不合法" in se, "错误提示说了是颜色的问题")
    check(os.listdir(d) == [], "没有产出半套结果")
    d = out_dir("noout")
    p = subprocess.run([PY, SCRIPT], input='{"params": {}}', capture_output=True,
                       text=True, encoding="utf-8", cwd=d)
    print("  stderr:", p.stderr.strip())
    check(p.returncode != 0 and "output_dir" in p.stderr, "缺 output_dir 时明确报错")
    p = subprocess.run([PY, SCRIPT], input="not json", capture_output=True,
                       text=True, encoding="utf-8", cwd=d)
    check(p.returncode != 0 and "JSON" in p.stderr, "payload 不是 JSON 时明确报错")

    # ---- 7) 预览纯函数 == 执行产出（逐像素）----
    print("\n[7] 服务端预览用的那个纯函数，和点「执行」产出的图逐像素一致")
    params = {"width": 256, "height": 300, "radius": 37, "strokeWidth": 9,
              "fillColor": "#123456", "strokeColor": "#fedcba"}
    d = out_dir("same")
    rc, _, _ = run(params, d)
    check(rc == 0, "执行退出码 0")
    preview = load_render()(params)
    check(ImageChops.difference(shot(d), preview.convert("RGBA")).getbbox() is None,
          "预览图与执行产物没有任何一个像素不同")
    check(preview.mode == "RGBA" and preview.size == (256, 300), f"纯函数返回 {preview.size} {preview.mode}")

    print()
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
