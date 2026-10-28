# -*- coding: utf-8 -*-
"""视频数据节点的本地验证（**不起服务、不调用任何模型**）。

覆盖：

- `server/media.probe()` 读得对（宽高 / 帧率 / 时长）
- `server/media.poster()` 抽得出首帧，是个能打开的 JPEG，宽视频会被压到 ≤640
- 文件池：视频和图片共用短名解析（`image_path` 认得 mp4）、封面跟着复制与删除

素材是现拿 ffmpeg 造的（`testsrc` + 正弦音轨），所以跑完不留东西。
没装 ffmpeg 时直接跳过并说明 —— 这不是失败。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_video.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import media  # noqa: E402
from workspace import Workspace  # noqa: E402

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)


def make_source(dest: str, size: str = "320x240") -> None:
    """1.5 秒 / 24fps 的测试片，带一条正弦音轨。"""
    r = run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration=1.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            dest,
        ]
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])


def main() -> int:
    if not media.available():
        print("跳过：PATH 里没有 ffmpeg / ffprobe（README 的「环境要求」）")
        return 0

    tmp = tempfile.mkdtemp(prefix="ipf_verify_video_")
    try:
        src = os.path.join(tmp, "src.mp4")
        make_source(src)

        # ---------------- 探测 ----------------
        print("\n[probe]")
        info = media.probe(src)
        check(info is not None, "probe 有结果")
        check(info["width"] == 320 and info["height"] == 240, f"宽高 320×240（得到 {info['width']}×{info['height']}）")
        check(abs(info["fps"] - 24) < 0.01, f"帧率 24（得到 {info['fps']}）")
        check(abs(info["duration"] - 1.5) < 0.1, f"时长 1.5s（得到 {info['duration']}）")
        check(info["frames"] == 36, f"帧数 36（得到 {info['frames']}）")

        # ---------------- 封面 ----------------
        print("\n[poster]")
        poster = os.path.join(tmp, "p.jpg")
        check(media.poster(src, poster), "抽得出首帧")
        check(os.path.getsize(poster) > 0, "封面非空")
        try:
            from PIL import Image

            with Image.open(poster) as im:
                check(im.format == "JPEG", f"是 JPEG（得到 {im.format}）")
                check(im.width <= 640, f"宽度压到 ≤640（得到 {im.width}）")
        except ImportError:
            print("  ..   Pillow 不可用，跳过格式检查")

        # 宽视频要被压到 640（小图不放大）
        wide_src = os.path.join(tmp, "wide.mp4")
        make_source(wide_src, "1280x720")
        wide_poster = os.path.join(tmp, "wide.jpg")
        check(media.poster(wide_src, wide_poster), "宽视频也抽得出")
        try:
            from PIL import Image

            with Image.open(wide_poster) as im:
                check(im.width == 640, f"1280 宽压到 640（得到 {im.width}）")
        except ImportError:
            pass

        # ---------------- 文件池 ----------------
        print("\n[workspace]")
        ws = Workspace(os.path.join(tmp, "ws"))
        ws.ensure()
        cid = "c_test"
        ws.image_dir(cid, create=True)
        shutil.copy2(src, ws.image_dir(cid) / "A.mp4")
        check(ws.image_path(cid, "A") is not None, "image_path 认得出 mp4")
        check(ws.image_path(cid, "A").suffix == ".mp4", "解析到的是 mp4 本体")
        target_poster = ws.poster_dir(cid, create=True) / "A.jpg"
        check(media.poster(src, target_poster), "封面落到 posters/ 目录")
        check(ws.poster_path(cid, "A") is not None, "poster_path 找得到")
        # 封面不能混进媒体池：`image_path` 按 `A.*` 找，放一起会被当成 A 的另一个版本
        check(ws.image_path(cid, "A") == ws.image_dir(cid) / "A.mp4", "封面没有污染媒体解析")

        # 复制到另一个画布：媒体 + 封面都要跟着走
        ws.copy_image(cid, "c_other", "A", "B")
        check(ws.image_path("c_other", "B") is not None, "跨画布复制带上了视频")
        check(ws.poster_path("c_other", "B") is not None, "跨画布复制带上了封面")

        # 删除：媒体和封面一起清掉
        ws.delete_image(cid, "A")
        check(ws.image_path(cid, "A") is None, "删除后媒体文件没了")
        check(ws.poster_path(cid, "A") is None, "删除后封面也没了")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
