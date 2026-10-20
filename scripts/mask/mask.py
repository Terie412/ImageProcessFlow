# -*- coding: utf-8 -*-
"""蒙版制作节点：按参数**画**一张蒙版，而不是处理输入图（所以它没有输入口）。

固定特征：形状内**不透明纯白**、外面**完全透明**（RGBA PNG）。这样它可以直接当 alpha 用
—— 叠图、抠图、遮罩都行；要黑白实心图的话在别处反一下就行，不该在这里存两份口径。

## 几何口径

- **画布就是「宽 × 高」**，形状永远**画布居中**。
- 「横轴半径 / 竖轴半径」是**半轴**（不是直径）：
  - 矩形 → 总宽 = `2 × 横轴半径`、总高 = `2 × 竖轴半径`；
  - 椭圆形 → 就是标准椭圆的横竖半轴（两个不等就是扁/长的椭圆）。
  所以 512×512 的画布上填满整张，矩形要填 256 / 256。
- 半径超出画布**不报错、被裁**：矩形直接铺满那一条边；椭圆则是两端被切掉，
  只剩中间那条带（半径越大，上下边缘越平）—— 这是有意义的用法，别去夹成半张画布。
- 两组半径**各存各的**（`rectRX/rectRY` 与 `ellipseRX/ellipseRY`），切形状不会丢。
- 半径填 0 = 这个方向上没有面积，整张全透明（预览里看得见，不是 bug）。

## 实时预览（与执行同一份代码）

模块里除了执行入口，还提供一个纯函数 `render(params) -> PIL.Image`：服务端
`POST /api/processes/<id>/preview` 直接 import 本模块调它（见 `server/preview.py`），
所以节点上的缩略图 / 双击的大图，和点「执行」产出的那张图是**同一份代码、逐像素一致**。

> 因此 **import 时不能有副作用** —— 读 stdin、写文件都得留在 `if __name__ == "__main__":` 后面。

节点脚本契约（见 DESIGN.md §2.5）：

    stdin  ← 一行 JSON: {"params": {...}, "inputs": {}, "output_dir": 目录}
    stdout → 日志（原样显示在节点上）
    产出   → output_dir/0.png
    成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:

    {
      "params": {"width": 512, "height": 512, "shape": "rect",
                 "rectRX": 192, "rectRY": 192, "ellipseRX": 192, "ellipseRY": 192},
      "inputs": {},
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import os
import sys

from PIL import Image, ImageDraw

# 参数区间（与 manifest.json 里的 min/max 对齐）。越界**夹住**、不报错 ——
# 和描边圆角框 / 色彩平衡一个口径：手改过 graph.json 时夹住比失败友好。
SIZE_RANGE = (16, 2048)
# 半径上限给得比画布宽：椭圆要允许「横轴远大于画布」那种近乎直边的效果
RADIUS_RANGE = (0, 4096)

SHAPES = {"rect": "矩形", "ellipse": "椭圆形"}

# 抗锯齿用超采样：按这个上限算倍率（工作画布最长边不超过 WORK_MAX）。
# 512 → 4×，2048 → 2×，再大就 1×（PIL 画的形状是硬边的，不采样边缘会有锯齿）。
WORK_MAX = 4096
MAX_SCALE = 4

WHITE = (255, 255, 255)
DEFAULTS = {
    "width": 512,
    "height": 512,
    "shape": "rect",
    "rectRX": 192,
    "rectRY": 192,
    "ellipseRX": 192,
    "ellipseRY": 192,
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _int(v, dflt: int, rng: tuple[int, int]) -> int:
    """取一个整数并夹进区间；拿不到数字（空串 / None / 乱填）就用默认值。

    空串是**正常输入**：界面上把数字框清空时前端发的就是 `''`，这时用默认值
    比报错友好 —— 否则实时预览会在你打字的中途弹一个红色错误。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    if f != f:  # NaN
        return dflt
    lo, hi = rng
    return int(max(lo, min(hi, round(f))))


def resolve(params: dict) -> dict:
    """把参数解析成**实际生效**的那一串值（兜底与夹取之后）。

    `render()` 和 `main()` 的日志都走这里 —— 两处各算一遍的话，
    「日志说 192，画出来是 256」这类漂移迟早出现（同 nodespec.py 的教训）。
    """
    p = params or {}
    shape = p.get("shape")
    if shape not in SHAPES:
        shape = DEFAULTS["shape"]
    key_x, key_y = ("rectRX", "rectRY") if shape == "rect" else ("ellipseRX", "ellipseRY")
    return {
        "w": _int(p.get("width"), DEFAULTS["width"], SIZE_RANGE),
        "h": _int(p.get("height"), DEFAULTS["height"], SIZE_RANGE),
        "shape": shape,
        "rx": _int(p.get(key_x), DEFAULTS[key_x], RADIUS_RANGE),
        "ry": _int(p.get(key_y), DEFAULTS[key_y], RADIUS_RANGE),
    }


def render(params: dict) -> Image.Image:
    """按参数画一张蒙版（RGBA：形状内纯白不透明，外面全透明）。**纯函数**。

    做法：先在 L 通道上按倍率画形状，**把蒙版缩回来**，最后整体铺白、putalpha。
    缩单通道 L 比缩 RGBA 快得多，白也只需要填一次。
    """
    g = resolve(params)
    w, h, rx, ry = g["w"], g["h"], g["rx"], g["ry"]

    scale = max(1, min(MAX_SCALE, WORK_MAX // max(w, h)))
    W, H = w * scale, h * scale

    m = Image.new("L", (W, H), 0)
    # 半径 0 = 这个方向上没有面积：**什么都不画**（整张全透明）。
    # 交给 PIL 会炸 —— `ImageDraw` 要求 x1 >= x0，而半径 0 时右边界会算到左边界的左边。
    if rx > 0 and ry > 0:
        # 连续坐标 → 像素框：中心对齐，右/下边界**减一**，否则形状会宽出半个像素
        # （画布 512、半径 256 时正好铺满，不多不少）
        cx, cy = W / 2.0, H / 2.0
        box = [
            round(cx - rx * scale),
            round(cy - ry * scale),
            round(cx + rx * scale) - 1,
            round(cy + ry * scale) - 1,
        ]
        d = ImageDraw.Draw(m)
        if g["shape"] == "ellipse":
            d.ellipse(box, fill=255)
        else:
            d.rectangle(box, fill=255)

    if scale > 1:
        # BOX 而不是 LANCZOS：倍率是整数，BOX 就是把每个小块的覆盖率取平均 ——
        # 正是超采样想要的盒式滤波（LANCZOS 的振铃会在边缘外侧描出一圈假的灰）
        m = m.resize((w, h), Image.Resampling.BOX)

    # 先造一张全透明、RGB 已经是白的底，再把 alpha 换成蒙版。
    # 全透明区的 RGB 留白（而不是 0,0,0）：下游忽略 alpha 直接采样时不会压出黑边。
    img = Image.new("RGBA", (w, h), (*WHITE, 0))
    img.putalpha(m)
    return img


def main() -> int:
    try:  # Windows 控制台默认不是 utf-8，日志里的中文会炸
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    raw = sys.stdin.read()
    if not raw.strip():
        print("stdin 没有收到 JSON payload", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"payload 不是合法 JSON: {e}", file=sys.stderr)
        return 1

    params = payload.get("params") or {}
    out_dir = payload.get("output_dir")
    if not out_dir:
        print("payload 缺少 output_dir", file=sys.stderr)
        return 1

    try:
        g = resolve(params)
        img = render(params)
    except Exception as e:  # noqa: BLE001
        print(f"绘制失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # 日志里给的是**生效之后**的值：半径被夹住 / 形状超出画布时，从这儿一眼看得出
    # 是「填了没生效」，而不是界面坏了
    p = params or {}
    req = {k: _int(p.get(k), DEFAULTS[k], RADIUS_RANGE) for k in
           ("rectRX", "rectRY", "ellipseRX", "ellipseRY")}
    req_key = "rectRX" if g["shape"] == "rect" else "ellipseRX"
    req_key_y = "rectRY" if g["shape"] == "rect" else "ellipseRY"
    capped = "（填的是 %d，被上限夹住）" % req[req_key] if req[req_key] != g["rx"] else ""

    log(f"[尺寸] {g['w']} × {g['h']}")
    log(f"[形状] {SHAPES[g['shape']]}")
    log(f"[半径] 横 {g['rx']}px · 竖 {g['ry']}px{capped}")
    if g["rx"] <= 0 or g["ry"] <= 0:
        log("[注意] 有一个半径是 0 —— 这个方向上没有面积，整张蒙版是全透明的")
    elif g["rx"] * 2 > g["w"] or g["ry"] * 2 > g["h"]:
        log(f"[注意] 形状比画布大，超出部分被裁（画布 {g['w']}×{g['h']}）")
    log("")

    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, "0.png")
    img.save(dest, "PNG", optimize=True)
    log(f"[输出] 0.png  {g['w']}×{g['h']}  PNG（形状内纯白不透明，外面全透明）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
