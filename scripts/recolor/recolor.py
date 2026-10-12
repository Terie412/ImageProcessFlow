# -*- coding: utf-8 -*-
"""单色替换节点：把图里**所有像素的 RGB 统一替换成指定颜色**，Alpha 原样保留。

输出是目标颜色的剪影 / 蒙版——比如给抠好的人物图换一个纯色，或者批量做
同色的图标层。每个像素的 RGB 都变成同一个值，不做明暗映射（要保留明暗
层次的话那是「调色」，不是这个节点干的事）。

多输入：有几个输入口连了图就产出几张，每张图的处理相互独立
（第 1 个连了图的口 → `0.png`，第 2 个 → `1.png`…）。某个口没连图就跳过，
所以输出序号是**紧凑**的，中间不会被空槽占位。

输出格式：
  - 带 Alpha（RGBA / LA / P+transparency）→ PNG（别的格式存不住透明）；
  - 不带 Alpha → 跟随原图格式（JPEG 进 JPEG 出、WEBP / BMP 同理）。
手机照片的 EXIF 旋转标记会先被摆正（`exif_transpose`），和其他节点一致。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {"color": "#RRGGBB"}, "inputs": {...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     { "color": "#ff8800" },
      "inputs":     { "image1": "/abs/workspace/images/A.png", "image2": null },
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import os
import re
import sys

from PIL import Image, ImageOps

# 能原样保留的格式 → (PIL 格式名, 文件扩展名)；不在表里的退化成 PNG
KEEP_FORMAT: dict[str, tuple[str, str]] = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def hex_to_rgb(v) -> tuple[int, int, int]:
    """'#RRGGBB' / '#RGB' / 'RRGGBB' → (r, g, b)；不合法就抛错（消息直接给用户看）。"""
    s = str(v if v is not None else "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError(f"颜色不合法：{v!r}（要 #RRGGBB，比如 #ff8800）")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def recolor_one(path: str, dest_dir: str, idx: int, rgb: tuple[int, int, int]) -> None:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        alpha_src = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)

    rgba = im.convert("RGBA")
    _r, _g, _b, a = rgba.split()

    out = Image.new("RGBA", rgba.size, (*rgb, 0))
    out.putalpha(a)

    if alpha_src:
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
            out.save(dest, "PNG", optimize=True)
            pil_fmt = "PNG"
        fmt = pil_fmt

    log(f"  #{idx + 1}  {os.path.basename(path)}  {rgba.size[0]}×{rgba.size[1]}  "
        f"{src_fmt} → {fmt}  全部像素 → #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")


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
        rgb = hex_to_rgb(params.get("color"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(k, inputs[k]) for k in sorted(inputs, key=port_order) if inputs.get(k)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    log(f"[颜色] #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            recolor_one(path, out_dir, i, rgb)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
