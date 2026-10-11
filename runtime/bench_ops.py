# -*- coding: utf-8 -*-
"""把删除路径逐段拆开计时，找出那 400ms 到底花在哪。"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "server"))
from workspace import Workspace  # noqa: E402

ROOT = APP / "runtime" / "opbench"
SRC = APP / "workspace" / "images" / "E.png"
BIG = SRC.read_bytes()
print(f"测试图大小: {len(BIG)/1024/1024:.1f} MB")


def setup():
    shutil.rmtree(ROOT, ignore_errors=True)
    ws = Workspace(ROOT)
    ws.ensure()
    for nm in ["A", "B", "C", "D"]:
        (ws.images_dir / f"{nm}.png").write_bytes(BIG)
    return ws


def timeit(label, fn, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    print(f"  {label:<38} min {min(ts):7.1f} ms   max {max(ts):7.1f} ms")
    return min(ts)


ws = setup()

print("\n===== 1. 目录扫描 =====")
timeit("glob images/*.png （4 个文件）", lambda: list(ws.images_dir.glob("*.png")))
timeit("image_path('A')", lambda: ws.image_path("A"))

print("\n===== 2. Pillow 读文件头 =====")
from PIL import Image  # noqa: E402

def pil_all():
    for p in sorted(ws.images_dir.glob("*.png")):
        with Image.open(p) as im:
            _ = (im.size, im.format)

timeit("Pillow 读 4 张大图文件头", pil_all)

print("\n===== 3. unlink 大文件 vs 小文件 =====")
def cycle_big():
    (ws.images_dir / "X.png").write_bytes(BIG)
    (ws.images_dir / "X.png").unlink()

def cycle_small():
    (ws.images_dir / "X.png").write_bytes(b"y" * 2000)
    (ws.images_dir / "X.png").unlink()

timeit("写入+删除 3MB 文件", cycle_big)
timeit("写入+删除 2KB 文件", cycle_small)

print("\n===== 4. delete_image（glob + unlink）=====")
def del_big():
    (ws.images_dir / "A.png").write_bytes(BIG)
    ws.delete_image("A")

def del_small():
    (ws.images_dir / "A.png").write_bytes(b"y" * 2000)
    ws.delete_image("A")

timeit("delete_image 删 3MB", del_big)
timeit("delete_image 删 2KB", del_small)

print("\n===== 5. 单独量 unlink 那一下 =====")
for size, label in [(len(BIG), "3MB"), (2000, "2KB")]:
    samples = []
    for _ in range(6):
        f = ws.images_dir / "Y.png"
        f.write_bytes(b"z" * size)
        t0 = time.perf_counter()
        f.unlink()
        samples.append((time.perf_counter() - t0) * 1000)
    print(f"  unlink {label:<6} min {min(samples):7.1f} ms   max {max(samples):7.1f} ms")

shutil.rmtree(ROOT, ignore_errors=True)
