# -*- coding: utf-8 -*-
"""直接跑 scripts/halo/halo.py，验证产出（不依赖服务端、不调用任何模型）。

覆盖：扩散 0 与源图逐像素一致 / **实测晕染延伸多少像素**（按 0.9~1.1 倍口径）/
浓度就是峰值不透明度（30 < 70 < 100 单调、85 ≈ 217/255）/ 晕染是目标色、
原图像素一个不改 / 不带 alpha 的图照原格式输出且日志点明「白做」/
带 alpha 一律 PNG / 多输入逐张且序号对齐 / 没连图与缺 output_dir 非 0 退出 /
空串兜底与越界夹取 / manifest 与脚本对得上。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_halo.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "scripts", "halo")
SCRIPT = os.path.join(DIR, "halo.py")
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
    spec = importlib.util.spec_from_file_location("halo_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def block_image(size=(800, 800), box=(300, 300, 500, 500)):
    """透明画布中间一块不透明的白方块 —— 形状明确，方便量「往外伸了多少」。"""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), box)
    return im, box


def right_extent(im, box):
    """从方块右边缘往外数，最后一个 alpha > 0 的像素离边缘多远（px）。"""
    y = (box[1] + box[3]) // 2
    edge = box[2] - 1
    ext = 0
    for x in range(edge + 1, im.size[0]):
        if im.getpixel((x, y))[3] > 0:
            ext = x - edge
    return ext


def edge_alpha(im, box, offset=1):
    """方块右边缘往外 offset 像素处的 alpha —— 晕染最浓的地方（原图里量不到，被盖住了）。"""
    return im.getchannel("A").getpixel((box[2] - 1 + offset, (box[1] + box[3]) // 2))


def main():
    base_tmp = os.path.join(ROOT, "runtime", "tmp")
    os.makedirs(base_tmp, exist_ok=True)
    root = tempfile.mkdtemp(prefix="halo_test_", dir=base_tmp)
    mod = load_mod()

    def out_dir(n):
        d = os.path.join(root, n)
        os.makedirs(d)
        return d

    block, box = block_image()
    src = os.path.join(root, "block.png")
    block.save(src)

    color = "#101820"
    rgb = (0x10, 0x18, 0x20)

    # ---- 1. 扩散 0：原样（逐像素） ----
    print("\n[扩散 0 = 不做晕染]")
    d = out_dir("spread0")
    code, out, err = run({"color": color, "spread": 0, "density": 80}, {"image1": src}, d)
    check(code == 0, f"退出码 0（实际 {code}，stderr={err.strip()[:120]}）")
    got = Image.open(os.path.join(d, "0.png")).convert("RGBA")
    check(got.tobytes() == block.tobytes(), "与源图逐像素一致")
    check("不做晕染" in out, "日志里点明了「不做晕染」")

    # ---- 2. 延伸距离 ≈ 扩散范围 ----
    print("\n[实测晕染延伸的像素数]")
    for spread in (24, 60, 120):
        d = out_dir(f"ext{spread}")
        code, out, err = run({"color": color, "spread": spread, "density": 100}, {"image1": src}, d)
        got = Image.open(os.path.join(d, "0.png")).convert("RGBA")
        ext = right_extent(got, box)
        tol = max(3, spread * 0.2)
        check(code == 0 and abs(ext - spread) <= tol, f"扩散 {spread} → 实测 {ext}px（容差 ±{tol:.0f}）")

    # ---- 3. 浓度 = 峰值不透明度，且单调 ----
    print("\n[浓度]")
    peaks = {}
    for dens in (30, 70, 100):
        d = out_dir(f"dens{dens}")
        code, out, err = run({"color": color, "spread": 40, "density": dens}, {"image1": src}, d)
        got = Image.open(os.path.join(d, "0.png")).convert("RGBA")
        peaks[dens] = edge_alpha(got, box)
    check(peaks[30] < peaks[70] < peaks[100], f"浓度越大贴边处越浓：{peaks[30]} < {peaks[70]} < {peaks[100]}")
    check(peaks[100] >= 250, f"浓度 100 → 贴边 {peaks[100]}/255（该到顶）")
    check(abs(peaks[70] - round(0.70 * 255)) <= 6, f"浓度 70 → 贴边 {peaks[70]}/255，≈179")

    # 直接问脚本内部的蒙版函数：峰值必须**精确**等于 浓度 × 255
    alpha = block.getchannel("A")
    exact = [mod.halo_alpha(alpha, 40, d).getextrema()[1] for d in (30, 70, 100)]
    check(exact == [round(d * 255 / 100) for d in (30, 70, 100)],
          f"蒙版峰值精确等于 浓度×255：{exact}")

    # 细笔画 + 大扩散：归一化后浓度仍然铺得满（不归一化时这里会掉到几十）
    thin = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
    thin.paste((255, 0, 0, 255), (400, 100, 404, 700))
    thin_src = os.path.join(root, "thin.png")
    thin.save(thin_src)
    d = out_dir("thin")
    run({"color": color, "spread": 300, "density": 100}, {"image1": thin_src}, d)
    thin_got = Image.open(os.path.join(d, "0.png")).convert("RGBA")
    thin_peak = max(thin_got.getchannel("A").getpixel((x, 400)) for x in (403, 405, 450, 550))
    check(thin_peak >= 250, f"4px 细线配扩散 300：贴边峰值 {thin_peak}/255（归一化后仍到顶）")

    # ---- 4. 颜色只染晕染，原图像素不动 ----
    print("\n[颜色 / 原图像素]")
    d = out_dir("color")
    run({"color": color, "spread": 60, "density": 100}, {"image1": src}, d)
    got = Image.open(os.path.join(d, "0.png")).convert("RGBA")
    y = (box[1] + box[3]) // 2
    check(got.getpixel(((box[2] + 30), y))[:3] == rgb, "晕染区是目标色")
    check(got.getpixel(((box[0] + 10), y)) == (255, 0, 0, 255), "原图方块内部一个像素都没改")

    # ---- 5. 没有 alpha 的图：照原格式输出 + 日志点明 ----
    print("\n[源图没有透明通道]")
    flat = Image.new("RGB", (300, 200), (200, 120, 60))
    flat_src = os.path.join(root, "flat.png")
    flat.save(flat_src)
    d = out_dir("flat")
    code, out, err = run({"color": color, "spread": 40, "density": 100}, {"image1": flat_src}, d)
    names = sorted(os.listdir(d))
    check(code == 0 and names == ["0.png"], f"PNG 进 PNG 出：{names}")
    got = Image.open(os.path.join(d, "0.png"))
    check(got.mode == "RGB" and got.tobytes() == flat.tobytes(), "不带 alpha：输出就是原图（RGB，逐像素一致）")
    check("没有透明区域" in out, "日志里点明了「没形状可认」")

    jpg_src = os.path.join(root, "flat.jpg")
    flat.save(jpg_src, "JPEG", quality=95)
    d = out_dir("flatjpg")
    code, out, err = run({"color": color, "spread": 40, "density": 100}, {"image1": jpg_src}, d)
    check(sorted(os.listdir(d)) == ["0.jpg"], f"JPEG 跟随原格式：{sorted(os.listdir(d))}")

    # ---- 6. 带 alpha 一律 PNG + 多输入序号对齐 ----
    print("\n[多输入 / 输出格式]")
    small = block_image(size=(400, 400), box=(150, 150, 250, 250))[0]
    small_src = os.path.join(root, "small.png")
    small.save(small_src)
    d = out_dir("multi")
    code, out, err = run(
        {"color": color, "spread": 30, "density": 70},
        {"image1": src, "image2": None, "image3": small_src},
        d,
    )
    names = sorted(os.listdir(d))
    check(code == 0 and names == ["0.png", "1.png"], f"两个连了图的输入 → {names}")
    check(Image.open(os.path.join(d, "0.png")).size == (800, 800), "0.png 尺寸跟第 1 个输入")
    check(Image.open(os.path.join(d, "1.png")).size == (400, 400), "1.png 尺寸跟第 3 个输入（空口跳过、序号紧凑）")

    # ---- 7. 兜底与夹取 ----
    print("\n[参数兜底与夹取]")
    check(mod.resolve({"spread": "", "density": ""})["spread"] == 24, "空串 → 默认扩散 24")
    check(mod.resolve({"spread": "", "density": ""})["density"] == 70, "空串 → 默认浓度 70")
    check(mod.resolve({"spread": -5, "density": 999})["spread"] == 0, "负数 → 0")
    check(mod.resolve({"spread": 999, "density": 999})["density"] == 100, "浓度 >100 → 100")
    check(mod.resolve({"spread": 9999, "density": 50})["spread"] == 300, "扩散 >300 → 300")
    d = out_dir("cap")
    code, out, err = run({"color": color, "spread": 9999, "density": 999}, {"image1": src}, d)
    check(code == 0 and "被夹到 300" in out and "被夹到 100" in out, "日志里说明了被夹住的值")

    # ---- 8. 错误分支 ----
    print("\n[错误分支]")
    d = out_dir("err")
    code, out, err = run({"color": color}, {"image1": None}, d)
    check(code == 3, f"没连图 → 退出码 3（实际 {code}）")
    check("一个输入口都没连图" in err, "stderr 说明没连图")
    code, out, err = run({"color": "不是颜色", "spread": 10, "density": 50}, {"image1": src}, d)
    check(code == 3, f"颜色不合法 → 退出码 3（实际 {code}）")
    p = subprocess.run(
        [PY, SCRIPT],
        input=json.dumps({"params": {}, "inputs": {"image1": src}}),
        capture_output=True, text=True, encoding="utf-8", cwd=d,
    )
    check(p.returncode == 1 and "output_dir" in p.stderr, "缺 output_dir → 退出码 1")

    # ---- 9. manifest 与脚本对得上 ----
    print("\n[manifest]")
    man = json.load(open(MANIFEST, encoding="utf-8"))
    check(man["id"] == "halo" and man["name"] == "晕染", "id / name")
    check(man["entry"] == "halo.py" and os.path.isfile(os.path.join(DIR, man["entry"])), "entry 指向存在的脚本")
    keys = [p["key"] for p in man["params"]]
    check(keys == ["color", "spread", "density"], f"参数：{keys}")
    spread_p = man["params"][1]
    check((spread_p["min"], spread_p["max"]) == mod.SPREAD_RANGE, "扩散区间与脚本一致")
    check((man["params"][2]["min"], man["params"][2]["max"]) == mod.DENSITY_RANGE, "浓度区间与脚本一致")
    check([p["key"] for p in man["outputs"]] == ["image"] and man["outputs"][0]["multi"] is True,
          "输出：单 key、multi")
    check(man["inputs"][0]["dynamic"] is True and man["inputs"][0]["max"] == 10, "输入：动态、最多 10 口")
    check(os.path.exists(os.path.join(base_tmp, os.path.basename(root))), "临时目录落在 runtime/tmp 下")

    print()
    if fails:
        print(f"FAILED {len(fails)} 项：")
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
