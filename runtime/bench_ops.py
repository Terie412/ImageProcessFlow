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
# 图片池现在按画布分，这里随便开一个画布来量文件操作（量的是文件系统，不是画布逻辑）
CID = "c_bench"
# 源图：v1 工作目录在 workspace/images/，v2 在 workspace/canvases/<画布id>/images/
SRC = next(iter((APP / "workspace").glob("**/images/E.png")), None)
assert SRC, "找不到源图 workspace/**/images/E.png"
BIG = SRC.read_bytes()
print(f"测试图大小: {len(BIG)/1024/1024:.1f} MB")


def setup():
    shutil.rmtree(ROOT, ignore_errors=True)
    ws = Workspace(ROOT)
    ws.ensure()
    for nm in ["A", "B", "C", "D"]:
        (ws.image_dir(CID, create=True) / f"{nm}.png").write_bytes(BIG)
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
timeit("glob images/*.png （4 个文件）", lambda: list(ws.image_dir(CID).glob("*.png")))
timeit("image_path('A')", lambda: ws.image_path(CID, "A"))

print("\n===== 2. Pillow 读文件头 =====")
from PIL import Image  # noqa: E402

def pil_all():
    for p in sorted(ws.image_dir(CID).glob("*.png")):
        with Image.open(p) as im:
            _ = (im.size, im.format)

timeit("Pillow 读 4 张大图文件头", pil_all)

print("\n===== 3. unlink 大文件 vs 小文件 =====")
def cycle_big():
    (ws.image_dir(CID) / "X.png").write_bytes(BIG)
    (ws.image_dir(CID) / "X.png").unlink()

def cycle_small():
    (ws.image_dir(CID) / "X.png").write_bytes(b"y" * 2000)
    (ws.image_dir(CID) / "X.png").unlink()

timeit("写入+删除 3MB 文件", cycle_big)
timeit("写入+删除 2KB 文件", cycle_small)

print("\n===== 4. delete_image（glob + unlink）=====")
def del_big():
    (ws.image_dir(CID) / "A.png").write_bytes(BIG)
    ws.delete_image(CID, "A")

def del_small():
    (ws.image_dir(CID) / "A.png").write_bytes(b"y" * 2000)
    ws.delete_image(CID, "A")

timeit("delete_image 删 3MB", del_big)
timeit("delete_image 删 2KB", del_small)

print("\n===== 5. 单独量 unlink 那一下 =====")
for size, label in [(len(BIG), "3MB"), (2000, "2KB")]:
    samples = []
    for _ in range(6):
        f = ws.image_dir(CID) / "Y.png"
        f.write_bytes(b"z" * size)
        t0 = time.perf_counter()
        f.unlink()
        samples.append((time.perf_counter() - t0) * 1000)
    print(f"  unlink {label:<6} min {min(samples):7.1f} ms   max {max(samples):7.1f} ms")

shutil.rmtree(ROOT, ignore_errors=True)
