# -*- coding: utf-8 -*-
"""用文本模型润色生图提示词。

- 固定走 DeepSeek 的 OpenAI 兼容接口；DeepSeek 里能看图的只有
  `deepseek-v4-flash-vision-exp`（其他模型会返回 400 "This model does not support image"）
- API Key 由用户在设置里指定「环境变量名」，运行时从环境变量取，key 本身不落盘
- 输入 = 当前提示词 + 全部参考图 + 生图模型的特点说明（MODEL_NOTES.md）
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

try:  # Pillow 缺失时退化成「参考图原样发送」，润色本身照常可用
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"

# 透明区域垫的棋盘格：灰白相间、16px 一格（Photoshop 那套约定俗成的画法）。
#
# 为什么非画不可：视觉模型**看不见 alpha** —— 送进去的 PNG 在它的预处理里会被
# 压到一个不透明底色上，于是「透明底」在它眼里就是「白底」，它会把透明区域
# 当成背景色写进提示词（实测：同一张图问「背景什么颜色」→「白色、不透明」）。
# 把透明**画成棋盘格**它才「看得见」，再配上提示词里那句「棋盘格 = 透明」，
# 就不会再把透明底描述成白/浅灰/渐变背景了。
CHECKER_LIGHT = (255, 255, 255)
CHECKER_DARK = (203, 203, 203)
CHECKER_CELL = 16

# 只有在「至少有一张参考图带透明通道」时才追加进去（见 polish()）
TRANSPARENCY_RULES = """
7. 参考图里的**灰白相间棋盘格花纹代表「透明区域」**，不是画面内容 —— 那只是把透明画出来给你看。
8. 所以**不要把透明区域描述成任何颜色的背景**（别写「白色背景」「浅灰背景」「渐变背景」这类话）。
   除非用户明确要求一个背景，否则不要给画面补背景色。"""

SYSTEM_TEMPLATE = """你是一个图像生成提示词的润色助手。

用户正在用下面这个生图模型来生成或编辑图片。你要结合「这个模型的特点」和
「参考图的实际内容」，把用户写的提示词改写得更有效。

=== 生图模型的特点 ===
{notes}
=== 特点结束 ===

改写要求：
1. 只输出改写后的提示词本身。不要任何解释、前后缀、markdown 代码块或引号。
2. 保持用户的原始意图不变，不要替他改成别的任务。
3. 参考图里的内容必须写成**具体可指认的视觉描述**（例如「画面中央穿黑衣的人物」「顶部的状态栏」）。
   绝对不要用「背景」「前景」「UI」「上层元素」「其他内容」这类模型无法定位的抽象词 ——
   这是这个生图模型最容易翻车的地方。
4. 如果用户想删除某些东西，把每一项逐个点名写清楚，不要概括成「所有 UI」「多余的元素」。
5. 如果用户想保留某个区域，也要具体描述那个区域里有什么，而不是只说「保留背景」。
6. 用与用户输入相同的语言输出（中文输入就输出中文）。{transparency}"""


def _mime_of(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return mime


def has_alpha(im) -> bool:
    """源图**实际**有没有透明通道 —— 只看模式，不看文件格式。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def _checkerboard(w: int, h: int):
    """铺一块 2×2 格的小瓦片，再平铺到整张图 —— 逐像素循环太慢。"""
    c = CHECKER_CELL
    tile = Image.new("RGB", (2 * c, 2 * c), CHECKER_LIGHT)
    tile.paste(CHECKER_DARK, (0, 0, c, c))
    tile.paste(CHECKER_DARK, (c, c, 2 * c, 2 * c))
    board = Image.new("RGB", (w, h))
    for y in range(0, h, 2 * c):
        for x in range(0, w, 2 * c):
            board.paste(tile, (x, y))
    return board


def flatten_for_view(path: str) -> tuple[bytes, str, bool]:
    """参考图送给视觉模型前先「压到一个能看见的不透明底上」。

    带 alpha 的图 → 合成到**棋盘格**（返回 PNG、`composited=True`），这样透明区域
    在模型眼里是棋盘格、而不是白底。
    没有透明通道的图 → **原样返回文件字节**（合成只会是恒等操作，白白重新编码一遍；
    JPEG 还会因此变大很多）。读不了 / 动图 / Pillow 缺失都退化成原样发送。
    """
    raw = Path(path).read_bytes()
    if Image is None:
        return raw, _mime_of(path), False
    try:
        with Image.open(path) as opened:
            if getattr(opened, "n_frames", 1) > 1 or not has_alpha(opened):
                return raw, _mime_of(path), False
            rgba = opened.convert("RGBA")
    except Exception:  # noqa: BLE001  读不了就原样发，别让润色挂掉
        return raw, _mime_of(path), False

    board = _checkerboard(*rgba.size)
    board.paste(rgba, (0, 0), rgba)  # 用 alpha 当蒙版合成
    buf = io.BytesIO()
    board.save(buf, "PNG", optimize=True)
    return buf.getvalue(), "image/png", True


def _data_url(path: str) -> str:
    data, mime, _ = flatten_for_view(path)
    return f"data:{mime};base64," + base64.b64encode(data).decode("utf-8")


def read_notes(path: str | Path, model_id: str) -> str:
    """从 MODEL_NOTES.md 里取出对应模型那一节（标题 `## <模型 id>`）。"""
    if not model_id:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    keep: list[str] = []
    capturing = False
    for line in text.splitlines():
        if line.startswith("## "):
            capturing = line[3:].strip() == model_id
            continue
        if capturing:
            keep.append(line)
    return "\n".join(keep).strip()


def polish(api_key: str, model: str, prompt: str, image_paths: list[str], notes: str) -> str:
    """调用 DeepSeek 返回改写后的提示词。失败时抛 RuntimeError。"""
    lines = [f"【用户当前的提示词】\n{prompt}"]

    # 参考图：带透明的先合成到棋盘格（这样模型才「看得见」透明），其余原样发
    urls: list[str] = []
    composited = False
    for p in image_paths:
        data, mime, did = flatten_for_view(p)
        urls.append(f"data:{mime};base64," + base64.b64encode(data).decode("utf-8"))
        composited = composited or did

    if image_paths:
        refs = "、".join(f"「参考图{i + 1}」" for i in range(len(image_paths)))
        lines.append(f"\n下面是 {len(image_paths)} 张参考图，依次对应界面上的 {refs}。")
        if composited:
            lines.append("（参考图里灰白相间的棋盘格花纹是**透明区域**的标记，不是画面内容。）")
    else:
        lines.append("\n（这次没有参考图，是纯文字生成。）")

    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for u in urls:
        content.append({"type": "image_url", "image_url": {"url": u}})

    body = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_TEMPLATE.format(
                    notes=notes or "（还没有这个模型的特点记录）",
                    transparency=TRANSPARENCY_RULES if composited else "",
                ),
            },
            {"role": "user", "content": content},
        ],
        "stream": False,
        # DeepSeek 这个是**推理模型**：思考过程写在 reasoning_content 里、正文才在 content，
        # 两者共用输出额度。不显式给足，长一点的润色会在思考阶段就被截断、正文返回空。
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"文本模型返回 HTTP {e.code}：{detail[:600]}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"调用文本模型失败：{e}") from e

    try:
        choice = result["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"响应结构异常：{json.dumps(result, ensure_ascii=False)[:600]}") from e

    text = (message.get("content") or "").strip()
    if not text:
        # 推理模型被截断时，正文会是空的、内容全留在 reasoning_content 里
        if choice.get("finish_reason") == "length":
            raise RuntimeError("文本模型的输出额度被思考过程用完了，没来得及给出正文，请重试")
        raise RuntimeError("文本模型返回了空内容")

    # 有些模型习惯性包一层 ``` 代码块，剥掉
    if text.startswith("```"):
        text = text[3:]
        if "\n" in text:
            text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return text.strip()
