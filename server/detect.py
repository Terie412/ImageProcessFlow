# -*- coding: utf-8 -*-
"""「自动识别」：让多模态模型**只挑参数**，真正的边界交给确定性几何算法。

链路（见 DESIGN.md §3.8）：
    图 + 参数卡 ──► 多模态模型 ──► {"args": {...}, "reason": "…"}
                                        │
                                        ▼
                         extract_elements.py（纯几何切分）
                                        │
                                        ▼
                                  区域数组 [{x,y,w,h}]

为什么不让模型直接给坐标：它对像素级边界完全没概念，只会给个大概，
而切图这事儿差几个像素就是切到内容上。它擅长的是"看一眼就知道这图是
排列规整的图标表还是散乱排布"，于是让它只做这个判断。
"""
from __future__ import annotations

import ast
import base64
import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"

# 服务端自己控制的开关，不给模型碰
SERVER_FLAGS = {"image", "outdir", "crops", "debug"}


def script_docstring(script: Path) -> str:
    """取脚本自己的 docstring —— 它本来就是写给「怎么用这个脚本」的人看的。"""
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return (ast.get_docstring(tree) or "").strip()


def build_card(script: Path, args_spec: dict, notes: str = "") -> str:
    """把「脚本说明 + 可调参数表」拼成给模型看的参数卡。"""
    lines: list[str] = ["=== 脚本说明（节选自脚本自身的说明文档）==="]
    doc = script_docstring(script)
    if doc:
        lines.append(doc)
    lines.append("")
    lines.append("=== 你可以返回的参数（只能从这个表里挑，键名必须完全一致）===")
    lines.append("注意：只有参数说明里明确写了「0=自动」的，填 0 才表示自动；其余的别填 0。")
    for key, spec in args_spec.items():
        kind = spec.get("type", "int")
        if kind == "enum":
            rng = "可选值 " + " | ".join(str(o) for o in spec.get("options", []))
        else:
            rng = f"整数，{spec.get('min', 0)} ~ {spec.get('max', 10 ** 9)}"
        lines.append(f"- {key}: {rng}，默认 {spec.get('default')}\n    {spec.get('desc', '')}")
    if notes:
        lines.append("")
        lines.append("=== 挑选建议 ===")
        lines.append(notes.strip())
    return "\n".join(lines)


SYSTEM_TEMPLATE = """你是一个图像切分脚本的参数调优助手。

给你一张图，和一个「元素区域自动解析脚本」的说明与参数表。这个脚本的目标是：
在**不预先知道元素个数**的前提下，把图里互相分离的元素各自切出来。

你的任务**只有一件**：根据这张图长什么样，挑一组最合适的脚本参数。

硬性要求：
1. 只输出一个 JSON 对象，不要任何解释、前后缀、markdown 代码块。格式：
   {"args": {"参数名": 值, ...}, "reason": "一句话说明为什么这么选"}
2. "args" 里的键必须来自参数表，不要自己发明。
3. **绝对不要输出任何坐标、方框或区域信息** —— 边界由脚本自己算，你不需要也不许给。
4. 拿不准的参数就留默认值，别乱调；参数说明里没写「0=自动」的，就不要填 0。
5. reason 用中文，控制在 40 字以内。"""


def _loads_loose(text: str):
    """解析模型返回的 JSON，容忍几种常见的多嘴形式。"""
    candidates = [text]
    if "{{" in text or "}}" in text:
        candidates.append(text.replace("{{", "{").replace("}}", "}"))
    for c in candidates:
        i, j = c.find("{"), c.rfind("}")
        if i == -1 or j <= i:
            continue
        try:
            obj = json.loads(c[i : j + 1])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _data_url(path: str, max_side: int = 1600) -> str:
    """给模型的图：长边超过 max_side 就先缩一下（只影响模型「看」，不影响坐标精度）。"""
    raw = Path(path).read_bytes()
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            if max(w, h) > max_side:
                k = max_side / float(max(w, h))
                im = im.convert("RGBA").resize((max(1, int(w * k)), max(1, int(h * k))))
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                raw = buf.getvalue()
    except Exception:  # noqa: BLE001
        pass
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("utf-8")


def _coerce_args(raw: dict, spec: dict) -> tuple[dict, list[str]]:
    """按参数表校验模型给的值：类型不对/超范围就夹住，未知键直接丢掉。"""
    out: dict = {}
    warns: list[str] = []
    for key, s in spec.items():
        default = s.get("default")
        if key not in raw:
            out[key] = default
            continue
        v = raw[key]
        if s.get("type") == "enum":
            opts = s.get("options", [])
            out[key] = v if v in opts else default
            if out[key] is not default and v not in opts:
                warns.append(f"{key}={v!r} 不在可选值里，已用默认 {default}")
            continue
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            out[key] = default
            warns.append(f"{key}={v!r} 不是数字，已用默认 {default}")
            continue
        lo, hi = s.get("min", 0), s.get("max", 10 ** 9)
        if n < lo or n > hi:
            warns.append(f"{key}={n} 超出 {lo}~{hi}，已夹住")
        out[key] = max(lo, min(hi, n))
    unknown = [k for k in raw if k not in spec]
    if unknown:
        warns.append("忽略未知参数：" + "、".join(map(str, unknown)))
    return out, warns


def pick_args(api_key: str, model: str, card: str, image_path: str) -> tuple[dict, str]:
    """调多模态模型，返回 (原始 args, reason)。失败抛 RuntimeError。"""
    body = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_TEMPLATE},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{card}\n\n=== 这是要处理的图 ==="},
                    {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
                ],
            },
        ],
        "stream": False,
        # 和润色一样：这个是推理模型，思考过程也吃输出额度，不给足正文会空
        "max_tokens": 2048,
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
        text = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"响应结构异常：{json.dumps(result, ensure_ascii=False)[:600]}") from e
    if not text:
        if choice.get("finish_reason") == "length":
            raise RuntimeError("模型输出额度被思考过程用完了，没给出正文，请重试")
        raise RuntimeError("模型返回了空内容")

    if text.startswith("```"):
        text = text[3:]
        if "\n" in text:
            text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    # 只截取最外层那个 JSON 对象，防止模型前后多嘴
    obj = _loads_loose(text)
    if obj is None:
        raise RuntimeError(f"模型返回的 JSON 解析失败：{text[:300]}")

    raw_args = obj.get("args")
    if not isinstance(raw_args, dict):
        raw_args = {k: v for k, v in obj.items() if k != "reason"}
    reason = str(obj.get("reason") or "").strip()
    return raw_args, reason


def detect(api_key: str, model: str, script: Path, image_path: str, spec: dict, notes: str = ""):
    """完整一步：挑参数 → 校验。真正的切分由调用方去跑脚本（见 main.py）。"""
    card = build_card(script, spec, notes)
    raw_args, reason = pick_args(api_key, model, card, image_path)
    args, warns = _coerce_args(raw_args, spec)
    return args, reason, warns
