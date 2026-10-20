# -*- coding: utf-8 -*-
"""高斯模糊节点：整张图做高斯模糊，只有一个参数「强度」（= 模糊半径，像素）。

## 口径

- **强度就是 `ImageFilter.GaussianBlur` 的 radius**（像素）：0 = 原样不模糊，
  数字越大越糊。填超了**夹住**（0~200），不报错 —— 手改过 graph.json 时夹住比失败友好。
- **Alpha 也一起被模糊**：RGBA 上 PIL 是逐通道糊的，alpha 跟着变软。这是刻意的 ——
  模糊一个带透明边的图标时，你多半也要软边；只想糊内容、保住硬边的话先垫底色。
  代价是**透明区的 RGB 会被混进来**（如果透明像素存的是黑色，边缘会有一圈偏暗）：
  这种情况先垫底色再糊，或者别指望它自动帮你处理预乘。
- **输出格式**：带 Alpha 一律 PNG（别的格式存不住透明）；不带 Alpha **跟随原图**
  （JPEG / WEBP / BMP / PNG），不认识的格式退化成 PNG。
- **多输入**：有几个口连了图就产出几张，输出序号与输入口顺序对齐（同其它处理节点）。

节点脚本契约（见 DESIGN.md §2.5）：

    stdin  ← 一行 JSON: {"params": {...}, "inputs": {...}, "output_dir": 目录}
    stdout → 日志（原样显示在节点上）
    产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
    成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:

    {
      "params": {"strength": 8},
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

# 强度（= 模糊半径，px）的区间，与 manifest 的 min/max 对齐
STRENGTH_RANGE = (0, 200)

# 能原样保留的格式 → (PIL 格式名, 文件扩展名)；不在表里的退化成 PNG
KEEP_FORMAT: dict[str, tuple[str, str]] = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"),
}

DEFAULTS = {"strength": 8}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


def _int(v, dflt: int, rng: tuple[int, int]) -> int:
    """取一个整数并夹进区间；拿不到数字（空串 / None / 乱填）就用默认值。

    空串是**正常输入**：界面上把数字框清空时前端发的就是 `''`，
    这时用默认值比报错友好（和描边圆角框 / 蒙版制作同一个口径）。
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
    """把参数解析成**实际生效**的值（兜底与夹取之后）—— 日志和执行走同一份。"""
    p = params or {}
    return {"strength": _int(p.get("strength"), DEFAULTS["strength"], STRENGTH_RANGE)}


def is_translucent(im: Image.Image) -> bool:
    """源图有没有**实际的**透明通道（看 mode，不看后缀 —— RGB 模式的 PNG 算没有）。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def blur_one(path: str, dest_dir: str, idx: int, radius: int) -> None:
    """处理一张图：高斯模糊后写进 dest_dir，文件名主干是 {idx}。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        # 格式必须在**close 之前**取：exif_transpose 返回的是副本，im.format 会是 None
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        alpha_src = is_translucent(im)

    out = im.convert("RGBA") if alpha_src else im.convert("RGB")
    if radius > 0:
        out = out.filter(ImageFilter.GaussianBlur(radius))

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
            out.save(dest, "PNG", optimize=True)
            pil_fmt = "PNG"
        fmt = pil_fmt

    size = im.size
    log(f"  #{idx + 1}  {os.path.basename(path)}  {size[0]}×{size[1]}  "
        f"{src_fmt} → {fmt}  强度 {radius}"
        + ("（原样，不模糊）" if radius == 0 else "")
        + ("（alpha 也一起模糊）" if alpha_src and radius > 0 else ""))


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

    g = resolve(params)
    radius = g["strength"]
    # 日志里要说明「填的」和「用的」不一样 —— 那多半是被区间夹住了，不是界面坏了
    try:
        asked = float(params.get("strength"))
    except (TypeError, ValueError):
        asked = None
    capped = asked is not None and round(asked) != radius

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(key, inputs[key]) for key in sorted(inputs, key=port_order) if inputs.get(key)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    log(f"[强度] {radius}px" + ("（原样，不模糊）" if radius == 0 else "")
        + (f"（填的是 {params.get('strength')!r}，被夹到 {radius}）" if capped else ""))
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            blur_one(path, out_dir, i, radius)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
