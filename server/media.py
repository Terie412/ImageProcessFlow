# -*- coding: utf-8 -*-
"""视频相关的本地能力：读元信息、抽一张缩略图。

## 为什么就这几行，还单独一个模块

视频在两个地方要被「看懂」：

1. **数据节点的界面** —— 要显示分辨率 / 时长 / 帧率，还要一张首帧缩略图；
2. **执行链路** —— 「尺寸 = 自动」要按源视频的宽高比挑预设。

两处都要 ffprobe / ffmpeg 这两条外部命令。如果各写一遍，就会出现「界面上算的是
显示宽高比、执行时按原始像素比」这类漂移；而且**没装 ffmpeg 时的报错文案**也会
各说各的。所以命令调用、解析、错误信息统一收在这里。

## 依赖

ffmpeg / ffprobe 必须在 PATH 里（README 的「环境要求」写了）。**不是 pip 依赖**，
也不打算打包进来 —— 一个静态 ffmpeg 有 100MB 上下，跟这个仓库的体积不成比例。

没装的时候不抛异常：`probe()` 返回 None、`poster()` 返回 False，界面退化成
「没有缩略图、没有时长」，节点本身照常能用。真正必须用到 ffmpeg 的只有
「AI 视频编辑」脚本，它自己会给出明确的前置提示。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def available() -> bool:
    """两条命令都在才算可用。"""
    return bool(FFMPEG and FFPROBE)


def _flags() -> int:
    """Windows 下不要弹黑框（引擎起子进程用的是同一套）"""
    return 0x08000000 if os.name == "nt" else 0


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=_flags(),
        )
    except BaseException:  # noqa: BLE001  超时 / 文件被占 / 命令不存在，一律当作拿不到
        return None


def probe(path: str | Path) -> dict | None:
    """读视频元信息：`{width, height, duration, fps, fpsRaw, frames}`。

    `fpsRaw` 是 ffprobe 原样给的分数（`30000/1001`），抽帧与合成时必须用**这个**，
    不能用四舍五入后的浮点数 —— 29.97 写成 30 会让 10 分钟的视频慢慢跑偏几秒。

    读不出来（不是视频 / 没装 ffprobe / 文件坏）返回 None，调用方按「没有信息」处理。
    """
    if not FFPROBE:
        return None
    r = _run(
        [
            FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration"
            ":format=duration",
            "-of", "json",
            str(path),
        ]
    )
    if r is None or r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None

    streams = data.get("streams") or []
    if not streams:
        return None
    st = streams[0] or {}
    fmt = data.get("format") or {}

    def num(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    # r_frame_rate 是容器里声明的基准帧率，avg_frame_rate 是实际平均帧率。
    # 可变帧率（VFR）两者会不一样：抽帧 / 合成按 avg 更贴近原片时长。
    raw = str(st.get("avg_frame_rate") or "")
    if not raw or raw.split("/")[0] in ("0", "N/A"):
        raw = str(st.get("r_frame_rate") or "")

    fps = 0.0
    if "/" in raw:
        a, b = raw.split("/", 1)
        fps = num(a) / (num(b) or 1)
    else:
        fps = num(raw)

    duration = num(fmt.get("duration")) or num(st.get("duration"))
    frames = None
    try:
        n = int(st.get("nb_frames"))
        frames = n if n > 0 else None
    except (TypeError, ValueError):
        pass
    if frames is None and fps > 0 and duration > 0:
        frames = int(round(fps * duration))

    return {
        "width": int(num(st.get("width"))) or None,
        "height": int(num(st.get("height"))) or None,
        "duration": round(duration, 3),
        "fps": round(fps, 3),
        "fpsRaw": raw or None,
        "frames": frames,
    }


def poster(src: str | Path, dest: str | Path, at: float = 0.0) -> bool:
    """从视频里抽一帧存成 jpg（宽度压到 640，够节点缩略图和大图查看器看）。

    `at` 默认 0 —— 取首帧。刻意**不做「取 10% 处」**那类小聪明：第一帧通常就是
    这张封面的本意，取别处反而会对不上用户心里的「这个视频长什么样」。

    失败返回 False（没装 ffmpeg / 文件读不了）。调用方不需要处理异常。
    """
    if not FFMPEG:
        return False
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except BaseException:  # noqa: BLE001
        return False
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-ss", f"{max(0.0, at):.3f}",
        "-i", str(src),
        "-frames:v", "1",
        # 竖屏视频按宽度 640 缩，`-2` 保证高度是偶数（libx264 / jpeg 都要求偶数）
        "-vf", "scale='min(640,iw)':-2",
        "-q:v", "4",
        str(dest),
    ]
    r = _run(cmd, timeout=60)
    return bool(r is not None and r.returncode == 0 and dest.is_file() and dest.stat().st_size > 0)


def refresh(resource: dict, media_file: Path, poster_file: Path | None = None) -> dict:
    """把一个**视频**数据节点的元信息补齐（就地改 `resource` 并返回它）。

    什么时候调：文件刚进来的时候（导入 / 执行产出）和替换文件的时候 —— 都是
    「文件变了」的那几个点。**不在 `/api/state` 里调**：每次刷新界面都跑两遍
    ffprobe 太贵，而且这些值不会自己变。

    顺手把 `media` 标成 `video`：前端靠它决定画缩略图还是 `<video>`。
    """
    resource["media"] = "video"
    info = probe(media_file)
    if info:
        resource["width"] = info["width"]
        resource["height"] = info["height"]
        resource["duration"] = info["duration"]
        resource["fps"] = info["fps"]
        resource["frames"] = info["frames"]
    if poster_file is not None and not poster_file.is_file():
        poster(media_file, poster_file)
    return resource
