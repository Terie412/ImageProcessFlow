# -*- coding: utf-8 -*-
"""程序化描边圆角框节点：按参数**画**一张图标，而不是处理输入图（所以它没有输入口）。

固定特征：圆角 + 单调色填充 + 一笔描边。可调的是宽高、圆角半径、描边宽度、主体色、描边色。

## 几何口径

- **画布就是「宽 × 高」**，没有额外留白；描边**完全画在框内**（外沿贴着图片边缘），
  所以任何尺寸下都不会被裁掉，拿来当图标素材正合适。
- 「圆角」说的是**外圆角**，**横轴、竖轴各一个半径**：两个相等就是普通圆角，不相等就是
  **椭圆角**（转角比正圆更扁/更长）。内圆角自动收成 `半径 − 描边宽度`，这样描边看着才是
  等宽的；两个半径各自被**它那一条边的一半**夹住（横轴上限于宽的一半，竖轴上限于高的一半），
  任一个填 0 就是直角。
- 背景全透明，输出**一律 PNG**（只有 PNG 存得住透明）。

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
      "params": {
        "width": 512, "height": 512, "radiusX": 96, "radiusY": 96, "strokeWidth": 12,
        "fillColor": "#ffffff", "strokeColor": "#111111"
      },
      "inputs": {},
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import math
import os
import re
import sys

from PIL import Image, ImageDraw

# 参数区间（与 manifest.json 里的 min/max 对齐）。越界**夹住**、不报错 ——
# 手改过 graph.json 时夹住比失败友好（同 DESIGN.md §3.14 色彩平衡节点的口径）。
SIZE_RANGE = (16, 2048)
RADIUS_RANGE = (0, 1024)
STROKE_RANGE = (0, 128)

# 抗锯齿用超采样：按这个上限算倍率（工作画布最长边不超过 WORK_MAX）。
# 512 的图标 → 4×，2048 → 2×，再大就 1×（PIL 的圆角是硬边的，不采样会有锯齿）。
WORK_MAX = 4096
MAX_SCALE = 4

DEFAULTS = {
    "width": 512,
    "height": 512,
    "radiusX": 96,
    "radiusY": 96,
    "strokeWidth": 12,
    "fillColor": "#ffffff",
    "strokeColor": "#111111",
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


def hex_to_rgb(v, dflt: tuple[int, int, int]) -> tuple[int, int, int]:
    """'#RRGGBB' / '#RGB' / 'RRGGBB' → (r, g, b)。

    空值退回默认色；**写了个不合法的颜色则报错**（同单色替换节点）—— 那是手改过
    graph.json 才会有的情况，静默换色比报错更难查。
    """
    s = str(v if v is not None else "").strip().lstrip("#")
    if not s:
        return dflt
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError(f"颜色不合法：{v!r}（要 #RRGGBB，比如 #111111）")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def resolve(params: dict) -> dict:
    """把参数解析成**实际生效**的那一串值（兜底与夹取之后）。

    `render()` 和 `main()` 的日志都走这里 —— 两处各算一遍的话，
    「日志说 96，画出来是 256」这类漂移迟早出现（同 nodespec.py 的教训）。
    """
    p = params or {}
    w = _int(p.get("width"), DEFAULTS["width"], SIZE_RANGE)
    h = _int(p.get("height"), DEFAULTS["height"], SIZE_RANGE)
    half_w, half_h = w // 2, h // 2
    # 两个圆角半径各自被**它那一条边**的一半夹住：横轴半径的上限是宽的一半，
    # 竖轴是高的一半（圆角这时才允许比短边的一半更大 —— 那正是椭圆角的意义）
    rx = min(_int(p.get("radiusX"), DEFAULTS["radiusX"], RADIUS_RANGE), half_w)
    ry = min(_int(p.get("radiusY"), DEFAULTS["radiusY"], RADIUS_RANGE), half_h)
    # 描边的上限是**短边**的一半：再厚就没有「框内」可画了
    sw = min(_int(p.get("strokeWidth"), DEFAULTS["strokeWidth"], STROKE_RANGE), min(half_w, half_h))
    return {
        "w": w,
        "h": h,
        "rx": rx,
        "ry": ry,
        "sw": sw,
        "fill": hex_to_rgb(p.get("fillColor"), hex_to_rgb(DEFAULTS["fillColor"], (255, 255, 255))),
        "stroke": hex_to_rgb(p.get("strokeColor"), hex_to_rgb(DEFAULTS["strokeColor"], (17, 17, 17))),
    }


# 每段四分之一椭圆采样多少段。最大弦高误差 ≈ r·π²/(32n²)：n=32 时，即使工作半径
# 到 2048（那是 2048 的图标 1× 渲染），也只有 ~1 个工作像素 —— 再经过盒式降采样，
# 落到成品上不到 0.3px，看不出来。
ARC_STEPS = 32


def _corner_mask(size: tuple[int, int], box: tuple[float, float, float, float],
                 rx: float, ry: float) -> Image.Image:
    """在 `size` 画布上、`box` 矩形内画一个圆角矩形蒙版，四角是**四分之一椭圆**。

    为什么不用 PIL 的 `ImageDraw.rounded_rectangle`：它只认圆角（横竖一个半径），
    做不了椭圆角。这里按参数方程采样四段弧拼成多边形 —— 横轴 rx、竖轴 ry 各自独立，
    任一个是 0 就退化成直角，两个都是 0 就是纯矩形。

    `box` 用的是**连续坐标**（x1/y1 是外沿，不是最右那个像素的下标）：与缩放、
    描边内缩都对得上，不必再各自 ±1 去凑。
    """
    W, H = size
    x0, y0, x1, y1 = box
    rx = max(0.0, min(float(rx), (x1 - x0) / 2))
    ry = max(0.0, min(float(ry), (y1 - y0) / 2))
    pts: list[tuple[float, float]] = []
    # 从左上角弧的起点（左边中点）开始，顺时针绕一圈。y 轴向下，所以 180° 在左、270° 在上
    for cx, cy, a0 in (
        (x0 + rx, y0 + ry, math.pi),          # 左上：180° → 270°
        (x1 - rx, y0 + ry, math.pi * 1.5),    # 右上：270° → 360°
        (x1 - rx, y1 - ry, 0.0),              # 右下：0° → 90°
        (x0 + rx, y1 - ry, math.pi * 0.5),    # 左下：90° → 180°
    ):
        for i in range(ARC_STEPS + 1):
            a = a0 + (math.pi / 2) * (i / ARC_STEPS)
            pts.append((round(cx + rx * math.cos(a)), round(cy + ry * math.sin(a))))

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    return mask


def render(params: dict) -> Image.Image:
    """按参数画一张图标（RGBA，透明底）。**纯函数**：不读文件、不写文件、不打印。"""
    g = resolve(params)
    w, h, rx, ry, sw = g["w"], g["h"], g["rx"], g["ry"], g["sw"]
    fill, stroke = g["fill"], g["stroke"]

    # ---- 超采样：先按倍率画两张蒙版，最后把**蒙版**缩回来 ----
    # 缩蒙版（单通道 L）而不是缩合成好的 RGBA：快得多，色彩只在这之后填一次。
    # 倍率按**工作画布不超过 WORK_MAX** 算：512 的图标 4×、2048 的 2×、再大就 1×。
    # （多边形/椭圆的边是硬的，不超采样会有锯齿）
    scale = max(1, min(MAX_SCALE, WORK_MAX // max(w, h)))
    W, H = w * scale, h * scale
    RX, RY, SW = rx * scale, ry * scale, sw * scale

    # 外沿 = 整个画布的椭圆角矩形；内沿 = 往内缩一个描边宽度、两个半径同步收小
    # （这样描边看着才是等宽的）
    outer = _corner_mask((W, H), (0, 0, W, H), RX, RY)
    inner = None
    if SW > 0 and W - 2 * SW > 0 and H - 2 * SW > 0:
        inner = _corner_mask((W, H), (SW, SW, W - SW, H - SW),
                             max(0.0, RX - SW), max(0.0, RY - SW))

    if scale > 1:
        # BOX 而不是 LANCZOS：倍率是整数，BOX 就是把每个小块的覆盖率取平均 ——
        # 正是超采样想要的盒式滤波。LANCZOS 的振铃会让描边内侧渗进主体色
        # （不透明的像素 alpha 掉到 239、描边里侧暗一圈），落在这种硬边界上纯属负作用。
        outer = outer.resize((w, h), Image.Resampling.BOX)
        if inner is not None:
            inner = inner.resize((w, h), Image.Resampling.BOX)

    if inner is None:
        # 没有描边：整块都是主体色，透不透明只由外沿决定
        img = Image.new("RGBA", (w, h), (*fill, 255))
    else:
        # 底铺**描边色**（它是最外侧那个可见色），再把主体色按内沿蒙版压上去 ——
        # **一次**混合，边界像素就是两色按覆盖率线性相加。分成「先压主体色、再压描边」
        # 两次的话主体色会被乘成平方（描边内侧发暗）。
        # 顺带把全透明像素的 RGB 留成描边色：下游忽略 alpha 直接采样时不会压出黑边。
        img = Image.new("RGBA", (w, h), (*stroke, 255))
        img.paste(fill, (0, 0), inner)
    img.putalpha(outer)  # alpha 只认外沿：里外两层都是不透明的，透明只发生在轮廓上
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
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"绘制失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # 日志里给的是**夹取之后**的值：填超了（圆角半径超过那条边的一半 / 描边超过短边的一半）
    # 时从这儿能一眼看出来是被夹住了，而不是「填了没生效」
    req_rx = _int(params.get("radiusX"), DEFAULTS["radiusX"], RADIUS_RANGE)
    req_ry = _int(params.get("radiusY"), DEFAULTS["radiusY"], RADIUS_RANGE)
    req_sw = _int(params.get("strokeWidth"), DEFAULTS["strokeWidth"], STROKE_RANGE)
    corner = f"{g['rx']} × {g['ry']}px" + ("（圆角）" if g["rx"] == g["ry"] else "（椭圆角）")
    capped = "，被各自的边长一半夹住" if (g["rx"] != req_rx or g["ry"] != req_ry) else ""
    log(f"[尺寸] {g['w']} × {g['h']}")
    log(f"[圆角] {corner}{capped}")
    log(f"[描边] {g['sw']}px  {_hex(g['stroke'])}"
        + (f"（要的是 {req_sw}，被短边一半夹住）" if g["sw"] != req_sw else "")
        + ("（无描边）" if g["sw"] == 0 else ""))
    log(f"[主体] {_hex(g['fill'])}")
    log("")

    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, "0.png")
    img.save(dest, "PNG", optimize=True)
    log(f"[输出] 0.png  {g['w']}×{g['h']}  PNG（透明底）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
