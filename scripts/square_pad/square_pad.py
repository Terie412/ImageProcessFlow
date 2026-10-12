# -*- coding: utf-8 -*-
"""正方形扩展节点：把输入图的**短边两侧居中补全透明**，使图片变成正方形。

例如 50×100 的图：短边是宽，往左右各补 25 像素的全透明 → 输出 100×100。
补出来的区域 alpha=0、RGB 也是 0，不往透明区域渗任何颜色。
补边发生在**长边决定的那一侧轴**上：宽 < 高就左右补，高 < 宽就上下补，
另一轴的坐标全部不动。

多输入：有几个输入口连了图就产出几张，每张图的处理相互独立
（第 1 个连了图的口 → `0.png`，第 2 个 → `1.png`…）。某个口没连图就跳过，
所以输出序号是**紧凑**的，中间不会被空槽占位。

输出规则：
  - 本来就是正方形 → 原文件直接复制，不做二次编码（JPEG 重编码是有损的）；
  - 需要补边 → 输出一律是 PNG：透明通道 JPEG / BMP 存不了，WEBP 虽然能存
    但格式跟着变了容易让下游意外，统一 PNG 最不容易踩坑。
手机照片的 EXIF 旋转标记会先被摆正（`exif_transpose`），和调整分辨率节点一致。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {}, "inputs": {"image1": 路径或 null, ...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     {},
      "inputs":     { "image1": "/abs/workspace/images/A.png", "image2": null },
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

from PIL import Image, ImageOps

MAX_SIDE = 16384          # 单边上限（补完也不会超过长边，这里只拦异常输入）
MAX_PIXELS = 100_000_000  # 总面积上限，与调整分辨率节点一致


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def pad_one(path: str, dest_dir: str, idx: int) -> None:
    """处理一张图：短边两侧居中补透明成正方形，写进 dest_dir。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        ow, oh = im.size

    side = max(ow, oh)
    if side > MAX_SIDE:
        raise ValueError(f"长边 {side} 超过上限 {MAX_SIDE}")
    if side * side > MAX_PIXELS:
        raise ValueError(
            f"扩展成 {side}×{side} 太大了（{side * side / 1e6:.0f} 百万像素，"
            f"上限 {MAX_PIXELS // 1000000} 百万）"
        )

    if ow == oh:
        # 已经是正方形：原样复制，不做二次编码
        src_ext = os.path.splitext(path)[1].lower()
        ext = src_ext if src_ext else ".png"
        shutil.copyfile(path, os.path.join(dest_dir, f"{idx}{ext}"))
        log(f"  #{idx + 1}  {os.path.basename(path)}  {ow}×{oh}  {src_fmt}  已是正方形，原样输出")
        return

    # ---- 补边量：短边两侧对半分；总和是奇数时左边/上边少 1 像素 ----
    pad = side - min(ow, oh)
    before = pad // 2
    after = pad - before

    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    src = im.convert("RGBA")  # P 模式带 transparency 的图也要转干净
    axis = ""
    if ow < oh:
        out.paste(src, (before, 0))
        axis = f"左 {before} px + 右 {after} px"
    else:
        out.paste(src, (0, before))
        axis = f"上 {before} px + 下 {after} px"

    dest = os.path.join(dest_dir, f"{idx}.png")
    out.save(dest, "PNG", optimize=True)
    log(f"  #{idx + 1}  {os.path.basename(path)}  {ow}×{oh}  {src_fmt}  补边 {axis} → {side}×{side} PNG")


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

    inputs = payload.get("inputs") or {}
    out_dir = payload.get("output_dir")
    if not out_dir:
        print("payload 缺少 output_dir", file=sys.stderr)
        return 1

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(k, inputs[k]) for k in sorted(inputs, key=port_order) if inputs.get(k)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            pad_one(path, out_dir, i)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
