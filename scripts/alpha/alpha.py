# -*- coding: utf-8 -*-
"""Alpha 缩放节点：把每个像素的**透明通道乘以一个系数**（0 ~ 1）。

只在 alpha 上做一次乘法，RGB 一个字节都不动：
`A' = round(A × k)`。所以 k = 1 是原样、k = 0.5 是「整体半透明」、
k 越小越透。**不是**「把图染成半透明色」——透明区域里原本的颜色仍然留在
RGB 里，只是被 alpha 挡住而已。

没透明通道的图（JPEG / BMP、以及不带 transparency 的 PNG）**先转成 PNG
再乘** —— 这类图的透明通道视为全 255，所以乘完得到一张「整图统一 k 透明度」
的 PNG。因此这个节点的输出**一律是 PNG**：它的产物本身就是带 alpha 的，
换别的格式 alpha 会被丢掉。

多输入：有几个输入口连了图就产出几张，每张图的处理相互独立
（第 1 个连了图的口 → `0.png`，第 2 个 → `1.png`…）。某个口没连图就跳过，
所以输出序号是**紧凑**的，中间不会被空槽占位。
手机照片的 EXIF 旋转标记会先被摆正（`exif_transpose`），和其他节点一致。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {"alpha": 0.5}, "inputs": {...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     { "alpha": 0.5 },
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


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def read_alpha(v) -> float:
    """把参数收敛成 0~1 的浮点数；不合法就抛错（错误消息直接给用户看）。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError("Alpha 没填 —— 给一个 0~1 之间的小数（例如 0.5）")
    try:
        k = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"Alpha 不是数字：{v!r}")
    if k != k:  # NaN
        raise ValueError("Alpha 不是数字")
    if not 0.0 <= k <= 1.0:
        raise ValueError(f"Alpha 要在 0~1 之间（当前 {k}）")
    return k


def is_translucent(im: Image.Image) -> bool:
    """源图是否本来就带透明通道 —— 只影响日志措辞，输出一律 PNG。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def scale_one(path: str, dest_dir: str, idx: int, k: float) -> None:
    """处理一张图：alpha 逐个像素 × k，写到 dest_dir 里的 `{idx}.png`。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        had_alpha = is_translucent(im)

    rgba = im.convert("RGBA")  # P + transparency 也在这里转干净
    r, g, b, a = rgba.split()
    if k < 1.0:
        # int(x + 0.5) 而不是 round()：Python 的 round 是「银行家舍入」，
        # 0.5 会被舍到偶数，逐像素跑下来会偏暗
        a = a.point(lambda v: int(v * k + 0.5))

    dest = os.path.join(dest_dir, f"{idx}.png")
    Image.merge("RGBA", (r, g, b, a)).save(dest, "PNG", optimize=True)

    src_note = "原有 alpha" if had_alpha else "无 alpha，先转 PNG 再乘"
    log(f"  #{idx + 1}  {os.path.basename(path)}  {rgba.size[0]}×{rgba.size[1]}  "
        f"{src_fmt} → PNG  Alpha ×{k:g}  ({src_note})")


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
        k = read_alpha(params.get("alpha"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(key, inputs[key]) for key in sorted(inputs, key=port_order) if inputs.get(key)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    log(f"[Alpha] ×{k:g}" + ("（原样，alpha 不变）" if k == 1.0 else ""))
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            scale_one(path, out_dir, i, k)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
