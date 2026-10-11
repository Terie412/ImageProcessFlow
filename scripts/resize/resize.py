# -*- coding: utf-8 -*-
"""调整分辨率节点：把每张输入图**强制拉伸**到「分辨率向量」(宽, 高)。

一个节点可以接多张图，但只填**一个**分辨率向量 —— 所有输入图都被拉到同一个尺寸。
宽高比和向量不一致时画面会拉伸变形，这是刻意的：给什么坐标就得到什么尺寸。

有几个输入口连了图，就产出几张，序号按输入口的先后顺序对齐
（第 1 个连了图的口 → `0.png`，第 2 个 → `1.png`…）。某个口没连图就跳过，
所以输出序号是**紧凑**的，中间不会被空槽占位。

输出格式**跟随原图**（JPEG 进 JPEG 出、PNG 进 PNG 出、WEBP / BMP 同理）。
两个例外：
  - 源图带 alpha（透明）时必须存 PNG，否则透明信息会丢；
  - 不认识的格式（GIF / TIFF 等）一律退化成 PNG。
手机照片的 EXIF 旋转标记会先被摆正（`exif_transpose`），否则换格式后图会躺倒。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {...}, "inputs": {"image1": 路径或 null, ...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.jpg…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     { "width": 1024, "height": 1024 },
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

MAX_SIDE = 16384          # 单边上限（与 manifest 里 width/height 的 max 保持一致）
MAX_PIXELS = 100_000_000  # 总面积上限，挡住「16384×16384」这类会把内存吃光的输入

# 能原样保留的格式 → (PIL 格式名, 文件扩展名)；不在表里的退化成 PNG
KEEP_FORMAT: dict[str, tuple[str, str]] = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"),
}

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


def log(msg: str = "") -> None:
    print(msg, flush=True)


def pos_int(v, name: str) -> int:
    """把参数值收敛成正整数；不合法就抛错（错误消息直接给用户看）。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"{name}没填 —— 分辨率向量要同时给出宽和高")
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        raise ValueError(f"{name}不是数字：{v!r}")
    if n < 1:
        raise ValueError(f"{name}必须是正整数，当前是 {n}")
    if n > MAX_SIDE:
        raise ValueError(f"{name}太大了（{n}），上限 {MAX_SIDE}")
    return n


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def target_format(im: Image.Image, src_format: str) -> tuple[str, str, bool]:
    """决定输出格式。返回 (PIL 格式名, 扩展名, 是否带 alpha)。

    `src_format` 必须由调用方在**打开时**取好传进来 —— `ImageOps.exif_transpose`
    返回的是副本，`im.format` 会变成 None，靠它判断格式会一律退化成 PNG。
    """
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if alpha:
        return "PNG", ".png", True
    pil_fmt, ext = KEEP_FORMAT.get(src_format.upper(), ("PNG", ".png"))
    return pil_fmt, ext, False


def save(im: Image.Image, pil_fmt: str, alpha: bool, dest: str) -> None:
    if pil_fmt == "JPEG":
        im.convert("RGB").save(dest, "JPEG", quality=95, subsampling=0, optimize=True)
    elif pil_fmt == "WEBP":
        im.save(dest, "WEBP", quality=95, method=6)
    elif pil_fmt == "BMP":
        im.convert("RGB").save(dest, "BMP")
    else:  # PNG
        if alpha and im.mode != "RGBA":
            im = im.convert("RGBA")
        im.save(dest, "PNG", optimize=True)


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

    # ---- 分辨率向量 ----
    try:
        tw = pos_int(params.get("width"), "宽")
        th = pos_int(params.get("height"), "高")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3
    if tw * th > MAX_PIXELS:
        print(
            f"分辨率 {tw}×{th} 太大了（{tw * th / 1e6:.0f} 百万像素，上限 {MAX_PIXELS // 1000000} 百万）",
            file=sys.stderr,
        )
        return 3

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(k, inputs[k]) for k in sorted(inputs, key=port_order) if inputs.get(k)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    log(f"[分辨率向量] {tw}×{th}")
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        if not os.path.isfile(path):
            print(f"输入图片不存在: {path}", file=sys.stderr)
            return 1
        try:
            with Image.open(path) as opened:
                src_fmt = (opened.format or "").upper() or "?"
                im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
                pil_fmt, ext, alpha = target_format(im, src_fmt)
                ow, oh = im.size
                dest = os.path.join(out_dir, f"{i}{ext}")
                src_ext = os.path.splitext(path)[1].lower()
                ext_ok = src_ext == ext or (ext == ".jpg" and src_ext == ".jpeg")

                if (ow, oh) == (tw, th) and ext_ok:
                    # 尺寸没变、格式也一样 → 直接复制原文件，不做二次编码
                    # （JPEG 重编码是有损的，没必要为了"跑一遍"把品质磨掉）
                    shutil.copyfile(path, dest)
                    log(f"  #{i + 1}  {os.path.basename(path)}  {ow}×{oh} 尺寸未变，直接复制 → {i}{ext}")
                    continue

                out = im.resize((tw, th), LANCZOS)
                save(out, pil_fmt, alpha, dest)
                tag = f"{src_fmt}→{pil_fmt}" if src_fmt != pil_fmt else pil_fmt
                log(f"  #{i + 1}  {os.path.basename(path)}  {ow}×{oh} → {tw}×{th}  {tag}")
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
