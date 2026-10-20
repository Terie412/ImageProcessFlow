# -*- coding: utf-8 -*-
"""直接跑 scripts/blur/blur.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：强度 0 逐像素不变 / 强度越大过渡带越宽（实测过渡带的像素数）/
alpha 也跟着变软 / 格式跟随（PNG→PNG、JPEG→.jpg）/ 带 alpha 一律 PNG /
多输入逐张产出且序号对齐 / 没连图与缺 output_dir 非 0 退出 /
空串兜底、越界夹取 / manifest 与脚本对得上。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_blur.py
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
DIR = os.path.join(ROOT, "scripts", "blur")
SCRIPT = os.path.join(DIR, "blur.py")
MANIFEST = os.path.join(DIR, "manifest.json")
PY = sys.executable

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run(params, inputs, out_dir):
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


def load_mod():
    spec = importlib.util.spec_from_file_location("blur_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def edge_image(size=(240, 60)):
    """左黑右白的一条硬边 —— 模糊之后会变成一段过渡带，用它量强度。"""
    im = Image.new("RGB", size, (0, 0, 0))
    for x in range(size[0] // 2, size[0]):
        for y in range(size[1]):
            im.putpixel((x, y), (255, 255, 255))
    return im


def ramp_width(im):
    """数一行里有多少个「既不是黑也不是白」的像素 = 过渡带宽度。"""
    y = im.size[1] // 2
    n = 0
    for x in range(im.size[0]):
        v = im.getpixel((x, y))
        v = v[0] if isinstance(v, tuple) else v
        if 10 < v < 245:
            n += 1
    return n


def main():
    root = tempfile.mkdtemp(prefix="blur_test_")
    mod = load_mod()

    def out_dir(n):
        d = os.path.join(root, n)
        os.makedirs(d)
        return d

    src = os.path.join(root, "edge.png")
    edge_image().save(src)
    src_im = Image.open(src).convert("RGB")

    # ---- 1) 默认强度：真的糊了，尺寸不变 ----
    print("[1] 默认参数（强度 8）→ 硬边变成过渡带")
    d = out_dir("default")
    rc, so, se = run({}, {"image1": src}, d)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check(os.path.isfile(os.path.join(d, "0.png")), "产出 0.png（PNG 进 PNG 出）")
    out = Image.open(os.path.join(d, "0.png")).convert("RGB")
    check(out.size == src_im.size, f"尺寸不变 {out.size}")
    w8 = ramp_width(out)
    check(w8 > 4, f"过渡带有 {w8} 像素（没糊的话应该是 0）")

    # ---- 2) 强度 0：逐像素不变 ----
    print("[2] 强度 0 → 原样")
    d = out_dir("zero")
    rc, so, se = run({"strength": 0}, {"image1": src}, d)
    print(so.rstrip())
    z = Image.open(os.path.join(d, "0.png")).convert("RGB")
    check(rc == 0, "退出码 0")
    check(ImageChops.difference(z, src_im).getbbox() is None, "与源图逐像素一致")
    check("原样" in so, "日志说明了没模糊")

    # ---- 3) 强度越大越糊（量过渡带）----
    print("[3] 强度 24 的过渡带要比 8 宽")
    d = out_dir("strong")
    rc, so, se = run({"strength": 24}, {"image1": src}, d)
    print(so.rstrip())
    w24 = ramp_width(Image.open(os.path.join(d, "0.png")).convert("RGB"))
    check(w24 > w8, f"过渡带 {w8} → {w24}")

    # ---- 4) 格式跟随 ----
    print("[4] 格式跟随：JPEG 进 .jpg 出、PNG 进 .png 出")
    jpg = os.path.join(root, "photo.jpg")
    Image.new("RGB", (64, 48), (120, 30, 30)).save(jpg, "JPEG", quality=95)
    d = out_dir("jpg")
    rc, so, se = run({"strength": 4}, {"image1": jpg}, d)
    print(so.rstrip())
    check(rc == 0 and os.path.isfile(os.path.join(d, "0.jpg")), "产出 0.jpg")
    check(Image.open(os.path.join(d, "0.jpg")).format == "JPEG", "真的是 JPEG")

    # ---- 5) 带 alpha：一律 PNG，而且 alpha 被模糊（硬边变软）----
    print("[5] 带 Alpha：存 PNG，alpha 边缘变软")
    rgba = Image.new("RGBA", (80, 40), (255, 0, 0, 255))
    for y in range(40):
        for x in range(40, 80):
            rgba.putpixel((x, y), (255, 0, 0, 0))
    rp = os.path.join(root, "alpha.png")
    rgba.save(rp)
    d = out_dir("alpha")
    rc, so, se = run({"strength": 6}, {"image1": rp}, d)
    print(so.rstrip())
    check(rc == 0 and os.path.isfile(os.path.join(d, "0.png")), "产出 0.png")
    a = Image.open(os.path.join(d, "0.png")).convert("RGBA")
    mids = [a.getpixel((x, 20))[3] for x in range(34, 47)]
    check(any(10 < v < 245 for v in mids), f"alpha 边缘出现中间值（{mids}）")
    check(all(v > 200 for v in [a.getpixel((x, 20))[3] for x in range(0, 30)]), "远离边缘的 alpha 不受影响")

    # ---- 6) 多输入：几张进几张出，序号对齐 ----
    print("[6] 两个输入 → 0.* / 1.*，序号跟端口顺序走")
    a1 = os.path.join(root, "one.png")
    a2 = os.path.join(root, "two.jpg")
    Image.new("RGB", (20, 20), (10, 10, 10)).save(a1)
    Image.new("RGB", (30, 30), (200, 200, 200)).save(a2, "JPEG", quality=90)
    d = out_dir("multi")
    rc, so, se = run({"strength": 3}, {"image2": a2, "image1": a1}, d)
    print(so.rstrip())
    check(rc == 0, "退出码 0")
    check(os.path.isfile(os.path.join(d, "0.png")), "0 是 image1（PNG）")
    check(os.path.isfile(os.path.join(d, "1.jpg")), "1 是 image2（JPEG）")
    check(Image.open(os.path.join(d, "0.png")).size == (20, 20), "0 的尺寸来自 image1")
    check(Image.open(os.path.join(d, "1.jpg")).size == (30, 30), "1 的尺寸来自 image2")

    # ---- 7) 兜底与夹取 ----
    print("[7] 空串走默认、越界夹住、乱填走默认")
    check(mod.resolve({})["strength"] == 8, "默认 8")
    check(mod.resolve({"strength": ""})["strength"] == 8, "空串 → 默认")
    check(mod.resolve({"strength": "abc"})["strength"] == 8, "非数字 → 默认")
    check(mod.resolve({"strength": -30})["strength"] == 0, "负数 → 0")
    check(mod.resolve({"strength": 9999})["strength"] == 200, "超大 → 200")
    d = out_dir("capped")
    rc, so, se = run({"strength": 9999}, {"image1": src}, d)
    check("被夹到" in so, "日志说明被夹住了")

    # ---- 8) 协议分支 ----
    print("[8] 没连图 / 缺 output_dir → 非 0 退出")
    d = out_dir("noinput")
    rc, so, se = run({}, {}, d)
    check(rc != 0 and "输入口" in se, "没连图报错")
    p = subprocess.run([PY, SCRIPT], input='{"params": {}}', capture_output=True, text=True, encoding="utf-8")
    check(p.returncode != 0 and "output_dir" in p.stderr, "缺 output_dir 报错")

    # ---- 9) manifest ----
    print("[9] manifest 与脚本对得上")
    mf = json.load(open(MANIFEST, encoding="utf-8"))
    check(mf["id"] == "blur" and mf["name"] == "高斯模糊", f"id/名字 {mf['id']} / {mf['name']}")
    check([p["key"] for p in mf["params"]] == ["strength"], "只有一个参数 strength")
    check(mf["outputs"][0]["multi"] is True, "多输出（多输入时逐张）")
    check(mf["inputs"][0].get("dynamic") is True, "输入口可增减")
    p0 = mf["params"][0]
    check(p0["min"] <= p0["default"] <= p0["max"], f"默认值在区间内（{p0['min']}~{p0['max']}）")

    print("=" * 66)
    ok = 0 if fails else 1
    print("全部通过" if ok else f"{len(fails)} 项失败")
    for m in fails:
        print("  FAIL " + m)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
