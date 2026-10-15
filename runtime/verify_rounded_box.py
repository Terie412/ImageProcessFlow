# -*- coding: utf-8 -*-
"""直接跑 scripts/rounded_box/rounded_box.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：默认与自定义参数、透明底、描边**画在框内**（逐像素量描边宽度）、圆角 0 / 拉满被夹住、
**椭圆角（横竖两个半径各自生效）**、
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


def load_resolve():
    """拿脚本的 resolve —— 夹取 / 兜底之后的实际取值（就是日志里打的那串）。"""
    spec = importlib.util.spec_from_file_location("rounded_box_resolve", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve


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
        {"width": 320, "height": 200, "radiusX": 40, "radiusY": 40, "strokeWidth": 16,
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

    # ---- 3) 圆角 0 = 直角；拉满时**各自**被那一条边的一半夹住 ----
    print("\n[3] 圆角 0 → 直角；圆角 999 → 横轴夹到宽的一半（100）、竖轴夹到高的一半（150）")
    d = out_dir("r0")
    rc, so, _ = run({"width": 200, "height": 200, "radiusX": 0, "radiusY": 0, "strokeWidth": 8}, d)
    im = shot(d)
    check(rc == 0 and px(im, 0, 0)[:3] == (17, 17, 17), f"直角：左上角就是不透明的描边色（{px(im, 0, 0)}）")
    d = out_dir("rmax")
    rc, so, se = run({"width": 200, "height": 300, "radiusX": 999, "radiusY": 999, "strokeWidth": 8}, d)
    print("  " + so.strip().replace("\n", "\n  "))
    im = shot(d)
    check(rc == 0, "退出码 0")
    check("夹住" in so, "日志点名了参数被夹住")
    # 注意阈值用 ≥191 而不是 ==255：弧线的**切线点**附近（这里是左右边的中点）覆盖率
    # 天然是渐变的，超采样在那里会低估一点点（见 [8] 末尾那段说明）—— 这是既有行为，
    # 旧实现（PIL 的 rounded_rectangle）一模一样
    check(px(im, 0, 150)[3] >= 191 and px(im, 100, 0)[3] >= 191
          and px(im, 100, 150) == (255, 255, 255, 255),
          "拉满时左右边中点、上下边中点都还在（两个半径各自到自己那条边的一半）")
    g = load_resolve()({"width": 200, "height": 300, "radiusX": 999, "radiusY": 999})
    check((g["rx"], g["ry"]) == (100, 150),
          f"resolve 里横轴 100 = 宽/2、竖轴 150 = 高/2（实际 {g['rx']}×{g['ry']}）")

    # ---- 4) 描边 0 = 无描边 ----
    print("\n[4] 描边 0 → 只有主体色，没有描边")
    d = out_dir("sw0")
    rc, so, _ = run({"width": 100, "height": 100, "radiusX": 0, "radiusY": 0, "strokeWidth": 0,
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
    rc, so, _ = run({"width": "", "height": "abc", "radiusX": None, "radiusY": ""}, d)
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
    params = {"width": 256, "height": 300, "radiusX": 37, "radiusY": 37, "strokeWidth": 9,
              "fillColor": "#123456", "strokeColor": "#fedcba"}
    d = out_dir("same")
    rc, _, _ = run(params, d)
    check(rc == 0, "执行退出码 0")
    preview = load_render()(params)
    check(ImageChops.difference(shot(d), preview.convert("RGBA")).getbbox() is None,
          "预览图与执行产物没有任何一个像素不同")
    check(preview.mode == "RGBA" and preview.size == (256, 300), f"纯函数返回 {preview.size} {preview.mode}")

    # ---- 8) 椭圆角：横竖两个半径各管自己那一边 ----
    print("\n[8] 椭圆角：两个半径各自生效")
    render = load_render()

    def spans(params):
        """返回「顶边完全不透明的 x 区间」「左边完全不透明的 y 区间」。"""
        im = render(params).convert("RGBA")
        W, H = im.size
        p2 = im.load()
        return (
            [x for x in range(W) if p2[x, 0][3] == 255],
            [y for y in range(H) if p2[0, y][3] == 255],
        )

    base = {"width": 200, "height": 200, "strokeWidth": 0}
    row60, col20 = spans({**base, "radiusX": 60, "radiusY": 20})
    row20, col60 = spans({**base, "radiusX": 20, "radiusY": 60})
    # 只断言**相对关系**，不钉死具体像素数：切线点附近的「完全不透明」阈值会漂几个像素
    # （超采样在那里低估覆盖率，见本组末尾）
    check(len(col20) > 150 and len(col60) < 110,
          f"竖轴越大，左边那条实线越短（ry=20 → {len(col20)}，ry=60 → {len(col60)}）")
    check(len(row60) < len(row20),
          f"横轴越大，顶边那条实线越短（rx=60 → {len(row60)}，rx=20 → {len(row20)}）")
    mid_ok = (abs((col20[0] + col20[-1]) / 2 - 99.5) <= 1.5
              and abs((row60[0] + row60[-1]) / 2 - 99.5) <= 1.5)
    check(mid_ok, "两个方向的不透明区间都关于画面中线对称")

    # 一个只有「椭圆角」才会不透明的点：(10,10) 在 rx=60/ry=20 的椭圆内，但在 r=60 的正圆外
    im_e = render({**base, "radiusX": 60, "radiusY": 20, "fillColor": "#ffffff"}).convert("RGBA")
    im_c = render({**base, "radiusX": 60, "radiusY": 60, "fillColor": "#ffffff"}).convert("RGBA")
    check(im_e.load()[10, 10][3] == 255,
          f"rx=60 ry=20 时 (10,10) 是实的 —— 竖轴按 20 收，不是正圆（实际 {im_e.load()[10, 10]}）")
    check(im_c.load()[10, 10][3] == 0,
          f"rx=ry=60 时同一个点在外面（正圆会切掉它）（实际 {im_c.load()[10, 10]}）")

    row0, col0 = spans({**base, "radiusX": 0, "radiusY": 96})
    check(row0[0] == 0 and col0[0] == 0,
          f"任一个半径为 0 就是直角：(0,0) 落在实心范围里（顶边从 x={row0[0]} 起、左边从 y={col0[0]} 起）")

    # 切线点附近：覆盖率会被低估一点点（弧线在那里几乎是竖的，把「按小格中心取样」的
    # 误差放大了）。这是**既有行为** —— 旧实现用 PIL 的 rounded_rectangle 一模一样
    # （正圆 r=60：旧 y=50..57 给 64,96,128,160,191,191,239,255，新给 16,64,128,160,191,207,255）。
    # 记在这里是为了：改渲染方式时知道有这么一根弦，别以为换了多边形才有的。
    def col_alpha(render_fn, params, x=0):
        im2 = render_fn(params).convert("RGBA")
        return [im2.load()[x, y][3] for y in range(im2.size[1])]

    ramp_old_like = col_alpha(render, {**base, "radiusX": 60, "radiusY": 60})
    check(ramp_old_like[52] < 255 and ramp_old_like[60] == 255,
          f"正圆 r=60：左边那一列在切线点（y=60）之前是渐变的、之后才是实心"
          f"（y=52 → {ramp_old_like[52]}，y=60 → {ramp_old_like[60]}）")

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
