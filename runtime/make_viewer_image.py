# -*- coding: utf-8 -*-
"""生成测试素材 runtime/viewer_big.png（3000×2000，四周透明、中间有图形）。

viewertest.js 用它验证大图查看器：「实际大小」会超出窗口、透明区能看出棋盘格。
素材是**固定的**，生成一次即可（和 seed.png 一样）。
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "viewer_big.png"


def main() -> None:
    im = Image.new("RGBA", (3000, 2000), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([200, 200, 2800, 1800], fill=(60, 120, 220, 255))
    d.ellipse([900, 500, 2100, 1500], fill=(240, 170, 40, 255))
    im.save(OUT)
    print(f"已生成 {OUT}  {im.size[0]}×{im.size[1]}  {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
