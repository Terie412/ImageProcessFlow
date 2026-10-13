# -*- coding: utf-8 -*-
"""直接跑 scripts/color_balance/color_balance.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：两轴为 0 → 原样复制（字节一致）；正/负 warm 的方向（变黄 / 变蓝）；
tint 的方向（变绿 / 变品红）；「保持亮度」开关真的锁住平均亮度；
带 alpha 的图 alpha 逐字节不变且出 PNG；无 alpha 的 JPEG 进 JPEG 出；
多输入 + 未连线的口跳过 + 序号紧凑；错误分支（没连输入图 / 参数不是数字）；
超范围参数被夹住而不是失败。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageStat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "color_balance", "color_balance.py")
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


def mean(src, band=None):
    with Image.open(src) as im:
        return ImageStat.Stat(im.convert("RGB" if band is None else band)).mean


def bytes_equal(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def mean_lin_luma(path):
    """整图平均**线性**亮度（Rec.709）——「保持亮度」归一化的就是它。

    走直方图，不逐像素解码。用它而不是 PIL 的 "L"：后者是**编码值**上的
    6xx 加权，和「亮度」不是一回事，量化噪声也会混进来。
    """
    def lin(v):
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    with Image.open(path) as im:
        rgb = im.convert("RGB")
        hist = rgb.histogram()
        total = rgb.width * rgb.height
        table = [lin(v) for v in range(256)]
        y = 0.0
        for ci, w in enumerate((0.2126, 0.7152, 0.0722)):
            h = hist[ci * 256:(ci + 1) * 256]
            y += w * sum(cnt * table[v] for v, cnt in enumerate(h)) / total
        return y


def main():
    root = tempfile.mkdtemp(prefix="color_balance_test_")
    src, out = os.path.join(root, "src"), os.path.join(root, "out")
    os.makedirs(src), os.makedirs(out)
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def only(d, idx=0):
        """产出目录里第 idx 张（按文件名排序，输入顺序落盘）"""
        return os.path.join(d, sorted(os.listdir(d))[idx])

    def fresh(name):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        return d

    # ---- 素材：一张「整体偏蓝」的图 ----
    a = os.path.join(src, "A.png")
    Image.new("RGB", (32, 32), (60, 90, 170)).save(a)

    # ---- 素材：带 alpha（alpha 刻意取非全 0/255，才看得出有没有被动过）----
    b = os.path.join(src, "B.png")
    imb = Image.new("RGBA", (16, 16), (60, 90, 170, 40))
    px = imb.load()
    px[0, 0] = (200, 30, 90, 123)
    px[15, 15] = (10, 220, 40, 7)
    imb.save(b)

    # ---- 素材：不带 alpha 的 JPEG ----
    c = os.path.join(src, "C.jpg")
    Image.new("RGB", (16, 16), (60, 90, 170)).save(c, "JPEG", quality=95)

    # ---- 1) 两轴为 0 → 原样复制（字节一致）----
    print("[1] 两轴都是 0：原样复制")
    d = fresh("d1")
    rc, _, err = run({"image1": a}, {"warm": 0, "tint": 0}, d)
    check(rc == 0, f"退码 0（实际 {rc} {err.strip()}）")
    check(bytes_equal(a, only(d)), "产出与输入逐字节一致（没有重新编码）")

    # ---- 2) warm 方向：正数变黄、负数变蓝 ----
    print("[2] warm 方向")
    base = mean(a)
    d = fresh("d2p")
    run({"image1": a}, {"warm": 35, "tint": 0}, d)
    mp = mean(only(d))
    d = fresh("d2n")
    run({"image1": a}, {"warm": -35, "tint": 0}, d)
    mn = mean(only(d))
    br = lambda m: m[2] - m[0]  # B − R：越大越蓝、越小越黄
    check(br(mp) < br(base), f"warm=+35 让 B−R 变小（{br(base):.0f} → {br(mp):.0f}）＝变黄")
    check(br(mn) > br(base), f"warm=−35 让 B−R 变大（{br(base):.0f} → {br(mn):.0f}）＝变蓝")

    # ---- 3) tint 方向：正数变绿、负数变品红 ----
    print("[3] tint 方向")
    d = fresh("d3g")
    run({"image1": a}, {"warm": 0, "tint": 35}, d)
    mg = mean(only(d))
    d = fresh("d3m")
    run({"image1": a}, {"warm": 0, "tint": -35}, d)
    mm = mean(only(d))
    gr = lambda m: m[1] - (m[0] + m[2]) / 2  # G 相对红蓝：越大越绿、越小越品红
    check(gr(mg) > gr(base), f"tint=+35 让 G 相对更高（{gr(base):.0f} → {gr(mg):.0f}）＝变绿")
    check(gr(mm) < gr(base), f"tint=−35 让 G 相对更低（{gr(base):.0f} → {gr(mm):.0f}）＝变品红")

    # ---- 4) 保持亮度开关 ----
    # 阈值形态：开着时只允许 8 位取整带来的零点几个百分点；
    # 关掉时必须明显偏离（这张偏蓝图推到 warm=+50，蓝通道被压到 0.5 倍，掉得多）
    print("[4] 保持亮度")
    baseY = mean_lin_luma(a)
    d = fresh("d4on")
    run({"image1": a}, {"warm": 50, "tint": 0, "keepLuma": True}, d)
    eOn = abs(mean_lin_luma(only(d)) - baseY) / baseY
    d = fresh("d4off")
    run({"image1": a}, {"warm": 50, "tint": 0, "keepLuma": False}, d)
    eOff = abs(mean_lin_luma(only(d)) - baseY) / baseY
    check(eOn < 0.02, f"开着时线性亮度基本不变（相对差 {eOn * 100:.2f}%）")
    check(eOff > 0.05, f"关掉时亮度明显变了（相对差 {eOff * 100:.2f}%）")

    # ---- 5) 带 alpha：alpha 逐字节不变 + 出 PNG ----
    print("[5] 带 alpha 的图")
    d = fresh("d5")
    rc, _, err = run({"image1": b}, {"warm": 40, "tint": 0}, d)
    o = only(d)
    check(rc == 0, f"退码 0（实际 {rc} {err.strip()}）")
    check(o.endswith(".png"), f"输出是 PNG（{os.path.basename(o)}）")
    with Image.open(b) as sb, Image.open(o) as so:
        check(list(sb.convert("RGBA").split()[3].getdata()) == list(so.convert("RGBA").split()[3].getdata()),
              "alpha 通道逐像素不变")
        check(sb.convert("RGB").tobytes() != so.convert("RGB").tobytes(), "RGB 确实被改了")

    # ---- 6) 无 alpha 的 JPEG：格式跟随 ----
    print("[6] JPEG 格式跟随")
    d = fresh("d6")
    run({"image1": c}, {"warm": 40, "tint": 0}, d)
    o = only(d)
    with Image.open(o) as so:
        check(o.endswith(".jpg") and so.format == "JPEG", f"JPEG 进 JPEG 出（{os.path.basename(o)}）")
        check(so.mode == "RGB", f"没有凭空多出 alpha（mode={so.mode}）")

    # ---- 7) 多输入：中间口没连图时输出序号紧凑 ----
    print("[7] 多输入")
    d = fresh("d7")
    rc, _, err = run({"image1": a, "image2": None, "image3": c}, {"warm": 20, "tint": 0}, d)
    fs = sorted(os.listdir(d))
    check(rc == 0 and len(fs) == 2, f"两个口连了图 → 两张产出（{fs}）rc={rc} {err.strip()}")
    check(b"".join(os.fsencode(x) for x in fs), f"输出序号紧凑、没有空槽（{fs}）")

    # ---- 8) 错误分支 ----
    print("[8] 错误分支")
    d = fresh("d8")
    rc, _, err = run({"image1": None}, {"warm": 10}, d)
    check(rc != 0, f"一个口都没连 → 非 0 退出（{rc}）")
    check("输入口" in err, f"错误信息说清了原因（{err.strip()}）")
    rc, _, _ = run({"image1": a}, {"warm": "abc"}, d)
    check(rc != 0, f"参数不是数字 → 非 0 退出（{rc}）")

    # ---- 9) 超范围参数被夹住，不是失败 ----
    print("[9] 超范围参数")
    d = fresh("d9")
    rc, sout, err = run({"image1": a}, {"warm": 9999, "tint": 0}, d)
    d2 = fresh("d9b")
    run({"image1": a}, {"warm": 50, "tint": 0}, d2)
    check(rc == 0, f"warm=9999 被夹到上限、照常出图（rc={rc} {err.strip()}）")
    check(bytes_equal(only(d), only(d2)), "夹到上限后的产出与 warm=50 完全一致")

    shutil.rmtree(root, ignore_errors=True)
    print()
    if fails:
        print(f"❌ {len(fails)} 项失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
