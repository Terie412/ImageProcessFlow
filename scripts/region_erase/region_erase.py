# -*- coding: utf-8 -*-
"""区域抹除节点：把「抹除区域」数组里的东西**全都抹掉**，输出一张图。

抹成什么颜色，取决于**源图实际有没有 Alpha 通道**（与 recolor / resize / alpha
三个节点同一口径 —— 只看 `im.mode`，不看文件格式）：
  - 带 Alpha（RGBA / LA / P+transparency）→ 抹成**透明** `(0, 0, 0, 0)`，输出 PNG；
  - 不带 Alpha → 抹成**黑色** `(0, 0, 0)`，输出**跟随原图格式**（JPEG / WEBP / BMP…）。
之所以按「有没有 Alpha」而不是「格式支持不支持透明」判：一张 RGB 模式的 PNG 格式上
支持透明、但它本身并没有透明通道，若按格式判会凭空给它加一层透明 —— 那是个惊喜。

抹除是**硬边**的：直接把区域内的像素写成目标的纯色，不做羽化、不做抗锯齿。

区域：原图像素坐标、原点左上，可以重叠、不要求排序。越界会被**夹回**图像范围
（只抹交集）；某个区域夹完是空的（完全落在图外）就**跳过并记一条日志**，不报错 ——
正常操作下区域由界面的编辑器产出、本来就在图内（`RegionEditor` 的 clampRect），
越界只可能是手改了 graph.json。**区域为空**、或所有区域都被跳过时，直接**原样复制
输入**，不做二次编码（JPEG 重编码是有损的，没必要为了"跑一遍"磨掉品质）。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {"regions": [{"x":0,"y":0,"w":100,"h":50}, …]},
                       "inputs": {"image": 路径或 null}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png / 0.jpg…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     { "regions": [ {"x": 20, "y": 30, "w": 120, "h": 80} ] },
      "inputs":     { "image": "/abs/workspace/images/A.png" },
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from PIL import Image, ImageDraw, ImageOps

# 能原样保留的格式 → (PIL 格式名, 文件扩展名)；不在表里的退化成 PNG
KEEP_FORMAT: dict[str, tuple[str, str]] = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _int(v) -> int:
    return int(round(float(v)))


def has_alpha(im: Image.Image) -> bool:
    """源图**实际**是否带透明通道 —— 只看模式，不看文件格式。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def clamp_box(r: dict, W: int, H: int) -> tuple[int, int, int, int] | None:
    """把区域夹回图像范围，返回像素坐标 `(x0, y0, x1, y1)`（右开）。

    完全落在图外（夹完宽或高 < 1）时返回 None，由调用方跳过。
    """
    x, y = _int(r["x"]), _int(r["y"])
    w, h = _int(r["w"]), _int(r["h"])
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return x0, y0, x1, y1


def save_image(out: Image.Image, src_fmt: str, alpha: bool, dest_base: str) -> tuple[str, str]:
    """把处理好的图存进 `dest_base + ext`，返回 `(PIL 格式名, 扩展名)`。

    `out` 已按 `alpha` 转成 RGBA 或 RGB，这里只负责挑格式与编码参数。
    """
    if alpha:
        dest = f"{dest_base}.png"
        out.save(dest, "PNG", optimize=True)
        return "PNG", ".png"

    pil_fmt, ext = KEEP_FORMAT.get(src_fmt.upper(), ("PNG", ".png"))
    dest = f"{dest_base}{ext}"
    if pil_fmt == "JPEG":
        out.convert("RGB").save(dest, "JPEG", quality=95, subsampling=0, optimize=True)
    elif pil_fmt == "WEBP":
        out.save(dest, "WEBP", quality=95, method=6)
    elif pil_fmt == "BMP":
        out.convert("RGB").save(dest, "BMP")
    else:
        pil_fmt, ext = "PNG", ".png"
        out.convert("RGB").save(dest, "PNG", optimize=True)
    return pil_fmt, ext


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

    src = inputs.get("image")
    if not src or not os.path.isfile(src):
        print("没有连接输入图（左侧那个输入口还没接上图片）", file=sys.stderr)
        return 2

    regions = params.get("regions") or []
    if not isinstance(regions, list):
        print("regions 不是数组", file=sys.stderr)
        return 3

    os.makedirs(out_dir, exist_ok=True)
    src_ext = os.path.splitext(src)[1].lower() or ".png"

    with Image.open(src) as opened:
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        alpha = has_alpha(im)
        W, H = im.size

        # ---- 先把区域夹回图内，攒出有效框 ----
        boxes: list[tuple[int, int, int, int]] = []
        for i, r in enumerate(regions):
            if not isinstance(r, dict):
                print(f"  第 {i + 1} 个区域格式不对，已跳过：{r}")
                continue
            try:
                box = clamp_box(r, W, H)
            except (KeyError, TypeError, ValueError):
                print(f"  第 {i + 1} 个区域格式不对，已跳过：{r}")
                continue
            if box is None:
                print(f"  第 {i + 1} 个区域 {r.get('w')}×{r.get('h')} @({r.get('x')},{r.get('y')}) "
                      f"完全落在图像外（图 {W}×{H}），已跳过")
                continue
            boxes.append(box)

        # ---- 没有可抹除的框 → 原样复制输入，不做二次编码 ----
        if not boxes:
            why = "区域是空的" if not regions else "所有区域都落在图像外"
            dest = os.path.join(out_dir, f"0{src_ext}")
            shutil.copyfile(src, dest)
            log(f"[区域抹除] {why}，原样复制输入 → 0{src_ext}")
            log(f"  {os.path.basename(src)}  {W}×{H}  {src_fmt}（未改动）")
            log("")
            log("完成，共 1 张输出")
            return 0

        # ---- 逐框抹除 ----
        fill = (0, 0, 0, 0) if alpha else (0, 0, 0)
        work = im.convert("RGBA") if alpha else im.convert("RGB")
        draw = ImageDraw.Draw(work)
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            # rectangle 的右下角是**闭区间**，所以减 1；右开的 (x1, y1) 对应 x1-1 / y1-1
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=fill)

        fmt, _ext = save_image(work, src_fmt, alpha, os.path.join(out_dir, "0"))

    target = "透明 (0,0,0,0)" if alpha else "黑色 (0,0,0)"
    log(f"[区域抹除] 共 {len(boxes)} 个区域 → 抹成{target}"
        + ("（源图带 Alpha，输出 PNG）" if alpha else f"（源图无 Alpha，输出 {fmt}）"))
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        log(f"  #{i + 1}  抹除 ({x0},{y0}) ~ ({x1},{y1})  {x1 - x0}×{y1 - y0}")
    log("")
    log(f"  {os.path.basename(src)}  {W}×{H}  {src_fmt} → {fmt}")
    log(f"完成，共 1 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
