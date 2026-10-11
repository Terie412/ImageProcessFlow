# -*- coding: utf-8 -*-
"""裁剪节点：按「裁剪区域」数组把输入图裁成多张。

每个区域一张图，命名 0.png、1.png…（序号 = 区域在数组里的下标，引擎据此绑定输出槽位）。
输出统一存 PNG：裁的多半是透明底素材，PNG 才能保住 alpha。

输入（stdin 一行 JSON）:
    {
      "params":     { "regions": [ {"x":0,"y":0,"w":100,"h":50}, ... ] },
      "inputs":     { "image": "/abs/workspace/images/A.png" },
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }

区域用的是**原图像素坐标**（原点左上）。超出图像的部分会被夹回来；
如果某个区域夹完还是空的，直接报错退出（不产出半套结果，旧输出也就不会被破坏）。
"""
from __future__ import annotations

import json
import os
import sys

from PIL import Image


def _int(v) -> int:
    return int(round(float(v)))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.readline() or "{}")
    except ValueError as e:
        print(f"入参不是合法 JSON：{e}", file=sys.stderr)
        return 2

    src = (payload.get("inputs") or {}).get("image")
    if not src or not os.path.isfile(src):
        print("没有连接输入图（左侧那个输入口还没接上图片）", file=sys.stderr)
        return 2

    regions = (payload.get("params") or {}).get("regions") or []
    if not isinstance(regions, list) or not regions:
        print("裁剪区域是空的 —— 先点「自动识别」，或在预览里双击手动添加区域", file=sys.stderr)
        return 3

    out_dir = payload["output_dir"]

    with Image.open(src) as im:
        im = im.convert("RGBA")
        W, H = im.size
        print(f"原图 {W}×{H}，共 {len(regions)} 个区域")

        boxes: list[tuple[int, int, int, int]] = []
        for i, r in enumerate(regions):
            try:
                x, y = _int(r["x"]), _int(r["y"])
                w, h = _int(r["w"]), _int(r["h"])
            except (KeyError, TypeError, ValueError):
                print(f"第 {i + 1} 个区域格式不对：{r}", file=sys.stderr)
                return 4
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(W, x + w), min(H, y + h)
            if x1 - x0 < 1 or y1 - y0 < 1:
                print(
                    f"第 {i + 1} 个区域 {w}×{h} @({x},{y}) 完全落在图像外（图只有 {W}×{H}）——"
                    "上游图可能换过了，请重新识别或编辑区域",
                    file=sys.stderr,
                )
                return 4
            if (x0, y0, x1, y1) != (x, y, x + w, y + h):
                print(f"  第 {i + 1} 个区域超出边界，已夹到 {(x0, y0, x1, y1)}")
            boxes.append((x0, y0, x1, y1))

        for i, box in enumerate(boxes):
            im.crop(box).save(os.path.join(out_dir, f"{i}.png"))
            print(f"  #{i + 1}  区域 {box[0]},{box[1]}  裁出 {box[2] - box[0]}×{box[3] - box[1]}")

    print(f"完成，共裁出 {len(boxes)} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
