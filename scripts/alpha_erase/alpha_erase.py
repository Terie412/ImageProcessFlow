# -*- coding: utf-8 -*-
"""按透明度抹除节点：把 alpha **低于阈值**的像素的 RGB 全部置 0。

它解决的是一个很具体的脏东西：带透明的图（尤其是 Qwen 的透明输出）在 alpha≈0 的地方
往往留着一层**杂色填充**（实测是紫红色 `#9C37C5` 那类）。那层颜色虽然被 alpha 挡着看不见，
但只要有人把图**压到一个不透明底色上**（白底、黑底）再看 —— 视觉模型就是这么干的 ——
就会看到一层「淡紫灰色」的底，进而把「透明背景」描述成一个不透明的浅色背景。

这个节点只动 RGB，**alpha 一个字节都不改**：`alpha < 阈值` 的像素 → `(0, 0, 0, alpha)`。
阈值单位是 **0~255**，和查看器里显示的 A 一致（默认 8 —— 刚好盖掉 alpha 0~1 的底噪）。

**没有 alpha 通道的图原样输出**（直接复制文件、不重新编码）。同样走原样复制的情形还有：
阈值 ≤ 0（没有像素会达标），以及整张图一个像素都没达标。

多输入：有几个输入口连了图就产出几张，逐张独立（第 1 个连了图的口 → `0.png`，第 2 个 →
`1.png`…）。没连图的口跳过，所以输出序号**紧凑**，中间不占空槽。
带 alpha 的图输出一律 PNG（只有 PNG 存得住透明）；会被处理的图必然带 alpha，故实际产出都是 PNG。
手机照片的 EXIF 旋转标记会先被摆正（`exif_transpose`），和其他节点一致。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {"threshold": 8}, "inputs": {...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     { "threshold": 8 },
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


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def has_alpha(im: Image.Image) -> bool:
    """源图**实际**有没有透明通道 —— 只看模式，不看文件格式。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def read_threshold(v) -> int:
    """把参数收敛成 0~255 的整数；不合法就抛错（错误消息直接给用户看）。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError("Alpha 阈值没填 —— 给一个 0~255 的整数")
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        raise ValueError(f"Alpha 阈值不是数字：{v!r}")
    if not 0 <= n <= 255:
        raise ValueError(f"Alpha 阈值要在 0~255 之间（当前 {n}）")
    return n


def _copy_as_is(path: str, dest_dir: str, idx: int) -> str:
    """原样复制输入，返回产出的文件名。"""
    ext = os.path.splitext(path)[1].lower() or ".png"
    dest = os.path.join(dest_dir, f"{idx}{ext}")
    shutil.copyfile(path, dest)
    return os.path.basename(dest)


def erase_one(path: str, dest_dir: str, idx: int, threshold: int) -> None:
    """处理一张图：alpha < threshold 的像素 RGB 置 0，写到 dest_dir 里的 `{idx}.png`。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        alpha = has_alpha(im)
        W, H = im.size

    base = f"  #{idx + 1}  {os.path.basename(path)}  {W}×{H}  {src_fmt}"

    if not alpha:
        name = _copy_as_is(path, dest_dir, idx)
        log(f"{base}  无 alpha 通道 → 原样复制 {name}")
        return

    if threshold <= 0:
        name = _copy_as_is(path, dest_dir, idx)
        log(f"{base}  阈值 0（没有像素低于它）→ 原样复制 {name}")
        return

    rgba = im.convert("RGBA")  # P + transparency 也在这里转干净
    r, g, b, a = rgba.split()

    # mask = 255 的位置就是「alpha 低于阈值」的像素
    mask = a.point(lambda v: 255 if v < threshold else 0)
    matched = mask.histogram()[255]
    if matched == 0:
        name = _copy_as_is(path, dest_dir, idx)
        log(f"{base}  没有像素低于阈值 {threshold} → 原样复制 {name}")
        return

    zero = Image.new("L", rgba.size, 0)
    r = Image.composite(zero, r, mask)  # mask=255 取前一个（全黑），否则保留原通道
    g = Image.composite(zero, g, mask)
    b = Image.composite(zero, b, mask)

    dest = os.path.join(dest_dir, f"{idx}.png")
    Image.merge("RGBA", (r, g, b, a)).save(dest, "PNG", optimize=True)
    log(f"{base}  → PNG  已抹 {matched} 个像素的 RGB（alpha < {threshold}，alpha 不变）")


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
        threshold = read_threshold(params.get("threshold"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(k, inputs[k]) for k in sorted(inputs, key=port_order) if inputs.get(k)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    log(f"[Alpha 阈值] {threshold}（低于它的像素 RGB 置 0）")
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            erase_one(path, out_dir, i, threshold)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
