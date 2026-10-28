# -*- coding: utf-8 -*-
"""视频链路的本地验证（**不起服务、不调用任何模型**）。

覆盖：

- `server/media.probe()` 读得对（宽高 / 帧率 / 时长）
- `server/media.poster()` 抽得出首帧，且是个能打开的 JPEG
- 文件池：视频和图片共用短名解析（`image_path` 认得 mp4）、缩略图跟着删除 / 复制
- `scripts_private/ai_video_edit` 的抽帧 - 合成流水线：**帧数、时长、帧率、音轨
  都跟源片一致**（用「原帧原样拼回去」当被测对象，不碰 AI 生成）

素材是现拿 ffmpeg 造的（`testsrc` + 正弦音轨），所以跑完不留东西。
没装 ffmpeg 时直接跳过并说明 —— 这不是失败。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_video.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import media  # noqa: E402
from workspace import Workspace  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts_private", "ai_video_edit", "ai_video_edit.py")

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)


def load_script():
    """把节点脚本当模块加载 —— 它的 main() 有 `__main__` 保护，import 无副作用。"""
    spec = importlib.util.spec_from_file_location("_verify_ai_video_edit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_source(dest: str) -> None:
    """1.5 秒 / 24fps / 320×240 的测试片，带一条正弦音轨。"""
    r = run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1.5",
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
    if not os.path.isfile(SCRIPT):
        print("跳过：scripts_private/ai_video_edit 不存在（私有节点不进 git）")
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

        # ---------------- 缩略图 ----------------
        print("\n[poster]")
        poster = os.path.join(tmp, "p.jpg")
        check(media.poster(src, poster), "抽得出首帧")
        check(os.path.getsize(poster) > 0, "缩略图非空")
        try:
            from PIL import Image

            with Image.open(poster) as im:
                check(im.format == "JPEG", f"是 JPEG（得到 {im.format}）")
                check(im.width <= 640, f"宽度压到 ≤640（得到 {im.width}）")
        except ImportError:
            print("  ..   Pillow 不可用，跳过格式检查")

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
        check(media.poster(src, target_poster), "海报落到 posters/ 目录")
        check(ws.poster_path(cid, "A") is not None, "poster_path 找得到")

        # 复制到另一个画布：媒体 + 海报都要跟着走
        ws.copy_image(cid, "c_other", "A", "B")
        check(ws.image_path("c_other", "B") is not None, "跨画布复制带上了视频")
        check(ws.poster_path("c_other", "B") is not None, "跨画布复制带上了缩略图")

        # 删除：媒体和海报一起清掉
        ws.delete_image(cid, "A")
        check(ws.image_path(cid, "A") is None, "删除后媒体文件没了")
        check(ws.poster_path(cid, "A") is None, "删除后缩略图也没了")

        # ---------------- 抽帧 / 合成 ----------------
        print("\n[抽帧 → 合成]")
        mod = load_script()
        frames = os.path.join(tmp, "frames")
        n = mod.extract_frames(src, __import__("pathlib").Path(frames))
        check(n == 36, f"1.5s × 24fps = 36 帧（得到 {n}）")

        edited = os.path.join(tmp, "edited")
        os.makedirs(edited, exist_ok=True)
        for name in sorted(os.listdir(frames)):
            shutil.copy2(os.path.join(frames, name), os.path.join(edited, name))

        out = os.path.join(tmp, "out.mp4")
        mod.compose(__import__("pathlib").Path(edited), src, out, info["fpsRaw"], 320, 240)
        back = media.probe(out)
        check(back is not None, "合成文件读得出来")
        check(back["width"] == 320 and back["height"] == 240, "输出尺寸不变")
        check(abs(back["fps"] - 24) < 0.01, f"输出帧率 24（得到 {back['fps']}）")
        check(abs(back["duration"] - 1.5) < 0.12, f"输出时长 1.5s（得到 {back['duration']}）")

        # 音轨还在？ffprobe 看有没有 a 流
        r = run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "json", out,
            ]
        )
        audio = json.loads(r.stdout or "{}").get("streams") or []
        check(bool(audio), "音轨被保留")
        if audio:
            check(audio[0].get("codec_name") == "aac", f"音频编码 aac（得到 {audio[0].get('codec_name')}）")
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
