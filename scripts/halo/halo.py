# -*- coding: utf-8 -*-
"""晕染节点：按图的**透明区域**长出一圈跟着字形走的晕染，垫在原图下面。

## 干什么用

艺术字直接放到背景上常常看不清（浅字压浅底、深底压深字）。这个节点在字的
**周围**铺一层形态贴合字形的色雾，把字从背景里托出来；字本身的像素一个不改，
画布尺寸也不变 —— 出来的图直接叠到背景上就行（晕染已经合进去了，见下）。

## 口径

- **认的是 Alpha，不是亮度**：形状完全来自源图的 alpha 通道。所以**必须**喂透明底
  的图（抠好的艺术字 / 图标）。没有透明通道、或者 alpha 全不透明的图，晕染整块被
  原图盖住、等于白做 —— 这种情况日志里会点明，输出就是原图。
- **扩散范围**（px）= 晕染从字形边缘向外伸出去多远。0 = 不做任何晕染。实现是
  `GaussianBlur(spread × 0.55)` 再拿一条 smoothstep 曲线把模糊场重映射成
  「贴边满浓度、往外渐隐」，所以实测延伸距离 ≈ 填的数值（`runtime/verify_halo.py`
  里量过，10~300 都在 0.9~1.1 倍之间）。
- **浓度**（0~100%）= **最浓处**的实际不透明度。模糊场先按自己的峰值归一化，
  所以哪怕是很细的笔画，浓度 100 也能铺到满 —— 否则大扩散配细笔画的图，峰值会
  被稀释到几十，让人以为「浓度参数没生效」。
- **颜色**只影响晕染，原图像素不动。
- **合成**：晕染垫在**原图下面**（`alpha_composite(glow, src)`），所以字上不会蒙
  一层色；半透明的字会透出下面的晕染，和真实叠放一致。
- **边缘会被裁**：画布尺寸 = 原图尺寸，晕染伸出画布的部分就没了。要留边先用
  「调整分辨率」把画布做大。
- **输出**：源图带 Alpha → PNG（要存透明）；不带 → 跟随原图格式（JPEG / WEBP /
  BMP / PNG），不认识的退化成 PNG。
- **多输入**：有几个口连了图就产出几张，输出序号与输入口顺序对齐（同其它节点）。

节点脚本契约（见 DESIGN.md §2.5）：

    stdin  ← 一行 JSON: {"params": {...}, "inputs": {...}, "output_dir": 目录}
    stdout → 日志（原样显示在节点上）
    产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
    成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:

    {
      "params": {"color": "#ffffff", "spread": 24, "density": 70},
      "inputs": {"image1": "/abs/ws/canvases/c_x/images/A.png"},
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import os
import re
import sys

from PIL import Image, ImageFilter, ImageOps

# 参数区间，与 manifest 的 min/max 对齐
SPREAD_RANGE = (0, 300)
DENSITY_RANGE = (0, 100)

# 模糊半径 / 扩散范围。0.55 是量出来的：模糊场被下面的曲线截到 0 的位置，
# 正好落在「字形边缘往外 spread 像素」上（verify_halo.py 实测 0.9~1.1 倍）。
SIGMA_PER_SPREAD = 0.55

# 模糊场 → 晕染不透明度的分界（占 255 的比例）：
# 低于 LO 算晕染的外缘（全透明），高于 HI 算满浓度。贴边处模糊场约 0.5，
# 已经超过 HI，所以**紧挨字边就是满浓度**，往外才开始淡 —— 这是「晕染」
# 而不是「模糊笔刷」的关键。
FIELD_LO = 0.02
FIELD_HI = 0.45

# 能原样保留的格式 → (PIL 格式名, 文件扩展名)；不在表里的退化成 PNG
KEEP_FORMAT: dict[str, tuple[str, str]] = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"),
}

DEFAULTS = {"color": "#ffffff", "spread": 24, "density": 70}

# 蒙版表：晕染 alpha 为 0 → 255（取源图 RGB），其余 → 0
ZERO_MASK_LUT = [255] + [0] * 255


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def _int(v, dflt: int, rng: tuple[int, int]) -> int:
    """取一个整数并夹进区间；拿不到数字（空串 / None / 乱填）就用默认值。

    空串是**正常输入**：界面上把数字框清空时前端发的就是 `''`，这时用默认值
    比报错友好（和模糊 / 描边圆角框 / 蒙版制作同一个口径）。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    if f != f:  # NaN
        return dflt
    lo, hi = rng
    return int(max(lo, min(hi, round(f))))


def hex_to_rgb(v) -> tuple[int, int, int]:
    """'#RRGGBB' / '#RGB' / 'RRGGBB' → (r, g, b)；不合法就抛错（消息直接给用户看）。"""
    s = str(v if v is not None else "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError(f"颜色不合法：{v!r}（要 #RRGGBB，比如 #ff8800）")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def resolve(params: dict) -> dict:
    """把参数解析成**实际生效**的值（兜底与夹取之后）—— 日志和执行走同一份。"""
    p = params or {}
    return {
        "color": hex_to_rgb(p.get("color") or DEFAULTS["color"]),
        "spread": _int(p.get("spread"), DEFAULTS["spread"], SPREAD_RANGE),
        "density": _int(p.get("density"), DEFAULTS["density"], DENSITY_RANGE),
    }


def alpha_lut(density: int) -> list[int]:
    """模糊场（0~255）→ 晕染不透明度（0~255）的查找表：smoothstep + 乘浓度。"""
    lo, hi = FIELD_LO * 255.0, FIELD_HI * 255.0
    out: list[int] = []
    for v in range(256):
        t = (v - lo) / (hi - lo)
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        s = t * t * (3.0 - 2.0 * t)  # smoothstep：两头切线为 0，不会出现硬边
        out.append(int(round(s * density * 255.0 / 100.0)))
    return out


def halo_alpha(alpha: Image.Image, spread: int, density: int) -> Image.Image:
    """由源图 alpha 算出晕染的不透明度蒙版（单通道 L）。"""
    if spread <= 0 or density <= 0:
        return Image.new("L", alpha.size, 0)

    field = alpha.filter(ImageFilter.GaussianBlur(spread * SIGMA_PER_SPREAD))

    # 按峰值归一化：让「浓度」永远等于最浓处的实际不透明度。细笔画 / 大扩散的
    # 图模糊后峰值会被稀释，不归一化的话浓度 100 也铺不满，像参数失灵。
    peak = field.getextrema()[1]
    if 0 < peak < 255:
        field = field.point(lambda v: min(255, round(v * 255 / peak)))

    return field.point(alpha_lut(density))


def halo_one(path: str, dest_dir: str, idx: int, rgb: tuple[int, int, int],
             spread: int, density: int) -> None:
    """处理一张图：算晕染、垫到原图下面、写进 dest_dir，文件名主干是 {idx}。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        # 格式必须在**close 之前**取：exif_transpose 返回的是副本，im.format 会是 None
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        alpha_src = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)

    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    a_min, a_max = alpha.getextrema()
    # 全不透明 = 没有形状可认（RGB 图、或者 alpha 铺满的 PNG 都算）
    shapeless = a_min == 255 or a_max == 0

    glow_alpha = halo_alpha(alpha, spread, density)
    if spread > 0 and density > 0:
        glow = Image.new("RGBA", rgba.size, (*rgb, 0))
        glow.putalpha(glow_alpha)
        out = Image.alpha_composite(glow, rgba)  # 晕染在下面，原图在上
        # 合成会在「整片透明、晕染也没到」的像素上留下晕染色（alpha 仍是 0）。
        # 那些像素看不见，但下游按 RGB 采样的节点会被它们带偏 —— 还原成源图的。
        out = Image.composite(rgba, out, glow_alpha.point(ZERO_MASK_LUT))
    else:
        out = rgba  # 不晕染就一个像素都不动

    if alpha_src:
        # 有 alpha 只能存 PNG：JPEG / BMP 会把透明信息丢掉
        dest = os.path.join(dest_dir, f"{idx}.png")
        out.save(dest, "PNG", optimize=True)
        fmt = "PNG"
    else:
        pil_fmt, ext = KEEP_FORMAT.get(src_fmt, ("PNG", ".png"))
        dest = os.path.join(dest_dir, f"{idx}{ext}")
        if pil_fmt == "JPEG":
            out.convert("RGB").save(dest, "JPEG", quality=95, subsampling=0, optimize=True)
        elif pil_fmt == "WEBP":
            out.save(dest, "WEBP", quality=95, method=6)
        elif pil_fmt == "BMP":
            out.convert("RGB").save(dest, "BMP")
        else:
            out.convert("RGB").save(dest, "PNG", optimize=True)
            pil_fmt = "PNG"
        fmt = pil_fmt

    peak = glow_alpha.getextrema()[1]
    log(f"  #{idx + 1}  {os.path.basename(path)}  {rgba.size[0]}×{rgba.size[1]}  "
        f"{src_fmt} → {fmt}  晕染峰值不透明度 {peak}/255")
    if shapeless and spread > 0 and density > 0:
        log("      ↑ 这张图没有透明区域（或者整张全透明）—— 没形状可认，"
            "晕染要么被原图整个盖住、要么无处可长。喂透明底的艺术字 / 图标才有用。")


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
    inputs = payload.get("inputs") or {}
    out_dir = payload.get("output_dir")
    if not out_dir:
        print("payload 缺少 output_dir", file=sys.stderr)
        return 1

    try:
        g = resolve(params)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    spread, density = g["spread"], g["density"]
    rgb = g["color"]
    # 日志里要说明「填的」和「用的」不一样 —— 那多半是被区间夹住了，不是界面坏了
    def capped(key: str, used: int) -> bool:
        try:
            asked = float(params.get(key))
        except (TypeError, ValueError):
            return False
        return round(asked) != used

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(key, inputs[key]) for key in sorted(inputs, key=port_order) if inputs.get(key)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    notes = []
    if capped("spread", spread):
        notes.append(f"扩散填的是 {params.get('spread')!r}，被夹到 {spread}")
    if capped("density", density):
        notes.append(f"浓度填的是 {params.get('density')!r}，被夹到 {density}")
    log(f"[晕染] 颜色 #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}  扩散 {spread}px  浓度 {density}%"
        + (f"（{'；'.join(notes)}）" if notes else ""))
    if spread <= 0 or density <= 0:
        log("      扩散 0 或浓度 0 —— 不做晕染，原图原样输出")
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            halo_one(path, out_dir, i, rgb, spread, density)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
